"""Diagnostic-dump tests — the load-bearing one is the canary no-leak check.

A synthetic run dir is seeded with labelled secrets/PII/code in every sink the
dump could possibly read; the rendered report (markdown + JSON) must contain
none of them, while still preserving the diagnostic *structure*.
"""

from __future__ import annotations

import json
import re

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
]


def _seed_run(root, run_id="20260627-120000-aaaa", *, extra_journal=None):
    """Build a run dir loaded with canaries in every readable sink."""
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
