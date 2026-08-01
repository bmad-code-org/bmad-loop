"""Diagnostic-dump tests — the load-bearing one is the canary no-leak check.

A synthetic run dir is seeded with labelled secrets/PII/code in every sink the
dump could possibly read; the rendered report (markdown + JSON) must contain
none of them, while still preserving the diagnostic *structure*.
"""

from __future__ import annotations

import json
import re

import pytest

from bmad_loop import diagnostics, sanitize
from bmad_loop.journal import Journal, save_state
from bmad_loop.model import Phase, RunState, SessionRecord, StoryTask, TokenUsage

# Labelled canaries planted across the run dir. NONE may appear in the dump.
EMAIL = "victim.canary@example.com"
STORY_KEY = "1.2-AcmeQuantumBillingEngine"
PROPRIETARY = "AcmeQuantumBillingEngine"
BRANCH = "feature/AcmeSecret"
SECRET_GH = "ghp_CANARYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx01"
SECRET_OPENAI = "sk-CANARYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx99"
SECRET_AWS = "AKIACANARY0123456789"
HOME_PATH = "/home/canaryuser/secret/proj"
CODE = "def steal_creds(token): return token"
SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
# Deliberately does NOT embed STORY_KEY: the harvest events journal a bare
# `spec_path.name`, and a spec is free to be named after the feature rather than
# the story. Such a name is identifier-shaped, so the scrub_json fallback emits
# it verbatim, and the egress backstop cannot repair what was never aliased —
# only per-field routing can. A name like `1.2-Acme….md` would be rescued by the
# story key inside it and would prove nothing.
SPEC_NAME = "AcmeVaultRotation.md"

CANARIES = [
    EMAIL,
    PROPRIETARY,
    "AcmeSecret",
    SECRET_GH,
    SECRET_OPENAI,
    SECRET_AWS,
    HOME_PATH,
    "/home/",
    CODE,
    "steal_creds",
    "CANARY_REASON",
    "CANARY_PROMPT",
    "CANARY_ESCALATION",
    "CANARY_LOG",
    "CANARY_TASKPROMPT",
    "CANARY_RESULT",
    "CANARY_FEEDBACK",
    "CANARY_PATCH",
    SHA,
    "AcmeVaultRotation",
]


def _seed_run(root, run_id="20260627-120000-aaaa", *, extra_journal=None, sweeps_triggered=()):
    """Build a run dir loaded with canaries in every readable sink.

    ``sweeps_triggered`` seeds a routing gap the MARKDOWN report can reach: the
    collector passes identifier-shaped entries through verbatim, and the report
    renders them inline. (``extra_journal`` seeds a gap only the JSON document
    reaches — markdown renders journal aggregates, never per-entry fields.)
    """
    run_dir = root / ".bmad-loop" / "runs" / run_id

    task = StoryTask(
        story_key=STORY_KEY,
        epic=1,
        phase=Phase.ESCALATED,
        attempt=2,
        review_cycle=1,
        branch=BRANCH,
        baseline_commit=SHA,
        commit_sha=SHA,
        defer_reason="CANARY_REASON proprietary detail",
        spec_file=f"{HOME_PATH}/{STORY_KEY}.md",
        baseline_untracked=["AcmeSecret.py", "src/secret/thing.py"],
        worktree_path=f"{HOME_PATH}/worktrees/{BRANCH}",
        dw_ids=["DW-1", "DW-2"],
    )
    task.record_session(
        SessionRecord(
            task_id=STORY_KEY,
            role="dev",
            status="completed",
            session_id="01234567-89ab-cdef-0123-456789abcdef",
            transcript_path=f"{HOME_PATH}/.claude/x.jsonl",
            usage=TokenUsage(input_tokens=100, output_tokens=50, cache_read_tokens=10),
        )
    )
    task.record_session(SessionRecord(task_id=STORY_KEY, role="review", status="stalled"))

    state = RunState(
        run_id=run_id,
        project=f"{HOME_PATH}",
        started_at="2026-06-27T12:00:00",
        run_type="story",
        target_branch=BRANCH,
        current_epic=1,
        paused_reason="CANARY_REASON proprietary detail",
        paused_stage="escalation",
        paused_story_key=STORY_KEY,
        policy_snapshot={
            "adapter": {
                "name": "claude",
                "model": "claude-opus-4-8",
                "extra_args": ["--api-key", SECRET_OPENAI],
                "env": {"OPENAI_API_KEY": SECRET_OPENAI},
            },
            "scm": {"commit_message_template": "Implements {story_key} for AcmeCorp"},
            "plugins": {
                "enabled": ["unity"],
                "settings": {"unity": {"token": SECRET_GH, "unity_path": HOME_PATH}},
            },
        },
        plugin_shared={"unity": {"creds": SECRET_AWS}},
        tasks={STORY_KEY: task},
        sweeps_triggered=list(sweeps_triggered),
    )
    save_state(run_dir, state)

    j = Journal(run_dir)
    j.set_active_log(STORY_KEY)
    j.append("run-start", run_type="story")
    j.append("session-start", story_key=STORY_KEY, role="dev", prompt="CANARY_PROMPT secret code")
    j.append(
        "story-escalated",
        story_key=STORY_KEY,
        reason=f"CANARY_ESCALATION contact {EMAIL}",
    )
    j.append("story-done", story_key=STORY_KEY, commit=SHA)
    j.append("sprint-status-unknown-keys", keys=[STORY_KEY, "9.9-OtherSecret"])
    j.append("spec-deferrals-harvested", story_key=STORY_KEY, spec=SPEC_NAME)
    for kind, fields in extra_journal or []:
        j.append(kind, **fields)

    # Danger files: contents must never reach the dump.
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{STORY_KEY}.log").write_text(f"CANARY_LOG {CODE}\n{EMAIL}\n")
    tasks = run_dir / "tasks" / STORY_KEY
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "prompt.txt").write_text("CANARY_TASKPROMPT confidential spec")
    (tasks / "result.json").write_text(json.dumps({"notes": "CANARY_RESULT", "secret": SECRET_GH}))
    feedback = run_dir / "feedback"
    feedback.mkdir(parents=True, exist_ok=True)
    (feedback / f"{STORY_KEY}-1.md").write_text("CANARY_FEEDBACK review prose about the code")
    failed = run_dir / "failed" / STORY_KEY
    failed.mkdir(parents=True, exist_ok=True)
    (failed / "changes.patch").write_text(f"CANARY_PATCH\n+{CODE}\n")
    return run_dir


def _render_all(run_dirs):
    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect(run_dirs, pseudo=pseudo)
    md = diagnostics.render_markdown(diag, pseudo=pseudo)
    js = diagnostics.render_json(diag, pseudo=pseudo)
    return diag, pseudo, md + "\n" + js


# ----------------------------------------------------------- the no-leak test


def test_no_canary_leaks_anywhere(project):
    run_dir = _seed_run(project.project)
    _diag, _pseudo, combined = _render_all([run_dir])
    for canary in CANARIES:
        assert canary not in combined, f"LEAK: {canary!r} appeared in the dump"


def test_known_safe_values_survive(project):
    """The scrubber isn't trivially passing by redacting everything."""
    run_dir = _seed_run(project.project)
    _diag, _pseudo, combined = _render_all([run_dir])
    assert "claude-opus-4-8" in combined  # model id is safe
    assert "20260627-120000-aaaa" in combined  # run id is opaque/safe
    assert "escalated" in combined  # phase enum survives
    assert "input_tokens" in combined  # token count keys survive


def test_pseudonymization_is_stable_and_correlates(project):
    run_dir = _seed_run(project.project)
    diag, _pseudo, combined = _render_all([run_dir])
    (run,) = diag.runs
    alias = run.tasks[0].alias
    assert re.fullmatch(r"s1-[0-9a-f]{12}", alias), alias
    # the same alias appears in the per-task journal event counts (correlation)
    assert alias in run.journal.per_alias_event_counts
    assert alias in combined


def test_spec_name_is_aliased_in_its_own_namespace(project):
    """The `spec-*` journal kinds carry a bare spec basename, which is the
    customer's feature name. `test_no_canary_leaks_anywhere` already proves it
    does not ship; this pins HOW — a per-field alias in a `spec` namespace of its
    own, so the value stays correlatable across events and never renders in the
    epic-less `story-<hex>` shape a reused "story" namespace would give a
    filename. Nothing else can cover it: the value is not in the legend until it
    is routed, so the egress backstop has no alias to substitute."""
    run_dir = _seed_run(project.project)
    diag, pseudo, combined = _render_all([run_dir])
    alias = next(a for _ns, orig, a in pseudo.entries() if orig == SPEC_NAME)
    assert re.fullmatch(r"spec-[0-9a-f]{12}", alias), alias
    assert alias in combined
    # distinct from the story alias, and not wearing its shape
    assert alias != diag.runs[0].tasks[0].alias


def test_structure_is_preserved(project):
    run_dir = _seed_run(project.project)
    diag, _pseudo, _combined = _render_all([run_dir])
    (run,) = diag.runs
    assert run.n_tasks == 1
    assert run.journal.kind_histogram["story-escalated"] == 1
    assert run.journal.escalation_count == 1
    assert run.phase_histogram["escalated"] == 1
    assert run.session_tally.by_status == {"completed": 1, "stalled": 1}
    # token totals equal the one session's usage (the other session has none)
    assert run.token_totals["input_tokens"] == 100
    assert run.token_totals["total"] == 160
    # both units, so a bundle reader isn't left recomputing the weighted figure
    # the budgets actually judged (#129): 100 + 50 + round(10 * 0.1)
    assert run.token_totals["weighted"] == 151
    assert run.tasks[0].tokens["weighted"] == 151
    # logs file group reports a nonzero size but no path/content (covered above)
    logs = next(g for g in run.files if g.category == "logs")
    assert logs.count == 1 and logs.total_bytes > 0 and logs.total_lines == 2
    # high-risk policy keys reduced, not leaked
    assert run.policy["adapter"]["extra_args_count"] == 2
    assert run.policy["scm"]["commit_message_template_set"] is True
    assert run.policy["plugins"]["settings"] == ["unity"]
    assert run.plugin_shared_keys == 1


def test_unknown_future_field_is_safe_by_default(project):
    run_dir = _seed_run(
        project.project,
        extra_journal=[("future-event", {"secret_field": "CANARY_FUTURE long prose detail"})],
    )
    _diag, _pseudo, combined = _render_all([run_dir])
    assert "CANARY_FUTURE" not in combined
    assert "future-event" in combined  # the kind itself is structural


def test_all_runs_scope(project):
    a = _seed_run(project.project, run_id="20260627-120000-aaaa")
    b = _seed_run(project.project, run_id="20260627-130000-bbbb")
    diag, _pseudo, _combined = _render_all([a, b])
    assert len(diag.runs) == 2


def test_legend_reverses_locally_but_never_ships(project):
    run_dir = _seed_run(project.project)
    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect([run_dir], pseudo=pseudo)
    combined = diagnostics.render_markdown(diag, pseudo=pseudo) + diagnostics.render_json(
        diag, pseudo=pseudo
    )
    legend = pseudo.legend()
    # the legend maps an alias back to the real story key (local convenience)...
    assert STORY_KEY in legend.values()
    # ...but the real key never appears in the shipped dump
    assert STORY_KEY not in combined
    assert PROPRIETARY not in combined


def test_unreadable_run_does_not_crash(project):
    run_dir = project.project / ".bmad-loop" / "runs" / "20260627-120000-cccc"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{ this is not valid json")
    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect([run_dir], pseudo=pseudo)
    assert len(diag.runs) == 1
    assert diag.runs[0].warnings  # flagged as unreadable
    # still renders without raising
    diagnostics.render_markdown(diag, pseudo=pseudo)


# ------------------------------------------------------ backstop repair (#186)


def _seed_routing_gap(project):
    """A run whose journal carries a real story key in an UNLISTED field — the
    _scrub_entry else-branch gap: identifier-shaped, so scrub_json passes it
    verbatim while its aliased twin put the original into the legend."""
    return _seed_run(
        project.project,
        extra_journal=[("custom-event", {"mystery_ref": STORY_KEY})],
    )


def test_routing_gap_is_repaired_end_to_end(project):
    run_dir = _seed_routing_gap(project)
    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect([run_dir], pseudo=pseudo)
    reps: list[tuple[str, int]] = []
    js = diagnostics.render_json(diag, pseudo=pseudo, repairs=reps)  # must not raise
    alias = next(a for ns, orig, a in pseudo.entries() if orig == STORY_KEY)
    assert STORY_KEY not in js
    assert alias in js
    # the repair is disclosed in the dump itself and reported to the caller
    assert json.loads(js)["backstop_repairs"] == {f"story:{alias}": 1}
    assert reps == [(f"story:{alias}", 1)]
    for canary in CANARIES:
        assert canary not in js, f"LEAK after repair: {canary!r}"


def test_render_json_keys_are_sorted(project):
    """`sort_keys=True` keeps two dumps diffable. Only object_pairs_hook can see
    key ORDER — json.loads into a dict preserves insertion order, so a plain
    round-trip cannot detect the flag being dropped."""
    run_dir = _seed_run(project.project)
    pseudo = sanitize.Pseudonymizer()
    js = diagnostics.render_json(diagnostics.collect([run_dir], pseudo=pseudo), pseudo=pseudo)

    def hook(pairs):
        keys = [k for k, _ in pairs]
        assert keys == sorted(keys), f"object keys not sorted: {keys}"
        return dict(pairs)

    json.loads(js, object_pairs_hook=hook)


def test_no_repairs_on_fully_routed_run(project):
    """The canonical seeded run needs ZERO repairs — the repair path must never
    silently normalize a new per-field routing gap (CI keeps catching them)."""
    run_dir = _seed_run(project.project)
    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect([run_dir], pseudo=pseudo)
    reps: list[tuple[str, int]] = []
    md = diagnostics.render_markdown(diag, pseudo=pseudo, repairs=reps)
    js = diagnostics.render_json(diag, pseudo=pseudo, repairs=reps)
    assert reps == []
    assert "Backstop repairs" not in md
    assert "backstop_repairs" not in js


def test_leakdetected_is_the_shared_sanitize_exception():
    """The re-export must stay importable as diagnostics.LeakDetected — cli.py's
    except clause resolves it here, and ruff's F401 autofix deletes a bare
    re-export (the noqa carries it; this pin catches the regression)."""
    assert diagnostics.LeakDetected is sanitize.LeakDetected


def test_repair_note_is_inside_verified_bytes(project):
    """The disclosure appended after repair is itself covered by the self-check."""
    run_dir = _seed_routing_gap(project)
    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect([run_dir], pseudo=pseudo)
    js = diagnostics.render_json(diag, pseudo=pseudo)
    extras = [(orig, f"{ns}:{alias}") for ns, orig, alias in pseudo.entries()]
    assert sanitize.assert_no_leak(js, extra=extras) == []


# ---- JSON-encoding fidelity of the guard (regression: #195 dropped the second render) ----
#
# Until `--json` became a pure document, EVERY dump was rendered to markdown too,
# and that raw-text pass is what actually caught these two. Once JSON mode stopped
# calling render_markdown, the JSON render became the only guard — and json.dumps
# is not a faithful carrier of the bytes assert_no_leak matches on: it doubles
# backslashes, and by default escapes non-ASCII to \uXXXX. Both evasions were live.
#
# These inject at the render boundary (the collector would scrub such a value long
# before it got here, which is exactly why the escape only ever bites on a routing
# gap — the case the backstop exists for). Everything downstream of _to_jsonable is
# the real path: real json.dumps options, real _guard, real fail-closed behavior.


def _render_json_over(monkeypatch, payload, *, pseudo=None):
    monkeypatch.setattr(diagnostics, "_to_jsonable", lambda _d: payload)
    return diagnostics.render_json(object(), pseudo=pseudo)


def test_json_escaped_windows_home_path_still_fails_closed(monkeypatch):
    """json.dumps doubles the separator: `C:\\Users\\x` serializes as `C:\\\\Users\\\\x`.
    A guard anchored on the raw form alone matched nothing and emitted the path."""
    with pytest.raises(diagnostics.LeakDetected) as exc:
        _render_json_over(monkeypatch, {"spec_file": r"C:\Users\alice\proj\story.md"})
    assert "absolute-home-path" in exc.value.rules
    # the POSIX form must keep firing too — the fix widened the rule, not moved it
    with pytest.raises(diagnostics.LeakDetected) as exc:
        _render_json_over(monkeypatch, {"spec_file": "/home/alice/proj/story.md"})
    assert "absolute-home-path" in exc.value.rules


def test_non_ascii_sensitive_value_reaches_the_guard(monkeypatch):
    """With the default ensure_ascii=True the value is escaped to `caf\\u00e9-user`,
    which matches no rule — yet json.loads hands the consumer back the original.
    The document a consumer parses is therefore what must be asserted on."""
    pseudo = sanitize.Pseudonymizer()
    original = "café-user"
    alias = pseudo.alias(original, ns="story", epic=1)

    rendered = _render_json_over(monkeypatch, {"mystery_ref": original}, pseudo=pseudo)

    # what a consumer actually receives — the escape hid the leak from `in rendered`
    assert json.loads(rendered)["mystery_ref"] == alias
    assert original not in rendered
    assert json.loads(rendered)["backstop_repairs"] == {f"story:{alias}": 1}


def test_non_ascii_survives_the_utf8_round_trip(tmp_path, monkeypatch):
    """ensure_ascii=False emits real non-ASCII, so confirm the document still
    round-trips through the encoding the CLI writes it with."""
    rendered = _render_json_over(monkeypatch, {"note": "café — naïve ✓"})
    path = tmp_path / "diag.json"
    path.write_text(rendered, encoding="utf-8")  # exactly what cmd_diagnose --out does
    assert json.loads(path.read_text(encoding="utf-8"))["note"] == "café — naïve ✓"


# The pure guard-mechanics tests (hard-rule refusal, repair tally, cyclic
# termination) live in tests/test_sanitize.py since #199 made guard shared API;
# this file keeps the integration surface: real collectors, real renders.
