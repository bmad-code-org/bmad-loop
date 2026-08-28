"""Diagnostic-dump tests — the load-bearing one is the canary no-leak check.

A synthetic run dir is seeded with labelled secrets/PII/code in every sink the
dump could possibly read; the rendered report (markdown + JSON) must contain
none of them, while still preserving the diagnostic *structure*.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import types
import typing
from collections.abc import Mapping
from pathlib import Path

import pytest

from bmad_loop import diagnostics, sanitize
from bmad_loop.journal import Journal, save_state
from bmad_loop.model import Phase, RunState, SessionRecord, StoryTask, TokenUsage
from bmad_loop.policy import Policy

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
# Deliberately does NOT embed STORY_KEY: `checkpoint-pause` journals
# `task.spec_file`, which `StoryTask._serialized_spec_file` persists
# worktree-relative — a bare basename for a spec at the worktree root — and a
# spec is free to be named after the feature rather than the story. Such a name
# is identifier-shaped, so the scrub_json fallback emits it verbatim, and the
# egress backstop cannot repair what was never aliased — only per-field routing
# can. A name like `1.2-Acme….md` would be rescued by the story key inside it
# and would prove nothing.
SPEC_NAME = "AcmeVaultRotation.md"
# The same spec as the reconcile kinds journal it: absolute, under the home path.
SPEC_ABS = f"{HOME_PATH}/docs/stories/{SPEC_NAME}"

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


def _seed_run(
    root,
    run_id="20260627-120000-aaaa",
    *,
    extra_journal=None,
    sweeps_triggered=(),
    sweeps_refused=None,
):
    """Build a run dir loaded with canaries in every readable sink.

    ``sweeps_triggered`` seeds a routing gap the MARKDOWN report can reach: the
    collector passes identifier-shaped entries through verbatim, and the report
    renders them inline. (``extra_journal`` seeds a gap only the JSON document
    reaches — markdown renders journal aggregates, never per-entry fields.)

    ``sweeps_refused`` is the #501 sibling and reaches both renders the same way,
    except that it is a mapping — so a seed can aim a canary at the key half, the
    value half, or both independently.
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
        sweeps_refused=dict(sweeps_refused or {}),
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
    j.append("checkpoint-pause", story_key=STORY_KEY, checkpoint="plan", spec=SPEC_NAME)
    # The SAME spec, in the other shape a producer emits: engine.py's reconcile
    # and marker-repair kinds journal `str(spec_path)`, which
    # `verify.resolve_spec_path` returns absolute, while `checkpoint-pause` above
    # journals `task.spec_file` — persisted worktree-relative, so a bare basename
    # for a root-level spec. Seeded here rather than in one test so the canary
    # sweep covers the path shape too.
    j.append("spec-status-reconciled", story_key=STORY_KEY, spec=SPEC_ABS)
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


# a plain relative project for tests that are about run payloads, not the #332
# verdict — any non-WSL path yields the same checked `False`
ANY_PROJECT = Path("p")


def _render_all(run_dirs):
    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect(run_dirs, pseudo=pseudo, project=ANY_PROJECT)
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


def test_sweeps_refused_redacts_both_halves(project):
    """#501: `sweeps_refused` is a mapping, so it has two redaction surfaces.

    The value is a closed `SWEEP_REFUSED_*` slug wherever the orchestrator wrote
    it — but neither half is re-validated on load, and the key is a trigger string
    off state.json, the same untrusted footing as `sweeps_triggered`. This matters
    more than a usual scrub: a home path reaching `sanitize.guard` is not redacted
    there, it RAISES `LeakDetected` and the whole dump is refused. Filtering here
    is what keeps a malformed run diagnosable at all.

    The structure is asserted before rendering on purpose. Under ablation the
    render would raise `LeakDetected` rather than fail an assert, which says "a
    dump was refused" and not which half leaked.

    Ablation, three axes, verified by reading the diff and not just the red:
    drop the key's `looks_like_identifier` branch and ONLY the path-shaped-key
    entry differs (pytest reports the other two as identical); drop the value's
    and only the `run-end` entry does; delete the markdown `sweeps_refused` row
    and only the render asserts at the end fail."""
    run_dir = _seed_run(
        project.project,
        sweeps_refused={HOME_PATH: "dirty", "run-end": HOME_PATH, "epic-1": "not-started"},
    )
    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect([run_dir], pseudo=pseudo, project=ANY_PROJECT)

    (r,) = diag.runs
    assert r.sweeps_refused == {
        "<redacted:str>": "dirty",  # path-shaped KEY
        "run-end": "<redacted:str>",  # path-shaped VALUE
        "epic-1": "not-started",  # the well-formed pair is untouched
    }

    md = diagnostics.render_markdown(diag, pseudo=pseudo)
    js = diagnostics.render_json(diag, pseudo=pseudo)
    assert HOME_PATH not in md + js
    # and the row actually renders — a field collected but never surfaced would
    # satisfy every leak assertion above while shipping nothing.
    assert "**sweeps_refused:**" in md and "`epic-1`: not-started" in md
    assert json.loads(js)["runs"][0]["sweeps_refused"]["epic-1"] == "not-started"


def test_env_names_the_platform_and_the_win32_on_wsl_path_verdict(project, monkeypatch):
    """#332: `platform.system()` says "Windows" for both a native shell and a WSL
    interop launch, so the raw sys.platform token plus the verdict are what explain
    which backend default the run took."""
    from bmad_loop.adapters.multiplexer import get_multiplexer

    # `collect_env` reaches `get_multiplexer()`, an lru_cache(maxsize=1) that selects
    # on `sys.platform`; without these clears the patched window caches the Windows
    # pick for every later test in the worker.
    get_multiplexer.cache_clear()
    try:
        monkeypatch.setattr(diagnostics.sys, "platform", "win32")
        pseudo = sanitize.Pseudonymizer()
        unc = Path("\\\\wsl.localhost\\Ubuntu-24.04\\home\\u\\p")
        diag = diagnostics.collect([_seed_run(project.project)], pseudo=pseudo, project=unc)
    finally:
        # pytest undoes the patch on its own, but only at teardown — a raise in
        # `collect` would leave the Windows pick cached past this test without this.
        monkeypatch.undo()
        get_multiplexer.cache_clear()
    assert diag.env.sys_platform == "win32"
    assert diag.env.win32_on_wsl_path is True
    md = diagnostics.render_markdown(diag, pseudo=pseudo)
    # the rendered *value*, not just the label: asserting the label alone passes
    # equally for the "no" verdict, which is the answer this test exists to reject.
    assert "**sys.platform:** win32" in md
    assert "**win32 on WSL distro path:** yes" in md
    # The label must not claim a WSL *shell*: `cd \\wsl.localhost\...` from native
    # PowerShell reaches this same state, and the sibling `host.win32-on-wsl-path`
    # finding is worded to that limit — the two surfaces must make the same claim.
    assert "wsl interop" not in md.lower()
    # the boolean ships; the path it was derived from never does — the redactor
    # leaves the Linux username in a \\wsl.localhost\...\home\<user> path standing.
    assert str(unc) not in md
    # the `--json` document is its own contract: pin the field names and values
    # there too, not only the markdown labels.
    js = diagnostics.render_json(diag, pseudo=pseudo)
    payload = json.loads(js)
    assert payload["env"]["sys_platform"] == "win32"
    assert payload["env"]["win32_on_wsl_path"] is True
    # Assert over the *decoded* values, never the rendered bytes. Two independent
    # reasons a substring scan of `js` cannot carry this guard, both measured:
    #   - `json.dumps` doubles every backslash, so the raw `\\wsl.localhost\...`
    #     spelling never appears in the rendered bytes it is compared against
    #     (the same trap `cli.py` already names at its diagnose egress guard);
    #   - `sanitize`'s absolute-path rules know POSIX `/home/`, `/Users/`, `/root/`
    #     and `C:\Users\` — never a backslash `\home\` under a UNC host — and its
    #     username rule compares the *Windows* account, so `assert_no_leak` returns
    #     `[]` for this shape no matter what leaked.
    # Ablation: adding a raw-path field to `EnvInfo` leaves both of those green and
    # only the check below reddens.
    assert all(str(unc) not in str(v) for v in payload["env"].values())
    assert sanitize.assert_no_leak(js) == []  # general backstop; blind to this shape


def test_env_win32_on_wsl_path_is_false_off_win32(project, monkeypatch):
    """The *platform* half of the twin gate, pinned. What #332 names is a mismatched
    interpreter, not a path shape: the very distro path a win32 interpreter warns about
    is a perfectly ordinary mount for a Linux one. `runsetup`'s twin already carries
    this row (`test_win32_on_wsl_path_stays_silent_off_the_shape`'s `linux` case);
    without it here, deleting `sys.platform == "win32"` from `collect_env` reddens
    nothing in this file — measured, which is why the row exists."""
    from bmad_loop.adapters.multiplexer import get_multiplexer

    # Same cache dance as the win32 test above, and for the same reason: `collect_env`
    # reaches `get_multiplexer()`, whose lru_cache selects on the patched `sys.platform`.
    get_multiplexer.cache_clear()
    try:
        monkeypatch.setattr(diagnostics.sys, "platform", "linux")
        pseudo = sanitize.Pseudonymizer()
        unc = Path("\\\\wsl.localhost\\Ubuntu-24.04\\home\\u\\p")
        diag = diagnostics.collect([_seed_run(project.project)], pseudo=pseudo, project=unc)
    finally:
        monkeypatch.undo()
        get_multiplexer.cache_clear()
    assert diag.env.sys_platform == "linux"
    assert diag.env.win32_on_wsl_path is False


def test_env_win32_on_wsl_path_is_false_for_a_plain_project(project):
    """Both new fields render for every host, not only the interop one."""
    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect([_seed_run(project.project)], pseudo=pseudo, project=project.project)
    assert diag.env.win32_on_wsl_path is False
    assert diag.env.sys_platform == sys.platform
    assert "**win32 on WSL distro path:** no" in diagnostics.render_markdown(diag, pseudo=pseudo)


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
    """The spec-carrying journal kinds carry the spec's name, which is the
    customer's feature name. `test_no_canary_leaks_anywhere` already proves it does
    not ship; this pins HOW — a per-field alias in a `spec` namespace of its own,
    so it never renders in the epic-less `story-<hex>` shape a reused "story"
    namespace would give a filename. Nothing else can cover it: the value is not
    in the legend until it is routed, so the egress backstop has no alias to
    substitute.

    The namespace, not just the routing, is what this pins: `fullmatch` on the
    `spec-` prefix is what fails if the field is moved to "story". (Correlation
    across the two shapes a producer emits is a separate contract with its own
    witness below — this test's run seeds both, but neither of its assertions can
    see the difference.)"""
    run_dir = _seed_run(project.project)
    diag, pseudo, combined = _render_all([run_dir])
    alias = next(a for _ns, orig, a in pseudo.entries() if orig == SPEC_NAME)
    assert re.fullmatch(r"spec-[0-9a-f]{12}", alias), alias
    assert alias in combined
    # distinct from the story alias, and not wearing its shape
    assert alias != diag.runs[0].tasks[0].alias


def test_one_spec_gets_one_alias_whichever_shape_it_was_journalled_in(project):
    """A spec journalled as a bare basename by `checkpoint-pause` and as an
    absolute path by the reconcile kinds is ONE spec, and must read as one.

    Aliasing is chosen over dropping precisely so a maintainer can follow one
    identifier across events (`_JOURNAL_ALIAS_FIELDS`' own docstring says so), so
    two aliases for one spec is not cosmetic — it is the feature failing silently
    in a dump that looks correct. The seeded run carries both shapes; the fixture
    can express the failure because `SPEC_ABS` and `SPEC_NAME` are different
    strings that must nonetheless collapse to a single alias.

    The legend assertion is the second half of the same fix and needs its own
    line: normalizing to the basename is also what keeps the user's absolute home
    path out of the `--legend` file. Before `spec` was routed at all, a path-shaped
    value was rejected by `scrub_json` and never entered the map; routing it
    without normalizing would have put it there."""
    run_dir = _seed_run(project.project)
    _diag, pseudo, _combined = _render_all([run_dir])
    aliases = {a for _ns, orig, a in pseudo.entries() if SPEC_NAME in orig}
    assert len(aliases) == 1, pseudo.entries()
    originals = [orig for _ns, orig, _a in pseudo.entries() if SPEC_NAME in orig]
    assert originals == [SPEC_NAME], originals
    assert HOME_PATH not in str(pseudo.legend())


def test_a_windows_spec_path_normalizes_to_the_same_alias():
    """The basename split handles backslashes, on every platform.

    A journal written on Windows is routinely read by `diagnose` on POSIX, where
    `PurePath(r"C:\\Users\\a\\x.md").name` is the WHOLE string — so the split is
    separator-agnostic by hand rather than delegated to pathlib. This test is the
    Windows witness that runs on the POSIX lanes too: it asserts on strings, never
    on the filesystem, so it must NOT be skipped off Windows — which is the only
    reason the divergence is covered at all."""
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    posix = diagnostics._scrub_entry(
        {"kind": "spec-status-reconciled", "spec": f"/home/u/docs/{SPEC_NAME}"}, pseudo, {}, None
    )
    windows = diagnostics._scrub_entry(
        {"kind": "spec-status-reconciled", "spec": f"C:\\Users\\u\\docs\\{SPEC_NAME}"},
        pseudo,
        {},
        None,
    )
    bare = diagnostics._scrub_entry(
        {"kind": "checkpoint-pause", "spec": SPEC_NAME}, pseudo, {}, None
    )
    assert posix["spec"] == windows["spec"] == bare["spec"]
    assert list(pseudo.legend().values()) == [SPEC_NAME]
    # Totality: a value ending in a separator has an empty tail, and `alias()`
    # passes "" through unaliased — the event would lose its only reference to
    # the spec rather than gain one. The `or value` fallback owns this line.
    trailing = diagnostics._scrub_entry(
        {"kind": "spec-status-reconciled", "spec": "docs/"}, pseudo, {}, None
    )
    assert trailing["spec"] != ""
    assert re.fullmatch(r"spec-[0-9a-f]{12}", trailing["spec"])


def test_verify_command_free_text_drops_to_presence_booleans():
    """A `verify-command-result` record ships its correlation half, never its text.

    `_scrub_entry` routes by field NAME, and five of this record's fields are free
    text: `command` is operator-authored shell, `output_tail` is a build's own
    output, `capture_error` is an OSError string carrying a path, and the two
    stream pointers embed the story key. Left to the `scrub_json` fallback they
    fail closed only by ACCIDENT of shape — `_IDENTIFIER_RE` forbids `/` and
    spaces, so paths, argv-ish commands and multi-line tails collapse — but a
    one-word command like `make` satisfies it and ships verbatim.

    Ablation: remove the five names from `_JOURNAL_DROP_FIELDS`. `command` comes
    back as the literal `make` (reddening the presence assertion AND the canary
    sweep), while `output_tail` / `capture_error` / `stdout_path` merely turn into
    `<redacted:str>` — which is why `make` is the value under test and not a
    path-shaped one: only it separates the drop list from the fallback.
    """
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    out = diagnostics._scrub_entry(
        {
            "ts": 1.0,
            "kind": "verify-command-result",
            "story_key": STORY_KEY,
            "attempt": 2,
            "verification_stage": "dev",
            "verification_sequence": 3,
            "command_index": 0,
            "command": "make",
            "returncode": 1,
            "output_tail": CODE,
            "capture_error": f"stdout: [Errno 28] No space left on device: '{HOME_PATH}/x'",
            "stdout_path": f"verify/verify-{STORY_KEY}-dev-2-3-0.stdout.log",
            "stderr_path": None,
            "stdout_bytes": 12,
            "stdout_truncated": False,
        },
        pseudo,
        {},
        1.0,
    )

    for field in ("command", "output_tail", "capture_error", "stdout_path", "stderr_path"):
        assert field not in out, f"{field} must never be emitted"
    assert out["command_present"] is True
    assert out["output_tail_present"] is True
    assert out["capture_error_present"] is True
    # the pointers keep the one fact they are worth: whether a stream was retained
    # at all — `stream_capture_kb = 0` and a failed write both leave it null.
    assert out["stdout_path_present"] is True
    assert out["stderr_path_present"] is False
    # ... while everything a maintainer correlates on still ships verbatim
    assert (out["verification_stage"], out["verification_sequence"]) == ("dev", 3)
    assert (out["command_index"], out["returncode"], out["attempt"]) == (0, 1, 2)
    assert (out["stdout_bytes"], out["stdout_truncated"]) == (12, False)

    rendered = json.dumps(out)
    for canary in ("make", CODE, HOME_PATH, PROPRIETARY, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"


def test_rearm_records_leak_neither_the_code_root_nor_a_spec_name():
    """The two records `runs.rearm_escalation` added must be routed by FIELD NAME,
    not left to the `scrub_json` fallback (#640, #716).

    `repo` is an absolute host path naming the run's code tree, and the fallback
    fails closed only by accident of shape — `looks_like_identifier` forbids `/`,
    so a POSIX root collapses, but a one-segment root would ship verbatim. It is
    DROPPED rather than aliased: one run has one code root, so it correlates
    nothing, and the `error` field on the same record is already dropped.

    `spec_file` is the customer's feature name (the very hazard
    `_JOURNAL_ALIAS_FIELDS`' `spec` entry exists for) and is ALIASED, so a
    maintainer can still follow one spec across events.

    `overwritten` and `baseline` are both shas on one record, so aliasing one and
    leaving the other would pseudonymize half a comparison — the assertion below
    is that BOTH come back aliased and DIFFERENT from each other.

    Ablation: drop `repo` from `_JOURNAL_DROP_FIELDS` and the PRESENCE assertion
    below reddens — not the canary sweep, which stays green because `scrub_json`
    already collapses an absolute path to `<redacted:str>` (verified by running that
    ablation: `repo` comes back as `'<redacted:str>'`, so the canary never appears).
    That is the whole point of the drop: the fallback happens to redact THIS path
    shape, so only an assertion on the field's absence can grade a routing decision
    taken for a shape the fallback would not catch. Drop `spec_file` or `overwritten`
    from `_JOURNAL_ALIAS_FIELDS` and the alias assertions redden (`spec_file` on the
    canary sweep too).
    """
    other_sha = "f" * 40
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    advance_failed = diagnostics._scrub_entry(
        {
            "ts": 1.0,
            "kind": "rearm-baseline-advance-failed",
            "story_key": STORY_KEY,
            "repo": HOME_PATH,
            "baseline": SHA,
            "error": f"GitError: git rev-parse HEAD failed in {HOME_PATH}",
        },
        pseudo,
        {},
        1.0,
    )
    restamped = diagnostics._scrub_entry(
        {
            "ts": 2.0,
            "kind": "rearm-baseline-restamped",
            "story_key": STORY_KEY,
            "spec_file": SPEC_ABS,
            "overwritten": other_sha,
            "baseline": SHA,
            "restore": False,
        },
        pseudo,
        {},
        1.0,
    )

    assert "repo" not in advance_failed and advance_failed["repo_present"] is True
    assert "error" not in advance_failed  # the sibling that was already routed
    # aliased, not dropped: the key stays and the VALUE is replaced, which is what
    # keeps the record correlatable across events
    alias = next(a for ns, orig, a in pseudo.entries() if ns == "spec" and orig == SPEC_NAME)
    assert restamped["spec_file"] == alias
    # the absolute spelling reduced to the basename first, so this spec has ONE
    # alias and the home path never entered the legend
    assert [orig for ns, orig, _a in pseudo.entries() if ns == "spec"] == [SPEC_NAME]
    # both shas aliased, and distinguishable from each other
    assert restamped["overwritten"] != other_sha and restamped["baseline"] != SHA
    assert restamped["overwritten"] != restamped["baseline"]
    assert restamped["restore"] is False  # a plain flag still ships

    # The OTHER three kinds `runs.rearm_escalation` journals `spec_file` on. Routing is
    # by field NAME, so these ride the same `_JOURNAL_ALIAS_FIELDS` entry as
    # `rearm-baseline-restamped` and are correct today for free — which is exactly why
    # they belong in the sweep: the canary is what catches a field added to one of
    # these kinds later, and a sweep that covers two of four grades the routing of a
    # record shape nobody re-checks.
    siblings = [
        diagnostics._scrub_entry(
            {"ts": 3.0, "kind": kind, "story_key": STORY_KEY, "spec_file": SPEC_ABS, **extra},
            pseudo,
            {},
            1.0,
        )
        for kind, extra in (
            ("rearm-spec-write-unreachable", {}),
            ("rearm-spec-flip-skipped", {"status": "ready-for-dev"}),
            ("rearm-baseline-restamp-skipped", {"baseline": SHA}),
        )
    ]
    # every one of them aliases to the SAME alias as the restamped record above: one
    # spec, one alias, however many kinds carry it
    assert [s["spec_file"] for s in siblings] == [alias, alias, alias]
    assert [orig for ns, orig, _a in pseudo.entries() if ns == "spec"] == [SPEC_NAME]

    rendered = json.dumps([advance_failed, restamped, *siblings])
    for canary in (SHA, other_sha, SPEC_NAME, PROPRIETARY, HOME_PATH, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"


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
    diag = diagnostics.collect([run_dir], pseudo=pseudo, project=ANY_PROJECT)
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
    diag = diagnostics.collect([run_dir], pseudo=pseudo, project=ANY_PROJECT)
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
    diag = diagnostics.collect([run_dir], pseudo=pseudo, project=ANY_PROJECT)
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
    diag = diagnostics.collect([run_dir], pseudo=pseudo, project=ANY_PROJECT)
    js = diagnostics.render_json(diag, pseudo=pseudo)

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
    diag = diagnostics.collect([run_dir], pseudo=pseudo, project=ANY_PROJECT)
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
    diag = diagnostics.collect([run_dir], pseudo=pseudo, project=ANY_PROJECT)
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


def test_env_tmux_version_folds_a_multi_line_probe(monkeypatch):
    """tmux_version is a scalar JSON field. Pre-fold (#321), a two-line probe
    hit scrub_text's line cap, whose "(N more lines redacted)" marker line
    re-introduced the very newline the cap was meant to remove."""
    from bmad_loop.adapters import multiplexer as mux_mod

    class _TwoLineMux:
        def version(self):
            return "tmux 3.3.7\npsmux 3.3.7"

    monkeypatch.setattr(mux_mod, "get_multiplexer", lambda: _TwoLineMux())

    env = diagnostics.collect_env(ANY_PROJECT)
    assert env.tmux_version == "tmux 3.3.7; psmux 3.3.7"
    assert env.multiplexer == "_TwoLineMux"


def test_env_tmux_version_stays_bounded(monkeypatch):
    """Nothing between here and the rendered dump bounds this field — asdict,
    json.dumps and sanitize.guard all pass it through — and scrub_text's cap
    only ever counted lines, so the fold owns the bound (#321)."""
    from bmad_loop.adapters import multiplexer as mux_mod

    class _ChattyMux:
        def version(self):
            return "tmux 3.3.7\n" + "\n".join("build detail " + "y" * 40 for _ in range(20))

    monkeypatch.setattr(mux_mod, "get_multiplexer", lambda: _ChattyMux())

    env = diagnostics.collect_env(ANY_PROJECT)
    assert env.tmux_version is not None
    assert len(env.tmux_version) == mux_mod.VERSION_MAX_CHARS
    assert "\n" not in env.tmux_version
    assert env.tmux_version.startswith("tmux 3.3.7")


def test_env_tmux_version_is_redacted_before_it_is_cut(monkeypatch):
    """The scrub runs on the whole probe, not on what survives the fold.

    The home path has to *straddle* the cut for this to mean anything: a probe
    that fits under the bound passes whichever way round the two run. Padded so
    the cut lands inside the path, folding first leaves a fragment `redact_home`
    can no longer match — and its `~` never appears, which is what discriminates
    the orders. `home not in ...` alone does not: the fragment isn't the whole
    path either.
    """
    from bmad_loop.adapters import multiplexer as mux_mod

    # A fixed home, not the host's: the padding below is arithmetic against its
    # length, and CI's home differs per runner. expanduser reads HOME on POSIX
    # and USERPROFILE on Windows, so set both (tests/test_sanitize.py does too).
    home = "/private/bmad-loop-home"
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    lead = "tmux 3.3.7 "
    pad = lead + "x" * (mux_mod.VERSION_MAX_CHARS - len(lead) - 3)

    class _HomeyMux:
        def version(self):
            return f"{pad}{home}/src/tmux"

    monkeypatch.setattr(mux_mod, "get_multiplexer", lambda: _HomeyMux())

    env = diagnostics.collect_env(ANY_PROJECT)
    assert env.tmux_version is not None
    assert env.tmux_version.endswith("…")  # the cut really fired
    assert home not in env.tmux_version
    assert "~" in env.tmux_version  # redaction saw the whole path, then the cut


def test_non_ascii_survives_the_utf8_round_trip(tmp_path, monkeypatch):
    """ensure_ascii=False emits real non-ASCII, so confirm the document still
    round-trips through the encoding the CLI writes it with."""
    rendered = _render_json_over(monkeypatch, {"note": "café — naïve ✓"})
    path = tmp_path / "diag.json"
    path.write_text(rendered, encoding="utf-8")  # exactly what cmd_diagnose --out does
    assert json.loads(path.read_text(encoding="utf-8"))["note"] == "café — naïve ✓"


# --------------------------------------------- the out-of-tree events channel


def _seed_bare_run(project_dir, run_id="20260812-090000-bbbb"):
    """A run dir with just enough state to collect: this test family is about
    WHERE `summarize_files` looks, not about the canary sweep."""
    run_dir = project_dir / ".bmad-loop" / "runs" / run_id
    save_state(
        run_dir,
        RunState(
            run_id=run_id,
            project=str(project_dir),
            started_at="2026-08-12T09:00:00",
            run_type="story",
        ),
    )
    return run_dir


def _events_group(run_dir, project_dir):
    diag = diagnostics.collect(
        [run_dir], pseudo=sanitize.Pseudonymizer(), project=Path(project_dir)
    )
    return next((g for g in diag.runs[0].files if g.category == "events"), None)


def _write_events(events_dir, n, *, prefix=""):
    events_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (events_dir / f"{prefix}17550000{i}-t-Stop.json").write_text("{}", encoding="utf-8")


def test_events_are_counted_at_the_out_of_tree_state_root(project, tmp_path, monkeypatch):
    """#494: the events channel left the project tree for the user state root, and
    `_FILE_CATEGORIES` listed "events" as a run-dir-relative name. That made the
    group VANISH silently rather than redden — the `is_dir()` guard turns a
    category with no directory into a no-op, so the dump simply stopped mentioning
    events at all, on exactly the runs a maintainer reads a dump to understand.

    The count is derived through the real `collect` path, not by handing
    `summarize_files` the directory: what has to hold is that a collector given
    only a run dir finds the channel, and the derivation (from the run's OWN
    recorded project and id) is the part that can break.

    Ablation guard: making `_events_dir` return None, or dropping the primary from
    `_category_roots`, makes this FAIL on the count."""
    from bmad_loop import envvars, runs

    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "state"))
    run_dir = _seed_bare_run(project.project)
    _write_events(runs.events_dir_for(project.project, run_dir.name), 3)

    group = _events_group(run_dir, project.project)
    assert group is not None and group.count == 3
    assert group.total_bytes > 0
    # The in-tree location is genuinely empty — the count above came from the
    # state root, not from a legacy directory a fixture happened to create.
    assert not (run_dir / "events").exists()


def test_legacy_in_tree_events_still_counted_and_summed_with_the_primary(
    project, tmp_path, monkeypatch
):
    """Both roots are live at once. The hook relay is COPIED into the project by
    `init`, so an upgraded orchestrator routinely drives sessions whose relay
    predates the move and still writes in-tree; `SignalWatcher` dual-polls for
    exactly that reason. A dump that counted only the primary would report zero
    for such a session, which is the same blind spot in a different place.

    Ablation guard: dropping the legacy root from `_category_roots` makes this
    FAIL (2 instead of 5)."""
    from bmad_loop import envvars, runs

    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "state"))
    run_dir = _seed_bare_run(project.project)
    _write_events(runs.events_dir_for(project.project, run_dir.name), 3, prefix="new-")
    _write_events(run_dir / "events", 2, prefix="old-")

    group = _events_group(run_dir, project.project)
    assert group is not None
    # ONE group, summed — the payload shape is the v1 schema and does not split.
    assert group.count == 5


def test_events_degrade_to_the_legacy_root_when_the_state_root_is_underivable(
    project, tmp_path, monkeypatch
):
    """A dump is routinely read on a machine that did not produce the run, and the
    state root is a HOST fact. `runs.state_root()` raises rather than guessing when
    no candidate answers (it is a write path); observation must not inherit that —
    the events count is worth losing, the dump is not.

    Ablation guard: narrowing `_events_dir`'s except clause so the raise escapes
    makes this FAIL — and the way it fails is the point. `collect` catches per
    run, so the escape does not crash the dump; it demotes the WHOLE run to
    `_unreadable_run`, losing its tasks, journal and every other file group to a
    host fact that has nothing to do with the run. Silent, and far worse than the
    count this degradation gives up."""
    from bmad_loop import envvars, runs

    monkeypatch.delenv(envvars.STATE_DIR, raising=False)
    monkeypatch.setattr(runs, "state_root", _raise_no_state_root)
    run_dir = _seed_bare_run(project.project)
    _write_events(run_dir / "events", 2)

    group = _events_group(run_dir, project.project)
    assert group is not None and group.count == 2


def _raise_no_state_root():
    from bmad_loop.runs import StateRootError

    raise StateRootError("no state root on this host")


def test_state_root_path_is_redacted_in_the_dump(project, tmp_path, monkeypatch):
    """#494 put a control-plane directory under the user's home, and every home is
    an egress hazard: `/home/<user>/.local/state/bmad-loop/...` and
    `C:\\Users\\<user>\\AppData\\Local\\bmad-loop\\state\\...` both name a real
    person. The dump only ever *stats* that directory, so no field carries it
    today — this pins the two backstops that must hold if one ever does.

    Both spellings are asserted on ONE host on purpose: `_ABS_HOME_RE` is
    platform-independent by construction (a Windows-shaped path is diagnosed on
    POSIX whenever a Windows run's dump is read on Linux), and a rule that only
    fired on its native platform would pass CI on the runner that never sees it.
    Neither uses the host's own home, which `redact_home` would rewrite to `~`
    before the rule was ever consulted — that would prove the wrong mechanism."""
    monkeypatch.setenv("BMAD_LOOP_STATE_DIR", str(tmp_path / "state"))
    posix_root = "/home/canaryoperator/.local/state/bmad-loop/9f1c2d3e4a5b6c7d/r/events"
    win_root = r"C:\Users\canaryoperator\AppData\Local\bmad-loop\state\9f1c2d3e4a5b6c7d\r\events"

    # The realistic vector: an unknown journal field. Unknown fields fall to
    # `scrub_json`, so this half proves per-field routing catches the path.
    for i, planted in enumerate((posix_root, win_root)):
        run_dir = _seed_bare_run(project.project, run_id=f"20260812-09000{i}-cccc")
        Journal(run_dir).append("events-routed", events_dir=planted)
        _diag, _pseudo, combined = _render_all([run_dir])
        assert planted not in combined
        assert "canaryoperator" not in combined

    # And this half proves the egress guard would have refused the path anyway —
    # which is what actually covers a field nobody has written yet. It runs LAST
    # and in its own loop: `_render_json_over` leaves `_to_jsonable` monkeypatched
    # for the rest of the test, so a later `_render_all` would re-render this
    # payload instead of its own run.
    for planted in (posix_root, win_root):
        with pytest.raises(diagnostics.LeakDetected) as exc:
            _render_json_over(monkeypatch, {"events_dir": planted})
        assert "absolute-home-path" in exc.value.rules


# ------------------------------------- the _scrub_policy key invariant (#202)
#
# `_scrub_policy` emits dict KEYS verbatim (the else-branch passthrough), unlike
# `sanitize._scrub`, which scrubs keys as well as values. That is safe only while
# no policy section is a free-keyed table. These pin the invariant so the rule is
# discoverable from a failure rather than only from the comment beside the code.


def _policy_free_keyed_fields(dc, prefix="", seen=frozenset()):
    """Dotted paths of every Mapping-typed field in ``dc``'s dataclass tree.

    `policy.py` uses `from __future__ import annotations`, so `field.type` is a
    STRING and only `typing.get_type_hints` gives back a comparable type."""
    if dc in seen:  # defensive: the policy tree is a DAG today, not a cycle
        return
    seen = seen | {dc}
    hints = typing.get_type_hints(dc)
    for fld in dataclasses.fields(dc):
        yield from _classify_policy_type(hints[fld.name], f"{prefix}{fld.name}", seen)


def _classify_policy_type(tp, path, seen):
    """Walk one resolved annotation, yielding ``path`` when it is a free-keyed
    table. `get_origin` covers the subscripted `dict[str, X]` case; the bare
    `dict`/`Mapping` case falls through to `tp` itself."""
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        for arg in args:
            yield from _classify_policy_type(arg, path, seen)
        return
    base = origin or tp
    if isinstance(base, type) and issubclass(base, Mapping):
        # A free-keyed table: report it and stop. Descending into its value type
        # would report the same field twice for `dict[str, dict[str, Any]]`.
        yield path
        return
    if dataclasses.is_dataclass(base):
        yield from _policy_free_keyed_fields(base, f"{path}.", seen)
        return
    for arg in args:  # tuple[X, ...] / list[X] could nest a policy dataclass
        yield from _classify_policy_type(arg, f"{path}[]", seen)


def test_no_policy_section_has_a_free_keyed_table():
    """The invariant `_scrub_policy`'s key passthrough rests on: no policy section
    is a free-keyed table, so every key in a diagnose dump is a compile-time field
    name rather than user data. `plugins.settings` is the sole exception and is
    intercepted by `_POLICY_KEYSET_KEYS` before it can reach the passthrough.

    This walks the field TYPES, and that is the load-bearing half of the pair. A
    newly added free-keyed section — `adapter.overrides: dict[str, str]` keyed by
    binary path, say — defaults to an EMPTY dict, so `Policy().to_dict()` yields
    none of its keys and the value-level twin
    (`test_every_policy_snapshot_key_is_identifier_shaped`) stays green while the
    hazard is live. At declaration time the type is the only evidence there is,
    and this test is what reads it.

    The assertion is an EXACT set rather than a subset for the same reason: a
    subset check is satisfied by a tree that grew a table, which is precisely the
    event it would exist to catch.

    When this fails, a new table was added. Either route it through
    `_POLICY_KEYSET_KEYS` / `_POLICY_COUNT_KEYS` in `diagnostics.py` so its keys
    are reduced before they ship, or establish that its keys cannot carry user
    data. Do not simply widen the expected set (#202)."""
    assert set(_policy_free_keyed_fields(Policy)) == {"plugins.settings"}


def test_every_policy_snapshot_key_is_identifier_shaped():
    """Every key the policy snapshot actually ships is a machine slug, never PII
    — the value-level companion to the type-level check above (#202)."""
    offenders: list[str] = []

    def walk(obj, path):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if not sanitize.looks_like_identifier(str(key)):
                    offenders.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                walk(item, f"{path}[]")

    # Belt to `test_no_policy_section_has_a_free_keyed_table`'s braces: this
    # catches a field NAME that is not slug-shaped, where a leading-underscore
    # private field is the realistic case (`sanitize._IDENTIFIER_RE` requires the
    # first character to be alphanumeric). It cannot catch an empty-by-default
    # free-keyed table — there are no keys to walk — which is why the type-level
    # test is the load-bearing one and this cannot replace it.
    walk(Policy().to_dict(), "policy")
    assert offenders == []


def test_scrub_policy_passes_unknown_section_keys_verbatim():
    """Characterization of the else-branch: an unknown section's keys are emitted
    VERBATIM — even a home path — while `sanitize.scrub_json` redacts the same key.

    This pins the CURRENT, deliberate behavior (field names are the point of the
    snapshot, and redacting them would cost the reader the dump's whole index) and
    is the exact hazard `test_no_policy_section_has_a_free_keyed_table` guards. It
    is not an endorsement: if `_scrub_policy` is ever changed to scrub keys, this
    test is expected to change with it rather than to stand in the way (#202).

    The passthrough is KEYS ONLY, and the value row below is what says so."""
    snapshot = {"future": {HOME_PATH: {"model": HOME_PATH}}}
    scrubbed = diagnostics._scrub_policy(snapshot)
    assert list(scrubbed["future"]) == [HOME_PATH]
    # Keys only. An unknown section's VALUES still go through the standard gate,
    # so the same home path IS redacted one level down. This row is load-bearing
    # against the shape of fix a reader reaches for when they want the keys kept:
    # flattening the else-branch's `_scrub_policy(value)` recursion to a bare
    # `value` keeps every other assertion here green while turning the whole
    # branch into a leak, so without it this test would characterize one.
    assert scrubbed["future"][HOME_PATH]["model"] == "<redacted:str>"
    # The contrast that makes the passthrough a deliberate divergence rather than
    # an oversight: the shared value gate would not have let this key through.
    assert HOME_PATH not in sanitize.scrub_json(snapshot)["future"]


# The pure guard-mechanics tests (hard-rule refusal, repair tally, cyclic
# termination) live in tests/test_sanitize.py since #199 made guard shared API;
# this file keeps the integration surface: real collectors, real renders.


# --------------------------------------------- the verifier stream store


def test_verify_streams_are_counted_but_never_read(project, tmp_path):
    """`verify/` is stat-only: its SIZE is the diagnostic, its contents are not.

    The store can be one of the larger things in a run dir — `stream_capture_kb`
    defaults to 256 KiB per stream, so up to 512 KiB per command per attempt, with
    no GC behind it yet — so a dump that omits it cannot show the retention or
    disk-usage problem a maintainer opens a dump to find. It is equally the one
    category that must never be READ into the output: retained verifier output is
    a build's own stdout/stderr and may carry anything the project's test suite
    prints.

    Ablation guard: drop `VERIFY_DIR` from `_FILE_CATEGORIES` and the group is
    None — the `is_dir()` guard makes an unregistered category vanish silently
    rather than redden, which is exactly how this was missed. Verified.
    """
    run_dir = _seed_bare_run(project.project)
    verify_dir = run_dir / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    secret = "SUPER-SECRET-BUILD-OUTPUT-DO-NOT-EMIT"
    (verify_dir / "verify-1-1-a-dev-1-1-0.stdout.log").write_text(secret, encoding="utf-8")
    (verify_dir / "verify-1-1-a-dev-1-1-0.stderr.log").write_text("err", encoding="utf-8")

    diag = diagnostics.collect(
        [run_dir], pseudo=sanitize.Pseudonymizer(), project=Path(project.project)
    )
    group = next((g for g in diag.runs[0].files if g.category == "verify"), None)

    assert group is not None, "verify/ is not registered as a diagnostic category"
    assert group.count == 2
    assert group.total_bytes == len(secret) + len("err")

    # the half that matters as much as the count: the dump STATS, never reads
    assert secret not in diagnostics.render_markdown(diag)
    assert secret not in diagnostics.render_json(diag)


@pytest.mark.skipif(
    sys.platform == "win32", reason="planting a directory symlink needs privilege on win32"
)
def test_a_redirected_verify_root_is_not_counted_as_this_runs_output(project, tmp_path):
    """A planted redirect at `verify/` must not make `diagnose` report someone
    else's tree as this run's retained verifier output.

    `summarize_files` admits a category root on `root.is_dir()`, which FOLLOWS a
    link, and then walked it with `rglob("*")`. Measured before the fix: two files
    and 3100 bytes from outside the run, attributed to this run. Registering
    `verify/` as a category — the fix for the earlier "invisible store" gap — is
    what put a session-plantable directory on that traversal at all; every other
    category root is engine-created, which is why the hole opened here and not
    years ago.

    Ablation: walk the root with `rglob("*")` again and the group comes back
    naming the target's count and bytes. Verified.
    """
    run_dir = _seed_bare_run(project.project)
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    (outside / "a.bin").write_bytes(b"a" * 3000)
    (outside / "b.bin").write_bytes(b"b" * 100)
    (run_dir / "verify").symlink_to(outside, target_is_directory=True)

    diag = diagnostics.collect(
        [run_dir], pseudo=sanitize.Pseudonymizer(), project=Path(project.project)
    )
    group = next((g for g in diag.runs[0].files if g.category == "verify"), None)

    assert group is None  # nothing of ours is in there, so there is nothing to report
    assert (outside / "a.bin").is_file()  # and the dump did not touch what it found


# ---------------------------------------------------- planted non-regular files
#
# `summarize_files` walks with `walk_files_unlinked`, and `os.walk` reports every
# NON-DIRECTORY entry — FIFOs and symlinks included. The `is_file()` guard the old
# `rglob` loop carried came off with that switch, and the `logs` arm OPENS what it
# counts. Four ablation axes, and each reddens exactly one test below — the
# loop's `S_ISREG` inventory filter, and `_count_lines`' `O_NONBLOCK`,
# `O_NOFOLLOW`, and `S_ISREG`-on-the-fd. Disjoint failures are what shows the
# four guards are not standing in for each other.

_FIFO = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")


@_FIFO
def test_count_lines_refuses_an_idle_fifo_instead_of_blocking(tmp_path):
    """A FIFO nobody is feeding: opening it read-only without ``O_NONBLOCK``
    blocks until a writer arrives, which for a run directory the session owns
    means `diagnose` never returns and the operator's terminal is wedged.

    Bounded with ``SIGALRM`` rather than a subprocess, following
    `test_runs.py`'s twin: a hang is the failure under test, so the test needs a
    deadline of its own or an ablation wedges the suite instead of reddening it.

    ABLATION: drop ``O_NONBLOCK`` from the flags and the alarm fires. Dropping the
    fd ``S_ISREG`` check instead does NOT show up here — with no writer the read
    hits EOF and answers 0 either way, which is exactly why the fed twin below
    exists. Verified."""
    import signal

    path = tmp_path / "session.log"
    os.mkfifo(path)

    def _blew_up(signum, frame):
        raise AssertionError("the line count blocked on the FIFO instead of refusing it")

    previous = signal.signal(signal.SIGALRM, _blew_up)
    signal.alarm(20)
    try:
        assert diagnostics._count_lines(path) == 0
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@_FIFO
def test_count_lines_refuses_a_fed_fifo_without_consuming_it(tmp_path):
    """The half the alarm above cannot see. There the FIFO is idle, so the harm is
    a hang and the bytes read are merely empty; here a writer holds it open and is
    feeding it, so a reader that gets past the open never blocks — it counts
    whatever the session piped in as this run's log lines, and drains the pipe on
    the way through. Neither shows up as a hang, so the alarm above would never
    notice.

    ``O_RDWR`` for the holder deliberately — a write-only open on a FIFO blocks
    until a reader arrives and would wedge the test itself, and ``O_RDWR`` never
    blocks.

    ABLATION: delete the ``S_ISREG(os.fstat(fd))`` check and this answers **3** —
    the piped lines, billed to this run. The byte assert grades the second harm on
    the same axis: the read consumed them, so the holder's own read no longer
    finds what it wrote. Verified."""
    path = tmp_path / "session.log"
    os.mkfifo(path)

    holder = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(holder, b"one\ntwo\nthree\n")
        assert diagnostics._count_lines(path) == 0
        assert os.read(holder, 64) == b"one\ntwo\nthree\n"  # untouched
    finally:
        os.close(holder)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink + O_NOFOLLOW")
def test_count_lines_refuses_a_symlink_instead_of_reading_its_target(tmp_path):
    """``O_NOFOLLOW``: the walk refuses to descend THROUGH a redirect, but the
    final component it hands back is still a name, and the inventory filter that
    normally screens a symlinked entry out is a check-then-open race on a
    directory the session can write. The read anchors on the flag instead.

    ABLATION: drop ``O_NOFOLLOW`` and this returns 2 — the target's lines,
    attributed to this run. Verified."""
    outside = tmp_path / "elsewhere.txt"
    outside.write_bytes(b"theirs\nnot ours\n")
    link = tmp_path / "session.log"
    link.symlink_to(outside)

    assert diagnostics._count_lines(link) == 0


@_FIFO
def test_a_planted_fifo_is_not_counted_as_this_runs_log_output(project, tmp_path):
    """The inventory half, at the level a maintainer reads: a FIFO and a symlink
    planted in the run's own `logs/` are not this run's retained output, and
    counting either bills the report for bytes nobody wrote.

    Alarmed like the unit twin because an ablation that reaches the open would
    hang `collect` rather than fail it.

    ABLATION: delete the two ``S_ISREG`` inventory lines in `summarize_files` and
    the group reports 3 files and the symlink target's 3000 bytes instead of the
    one real log. Verified."""
    import signal

    run_dir = _seed_bare_run(project.project)
    logs = run_dir / "logs"
    logs.mkdir(parents=True)
    (logs / "dev.log").write_bytes(b"one\ntwo\n")
    os.mkfifo(logs / "piped.log")
    outside = tmp_path / "theirs.log"
    outside.write_bytes(b"t" * 3000)
    (logs / "linked.log").symlink_to(outside)

    def _blew_up(signum, frame):
        raise AssertionError("collect blocked on the planted FIFO")

    previous = signal.signal(signal.SIGALRM, _blew_up)
    signal.alarm(30)
    try:
        diag = diagnostics.collect(
            [run_dir], pseudo=sanitize.Pseudonymizer(), project=Path(project.project)
        )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    group = next(g for g in diag.runs[0].files if g.category == "logs")
    assert (group.count, group.total_bytes, group.total_lines) == (1, 8, 2)


def test_env_git_version_is_recorded(monkeypatch):
    """The field a floor refusal is read against: a dump has to be able to say which
    git ran, since `verify.GIT_FLOOR` is what `run` aborts below."""
    import subprocess

    from bmad_loop import verify

    monkeypatch.setattr(
        verify,
        "git_bytes",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["git", "version"], returncode=0, stdout=b"git version 2.34.1\n", stderr=b""
        ),
    )

    env = diagnostics.collect_env(ANY_PROJECT)
    assert env.git_version == "git version 2.34.1"


def test_env_git_version_probe_is_bounded(monkeypatch):
    """The probe carries its own short deadline instead of inheriting the engine's
    `GIT_TIMEOUT_S`. `diagnose` is the command an operator reaches for once the host
    is already broken, and a git that hangs is one of the states it has to stay
    usable in — on the engine bound it sat silent for two minutes and then swallowed
    the fault anyway, so the entire wait bought the same `None` a five-second bound
    reaches. Asserted through the seam rather than by timing anything, so the row
    cannot go flaky on a loaded box.

    Both halves matter: `is not None` catches a probe that went back to inheriting,
    and the comparison catches a "bound" that is no bound at all.

    Ablation: drop `timeout_s=5` in `collect_env` and this fails on the first
    assertion."""
    import subprocess

    from bmad_loop import verify

    seen = {}

    def probe(_project, *args, timeout_s=None):
        seen["args"] = args
        seen["timeout_s"] = timeout_s
        return subprocess.CompletedProcess(
            args=["git", "version"], returncode=0, stdout=b"git version 2.34.1\n", stderr=b""
        )

    monkeypatch.setattr(verify, "git_bytes", probe)

    env = diagnostics.collect_env(ANY_PROJECT)
    assert env.git_version == "git version 2.34.1"  # the probe still answers
    assert seen["args"] == ("version",)
    assert seen["timeout_s"] is not None, "the probe inherits the engine's git deadline"
    assert seen["timeout_s"] < verify.GIT_TIMEOUT_S


def test_env_git_version_is_none_when_the_probe_fails(monkeypatch):
    """`verify.git_bytes` ANSWERS a non-zero rc rather than raising, so a failed
    probe reaches the fold with whatever it wrote to stdout.

    Recording that as the version would put a fabricated fact in a dump read
    precisely to explain a refusal — worse than the honest `None`, because a
    plausible-looking version is not obviously absent. The stdout here is
    deliberately version-SHAPED: an empty one would pass on `or None` even with the
    rc guard gone, which is the vacuous form of this test.

    Ablation: drop the `probed.returncode == 0` guard in `collect_env` and this
    fails — the dump reports `git version 9.9.9`."""
    import subprocess

    from bmad_loop import verify

    monkeypatch.setattr(
        verify,
        "git_bytes",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["git", "version"], returncode=128, stdout=b"git version 9.9.9\n", stderr=b"fatal"
        ),
    )

    env = diagnostics.collect_env(ANY_PROJECT)
    assert env.git_version is None
