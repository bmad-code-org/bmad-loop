"""CLI command tests — init policy-derived profiles and per-stage dry-run."""

import argparse
import io
import json
import sys

import pytest
import yaml
from conftest import (
    escalated_run,
    fault_read_text,
    git,
    install_bmad_config,
    install_build_auto_skill,
    install_dev_base_skills,
    install_dev_shim,
    machine_json,
    mark_ledger_done,
    spec_path,
    write_ledger,
    write_spec,
    write_sprint,
)

from bmad_loop import cli
from bmad_loop import policy as policy_mod
from bmad_loop.adapters import multiplexer as mux_mod

STORIES_SPEC_FOLDER = "_bmad-output/epic-1"


def _stories_entry(story_id, **over):
    d = {"id": story_id, "title": f"Story {story_id}", "description": "does a thing"}
    d.update(over)
    return d


def _setup_stories_fixture(paths, entries, *, with_spec_md=True):
    folder = paths.project / STORIES_SPEC_FOLDER
    (folder / "stories").mkdir(parents=True, exist_ok=True)
    if with_spec_md:
        (folder / "SPEC.md").write_text("# Epic 1\n", encoding="utf-8")
    (folder / "stories.yaml").write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    return folder


DUAL_CLIENT_POLICY = """\
[adapter]
name = "claude"
model = "opus"
[adapter.review]
name = "codex"
model = "gpt-5-codex"
"""


def _write_policy(project, text=DUAL_CLIENT_POLICY) -> None:
    bmad_loop_dir = project / ".bmad-loop"
    bmad_loop_dir.mkdir(parents=True, exist_ok=True)
    (bmad_loop_dir / "policy.toml").write_text(text)


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
    assert (tmp_path / ".claude" / "skills" / "bmad-loop-sweep" / "SKILL.md").is_file()


def test_init_no_skills_flag(tmp_path):
    assert cli.main(["init", "--project", str(tmp_path), "--no-skills"]) == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert not (tmp_path / ".claude" / "skills").exists()


def test_init_force_skills_flag(tmp_path):
    skill_md = tmp_path / ".claude" / "skills" / "bmad-loop-sweep" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("CUSTOM", encoding="utf-8")
    assert cli.main(["init", "--project", str(tmp_path), "--force-skills"]) == 0
    assert skill_md.read_text() != "CUSTOM"


def test_dry_run_renders_per_stage_commands(project, capsys):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args) == 0
    out = capsys.readouterr().out
    dev_line = next(line for line in out.splitlines() if "dev:" in line)
    review_line = next(line for line in out.splitlines() if "review:" in line)
    assert "claude" in dev_line and "--model opus" in dev_line
    assert review_line.split("review:")[1].strip().startswith("codex ")
    assert "--model gpt-5-codex" in review_line


def _shim_only(paths) -> None:
    """Post-rename install left with nothing but the forwarding shim, in every
    tree the dual-client policy reads."""
    import shutil

    from conftest import install_base_skills

    install_base_skills(paths)
    for tree in (".claude/skills", ".agents/skills"):
        shutil.rmtree(paths.project / tree / "bmad-build-auto")
        shutil.rmtree(paths.project / tree / "bmad-dev-auto")
        install_dev_shim(paths.project, tree)


def test_dry_run_warns_when_preflight_would_abort(project, capsys):
    """`--dry-run` returns before `_require_base_skills`, so a broken install still
    renders a plausible preview. On a shim-only project that preview is a lie the
    operator cannot see through — the shim IS a valid slash command, so
    `/bmad-dev-auto ...` reads fine and would HALT the session. Say so.

    stdout keeps the schedule (a diagnostic must not withhold what was asked for)
    and the exit code stays 0; the banner is stderr-only."""
    _shim_only(project)
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args) == 0
    out, err = capsys.readouterr()
    assert "NOT runnable" in err and "bmad-build-auto" in err
    assert "run `bmad-loop validate` for details" in err
    assert "1-1-a" in out  # the schedule itself still rendered


def test_dry_run_stories_warns_when_preflight_would_abort(project, capsys):
    """Same banner on the stories preview, which additionally probes folder+id
    dispatch support on the resolved primitive."""
    _shim_only(project)
    _setup_stories_fixture(project, [_stories_entry("1")])
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(spec=STORIES_SPEC_FOLDER, epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args, True, STORIES_SPEC_FOLDER) == 0
    out, err = capsys.readouterr()
    assert "NOT runnable" in err and "bmad-build-auto" in err
    assert "Story id: 1." in out


def test_sweep_dry_run_warns_when_preflight_would_abort(project, capsys):
    """`cmd_sweep` has the same shape — dry-run returns before its preflight. The
    banner is emitted BEFORE the no-ledger early return, so a project with a broken
    install and nothing to sweep still hears about the install."""
    _shim_only(project)
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")

    assert not project.deferred_work.is_file()  # the early-return leg
    assert cli._sweep_dry_run(project, pol) == 0
    assert "NOT runnable" in capsys.readouterr().err


def test_dry_run_is_silent_when_preflight_would_pass(project, capsys):
    """The banner must be evidence, not decoration: a complete install prints
    nothing to stderr. Without this the warning could be unconditional and every
    assertion above would still pass."""
    from conftest import install_base_skills

    install_base_skills(project)
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args) == 0
    out, err = capsys.readouterr()
    assert err == ""
    # ...and the preview spells the name that actually resolved: install_base_skills
    # lays down BOTH eras, so the tree resolves to the post-rename primitive.
    assert "/bmad-build-auto 1-1-a" in out


def test_dry_run_banner_stays_silent_for_a_warning_only_finding(project, capsys):
    """Severity decides, exactly as in `_require_base_skills`. That gate steps over
    non-problem findings — an unresolvable review layer is reported and the run
    proceeds — so listing one under a banner that claims "the real command aborts"
    would be a false alarm on a project that runs fine. A false "NOT runnable" is
    the wrong direction for a check with no severity filter and no `--force`.

    (The shipped 0.9.1 banner had no severity filter; this is a deliberate
    correction, not a port artefact.)"""
    from conftest import install_base_skills

    from bmad_loop import install

    install_base_skills(project)
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(epic=None, story=None, max_stories=None)

    # The stub installer writes every marker as placeholder text, so the primitive's
    # OWN customize.toml does not parse — and that is the one case `_merged_review_layers`
    # reads as "shape unknown", which short-circuits to the static catalog and returns no
    # finding at all. Give it a real layer so resolution SUCCEEDS; only then is an
    # override reached, and only then can an unparseable one be reported.
    layer = (
        "[[workflow.review_layers]]\n"
        'id = "adversarial"\n'
        'instruction = "Invoke the `bmad-review` skill with only the `adversarial` lens."\n'
    )
    for tree in cli._skill_trees(project.project, pol):
        primitive = project.project / tree / install.DEV_PRIMITIVE_NEW
        (primitive / "customize.toml").write_text(layer, encoding="utf-8")

    # an unparseable override layer: the resolver skips it and the run still goes
    custom = project.project / install.CUSTOMIZE_DIR
    custom.mkdir(parents=True, exist_ok=True)
    (custom / f"{install.DEV_PRIMITIVE_NEW}.toml").write_text("not = [toml\n", encoding="utf-8")

    findings = install.missing_base_skills(project.project, cli._skill_trees(project.project, pol))
    severities = {f.severity for f in findings}
    assert severities == {"warning"}, f"fixture no longer produces a warning-only set: {findings}"
    assert {f.check for f in findings} == {"skills.customize-unreadable"}

    assert cli._dry_run(project, pol, args) == 0
    assert capsys.readouterr().err == ""


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
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(epic=epic, story=story, max_stories=None)

    assert cli._dry_run(project, pol, args) == 0
    out = capsys.readouterr().out
    assert "3-1-user-auth" in out
    assert "3-2-foo" not in out and "4-1-bar" not in out


@pytest.mark.parametrize(
    "story,expected",
    [
        ("2-6a", ["2-6a-build-structure"]),
        ("2.6a", ["2-6a-build-structure"]),
        ("2-6a-build-structure", ["2-6a-build-structure"]),
        ("2-6", ["2-6a-build-structure", "2-6b-extend-structure"]),  # whole family
    ],
)
def test_dry_run_selects_split_story(project, capsys, story, expected):
    # split-story keys (issue #144): visible, selectable, and suffix-exact
    write_sprint(
        project,
        {
            "2-6a-build-structure": "backlog",
            "2-6b-extend-structure": "backlog",
            "2-7-later": "backlog",
        },
    )
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(epic=None, story=story, max_stories=None)

    assert cli._dry_run(project, pol, args) == 0
    out = capsys.readouterr().out
    assert [k for k in ("2-6a-build-structure", "2-6b-extend-structure") if k in out] == expected
    assert "2-7-later" not in out


def test_dry_run_warns_unknown_keys(project, capsys):
    write_sprint(
        project,
        {"1-1-a": "ready-for-dev", "totally-weird": "huh", "2-6.1-nested-split": "backlog"},
    )
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args) == 0
    err = capsys.readouterr().err
    assert "warning: ignoring unparseable sprint-status keys: " in err
    assert "totally-weird" in err and "2-6.1-nested-split" in err


def test_dry_run_reports_targeted_not_actionable(project, capsys):
    write_sprint(project, {"3-1-user-auth": "ready-for-dev", "3-2-foo": "done"})
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(epic=None, story="3-2", max_stories=None)

    assert cli._dry_run(project, pol, args) == 1
    err = capsys.readouterr().err
    assert "3-2 matched 3-2-foo" in err and "not actionable" in err


# ------------------------------------------------------------ stories mode


def test_stories_mode_forced_by_spec_flag():
    args = argparse.Namespace(spec="_bmad-output/epic-1")
    on, folder = cli._stories_mode(args, policy_mod.loads(""))
    assert on is True and folder == "_bmad-output/epic-1"


def test_stories_mode_from_policy_source():
    pol = policy_mod.loads('[stories]\nsource = "stories"\nspec_folder = "epic-2"\n')
    assert cli._stories_mode(argparse.Namespace(spec=None), pol) == (True, "epic-2")


def test_stories_mode_default_off():
    assert cli._stories_mode(argparse.Namespace(spec=None), policy_mod.loads("")) == (False, "")


def test_stories_mode_spec_flag_overrides_policy_sprint_source():
    # --spec forces stories mode even when policy says sprint-status
    args = argparse.Namespace(spec="_bmad-output/epic-9")
    on, folder = cli._stories_mode(args, policy_mod.loads(""))
    assert on and folder == "_bmad-output/epic-9"


def test_validate_stories_folder_ok(project):
    _setup_stories_fixture(project, [_stories_entry("1")])
    assert cli._validate_stories_folder(project, STORIES_SPEC_FOLDER) is None


def test_validate_stories_folder_missing_manifest(project):
    problem = cli._validate_stories_folder(project, STORIES_SPEC_FOLDER)
    assert problem is not None and "no stories.yaml found" in problem


def test_validate_stories_folder_missing_spec_md(project):
    _setup_stories_fixture(project, [_stories_entry("1")], with_spec_md=False)
    problem = cli._validate_stories_folder(project, STORIES_SPEC_FOLDER)
    assert problem is not None and "SPEC.md not found" in problem


def test_validate_stories_folder_invalid_manifest(project):
    _setup_stories_fixture(project, [_stories_entry("3"), _stories_entry("3", title="dup")])
    problem = cli._validate_stories_folder(project, STORIES_SPEC_FOLDER)
    assert problem is not None and "duplicate id" in problem


def test_dry_run_stories_prints_linear_schedule(project, capsys):
    _setup_stories_fixture(
        project,
        [
            _stories_entry("1", spec_checkpoint=True, done_checkpoint=True),
            _stories_entry("2"),
        ],
    )
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(spec=STORIES_SPEC_FOLDER, epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args, True, STORIES_SPEC_FOLDER) == 0
    out = capsys.readouterr().out
    assert "linear schedule" in out
    assert "Spec folder: _bmad-output/epic-1. Story id: 1." in out
    assert "Spec folder: _bmad-output/epic-1. Story id: 2." in out
    assert "spec-checkpoint" in out and "done-checkpoint" in out
    assert "BMAD_LOOP_SPEC_FOLDER=_bmad-output/epic-1" in out
    # pending on-disk state shown for an unstarted story
    assert "(pending)" in out


def test_dry_run_stories_filters_by_story_id(project, capsys):
    _setup_stories_fixture(project, [_stories_entry("1"), _stories_entry("2")])
    pol = policy_mod.loads("")
    args = argparse.Namespace(spec=STORIES_SPEC_FOLDER, epic=None, story="2", max_stories=None)
    assert cli._dry_run(project, pol, args, True, STORIES_SPEC_FOLDER) == 0
    out = capsys.readouterr().out
    assert "Story id: 2." in out and "Story id: 1." not in out


def test_run_warns_when_epic_passed_with_spec(project, capsys):
    """A4: --epic has no effect in stories mode (StoriesEngine nulls epic_filter), so
    `run --spec ... --epic N` must warn rather than silently ignore the flag. Exercised
    through cmd_run via --dry-run (the warning fires before the dry-run branch)."""
    install_bmad_config(project)
    _setup_stories_fixture(project, [_stories_entry("1"), _stories_entry("2")])
    _write_policy(project.project)
    rc = cli.main(
        [
            "run",
            "--project",
            str(project.project),
            "--spec",
            STORIES_SPEC_FOLDER,
            "--epic",
            "3",
            "--dry-run",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "--epic is ignored in stories mode" in err


def test_dry_run_stories_bad_folder_errors(project, capsys):
    pol = policy_mod.loads("")
    args = argparse.Namespace(spec=STORIES_SPEC_FOLDER, epic=None, story=None, max_stories=None)
    assert cli._dry_run(project, pol, args, True, STORIES_SPEC_FOLDER) == 1
    assert "no stories.yaml found" in capsys.readouterr().err


def test_stories_non_utf8_manifest_clean_error_not_crash(project, capsys):
    # A binary/non-UTF-8 stories.yaml must surface as a clean "stories mode: ... not valid
    # UTF-8" error at both preflight and dry-run — not an uncaught UnicodeDecodeError.
    folder = project.project / STORIES_SPEC_FOLDER
    (folder / "stories").mkdir(parents=True, exist_ok=True)
    (folder / "SPEC.md").write_text("# Epic 1\n", encoding="utf-8")
    (folder / "stories.yaml").write_bytes(b"\xff\xfe\x00\x01 not utf-8 \x80\x81")

    problem = cli._validate_stories_folder(project, STORIES_SPEC_FOLDER)
    assert problem is not None and "not valid UTF-8" in problem

    pol = policy_mod.loads("")
    args = argparse.Namespace(spec=STORIES_SPEC_FOLDER, epic=None, story=None, max_stories=None)
    assert cli._dry_run(project, pol, args, True, STORIES_SPEC_FOLDER) == 1
    err = capsys.readouterr().err
    assert "stories mode:" in err and "not valid UTF-8" in err


def _make_run_with_decision(project, run_id="20260101-000000-aaaa", dw_id="DW-1"):
    run_dir = project.project / ".bmad-loop" / "runs" / run_id
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


def _decisions_json(project, capsys, *extra_args, **kwargs):
    return machine_json(
        ["decisions", "--project", str(project.project), "--json", *extra_args], capsys, **kwargs
    )


def _make_run_with_rich_decision(project, run_id="20260101-000000-aaaa"):
    """A triage whose options populate every DecisionOption field, including the
    three (`intent`, `resolution`, `bundle_name`) the `--list` text never shows."""
    run_dir = project.project / ".bmad-loop" / "runs" / run_id
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
                "open_ids": ["DW-1"],
                "already_resolved": [],
                "bundles": [],
                "blocked": [],
                "skip": [],
                "decisions": [
                    {
                        "id": "DW-1",
                        "question": "build the widening?",
                        "context": "the parser only accepts ASCII keys",
                        "options": [
                            {
                                "key": "1",
                                "label": "Widen",
                                "effect": "build",
                                "intent": "accept unicode keys",
                                "bundle_name": "unicode-keys",
                            },
                            {
                                "key": "2",
                                "label": "Close",
                                "effect": "close",
                                "resolution": "ASCII is the documented contract",
                            },
                        ],
                        "recommendation": "2",
                    }
                ],
                "escalations": [],
            }
        ),
        encoding="utf-8",
    )


def test_decisions_json_emits_pure_document(project, capsys):
    """Every field round-trips, including the three the `--list` text drops:
    Decision.context, and each option's intent / resolution / bundle_name — the
    fields that decide what a sweep actually builds or writes, and which a
    caller answering by policy has no other way to read."""
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run_with_rich_decision(project)

    doc = _decisions_json(project, capsys, "--list")
    assert doc["schema_version"] == cli.DECISIONS_SCHEMA_VERSION == 1
    (decision,) = doc["decisions"]
    assert decision == {
        "id": "DW-1",
        "question": "build the widening?",
        "context": "the parser only accepts ASCII keys",
        "recommendation": "2",
        "options": [
            {
                "key": "1",
                "label": "Widen",
                "effect": "build",
                "intent": "accept unicode keys",
                "resolution": "",
                "bundle_name": "unicode-keys",
                "recommended": False,
            },
            {
                "key": "2",
                "label": "Close",
                "effect": "close",
                "intent": "",
                "resolution": "ASCII is the documented contract",
                "bundle_name": "",
                "recommended": True,
            },
        ],
    }


def test_decisions_json_marks_exactly_one_option_recommended(project, capsys):
    """`recommended` is derived from the recommendation key, so it can never
    disagree with it — and it lands on the recommended option, not merely the
    first. The text form encodes this as a suffix on a free-text line."""
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run_with_rich_decision(project)

    (decision,) = _decisions_json(project, capsys, "--list")["decisions"]
    recommended = [o for o in decision["options"] if o["recommended"]]
    assert [o["key"] for o in recommended] == [decision["recommendation"]] == ["2"]


def test_decisions_json_empty_is_valid_empty_document(project, capsys):
    """Nothing pending is a valid empty document with exit 0 — never the text
    "no unanswered decisions from past sweeps", which would corrupt the stream.
    Empty stdout is reserved for errors (same call `list --json` made in #192)."""
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "done 2026-06-01"})

    assert _decisions_json(project, capsys, "--list") == {"schema_version": 1, "decisions": []}


def test_decisions_json_without_list_emits_document_and_never_prompts(project, capsys, monkeypatch):
    """--json implies the listing: with pending decisions and no --list, the
    text form would construct the interactive DecisionPrompter and read stdin,
    which no pure document can survive. Both paths are booby-trapped, so a
    fall-through fails loudly here instead of hanging a caller's pipeline."""
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run_with_rich_decision(project)

    def _boom(*a, **k):
        raise AssertionError("--json must not reach the interactive prompter")

    monkeypatch.setattr("bmad_loop.sweep.DecisionPrompter", _boom)
    monkeypatch.setattr("builtins.input", _boom)

    doc = _decisions_json(project, capsys)  # no --list
    assert [d["id"] for d in doc["decisions"]] == ["DW-1"]


def test_decisions_json_config_error_leaves_stdout_empty(project, capsys):
    """A missing BMAD config exits 1 with the message on stderr and stdout
    empty — a consumer piping to a parser sees the failure in the exit code,
    never a partial or non-JSON document."""
    assert cli.main(["decisions", "--project", str(project.project), "--json", "--list"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "error:" in err


def test_decisions_answer_records_and_carries_forward(project, capsys, monkeypatch):
    from conftest import write_ledger

    from bmad_loop import decisions

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run_with_decision(project)

    class _StubPrompter:
        def ask(self, decision):
            return decision.option("1")  # choose build

    monkeypatch.setattr("bmad_loop.sweep.DecisionPrompter", lambda *a, **k: _StubPrompter())
    assert cli.main(["decisions", "--project", str(project.project)]) == 0
    out = capsys.readouterr().out
    assert "DW-1: queued" in out
    stored = decisions.load_pre_answers(project.project)
    assert stored["DW-1"]["effect"] == "build"
    # and it no longer shows as pending
    assert decisions.pending_missed_decisions(project.project) == []


def test_decisions_names_the_decision_a_bad_date_failed(project, capsys, monkeypatch):
    """`apply_pre_answer`'s `date` precondition raises `ValueError`, and the loop's
    handler must attribute it to the decision that did not land.

    Only the `could not record DW-1` assertion bites. `main`'s tail catches every
    exception this handler does — BmadConfigError by name, the rest through a bare
    `except Exception` — so deleting the handler still exits 1 and still prints the
    `date must be YYYY-MM-DD` text. Those two assertions are the message's shape,
    not the guard; the id prefix is the whole observable."""
    from conftest import write_ledger

    from bmad_loop import decisions

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run_with_decision(project)

    class _StubPrompter:
        def ask(self, decision):
            return decision.option("1")

    monkeypatch.setattr("bmad_loop.sweep.DecisionPrompter", lambda *a, **k: _StubPrompter())
    monkeypatch.setattr("bmad_loop.cli.time.strftime", lambda *_a: "13/06/2026")

    assert cli.main(["decisions", "--project", str(project.project)]) == 1

    captured = capsys.readouterr()
    assert "could not record DW-1" in captured.err
    assert "date must be YYYY-MM-DD" in captured.err
    assert decisions.load_pre_answers(project.project) == {}  # nothing recorded


def test_decisions_names_the_decision_a_broken_config_failed(project, capsys, monkeypatch):
    """The reachable half of the same handler, and the reason `BmadConfigError`
    belongs in its tuple beside the unreachable `ValueError`.

    `apply_pre_answer` re-reads the BMAD config on every call while `prompter.ask`
    blocks on the human, so the config can break *after* the read at the top of
    this command succeeded — reproduced here by removing it from inside `ask`
    rather than by patching `apply_pre_answer` to raise, so the live `load_paths`
    call is what fails.

    Asserted on the `DW-1` prefix, not on the exit code: `main` catches
    `BmadConfigError` by name, so without this tuple entry the command still exits
    1 and still prints the config message — just bare, after a run of per-decision
    outcome lines that then do not say which decision it belongs to."""
    from conftest import write_ledger

    from bmad_loop import decisions

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run_with_decision(project)
    config = project.project / "_bmad" / "bmm" / "config.yaml"

    class _StubPrompter:
        def ask(self, decision):
            config.unlink()  # the human broke it while this prompt was blocking
            return decision.option("1")

    monkeypatch.setattr("bmad_loop.sweep.DecisionPrompter", lambda *a, **k: _StubPrompter())

    assert cli.main(["decisions", "--project", str(project.project)]) == 1

    captured = capsys.readouterr()
    assert "could not record DW-1" in captured.err
    assert "BMAD config not found" in captured.err
    assert decisions.load_pre_answers(project.project) == {}  # nothing recorded


def test_status_surfaces_missed_decision_count(project, capsys):
    from conftest import write_ledger, write_sprint

    install_bmad_config(project)
    write_sprint(project, {})
    write_ledger(project, {"DW-1": "open"})
    _make_run_with_decision(project, run_id="20260102-000000-bbbb")
    # status needs a run to report; the decision run dir doubles as one
    assert cli.main(["status", "--project", str(project.project)]) == 0
    assert "decisions awaiting an answer: 1" in capsys.readouterr().out


def _make_run_with_tokens(project, tasks, *, weight, run_id="20260101-000000-aaaa"):
    """A finished run whose persisted snapshot pins cache_read_weight, so status
    assertions prove the weight came from state.json rather than the default."""
    from bmad_loop.journal import save_state
    from bmad_loop.model import RunState

    run_dir = project.project / ".bmad-loop" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    save_state(
        run_dir,
        RunState(
            run_id=run_id,
            project=str(project.project),
            started_at="2026-01-01T00:00:00",
            finished=True,
            tasks=tasks,
            policy_snapshot={"limits": {"cache_read_weight": weight}},
        ),
    )
    return run_dir


def _story_row(out, story_key="1-1-login"):
    """The per-story status line, split into fields — so token-cell assertions
    can't be satisfied by an unrelated dash elsewhere in the output (run ids
    are full of them)."""
    (line,) = [ln for ln in out.splitlines() if ln.startswith(f"  {story_key} ")]
    return line.split()


def test_status_shows_weighted_and_raw_tokens(project, capsys):
    """Budgets judge the weighted total; showing only raw overstated spend ~6.5x
    on cache-heavy runs. Same fixture numbers as the TUI test, deliberately, so
    the two surfaces are visibly the same case (#129)."""
    from bmad_loop.model import Phase, StoryTask, TokenUsage

    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.tokens = TokenUsage(
        input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
    )
    # 0.5, not the 0.1 default: with the default an assertion cannot tell
    # "read the snapshot" from "silently fell back".
    _make_run_with_tokens(project, {"1-1-login": task}, weight=0.5)

    assert cli.main(["status", "--project", str(project.project)]) == 0
    out = capsys.readouterr().out
    assert "660t (1,160 raw)" in out
    assert "tokens: 660 weighted (1,160 raw incl. cache reads, cache_read_weight 0.5)" in out


def test_status_zero_weight_shows_zero_not_dash(project, capsys):
    """With cache_read_weight=0 a cache-read-only story weighs 0 but is not
    untracked. "-" means no tokens at all; rendering 0 as "-" would read as
    missing data. Mirrors the TUI guard in tui/screens/dashboard.py."""
    from bmad_loop.model import Phase, StoryTask, TokenUsage

    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.tokens = TokenUsage(cache_read_tokens=1000)
    _make_run_with_tokens(project, {"1-1-login": task}, weight=0.0)

    assert cli.main(["status", "--project", str(project.project)]) == 0
    out = capsys.readouterr().out
    assert "0t (1,000 raw)" in out
    # the token cell itself is "0", not the "-" placeholder
    assert _story_row(out)[4] == "0t"


def test_status_omits_token_line_when_nothing_tracked(project, capsys):
    """usage_parser = "none" profiles never report usage — a totals line of
    zeros would assert free work rather than absent data."""
    from bmad_loop.model import Phase, StoryTask

    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    _make_run_with_tokens(project, {"1-1-login": task}, weight=0.1)

    assert cli.main(["status", "--project", str(project.project)]) == 0
    out = capsys.readouterr().out
    assert "tokens:" not in out
    # no tokens at all is exactly what "-" is reserved for
    assert _story_row(out)[4] == "-"


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


def _make_stories_run(project, run_id="20260101-000000-st01"):
    """A stories-mode run dir + state.json pinned to source=stories."""
    run_dir = project.project / ".bmad-loop" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": str(project.project),
                "started_at": "now",
                "source": "stories",
                "spec_folder": STORIES_SPEC_FOLDER,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def test_status_stories_mode_prints_board(project, capsys):
    from bmad_loop.stories import STORIES_SUBDIR

    _setup_stories_fixture(
        project, [_stories_entry("1", spec_checkpoint=True), _stories_entry("2")]
    )
    (project.project / STORIES_SPEC_FOLDER / STORIES_SUBDIR / "1-slug.md").write_text(
        "---\nstatus: done\n---\n", encoding="utf-8"
    )
    _make_stories_run(project)
    assert cli.main(["status", "--project", str(project.project)]) == 0
    out = capsys.readouterr().out
    assert "stories: 1/2 done" in out
    assert "spec-checkpoint" in out
    # the sprint-mode backlog line must not appear for a stories run
    assert "sprint backlog remaining" not in out


def test_status_stories_mode_bad_manifest_is_soft(project, capsys):
    # a stories run whose manifest is gone still prints the run header, not a crash
    _make_stories_run(project)
    assert cli.main(["status", "--project", str(project.project)]) == 0
    assert "no stories.yaml found" in capsys.readouterr().out


def test_emit_document_verifies_without_altering_the_bytes(capsys):
    """The helper half the --json commands write through: it must validate, and
    it must emit the ORIGINAL string — diagnose's leak self-check verified those
    exact bytes, so a re-serialization would ship bytes nothing checked."""
    from bmad_loop import machine

    # deliberately non-canonical: unsorted keys, 4-space indent, real non-ASCII.
    # A re-`json.dumps` would normalize every one of these away.
    original = '{\n    "z": 1,\n    "a": "café"\n}'
    machine.emit_document(original)
    out, _ = capsys.readouterr()
    assert out == original + "\n"  # byte-identical but for print's newline

    with pytest.raises(ValueError, match="malformed JSON document"):
        machine.emit_document('{"truncated": ')
    assert capsys.readouterr().out == ""  # refused before writing anything


def test_emit_document_writes_utf8_to_a_console_that_cannot_encode_the_document():
    """A document is not necessarily ASCII: `diagnostics.render_json` dumps with
    ensure_ascii=False so its leak guard can scan values unescaped, which lets a
    non-sensitive non-ASCII field through to stdout verbatim. On a console that
    cannot encode it — a legacy non-UTF-8 Windows one — that used to take the
    whole command down with UnicodeEncodeError before a byte was written (#200).

    The stream is widened to fit the document, not the document narrowed to fit
    the stream: re-dumping with ensure_ascii=True would emit bytes the leak
    check never saw, which is the one thing emit_document exists to prevent."""
    from bmad_loop import machine

    raw = io.BytesIO()
    # newline="" so the assertion below is byte-exact on Windows too, where the
    # default would translate print's \n to \r\n. Matches pytest's own CaptureIO.
    ascii_console = io.TextIOWrapper(raw, encoding="ascii", newline="")
    original = '{\n  "os_release": "5.15-café"\n}'
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "stdout", ascii_console)
        machine.emit_document(original)
        ascii_console.flush()

    written = raw.getvalue().decode("utf-8")
    assert written == original + "\n"  # verbatim, as on any other stream
    assert json.loads(written)["os_release"] == "5.15-café"


def test_emit_document_survives_a_stdout_that_cannot_be_reconfigured(capsys):
    """The UTF-8 switch is hasattr-guarded: a substituted stdout need not be a
    TextIOWrapper, and StringIO — no reconfigure, and no encoding to fail on —
    is the shape that proves the guard does not itself become the crash."""
    from bmad_loop import machine

    sink = io.StringIO()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "stdout", sink)
        machine.emit_document('{"a": "café"}')
    assert sink.getvalue() == '{"a": "café"}\n'
    assert capsys.readouterr().out == ""  # nothing leaked to the real stdout


def test_write_document_matches_the_stdout_bytes_and_validates(tmp_path, capsys):
    """`--out FILE` is the same contract aimed at a file, so it holds the same
    line: identical bytes to the stdout form, and the same refusal. Until it went
    through here the file was the weaker half — stdout refused a malformed
    document while write_text accepted it, which is backwards: the file is the one
    nobody eyeballs before feeding it to a parser."""
    from bmad_loop import machine

    original = '{\n    "z": 1,\n    "a": "café"\n}'
    machine.emit_document(original)
    piped = capsys.readouterr().out  # what `--json > FILE` would put in the file

    out_file = tmp_path / "doc.json"
    machine.write_document(out_file, original)
    assert out_file.read_text(encoding="utf-8") == piped  # byte-identical

    missing = tmp_path / "never.json"
    with pytest.raises(ValueError, match="malformed JSON document"):
        machine.write_document(missing, '{"truncated": ')
    assert not missing.exists()  # refused before the file was created


def _status_json(project, capsys, *extra_args):
    return machine_json(
        ["status", "--project", str(project.project), "--json", *extra_args], capsys
    )


def _list_json(project, capsys):
    return machine_json(["list", "--project", str(project.project), "--json"], capsys)


def test_status_json_emits_pure_document(project, capsys):
    from bmad_loop.model import Phase, StoryTask, TokenUsage

    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.tokens = TokenUsage(
        input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
    )
    _make_run_with_tokens(project, {"1-1-login": task}, weight=0.5)

    doc = _status_json(project, capsys)
    assert doc["schema_version"] == cli.STATUS_SCHEMA_VERSION == 1
    assert doc["run_id"] == "20260101-000000-aaaa"
    assert doc["run_type"] == "story"
    assert doc["source"] == "sprint-status"
    assert doc["started_at"] == "2026-01-01T00:00:00"
    assert doc["status"] == "finished"
    assert doc["finished"] is True
    assert doc["stopped"] is False
    assert doc["crashed"] is False
    assert doc["crash_error"] is None
    assert doc["paused_stage"] is None
    assert doc["paused_reason"] is None
    assert doc["paused_story_key"] is None
    (entry,) = doc["tasks"]
    assert entry["story_key"] == "1-1-login"
    assert entry["epic"] == 1
    assert entry["phase"] == "done"
    assert entry["attempt"] == 0
    assert entry["review_cycle"] == 0
    # same fixture numbers as the text test above (#129): weighted 660, raw 1160
    assert entry["tokens"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 1000,
        "cache_creation_tokens": 10,
        "raw": 1160,
        "weighted": 660,
    }
    # #153 phase 3: this fixture's snapshot has no [adapter] block and the task has
    # no stamped sessions, so both identity keys report their "nothing recorded"
    # form — None (not an all-claude default) run-level, {} per-task.
    assert doc["adapters"] is None
    assert entry["adapters_used"] == {}


def test_status_json_exposes_adapter_identity(project, capsys):
    """#153 phase 3: run-level ``adapters`` is the snapshot's configured-resolved
    identity for every role (matching AdapterPolicy.resolved, stage overrides and
    the client-switch model reset included), and per-task ``adapters_used`` is the
    last stamped session per role — the two are deliberately separate surfaces."""
    import json

    from bmad_loop import policy
    from bmad_loop.journal import save_state
    from bmad_loop.model import Phase, RunState, SessionRecord, StoryTask, TokenUsage

    # A real policy: base claude/opus, a review stage that switches client to codex
    # (which resets the resolved model to "" — the #189 client-switch rule), and a
    # pinned weight so the snapshot is a faithful json round-trip of asdict(Policy).
    pol = policy.loads(
        "[limits]\ncache_read_weight = 0.5\n"
        '[adapter]\nname = "claude"\nmodel = "opus"\n'
        '[adapter.review]\nname = "codex"\n'
    )
    snapshot = json.loads(json.dumps(pol.to_dict()))

    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.tokens = TokenUsage(input_tokens=10)
    # Two dev sessions: the LAST stamped one per role wins (a mid-run model swap).
    task.sessions = [
        SessionRecord(task_id="t", role="dev", status="ok", adapter="claude", model="sonnet"),
        SessionRecord(task_id="t", role="dev", status="ok", adapter="claude", model="opus"),
        SessionRecord(task_id="t", role="review", status="ok", adapter="codex", model=""),
    ]

    run_id = "20260101-000000-aaaa"
    run_dir = project.project / ".bmad-loop" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    save_state(
        run_dir,
        RunState(
            run_id=run_id,
            project=str(project.project),
            started_at="2026-01-01T00:00:00",
            finished=True,
            tasks={"1-1-login": task},
            policy_snapshot=snapshot,
        ),
    )

    doc = _status_json(project, capsys)
    assert doc["schema_version"] == cli.STATUS_SCHEMA_VERSION == 1
    # Run-level adapters resolve exactly as the live policy does, per role.
    assert doc["adapters"] == {
        role: {
            "name": pol.adapter.resolved(role).name,
            "model": pol.adapter.resolved(role).model,
        }
        for role in ("dev", "review", "triage")
    }
    # Concretely: dev/triage inherit claude/opus; the review client switch to codex
    # resets the model to "".
    assert doc["adapters"]["dev"] == {"name": "claude", "model": "opus"}
    assert doc["adapters"]["review"] == {"name": "codex", "model": ""}
    assert doc["adapters"]["triage"] == {"name": "claude", "model": "opus"}
    # Per-task used identity: the last dev session (opus, not the earlier sonnet)
    # and the one review session.
    (entry,) = doc["tasks"]
    assert entry["adapters_used"] == {
        "dev": {"name": "claude", "model": "opus"},
        "review": {"name": "codex", "model": ""},
    }


def test_status_json_adapter_identity_absent_for_pre_upgrade_runs(project, capsys):
    """A run persisted before adapter stamping: the snapshot carries no rebuildable
    [adapter] block (``adapters`` is None, never a synthesized all-claude default),
    and its sessions carry the empty-adapter sentinel (``adapters_used`` is {})."""
    from bmad_loop.model import Phase, SessionRecord, StoryTask, TokenUsage

    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.tokens = TokenUsage(input_tokens=10)
    # Pre-upgrade record: adapter defaults to "" (the phase-1 sentinel).
    task.sessions = [SessionRecord(task_id="t", role="dev", status="ok")]
    # _make_run_with_tokens pins only [limits].cache_read_weight — no [adapter].
    _make_run_with_tokens(project, {"1-1-login": task}, weight=0.1)

    doc = _status_json(project, capsys)
    assert doc["adapters"] is None
    (entry,) = doc["tasks"]
    assert entry["adapters_used"] == {}


def test_status_json_weight_from_snapshot_and_per_task_rounding(project, capsys):
    """The run-level weighted total must be the sum of per-task weighted totals
    (sum-of-rounds), never weighted_total of the summed counters: two tasks of
    101 cache reads at weight 0.5 weigh 50 + 50 = 100, not round(101) = 101.
    And weight 0.5 (not the 0.1 default) proves it came from the snapshot."""
    from bmad_loop.model import Phase, StoryTask, TokenUsage

    a = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    a.tokens = TokenUsage(input_tokens=10, cache_read_tokens=101)
    b = StoryTask(story_key="1-2-logout", epic=1, phase=Phase.DONE)
    b.tokens = TokenUsage(output_tokens=20, cache_read_tokens=101)
    _make_run_with_tokens(project, {"1-1-login": a, "1-2-logout": b}, weight=0.5)

    doc = _status_json(project, capsys)
    assert doc["cache_read_weight"] == 0.5
    entry_a, entry_b = doc["tasks"]
    assert entry_a["tokens"]["weighted"] == 60
    assert entry_b["tokens"]["weighted"] == 70
    assert doc["tokens"] == {"raw": 232, "weighted": 130}


def test_status_json_zero_weight_is_numeric_zero(project, capsys):
    """JSON has no "-" placeholder: a cache-read-only task at weight 0 emits a
    numeric 0 weighted next to its nonzero raw, so consumers can always tell
    "weighs nothing" from "no usage data" by the raw counters."""
    from bmad_loop.model import Phase, StoryTask, TokenUsage

    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.tokens = TokenUsage(cache_read_tokens=1000)
    _make_run_with_tokens(project, {"1-1-login": task}, weight=0.0)

    doc = _status_json(project, capsys)
    (entry,) = doc["tasks"]
    assert entry["tokens"]["weighted"] == 0
    assert entry["tokens"]["raw"] == 1000
    assert doc["tokens"] == {"raw": 1000, "weighted": 0}


def test_status_json_paused_run(project, capsys):
    from bmad_loop.journal import save_state
    from bmad_loop.model import PAUSE_ESCALATION, Phase, RunState, StoryTask

    run_id = "20260101-000000-aaaa"
    run_dir = project.project / ".bmad-loop" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    save_state(
        run_dir,
        RunState(
            run_id=run_id,
            project=str(project.project),
            started_at="2026-01-01T00:00:00",
            paused_reason="dev session escalated",
            paused_stage=PAUSE_ESCALATION,
            paused_story_key="1-1-login",
            tasks={"1-1-login": StoryTask(story_key="1-1-login", epic=1, phase=Phase.ESCALATED)},
            policy_snapshot={"limits": {"cache_read_weight": 0.1}},
        ),
    )

    doc = _status_json(project, capsys)
    assert doc["status"] == "paused"
    assert doc["paused_stage"] == "escalation"
    assert doc["paused_reason"] == "dev session escalated"
    assert doc["paused_story_key"] == "1-1-login"
    assert doc["finished"] is False


def test_status_json_defer_and_commit_are_separate_fields(project, capsys):
    """The text line's trailing cell is `defer_reason or commit_sha` — ambiguous
    free text, the core #190 complaint. The document keeps them apart."""
    from bmad_loop.model import Phase, StoryTask

    deferred = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DEFERRED)
    deferred.defer_reason = "attempts exhausted: 3 dev sessions"
    done = StoryTask(story_key="1-2-logout", epic=1, phase=Phase.DONE)
    done.commit_sha = "abc1234"
    _make_run_with_tokens(project, {"1-1-login": deferred, "1-2-logout": done}, weight=0.1)

    doc = _status_json(project, capsys)
    entry_deferred, entry_done = doc["tasks"]
    assert entry_deferred["phase"] == "deferred"
    assert entry_deferred["defer_reason"] == "attempts exhausted: 3 dev sessions"
    assert entry_deferred["commit_sha"] is None
    assert entry_done["phase"] == "done"
    assert entry_done["commit_sha"] == "abc1234"
    assert entry_done["defer_reason"] is None


def test_status_json_carries_the_preserve_ref(project, capsys):
    """#333: the recovery ref a rolled-back attempt was parked on is reported
    verbatim from state.json — status never runs git, so a ref a later run's
    `preserve_keep` pruning deleted is still named rather than silently dropped."""
    from bmad_loop.model import Phase, StoryTask

    deferred = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DEFERRED)
    deferred.defer_reason = "review did not converge within budget"
    deferred.preserve_ref = "refs/attempt-preserve-dirty/run-1-abcd1234-2"
    done = StoryTask(story_key="1-2-logout", epic=1, phase=Phase.DONE)
    _make_run_with_tokens(project, {"1-1-login": deferred, "1-2-logout": done}, weight=0.1)

    doc = _status_json(project, capsys)
    entry_deferred, entry_done = doc["tasks"]
    assert entry_deferred["preserve_ref"] == "refs/attempt-preserve-dirty/run-1-abcd1234-2"
    assert entry_done["preserve_ref"] is None


def test_status_text_names_the_preserve_ref_beside_the_reason(project, capsys):
    from bmad_loop.model import Phase, StoryTask

    deferred = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DEFERRED)
    deferred.defer_reason = "review did not converge within budget"
    deferred.preserve_ref = "attempt-preserve/run-1-abcd1234"
    _make_run_with_tokens(project, {"1-1-login": deferred}, weight=0.1)

    assert cli.main(["status", "--project", str(project.project)]) == 0
    out = capsys.readouterr().out
    assert "review did not converge within budget [attempt-preserve/run-1-abcd1234]" in out


def test_status_reports_a_parked_story_on_both_surfaces(project, capsys):
    """The `--json` projection is already phase-agnostic (`str(task.phase)`), so a
    new phase needs no schema change and no version bump — but "needs no change"
    is a claim about the contract, and this is what holds it: the parked task
    reports its own token, carries its commit, and is not confused with a defer.

    On the text side the phase cell is measured, not just matched:
    `awaiting-operator` is the longest phase token there is, and a field too
    narrow for it does not truncate — it pushes every following cell one column
    right on that one row, which reads as corrupt output rather than as a long
    status. `_story_row` splits on whitespace and so cannot see that; comparing
    the two rows' column offsets can.
    """
    from bmad_loop.model import Phase, StoryTask

    parked = StoryTask(story_key="1-1-login", epic=1, phase=Phase.AWAITING_OPERATOR)
    parked.commit_sha = "abc1234"
    parked.operator_actions = ["publish the DKIM TXT record"]
    done = StoryTask(story_key="1-2-logout", epic=1, phase=Phase.DONE)
    _make_run_with_tokens(project, {"1-1-login": parked, "1-2-logout": done}, weight=0.1)

    doc = _status_json(project, capsys)
    entry_parked, _ = doc["tasks"]
    assert entry_parked["phase"] == "awaiting-operator"
    assert entry_parked["commit_sha"] == "abc1234"  # parked work is committed work
    assert entry_parked["defer_reason"] is None  # a park is not a failure
    assert doc["schema_version"] == 1  # additive: no consumer breaks
    # the v1 document carries the phase, not the actions themselves — the
    # operator-actions index and `confirm --list` are their surface
    assert "operator_actions" not in entry_parked

    assert cli.main(["status", "--project", str(project.project)]) == 0
    out = capsys.readouterr().out
    assert _story_row(out)[1] == "awaiting-operator"
    (parked_line,) = [ln for ln in out.splitlines() if ln.startswith("  1-1-login ")]
    (done_line,) = [ln for ln in out.splitlines() if ln.startswith("  1-2-logout ")]
    assert parked_line.index("dev×") == done_line.index("dev×")


def test_status_json_stories_mode_is_pure_json(project, capsys):
    """--json must skip every text trailer (stories board, backlog, decisions
    nudge) — the stories-mode board would otherwise corrupt the document."""
    _setup_stories_fixture(project, [_stories_entry("1"), _stories_entry("2")])
    _make_stories_run(project)
    doc = _status_json(project, capsys)
    assert doc["source"] == "stories"
    assert doc["tasks"] == []


def test_status_json_error_paths_leave_stdout_empty(project, capsys):
    """Exit 1 with an empty stdout: a consumer piping to a JSON parser sees the
    failure in the exit code, never a partial or non-JSON document."""
    assert cli.main(["status", "--project", str(project.project), "--json"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "no runs found" in err

    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    assert cli.main(["status", "--project", str(project.project), "--json", "zzzz"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "no such run: zzzz" in err


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


def _make_list_run(project, run_id, **state_kwargs):
    """A run whose state.json pins one of the deterministic list statuses
    (finished/paused/stopped/crashed) — running/interrupted would probe pid
    liveness and flake."""
    from bmad_loop.journal import save_state
    from bmad_loop.model import RunState

    run_dir = project.project / ".bmad-loop" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    save_state(
        run_dir,
        RunState(run_id=run_id, project=str(project.project), **state_kwargs),
    )
    return run_dir


def test_list_json_emits_pure_document(project, capsys):
    from bmad_loop import runs

    _make_list_run(project, "20260101-000000-aaaa", started_at="2026-01-01T00:00:00", finished=True)
    _make_list_run(
        project,
        "20260102-000000-bbbb",
        started_at="2026-01-02T00:00:00",
        run_type="sweep",
        stopped=True,
    )

    doc = _list_json(project, capsys)
    assert doc["schema_version"] == cli.LIST_SCHEMA_VERSION == 1
    first, second = doc["runs"]  # oldest first, like the table
    assert first == {
        "ref": runs.short_ref("20260101-000000-aaaa"),
        "run_id": "20260101-000000-aaaa",
        "run_type": "story",
        "started_at": "2026-01-01T00:00:00",
        "status": "finished",
        "paused_stage": "",
    }
    assert second == {
        "ref": runs.short_ref("20260102-000000-bbbb"),
        "run_id": "20260102-000000-bbbb",
        "run_type": "sweep",
        "started_at": "2026-01-02T00:00:00",
        "status": "stopped",
        "paused_stage": "",
    }


def test_list_json_empty_runs_is_valid_empty_document(project, capsys):
    """No runs is a valid empty document with exit 0 — never the text
    "no runs found" (which would corrupt the stream; exit-code parity holds)."""
    doc = _list_json(project, capsys)
    assert doc == {"schema_version": 1, "runs": []}


def test_list_json_unparseable_state_reported_unknown(project, capsys):
    """A corrupt state.json still lists — enumeration scripts must see every
    run dir, same as the table."""
    run_dir = project.project / ".bmad-loop" / "runs" / "20260101-000000-cccc"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{not json", encoding="utf-8")

    doc = _list_json(project, capsys)
    (entry,) = doc["runs"]
    assert entry["ref"] == "cccc"
    assert entry["run_id"] == "20260101-000000-cccc"
    assert entry["run_type"] == "?"
    assert entry["started_at"] == ""
    assert entry["status"] == "unknown"
    assert entry["paused_stage"] == ""


def test_list_json_paused_run_carries_stage(project, capsys):
    """paused_stage is a bonus field the text table drops — emitted verbatim
    when paused."""
    from bmad_loop.model import PAUSE_ESCALATION

    _make_list_run(
        project,
        "20260101-000000-aaaa",
        started_at="2026-01-01T00:00:00",
        paused_reason="dev session escalated",
        paused_stage=PAUSE_ESCALATION,
    )

    doc = _list_json(project, capsys)
    (entry,) = doc["runs"]
    assert entry["status"] == "paused"
    assert entry["paused_stage"] == PAUSE_ESCALATION


# ---------------------------------------- documents.py as an imported library

# documents.py exists so a non-CLI frontend (the planned web backend) can import
# the builders and serialize them itself, never shelling out to the CLI to parse
# its stdout. These two pin that promise the only way that matters: drive one
# fixture down BOTH paths and assert the same dict comes back. Without them the
# library path has no coverage of its own — every other --json test reaches the
# builders through `cli.main`, so a change that reached `status --json` but not
# `status_document` (or the reverse) would land green and only break in a
# consumer. Equality is asserted against the RAW builder return, not a
# json round-trip: a consumer holds this dict before serializing it, so a tuple
# where a list belongs is a real defect the round-trip would hide.


def test_status_document_library_call_matches_the_cli(project, capsys):
    from bmad_loop.documents import status_document
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase, StoryTask, TokenUsage

    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.tokens = TokenUsage(
        input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
    )
    run_dir = _make_run_with_tokens(project, {"1-1-login": task}, weight=0.5)

    from_cli = _status_json(project, capsys)
    from_library = status_document(load_state(run_dir))

    assert from_library == from_cli


def test_list_document_library_call_matches_the_cli(project, capsys):
    # cmd_list sources its RunInfos the same lazy way — data.py has no textual
    # imports, so this does not drag the TUI into a library consumer's process.
    from bmad_loop.documents import list_document
    from bmad_loop.tui.data import discover_runs

    # Deterministic statuses only: running/interrupted probe pid liveness and
    # would flake (see _make_list_run).
    _make_list_run(project, "20260101-000000-aaaa", started_at="2026-01-01T00:00:00", finished=True)
    _make_list_run(
        project,
        "20260102-000000-bbbb",
        started_at="2026-01-02T00:00:00",
        run_type="sweep",
        stopped=True,
    )

    from_cli = _list_json(project, capsys)
    from_library = list_document(discover_runs(project.project))

    assert from_library == from_cli


def test_attach_records_return_pane_inside_tmux(project, monkeypatch):
    from bmad_loop.tui import launch

    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    monkeypatch.setattr(
        launch,
        "attach_plan",
        lambda proj, rid: (
            ["tmux", "switch-client", "-t", "=bmad-loop-ctl"],
            "=bmad-loop-ctl:sweep-RID",
        ),
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setattr(launch, "current_return_target", lambda: "=main:%3")
    recorded: list = []
    monkeypatch.setattr(launch, "set_return_pane", lambda w, p: recorded.append((w, p)))
    called: list = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: called.append(argv) or 0)

    assert cli.main(["attach", "--project", str(project.project), "20260101-000000-aaaa"]) == 0
    assert recorded == [("=bmad-loop-ctl:sweep-RID", "=main:%3")]
    assert called == [["tmux", "switch-client", "-t", "=bmad-loop-ctl"]]


def test_attach_records_detach_outside_tmux(project, monkeypatch):
    from bmad_loop.tui import launch

    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    monkeypatch.setattr(
        launch,
        "attach_plan",
        lambda proj, rid: (
            ["tmux", "attach", "-t", "=bmad-loop-ctl"],
            "=bmad-loop-ctl:sweep-RID",
        ),
    )
    monkeypatch.delenv("TMUX", raising=False)
    recorded: list = []
    monkeypatch.setattr(launch, "set_return_pane", lambda w, p: recorded.append((w, p)))
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: 0)

    assert cli.main(["attach", "--project", str(project.project), "20260101-000000-aaaa"]) == 0
    assert recorded == [("=bmad-loop-ctl:sweep-RID", launch.RETURN_DETACH)]


def test_attach_agent_session_records_no_return(project, monkeypatch):
    from bmad_loop.tui import launch

    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    monkeypatch.setattr(
        launch,
        "attach_plan",
        lambda proj, rid: (["tmux", "attach", "-t", "=bmad-loop-20260101-000000-aaaa"], None),
    )
    recorded: list = []
    monkeypatch.setattr(launch, "set_return_pane", lambda w, p: recorded.append((w, p)))
    called: list = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: called.append(argv) or 0)

    assert cli.main(["attach", "--project", str(project.project), "20260101-000000-aaaa"]) == 0
    assert recorded == []
    assert called == [["tmux", "attach", "-t", "=bmad-loop-20260101-000000-aaaa"]]


def test_attach_nothing_to_attach(project, monkeypatch, capsys):
    from bmad_loop.tui import launch

    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    monkeypatch.setattr(launch, "attach_plan", lambda proj, rid: None)

    assert cli.main(["attach", "--project", str(project.project), "20260101-000000-aaaa"]) == 1
    assert "nothing to attach" in capsys.readouterr().err


def test_attach_multiplexer_error_surfaces_clean_error(project, monkeypatch, capsys):
    # attach_plan reaches the multiplexer (a server round-trip on server-backed
    # backends like the external herdr adapter); when that raises, main()'s
    # backstop must surface `error: <msg>` + rc 1, never a traceback to the
    # parked control pane.
    from bmad_loop.adapters.multiplexer import MultiplexerError
    from bmad_loop.tui import launch

    def boom(_proj, _rid):
        raise MultiplexerError("backend server not reachable")

    _make_run_with_decision(project, run_id="20260101-000000-aaaa")
    monkeypatch.setattr(launch, "attach_plan", boom)
    assert cli.main(["attach", "--project", str(project.project), "20260101-000000-aaaa"]) == 1
    assert "error: backend server not reachable" in capsys.readouterr().err


def test_sweep_dry_run_lists_open_entries(project, capsys):
    from conftest import write_ledger

    write_ledger(project, {"DW-1": "open", "DW-2": "done 2026-06-01"}, commit=False)
    assert cli._sweep_dry_run(project, policy_mod.load(None)) == 0
    out = capsys.readouterr().out
    assert "1 open" in out
    assert "DW-1" in out and "DW-2" not in out
    triage_line = next(line for line in out.splitlines() if "triage:" in line)
    assert "bmad-loop-sweep" in triage_line


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
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    assert cli._sweep_dry_run(project, pol) == 0
    out = capsys.readouterr().out
    triage_line = next(line for line in out.splitlines() if "triage:" in line)
    assert triage_line.split("triage:")[1].strip().startswith("gemini ")
    # client switch: base model is claude-specific, must not leak into gemini
    assert "--model" not in triage_line


def test_sweep_dry_run_no_ledger(project, capsys):
    assert cli._sweep_dry_run(project, policy_mod.load(None)) == 0
    assert "no deferred-work ledger" in capsys.readouterr().out


def test_make_adapters_review_synthesizes_from_spec(project, monkeypatch):
    """Both dev AND review are bmad-dev-auto runs that write no result.json, so
    both roles must get the spec-synthesizing GenericDevAdapter; triage (a real
    result.json skill) stays a plain GenericAdapter."""
    from bmad_loop.adapters.generic import GenericAdapter, GenericDevAdapter

    monkeypatch.setattr(mux_mod, "_usable", lambda mux: True)
    install_bmad_config(project)
    adapters = cli._make_adapters(
        project.project, project.project / ".bmad-loop" / "runs" / "r", policy_mod.load(None)
    )
    assert isinstance(adapters["dev"], GenericDevAdapter)
    assert isinstance(adapters["review"], GenericDevAdapter)
    assert isinstance(adapters["triage"], GenericAdapter)
    assert not isinstance(adapters["triage"], GenericDevAdapter)


def test_make_adapters_hookless_synthesizing_roles_get_dev_adapter(project, monkeypatch):
    """Hookless dev/review (bmad-dev-auto roles) dispatch to OpencodeDevAdapter —
    the _DevSynthesisMixin composed over the HTTP transport — sharing one
    instance via the (cfg, synthesizes) key, while triage on the same config
    gets a separate plain OpencodeHttpAdapter (it reads a real result.json)."""
    from bmad_loop.adapters import opencode_http
    from bmad_loop.adapters.opencode_http import OpencodeDevAdapter, OpencodeHttpAdapter

    def no_mux():
        raise AssertionError("hookless adapters must not resolve a multiplexer")

    monkeypatch.setattr(opencode_http, "_require_httpx", lambda: object())
    monkeypatch.setattr(mux_mod, "get_multiplexer", no_mux)
    install_bmad_config(project)
    _write_policy(project.project, '[adapter]\nname = "opencode"\n')
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    adapters = cli._make_adapters(
        project.project, project.project / ".bmad-loop" / "runs" / "r", pol
    )
    assert isinstance(adapters["dev"], OpencodeDevAdapter)
    assert adapters["dev"] is adapters["review"]  # (cfg, synthesizes) sharing intact
    assert adapters["dev"].paths.project == project.project
    assert isinstance(adapters["triage"], OpencodeHttpAdapter)
    assert not isinstance(adapters["triage"], OpencodeDevAdapter)
    assert adapters["triage"] is not adapters["dev"]


def test_make_adapters_hookless_triage_dispatches_http_adapter(project, monkeypatch):
    """A hookless profile on a non-synthesizing role (triage) dispatches to the
    HTTP adapter — resolved via the `opencode` alias — while dev/review keep the
    shared spec-synthesizing tmux adapter. The HTTP adapter exposes `profile`
    (worktree provisioning keys off it) and never constructs a multiplexer."""
    from bmad_loop.adapters import opencode_http
    from bmad_loop.adapters.generic import GenericDevAdapter
    from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter

    monkeypatch.setattr(opencode_http, "_require_httpx", lambda: object())
    monkeypatch.setattr(mux_mod, "_usable", lambda mux: True)
    install_bmad_config(project)
    _write_policy(project.project, '[adapter.triage]\nname = "opencode"\n')
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    adapters = cli._make_adapters(
        project.project, project.project / ".bmad-loop" / "runs" / "r", pol
    )
    assert isinstance(adapters["triage"], OpencodeHttpAdapter)
    assert adapters["triage"].profile.name == "opencode-http"
    assert isinstance(adapters["dev"], GenericDevAdapter)
    assert adapters["dev"] is adapters["review"]  # (cfg, synthesizes) sharing intact


def test_make_adapters_refuses_unusable_mux(project, monkeypatch):
    """Selection's fallback rung returns a platform-matched backend even when it
    is unusable; the run bootstrap must refuse it so a run never drives a
    missing or version-gated multiplexer."""
    install_bmad_config(project)
    # isolate the forced legs: a developer-shell env var or a leaked policy pin
    # would flip backend_forced() and let this preflight stand down
    monkeypatch.delenv("BMAD_LOOP_MUX_BACKEND", raising=False)
    monkeypatch.setattr(mux_mod, "_CONFIGURED", None)
    monkeypatch.setattr(mux_mod, "_usable", lambda mux: False)
    with pytest.raises(SystemExit, match="not usable"):
        cli._make_adapters(
            project.project, project.project / ".bmad-loop" / "runs" / "r", policy_mod.load(None)
        )


def test_make_adapters_trusts_forced_backend(project, monkeypatch, capsys):
    """A forced name bypasses available() in selection; the run-bootstrap
    preflight must stand down for it the same way — but loudly (a version-gated
    binary works right up until the gated defect fires)."""
    from bmad_loop.adapters.multiplexer import get_multiplexer

    install_bmad_config(project)
    monkeypatch.setattr(mux_mod, "_usable", lambda mux: False)
    monkeypatch.setattr(mux_mod, "_FORCED_UNUSABLE_WARNED", False)
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "tmux")
    get_multiplexer.cache_clear()
    try:
        adapters = cli._make_adapters(
            project.project, project.project / ".bmad-loop" / "runs" / "r", policy_mod.load(None)
        )
    finally:
        get_multiplexer.cache_clear()  # don't leak the forced pick to other tests
    assert set(adapters) == set(cli.ROLES)
    assert "forced multiplexer backend" in capsys.readouterr().err


def test_make_adapters_trusts_settings_pinned_backend(project, monkeypatch, capsys):
    """The policy pin (`bmad-loop mux set`) is the second forced leg: the
    preflight stands down for it exactly like the env var, with the same
    warning."""
    from bmad_loop.adapters.multiplexer import configure_multiplexer

    install_bmad_config(project)
    monkeypatch.delenv("BMAD_LOOP_MUX_BACKEND", raising=False)
    monkeypatch.setattr(mux_mod, "_usable", lambda mux: False)
    monkeypatch.setattr(mux_mod, "_FORCED_UNUSABLE_WARNED", False)
    configure_multiplexer("tmux")
    try:
        adapters = cli._make_adapters(
            project.project, project.project / ".bmad-loop" / "runs" / "r", policy_mod.load(None)
        )
    finally:
        configure_multiplexer(None)  # also clears the selection cache
    assert set(adapters) == set(cli.ROLES)
    assert "forced multiplexer backend" in capsys.readouterr().err


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
    run_dir = project.project / ".bmad-loop" / "runs" / run_id
    assert json.loads((run_dir / "state.json").read_text())["run_id"] == run_id
    # engine.pid is "<pid>" or "<pid> <identity>" (identity persisted on platforms
    # that provide one) — assert on the pid token, not the whole line.
    assert (run_dir / "engine.pid").read_text().split()[0] == str(os.getpid())


_BAD_RUN_IDS = [
    "../../escape",  # traversal out of the runs dir
    "..",
    "/etc/passwd",  # posix absolute
    "C:\\windows",  # windows drive-absolute
    "a/b",  # path separators
    "a\\b",
    "a.b",  # dot/colon mangle a multiplexer session name
    "a:b",
    "-lead",  # leading dash
    "a b",  # whitespace
    "",  # empty
    "CON",  # reserved windows device basename
]


@pytest.mark.parametrize("bad", _BAD_RUN_IDS)
@pytest.mark.parametrize("command", ["run", "sweep"])
def test_start_rejects_invalid_run_id(project, monkeypatch, capsys, command, bad):
    """The hidden --run-id flag is a lookup key that becomes a directory name, a
    multiplexer session name and a git ref. Reject at the boundary — before any
    directory is created, and before the preflight side effects run."""
    install_bmad_config(project)
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    monkeypatch.setattr(cli, "SweepEngine", _StubEngine)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})

    # `--run-id=<bad>` (not a separate argv token) so argparse doesn't first reject
    # a leading-dash value as an unknown option — the guard is what must reject it.
    assert cli.main([command, "--project", str(project.project), f"--run-id={bad}"]) == 1
    err = capsys.readouterr().err
    # the stderr message is the real pin: an unguarded run/sweep would also return 1
    # here (dirty tree / missing base skills), just for the wrong reason.
    assert "invalid --run-id" in err and repr(bad) in err
    # rejected before the id reached a path: no run dir, inside or outside the runs dir
    assert not (project.project / ".bmad-loop" / "runs").exists()
    assert not (project.project / "escape").exists()


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
    import bmad_loop.tui.app as tui_app

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
    from bmad_loop.journal import save_state
    from bmad_loop.model import RunState

    run_dir = project / ".bmad-loop" / "runs" / run_id
    save_state(
        run_dir,
        RunState(run_id=run_id, project=str(project), started_at="now", **state_kwargs),
    )
    return run_dir


def test_stop_no_such_run(tmp_path, capsys):
    assert cli.main(["stop", "--project", str(tmp_path), "missing"]) == 1
    assert "no such run" in capsys.readouterr().err


def test_stop_marks_stopped(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_run_with_state(tmp_path, "r1")  # no pid -> fallback marks stopped
    assert cli.main(["stop", "--project", str(tmp_path), "r1"]) == 0
    assert "r1 stopped" in capsys.readouterr().out
    from bmad_loop.journal import load_state

    assert load_state(run_dir).stopped is True


def test_stop_already_finished(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    _make_run_with_state(tmp_path, "r1", finished=True)
    assert cli.main(["stop", "--project", str(tmp_path), "r1"]) == 1
    assert "already finished" in capsys.readouterr().err


# --- graceful stop: `stop --graceful` / `--cancel-graceful` + the status field ---


def _pending_graceful_run(tmp_path, run_id="r1", **state_kwargs):
    """A run with a graceful-stop control file already on disk. Written directly,
    not via runs.request_graceful_stop: that helper refuses without a live pid the
    test process can't fake, and the engine only ever checks the file's existence."""
    from bmad_loop import runs

    run_dir = _make_run_with_state(tmp_path, run_id, **state_kwargs)
    (run_dir / runs.STOP_REQUEST_FILE).write_text(
        '{"requested_at": "now", "mode": "graceful"}', encoding="utf-8"
    )
    return run_dir


def test_stop_graceful_requests_and_names_current_item(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs
    from bmad_loop.model import Phase, StoryTask

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    run_dir = _make_run_with_state(
        tmp_path,
        "r1",
        tasks={
            "1-1-done": StoryTask(story_key="1-1-done", epic=1, phase=Phase.DONE),
            "1-2-live": StoryTask(story_key="1-2-live", epic=1, phase=Phase.DEV_RUNNING),
        },
    )
    assert cli.main(["stop", "--project", str(tmp_path), "r1", "--graceful"]) == 0
    out = capsys.readouterr().out
    assert "graceful stop requested" in out
    assert "current item: 1-2-live" in out  # first non-terminal task, not the DONE one
    assert "bmad-loop resume r1" in out
    assert (run_dir / runs.STOP_REQUEST_FILE).is_file()


def test_stop_graceful_refuses_dead_engine(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "dead")
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["stop", "--project", str(tmp_path), "r1", "--graceful"]) == 1
    assert "no live engine" in capsys.readouterr().err
    assert not (run_dir / runs.STOP_REQUEST_FILE).exists()  # a refusal writes nothing


def test_stop_graceful_is_idempotent(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    run_dir = _pending_graceful_run(tmp_path)  # request already on disk
    before = (run_dir / runs.STOP_REQUEST_FILE).read_text()
    assert cli.main(["stop", "--project", str(tmp_path), "r1", "--graceful"]) == 0
    assert "already has a graceful stop pending" in capsys.readouterr().out
    # left untouched — the original request's timestamp stands
    assert (run_dir / runs.STOP_REQUEST_FILE).read_text() == before


def test_stop_cancel_graceful_clears_pending(tmp_path, capsys):
    from bmad_loop import runs

    run_dir = _pending_graceful_run(tmp_path)
    assert cli.main(["stop", "--project", str(tmp_path), "r1", "--cancel-graceful"]) == 0
    assert "cancelled" in capsys.readouterr().out
    assert not (run_dir / runs.STOP_REQUEST_FILE).exists()


def test_stop_cancel_graceful_without_pending_errors(tmp_path, capsys):
    _make_run_with_state(tmp_path, "r1")  # nothing on disk to cancel
    assert cli.main(["stop", "--project", str(tmp_path), "r1", "--cancel-graceful"]) == 1
    assert "no graceful stop pending" in capsys.readouterr().err


def test_stop_graceful_and_cancel_are_mutually_exclusive(tmp_path):
    # argparse rejects the pair at parse time, before cmd_stop runs — no run needed.
    with pytest.raises(SystemExit) as exc:
        cli.main(["stop", "--project", str(tmp_path), "r1", "--graceful", "--cancel-graceful"])
    assert exc.value.code == 2  # argparse usage error


def test_status_text_flags_graceful_stop_pending(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs
    from bmad_loop.model import Phase, StoryTask

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    _pending_graceful_run(
        tmp_path,
        tasks={"1-1-live": StoryTask(story_key="1-1-live", epic=1, phase=Phase.DEV_RUNNING)},
    )
    assert cli.main(["status", "--project", str(tmp_path), "r1"]) == 0
    assert "graceful stop pending" in capsys.readouterr().out


def test_status_json_graceful_stop_pending_true(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    _pending_graceful_run(tmp_path)
    doc = machine_json(["status", "--project", str(tmp_path), "r1", "--json"], capsys)
    assert doc["graceful_stop_pending"] is True
    assert doc["schema_version"] == 1  # additive field — no schema bump


def test_status_json_graceful_stop_pending_false_without_request(tmp_path, capsys):
    # no control file -> the cheap existence check short-circuits the liveness probe
    _make_run_with_state(tmp_path, "r1")
    doc = machine_json(["status", "--project", str(tmp_path), "r1", "--json"], capsys)
    assert doc["graceful_stop_pending"] is False
    assert doc["schema_version"] == 1


def test_delete_refuses_live_run_without_force(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["delete", "--project", str(tmp_path), "r1"]) == 1
    assert "stop it first" in capsys.readouterr().err
    assert run_dir.exists()


def test_delete_force_stops_then_removes(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

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
    from bmad_loop import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")

    def _raise(_rd):
        raise runs.StopRunError("boom")

    monkeypatch.setattr(runs, "stop_run", _raise)
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["delete", "--project", str(tmp_path), "r1", "--force"]) == 1
    assert "boom" in capsys.readouterr().err
    assert run_dir.exists()


def test_archive_force_stop_error_blocks(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs
    from bmad_loop.process_host import ProcessHostError

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
    from bmad_loop import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "unknown")
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["delete", "--project", str(tmp_path), "r1"]) == 0
    assert "unverifiable pid" in capsys.readouterr().err
    assert not run_dir.exists()


def test_archive_unknown_warns_but_proceeds(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "unknown")
    run_dir = _make_run_with_state(tmp_path, "20260611-100000-aaaa")
    assert cli.main(["archive", "--project", str(tmp_path), "20260611-100000-aaaa"]) == 0
    assert "unverifiable pid" in capsys.readouterr().err
    assert not run_dir.exists()


def test_archive_creates_tarball_and_removes_run(tmp_path, capsys):
    run_dir = _make_run_with_state(tmp_path, "20260611-100000-aaaa")
    assert cli.main(["archive", "--project", str(tmp_path), "20260611-100000-aaaa"]) == 0
    out = capsys.readouterr().out
    dest = tmp_path / ".bmad-loop" / "archive" / "20260611-100000-aaaa.tar.gz"
    assert "archived to" in out
    assert dest.is_file()
    assert not run_dir.exists()


def test_archive_refuses_live_run_without_force(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["archive", "--project", str(tmp_path), "r1"]) == 1
    assert "stop it first" in capsys.readouterr().err
    assert run_dir.exists()


def _write_bmad_config(project, impl="{project-root}/artifacts"):
    """Minimal _bmad/bmm/config.yaml — restore-patch validation resolves the
    artifact roots through bmadconfig.load_paths, like every real project."""
    cfg = project / "_bmad" / "bmm"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text(
        f"implementation_artifacts: '{impl}'\nplanning_artifacts: '{{project-root}}/planning'\n",
        encoding="utf-8",
    )


def _escalated_run(project, run_id="r1", *, story="s1", spec_file=None, worktree_path=""):
    """conftest's builder with this module's shape: only the run_dir comes back (the
    CLI tests drive the real `resolve` command and re-load state from disk)."""
    return escalated_run(
        project, run_id, story_key=story, spec_file=spec_file, worktree_path=worktree_path
    ).run_dir


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
    from bmad_loop import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: liveness)
    _escalated_run(tmp_path, "r1")
    assert cli.main(["resolve", "--project", str(tmp_path), "r1"]) == 1
    err = capsys.readouterr().err
    assert msg in err
    if liveness == "unknown":
        assert "--force" in err  # the refusal carries the recovery instructions


def test_resolve_force_alive_still_refuses(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "alive")
    _escalated_run(tmp_path, "r1")
    assert cli.main(["resolve", "--project", str(tmp_path), "r1", "--force"]) == 1
    assert "is still live — stop it first" in capsys.readouterr().err


def test_resolve_force_unknown_proceeds(tmp_path, monkeypatch, capsys):
    # --force is the only escape from a squatted/unverifiable pid: `stop` cannot
    # clear 'unknown' (engine.pid is never deleted), so without it resolve would
    # refuse forever.
    from bmad_loop import runs
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

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
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

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


def test_resolve_echoes_this_rearms_stale_restore_events(tmp_path, monkeypatch, capsys):
    """#90's journal entries reach the operator. The commits variant is warn-only —
    stderr is the only place it ever surfaces. Entries from *earlier* re-arms are
    already-acted-on history and must not be replayed."""
    from bmad_loop import runs
    from bmad_loop.journal import Journal

    run_dir = _escalated_run(tmp_path, "r1")
    Journal(run_dir).append("stale-restore-excluded", story_key="s1", files=["FROM-LAST-TIME.txt"])

    def fake_rearm(rd, key, *, restore_patch=None):
        journal = Journal(rd)
        journal.append("stale-restore-excluded", story_key=key, patch="a.patch", files=["new.txt"])
        journal.append("stale-restore-unparseable", story_key=key, patch="b.patch", error="OSErr")
        journal.append("stale-restore-commits", story_key=key, old_baseline="f" * 40, commits=["c"])
        return key

    monkeypatch.setattr(runs, "rearm_escalation", fake_rearm)
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: 0)
    assert (
        cli.main(["resolve", "--project", str(tmp_path), "r1", "--no-interactive", "--resume"]) == 0
    )

    err = capsys.readouterr().err
    assert "excluded the abandoned restore's new files from the re-drive baseline: new.txt" in err
    assert "could not read the abandoned restore patch (b.patch)" in err
    assert "1 commit(s) sit below the re-drive's new baseline (ffffffffffff..)" in err
    assert "FROM-LAST-TIME.txt" not in err


def test_resolve_interactive_runs_session_then_rearms(tmp_path, monkeypatch):
    from bmad_loop import resolve
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

    _escalated_run(tmp_path, "r1")
    calls = {}
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {"dev": object()})
    monkeypatch.setattr(resolve, "build_context", lambda *a, **k: calls.setdefault("ctx", True))
    monkeypatch.setattr(
        resolve, "run_session", lambda *a, **k: calls.setdefault("session", True) or True
    )
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: 0)
    run_dir = tmp_path / ".bmad-loop" / "runs" / "r1"
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--resume"])
    assert rc == 0
    assert calls == {"ctx": True, "session": True}
    assert load_state(run_dir).tasks["s1"].phase == Phase.PENDING


def test_resolve_interactive_unsupported_adapter(tmp_path, monkeypatch, capsys):
    from bmad_loop import resolve

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
    from bmad_loop.tui import launch

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


def test_resolve_no_interactive_restore_patch_latches_in_review(tmp_path, monkeypatch, capsys):
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    _write_bmad_config(tmp_path)
    patch = tmp_path / "artifacts" / "attempt.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("diff", encoding="utf-8")
    run_dir = _escalated_run(tmp_path, "r1", spec_file=str(spec))

    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: 0)
    rc = cli.main(
        [
            "resolve",
            "--project",
            str(tmp_path),
            "r1",
            "--no-interactive",
            "--restore-patch",
            str(patch),
            "--resume",
        ]
    )
    assert rc == 0
    assert "restoring the attempted change" in capsys.readouterr().out
    task = load_state(run_dir).tasks["s1"]
    assert task.phase == Phase.PENDING
    assert task.restore_patch == str(patch.resolve())  # validated + latched (absolute)
    assert "in-review" in spec.read_text()  # restore mode routes step-01 -> step-04


def test_resolve_restore_patch_missing_file_rejected(tmp_path, monkeypatch, capsys):
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    _write_bmad_config(tmp_path)
    run_dir = _escalated_run(tmp_path, "r1", spec_file=str(spec))
    called: list = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: called.append(rd) or 0)
    rc = cli.main(
        [
            "resolve",
            "--project",
            str(tmp_path),
            "r1",
            "--no-interactive",
            "--restore-patch",
            str(tmp_path / "nope.patch"),
            "--resume",
        ]
    )
    assert rc == 1
    assert "not a file under the project" in capsys.readouterr().err
    assert called == []  # never resumed
    assert load_state(run_dir).tasks["s1"].phase == Phase.ESCALATED  # not re-armed


def test_resolve_restore_patch_outside_project_rejected(tmp_path, monkeypatch, capsys):
    """A patch that EXISTS but sits outside the project root is rejected the same
    as a missing one: an absolute path escaping the workspace must never be
    latched (the engine would lay whatever it points at onto the tree)."""
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

    project = tmp_path / "proj"
    project.mkdir()
    _write_bmad_config(project)
    outside = tmp_path / "outside.patch"  # a real file, wrong side of the fence
    outside.write_text("diff", encoding="utf-8")
    spec = project / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    run_dir = _escalated_run(project, "r1", spec_file=str(spec))
    called: list = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: called.append(rd) or 0)
    rc = cli.main(
        [
            "resolve",
            "--project",
            str(project),
            "r1",
            "--no-interactive",
            "--restore-patch",
            str(outside),
            "--resume",
        ]
    )
    assert rc == 1
    assert "not a file under the project" in capsys.readouterr().err
    assert called == []  # never resumed
    task = load_state(run_dir).tasks["s1"]
    assert task.phase == Phase.ESCALATED and task.restore_patch is None  # not re-armed


def test_resolve_restore_patch_in_outside_project_artifacts_allowed(tmp_path, monkeypatch):
    """Artifact dirs configured OUTSIDE the project tree are a supported layout
    (bmadconfig keeps them absolute; verify special-cases them throughout), and
    bmad-dev-auto saves the intent-gap patch in implementation_artifacts — so a
    patch there is a legitimate restore target: the containment check uses the
    same trusted roots as spec_within_roots, not a bare under-project test."""
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

    project = tmp_path / "proj"
    project.mkdir()
    shared = tmp_path / "shared-artifacts"  # sibling dir, outside the project tree
    shared.mkdir()
    _write_bmad_config(project, impl=str(shared))
    patch = shared / "attempt.patch"
    patch.write_text("diff", encoding="utf-8")
    spec = project / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    run_dir = _escalated_run(project, "r1", spec_file=str(spec))

    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: 0)
    rc = cli.main(
        [
            "resolve",
            "--project",
            str(project),
            "r1",
            "--no-interactive",
            "--restore-patch",
            str(patch),
            "--resume",
        ]
    )
    assert rc == 0
    task = load_state(run_dir).tasks["s1"]
    assert task.phase == Phase.PENDING
    assert task.restore_patch == str(patch.resolve())  # latched despite being out-of-project
    assert "in-review" in spec.read_text()


def test_resolve_restore_patch_worktree_isolation_rejected(tmp_path, monkeypatch, capsys):
    """B4d: restore is an in-place-only recovery — a worktree-isolation re-drive
    discards and re-mounts the unit's worktree, so a latched patch would silently
    never restore. Rejected up front: no re-arm, no latch, spec untouched."""
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

    _write_policy(tmp_path, '[scm]\nisolation = "worktree"\n')
    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    patch = tmp_path / "artifacts" / "attempt.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("diff", encoding="utf-8")
    run_dir = _escalated_run(tmp_path, "r1", spec_file=str(spec))
    called: list = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: called.append(rd) or 0)
    rc = cli.main(
        [
            "resolve",
            "--project",
            str(tmp_path),
            "r1",
            "--no-interactive",
            "--restore-patch",
            str(patch),
            "--resume",
        ]
    )
    assert rc == 1
    assert "worktree" in capsys.readouterr().err
    assert called == []  # never resumed
    task = load_state(run_dir).tasks["s1"]
    assert task.phase == Phase.ESCALATED and task.restore_patch is None  # not re-armed
    assert "status: blocked" in spec.read_text()  # spec status untouched


def test_resolve_restore_patch_flag_rejected_before_the_session(tmp_path, monkeypatch, capsys):
    """The explicit --restore-patch flag is fully knowable pre-session (isolation
    mode, path containment), so on a worktree-isolation run it must be rejected
    BEFORE launching the interactive resolve agent — not after a whole
    conversation the abort would throw away."""
    from bmad_loop import resolve

    _write_policy(tmp_path, '[scm]\nisolation = "worktree"\n')
    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    _write_bmad_config(tmp_path)
    patch = tmp_path / "artifacts" / "attempt.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("diff", encoding="utf-8")
    _escalated_run(tmp_path, "r1", spec_file=str(spec))

    def never(*a, **k):
        raise AssertionError("interactive resolve session launched despite a doomed restore")

    monkeypatch.setattr(cli, "_make_adapters", never)
    monkeypatch.setattr(resolve, "run_session", never)
    # interactive is the default: no --no-interactive here
    rc = cli.main(
        ["resolve", "--project", str(tmp_path), "r1", "--restore-patch", str(patch), "--resume"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "worktree" in err
    assert "--no-interactive" in err  # the deterministic escape is named


def test_resolve_restore_patch_rejected_for_worktree_executed_task(tmp_path, monkeypatch, capsys):
    """The guard keys on the recorded run state too: a task that actually
    executed in a worktree (task.worktree_path) rejects a restore even when the
    policy has since been flipped back to in-place — the patch was saved inside
    the (discarded) worktree, so there is nothing durable to restore."""
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    _write_bmad_config(tmp_path)
    patch = tmp_path / "artifacts" / "attempt.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("diff", encoding="utf-8")
    run_dir = _escalated_run(
        tmp_path, "r1", spec_file=str(spec), worktree_path=str(tmp_path / "wt" / "s1")
    )
    called: list = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: called.append(rd) or 0)
    rc = cli.main(
        [
            "resolve",
            "--project",
            str(tmp_path),
            "r1",
            "--no-interactive",
            "--restore-patch",
            str(patch),
            "--resume",
        ]
    )
    assert rc == 1
    assert "worktree" in capsys.readouterr().err
    assert called == []
    assert load_state(run_dir).tasks["s1"].phase == Phase.ESCALATED  # not re-armed


def test_resolve_interactive_restore_patch_from_resolution_json(tmp_path, monkeypatch):
    from bmad_loop import resolve
    from bmad_loop.journal import load_state

    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    _write_bmad_config(tmp_path)
    patch = tmp_path / "artifacts" / "attempt.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("diff", encoding="utf-8")
    run_dir = _escalated_run(tmp_path, "r1", spec_file=str(spec))

    def fake_session(adapter, project, rd, story_key, *, model=""):
        # the resolve agent records a restore_patch in its output marker
        marker = resolve.resolution_path(rd, story_key)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"restore_patch": str(patch)}), encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {"dev": object()})
    monkeypatch.setattr(resolve, "build_context", lambda *a, **k: None)
    monkeypatch.setattr(resolve, "run_session", fake_session)
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: 0)
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--resume"])
    assert rc == 0
    task = load_state(run_dir).tasks["s1"]
    assert task.restore_patch == str(patch.resolve())  # picked up from resolution.json
    assert "in-review" in spec.read_text()


def test_resolve_corrupt_resolution_json_aborts_loudly(tmp_path, monkeypatch, capsys):
    """A present-but-unparseable resolution.json may carry the agent's recorded
    restore decision, and a re-arm consumes the escalation — so corruption must
    abort (no re-arm, no resume), never silently downgrade to a from-scratch
    re-drive quieter than an absent marker."""
    from bmad_loop import resolve
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    run_dir = _escalated_run(tmp_path, "r1", spec_file=str(spec))

    def fake_session(adapter, project, rd, story_key, *, model=""):
        marker = resolve.resolution_path(rd, story_key)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"restore_patch": "artifacts/attempt.patch",}', encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {"dev": object()})
    monkeypatch.setattr(resolve, "build_context", lambda *a, **k: None)
    monkeypatch.setattr(resolve, "run_session", fake_session)
    called: list = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: called.append(rd) or 0)
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--resume"])
    assert rc == 1
    assert "unreadable" in capsys.readouterr().err
    assert called == []  # never resumed
    task = load_state(run_dir).tasks["s1"]
    assert task.phase == Phase.ESCALATED and task.restore_patch is None  # not re-armed
    assert "status: blocked" in spec.read_text()  # spec untouched


def test_resolve_empty_restore_patch_field_aborts_loudly(tmp_path, monkeypatch, capsys):
    """`"restore_patch": ""` in resolution.json (schema says omit the field) is a
    corrupted recorded decision, not "no restore" — abort, don't re-arm."""
    from bmad_loop import resolve
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    run_dir = _escalated_run(tmp_path, "r1", spec_file=str(spec))

    def fake_session(adapter, project, rd, story_key, *, model=""):
        marker = resolve.resolution_path(rd, story_key)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"restore_patch": ""}), encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {"dev": object()})
    monkeypatch.setattr(resolve, "build_context", lambda *a, **k: None)
    monkeypatch.setattr(resolve, "run_session", fake_session)
    called: list = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: called.append(rd) or 0)
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--resume"])
    assert rc == 1
    assert "invalid" in capsys.readouterr().err
    assert called == []
    assert load_state(run_dir).tasks["s1"].phase == Phase.ESCALATED  # not re-armed


def test_resolve_empty_restore_patch_flag_aborts_loudly(tmp_path, monkeypatch, capsys):
    """`--restore-patch ""` (unset shell variable) must abort, not silently
    re-drive from scratch: the flag said restore, and a re-arm would consume the
    escalation with the decision dropped."""
    from bmad_loop.journal import load_state
    from bmad_loop.model import Phase

    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    run_dir = _escalated_run(tmp_path, "r1", spec_file=str(spec))
    called: list = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: called.append(rd) or 0)
    rc = cli.main(
        [
            "resolve",
            "--project",
            str(tmp_path),
            "r1",
            "--no-interactive",
            "--restore-patch",
            "",
            "--resume",
        ]
    )
    assert rc == 1
    assert "empty path" in capsys.readouterr().err
    assert called == []
    assert load_state(run_dir).tasks["s1"].phase == Phase.ESCALATED  # not re-armed


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
    from bmad_loop import runs
    from bmad_loop.tui import launch

    monkeypatch.setattr(launch, "prunable_ctl_windows", lambda _proj: ["sweep-fin-1"])
    dry_runs: list[bool] = []
    monkeypatch.setattr(
        runs,
        "prune_sessions",
        lambda _proj, dry_run=False: dry_runs.append(dry_run) or (["fin-1"], ["live-1"], set()),
    )

    assert cli.main(["cleanup", "--project", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would kill session bmad-loop-fin-1" in out
    assert "would close ctl window sweep-fin-1" in out
    assert "leaving 1 live session(s) untouched" in out
    assert dry_runs == [True]  # one partition sample, with the kill suppressed


def test_cleanup_prunes_sessions_and_windows(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs
    from bmad_loop.tui import launch

    monkeypatch.setattr(runs, "prune_sessions", lambda _proj, dry_run=False: (["fin-1"], [], set()))
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _proj: ["sweep-fin-1"])

    assert cli.main(["cleanup", "--project", str(tmp_path)]) == 0
    assert "removed 1 session(s), 1 ctl window(s)" in capsys.readouterr().out


def test_cleanup_warns_per_unknown_session(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs
    from bmad_loop.tui import launch

    monkeypatch.setattr(
        runs, "prune_sessions", lambda _proj, dry_run=False: (["fin-1", "odd-1"], [], {"odd-1"})
    )
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _proj: [])

    assert cli.main(["cleanup", "--project", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "run odd-1: engine may still be live (unverifiable pid)" in captured.err
    assert "fin-1: engine may still be live" not in captured.err  # only unknown ids warn
    assert "removed 2 session(s), 0 ctl window(s)" in captured.out


def test_cleanup_json_dry_run_plans_without_pruning(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs
    from bmad_loop.tui import launch

    monkeypatch.setattr(launch, "prunable_ctl_windows", lambda _proj: ["sweep-fin-1"])
    # a real prune would have to go through prune_ctl_windows; leaving it
    # unpatched proves --dry-run never reaches it
    dry_runs: list[bool] = []
    monkeypatch.setattr(
        runs,
        "prune_sessions",
        lambda _proj, dry_run=False: dry_runs.append(dry_run) or (["fin-1"], ["live-1"], set()),
    )

    doc = machine_json(["cleanup", "--project", str(tmp_path), "--dry-run", "--json"], capsys)

    assert doc["schema_version"] == cli.CLEANUP_SCHEMA_VERSION
    assert doc["dry_run"] is True
    assert doc["sessions"] == {"removed": ["fin-1"], "live": ["live-1"], "unverifiable_pid": []}
    assert doc["ctl_windows"] == {"removed": ["sweep-fin-1"]}
    assert dry_runs == [True]  # the kill stayed suppressed


def test_cleanup_json_real_run_reports_what_it_did(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs
    from bmad_loop.tui import launch

    monkeypatch.setattr(runs, "prune_sessions", lambda _proj, dry_run=False: (["fin-1"], [], set()))
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _proj: ["sweep-fin-1"])

    doc = machine_json(["cleanup", "--project", str(tmp_path), "--json"], capsys)

    assert doc["dry_run"] is False
    assert doc["sessions"]["removed"] == ["fin-1"]
    assert doc["ctl_windows"]["removed"] == ["sweep-fin-1"]


def test_cleanup_json_carries_unverifiable_pid_with_empty_stderr(tmp_path, monkeypatch, capsys):
    # the text mode's per-session stderr warning becomes a document field;
    # machine_json's default asserts stderr is empty, which is what is tested.
    from bmad_loop import runs
    from bmad_loop.tui import launch

    monkeypatch.setattr(
        runs, "prune_sessions", lambda _proj, dry_run=False: (["fin-1", "odd-1"], [], {"odd-1"})
    )
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _proj: [])

    doc = machine_json(["cleanup", "--project", str(tmp_path), "--json"], capsys)

    assert doc["sessions"]["removed"] == ["fin-1", "odd-1"]
    assert doc["sessions"]["unverifiable_pid"] == ["odd-1"]  # only unknown ids


def test_cleanup_json_nothing_to_clean_up_is_a_valid_empty_document(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs
    from bmad_loop.tui import launch

    monkeypatch.setattr(runs, "prune_sessions", lambda _proj, dry_run=False: ([], [], set()))
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _proj: [])

    doc = machine_json(["cleanup", "--project", str(tmp_path), "--json"], capsys)

    assert doc["schema_version"] == cli.CLEANUP_SCHEMA_VERSION
    assert doc["sessions"] == {"removed": [], "live": [], "unverifiable_pid": []}
    assert doc["ctl_windows"] == {"removed": []}


def test_resume_kills_stale_session_before_running(project, monkeypatch):
    from conftest import install_base_skills

    from bmad_loop import runs

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

    from bmad_loop import runs

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


# --------------------------------------------------- resume re-stamps the snapshot (#189)

# 0.5, never the 0.1 default: with the default an assertion cannot tell "read the
# re-stamped snapshot" from "fell back to RunState.cache_read_weight's default".
# `gates.mode` and `verify.commands` are non-defaults, so a whole-tree stamp is
# distinguishable from a surgical poke at the weight.
RESUME_POLICY = """\
[gates]
mode = "none"

[verify]
commands = ["true"]

[limits]
cache_read_weight = 0.5
"""
LAUNCH_SNAPSHOT = {"limits": {"cache_read_weight": 0.1}}


def _paused_run_for_resume(project, monkeypatch, *, snapshot=LAUNCH_SNAPSHOT, **state_kwargs):
    """A paused run plus a real policy.toml, wired so `_resume_paused_run` drives
    a stub engine for real. `snapshot=None` omits `policy_snapshot` entirely —
    a legacy run persisted before the field existed."""
    from conftest import install_base_skills

    from bmad_loop import runs

    install_bmad_config(project)
    install_base_skills(project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    _write_policy(project.project, RESUME_POLICY)
    if snapshot is not None:
        state_kwargs["policy_snapshot"] = snapshot
    run_dir = _make_run_with_state(
        project.project,
        "20990101-000000-beef",
        paused_reason="escalation",
        paused_stage="escalation",
        **state_kwargs,
    )
    monkeypatch.setattr(runs, "kill_session", lambda rid: None)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})
    return run_dir


def _state_reading_engine(seen):
    """A stub engine that records state.json as it stood when the engine started.
    _StubEngine never saves, so anything `seen` contains was written by the CLI
    before `run()` — which is the actual requirement, since `status`, the TUI and
    `diagnose` can only read the file."""

    class _Engine(_StubEngine):
        def __init__(self, **kwargs):
            self._run_dir = kwargs["run_dir"]

        def run(self):
            from bmad_loop.journal import load_state

            seen.append(load_state(self._run_dir))
            return super().run()

    return _Engine


def _resume_entries(run_dir):
    from bmad_loop.journal import Journal

    return [e for e in Journal(run_dir).entries() if e["kind"] == "run-resume"]


def _resume_entry(run_dir):
    (entry,) = _resume_entries(run_dir)
    return entry


def test_resume_restamps_policy_snapshot_before_the_engine_runs(project, monkeypatch):
    """#189: resume reloads policy.toml and enforces it (the per-story budget,
    every SessionSpec) but used to leave the launch-time snapshot in place, so
    every display read a weight the budget was not using — silently up to 10x off
    at the legal extremes. The stamp must also be *durable before* the engine
    starts, since Engine._save() may not fire for minutes."""
    seen: list = []
    run_dir = _paused_run_for_resume(project, monkeypatch)
    monkeypatch.setattr(cli, "Engine", _state_reading_engine(seen))

    assert cli._resume_paused_run(project.project, run_dir) == 0

    (at_start,) = seen
    assert at_start.cache_read_weight() == 0.5  # was 0.1 — the launch-time value
    assert at_start.paused is False


def test_resume_restamps_the_whole_policy_not_just_the_weight(project, monkeypatch):
    """The snapshot backs the diagnose `policy` block too, not only the weight,
    so the stamp is the whole tree — a surgical poke at limits.cache_read_weight
    would pass the weight assertions and still ship a stale bundle."""
    from bmad_loop.journal import load_state

    run_dir = _paused_run_for_resume(project, monkeypatch)
    monkeypatch.setattr(cli, "Engine", _StubEngine)

    assert cli._resume_paused_run(project.project, run_dir) == 0

    snapshot = load_state(run_dir).policy_snapshot
    assert snapshot["limits"]["cache_read_weight"] == 0.5
    assert snapshot["gates"]["mode"] == "none"  # non-default, straight from policy.toml
    assert snapshot["verify"]["commands"] == ["true"]
    assert "adapter" in snapshot  # a section policy.toml never mentions


def test_resume_restamps_policy_snapshot_for_sweep_runs(project, monkeypatch):
    """The sweep arm of the resume branch rebuilds a SweepEngine down a separate
    path — fixing only the story arm would leave sweeps displaying stale."""
    from bmad_loop.journal import load_state

    run_dir = _paused_run_for_resume(project, monkeypatch, run_type="sweep")
    monkeypatch.setattr(cli, "SweepEngine", _StubEngine)

    assert cli._resume_paused_run(project.project, run_dir) == 0

    assert load_state(run_dir).cache_read_weight() == 0.5


def test_resume_tolerates_a_corrupt_sweep_json(project, monkeypatch):
    """A torn/corrupt sweep.json (a crash mid-write on an older run) must not abort
    resume — the recovery path. compose_resume guards the read and falls back to the
    same launch defaults as the missing-file arm instead of letting json.loads raise."""
    run_dir = _paused_run_for_resume(project, monkeypatch, run_type="sweep")
    (run_dir / "sweep.json").write_text("{ not json", encoding="utf-8")

    captured: dict = {}

    class _CapturingSweep(_StubEngine):
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(cli, "SweepEngine", _CapturingSweep)

    # resume does not raise, and the sweep is rebuilt with default options
    assert cli._resume_paused_run(project.project, run_dir) == 0
    assert captured["prompting"] is False
    assert captured["decisions_only"] is False
    assert captured["max_bundles"] is None


def test_resume_stamps_a_legacy_run_with_no_snapshot(project, monkeypatch):
    """A run persisted before policy_snapshot existed carries `{}` and displays at
    the hardcoded 0.1 default. Its first resume stamps it — and must not report
    that as a policy *change*, since there is no prior policy to have changed."""
    from bmad_loop.journal import load_state

    run_dir = _paused_run_for_resume(project, monkeypatch, snapshot=None)
    monkeypatch.setattr(cli, "Engine", _StubEngine)

    assert cli._resume_paused_run(project.project, run_dir) == 0

    state = load_state(run_dir)
    assert state.policy_snapshot != {}
    assert state.cache_read_weight() == 0.5
    assert _resume_entry(run_dir)["policy_changed"] is False


def test_status_after_resume_shows_the_edited_weight(project, monkeypatch, capsys):
    """The user-visible bug, end to end: edit the weight, resume, and `status`
    used to keep reporting the launch-time total (260) while the budget judged
    the run at the edited one (660)."""
    from bmad_loop.model import Phase, StoryTask, TokenUsage

    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.tokens = TokenUsage(
        input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
    )
    run_dir = _paused_run_for_resume(project, monkeypatch, tasks={"1-1-login": task})
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    assert cli._resume_paused_run(project.project, run_dir) == 0
    capsys.readouterr()

    assert cli.main(["status", "--project", str(project.project)]) == 0
    out = capsys.readouterr().out
    assert "tokens: 660 weighted (1,160 raw incl. cache reads, cache_read_weight 0.5)" in out


def test_resume_journals_the_weight_it_is_resuming_under(project, monkeypatch):
    """Re-stamping re-weights the run's whole history, so `session-end` entries
    written before the resume stop being reconstructible from the (now newer)
    snapshot. Recording both weights on `run-resume` keeps them recoverable."""
    run_dir = _paused_run_for_resume(project, monkeypatch)
    monkeypatch.setattr(cli, "Engine", _StubEngine)

    assert cli._resume_paused_run(project.project, run_dir) == 0

    entry = _resume_entry(run_dir)
    assert entry["cache_read_weight"] == 0.5
    assert entry["cache_read_weight_was"] == 0.1
    assert entry["policy_changed"] is True


def test_resume_under_an_unchanged_policy_reports_no_change(project, monkeypatch):
    """Load-bearing: Policy.to_dict() yields TUPLES for verify.commands,
    extra_args and plugins.enabled, while the persisted snapshot round-trips them
    back as LISTS. A plain `!=` between the two therefore reports "policy
    changed" on every single resume, even an untouched one — so the comparison
    has to normalize through JSON the way save_state does."""
    from bmad_loop.journal import load_state

    run_dir = _paused_run_for_resume(project, monkeypatch)
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    # first resume stamps the live policy; the second resumes onto that stamp
    # with policy.toml untouched, so nothing has changed by then.
    assert cli._resume_paused_run(project.project, run_dir) == 0
    stamped = load_state(run_dir).policy_snapshot
    assert stamped["verify"]["commands"] == ["true"]  # a list on disk...
    assert policy_mod.load(project.project / ".bmad-loop" / "policy.toml").verify.commands == (
        "true",
    )  # ...but a tuple in the live policy

    assert cli._resume_paused_run(project.project, run_dir) == 0

    entry = _resume_entries(run_dir)[-1]
    assert entry["policy_changed"] is False
    assert "cache_read_weight_was" not in entry  # unchanged -> omitted


def test_resume_discards_stale_graceful_stop_request(project, monkeypatch, capsys):
    """A resume is fresh user intent: a graceful-stop request left over from the
    prior stopped-gracefully run must be cleared before write_pid re-arms the
    engine, or the re-driven loop would consume it at the first item boundary and
    immediately re-stop. The clear is noted on stderr."""
    from bmad_loop import runs

    run_dir = _paused_run_for_resume(project, monkeypatch)
    (run_dir / runs.STOP_REQUEST_FILE).write_text(
        '{"requested_at": "old", "mode": "graceful"}', encoding="utf-8"
    )
    monkeypatch.setattr(cli, "Engine", _StubEngine)

    assert cli._resume_paused_run(project.project, run_dir) == 0

    assert not (run_dir / runs.STOP_REQUEST_FILE).exists()  # consumed before the engine ran
    assert "discarded a stale graceful-stop request" in capsys.readouterr().err


def test_resume_refuses_live_run(tmp_path, monkeypatch, capsys):
    from bmad_loop import runs

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

    from bmad_loop import runs

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
    rc = cli.main(["diagnose", "--project", str(project.project), "--out", str(out_file)])
    assert rc == 0
    report = out_file.read_text()
    assert "diagnostic dump (sanitized)" in report
    for canary in CANARIES:
        assert canary not in report, f"LEAK via CLI: {canary!r}"


def test_diagnose_json_emits_pure_document(project, capsys):
    """--json is the whole of stdout — no markdown report, no fence to scrape."""
    from test_diagnostics import CANARIES, _seed_run

    from bmad_loop import diagnostics

    _seed_run(project.project)
    doc = machine_json(["diagnose", "--project", str(project.project), "--json"], capsys)
    assert doc["schema_version"] == diagnostics.SCHEMA_VERSION == 1
    assert doc["runs"], "the document carries the run it resolved"
    for canary in CANARIES:
        assert canary not in json.dumps(doc), f"LEAK via CLI: {canary!r}"


def test_diagnose_json_out_writes_document_and_keeps_stdout_empty(project, tmp_path, capsys):
    from test_diagnostics import CANARIES, _seed_run

    from bmad_loop import diagnostics

    _seed_run(project.project)
    out_file = tmp_path / "diag.json"
    rc = cli.main(["diagnose", "--project", str(project.project), "--json", "--out", str(out_file)])
    assert rc == 0
    out, err = capsys.readouterr()
    assert out == ""  # the document went to the file; stdout stays empty
    assert "written to" in err  # the confirmation moved to stderr
    written = out_file.read_text()
    doc = json.loads(written)
    assert doc["schema_version"] == diagnostics.SCHEMA_VERSION == 1
    assert "```" not in written  # no fences in a file written in JSON mode
    for canary in CANARIES:
        assert canary not in written, f"LEAK via CLI: {canary!r}"


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_diagnose_no_runs(tmp_path, capsys, json_mode):
    """Nothing to dump is an error, not an empty document — and in JSON mode it
    still leaves stdout empty rather than emitting `no runs found` into the
    stream a consumer is parsing."""
    argv = ["diagnose", "--project", str(tmp_path)]
    assert cli.main([*argv, "--json"] if json_mode else argv) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "no runs found" in err


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


# The two modes call *different* renders — JSON mode skips render_markdown
# entirely — so a refusal test must patch the one its mode actually reaches.
# Patching only render_markdown would let the JSON leg pass vacuously: the real
# render would simply succeed and the refusal branch would never run.
def _patch_render_boom(monkeypatch, diagnostics, json_mode, rules):
    def boom(*a, **k):
        raise diagnostics.LeakDetected(rules)

    monkeypatch.setattr(diagnostics, "render_json" if json_mode else "render_markdown", boom)


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_diagnose_refusal_branch(project, tmp_path, capsys, monkeypatch, json_mode):
    """A hard-rule leak refuses to emit: rc 1, no dump written, actionable hint."""
    from test_diagnostics import _seed_run

    from bmad_loop import diagnostics

    _seed_run(project.project)
    _patch_render_boom(monkeypatch, diagnostics, json_mode, ["email"])

    out_file = tmp_path / "diag.md"
    argv = ["diagnose", "--project", str(project.project), "--out", str(out_file)]
    rc = cli.main([*argv, "--json"] if json_mode else argv)
    assert rc == 1
    out, err = capsys.readouterr()
    # Never a partial document: the refusal returns before anything is written
    # or printed, in either mode.
    assert out == ""
    assert "refusing to emit" in err and "email" in err
    assert "--legend" in err  # the hint names the local decode path
    assert not out_file.exists()


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_diagnose_legend_written_on_failure(project, tmp_path, capsys, monkeypatch, json_mode):
    """On refusal the legend still lands locally — it decodes sensitive[<ns>:<alias>]."""
    import os as os_mod

    from test_diagnostics import STORY_KEY, _seed_run

    from bmad_loop import diagnostics

    _seed_run(project.project)
    _patch_render_boom(monkeypatch, diagnostics, json_mode, ["sensitive[story:s1-deadbeef0000]"])

    legend_file = tmp_path / "legend.json"
    argv = ["diagnose", "--project", str(project.project), "--legend", str(legend_file)]
    rc = cli.main([*argv, "--json"] if json_mode else argv)
    assert rc == 1
    legend = json.loads(legend_file.read_text())
    assert STORY_KEY in legend.values()
    if os_mod.name == "posix":
        assert (legend_file.stat().st_mode & 0o077) == 0  # still owner-only


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_diagnose_repair_warns_but_emits(project, tmp_path, capsys, json_mode):
    """A stray pseudonymized original is repaired: rc 0, dump emitted, disclosed.

    Both modes, because each discloses the repair through its OWN render — the
    markdown `### Backstop repairs` section and the JSON `backstop_repairs` key
    are separate code paths, and JSON mode no longer reaches the markdown one.

    The seed is `sweeps_triggered` specifically because BOTH renders reach it —
    which is what makes the `1 stray occurrence(s)` assertion below able to fail.
    A journal-entry gap is invisible to markdown (it renders journal aggregates,
    never per-entry fields), so with that seed a reintroduced double render would
    still tally 1 and the count would pin nothing. Here md=1, json=1, both=2.
    """
    from test_diagnostics import CANARIES, STORY_KEY, _seed_run

    _seed_run(project.project, sweeps_triggered=[STORY_KEY])
    out_file = tmp_path / "diag.md"
    argv = ["diagnose", "--project", str(project.project), "--out", str(out_file)]
    rc = cli.main([*argv, "--json"] if json_mode else argv)
    assert rc == 0
    report = out_file.read_text()
    for canary in CANARIES:
        assert canary not in report, f"LEAK via CLI: {canary!r}"
    if json_mode:
        # A repaired dump is still a whole document, not a document plus an
        # apology: parse the file rather than grepping it before trusting the key.
        assert "backstop_repairs" in json.loads(report)
    else:
        assert "### Backstop repairs" in report
        assert "1 stray occurrence(s) pseudonymized" in report
        assert STORY_KEY not in report  # the note names the alias, never the original

    err = capsys.readouterr().err
    # The literal count, not just the word "backstop": rendering BOTH reports
    # extends the same `repairs` list and doubles every tally, which is exactly
    # the bug JSON mode's single render fixed. A substring match would not see it.
    assert "pseudonymized 1 stray occurrence(s)" in err
    assert "story:" in err and "x1" in err
    assert STORY_KEY not in err  # the warning names labels, never originals


def test_diagnose_json_repair_keeps_stdout_pure(project, capsys):
    """Repairs firing while the document goes to STDOUT — the one configuration
    where a stray warning line would corrupt a consumer's parse."""
    from test_diagnostics import STORY_KEY, _seed_run

    _seed_run(
        project.project,
        extra_journal=[("custom-event", {"mystery_ref": STORY_KEY})],
    )
    assert cli.main(["diagnose", "--project", str(project.project), "--json"]) == 0
    out, err = capsys.readouterr()
    doc = json.loads(out)  # parses WHOLE — the warning never touched stdout
    assert "backstop_repairs" in doc
    assert "pseudonymized 1 stray occurrence(s)" in err
    assert STORY_KEY not in out and STORY_KEY not in err


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
    from bmad_loop import process_host as ph_mod
    from bmad_loop.adapters import multiplexer as mux_mod

    monkeypatch.setattr(mux_mod, "get_multiplexer", lambda: backend)
    monkeypatch.setattr(ph_mod, "get_process_host", lambda: _FakeHost())


def _preflight_notes_problems():
    """The preflight's pre-#205 (notes, problems) shape, rebuilt from its findings —
    these tests assert the *seam probing*, which the Finding refactor did not change."""
    found = cli._platform_preflight()
    return (
        [f.message for f in found if f.severity != "problem"],
        [f.message for f in found if f.severity == "problem"],
    )


def test_platform_preflight_reports_available_backend(monkeypatch):
    # An available backend reports through available()/version() — no sys.platform
    # branch — and the selected process host is named for visibility.
    _patch_preflight(monkeypatch, _FakeBackend(ok=True, version="tmux 3.4"))
    notes, problems = _preflight_notes_problems()
    assert not problems
    assert any("_FakeBackend" in n and "tmux 3.4" in n for n in notes)
    assert any("process host" in n and "_FakeHost" in n for n in notes)


def test_platform_preflight_flags_unavailable_backend(monkeypatch):
    # A backend whose transport binary is absent surfaces here as a problem, so a
    # new OS registers a backend rather than inlining a win32 block in validate.
    _patch_preflight(monkeypatch, _FakeBackend(ok=False))
    notes, problems = _preflight_notes_problems()
    assert any("unavailable" in p for p in problems)


def test_platform_preflight_reports_multiplexer_selection_error(monkeypatch):
    # A bad BMAD_LOOP_MUX_BACKEND makes get_multiplexer() raise; preflight must report
    # it as a problem (so `validate` exits cleanly) rather than let it abort the command.
    from bmad_loop.adapters import multiplexer as mux_mod
    from bmad_loop.adapters.multiplexer import MultiplexerError

    def _boom():
        raise MultiplexerError("BMAD_LOOP_MUX_BACKEND='bogus' matches no registered backend")

    monkeypatch.setattr(mux_mod, "get_multiplexer", _boom)
    notes, problems = _preflight_notes_problems()  # must not raise
    assert any("bogus" in p for p in problems)


def test_platform_preflight_reports_process_host_selection_error(monkeypatch):
    # A bad BMAD_LOOP_PROCESS_HOST makes get_process_host() raise; preflight must report
    # it as a problem, and an otherwise-healthy multiplexer still gets its note.
    from bmad_loop import process_host as ph_mod
    from bmad_loop.process_host import ProcessHostError

    _patch_preflight(monkeypatch, _FakeBackend(ok=True, version="tmux 3.4"))

    def _boom():
        raise ProcessHostError("BMAD_LOOP_PROCESS_HOST='bogus' matches no registered host")

    monkeypatch.setattr(ph_mod, "get_process_host", _boom)
    notes, problems = _preflight_notes_problems()  # must not raise
    assert any("bogus" in p for p in problems)
    assert any("_FakeBackend" in n for n in notes)  # the healthy seam still reported


# --------------- item 8/10: stories-aware validate + selector preflight -------

STORIES_POLICY = '[stories]\nsource = "stories"\nspec_folder = "_bmad-output/epic-1"\n'


def test_require_base_skills_warnings_do_not_block_the_run(tmp_path, monkeypatch, capsys):
    """A review layer the preflight cannot statically resolve (a `when` gate, a
    handoff phrasing it can't confirm, an unparseable override) is reported and then
    stepped over. Aborting on it would be #260's false FAIL — this time on every
    run, not just on validate — so severity decides, not emptiness."""
    from bmad_loop import install as install_mod
    from bmad_loop.checks import Finding

    _write_policy(tmp_path)
    pol = policy_mod.load(tmp_path / ".bmad-loop" / "policy.toml")
    warning = Finding("skills.review-layer-unresolved", "warning", "conditional layer", {})
    monkeypatch.setattr(install_mod, "missing_base_skills", lambda *a, **k: [warning])

    assert cli._require_base_skills(tmp_path, pol) is True
    err = capsys.readouterr().err
    assert "conditional layer" in err
    assert "FAIL" not in err

    # ...and a real problem alongside it still aborts
    problem = Finding("skills.review-layer-missing", "problem", "missing reviewer", {})
    monkeypatch.setattr(install_mod, "missing_base_skills", lambda *a, **k: [warning, problem])
    assert cli._require_base_skills(tmp_path, pol) is False
    assert "FAIL: missing reviewer" in capsys.readouterr().err


def _validate_output(capsys):
    out = capsys.readouterr()
    return (out.out + out.err).lower()


def test_validate_hookless_profile_notes_no_hook_registration(project, capsys):
    """A hookless profile (opencode-http, via its `opencode` alias) on a role it
    can drive (triage) validates with an informational note instead of the
    'hooks not registered' FAIL — there is no hook config to check. Exit code is
    not asserted: other gates (binary on PATH, upstream skills) legitimately
    vary by machine."""
    install_bmad_config(project)
    _write_policy(project.project, '[adapter.triage]\nname = "opencode"\n')
    args = argparse.Namespace(project=str(project.project), spec=None)

    cli.cmd_validate(args)
    text = _validate_output(capsys)
    assert "opencode-http: hookless (http/sse transport)" in text
    assert "hooks not registered for opencode-http" not in text
    # httpx ships in the dev group, so the extra-dependency gate passes here
    assert "httpx available for opencode-http" in text
    # triage-only is runnable today: no Phase 4 guard problem
    assert "phase 4" not in text


def test_validate_hookless_dev_review_is_runnable(project, capsys):
    """A hookless profile on the synthesizing dev/review roles is runnable
    since Phase 4 (OpencodeDevAdapter) — validate must not raise the retired
    'cannot drive dev/review' problem. Exit code is not asserted: other gates
    (binary on PATH, upstream skills) legitimately vary by machine."""
    install_bmad_config(project)
    _write_policy(project.project, '[adapter]\nname = "opencode"\n')
    args = argparse.Namespace(project=str(project.project), spec=None)

    cli.cmd_validate(args)
    text = _validate_output(capsys)
    assert "opencode-http: hookless (http/sse transport)" in text
    assert "cannot drive the dev/review roles" not in text
    assert "phase 4" not in text


def test_validate_hookless_profile_flags_missing_httpx(project, capsys, monkeypatch):
    """A hookless profile whose httpx extra is not installed FAILs validate with
    the actionable install hint — at validate time, not deep in a run start."""
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "httpx":
            return None
        return real_find_spec(name, *args, **kwargs)

    install_bmad_config(project)
    _write_policy(project.project, '[adapter]\nname = "opencode"\n')
    monkeypatch.setattr(cli.importlib.util, "find_spec", fake_find_spec)
    args = argparse.Namespace(project=str(project.project), spec=None)

    cli.cmd_validate(args)
    text = _validate_output(capsys)
    assert "httpx not installed" in text
    assert "bmad-loop[opencode]" in text


def test_validate_warns_when_no_desktop_notifier(project, monkeypatch, capsys):
    """#231: notify.desktop defaults on, but when this platform resolves no
    notifier the setting is silently inert — validate must surface it as a
    warning finding (not a FAIL — desktop notification is best-effort)."""
    install_bmad_config(project)
    _write_policy(project.project)  # default notify.desktop = true
    monkeypatch.setattr(cli.gates, "desktop_notifier_kind", lambda: None)
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)  # rc varies by host (binary/skills) — parse the document instead
    doc = json.loads(capsys.readouterr().out)
    finding = next(f for f in doc["findings"] if f["check"] == "notify.desktop-unavailable")
    assert finding["severity"] == "warning"
    assert "notify.desktop is set" in finding["message"]


def test_validate_silent_when_desktop_notifier_available(project, monkeypatch, capsys):
    """The mirror case: a resolvable notifier means notify.desktop works, so no
    notify.desktop-unavailable finding is emitted at all."""
    install_bmad_config(project)
    _write_policy(project.project)
    monkeypatch.setattr(cli.gates, "desktop_notifier_kind", lambda: "osascript")
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)
    doc = json.loads(capsys.readouterr().out)
    assert all(f["check"] != "notify.desktop-unavailable" for f in doc["findings"])


def _declare_closes_deferred(project, declared, *, ledger_ids=("DW-1",), manifest=None):
    """A stories-mode fixture for one story: `manifest` ids declared on its
    stories.yaml entry, `declared` ids in its spec frontmatter (None writes no spec
    at all), over a ledger holding `ledger_ids` — all open."""
    over = {"closes_deferred": list(manifest)} if manifest is not None else {}
    folder = _setup_stories_fixture(project, [_stories_entry("1", **over)])
    write_ledger(project, {dw: "open" for dw in ledger_ids}, commit=False)
    if declared is not None:
        (folder / "stories" / "1-slug.md").write_text(
            f"---\ntitle: 'test'\nstatus: 'ready-for-dev'\n"
            f"closes_deferred: [{', '.join(declared)}]\n---\n\n## Intent\n\ntest\n",
            encoding="utf-8",
        )
    return folder


def _closes_deferred_findings(capsys):
    doc = json.loads(capsys.readouterr().out)
    return [f for f in doc["findings"] if f["check"] == "deferred.closes-unknown"]


def test_validate_warns_on_unknown_closes_deferred(project, capsys):
    """#234: a story declaring an id the ledger doesn't carry — a typo, or an entry
    renumbered since the spec was written — would annotate nothing at close, and
    only the journal would say so. Preflight names it while it is still fixable."""
    install_bmad_config(project)
    _write_policy(project.project, STORIES_POLICY)
    _declare_closes_deferred(project, ["DW-99"])
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)  # rc varies by host (binary/skills) — parse the document
    findings = _closes_deferred_findings(capsys)
    assert len(findings) == 1
    assert findings[0]["severity"] == "warning"  # advisory: never blocks a run
    assert findings[0]["detail"] == {"source": "story 1", "unknown_ids": ["DW-99"]}
    assert "DW-99" in findings[0]["message"]


def test_validate_silent_when_closes_deferred_all_present(project, capsys):
    """The mirror case: every declared id is in the ledger, so nothing is emitted.
    A present entry an earlier run already closed is a *satisfied* declaration, not
    a mismatch — the check keys off presence, never status, so a resumed project
    doesn't accumulate warnings for work that landed."""
    install_bmad_config(project)
    _write_policy(project.project, STORIES_POLICY)
    _declare_closes_deferred(project, ["DW-1", "DW-2"], ledger_ids=("DW-1", "DW-2"))
    mark_ledger_done(project, ["DW-2"])  # already closed by an earlier run
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)
    assert _closes_deferred_findings(capsys) == []


def test_validate_warns_when_a_declared_entry_status_is_unreadable(project, capsys):
    """A declared id can also point at an entry the ledger carries but whose
    `status:` reads as neither `open` nor `done`. Nothing gets marked for it and
    the close hook journals `deferred-close-malformed`, but preflight covered only
    absent ids and unreadable declarations — leaving this third case to be found in
    the journal after the run it should have preceded (CodeRabbit, round 3).

    The remedy is in the ledger, not the declaration, so it is its own check id
    rather than folded into `deferred.closes-unknown`."""
    install_bmad_config(project)
    _write_policy(project.project, STORIES_POLICY)
    _declare_closes_deferred(project, ["DW-1"])
    ledger = project.deferred_work
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace("status: open", "status: in-progress"),
        encoding="utf-8",
    )
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)
    doc = json.loads(capsys.readouterr().out)
    findings = [f for f in doc["findings"] if f["check"] == "deferred.closes-entry-unreadable"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "warning"  # advisory, like the rest
    assert findings[0]["detail"] == {"source": "story 1", "dw_ids": ["DW-1"]}
    # not misreported as a typo: the id IS in the ledger
    assert not [f for f in doc["findings"] if f["check"] == "deferred.closes-unknown"]


def test_validate_warns_on_unknown_closes_deferred_in_the_manifest(project, capsys):
    """The manifest channel is what makes this a genuine *pre*-flight: a stories.yaml
    entry declares its ids before the story has ever been dispatched, so a typo is
    caught while no spec exists yet — the frontmatter check can't see that far."""
    install_bmad_config(project)
    _write_policy(project.project, STORIES_POLICY)
    _declare_closes_deferred(project, None, manifest=["DW-99"])  # no story spec on disk
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)
    findings = _closes_deferred_findings(capsys)
    assert len(findings) == 1
    assert findings[0]["detail"] == {"source": "story 1", "unknown_ids": ["DW-99"]}


def test_validate_closes_deferred_dedupes_across_both_channels(project, capsys):
    """An id declared in both the manifest and the spec is one mismatch, reported
    once — not the same typo twice."""
    install_bmad_config(project)
    _write_policy(project.project, STORIES_POLICY)
    _declare_closes_deferred(project, ["DW-99"], manifest=["DW-99"])
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)
    findings = _closes_deferred_findings(capsys)
    assert len(findings) == 1
    assert findings[0]["detail"]["unknown_ids"] == ["DW-99"]  # once, not twice


def test_validate_closes_deferred_warning_does_not_fail_the_run(project, capsys, monkeypatch):
    """The warning must not flip the exit code. An otherwise-clean project carrying
    a stale declaration still validates ok/rc 0 — a typo in a traceability field can
    never be the thing that blocks a run from starting."""
    _make_validate_pass(project, monkeypatch, capsys)
    _write_policy(project.project, CLAUDE_ONLY_POLICY + STORIES_POLICY)  # same, stories mode
    _declare_closes_deferred(project, ["DW-99"])
    git(project.project, "add", "-A")  # the fixture above dirties the worktree gate
    git(project.project, "commit", "-q", "-m", "stories fixture")
    capsys.readouterr()

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)  # rc 0
    assert doc["ok"] is True
    assert doc["counts"]["problem"] == 0
    assert [f for f in doc["findings"] if f["check"] == "deferred.closes-unknown"]


def test_validate_warns_on_unknown_closes_deferred_in_sprint_mode(project, capsys):
    """The check runs in BOTH queue modes (#234 review, finding 6). Sprint mode
    has no manifest, but `create-story` writes its story specs into the artifacts
    dir before the run — so a typo there is caught at preflight too, rather than
    surfacing only as a journal line nobody reads until the retro."""
    install_bmad_config(project)
    _write_policy(project.project)  # sprint mode: no [stories] source
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"}, commit=False)
    write_spec(spec_path(project, "1-1-a"), "ready-for-dev", "abc123", closes_deferred=["DW-99"])
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)

    findings = _closes_deferred_findings(capsys)
    assert len(findings) == 1
    assert findings[0]["severity"] == "warning"
    assert findings[0]["detail"] == {"source": "spec spec-1-1-a.md", "unknown_ids": ["DW-99"]}


def test_validate_warns_when_the_ledger_itself_is_unreadable(project, capsys, monkeypatch):
    """The ledger read shared a `try` with the manifest read, and that arm returns
    silently — correctly for the manifest, which `queue.stories-manifest` already
    reports, but nothing else in `validate` reads the ledger. So an unreadable one
    produced no finding at all: preflight reported success for a check that
    examined nothing, against the very file the run's closure will fail on
    (#284 round-5 review, finding 6)."""
    install_bmad_config(project)
    _write_policy(project.project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"}, commit=False)
    write_spec(spec_path(project, "1-1-a"), "ready-for-dev", "abc123", closes_deferred=["DW-1"])
    fault_read_text(monkeypatch, project.deferred_work)
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)

    doc = json.loads(capsys.readouterr().out)
    findings = [f for f in doc["findings"] if f["check"] == "deferred.ledger-unreadable"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "warning"  # advisory: still never a gate
    assert findings[0]["detail"]["ledger"] == str(project.deferred_work)
    # and the declaration checks it could not run stay quiet rather than guessing
    assert not [f for f in doc["findings"] if f["check"] == "deferred.closes-unknown"]


def test_validate_warns_on_a_malformed_closes_deferred_declaration(project, capsys):
    """`closes_deferred: DW-1` (a scalar, not a list) declares real intent that
    reads as empty. Silence there was indistinguishable from having no
    declaration at all, so the same mistake meant nothing in a spec and a hard
    schema error in stories.yaml — preflight now names it either way."""
    install_bmad_config(project)
    _write_policy(project.project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"}, commit=False)
    write_spec(spec_path(project, "1-1-a"), "ready-for-dev", "abc123", closes_deferred="DW-1")
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)

    doc = json.loads(capsys.readouterr().out)
    findings = [f for f in doc["findings"] if f["check"] == "deferred.closes-malformed"]
    assert len(findings) == 1 and findings[0]["severity"] == "warning"
    assert "must be a list" in findings[0]["detail"]["error"]


def test_validate_sprint_mode_silent_without_declarations(project, capsys):
    """The ordinary sprint project declares nothing: scanning the artifacts dir
    must add no findings at all."""
    install_bmad_config(project)
    _write_policy(project.project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    write_ledger(project, {"DW-1": "open"}, commit=False)
    write_spec(spec_path(project, "1-1-a"), "ready-for-dev", "abc123")
    args = argparse.Namespace(project=str(project.project), spec=None, json=True)

    cli.cmd_validate(args)

    doc = json.loads(capsys.readouterr().out)
    assert [f for f in doc["findings"] if f["check"].startswith("deferred.closes")] == []


OPENCODE_QUALIFIED_POLICY = '[adapter]\nname = "opencode"\nmodel = "anthropic/claude-haiku-4-5"\n'


def test_dry_run_renders_hookless_http_line(project, capsys):
    """A hookless adapter has no shell invocation — dry-run renders the honest
    HTTP sequence (server spawn → session → prompt_async) instead of a fake
    argv that run would never execute."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    _write_policy(project.project, OPENCODE_QUALIFIED_POLICY)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args) == 0
    out = capsys.readouterr().out
    dev_line = next(line for line in out.splitlines() if "dev:" in line)
    assert "opencode serve --hostname 127.0.0.1 --port <auto>" in dev_line
    assert "POST /session" in dev_line and "prompt_async" in dev_line
    # the profile's codex-style template is rendered into the prompt_async body
    assert "Use the bmad-dev-auto skill now:" in dev_line
    assert "model=anthropic/claude-haiku-4-5" in dev_line


def test_validate_warns_on_bare_opencode_model(project, capsys):
    """opencode model ids are 'provider/model'; a bare name silently falls back
    to the server's default model — validate surfaces an advisory warning (a
    note, not a FAIL)."""
    install_bmad_config(project)
    _write_policy(project.project, '[adapter]\nname = "opencode"\nmodel = "haiku"\n')
    args = argparse.Namespace(project=str(project.project), spec=None)

    cli.cmd_validate(args)
    text = _validate_output(capsys)
    assert "warning: dev model 'haiku' is not 'provider/model'" in text


def test_validate_model_warning_spares_qualified_model(project, capsys):
    install_bmad_config(project)
    _write_policy(project.project, OPENCODE_QUALIFIED_POLICY)
    args = argparse.Namespace(project=str(project.project), spec=None)

    cli.cmd_validate(args)
    assert "is not 'provider/model'" not in _validate_output(capsys)


def test_validate_model_warning_ignores_tmux_profiles(project, capsys):
    """Bare model names are the norm for tmux CLIs (claude 'opus', codex
    'gpt-5-codex') — the provider/model advisory is hookless-only."""
    install_bmad_config(project)
    _write_policy(project.project)  # DUAL_CLIENT_POLICY: bare models, tmux profiles
    args = argparse.Namespace(project=str(project.project), spec=None)

    cli.cmd_validate(args)
    assert "is not 'provider/model'" not in _validate_output(capsys)


def test_validate_stories_mode_skips_sprint_gate(project, capsys):
    """Item 8: a stories-mode project (no sprint-status.yaml) validates its
    stories.yaml manifest instead of failing on the missing sprint gate."""
    install_bmad_config(project)
    _setup_stories_fixture(project, [_stories_entry("1")])
    _write_policy(project.project, STORIES_POLICY)
    args = argparse.Namespace(project=str(project.project), spec=None)

    cli.cmd_validate(args)
    text = _validate_output(capsys)
    assert "sprint status" not in text  # the sprint gate is skipped
    assert "stories mode ok" in text  # the manifest validated instead


def test_validate_stories_mode_reports_missing_manifest(project, capsys):
    """Item 8: stories mode with no stories.yaml fails validate with the pinned
    remediation-bearing message (not the sprint-status error)."""
    install_bmad_config(project)
    _write_policy(project.project, STORIES_POLICY)
    args = argparse.Namespace(project=str(project.project), spec=None)

    assert cli.cmd_validate(args) == 1
    text = _validate_output(capsys)
    assert "no stories.yaml found" in text
    assert "sprint status" not in text


def test_validate_sprint_mode_still_gates_on_sprint_status(project, capsys):
    """Item 8 regression: the default (sprint) mode still requires sprint-status."""
    install_bmad_config(project)
    _write_policy(project.project)  # DUAL_CLIENT_POLICY -> sprint mode (no [stories])
    args = argparse.Namespace(project=str(project.project), spec=None)

    assert cli.cmd_validate(args) == 1
    assert "sprint" in _validate_output(capsys)


def test_validate_spec_flag_forces_stories_mode(project, capsys):
    """Item 8: `validate --spec <folder>` forces stories mode even under a sprint
    policy — the sprint gate is skipped and the manifest is validated."""
    install_bmad_config(project)
    _setup_stories_fixture(project, [_stories_entry("1")])
    _write_policy(project.project)  # sprint policy
    args = argparse.Namespace(project=str(project.project), spec=STORIES_SPEC_FOLDER)

    cli.cmd_validate(args)
    text = _validate_output(capsys)
    assert "stories mode ok" in text
    assert "sprint status" not in text


def test_validate_stories_folder_unknown_selector(project):
    """Item 10: an unknown --story id is caught at preflight (fails before the run
    starts) rather than crashing mid-flight in the scheduler."""
    _setup_stories_fixture(project, [_stories_entry("1"), _stories_entry("2")])
    problem = cli._validate_stories_folder(project, STORIES_SPEC_FOLDER, selector="99")
    assert problem is not None and "'99'" in problem and "not in stories.yaml" in problem


def test_validate_stories_folder_known_selector_ok(project):
    _setup_stories_fixture(project, [_stories_entry("1"), _stories_entry("2")])
    assert cli._validate_stories_folder(project, STORIES_SPEC_FOLDER, selector="2") is None


# ------------------------- #205: validate --json ------------------------------

CLAUDE_ONLY_POLICY = '[adapter]\nname = "claude"\nmodel = "opus"\n'


def _make_validate_pass(project, monkeypatch, capsys, *, policy=CLAUDE_ONLY_POLICY, skills=None):
    """Set a project up so every validate gate passes, and pin the two gates whose
    outcome is a property of the *host* rather than of the project: whether the CLI
    binary is on PATH and whether a multiplexer is installed. Without those pins the
    rc-0 leg would pass or fail by machine, which is exactly the kind of green that
    means nothing.

    ``policy`` and ``skills`` exist so the dev-primitive-rename tests can vary the
    project's *topology* (which CLIs on which roles, which primitive era in which
    tree) while keeping every other gate green — an rc-0 assertion about one check is
    worthless if some unrelated gate is what is actually failing. ``skills`` is called
    with the project root BEFORE the commit, so whatever it lays down is committed and
    the worktree-clean gate still passes."""
    install_bmad_config(project)
    _write_policy(project.project, policy)
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    if skills is None:
        install_dev_base_skills(project.project, folder_id=True)
    else:
        skills(project.project)
    assert cli.main(["init", "--project", str(project.project)]) == 0  # registers the hooks
    git(project.project, "add", "-A")  # every file above is a worktree change
    git(project.project, "commit", "-q", "-m", "validate fixture")
    monkeypatch.setattr(cli.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        cli,
        "_platform_preflight",
        lambda: [cli.Finding("mux.backend", "ok", "multiplexer TmuxBackend available (tmux 3.4)")],
    )
    capsys.readouterr()  # drop `init`'s chatter — the next read must see only the document


def test_validate_json_clean_project_is_a_pure_document_at_rc_0(project, capsys, monkeypatch):
    """The happy path: one whole document on stdout, nothing else, ok true."""
    _make_validate_pass(project, monkeypatch, capsys)

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)
    assert doc["schema_version"] == cli.VALIDATE_SCHEMA_VERSION == 1
    assert doc["ok"] is True
    assert doc["counts"]["problem"] == 0
    assert doc["findings"], "a passing validate still reports what it checked"
    assert {f["check"] for f in doc["findings"]} >= {"bmad-config", "policy", "git.worktree-clean"}


def test_validate_json_failing_check_emits_whole_document_at_rc_1(project, capsys):
    """#205's interesting case: rc 1 is the *verdict*, not a failure to produce a
    document. stdout is still one complete document and stderr is still empty —
    the `FAIL:` lines became findings, they did not move to the other stream."""
    _write_policy(project.project, CLAUDE_ONLY_POLICY)  # no BMAD config -> a real problem

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)
    assert doc["ok"] is False
    assert doc["counts"]["problem"] >= 1
    failed = [f for f in doc["findings"] if f["severity"] == "problem"]
    assert "bmad-config" in {f["check"] for f in failed}


def test_validate_reports_an_undecodable_policy_instead_of_crashing(project, capsys):
    """`read_text` raises `UnicodeDecodeError` on a policy.toml saved as UTF-16 or
    latin-1 — a ValueError, not an OSError, so it used to escape every
    `except (PolicyError, OSError)` in the codebase, including `_configure_mux`,
    which runs before argument dispatch on EVERY command. The result was a bare
    `error:` line at startup in place of the named findings validate exists to print.
    `policy.load` converts it, so the file is reported like any other bad policy.

    Routed through `machine_json` deliberately: `main`'s bare `except Exception`
    backstop also returns 1, so an rc-only assertion is green with the conversion
    reverted. What bites is the document — stderr carries the backstop's line and
    stdout carries nothing to parse."""
    _write_policy(project.project, CLAUDE_ONLY_POLICY)
    (project.project / ".bmad-loop" / "policy.toml").write_bytes(b'[scm]\nisolation = "\xff\xfe"\n')

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)
    finding = next(f for f in doc["findings"] if f["check"] == "policy")
    assert finding["severity"] == "problem"
    assert "not valid UTF-8" in finding["message"]


def test_validate_reports_an_undecodable_bmad_config_instead_of_crashing(project, capsys):
    """The `config.yaml` half of the same conversion. Separate from the policy leg
    because they are separate loaders with separate typed errors, and the callers that
    first exposed this call BOTH — a fix to one would leave the other raising a raw
    ValueError straight through the handler."""
    _write_policy(project.project, CLAUDE_ONLY_POLICY)
    install_bmad_config(project)
    config = project.project / "_bmad" / "bmm" / "config.yaml"
    config.write_bytes(b"implementation_artifacts: '\xff\xfe'\n")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)
    finding = next(f for f in doc["findings"] if f["check"] == "bmad-config")
    assert finding["severity"] == "problem"
    assert "not valid UTF-8" in finding["message"]


def test_validate_json_counts_and_ok_agree_with_findings(project, capsys):
    """`ok` mirrors the exit code exactly: problems clear it, warnings never do."""
    install_bmad_config(project)
    _write_policy(project.project, '[adapter]\nname = "opencode"\nmodel = "haiku"\n')
    write_sprint(project, {"epic-1": "backlog"})

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)
    findings = doc["findings"]
    for severity in ("ok", "warning", "problem"):
        assert doc["counts"][severity] == sum(1 for f in findings if f["severity"] == severity)
    assert doc["ok"] == (doc["counts"]["problem"] == 0)
    # this project warns (bare opencode model) *and* fails — the warning did not
    # clear ok, and the warning is not counted among the problems
    assert doc["counts"]["warning"] >= 1 and doc["ok"] is False


def test_validate_json_warning_message_carries_no_severity_prefix(project, capsys):
    """The severity lives in the `severity` field, never doubled into the message.

    The text form prints `  ok:   warning: ...` because the note *stored* the
    "warning: " prefix; a consumer must not have to strip it back off."""
    install_bmad_config(project)
    _write_policy(project.project, '[adapter]\nname = "opencode"\nmodel = "haiku"\n')
    write_sprint(project, {"epic-1": "backlog"})

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)
    warned = [f for f in doc["findings"] if f["severity"] == "warning"]
    assert warned, "the bare opencode model warns"
    assert any(f["check"] == "policy.model-qualified" for f in warned)
    for finding in warned:
        assert "warning:" not in finding["message"]
        assert not finding["message"].startswith(" ")


def test_validate_json_detail_round_trips_for_every_real_shape(capsys):
    """Every `detail` shape the real gates attach must survive `validate --json`'s
    `json.dumps` boundary.

    #268 widened `Finding.detail` to `Mapping[str, object]` for strict-mode
    covariance — so `install.py`'s `dict[str, str]` stays assignable — which leaves
    implicit the invariant this pins: the production path
    `machine.emit(validate_document(...))` still emits one whole, parseable document
    for every detail a caller actually builds. Each case below mirrors a real call
    site (str values, the nested `dict(role_names)` dict, str+int, install.py's
    `{**detail, "marker": ...}`, and the `detail=None` leg). A future caller that
    attaches a non-JSON-serializable detail fails here, by name, rather than at
    runtime on stdout.
    """
    from bmad_loop import machine

    report = cli.ValidationReport()
    report.ok("adapter.binary", "codex found", {"binary": "codex"})  # str values (cli.py)
    report.ok(  # nested plain dict — the `dict(role_names)` shape (cli.py)
        "policy",
        "policy OK",
        {"gates_mode": "warn", "adapters": {"dev": "claude", "review": "claude"}},
    )
    report.ok(  # str + int (cli.py)
        "queue.stories-manifest", "stories OK", {"spec_folder": "docs", "stories": 3}
    )
    report.fail(  # install.py's `{**detail, "marker": ...}` shape
        "skills.stories-dispatch-stale",
        "stale",
        {"tree": ".claude", "skill": "s", "file": "f", "marker": "m"},
    )
    report.ok("git.worktree-clean", "clean")  # the detail=None leg

    # the exact production path: cli.py does `machine.emit(validate_document(...))`.
    machine.emit(cli.validate_document(report, stories_on=False, spec_folder=""))
    parsed = json.loads(capsys.readouterr().out)  # whole stdout parses => a pure document

    by_check = {f["check"]: f for f in parsed["findings"]}
    assert by_check["policy"]["detail"]["adapters"]["dev"] == "claude"  # nested dict survives
    assert by_check["queue.stories-manifest"]["detail"]["stories"] == 3  # int, not "3"
    assert by_check["skills.stories-dispatch-stale"]["detail"]["marker"] == "m"
    assert by_check["git.worktree-clean"]["detail"] is None  # None -> null round-trips


@pytest.mark.parametrize(
    ("spec", "mode", "folder"),
    [(None, "sprint", ""), (STORIES_SPEC_FOLDER, "stories", STORIES_SPEC_FOLDER)],
    ids=["sprint", "stories"],
)
def test_validate_json_mode_and_spec_folder_reflect_the_flag(project, capsys, spec, mode, folder):
    """`--spec` forces stories mode, and the document says which queue was gated.

    spec_folder is "" — not null — in sprint mode, where it is inapplicable: the
    same ""-for-inapplicable convention as list_document's paused_stage."""
    install_bmad_config(project)
    _setup_stories_fixture(project, [_stories_entry("1")])
    _write_policy(project.project, CLAUDE_ONLY_POLICY)  # sprint policy either way
    write_sprint(project, {"epic-1": "backlog"})

    argv = ["validate", "--project", str(project.project), "--json"]
    if spec:
        argv += ["--spec", spec]
    doc = machine_json(argv, capsys, rc=1)
    assert doc["mode"] == mode
    assert doc["spec_folder"] == folder
    gate = "queue.stories-manifest" if spec else "queue.sprint-status"
    assert gate in {f["check"] for f in doc["findings"]}


def test_validate_json_every_emitted_check_is_registered(project, capsys, monkeypatch):
    """The `add` assert enforces this per call site; this proves it end-to-end over a
    real run, so an id that only ever appears on an unexercised path still has to be
    in the registry."""
    from bmad_loop.checks import VALIDATE_CHECKS

    _make_validate_pass(project, monkeypatch, capsys)
    passing = machine_json(["validate", "--project", str(project.project), "--json"], capsys)
    _write_policy(project.project, "[adapter]\nname = ")  # unparseable -> the failure paths
    failing = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)
    emitted = {f["check"] for f in (*passing["findings"], *failing["findings"])}
    assert emitted, "the run emitted findings"
    assert emitted <= VALIDATE_CHECKS


@pytest.mark.parametrize("passing", [True, False], ids=["rc-0", "rc-1"])
def test_tui_renderer_draws_every_detail_shape_a_real_validate_emits(
    project, capsys, monkeypatch, passing
):
    """The bridge between the `--json` document and the TUI's renderer (#210), run
    against a REAL validate rather than a fixture.

    It lives in this file, not test_tui_app.py, because everything that produces a
    real document — _make_validate_pass and the failure setups — is here; the
    renderer is the thing imported. Its value is zero fixture maintenance: it draws
    whatever shapes validate actually emits today, so a check site that starts
    carrying a new `detail` shape is covered the moment it ships, rather than when
    someone remembers to update a fixture.

    Both legs matter: `ok`-severity detail shapes never appear on a failing run.
    """
    from rich.console import Console

    from bmad_loop.tui import widgets

    if passing:
        _make_validate_pass(project, monkeypatch, capsys)
        doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)
    else:
        _write_policy(project.project, CLAUDE_ONLY_POLICY)  # no BMAD config -> real problems
        doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)
    assert doc["findings"], "the leg under test emitted findings"

    console = Console(width=96)  # the width the modal is laid out for
    with console.capture() as capture:
        console.print(widgets.validate_findings(doc, details=True))
    rendered = capture.get()

    for finding in doc["findings"]:
        assert finding["check"] in rendered
    # The nested-dict trap: `policy`'s detail is {"adapters": {"dev": ...}}, emitted
    # on the *passing* path. A renderer that str()s a shape it did not model prints
    # a Python repr, and this is the tell.
    assert "{'" not in rendered
    policy = next(f for f in doc["findings"] if f["check"] == "policy")
    assert policy["detail"]["adapters"], "policy still carries the nested adapters dict"
    assert "adapters: dev=claude" in rendered

    # and the document the CLI just emitted is one this renderer accepts at all
    assert widgets.validate_document(json.dumps(doc)) == doc


def test_validate_json_mux_detail_keeps_the_rows_the_text_flattens(mux_registry, capsys):
    """The text line ("alpha*, beta (unavailable)") makes a consumer parse a trailing
    `*` to learn which backend is selected. The detail keeps all six MuxBackendInfo
    fields verbatim — and the text line itself is unchanged."""
    import sys as _sys

    mux_registry.register_multiplexer(
        "alpha", lambda p: p == _sys.platform, lambda: _MuxStub(avail=True, version="alpha 1.2")
    )
    mux_registry.register_multiplexer("beta", lambda p: False, lambda: _MuxStub(avail=False))

    found = cli._platform_preflight()
    listing = next(f for f in found if f.check == "mux.backends-detected")
    # unchanged text
    assert "alpha*" in listing.message and "beta (unavailable)" in listing.message
    rows = {r["name"]: r for r in listing.detail["backends"]}
    assert set(rows) == {"alpha", "beta"}
    assert set(rows["alpha"]) == {
        "name",
        "matches_platform",
        "available",
        "version",
        "selected",
        "reason",
    }
    assert rows["alpha"]["selected"] is True and rows["beta"]["selected"] is False
    assert rows["alpha"]["version"] == "alpha 1.2" and rows["beta"]["version"] is None
    assert rows["beta"]["matches_platform"] is False and rows["beta"]["available"] is False


def test_platform_preflight_selection_detail_keeps_the_raw_reason(mux_registry, monkeypatch):
    """mux.selection's message renders _mux_reason_label's prose; the detail keeps the
    enum MuxBackendInfo.reason actually carries, which is the matchable value."""
    mux_registry.register_multiplexer("alpha", lambda p: False, lambda: _MuxStub(avail=True))
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "alpha")
    mux_registry.get_multiplexer.cache_clear()

    selection = next(f for f in cli._platform_preflight() if f.check == "mux.selection")
    assert selection.detail == {"backend": "alpha", "reason": "env"}
    assert "forced by BMAD_LOOP_MUX_BACKEND" in selection.message  # prose stays in the message


def test_external_backend_failure_is_a_warning_not_a_note(mux_registry, monkeypatch, capsys):
    """A package the operator installed did not load — a real failure, so it carries
    `warning`, matching what `cmd_mux` has always printed for the same condition.

    It stays below `problem` on purpose: selection degraded past it, so the verdict
    and rc must not flip. Held at `ok` until #210 only because promoting inserts
    `  warning: ` into the text (render() keeps the double prefix by design) and the
    TUI rendered that text verbatim; the second assert is that now-shipping line.
    """
    from bmad_loop.checks import ValidationReport

    monkeypatch.setattr(mux_registry, "_EXTERNAL_ERRORS", {"brokenmux": "ImportError: no ghost"})
    monkeypatch.setattr(mux_registry, "_EXTERNALS_LOADED", True)  # no rescan over the stub

    finding = next(f for f in cli._platform_preflight() if f.check == "mux.external-backend")
    assert finding.severity == "warning"
    assert finding.detail == {"entry_point": "brokenmux", "error": "ImportError: no ghost"}

    report = ValidationReport()
    report.extend([finding])
    report.render()
    assert capsys.readouterr().out == (
        "  ok:   warning: external mux backend 'brokenmux' failed to load: ImportError: no ghost\n"
    )


def test_validate_without_a_json_attribute_still_renders_text(project, capsys):
    """cmd_validate reads `getattr(args, "json", False)`, not `args.json`: it is called
    directly with hand-built Namespaces that predate the flag (every validate test
    above does), and an AttributeError there would be a crash, not a fallback."""
    install_bmad_config(project)
    _write_policy(project.project, CLAUDE_ONLY_POLICY)
    args = argparse.Namespace(project=str(project.project), spec=None)  # no `json` attribute

    cli.cmd_validate(args)  # must not raise
    out = capsys.readouterr().out
    assert "  ok: BMAD config OK:" in out  # the text form, not a document
    assert not out.lstrip().startswith("{")


def test_validation_report_renders_each_severity_verbatim(capsys):
    """The exact bytes of all three severities.

    The doubled space in the warning line is shipped output, not a typo: the
    warning sites stored `"  warning: " + msg` into the list the `  ok: ` printer
    walked. _validate_output lowercases and substring-matches, so tidying it would
    pass every text test silently — this test is the thing that would fail."""
    from bmad_loop.checks import ValidationReport

    report = ValidationReport()
    report.ok("git.worktree-clean", "git worktree clean")
    report.warn("queue.sprint-status-unknown-keys", "unknown keys ignored: x, y")
    report.fail("adapter.binary", "codex not found on PATH")
    report.render()

    out, err = capsys.readouterr()
    assert out == "  ok: git worktree clean\n  ok:   warning: unknown keys ignored: x, y\n"
    assert err == "FAIL: codex not found on PATH\n"


def test_validation_report_rejects_an_unregistered_check_id():
    """One printed line = exactly one Finding, and every Finding names a registered
    gate. A new check site cannot ship without adding its id to VALIDATE_CHECKS."""
    from bmad_loop.checks import ValidationReport

    with pytest.raises(AssertionError, match="unregistered check id"):
        ValidationReport().ok("git.worktee-clean", "typo'd id")  # codespell:ignore


def test_dry_run_stories_shows_plan_halt_markers(project, capsys):
    """Item 10: dry-run mirrors the real dispatch's leg-1 markers for a pending
    spec_checkpoint story (`Halt after planning.` + BMAD_LOOP_PLAN_HALT)."""
    _setup_stories_fixture(project, [_stories_entry("1", spec_checkpoint=True)])
    pol = policy_mod.loads("")
    args = argparse.Namespace(spec=STORIES_SPEC_FOLDER, epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args, True, STORIES_SPEC_FOLDER) == 0
    out = capsys.readouterr().out
    assert "Halt after planning." in out
    assert "BMAD_LOOP_PLAN_HALT=1" in out


def test_dry_run_stories_relativizes_absolute_folder(project, capsys):
    """Item 10: an absolute --spec inside the project renders the project-relative
    folder in the dispatch/env, matching what the engine actually dispatches."""
    _setup_stories_fixture(project, [_stories_entry("1")])
    abs_folder = str(project.project / STORIES_SPEC_FOLDER)
    pol = policy_mod.loads("")
    args = argparse.Namespace(spec=abs_folder, epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args, True, abs_folder) == 0
    out = capsys.readouterr().out
    assert "Spec folder: _bmad-output/epic-1. Story id: 1." in out
    assert f"Spec folder: {abs_folder}" not in out  # not the raw absolute path


# ------------- #384: legacy shared-exclude pollution is a validate warning -----


def _pollute_exclude(project, *lines: str):
    """Append `lines` to the repository-wide exclude, the way a pre-#384 run did.

    Called AFTER the fixture's own commit: these patterns name paths the fixture
    tracks, and an exclude in place earlier would change what `git add -A` stages
    — which is the very bug, and would make the fixture, not the gate, the thing
    under test."""
    exclude = project.project / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write("".join(f"{line}\n" for line in lines))
    return exclude


def test_validate_warns_on_legacy_shared_exclude_pollution(project, capsys, monkeypatch):
    """#384's residue: lines an older bmad-loop left in `.git/info/exclude` hide
    every NEW file under those paths from this checkout's `git add -A`, forever.
    validate names the file and the exact lines."""
    _make_validate_pass(project, monkeypatch, capsys)
    exclude = _pollute_exclude(project, "/.claude/skills", "/_bmad/custom")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1, "the pollution gate fires exactly once"
    assert warned[0]["severity"] == "warning"
    assert warned[0]["detail"]["lines"] == ["/.claude/skills", "/_bmad/custom"]
    assert warned[0]["detail"]["exclude"] == str(exclude)
    # a warning is not a verdict: the operator is told, not blocked
    assert doc["ok"] is True


def test_validate_pollution_warning_prints_the_lines_in_text_mode(project, capsys, monkeypatch):
    """The remedy is manual, so the human-readable output has to carry enough to
    act on: which file, which lines, and what the symptom looks like."""
    _make_validate_pass(project, monkeypatch, capsys)
    exclude = _pollute_exclude(project, "/.claude/skills")

    cli.main(["validate", "--project", str(project.project)])

    text = _validate_output(capsys)
    assert "/.claude/skills" in text
    # the file, by its real path rather than a POSIX-shaped substring: on Windows
    # this renders with backslashes, so asserting "info/exclude" passed nowhere
    assert str(exclude).lower() in text
    assert "git add -a" in text  # the symptom, lowercased by _validate_output
    assert "review each before deleting it" in text  # the remedy


def test_validate_does_not_flag_an_operator_authored_exclude_line(project, capsys, monkeypatch):
    """A line for any path that is not a shield pattern is the operator's own, and
    telling them to delete it would be wrong. Exact match only — never substring
    or containment (ablation: widen the match and this fails)."""
    _make_validate_pass(project, monkeypatch, capsys)
    _pollute_exclude(project, "/scratch", "/.claude/skills-mine", ".claude/skills")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    assert not [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]


def test_validate_silent_when_the_exclude_file_is_absent(project, capsys, monkeypatch):
    """A repo never provisioned by an old bmad-loop gets no warning at all."""
    _make_validate_pass(project, monkeypatch, capsys)
    (project.project / ".git" / "info" / "exclude").unlink(missing_ok=True)

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    assert not [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]


def test_validate_pollution_reports_a_profile_this_policy_never_selects(
    project, capsys, monkeypatch
):
    """The policy names claude only, but the residue is a gemini path — written when
    the project ran on a different CLI. Candidates come from every DISCOVERED
    profile, so switching CLIs cannot hide the pollution.

    Ablation: build the candidate set from the role-resolved `profiles` instead
    of `_legacy_exclude_candidate_sources`' union and this stops reporting."""
    from bmad_loop.adapters.profile import get_profile

    _make_validate_pass(project, monkeypatch, capsys)
    gemini = get_profile("gemini")
    _pollute_exclude(project, f"/{gemini.hooks.config_path}")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1
    assert warned[0]["detail"]["lines"] == [f"/{gemini.hooks.config_path}"]


def test_validate_pollution_reports_a_seed_file_though_seeding_is_off_today(
    project, capsys, monkeypatch
):
    """`seed_adapter_defaults` describes the policy NOW; the polluting run had its
    own. `.mcp.json` is reachable only through a profile's `seed_files`, so with
    the union re-gated on today's flag this residue would report nothing.

    Ablation: gate the `seed_files` union on `pol.scm.seed_adapter_defaults` (or
    drop it) and this fails."""
    _make_validate_pass(
        project,
        monkeypatch,
        capsys,
        policy=CLAUDE_ONLY_POLICY + "[scm]\nseed_adapter_defaults = false\n",
    )
    _pollute_exclude(project, "/.mcp.json")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1
    assert warned[0]["detail"]["lines"] == ["/.mcp.json"]


def test_validate_pollution_reports_a_plugin_seed_glob_expansion(project, capsys, monkeypatch):
    """The third seed source. A pre-#385 run expanded each plugin `seed_globs`
    pattern and wrote one exclude line PER MATCH, so a repo can carry lines no
    profile and no policy entry can explain.

    Ablation: drop the manifest loop from `_legacy_exclude_candidate_sources`
    and this stops reporting."""
    _make_validate_pass(project, monkeypatch, capsys)
    root = project.project
    (root / ".bmad-loop" / "plugins" / "seedy").mkdir(parents=True, exist_ok=True)
    (root / ".bmad-loop" / "plugins" / "seedy" / "plugin.toml").write_text(
        '[plugin]\nname = "seedy"\napi_version = 1\nseed_globs = ["vendored/*"]\n',
        encoding="utf-8",
    )
    (root / "vendored").mkdir(exist_ok=True)
    (root / "vendored" / "tool.json").write_text("{}\n", encoding="utf-8")
    # commit first: the fixture's own files would otherwise fail git.worktree-clean,
    # and the exclude has to land after the commit anyway (see _pollute_exclude)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "plugin seed fixture")
    capsys.readouterr()
    _pollute_exclude(project, "/vendored/tool.json")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1
    assert warned[0]["detail"]["lines"] == ["/vendored/tool.json"]


def test_validate_pollution_reports_a_plugin_disabled_since_the_polluting_run(
    project, capsys, monkeypatch
):
    """The plugin half of the current-policy narrowing. `PluginRegistry.seed_files`
    and `seed_globs` filter through `_active_for_seeds`, which requires a `[python]`
    plugin to be in `[plugins] enabled` — so a plugin that was enabled when the
    polluting run happened and is disabled today contributes NO candidates, and its
    lines keep hiding new files unreported. Measured with nothing enabled: both
    accessors return empty, dropping even the shipped tea and unity globs.

    Reconstruction is historical, so it reads every DISCOVERED manifest instead.

    Ablation: build the candidates from `registry.seed_files()`/`seed_globs()` and
    this stops reporting. The sibling glob test cannot catch it — its plugin has no
    `[python]`, so it is declarative and always active."""
    _make_validate_pass(project, monkeypatch, capsys)
    root = project.project
    plug = root / ".bmad-loop" / "plugins" / "offnow"
    plug.mkdir(parents=True, exist_ok=True)
    # `[python]` and NOT in `[plugins] enabled` -> untrusted, module never imported
    (plug / "plugin.toml").write_text(
        '[plugin]\nname = "offnow"\napi_version = 1\nseed_files = ["vendored/off.json"]\n'
        '[python]\nmodule = "p.py"\n',
        encoding="utf-8",
    )
    (plug / "p.py").write_text("x = 1\n", encoding="utf-8")
    (root / "vendored").mkdir(exist_ok=True)
    (root / "vendored" / "off.json").write_text("{}\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "disabled plugin fixture")
    capsys.readouterr()
    _pollute_exclude(project, "/vendored/off.json")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1, "a plugin disabled TODAY still explains the line"
    assert warned[0]["detail"]["lines"] == ["/vendored/off.json"]


def test_validate_pollution_reports_plugin_lines_though_the_policy_is_malformed(
    project, capsys, monkeypatch
):
    """A policy that fails to parse is a fact about TODAY's config, and the
    reconstruction target is historical — the same rule as the profile set and the
    `_active_for_seeds` filter, one source over. Manifest discovery never reads the
    policy (`discovered_manifests` takes only the project root; policy governs the
    settings overlay and the trust gate, neither of which is a manifest), so a
    broken policy.toml must not shrink the candidate set. It is the one run where
    the operator is already editing that file, so a silent drop there is the worst
    place for one.

    Ablation: put the manifest loop back inside `if pol is not None:` and this
    reports nothing."""
    _make_validate_pass(project, monkeypatch, capsys)
    root = project.project
    plug = root / ".bmad-loop" / "plugins" / "declarative"
    plug.mkdir(parents=True, exist_ok=True)
    # No `[python]`, so `_active_for_seeds` keeps it either way: the policy guard is
    # the only thing that can drop this line, which is what makes the ablation exact.
    (plug / "plugin.toml").write_text(
        '[plugin]\nname = "declarative"\napi_version = 1\nseed_files = ["vendored/tool.json"]\n',
        encoding="utf-8",
    )
    (root / "vendored").mkdir(exist_ok=True)
    (root / "vendored" / "tool.json").write_text("{}\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "declarative plugin fixture")
    capsys.readouterr()
    _pollute_exclude(project, "/vendored/tool.json")
    (root / ".bmad-loop" / "policy.toml").write_text("not = = toml\n", encoding="utf-8")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)

    problems = [f["check"] for f in doc["findings"] if f["severity"] == "problem"]
    assert "policy" in problems, "non-vacuity: the policy must really have failed to parse"
    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1, "a policy that will not parse still cannot hide plugin lines"
    assert warned[0]["detail"]["lines"] == ["/vendored/tool.json"]


def test_validate_pollution_survives_a_malformed_profile_overlay(project, capsys, monkeypatch):
    """`load_profiles` overlays `.bmad-loop/profiles/*.toml` onto the packaged
    built-ins and raises on the first malformed one, discarding the built-ins it had
    already collected — so this run used to leave the candidate set with NO profile
    entries and `/.claude/skills` unreported, in the very run that reported the
    overlay error. `discovered_profiles` parses per item: the broken overlay skips
    only itself and the packaged profiles still contribute.

    Ablation: route the candidate profiles through `load_profiles` (the effective
    map) and this reports nothing."""
    _make_validate_pass(project, monkeypatch, capsys)
    root = project.project
    overlay = root / ".bmad-loop" / "profiles"
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / "broken.toml").write_text("this is not = = toml\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "malformed profile overlay")
    capsys.readouterr()
    _pollute_exclude(project, "/.claude/skills")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)

    problems = [f["check"] for f in doc["findings"] if f["severity"] == "problem"]
    assert "adapter.profile" in problems, "non-vacuity: the overlay must really be rejected"
    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1, "one bad overlay cannot hide the packaged profiles' lines"
    assert warned[0]["detail"]["lines"] == ["/.claude/skills"]


def test_validate_never_runs_plugin_code_while_building_candidates(project, capsys, monkeypatch):
    """The candidate set needs MANIFESTS, so it reads `discovered_manifests`,
    never `PluginRegistry.build` — which resolves, and for an enabled `[python]`
    plugin that means `exec_module` plus the constructor: third-party code inside
    a preflight.

    Two contracts break when it runs. A top-level `print` puts its line ahead of the
    document, so the whole of stdout stops being one JSON object (AGENTS.md's `--json`
    invariant, asserted here by `machine_json` parsing all of stdout). And a top-level
    write lands a new file in the repo AFTER `git.worktree-clean` has already passed.

    Ablation: build the candidates through `PluginRegistry.build(project, pol).plugins()`
    and this fails on the stdout contract."""
    _make_validate_pass(project, monkeypatch, capsys)
    root = project.project
    plug = root / ".bmad-loop" / "plugins" / "noisy"
    plug.mkdir(parents=True, exist_ok=True)
    (plug / "plugin.toml").write_text(
        '[plugin]\nname = "noisy"\napi_version = 1\nseed_files = ["vendored/n.json"]\n'
        '[python]\nmodule = "p.py"\ncls = "P"\n',
        encoding="utf-8",
    )
    (plug / "p.py").write_text(
        "import pathlib\n"
        "print('PLUGIN TOP-LEVEL STDOUT')\n"
        "pathlib.Path('MUTATED-BY-PLUGIN.txt').write_text('x', encoding='utf-8')\n"
        "class P:\n"
        "    def __init__(self, *a, **k):\n"
        "        print('PLUGIN CONSTRUCTOR STDOUT')\n",
        encoding="utf-8",
    )
    (root / "vendored").mkdir(exist_ok=True)
    (root / "vendored" / "n.json").write_text("{}\n", encoding="utf-8")
    pol_file = root / ".bmad-loop" / "policy.toml"
    pol_file.write_text(
        pol_file.read_text(encoding="utf-8") + '\n[plugins]\nenabled = ["noisy"]\n',
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "enabled python plugin fixture")
    capsys.readouterr()
    _pollute_exclude(project, "/vendored/n.json")
    # the plugin's write is relative, so it would land wherever validate is run from
    monkeypatch.chdir(root)

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1, "non-vacuity: the plugin's manifest must still be read"
    assert warned[0]["detail"]["lines"] == ["/vendored/n.json"]
    assert not (root / "MUTATED-BY-PLUGIN.txt").exists(), "preflight ran plugin code"


def test_validate_pollution_detail_splits_by_tracked_content(project, capsys, monkeypatch):
    """`.claude/skills` is tracked by the fixture, so the rule over it can only be
    hiding that path's NEW files; nothing is tracked under the seeded customize dir.

    Ablation: classify every hit as `hiding_new_files` and this fails."""
    _make_validate_pass(project, monkeypatch, capsys)
    _pollute_exclude(project, "/.claude/skills", "/_bmad/custom")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    detail = next(f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution")[
        "detail"
    ]
    assert detail["hiding_new_files"] == ["/.claude/skills"], "tracked by the fixture"
    assert detail["no_tracked_content"] == ["/_bmad/custom"], "nothing tracked under it"
    assert sorted(detail["hiding_new_files"] + detail["no_tracked_content"]) == detail["lines"]


def test_validate_pollution_neutralized_line_is_reported_without_an_effect_claim(
    project, capsys, monkeypatch
):
    """A `!` line below a hit can re-include the path (gitignore is last-match-
    wins), leaving the hit inert — so the message must not claim it is hiding new
    files. The hit still reports (residue to review either way) under the hedged
    `maybe_neutralized` bucket.

    Ablation: classify every tracked hit as `hiding_new_files` and both detail
    asserts fail."""
    _make_validate_pass(project, monkeypatch, capsys)
    _pollute_exclude(project, "/.claude/skills", "!/.claude/skills", "!/.claude/skills/**")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    finding = next(f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution")
    assert finding["detail"]["maybe_neutralized"] == ["/.claude/skills"]
    assert finding["detail"]["hiding_new_files"] == []
    assert "may re-include them" in finding["message"]
    assert "hiding that path's NEW files" not in finding["message"]


def test_validate_pollution_tracked_config_file_gets_no_effect_claim(project, capsys, monkeypatch):
    """`.claude/settings.json` is a first-class shield candidate (`hooks.config_path`)
    and the fixture tracks it as a regular FILE. An exclude suppresses neither a
    tracked path nor "new files under" a non-directory, so the strong sentence would
    be false — this is the common case, not an edge one, and the sibling directory
    line in the same run must keep the strong claim.

    Ablation: grade on `tracked` alone (drop the descendants probe) and the settings
    line moves back into `hiding_new_files`, failing both detail asserts."""
    _make_validate_pass(project, monkeypatch, capsys)
    _pollute_exclude(project, "/.claude/settings.json", "/.claude/skills")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    finding = next(f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution")
    assert finding["detail"]["tracked_file_no_children"] == ["/.claude/settings.json"]
    assert finding["detail"]["hiding_new_files"] == ["/.claude/skills"], "the dir keeps the claim"
    assert "not hiding anything today" in finding["message"]
    assert doc["ok"] is True


def test_validate_pollution_withholds_the_effect_claim_from_a_glob_line(
    project, capsys, monkeypatch
):
    """A hit whose pattern carries gitignore syntax gets no effect claim: `/a[bc]d`
    covers `abd` and `acd`, never the tracked `a[bc]d/` it spells, so the strong
    sentence asserted the opposite of what git does. The sibling literal line in
    the same run must keep its claim — this withholds a verdict, it does not
    switch the check off.

    Ablation: drop the `PATTERN_SPECIALS` guard and the class line joins
    `hiding_new_files`, failing the first two asserts."""
    root = project.project
    (root / "a[bc]d").mkdir()
    (root / "a[bc]d" / "f.txt").write_text("x\n", encoding="utf-8")
    # `worktree_seed` is what makes `/a[bc]d` a CANDIDATE — a bracketed directory
    # with no seed source naming it is not a hit at all, and the test would pass
    # for the wrong reason. It loads: no seed-boundary predicate rejects a `[`.
    _make_validate_pass(
        project,
        monkeypatch,
        capsys,
        policy=CLAUDE_ONLY_POLICY + '[scm]\nworktree_seed = ["a[bc]d"]\n',
    )
    _pollute_exclude(project, "/a[bc]d", "/.claude/skills")

    doc = machine_json(["validate", "--project", str(root), "--json"], capsys)

    finding = next(f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution")
    assert finding["detail"]["pattern_not_plain_path"] == ["/a[bc]d"]
    assert finding["detail"]["hiding_new_files"] == ["/.claude/skills"], "the literal line keeps it"
    assert "carry gitignore pattern syntax" in finding["message"]
    assert "/a[bc]d" in finding["detail"]["lines"], "still reported, still deletable"


def test_validate_pollution_withholds_the_effect_claim_from_a_non_canonical_line(
    project, capsys, monkeypatch
):
    """The other half of the same bucket, end to end: `/foo//bar` is INERT as an
    ignore pattern (git normalizes no segment of a pattern) while the `:(literal)`
    pathspec grading probes with DOES normalize it, so the line was reported as
    hiding `foo/bar`'s new files with a deletion prompt. The sibling literal line
    in the same run must keep its claim.

    `worktree_seed = ["foo//bar"]` is what makes it a candidate, and it is also the
    reachability proof: `policy.load` stores the spelling verbatim because each of
    its three boundary predicates normalizes internally before deciding.

    Ablation: restore `line.strip("/")` and `/foo//bar` joins `hiding_new_files`."""
    root = project.project
    (root / "foo" / "bar").mkdir(parents=True)
    (root / "foo" / "bar" / "f.txt").write_text("x\n", encoding="utf-8")
    _make_validate_pass(
        project,
        monkeypatch,
        capsys,
        policy=CLAUDE_ONLY_POLICY + '[scm]\nworktree_seed = ["foo//bar"]\n',
    )
    _pollute_exclude(project, "/foo//bar", "/.claude/skills")

    doc = machine_json(["validate", "--project", str(root), "--json"], capsys)

    finding = next(f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution")
    assert finding["detail"]["pattern_not_plain_path"] == ["/foo//bar"]
    assert finding["detail"]["hiding_new_files"] == ["/.claude/skills"], "the literal line keeps it"
    assert "spell a path git reads two ways" in finding["message"]
    assert "/foo//bar" in finding["detail"]["lines"], "still reported, still deletable"


def test_validate_pollution_ignores_a_root_naming_exclude_line(project, capsys, monkeypatch):
    """A plugin's `seed_globs = ["**"]` expands to include the project root itself,
    whose rel is `"."` — so `/.` reached the candidate set and an operator's own
    `/.` line was reported with a deletion prompt, in the strongest bucket.

    Dropped for the same reason the bare `/` is: root-naming lines are inert as
    gitignore patterns, plausible for an operator to have written, and no config
    that still loads can seed the root.

    Ablation: restore `candidates.discard("/")` and this fails."""
    _make_validate_pass(project, monkeypatch, capsys)
    root = project.project
    d = root / ".bmad-loop" / "plugins" / "starry"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.toml").write_text(
        '[plugin]\nname = "starry"\napi_version = 1\nseed_globs = ["**"]\n', encoding="utf-8"
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "root-glob plugin fixture")
    capsys.readouterr()
    # `/vendored` is the non-vacuity pin: the same `**` expansion still reconstructs
    # ordinary matches, so this is not "the manifest went unread". A DIRECTORY, not
    # the file inside it: `Path.glob("**")` yields directories only before Python
    # 3.13, so pinning on `/vendored/tool.json` made this test pass on 3.13+ and
    # fail on 3.11/3.12 for a reason that has nothing to do with what it asserts.
    (root / "vendored").mkdir(exist_ok=True)
    (root / "vendored" / "tool.json").write_text("{}\n", encoding="utf-8")
    _pollute_exclude(project, "/.", "/vendored")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    finding = next(f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution")
    assert finding["detail"]["lines"] == ["/vendored"], "the root line is not ours"


def test_validate_pollution_asks_for_review_before_deleting_every_line(
    project, capsys, monkeypatch
):
    """The remedy is a deletion and NOTHING here can attribute a line, so the review
    instruction covers every hit — including the tracked bucket, which grades what
    the line is doing rather than who wrote it. An operator can hold tracked content
    under a rule they wrote themselves, so a message that told them to delete the
    tracked ones outright would be telling them to delete their own ignore rule.

    Ablation: restore the earlier wording — move the review hedge onto the untracked
    bucket alone and tell the operator to `delete them by hand` for the tracked one
    — and both assertions below fail."""
    _make_validate_pass(project, monkeypatch, capsys)
    _pollute_exclude(project, "/.claude/skills", "/_bmad/custom")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    msg = next(f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution")[
        "message"
    ]
    assert "review each before deleting it" in msg, "the hedge covers all hits, not one bucket"
    assert "delete them by hand" not in msg, "no bucket may be presented as safe to delete"


def test_validate_pollution_reports_packaged_paths_beside_a_valid_same_name_overlay(
    project, capsys, monkeypatch
):
    """A VALID `.bmad-loop/profiles/claude.toml` replaces the packaged claude in
    the effective map — no fault anywhere, an ordinary override — and candidates
    built from that map lose the packaged profile's `config_path`/`seed_files`
    lines, though the pollution was written while the packaged profile was the
    one running. The union of every DISCOVERED profile keeps both sides.

    The overlay's own `/.claude/alt-settings.json` line is the non-vacuity pin:
    an overlay that failed to parse takes the ProfileError path and leaves that
    line with no candidate, failing the lines assert — an invalid overlay
    imitates a refutation (this PR's void-fixture family), so the fixture must
    prove the overlay APPLIED, which the hooks.registered detail does directly.

    The overlay deliberately does NOT list `.claude/settings.json` in its
    seed_files: the packaged claude's seed_files also carries that path, so an
    overlay keeping it would supply the packaged line itself and the
    union-vs-effective distinction would vanish for this fixture.

    Ablation: build `all_profiles` from `load_profiles` (the effective map) and
    the `/.claude/settings.json` line goes unreported."""
    _make_validate_pass(project, monkeypatch, capsys)
    root = project.project
    overlay = root / ".bmad-loop" / "profiles"
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / "claude.toml").write_text(
        'name = "claude"\n'
        'binary = "claude"\n'
        'seed_files = [".mcp.json"]\n'
        "[hooks]\n"
        'dialect = "claude-settings-json"\n'
        'config_path = ".claude/alt-settings.json"\n'
        'events = { Stop = "Stop" }\n',
        encoding="utf-8",
    )
    # the role loop resolves the OVERLAY now, so its config_path must carry the
    # relay registration for hooks.registered to stay green — the settings.json
    # `init` just wrote has it, and the copy keeps this test about the candidate
    # set rather than about a deliberately broken hook config
    settings = root / ".claude" / "settings.json"
    (root / ".claude" / "alt-settings.json").write_text(
        settings.read_text(encoding="utf-8"), encoding="utf-8"
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "valid same-name overlay fixture")
    capsys.readouterr()
    _pollute_exclude(project, "/.claude/settings.json", "/.claude/alt-settings.json")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    hooks_f = next(f for f in doc["findings"] if f["check"] == "hooks.registered")
    assert hooks_f["detail"]["config_path"] == str(
        root / ".claude" / "alt-settings.json"
    ), "non-vacuity: the overlay must really have replaced the packaged profile"
    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1, "an ordinary valid override cannot hide the packaged paths"
    assert warned[0]["detail"]["lines"] == [
        "/.claude/alt-settings.json",  # the overlay's path — proves it contributed
        "/.claude/settings.json",  # the packaged path the effective map loses
    ]


def test_validate_pollution_reports_builtin_lines_despite_a_malformed_project_manifest(
    project, capsys, monkeypatch
):
    """One malformed project `plugin.toml` used to make the manifest walk raise
    mid-stream, zeroing every BUILTIN manifest — and with them the builtin
    unity's `.claude/skills/*` glob candidates. Discovery is per-item now: the
    broken manifest skips only itself.

    Two breakage shapes: `a-broken` fails at the TOML layer, `a2-badfield` is
    valid TOML whose `priority` hits a raw `int()`. The second one escaped the
    per-item guard as a bare ValueError and took the whole command down before
    any document reached stdout — `machine_json` parsing all of stdout is what
    catches that here.

    `a-broken` sorts FIRST in the project dir, so the `/vendored/good.json`
    line doubles as the kill for a per-source `break`: a guard that stops the
    project source at its first fault loses b-good's line too.

    Ablation: build the manifests from `load_plugins` and validate crashes here
    instead of reporting; per-item `continue` -> `break` drops the b-good line;
    drop `load_manifest`'s conversion funnel and the command crashes with no
    JSON at all."""
    from bmad_loop.plugins import PluginError, load_plugins

    _make_validate_pass(project, monkeypatch, capsys)
    root = project.project
    plugins_dir = root / ".bmad-loop" / "plugins"
    (plugins_dir / "a-broken").mkdir(parents=True, exist_ok=True)
    (plugins_dir / "a-broken" / "plugin.toml").write_text("[plugin]\nname = \n", encoding="utf-8")
    (plugins_dir / "a2-badfield").mkdir(parents=True, exist_ok=True)
    (plugins_dir / "a2-badfield" / "plugin.toml").write_text(
        '[plugin]\nname = "a2-badfield"\napi_version = 1\npriority = "x"\n'
        'seed_files = ["vendored/never.json"]\n',
        encoding="utf-8",
    )
    (plugins_dir / "b-good").mkdir(parents=True, exist_ok=True)
    (plugins_dir / "b-good" / "plugin.toml").write_text(
        '[plugin]\nname = "b-good"\napi_version = 1\nseed_files = ["vendored/good.json"]\n',
        encoding="utf-8",
    )
    # the tree install_dev_base_skills laid down already holds .claude/skills
    (root / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "skills" / "keep.md").write_text("x\n", encoding="utf-8")
    (root / "vendored").mkdir(exist_ok=True)
    (root / "vendored" / "good.json").write_text("{}\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "malformed manifest fixture")
    capsys.readouterr()
    # non-vacuity: the effective loader really refuses this project dir, so the
    # candidates below cannot be coming from a load_plugins that happened to work
    with pytest.raises(PluginError):
        load_plugins(root)
    _pollute_exclude(
        project, "/.claude/skills/keep.md", "/vendored/good.json", "/vendored/never.json"
    )

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1, "one malformed manifest cannot hide the builtin lines"
    # never.json is a2-badfield's seed: a manifest today's schema cannot parse
    # contributes no candidate, the documented safe direction of this detector
    assert warned[0]["detail"]["lines"] == ["/.claude/skills/keep.md", "/vendored/good.json"]


def test_validate_pollution_reports_builtin_seeds_beside_a_same_name_project_plugin(
    project, capsys, monkeypatch
):
    """The plugin twin of the valid-overlay case: a project plugin named "unity"
    EVICTS the builtin unity from the effective map — an ordinary override, no
    fault — and with it the builtin's `.claude/skills/*` glob candidates. The
    union keeps both manifests, so the builtin's expansion line and the project
    plugin's own seed line are both reported.

    Declarative (no `[python]`) on purpose: `_active_for_seeds` and the trust
    gate keep such a plugin either way, so the NAME collision is the only
    mechanism that can drop a line here.

    Ablation: collapse the union name-keyed (dict.values()) and the
    `/.claude/skills/keep.md` line goes unreported."""
    from bmad_loop.plugins import load_plugins

    _make_validate_pass(project, monkeypatch, capsys)
    root = project.project
    plug = root / ".bmad-loop" / "plugins" / "unity"
    plug.mkdir(parents=True, exist_ok=True)
    (plug / "plugin.toml").write_text(
        '[plugin]\nname = "unity"\napi_version = 1\nseed_files = ["vendored/mine.json"]\n',
        encoding="utf-8",
    )
    (root / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "skills" / "keep.md").write_text("x\n", encoding="utf-8")
    (root / "vendored").mkdir(exist_ok=True)
    (root / "vendored" / "mine.json").write_text("{}\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "same-name project plugin fixture")
    capsys.readouterr()
    # non-vacuity: the eviction is real — the effective map holds the project one
    assert load_plugins(root)["unity"].source == "project"
    _pollute_exclude(project, "/.claude/skills/keep.md", "/vendored/mine.json")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)

    warned = [f for f in doc["findings"] if f["check"] == "git.exclude-legacy-pollution"]
    assert len(warned) == 1, "an ordinary same-name plugin cannot hide the builtin's lines"
    assert warned[0]["detail"]["lines"] == ["/.claude/skills/keep.md", "/vendored/mine.json"]


# --------------- `bmad-loop mux`: backend listing + persisted choice (issue #87) ----


class _MuxStub:
    """Selection-surface double (available/version only — `mux` needs no more)."""

    def __init__(self, avail=True, version=None):
        self._avail, self._version = avail, version

    def available(self):
        return self._avail

    def version(self):
        return self._version


@pytest.fixture
def mux_registry(monkeypatch):
    """Isolated multiplexer registry with builtins suppressed, so `mux` tests are
    deterministic regardless of whether the host has tmux installed."""
    from bmad_loop.adapters import multiplexer as m

    monkeypatch.delenv("BMAD_LOOP_MUX_BACKEND", raising=False)
    saved_backends = list(m._BACKENDS)
    saved_loaded = m._BUILTINS_LOADED
    saved_configured = m._CONFIGURED
    m._BACKENDS.clear()
    m._BUILTINS_LOADED = True  # suppress the real tmux builtin
    m._CONFIGURED = None
    m.get_multiplexer.cache_clear()
    yield m
    m._BACKENDS[:] = saved_backends
    m._BUILTINS_LOADED = saved_loaded
    m._CONFIGURED = saved_configured
    m.get_multiplexer.cache_clear()


def test_mux_lists_backends_and_selection(mux_registry, tmp_path, capsys):
    import sys as _sys

    mux_registry.register_multiplexer(
        "alpha", lambda p: p == _sys.platform, lambda: _MuxStub(avail=True, version="alpha 1.2")
    )
    mux_registry.register_multiplexer("beta", lambda p: False, lambda: _MuxStub(avail=False))
    assert cli.main(["mux", "--project", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "alpha 1.2" in out
    assert "beta" in out
    assert "selection: alpha (_MuxStub) — first available platform match" in out
    assert "bmad-loop mux set <name>" in out  # the no-prompt override hint


def test_mux_exits_1_on_forced_unknown_name(mux_registry, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "ghost")
    mux_registry.get_multiplexer.cache_clear()
    assert cli.main(["mux", "--project", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "NAME" in captured.out  # the listing still prints for diagnosis
    assert "ghost" in captured.err and "FAIL" in captured.err


def test_mux_set_persists_choice(mux_registry, tmp_path, capsys):
    import sys as _sys

    mux_registry.register_multiplexer(
        "alpha", lambda p: p == _sys.platform, lambda: _MuxStub(avail=True)
    )
    assert cli.main(["mux", "set", "alpha", "--project", str(tmp_path)]) == 0
    assert policy_mod.load(tmp_path / ".bmad-loop" / "policy.toml").mux.backend == "alpha"
    assert 'mux backend set to "alpha"' in capsys.readouterr().out


def test_mux_set_unavailable_backend_warns_but_persists(mux_registry, tmp_path, capsys):
    mux_registry.register_multiplexer("alpha", lambda p: False, lambda: _MuxStub(avail=False))
    assert cli.main(["mux", "set", "alpha", "--project", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "not available on this host" in captured.err
    assert policy_mod.load(tmp_path / ".bmad-loop" / "policy.toml").mux.backend == "alpha"


def test_mux_set_unregistered_errors_without_force(mux_registry, tmp_path, capsys):
    assert cli.main(["mux", "set", "ghost", "--project", str(tmp_path)]) == 1
    assert "not a registered backend" in capsys.readouterr().err
    assert not (tmp_path / ".bmad-loop" / "policy.toml").exists()


def test_mux_set_force_persists_unregistered_name(mux_registry, tmp_path):
    assert cli.main(["mux", "set", "ghost", "--force", "--project", str(tmp_path)]) == 0
    assert policy_mod.load(tmp_path / ".bmad-loop" / "policy.toml").mux.backend == "ghost"


def test_mux_set_requires_a_name(mux_registry, tmp_path, capsys):
    assert cli.main(["mux", "set", "--project", str(tmp_path)]) == 1
    assert "requires a backend name" in capsys.readouterr().err


def test_mux_set_clear_returns_to_auto(mux_registry, tmp_path):
    mux_registry.register_multiplexer("alpha", lambda p: False, lambda: _MuxStub())
    assert cli.main(["mux", "set", "alpha", "--project", str(tmp_path)]) == 0
    assert cli.main(["mux", "set", "--clear", "--project", str(tmp_path)]) == 0
    assert policy_mod.load(tmp_path / ".bmad-loop" / "policy.toml").mux.backend == ""


def test_mux_set_notes_env_override_shadowing(mux_registry, tmp_path, capsys, monkeypatch):
    mux_registry.register_multiplexer("alpha", lambda p: False, lambda: _MuxStub())
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "alpha")
    assert cli.main(["mux", "set", "alpha", "--project", str(tmp_path)]) == 0
    assert "outranks the persisted choice" in capsys.readouterr().err


def test_policy_mux_choice_reaches_selection_through_main(mux_registry, tmp_path, capsys):
    """End-to-end chokepoint proof: a [mux] backend persisted in policy.toml is
    honored by a fresh CLI invocation (main() installs it before dispatch)."""
    mux_registry.register_multiplexer("fake", lambda p: False, lambda: _MuxStub(avail=True))
    _write_policy(tmp_path, '[mux]\nbackend = "fake"\n')
    assert cli.main(["mux", "--project", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "selection: fake (_MuxStub) — set by [mux] backend" in out


def test_policy_mux_unknown_name_fails_loud_through_main(mux_registry, tmp_path, capsys):
    """A stale persisted choice (backend uninstalled later) must fail loudly and
    name the policy file to edit."""
    _write_policy(tmp_path, '[mux]\nbackend = "ghost"\n')
    assert cli.main(["mux", "--project", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "ghost" in err and "policy.toml" in err


def test_platform_preflight_lists_multiple_backends(mux_registry, monkeypatch):
    import sys as _sys

    mux_registry.register_multiplexer(
        "alpha", lambda p: p == _sys.platform, lambda: _MuxStub(avail=True, version="alpha 1.2")
    )
    mux_registry.register_multiplexer("beta", lambda p: False, lambda: _MuxStub(avail=False))
    notes, problems = _preflight_notes_problems()
    assert any("mux backends:" in n and "alpha*" in n and "beta (unavailable)" in n for n in notes)


def test_platform_preflight_single_backend_gets_no_listing(mux_registry):
    mux_registry.register_multiplexer("alpha", lambda p: True, lambda: _MuxStub(avail=True))
    notes, problems = _preflight_notes_problems()
    assert not any("mux backends:" in n for n in notes)


def test_platform_preflight_notes_forced_selection_provenance(mux_registry, monkeypatch):
    mux_registry.register_multiplexer("alpha", lambda p: False, lambda: _MuxStub(avail=True))
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "alpha")
    mux_registry.get_multiplexer.cache_clear()
    notes, problems = _preflight_notes_problems()
    assert any("forced by BMAD_LOOP_MUX_BACKEND" in n for n in notes)


# ---------------------------------------- bmad-loop confirm (#335 part 3, #356)
#
# The exit from `awaiting-operator`. Out of band by construction: the run that
# parked the story is long finished and its task is terminal, so `confirm` writes
# only the two things that DEFINE completion (the spec and the board) and drops
# the park record that pointed at them.

CONFIRM_ACTIONS = ["buy example.com at the registrar", "publish the _acme-challenge TXT record"]


def _park_story(paths, key="1-1-a", *, actions=None, spec_status="awaiting-operator", board=None):
    """Put the project in the state a park leaves behind: spec, board, record."""
    from bmad_loop import operatoractions

    acts = CONFIRM_ACTIONS if actions is None else actions
    sp = spec_path(paths, key)
    write_spec(sp, spec_status, "base", operator_actions=acts)
    write_sprint(paths, {"epic-1": "in-progress", key: board or "awaiting-operator"})
    operatoractions.record_park(
        paths.project,
        key,
        actions=list(acts) if isinstance(acts, list) else [],
        spec_file=sp.relative_to(paths.project).as_posix(),
        run_id="run-1",
        parked_at="2026-07-28",
    )
    return sp


def _confirm_argv(paths, *extra):
    return ["confirm", "--project", str(paths.project), *extra]


def test_confirm_flips_spec_board_record_and_commits(project, capsys, monkeypatch):
    """The happy path, end to end. All four records move together: the spec gains
    its audit section and reaches done, the board advances through the sole
    writer, the park record goes, and the set is committed — the record's
    DELETION included, because the record arrived in the park's commit (#356)
    and leaving its removal uncommitted would dirty the tree the next run's
    preflight refuses.

    Ablation: delete the `sprintstatus.advance` call in `_apply_confirmation` and
    this fails on the board assertion — a spec saying done over a board still
    parked is the false-green the state exists to prevent. For the deletion leg:
    drop `record` from `_land_confirmation`'s `commit_paths` list and this fails
    on the name-only assertion, leaving a staged-nothing deletion dirtying the
    tree."""
    from bmad_loop import devcontract, operatoractions, sprintstatus, verify

    install_bmad_config(project)
    sp = _park_story(project)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "park")
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 0

    text = sp.read_text()
    assert "status: 'done'" in text or "status: done" in text
    assert "## Operator Confirmation" in text
    assert devcontract.OPERATOR_CONFIRM_NOTE in text
    for action in CONFIRM_ACTIONS:
        assert f"- {action}" in text
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "done"
    assert operatoractions.load(project.project) == {}
    # the spec, board and record deletion moved into history together
    assert "confirm 1-1-a" in git(project.project, "log", "-1", "--format=%s")
    changed = git(project.project, "show", "--name-only", "--format=", "HEAD")
    assert ".bmad-loop/operator/1-1-a.json" in changed
    assert verify.worktree_clean(project.project)
    assert "✓ 1-1-a confirmed" in capsys.readouterr().out


def test_confirm_asks_about_every_action_separately(project, capsys, monkeypatch):
    """One blanket "all done?" invites exactly the failure this state exists to
    prevent — a story marked done with some obligations quietly unmet."""
    install_bmad_config(project)
    _park_story(project)
    asked = []
    monkeypatch.setattr(cli, "_confirm", lambda q: asked.append(q) or True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 0
    assert len(asked) == len(CONFIRM_ACTIONS)
    for action in CONFIRM_ACTIONS:
        assert any(action in q for q in asked)


def test_confirm_declined_changes_nothing(project, capsys, monkeypatch):
    """A declined confirmation is a successful no-op (rc 0), and it must not have
    half-written anything — declining the SECOND action still leaves the first
    unacknowledged on disk."""
    from bmad_loop import operatoractions, sprintstatus

    install_bmad_config(project)
    sp = _park_story(project)
    before = sp.read_text()
    answers = iter([True, False])
    monkeypatch.setattr(cli, "_confirm", lambda _q: next(answers))

    assert cli.main(_confirm_argv(project, "1-1-a")) == 0
    assert "cancelled" in capsys.readouterr().out
    assert sp.read_text() == before
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "awaiting-operator"
    assert "1-1-a" in operatoractions.load(project.project)


def test_confirm_yes_skips_the_prompts_but_still_prints_the_actions(project, capsys, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("--yes must not prompt")

    monkeypatch.setattr("builtins.input", _boom)
    install_bmad_config(project)
    _park_story(project)

    assert cli.main(_confirm_argv(project, "1-1-a", "--yes")) == 0
    out = capsys.readouterr().out
    for action in CONFIRM_ACTIONS:
        assert action in out  # unattended still means auditable


def test_confirm_unknown_story_explains_the_committed_record(project, capsys):
    """Since #356 the record travels with the story's commit, so "not here" means
    the checkout does not carry it (or the story was never parked) — the error
    points at pulling the branch that does, never at some other machine."""
    install_bmad_config(project)
    write_sprint(project, {"epic-1": "in-progress"})

    assert cli.main(_confirm_argv(project, "9-9-z")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not awaiting operator actions in this checkout" in captured.err
    assert ".bmad-loop/operator" in captured.err and "pull the branch" in captured.err
    assert "--list" in captured.err


@pytest.mark.parametrize(
    ("spec_status", "board", "expected"),
    [
        ("done", "awaiting-operator", "its spec now says status: done"),
        ("awaiting-operator", "done", "the board now says done"),
    ],
)
def test_confirm_refuses_a_drifted_entry(
    project, capsys, monkeypatch, spec_status, board, expected
):
    """The index can drift from the committed truth it points at. `confirm` must
    refuse rather than flip a board on the index's word alone.

    Ablation: drop the `drift()` guard in `cmd_confirm` and this fails — the
    stale entry is confirmed and the disagreement is silently overwritten."""
    from bmad_loop import operatoractions

    install_bmad_config(project)
    sp = _park_story(project, spec_status=spec_status, board=board)
    before = sp.read_text()
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err and "Nothing was changed" in captured.err
    assert sp.read_text() == before
    assert "1-1-a" in operatoractions.load(project.project)  # left for repair


def test_confirm_refuses_a_park_declaring_nothing(project, capsys, monkeypatch):
    """A park is DEFINED by owing at least one action; an entry with none is not
    something a human can acknowledge."""
    install_bmad_config(project)
    _park_story(project, actions=[])
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 1
    assert "no readable actions" in capsys.readouterr().err


def test_confirm_list_shows_what_each_story_owes(project, capsys):
    install_bmad_config(project)
    _park_story(project)

    assert cli.main(_confirm_argv(project, "--list")) == 0
    out = capsys.readouterr().out
    assert "1-1-a" in out and "parked 2026-07-28" in out
    for action in CONFIRM_ACTIONS:
        assert action in out


def test_confirm_list_marks_a_drifted_entry_rather_than_hiding_it(project, capsys):
    install_bmad_config(project)
    _park_story(project, spec_status="done")

    assert cli.main(_confirm_argv(project, "--list")) == 0
    assert "NOT CONFIRMABLE" in capsys.readouterr().out


def test_confirm_list_with_nothing_parked(project, capsys):
    install_bmad_config(project)
    write_sprint(project, {"epic-1": "in-progress"})

    assert cli.main(_confirm_argv(project, "--list")) == 0
    assert "no stories are awaiting operator actions" in capsys.readouterr().out


def test_confirm_with_no_key_and_no_list_is_a_usage_error(project, capsys):
    install_bmad_config(project)
    _park_story(project)

    assert cli.main(_confirm_argv(project)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--list" in captured.err


def test_confirm_json_is_a_pure_document(project, capsys):
    install_bmad_config(project)
    _park_story(project)

    doc = machine_json(_confirm_argv(project, "--json"), capsys)
    assert doc["schema_version"] == cli.CONFIRM_SCHEMA_VERSION
    (entry,) = doc["parked"]
    assert entry["story_key"] == "1-1-a"
    assert entry["actions"] == CONFIRM_ACTIONS
    assert entry["confirmable"] is True and entry["drift"] is None
    # `commit` is DERIVED provenance since #356 — "" for this uncommitted record,
    # a real sha once the park's commit exists. Presence and type are stable.
    assert entry["commit"] == "" and entry["run_id"] == "run-1"


def test_confirm_json_carries_both_sides_of_a_disagreement(project, capsys):
    """A consumer triaging a stuck board needs to see WHICH side disagreed;
    `confirmable` alone would say a story cannot be confirmed without ever saying
    what is wrong with it."""
    install_bmad_config(project)
    _park_story(project, spec_status="done")

    doc = machine_json(_confirm_argv(project, "--json"), capsys)
    (entry,) = doc["parked"]
    assert entry["spec_status"] == "done" and entry["board_status"] == "awaiting-operator"
    assert entry["confirmable"] is False and "done" in entry["drift"]


def test_confirm_json_with_nothing_parked_is_an_empty_document(project, capsys):
    install_bmad_config(project)
    write_sprint(project, {"epic-1": "in-progress"})

    assert machine_json(_confirm_argv(project, "--json"), capsys)["parked"] == []


def test_confirm_json_never_reaches_the_prompt(project, capsys, monkeypatch):
    """--json IS the listing. It cannot fall through to a path that reads stdin —
    neither the prompt nor its progress prints survive a pure document."""

    def _boom(*a, **k):
        raise AssertionError("--json must not prompt")

    monkeypatch.setattr("builtins.input", _boom)
    monkeypatch.setattr(cli, "_confirm", _boom)
    install_bmad_config(project)
    _park_story(project)

    machine_json(_confirm_argv(project, "1-1-a", "--json"), capsys)


def test_confirm_json_error_keeps_stdout_empty(project, capsys):
    """No BMAD config: the message goes to stderr, stdout stays empty, rc is 1 —
    errors never produce a partial document."""
    assert cli.main(_confirm_argv(project, "--json")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "BMAD config not found" in captured.err


def test_confirm_reverify_failure_blocks_the_flip(project, capsys, monkeypatch):
    """The external action may have changed what the tests see — that is the whole
    reason --reverify exists. A failure must leave every record untouched.

    Ablation: ignore `_reverify`'s return value and this fails — the story is
    confirmed on top of a red verify command."""
    from bmad_loop import operatoractions, sprintstatus

    install_bmad_config(project)
    sp = _park_story(project)
    before = sp.read_text()
    _write_policy(project.project, '[verify]\ncommands = ["python -c \\"raise SystemExit(3)\\""]\n')
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a", "--reverify")) == 1
    captured = capsys.readouterr()
    assert "--reverify failed" in captured.err and "NOT confirmed" in captured.err
    assert sp.read_text() == before
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "awaiting-operator"
    assert "1-1-a" in operatoractions.load(project.project)


def test_confirm_reverify_success_lets_the_flip_through(project, capsys, monkeypatch):
    from bmad_loop import sprintstatus

    install_bmad_config(project)
    _park_story(project)
    _write_policy(project.project, '[verify]\ncommands = ["python -c \\"pass\\""]\n')
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a", "--reverify")) == 0
    assert "verify commands passed" in capsys.readouterr().out
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "done"


def test_confirm_reverify_says_so_when_nothing_is_configured(project, capsys, monkeypatch):
    """An empty command list is not a green gate, and must not be reported as one."""
    install_bmad_config(project)
    _park_story(project)
    _write_policy(project.project, "[verify]\ncommands = []\n")
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a", "--reverify")) == 0
    out = capsys.readouterr().out
    assert "no [verify] commands are configured" in out
    assert "verify commands passed" not in out


def test_confirm_survives_a_non_git_project(project, capsys, monkeypatch):
    """The files are the state; git history is the record of it. A commit failure
    must not undo a confirmation that already happened on disk."""
    from bmad_loop import sprintstatus, verify

    install_bmad_config(project)
    _park_story(project)
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    def boom(*a, **k):
        raise verify.GitError("not a git repository")

    monkeypatch.setattr(cli.verify, "commit_paths", boom)
    assert cli.main(_confirm_argv(project, "1-1-a")) == 0
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "done"


def test_confirm_commits_even_without_a_committed_record(project, capsys, monkeypatch):
    """A pre-#356 park has a legacy machine-local entry and NO record file — so
    `_land_confirmation` hands `commit_paths` a record path git has never seen.
    `git add` hard-fails on a pathspec matching nothing, the `GitError` is
    swallowed as best-effort, and the spec+board commit would silently vanish
    behind a "✓ confirmed".

    Ablation: revert `commit_paths`' missing-and-untracked filter and this fails
    on the log assertion — confirm reports success, but nothing was committed."""
    from bmad_loop import operatoractions, sprintstatus, verify

    install_bmad_config(project)
    sp = _park_story(project)
    operatoractions.record_path(project.project, "1-1-a").unlink()  # old versions wrote none
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "park (pre-#356)")
    # the machine-local entry, written AFTER the commit the way the old version did
    store = operatoractions.legacy_store_path(project.project)
    store.write_text(
        json.dumps(
            {
                "1-1-a": {
                    "actions": CONFIRM_ACTIONS,
                    "spec_file": sp.relative_to(project.project).as_posix(),
                    "commit": "abcdef1234567890",
                    "run_id": "run-legacy",
                    "parked_at": "2026-07-01",
                }
            }
        )
    )
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 0
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "done"
    # the commit LANDED despite the never-committed record path in its list
    assert "confirm 1-1-a" in git(project.project, "log", "-1", "--format=%s")
    assert operatoractions.load(project.project) == {}  # legacy entry pruned too
    assert verify.worktree_clean(project.project)


def test_confirm_works_on_a_fresh_clone_of_a_parked_repo(project, capsys, monkeypatch, tmp_path):
    """The #356 acceptance criterion at the CLI layer: the record travels with
    the park commit, so a clone that never ran the orchestrator can list AND
    confirm the story. Before #356 this exact flow refused with "confirm runs
    where the orchestrator ran"."""
    from bmad_loop import bmadconfig, sprintstatus

    install_bmad_config(project)
    _park_story(project)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "auto: 1-1-a (awaiting operator)")

    clone = tmp_path / "fresh-clone"
    git(tmp_path, "clone", "-q", str(project.project), str(clone))
    git(clone, "config", "user.email", "operator@test")
    git(clone, "config", "user.name", "operator")
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(["confirm", "--project", str(clone), "--list"]) == 0
    listing = capsys.readouterr().out
    assert "1-1-a" in listing
    for action in CONFIRM_ACTIONS:
        assert action in listing

    assert cli.main(["confirm", "--project", str(clone), "1-1-a"]) == 0
    clone_paths = bmadconfig.load_paths(clone)
    assert sprintstatus.story_status(clone_paths.sprint_status, "1-1-a") == "done"
    assert "confirm 1-1-a" in git(clone, "log", "-1", "--format=%s")


# ------------------------- confirm: a write that did not land is not a confirmation
#
# `confirm` used to discard both spec-write results and print "✓ confirmed"
# regardless. Each of these pins a way a write can fail to land, and each asserts
# the resulting STATE — the board never moves and the index entry stays, so the
# command is still re-runnable.


def _unrewritable_spec(paths, key="1-1-a"):
    """A spec whose status reads as `awaiting-operator` (so `drift()` clears and
    `confirm` proceeds) but sits in a shape no in-place line edit can move: a
    literal block scalar. Replacing the `status: |` line orphans its indented
    continuation, so every candidate edit fails verification."""
    sp = _park_story(paths, key)
    sp.write_text(
        "---\n"
        "status: |\n"
        "  awaiting-operator\n"
        "baseline_revision: base\n"
        "operator_actions:\n" + "".join(f"  - {a}\n" for a in CONFIRM_ACTIONS) + "---\n\n"
        "# Story\n",
        encoding="utf-8",
    )
    return sp


def test_confirm_refuses_when_the_spec_status_cannot_be_rewritten(project, capsys, monkeypatch):
    """The seam that proves the frontmatter defect mattered. The reader sees
    `awaiting-operator`, so nothing upstream refuses; the writer cannot move it.
    Before the fix the discarded `False` still advanced the board, dropped the
    entry, and printed `✓ confirmed` over a spec still parked.

    ABLATION A1 (`frontmatter._edit_frontmatter_block`: turn the
    exhausted-candidates `raise` into `return False`) — run, and it FAILS here on
    the message. Recorded precisely, because the result is not what the plan for
    this predicted: the confirmation is still refused under A1, since the
    read-back guard below checks the FILE rather than the writer's word. What A1
    costs is the diagnosis — the human gets "still does not read status: done"
    instead of the shape that blocked it and the instruction to repair it. The
    raise is what carries the reason; the read-back is what carries the refusal.
    (A1 also fails 7 tests in tests/test_frontmatter.py, and `rearm_escalation`
    has no read-back at all, so there a `False` is silent.)"""
    from bmad_loop import operatoractions, sprintstatus

    install_bmad_config(project)
    sp = _unrewritable_spec(project)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "park")
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 1
    err = capsys.readouterr().err
    assert "carries a status this cannot rewrite" in err
    assert "NOT confirmed" in err and "re-run" in err
    # the audit section is already on disk, and this refusal ASKS for the re-run
    # that appends a second one — surfaced by smoke-testing a real project, where
    # taking the advice quietly doubled the sign-off record
    assert "SECOND" in err and "delete this one first" in err
    assert "awaiting-operator" in sp.read_text()  # the status never moved
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "awaiting-operator"
    assert "1-1-a" in operatoractions.load(project.project)  # still findable
    assert "confirm 1-1-a" not in git(project.project, "log", "-1", "--format=%s")


def test_confirm_refuses_when_the_spec_does_not_read_back_as_done(project, capsys, monkeypatch):
    """The guard behind the raise: a writer that reports success without landing.
    Unreachable through `set_frontmatter_status` today by construction — which is
    the point, since it covers what no return value can (an `atomic_replace`
    landing elsewhere on Windows, a shape nobody has enumerated, a concurrent
    writer). Simulated by a writer that claims True and writes nothing.

    Ablation: delete the read-back and this fails — the board advances and the
    index entry is dropped over a spec still parked."""
    from bmad_loop import operatoractions, sprintstatus

    install_bmad_config(project)
    _park_story(project)
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)
    monkeypatch.setattr(cli.frontmatter, "set_frontmatter_status", lambda *a, **k: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 1
    err = capsys.readouterr().err
    assert "still does not read status: done" in err and "NOT confirmed" in err
    assert "SECOND" in err and "delete this one first" in err
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "awaiting-operator"
    assert "1-1-a" in operatoractions.load(project.project)


def test_confirm_refuses_when_the_spec_vanished_before_the_audit_section(
    project, capsys, monkeypatch
):
    """`append_operator_confirmation` returns False in exactly one case — the spec
    is gone since `resolve` read it — and its docstring says "the caller decides
    whether that is fatal". It is: the section is the only record of the part of
    this story that happened outside the repository.

    Ablation: discard the return value and this fails — but on the MESSAGE, not
    the refusal, and that is the whole reason the guard is here rather than left
    to the read-back below. Without it the confirmation is still refused (an
    absent spec reads back as status "", not "done"), and the human is told "the
    audit section was appended" about a spec that no longer exists. A refusal that
    misreports what landed is what sends someone looking for a section to
    de-duplicate before re-running."""
    from bmad_loop import operatoractions, sprintstatus

    install_bmad_config(project)
    sp = _park_story(project)

    def _answer_and_delete(_q):  # the spec goes between `resolve` and the append
        sp.unlink(missing_ok=True)
        return True

    monkeypatch.setattr(cli, "_confirm", _answer_and_delete)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 1
    err = capsys.readouterr().err
    assert "disappeared" in err and "nothing was changed" in err
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "awaiting-operator"
    assert "1-1-a" in operatoractions.load(project.project)


def test_confirm_reports_a_board_that_did_not_advance(project, capsys, monkeypatch):
    """`sprintstatus.advance` returns the CURRENT status when its line regex
    cannot rewrite the entry `story_status` resolved via YAML — a quoted story key
    is enough, no mock required. That leaves the half-applied state the message
    has to be honest about: spec signed off and done, board still parked.

    Ablation: drop the `landed != "done"` branch and this fails — `✓ confirmed`
    is printed and the entry is dropped over a board still at awaiting-operator,
    with nothing left that can find the story."""
    from bmad_loop import devcontract, frontmatter, operatoractions, sprintstatus

    install_bmad_config(project)
    sp = _park_story(project)
    project.sprint_status.write_text(
        "last_updated: 01-06-2026 10:00\n"
        "development_status:\n"
        "  epic-1: in-progress\n"
        "  '1-1-a': awaiting-operator\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 1
    err = capsys.readouterr().err
    assert "did not advance" in err and "awaiting-operator" in err
    assert "Fix the board by hand" in err and "re-run" in err
    # the spec half landed and stays landed — that is what makes this recoverable
    assert frontmatter.status_of(frontmatter.read_frontmatter(sp)) == "done"
    assert devcontract.has_operator_confirmation(sp)
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "awaiting-operator"
    assert "1-1-a" in operatoractions.load(project.project)  # NOT dropped


# ------------------------------- confirm: finishing an interrupted confirmation
#
# The state a failed board write leaves: spec signed off and at `done`, board
# still parked, index entry still there. `drift()` calls that stale, so before
# this branch existed `confirm` refused the one state re-running it exists to
# clear — and nothing else would ever drop the entry.


def _interrupted_story(paths, key="1-1-a", *, board="awaiting-operator"):
    """Exactly what `confirm` leaves when `sprintstatus.advance` raises or returns
    unchanged: audit section appended, spec at done, board where the park left
    it, entry still indexed."""
    from bmad_loop import devcontract

    sp = _park_story(paths, key, spec_status="done", board=board)
    devcontract.append_operator_confirmation(sp, CONFIRM_ACTIONS, date="2026-07-28")
    return sp


def test_confirm_finishes_an_interrupted_confirmation(project, capsys, monkeypatch):
    """Ablation: delete the `resumable` branch in `cmd_confirm` and this fails —
    the refusal names "its spec now says status: done" and the entry is stranded
    with `validate` warning about it forever."""
    from bmad_loop import operatoractions, sprintstatus

    def _boom(*a, **k):
        raise AssertionError("a resume must not re-prompt")

    monkeypatch.setattr("builtins.input", _boom)
    monkeypatch.setattr(cli, "_confirm", _boom)
    install_bmad_config(project)
    _interrupted_story(project)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 0
    out = capsys.readouterr().out
    assert "was already confirmed" in out and "✓ 1-1-a confirmed" in out
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "done"
    assert operatoractions.load(project.project) == {}


def test_a_resume_does_not_append_a_second_audit_section(project, capsys, monkeypatch):
    """`append_operator_confirmation` accumulates rather than no-ops, because a
    genuinely repeated confirmation is a real event. A resume is not one — the
    section on disk IS the acknowledgment being finished.

    Ablation: route the resume through `_apply_confirmation` and this fails —
    the audit trail claims two sign-offs for one event."""
    from bmad_loop import devcontract

    install_bmad_config(project)
    sp = _interrupted_story(project)
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 0
    assert sp.read_text().count(devcontract.OPERATOR_CONFIRM_HEADING) == 1


def test_a_resume_finishes_a_board_a_human_already_fixed(project, capsys, monkeypatch):
    """⚠️ The half of the resume nothing else can do. Told to "fix the board by
    hand", a human does — and now spec and board are both `done` with an index
    entry pointing at neither. Advancing is idempotent there, so all that is left
    is dropping the entry, which is the only thing that stops `validate` nagging.

    Ablation: narrow `ParkedStory.resumable`'s board arm to `== AWAITING_OPERATOR`
    and this fails — rc 1, and the entry survives forever."""
    from bmad_loop import operatoractions, sprintstatus

    install_bmad_config(project)
    _interrupted_story(project, board="done")
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 0
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "done"
    assert operatoractions.load(project.project) == {}


def test_a_resume_commits_the_spec_the_interrupted_run_never_committed(
    project, capsys, monkeypatch
):
    """The commit is the LAST step, so an interruption before it leaves the
    signed-off spec uncommitted. The resume carries it into history with the
    board, exactly as an uninterrupted confirmation would."""
    install_bmad_config(project)
    _park_story(project)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "park")
    _interrupted_story(project)
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 0
    assert "confirm 1-1-a" in git(project.project, "log", "-1", "--format=%s")


def test_a_resume_still_honors_reverify_and_says_what_was_not_done(project, capsys, monkeypatch):
    """`--reverify` is a gate the caller requested for THIS invocation, not an
    acknowledgment, so it still runs. The message has to say "NOT advanced" — the
    confirmation itself already happened, and telling a human it was "NOT
    confirmed" sends them looking for a sign-off to redo."""
    from bmad_loop import operatoractions, sprintstatus

    install_bmad_config(project)
    _interrupted_story(project)
    _write_policy(project.project, '[verify]\ncommands = ["python -c \\"raise SystemExit(3)\\""]\n')
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a", "--reverify")) == 1
    err = capsys.readouterr().err
    assert "NOT advanced" in err and "NOT confirmed" not in err
    assert sprintstatus.story_status(project.sprint_status, "1-1-a") == "awaiting-operator"
    assert "1-1-a" in operatoractions.load(project.project)


def test_list_marks_an_interrupted_confirmation_as_signed_off_not_refused(project, capsys):
    """An interrupted confirmation drifts, so a listing that reads `drift()` alone
    labels it NOT CONFIRMABLE — telling a human confirm will refuse a story confirm
    will happily finish.

    Ablation: drop the `resumable` arm of `_print_parked` and this fails."""
    install_bmad_config(project)
    _interrupted_story(project)

    assert cli.main(_confirm_argv(project, "--list")) == 0
    out = capsys.readouterr().out
    assert "ALREADY SIGNED OFF" in out and "re-run confirm" in out
    assert "NOT CONFIRMABLE" not in out


def test_json_carries_resumable_and_the_reading_it_came_from(project, capsys):
    """The builder commits to shipping the raw inputs a derived boolean came from,
    so a consumer triaging a stuck board can see WHICH reading made it what it is.

    ⚠️ `schema_version` stays 1 deliberately: evolution is additive-only, and every
    field that already existed produces the same value for every input."""
    install_bmad_config(project)
    _interrupted_story(project)

    doc = machine_json(_confirm_argv(project, "--json"), capsys)
    assert doc["schema_version"] == 1
    (entry,) = doc["parked"]
    assert entry["resumable"] is True and entry["confirmation_recorded"] is True
    # unchanged for the same input — the half-applied state is still not a park
    assert entry["confirmable"] is False and entry["drift"] == "its spec now says status: done"


def test_json_says_a_plain_park_is_not_resumable(project, capsys):
    install_bmad_config(project)
    _park_story(project)

    (entry,) = machine_json(_confirm_argv(project, "--json"), capsys)["parked"]
    assert entry["resumable"] is False and entry["confirmation_recorded"] is False


def test_a_spec_at_done_without_a_sign_off_is_still_refused(project, capsys, monkeypatch):
    """The guard that keeps the resume from swallowing ordinary drift: a story
    re-driven to done, or finished by hand, carries no ``## Operator Confirmation``
    section. Confirming it would advance a board on nobody's word.

    Ablation: drop the `confirmation_recorded` arm of `resumable` and this fails —
    rc 0, and a story no human signed off is reported as confirmed."""
    from bmad_loop import operatoractions

    install_bmad_config(project)
    _park_story(project, spec_status="done")
    monkeypatch.setattr(cli, "_confirm", lambda _q: True)

    assert cli.main(_confirm_argv(project, "1-1-a")) == 1
    assert "its spec now says status: done" in capsys.readouterr().err
    assert "1-1-a" in operatoractions.load(project.project)


# ------------------------------------- validate: operator-index drift warnings


def _validate_findings(paths, capsys, rc=0):
    doc = machine_json(["validate", "--project", str(paths.project), "--json"], capsys, rc=rc)
    return {f["check"]: f for f in doc["findings"]}


def _render_snapshot(project):
    snapshot = (
        project.project
        / "_bmad"
        / "render"
        / "bmad-build-auto"
        / "sandbox-a1b2"
        / "generation-c3d4"
    )
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "workflow.md").write_text("rendered\n", encoding="utf-8")
    return snapshot


def test_validate_warns_when_renderer_output_is_already_tracked(project, monkeypatch, capsys):
    _make_validate_pass(project, monkeypatch, capsys)
    _render_snapshot(project)
    git(project.project, "add", "-f", "_bmad/render")
    git(project.project, "commit", "-q", "-m", "track renderer output")

    findings = _validate_findings(project, capsys)
    warning = findings["git.render-tracked"]
    assert warning["severity"] == "warning"
    assert warning["detail"] == {"path": "_bmad/render"}
    assert "git rm -r --cached _bmad/render" in warning["message"]


def test_validate_is_silent_for_renderer_output_only_present_on_disk(project, monkeypatch, capsys):
    """Clearing leg: filesystem presence is not index membership."""
    _make_validate_pass(project, monkeypatch, capsys)
    snapshot = _render_snapshot(project)

    findings = _validate_findings(project, capsys)
    assert (snapshot / "workflow.md").is_file()
    assert "git.render-tracked" not in findings


def test_validate_render_tracked_probe_degrades_without_a_fabricated_result(
    project, monkeypatch, capsys
):
    from bmad_loop import verify

    _make_validate_pass(project, monkeypatch, capsys)
    calls = 0

    def unavailable(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise verify.GitError("index disappeared")

    monkeypatch.setattr(verify, "path_tracked", unavailable)
    findings = _validate_findings(project, capsys)
    assert calls == 1
    assert "git.render-tracked" not in findings


def test_validate_warns_on_a_stale_index_entry(project, capsys):
    """The index is machine-local and never committed, so it CAN fall out of step
    with the specs and board that are the real record. That is only a safe trade
    if something says so out loud.

    Ablation: delete the `drift()` arm of `_validate_operator_registry` and this
    fails — the stale entry is reported by nothing."""
    install_bmad_config(project)
    _park_story(project, spec_status="done")

    findings = _validate_findings(project, capsys, rc=1)
    assert findings["operator.registry-stale"]["severity"] == "warning"
    assert "1-1-a" in findings["operator.registry-stale"]["message"]


def test_validate_reports_a_parked_board_with_no_record_under_its_own_id(project, capsys):
    """The opposite direction under its own id (#356). Before the committed
    records this was the fresh-clone NORM and shared `registry-stale`; now the
    record travels with the park's own commit, so a board at `awaiting-operator`
    that no record claims is always evidence of something — a failed record
    write, a pre-upgrade park, a checkout without the park's branch, a deleted
    record — and its remedy differs from a stale entry's discard-it, which is
    exactly where `checks` splits ids.

    Ablation: report the orphan arm under `operator.registry-stale` again and
    this fails on the id lookup."""
    install_bmad_config(project)
    write_sprint(project, {"epic-1": "in-progress", "1-1-a": "awaiting-operator"})

    findings = _validate_findings(project, capsys, rc=1)
    missing = findings["operator.park-record-missing"]
    assert missing["severity"] == "warning"
    assert "no park record" in missing["message"]
    assert missing["detail"]["story_keys"] == ["1-1-a"]
    assert "operator.registry-stale" not in findings


def test_validate_warns_on_a_park_declaring_nothing_readable(project, capsys):
    """Ablation: delete the `actions-malformed` arm and this fails — a parked
    story with an unreadable action list is reported as merely stale, or not at
    all."""
    install_bmad_config(project)
    _park_story(project, actions="a bare string is not a list")

    findings = _validate_findings(project, capsys, rc=1)
    malformed = findings["operator.actions-malformed"]
    assert malformed["severity"] == "warning"
    assert "declares nothing readable" in malformed["message"]


def test_validate_is_silent_when_the_index_agrees_with_the_board(project, capsys):
    install_bmad_config(project)
    _park_story(project)

    findings = _validate_findings(project, capsys, rc=1)
    assert "operator.registry-stale" not in findings
    assert "operator.actions-malformed" not in findings


def test_validate_reports_a_board_drift_alongside_a_malformed_action_list(project, capsys):
    """Two findings, not one. The malformed list is about the INDEX side and the
    board disagreement is about the COMMITTED side, and their remedies differ —
    repair the list versus discard the entry. The `continue` that used to follow
    the malformed warning dropped the second one entirely, because `drift()`
    checks the board BEFORE the empty-actions cause, so nothing ever reported it.

    Ablation: restore the `continue` and this fails. ⚠️ `_validate_findings` keys
    by check id and so cannot prove a COUNT — that is safe here only because the
    two ids differ; do not reuse it to assert two same-id findings."""
    install_bmad_config(project)
    _park_story(project, actions=[], board="done")

    findings = _validate_findings(project, capsys, rc=1)
    assert "declares nothing readable" in findings["operator.actions-malformed"]["message"]
    stale = findings["operator.registry-stale"]
    assert stale["detail"]["drift"] == "the board now says done"


def test_validate_does_not_call_a_malformed_park_stale_on_its_own(project, capsys):
    """The other half of dropping the `continue`: with the committed state still
    describing a park, the unreadable list is the ONLY finding. Reporting it twice
    — once as malformed, once as "stale because it declares no readable actions" —
    would send a human to two remedies for one fault.

    Ablation: call `drift()` instead of `committed_drift()` in the malformed arm
    and this fails."""
    install_bmad_config(project)
    _park_story(project, actions=[])

    findings = _validate_findings(project, capsys, rc=1)
    assert "operator.actions-malformed" in findings
    assert "operator.registry-stale" not in findings


def test_validate_reports_an_interrupted_confirmation_under_its_own_id(project, capsys):
    """A new id rather than `registry-stale`, because the remedy INVERTS: re-run
    confirm to finish it, not discard the entry. Telling a human "the entry is
    stale and confirm will refuse it" about a story confirm now completes is the
    same class of lie the half-applied state itself is.

    Ablation: delete the `resumable` arm and this fails — the finding comes back
    as `operator.registry-stale` saying confirm will refuse it."""
    install_bmad_config(project)
    _interrupted_story(project)

    findings = _validate_findings(project, capsys, rc=1)
    interrupted = findings["operator.confirm-interrupted"]
    assert interrupted["severity"] == "warning"
    assert "board was never advanced" in interrupted["message"]
    assert "confirm 1-1-a" in interrupted["message"]
    assert "operator.registry-stale" not in findings


def test_validate_operator_warnings_never_fail_the_run(project, capsys):
    """Warnings only, both directions: `confirm` already refuses a drifted entry,
    so a stale index must not be able to block a run that would otherwise start."""
    from bmad_loop.checks import ValidationReport

    report = ValidationReport()
    install_bmad_config(project)
    _park_story(project, spec_status="done")
    cli._validate_operator_registry(project.project, project, report)
    assert report.findings and report.passed


# ------------- the dev primitive's rename: bmad-dev-auto -> bmad-build-auto ---

# dev+review on claude (skill tree `.claude/skills`) with triage on a CLI whose
# profile reads a DIFFERENT tree (`.agents/skills`). This is the topology the
# 3-roles-vs-2 mismatch broke: nothing ever dispatches a bmm skill into triage's
# tree, so gating it refused runs over skills that tree will never need.
TRIAGE_SPLIT_POLICY = (
    '[adapter]\nname = "claude"\nmodel = "opus"\n[adapter.triage]\nname = "gemini"\n'
)

# Two CLIs across two trees, with the review STAGE switched off. The adapter is
# still resolved, still provisioned, and still hooked — see #424 below.
REVIEW_DISABLED_POLICY = DUAL_CLIENT_POLICY + "[review]\nenabled = false\n"


def test_require_base_skills_does_not_gate_a_triage_only_skill_tree(project):
    """A triage CLI on its own skill tree must not be able to refuse a run.

    ``_skill_trees`` iterates :data:`install.DEV_PRIMITIVE_ROLES` (dev, review), not
    :data:`runsetup.ROLES` (dev, review, triage). Every skill this preflight asks
    about — the dev primitive and the review layers its step-04 invokes inline — is
    one only a dev or review session dispatches, and ``WorktreeFlow`` only ever
    provisions those same two profiles. Gating all three meant
    ``[adapter.triage] name = "gemini"`` beside a claude dev/review pair demanded the
    whole bmm module in ``.agents/skills`` before the run could start: a hard FAIL
    over a tree no session dispatches one of these skills into, on a gate with no
    ``--force``. Triage's only prompt is ``/bmad-loop-sweep``, which ships in this
    wheel and is laid down by ``bmad-loop init``.

    Ablation: point ``_skill_trees`` back at ``ROLES`` and the ``is True`` below goes
    False — nothing installed ``.agents/skills/bmad-build-auto``, and nothing ever
    will."""
    from bmad_loop.install import DEV_PRIMITIVE_ROLES
    from bmad_loop.runsetup import ROLES

    _write_policy(project.project, TRIAGE_SPLIT_POLICY)
    install_build_auto_skill(project.project, ".claude/skills")  # claude's tree ONLY
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")

    assert cli._require_base_skills(project.project, pol) is True
    # ...because triage's tree was never asked about. Asserting the whole list, not
    # `".agents/skills" not in`: the point is which trees ARE gated, and an empty
    # list would satisfy the negative form while gating nothing at all.
    assert cli._skill_trees(project.project, pol) == [".claude/skills"]

    # The structural half, so a `triage` that creeps back into the constant fails
    # here by name rather than only through a filesystem shape. The two sets are one
    # decision — `WorktreeFlow.worktree_profiles` reads the same constant — so the
    # dev-primitive set being a STRICT subset is what keeps "gated" and "provisioned"
    # from drifting apart in either direction.
    assert DEV_PRIMITIVE_ROLES == ("dev", "review")
    assert set(DEV_PRIMITIVE_ROLES) < set(ROLES)


def test_skill_trees_covers_review_even_when_review_is_disabled(project):
    """``review.enabled = false`` must NOT narrow the gated trees (#424).

    This test exists to make a future narrowing FAIL. The obvious follow-on to the
    role-scoping above is "and drop review's tree when the review stage is off" — it
    is wrong, and it is wrong quietly. Disabling the review stage does not retire the
    review ADAPTER: a plugin workflow can declare ``role = "review"`` and dispatch on
    ``adapters["review"]`` with review disabled, and the same profile list drives
    per-CLI Stop-signal hook registration as well as worktree seeding. A worktree
    provisioned without the review profile has no completion signal for those
    sessions, so narrowing here would convert a clean, actionable preflight refusal
    into a silent stall that burns the run's whole budget."""
    _write_policy(project.project, REVIEW_DISABLED_POLICY)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    assert pol.review.enabled is False, "the fixture only bites with the stage off"

    trees = cli._skill_trees(project.project, pol)
    assert trees == [".claude/skills", ".agents/skills"]  # dev=claude, review=codex


def test_gated_trees_and_provisioned_trees_stay_one_decision(project):
    """Constraint 1, pinned from BOTH sides rather than asserted in a comment.

    ``cli._skill_trees`` decides which trees can REFUSE a run;
    ``WorktreeFlow.worktree_profiles`` decides which profiles get carried INTO a
    worktree. The only thing making those one decision is that both iterate
    :data:`install.DEV_PRIMITIVE_ROLES` — nothing enforced it, so a one-sided edit
    used to be invisible here. Either direction ships a silent bug: gated but not
    provisioned drops the session into the `Unknown command` stall the preflight
    exists to catch, provisioned but not gated refuses runs over a skill nothing
    reads.

    The triage split is what makes the two sets separable at all — with one CLI
    everywhere, both readings agree by construction and the test proves nothing.

    ``worktree_profiles`` is called unbound on a stub carrying only
    ``_adapters_get``, which is the entirety of the ``self`` it touches: building a
    whole `WorktreeFlow` here would drag in the engine callbacks without making the
    assertion any stronger. The adapters dict carries ALL of `runsetup.ROLES`, so a
    one-sided revert to `ROLES` fails on the set comparison below rather than
    escaping as a `KeyError` on a missing triage key."""
    from types import SimpleNamespace

    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.runsetup import ROLES
    from bmad_loop.worktree_flow import WorktreeFlow

    _write_policy(project.project, TRIAGE_SPLIT_POLICY)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    adapters = {
        role: SimpleNamespace(profile=get_profile(pol.adapter.resolved(role).name, project.project))
        for role in ROLES
    }

    gated = cli._skill_trees(project.project, pol)
    provisioned = WorktreeFlow.worktree_profiles(SimpleNamespace(_adapters_get=lambda: adapters))

    # Pin the value, not just the agreement: two empty sets would satisfy the
    # equality below while gating and provisioning nothing at all.
    assert gated == [".claude/skills"]
    assert {p.skill_tree for p in provisioned} == set(gated)


@pytest.mark.parametrize(
    ("installer", "primitive"),
    [
        (lambda root: install_build_auto_skill(root), "bmad-build-auto"),
        (lambda root: install_dev_base_skills(root, folder_id=True), "bmad-dev-auto"),
    ],
    ids=["renamed", "legacy"],
)
def test_validate_names_the_dev_primitive_that_actually_resolved(
    project, capsys, monkeypatch, installer, primitive
):
    """The ``skills.base`` ok line names what is on disk, not a hardcoded era.

    It used to read "upstream skills present (bmad-dev-auto + review layers)"
    unconditionally, which on an upgraded project is a green line asserting the
    rename did not happen. On a rename this line is the operator's only confirmation
    that the new name was picked up, so it is asserted whole rather than by
    substring, and the machine-readable ``dev_primitive`` beside it is what a
    consumer reads instead of parsing the sentence back apart."""
    _make_validate_pass(project, monkeypatch, capsys, skills=installer)

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)
    base = next(f for f in doc["findings"] if f["check"] == "skills.base")
    assert base["message"] == f"upstream skills present ({primitive} + review layers)"
    assert base["detail"]["dev_primitive"] == [primitive]
    assert base["detail"]["trees"] == [".claude/skills"]


def test_validate_reports_a_different_primitive_era_in_each_tree(project, capsys, monkeypatch):
    """Why ``dev_primitive`` is a list and the ok line joins it.

    A project can sit mid-upgrade: ``.claude/skills`` (claude, dev) already carries
    ``bmad-build-auto`` while ``.agents/skills`` (codex, review) still carries a
    complete pre-rename ``bmad-dev-auto``. Both are drivable, and each tree is driven
    under its OWN name — the resolution is per tree, not per project. A scalar field
    would have to pick a winner and would then tell the operator the rename landed
    everywhere when it landed in one tree."""

    def _mixed_eras(root):
        install_build_auto_skill(root, ".claude/skills")
        install_dev_base_skills(root, ".agents/skills", folder_id=True)

    _make_validate_pass(project, monkeypatch, capsys, policy=DUAL_CLIENT_POLICY, skills=_mixed_eras)

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)
    base = next(f for f in doc["findings"] if f["check"] == "skills.base")
    assert base["detail"]["trees"] == [".claude/skills", ".agents/skills"]
    assert base["detail"]["dev_primitive"] == ["bmad-build-auto", "bmad-dev-auto"]
    assert base["message"] == (
        "upstream skills present (bmad-build-auto + bmad-dev-auto + review layers)"
    )


def test_validate_reports_each_renderer_problem_and_the_complete_surface_clears(
    project, capsys, monkeypatch
):
    """Validate consumes the canonical missing_base_skills findings, with no side gate."""
    from conftest import RENDERER_WORKFLOW_MD

    from bmad_loop.install import (
        CENTRAL_CONFIG_REL,
        DEV_PRIMITIVE_NEW,
        RENDERER_ENTRY_REL,
        RENDERER_SCRIPT_REL,
    )

    def broken_renderer(root):
        skills = install_build_auto_skill(root, renderer_stub=True)
        (skills / DEV_PRIMITIVE_NEW / RENDERER_ENTRY_REL).unlink()

    _make_validate_pass(project, monkeypatch, capsys, skills=broken_renderer)

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)
    found = {f["check"]: f for f in doc["findings"]}
    assert doc["ok"] is False
    assert found["skills.dev-renderer"]["severity"] == "problem"
    assert found["skills.dev-renderer"]["detail"]["missing_scripts"] == [RENDERER_SCRIPT_REL]
    assert found["skills.dev-renderer-sources"]["detail"]["missing_sources"] == [RENDERER_ENTRY_REL]
    assert found["skills.dev-renderer-config"]["detail"] == {"config": CENTRAL_CONFIG_REL}
    rendered = _render_findings(doc)
    for check in (
        "skills.dev-renderer",
        "skills.dev-renderer-sources",
        "skills.dev-renderer-config",
    ):
        assert check in rendered

    # The far side: restore each precise prerequisite and every renderer finding
    # disappears while the rest of validate stays green.
    primitive = project.project / ".claude/skills" / DEV_PRIMITIVE_NEW
    (primitive / RENDERER_ENTRY_REL).write_text(RENDERER_WORKFLOW_MD, encoding="utf-8")
    renderer = project.project / RENDERER_SCRIPT_REL
    renderer.parent.mkdir(parents=True, exist_ok=True)
    renderer.write_text("# self-contained renderer\n", encoding="utf-8")
    central = project.project / CENTRAL_CONFIG_REL
    central.parent.mkdir(parents=True, exist_ok=True)
    central.write_text("[core]\nname = 'fixture'\n", encoding="utf-8")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "complete renderer surface")

    cleared = _validate_findings(project, capsys)
    assert not {check for check in cleared if check.startswith("skills.dev-renderer")}


def test_a_forwarding_shim_install_fails_validate_and_aborts_the_run(project, capsys, monkeypatch):
    """The shim upstream's rename left behind is REFUSED, not driven.

    Post-rename ``bmad-dev-auto`` is a lone SKILL.md whose customization-migration
    gate is interactive. An unattended session dispatched into it HALTs having
    written nothing to disk — no spec, no result artifact, nothing the post-session
    verification can read — so the story stalls rather than fails, and the run burns
    its budget on it. Both entry points must refuse it before any session is spawned,
    and the remediation has to name the rename: without that, an operator is being
    told a skill that is visibly PRESENT is missing."""
    install_bmad_config(project)
    _write_policy(project.project, CLAUDE_ONLY_POLICY)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    install_dev_shim(project.project)  # lays the review stub too, so this is the only finding
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "shim fixture")

    findings = _validate_findings(project, capsys, rc=1)
    shim = findings["skills.base-shim"]
    assert shim["severity"] == "problem"
    assert shim["detail"] == {
        "tree": ".claude/skills",
        "skill": "bmad-dev-auto",
        "expected": "bmad-build-auto",
        "missing_markers": ["step-04-review.md", "customize.toml"],
    }
    assert "skills.base" not in findings  # no green line riding beside the refusal

    # ...and the same install aborts `run` through _require_base_skills, before the
    # engine is reached at all.
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})

    assert cli.main(["run", "--project", str(project.project)]) == 1
    err = capsys.readouterr().err
    assert "forwarding shim the BMad Method's rename left behind" in err
    assert "bmad-build-auto is not installed" in err


def _install_renderer_stub_project(
    project, monkeypatch, *, script: bool, utils: bool, config: bool
):
    """Committed run sandbox with one renderer-backed dev primitive.

    Each Boolean controls one project-global prerequisite; the skill-relative
    workflow graph comes from ``install_build_auto_skill(renderer_stub=True)``.
    The installed script uses upstream's module-scope helper import so ``utils``
    exercises the real content discriminator.
    """
    from conftest import RENDERER_SCRIPT_IMPORTING_SIBLING

    from bmad_loop.install import (
        CENTRAL_CONFIG_REL,
        DEV_PRIMITIVE_NEW,
        RENDERER_CONFIG_UTILS_REL,
        RENDERER_SCRIPT_REL,
    )

    install_bmad_config(project)
    _write_policy(project.project, CLAUDE_ONLY_POLICY)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    skills = install_build_auto_skill(project.project, renderer_stub=True)
    if script:
        renderer = project.project / RENDERER_SCRIPT_REL
        renderer.parent.mkdir(parents=True, exist_ok=True)
        renderer.write_text(RENDERER_SCRIPT_IMPORTING_SIBLING, encoding="utf-8")
    if utils:
        helper = project.project / RENDERER_CONFIG_UTILS_REL
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("# helper\n", encoding="utf-8")
    if config:
        central = project.project / CENTRAL_CONFIG_REL
        central.parent.mkdir(parents=True, exist_ok=True)
        central.write_text("[core]\nname = 'sandbox'\n", encoding="utf-8")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "renderer sandbox")
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})
    return skills / DEV_PRIMITIVE_NEW


@pytest.mark.parametrize(
    ("script", "utils", "config", "missing"),
    [
        (False, False, True, "_bmad/scripts/render_skill.py"),
        (True, False, True, "_bmad/scripts/config_utils.py"),
        (True, True, False, "_bmad/config.toml"),
    ],
    ids=["script", "helper", "central-config"],
)
def test_run_aborts_before_engine_on_missing_renderer_surface(
    project, monkeypatch, capsys, script, utils, config, missing
):
    """Full CLI sandbox: each deterministic renderer HALT refuses the run."""
    _install_renderer_stub_project(project, monkeypatch, script=script, utils=utils, config=config)

    assert cli.main(["run", "--project", str(project.project)]) == 1
    err = capsys.readouterr().err
    assert missing in err
    assert "HALT" in err


def test_run_aborts_when_the_renderer_entry_source_is_missing(project, monkeypatch, capsys):
    from bmad_loop.install import RENDERER_ENTRY_REL

    skill = _install_renderer_stub_project(
        project, monkeypatch, script=True, utils=True, config=True
    )
    (skill / RENDERER_ENTRY_REL).unlink()
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "truncate renderer sources")

    assert cli.main(["run", "--project", str(project.project)]) == 1
    err = capsys.readouterr().err
    assert RENDERER_ENTRY_REL in err and "HALT" in err


def test_run_proceeds_once_the_complete_renderer_surface_is_present(project, monkeypatch, capsys):
    """Clearing leg: a healthy renderer stub is allowed to reach the engine."""
    _install_renderer_stub_project(project, monkeypatch, script=True, utils=True, config=True)

    assert cli.main(["run", "--project", str(project.project)]) == 0
    assert "render_skill.py" not in capsys.readouterr().err


def test_dry_run_warns_that_a_missing_renderer_makes_the_preview_unrunnable(
    project, monkeypatch, capsys
):
    _install_renderer_stub_project(project, monkeypatch, script=False, utils=False, config=True)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    args = argparse.Namespace(epic=None, story=None, max_stories=None)
    capsys.readouterr()

    assert cli._dry_run(project, pol, args) == 0
    out, err = capsys.readouterr()
    assert "NOT runnable" in err and "_bmad/scripts/render_skill.py" in err
    assert "1-1-a" in out


def test_require_base_skills_ignores_a_broken_renderer_in_a_triage_only_tree(project, capsys):
    """Renderer problems obey the same dev+review role scope as every base check."""
    _write_policy(project.project, TRIAGE_SPLIT_POLICY)
    install_build_auto_skill(project.project, ".claude/skills")
    install_build_auto_skill(project.project, ".agents/skills", renderer_stub=True)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")

    assert cli._require_base_skills(project.project, pol) is True
    assert "render_skill.py" not in capsys.readouterr().err

    # Move the identical broken shape onto the dev tree: the problem now gates.
    install_build_auto_skill(project.project, ".claude/skills", renderer_stub=True)
    assert cli._require_base_skills(project.project, pol) is False
    assert "render_skill.py" in capsys.readouterr().err


def test_validate_reports_an_orphaned_legacy_override_as_a_warning(project, capsys, monkeypatch):
    """The rename orphans ``_bmad/custom/bmad-dev-auto.toml`` — and that is a WARNING.

    Upstream's resolver keys on the skill directory, so on a renamed project the
    legacy override is simply never read: the session still runs, it just runs
    unstyled. The severity is the whole point. ``missing_base_skills`` feeds
    ``_require_base_skills``, which has no severity filter and no ``--force`` — a
    problem here would pause every run behind a remediation nobody can apply, so on a
    survivable condition a false green is the safe direction and the honest response
    is to name the rename rather than block."""
    custom = project.project / "_bmad" / "custom"

    def _renamed_with_an_orphan(root):
        install_build_auto_skill(root)
        custom.mkdir(parents=True, exist_ok=True)
        (custom / "bmad-dev-auto.toml").write_text("# stale override\n", encoding="utf-8")

    _make_validate_pass(project, monkeypatch, capsys, skills=_renamed_with_an_orphan)

    findings = _validate_findings(project, capsys)  # rc 0: the warning never gates
    orphan = findings["skills.customize-legacy"]
    assert orphan["severity"] == "warning"
    assert orphan["detail"]["files"] == ["_bmad/custom/bmad-dev-auto.toml"]
    assert findings["skills.base"]["detail"]["dev_primitive"] == ["bmad-build-auto"]

    # ...and it is the ORPHANING that is reported, not the legacy file's existence:
    # land the counterpart and the finding goes.
    (custom / "bmad-build-auto.toml").write_text("# migrated\n", encoding="utf-8")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "migrate the override")

    assert "skills.customize-legacy" not in _validate_findings(project, capsys)


def test_validate_does_not_gate_a_triage_only_skill_tree(project, capsys, monkeypatch):
    """validate's verdict and run's abort are now one decision, on the same trees.

    ``cmd_validate`` builds its trees through the same ``_skill_trees`` the real
    preflight calls, so a triage CLI on its own tree can no more fail validate than
    it can abort a run. Before that, ``cmd_validate`` passed
    ``[p.skill_tree for p in profiles]`` — every loaded profile, triage included —
    and reported ``.agents/skills/bmad-build-auto not found`` as a hard problem about
    a tree no session dispatches into."""
    _make_validate_pass(
        project,
        monkeypatch,
        capsys,
        policy=TRIAGE_SPLIT_POLICY,
        skills=lambda root: install_build_auto_skill(root, ".claude/skills"),
    )

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys)
    assert doc["ok"] is True and doc["counts"]["problem"] == 0
    base = next(f for f in doc["findings"] if f["check"] == "skills.base")
    assert base["detail"]["trees"] == [".claude/skills"]  # the member, not a prefix
    # and nothing under skills.* so much as mentions triage's tree — keyed on the
    # detail's own `tree` field, because a past bug here was a substring assertion
    # that three unrelated review findings happened to satisfy.
    assert not [
        f
        for f in doc["findings"]
        if f["check"].startswith("skills.") and (f["detail"] or {}).get("tree") == ".agents/skills"
    ]


# ---- #414: worktree isolation is refused under a repo_root override ----------

ISOLATION_WORKTREE_POLICY = (
    '[adapter]\nname = "claude"\nmodel = "opus"\n\n[scm]\nisolation = "worktree"\n'
)
NO_ISOLATION_POLICY = '[adapter]\nname = "claude"\nmodel = "opus"\n\n[scm]\nisolation = "none"\n'
REFUSAL = 'isolation = "worktree" is not supported'


def _override_repo_root(paths, rel="git-root"):
    """Point `repo_root` away from the project — #414's monorepo layout, minus the
    monorepo. The target has no `_bmad/`, which is the shape the issue reports, but
    it DOES exist: `cmd_run`/`cmd_sweep` probe `verify.worktree_clean(repo_root)`,
    and against a missing dir that raises `GitError` instead of answering, which
    would make every "the isolation gate spoke first" assertion below unfalsifiable
    — the later gate would crash rather than print the message it is asserted not to
    print."""
    (paths.project / rel).mkdir(exist_ok=True)
    cfg = paths.project / "_bmad" / "bmm" / "config.yaml"
    cfg.write_text(cfg.read_text() + f"repo_root: '{{project-root}}/{rel}'\n", encoding="utf-8")


def _render_findings(doc) -> str:
    """Draw a document through the TUI renderer (#210) and return the text. The
    `{'` assertion is that file's nested-dict tell: a renderer that str()s a detail
    shape it did not model prints a Python repr."""
    from rich.console import Console

    from bmad_loop.tui import widgets

    console = Console(width=96)
    with console.capture() as capture:
        console.print(widgets.validate_findings(doc, details=True))
    rendered = capture.get()
    assert "{'" not in rendered
    return rendered


def test_validate_refuses_worktree_isolation_under_a_repo_root_override(
    project, monkeypatch, capsys
):
    """#414: provisioning seeds every non-git surface from `repo_root` while every
    gate validate runs probes `project`, so a split pair makes validate approve a
    surface the isolated run never receives. A `problem`, so the rc flips — and the
    fixture is committed first, so the rc-1 is this gate and not a dirty tree."""
    _make_validate_pass(project, monkeypatch, capsys, policy=ISOLATION_WORKTREE_POLICY)
    _override_repo_root(project)
    git(project.project, "commit", "-qam", "repo_root override")

    doc = machine_json(["validate", "--project", str(project.project), "--json"], capsys, rc=1)
    finding = {f["check"]: f for f in doc["findings"]}["policy.isolation-repo-root"]
    assert finding["severity"] == "problem"
    # Both remediations named, and only remediations that exist on this line: fix-1
    # (plumbing `project` through provisioning) is a main-line option, so the message
    # must not gesture at a flag or a version that would make the pair work.
    assert "Remove the `repo_root` key" in finding["message"]
    assert '`isolation = "none"`' in finding["message"]
    assert finding["detail"] == {
        "repo_root": str(project.project / "git-root"),
        "project": str(project.project),
    }
    _render_findings(doc)  # the detail shape draws in the TUI modal


@pytest.mark.parametrize(
    "override,policy_text",
    [(True, NO_ISOLATION_POLICY), (False, ISOLATION_WORKTREE_POLICY)],
    ids=["override-without-worktree", "worktree-without-override"],
)
def test_validate_isolation_gate_needs_both_halves(
    project, monkeypatch, capsys, override, policy_text
):
    """Either half alone is a configuration this release supports and documents: a
    `repo_root` override under `isolation = "none"` is the monorepo knob README's
    "Worktree isolation" section documents, and
    worktree isolation without an override is the ordinary isolated setup. The
    gate stays silent on both, and validate still passes — the green rc is the second
    witness, since a gate that fired would take the whole verdict with it."""
    from bmad_loop import bmadconfig

    _make_validate_pass(project, monkeypatch, capsys, policy=policy_text)
    if override:
        _override_repo_root(project)
        git(project.project, "commit", "-qam", "repo_root override")

    # The fixture can express the failing value: each leg really does carry exactly
    # one half of it, so neither silence is the silence of a setup that never landed.
    loaded = bmadconfig.load_paths(project.project)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    assert (loaded.repo_root != loaded.project) is override
    assert (pol.scm.isolation == "worktree") is not override

    assert "policy.isolation-repo-root" not in _validate_findings(project, capsys)


def _split_root_project(project, *, policy_text=ISOLATION_WORKTREE_POLICY):
    install_bmad_config(project)
    _override_repo_root(project)
    _write_policy(project.project, policy_text)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    # `worktree_clean` scopes `git status` to `-- .` inside the dir it is handed, so
    # only dirt UNDER repo_root can make the next gate speak. Without this the
    # ordering assertion below passes no matter where the gate sits.
    (project.project / "git-root" / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")


@pytest.mark.parametrize("command", ["run", "sweep"])
def test_start_refuses_worktree_isolation_under_a_repo_root_override(
    project, monkeypatch, capsys, command
):
    """The refusal validate reports is also the one the real command makes, and it is
    the FIRST one: `repo_root` is left genuinely dirty, so a gate ordered after
    `worktree_clean` would answer "commit or stash first" instead — a message that
    sends the operator to fix something that is not the problem. Both halves of that
    are load-bearing: the probe reads `repo_root`, not `project`, so dirtying the
    project would prove nothing."""
    _split_root_project(project)
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})

    assert cli.main([command, "--project", str(project.project)]) == 1
    err = capsys.readouterr().err
    assert REFUSAL in err
    assert "not clean" not in err


def test_resume_refuses_worktree_isolation_under_a_repo_root_override(project, monkeypatch, capsys):
    """Resume re-reads config.yaml and policy.toml off disk, so it is a second
    entrypoint into the same provisioning: a run whose config grew the override
    mid-flight must not finish its remaining stories through worktrees the preflight
    would now refuse. Refused before the `run-resume` entry, so the journal does not
    record a resume that never happened."""
    run_dir = _paused_run_for_resume(project, monkeypatch)
    _write_policy(project.project, RESUME_POLICY + '\n[scm]\nisolation = "worktree"\n')
    _override_repo_root(project)
    monkeypatch.setattr(cli, "Engine", lambda **kw: pytest.fail("engine constructed"))

    assert cli._resume_paused_run(project.project, run_dir) == 1
    assert REFUSAL in capsys.readouterr().err
    assert _resume_entries(run_dir) == []


def test_auto_sweep_refuses_worktree_isolation_under_a_repo_root_override(project, monkeypatch):
    """The child sweep an engine auto-triggers is the one caller that reloads
    policy.toml while reusing the parent's already-loaded paths — so it is the only
    way a mid-run flip to `isolation = "worktree"` reaches provisioning under a split
    the parent's own start was allowed not to check.

    It RAISES rather than returning, which is the whole point: `_maybe_auto_sweep`
    has already journaled `sweep-auto-trigger` and latched the trigger by the time it
    calls the factory, and it reads a plain return as success — so a quiet decline is
    recorded as `sweep-auto-finished`, a child sweep that ran and finished when none
    was launched. Raising lands on the `sweep-auto-failed` + notify path instead,
    which is the same one an unparseable policy.toml already takes, and the parent
    run is still unaffected (`_maybe_auto_sweep` swallows it)."""
    from bmad_loop import bmadconfig

    _split_root_project(project)
    started = []
    monkeypatch.setattr(cli, "_start_sweep", lambda *a, **kw: started.append(kw) or 0)

    factory = cli._sweep_factory(project.project, bmadconfig.load_paths(project.project))
    with pytest.raises(RuntimeError, match=REFUSAL):
        factory("epic-boundary")
    assert started == []


def test_dry_run_banner_names_the_isolation_refusal_first(project, capsys):
    """The preview keeps rc 0 and still renders the schedule, but the banner has to
    name every refusal the dry-run's early return skips past — and in the order the
    real command makes them. (Not every refusal there is: the dirty-tree, queue and
    run-id gates are not part of this banner.) This
    project is short of base skills too, so the ordering is observable: the isolation
    refusal aborts before `_require_base_skills`, so it heads the list."""
    import dataclasses

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    _write_policy(project.project, ISOLATION_WORKTREE_POLICY)
    pol = policy_mod.load(project.project / ".bmad-loop" / "policy.toml")
    paths = dataclasses.replace(project, repo_root=project.project / "git-root")
    args = argparse.Namespace(epic=None, story=None, max_stories=None)

    assert cli._dry_run(paths, pol, args) == 0
    out, err = capsys.readouterr()
    fails = [line for line in err.splitlines() if line.startswith("  FAIL:")]
    assert REFUSAL in fails[0]
    assert len(fails) > 1, "the base-skill problems the banner already reported"
    assert "1-1-a" in out  # the schedule itself still rendered
