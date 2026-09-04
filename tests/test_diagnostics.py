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
from bmad_loop.journal import Journal, load_state, save_state
from bmad_loop.model import Phase, RunState, SessionRecord, StoryTask, TokenUsage
from bmad_loop.policy import Policy

# Labelled canaries planted across the run dir. NONE may appear in the dump.
EMAIL = "victim.canary@example.com"
STORY_KEY = "1.2-AcmeQuantumBillingEngine"
PROPRIETARY = "AcmeQuantumBillingEngine"
BRANCH = "feature/AcmeSecret"
# A branch name with NO separator, for the one row that grades branch-field ROUTING.
# `BRANCH` cannot: `scrub_json`'s `_IDENTIFIER_RE` forbids `/`, so a slashed name is
# collapsed to `<redacted:str>` by the fallback and a canary sweep over it stays green
# with the routing entry deleted — the same false green `repo` has, documented on
# `test_rearm_journal_fields_are_routed`. Bare `main`/`develop`-style names are the
# common case anyway, and they are exactly the ones the fallback waves through verbatim.
REARM_BRANCH = "AcmeSecretRelease"
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

    `_scrub_entry` routes by field NAME, and six of this record's fields are free
    text: `command` is operator-authored shell, `output_tail` is a build's own
    output, `capture_error` is an OSError string carrying a path, `spawn_error` is
    an OSError string carrying the run's code root twice, and the two stream
    pointers embed the story key. Left to the `scrub_json` fallback they fail
    closed only by ACCIDENT of shape — `_IDENTIFIER_RE` forbids `/` and spaces, so
    paths, argv-ish commands and multi-line tails collapse — but a one-word
    command like `make` satisfies it and ships verbatim.

    Ablation: remove the six names from `_JOURNAL_DROP_FIELDS`. `command` comes
    back as the literal `make` (reddening the presence assertion AND the canary
    sweep), while `output_tail` / `capture_error` / `spawn_error` / `stdout_path`
    merely turn into `<redacted:str>` — which is why `make` is the value under
    test and not a path-shaped one: only it separates the drop list from the
    fallback.
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
            "spawn_error": (
                f"child not started; cwd was {HOME_PATH}/code; "
                f"NotADirectoryError: [Errno 20] Not a directory: '{HOME_PATH}/code'"
            ),
            "stdout_path": f"verify/verify-{STORY_KEY}-dev-2-3-0.stdout.log",
            "stderr_path": None,
            "stdout_bytes": 12,
            "stdout_truncated": False,
        },
        pseudo,
        {},
        1.0,
    )

    for field in (
        "command",
        "output_tail",
        "capture_error",
        "spawn_error",
        "stdout_path",
        "stderr_path",
    ):
        assert field not in out, f"{field} must never be emitted"
    assert out["command_present"] is True
    assert out["output_tail_present"] is True
    assert out["capture_error_present"] is True
    assert out["spawn_error_present"] is True
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
    canary sweep too). Drop `target_branch` and the branch row reddens on BOTH the alias
    lookup and the canary sweep — that field is identifier-shaped by design, so unlike
    `repo` the fallback does not accidentally rescue it.
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

    # The OTHER four kinds the re-arm family journals `spec_file` on. Routing is
    # by field NAME, so these ride the same `_JOURNAL_ALIAS_FIELDS` entry as
    # `rearm-baseline-restamped` and are correct today for free — which is exactly why
    # they belong in the sweep: the canary is what catches a field added to one of
    # these kinds later, and a sweep that covers two of five grades the routing of a
    # record shape nobody re-checks.
    #
    # `rearm-aborted` is the fifth and the one written by a DIFFERENT function
    # (`runs._rollback_rearm`, from the transaction guard's error path) rather than by
    # `rearm_escalation` itself — the divergence that made the routing entry's own
    # producer note undercount. It carries two fields the others do not: `error`, which
    # the free-text drop set reaches, and `rollback`, a literal enum string that is
    # declared benign rather than routed and must therefore still ship VERBATIM.
    siblings = [
        diagnostics._scrub_entry(
            {"ts": 3.0, "kind": kind, "story_key": STORY_KEY, "spec_file": SPEC_ABS, **extra},
            pseudo,
            {},
            1.0,
        )
        for kind, extra in (
            ("rearm-spec-write-unreachable", {"target_branch": REARM_BRANCH}),
            ("rearm-spec-flip-skipped", {"status": "ready-for-dev"}),
            ("rearm-baseline-restamp-skipped", {"baseline": SHA}),
            (
                "rearm-aborted",
                {"error": f"OSError: cannot write {HOME_PATH}/spec.md", "rollback": "restored"},
            ),
        )
    ]
    # every one of them aliases to the SAME alias as the restamped record above: one
    # spec, one alias, however many kinds carry it
    assert [s["spec_file"] for s in siblings] == [alias, alias, alias, alias]
    # the abort record's own two fields: the free-text one is dropped (it quotes a host
    # path back), the enum one is deliberately NOT aliased — both surfaces read the
    # record for `rollback`, so pseudonymizing it would destroy the field's whole point
    assert "error" not in siblings[3] and siblings[3]["error_present"] is True
    assert siblings[3]["rollback"] == "restored"
    assert [orig for ns, orig, _a in pseudo.entries() if ns == "spec"] == [SPEC_NAME]

    # `rearm-spec-write-unreachable` names the branch the re-drive cuts its replacement
    # worktree from, so the operator is told WHERE to commit. It is journalled as
    # `target_branch` rather than a fresh spelling for exactly this reason: routing is
    # by field NAME, and that name is already in `_JOURNAL_ALIAS_FIELDS` under the
    # `branch` namespace. Graded on a SEPARATOR-FREE name (see `REARM_BRANCH`) because
    # a slashed one dies at `scrub_json` and would pass with the routing deleted.
    #
    # In a real run `ensure_target_branch` journals the same string as `branch` first,
    # so an unrouted spelling would be caught by the egress backstop and disclosed as a
    # `backstop_repairs` gap — but a truncated journal missing that event has nothing to
    # repair from, and the branch would ship verbatim in a shareable bundle.
    branch_alias = next(
        a for ns, orig, a in pseudo.entries() if ns == "branch" and orig == REARM_BRANCH
    )
    assert siblings[0]["target_branch"] == branch_alias != REARM_BRANCH

    rendered = json.dumps([advance_failed, restamped, *siblings])
    for canary in (SHA, other_sha, SPEC_NAME, PROPRIETARY, HOME_PATH, REARM_BRANCH, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"


def test_the_two_commit_probe_records_alias_one_baseline_to_one_name():
    """`old_baseline` is a 40-hex sha on BOTH of `_stale_restore_residue`'s records,
    and routing is by field NAME, so one entry has to cover both kinds (DW-81).

    It was declared benign in `tests/test_portability_guard.py`'s inventory, which is
    the misfiling that inventory's own warning describes — "a name carrying a story
    key, a branch, a sha, a spec filename, a path, or free text belongs in a
    `diagnostics` table instead" — and the second producer is what forced it.

    The two kinds are graded together rather than one standing in for the other,
    because the value's whole use is a comparison an operator makes across them: the
    probe-failure record says "I could not tell you what sits above this sha" and the
    commits record says "these do". Aliasing one spelling and not the other would
    destroy that correlation. That is also why the producer was not respelled to the
    already-routed `baseline` — same sha, two spellings, two aliases in one dump.

    Ablation: drop `"old_baseline"` from `_JOURNAL_ALIAS_FIELDS` and the test dies at
    the `next(...)` alias lookup with `StopIteration`. The canary sweep is not the
    grade: depending on its entropy, the fallback may redact a sha as a secret rather
    than preserving the correlatable alias this table promises.

    `commits` remains deliberately outside this test and outside DW-81's routing
    change. It is a list on `stale-restore-commits` but an integer count on
    `rollback-manual-required`; routing it requires a separate, kind-scoped policy.
    """
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    probe_failed = diagnostics._scrub_entry(
        {
            "ts": 1.0,
            "kind": "rearm-commits-probe-failed",
            "story_key": STORY_KEY,
            "old_baseline": SHA,
            "error": f"GitError: git rev-list {SHA}..HEAD failed in {HOME_PATH}",
        },
        pseudo,
        {},
        1.0,
    )
    commits = diagnostics._scrub_entry(
        {
            "ts": 2.0,
            "kind": "stale-restore-commits",
            "story_key": STORY_KEY,
            "old_baseline": SHA,
            "commits": ["c" * 40],
        },
        pseudo,
        {},
        1.0,
    )

    alias = next(a for ns, orig, a in pseudo.entries() if ns == "commit" and orig == SHA)
    # aliased, not dropped — the key stays and only the VALUE is replaced
    assert probe_failed["old_baseline"] == commits["old_baseline"] == alias != SHA
    # one legend entry for the shared baseline, not one per record spelling
    assert {orig for ns, orig, _a in pseudo.entries() if ns == "commit"} == {SHA}
    # the free-text sibling on the probe record quotes both the sha and a host path
    # back, and is reached by the drop set rather than aliased
    assert "error" not in probe_failed and probe_failed["error_present"] is True

    rendered = json.dumps([probe_failed, commits])
    for canary in (SHA, PROPRIETARY, HOME_PATH, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"


def test_sentinel_upstream_record_drops_the_stories_root_it_names():
    """`rearm-upstream-write-unreachable` carries an absolute host path naming the
    folder a sentinel's upstream correction has to land in.

    Routed like `repo` and NOT like `spec_file`, and the two precedents genuinely
    disagree: a spec filename is the customer's feature name and correlates one spec
    across four kinds, so it is ALIASED. This is a DIRECTORY, journalled by one kind,
    and one run has one spec folder — it correlates nothing, and a `spec` alias would
    additionally be wrong, since that namespace reduces to a basename and every run we
    author would collapse onto the same `epic-*` tail.

    Graded on the field's ABSENCE, because the canary sweep below is a false green on
    its own: `_IDENTIFIER_RE` forbids `/`, so `scrub_json` already collapses any real
    path to `<redacted:str>` and the home path never appears whether the field is routed
    or not. That is precisely the argument `repo`'s own row makes, and the reason both
    are asserted the same way. `target_branch` beside it is the control: identifier-
    shaped by design, so it must come back ALIASED rather than dropped, and it does leak
    through the sweep when unrouted.

    Ablation: drop `stories_root` from `_JOURNAL_DROP_FIELDS` and the presence assertion
    reddens while the canary sweep stays green; drop `target_branch` from
    `_JOURNAL_ALIAS_FIELDS` and the branch assertions redden on BOTH.
    """
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    scrubbed = diagnostics._scrub_entry(
        {
            "ts": 2.0,
            "kind": "rearm-upstream-write-unreachable",
            "story_key": STORY_KEY,
            "stories_root": f"{HOME_PATH}/_bmad-output/epic-6",
            "target_branch": REARM_BRANCH,
        },
        pseudo,
        {},
        1.0,
    )

    assert "stories_root" not in scrubbed and scrubbed["stories_root_present"] is True
    branch_alias = next(
        a for ns, orig, a in pseudo.entries() if ns == "branch" and orig == REARM_BRANCH
    )
    assert scrubbed["target_branch"] == branch_alias != REARM_BRANCH
    # the folder never entered the legend either — dropped means dropped, not aliased
    assert not [orig for ns, orig, _a in pseudo.entries() if ns == "spec"]

    rendered = json.dumps(scrubbed)
    for canary in (HOME_PATH, REARM_BRANCH, PROPRIETARY, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"


_PATCH_PATH_ROUTING_ROWS = (
    (
        "stale-restore-excluded",
        "patch",
        f"{HOME_PATH}/artifacts/{SPEC_NAME}.patch",
    ),
    (
        "stale-restore-unparseable",
        "patch",
        f"{HOME_PATH}/artifacts/{SPEC_NAME}.patch",
    ),
    ("attempt-restored", "patch", "attempt.patch"),
    ("attempt-restore-failed", "patch", f"{HOME_PATH}/artifacts/attempt.patch"),
    (
        "unit-closed",
        "patch",
        f"{HOME_PATH}/.bmad-loop/runs/r1/failed/{STORY_KEY}/changes.patch",
    ),
    (
        "deferred-artifacts-stashed",
        "stashed_to",
        f"{HOME_PATH}/.bmad-loop/runs/r1/deferred/{STORY_KEY}/{SPEC_NAME}",
    ),
)


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    _PATCH_PATH_ROUTING_ROWS,
    ids=[row[0] for row in _PATCH_PATH_ROUTING_ROWS],
)
def test_patch_and_stash_path_fields_are_dropped_at_the_routing_seam(kind, field, value):
    """Every current producer is routed by field name, including a retained
    unit's full forensic path and a bare operator latch.

    Ablation: remove either field from ``_JOURNAL_DROP_FIELDS`` and its rows fail
    the structural absence/presence assertions even when path-shaped canaries stay green.
    """
    control_story = "1.2-ControlStory"
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    scrubbed = diagnostics._scrub_entry(
        {"ts": 2.0, "kind": kind, "story_key": control_story, field: value},
        pseudo,
        {},
        1.0,
    )

    assert field not in scrubbed
    assert scrubbed[f"{field}_present"] is True
    story_alias = next(
        a for ns, orig, a in pseudo.entries() if ns == "story" and orig == control_story
    )
    assert scrubbed["story_key"] == story_alias
    assert scrubbed["story_key"] != control_story
    entries = pseudo.entries()
    assert not [orig for ns, orig, _alias in entries if ns == "spec"]
    legend_values = {orig for _ns, orig, _alias in entries}
    assert value not in legend_values
    assert Path(value).name not in legend_values
    rendered = json.dumps(scrubbed)
    for canary in (value, HOME_PATH, SPEC_NAME, PROPRIETARY, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"
    legend = json.dumps(pseudo.legend())
    for canary in (value, HOME_PATH, SPEC_NAME, PROPRIETARY, *CANARIES):
        assert canary not in legend, f"LEAK via legend: {canary!r}"


def test_patch_and_stash_paths_are_absent_from_public_diagnostic_renders(project):
    """Journal records flow through collect and both public renderers.

    Ablation: remove either DROP route and the decoded JSON entry retains the
    source field, so this fails even though the absolute-path leak sweep stays green.
    """
    run_dir = _seed_run(project.project)
    patch_path = f"{HOME_PATH}/artifacts/patch-{SPEC_NAME}.patch"
    stash_path = f"{HOME_PATH}/.bmad-loop/runs/r1/deferred/{STORY_KEY}/stash-{SPEC_NAME}"
    journal = Journal(run_dir)
    journal.append(
        "stale-restore-excluded",
        story_key=STORY_KEY,
        patch=patch_path,
        files=["newfile.txt"],
    )
    journal.append(
        "deferred-artifacts-stashed",
        story_key=STORY_KEY,
        stashed_to=stash_path,
    )

    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    diag = diagnostics.collect([run_dir], pseudo=pseudo, project=project.project)
    markdown = diagnostics.render_markdown(diag, pseudo=pseudo)
    json_text = diagnostics.render_json(diag, pseudo=pseudo)
    document = json.loads(json_text)
    entries = document["runs"][0]["journal"]["entries"]
    excluded = next(entry for entry in entries if entry["kind"] == "stale-restore-excluded")
    stashed = next(entry for entry in entries if entry["kind"] == "deferred-artifacts-stashed")

    assert "patch" not in excluded
    assert excluded["patch_present"] is True
    assert "stashed_to" not in stashed
    assert stashed["stashed_to_present"] is True
    story_alias = next(a for ns, orig, a in pseudo.entries() if ns == "story" and orig == STORY_KEY)
    assert excluded["story_key"] == story_alias
    assert stashed["story_key"] == story_alias
    assert story_alias in markdown
    rendered = markdown + json_text
    for canary in (patch_path, stash_path, HOME_PATH, SPEC_NAME, PROPRIETARY, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"
    spec_legend_values = {orig for ns, orig, _alias in pseudo.entries() if ns == "spec"}
    assert spec_legend_values == {SPEC_NAME}
    legend_values = set(pseudo.legend().values())
    for dropped in (patch_path, Path(patch_path).name, stash_path, Path(stash_path).name):
        assert dropped not in legend_values


def test_target_field_routes_by_kind_because_it_carries_two_kinds_of_value():
    """`target` is a BRANCH on the merge kinds and a sprint STATUS on `board-advance-*`.

    That overload is why the field is absent from `_JOURNAL_ALIAS_FIELDS`: routing there
    is by field NAME, so a single entry would have to be wrong for one of the two
    families. Leaving it unrouted was the wrong half to be wrong on — a bare
    `main`/`release`-style branch name is identifier-shaped, so `scrub_json` ships it
    VERBATIM into a bundle whose guiding assumption is that it will be posted publicly.

    The backstop is not the answer here. It repairs only values already in the legend,
    so it rescues this exactly when `ensure_target_branch` journalled the same string as
    `branch` earlier in the same file — and then discloses a `backstop_repairs` gap on a
    routine run. A journal truncated past that event has nothing to repair from.

    Both directions are graded, because either alone is satisfied by a wrong fix:

    - the three merge kinds ALIAS, to the branch namespace, and to the SAME alias as the
      `branch` field beside them when the value matches — a by-name entry would pass
      this too.
    - `board-advance-carried` keeps its `target` VERBATIM. A by-name entry reddens here,
      rendering the sprint status a maintainer reads that kind for as `branch-<hex>`.

    Separator-free names throughout: `_IDENTIFIER_RE` forbids `/`, so a `feature/x`
    spelling is collapsed by the FALLBACK and every assertion below would pass with the
    routing deleted (the same false green documented for `repo` above).

    Ablation: empty `_JOURNAL_KIND_ALIAS_FIELDS` and the three merge rows redden on the
    legend lookup (`StopIteration`); move `"target": "branch"` into
    `_JOURNAL_ALIAS_FIELDS` instead and the board-advance row reddens on the status.
    """
    target_branch = "AcmeSecretIntegration"
    unit_branch = "AcmeSecretUnit"
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    merges = [
        diagnostics._scrub_entry(
            {
                "ts": 1.0,
                "kind": kind,
                "story_key": STORY_KEY,
                "branch": unit_branch,
                "target": target_branch,
            },
            pseudo,
            {},
            1.0,
        )
        for kind in ("unit-merge-started", "unit-merged", "resume-unit-merge")
    ]
    board = diagnostics._scrub_entry(
        {
            "ts": 2.0,
            "kind": "board-advance-carried",
            "story_key": STORY_KEY,
            "target": "done",
            "status": "done",
        },
        pseudo,
        {},
        1.0,
    )

    target_alias = next(
        a for ns, orig, a in pseudo.entries() if ns == "branch" and orig == target_branch
    )
    unit_alias = next(
        a for ns, orig, a in pseudo.entries() if ns == "branch" and orig == unit_branch
    )
    # every merge kind aliases the same target to the same alias — one branch, one alias,
    # however many kinds name it
    assert [m["target"] for m in merges] == [target_alias] * 3
    # ...and the unit branch beside it stays DISTINGUISHABLE, so a maintainer can still
    # read "this branch merged into that one" off the scrubbed record
    assert [m["branch"] for m in merges] == [unit_alias] * 3
    assert target_alias != unit_alias

    # the other family keeps the same field verbatim: it is a sprint status, and
    # aliasing it would destroy the only thing the record is read for
    assert board["target"] == "done" and board["status"] == "done"
    assert not [orig for ns, orig, _a in pseudo.entries() if ns == "branch" and orig == "done"]

    rendered = json.dumps([*merges, board])
    for canary in (target_branch, unit_branch, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"


def test_stranded_bundle_story_keys_are_aliased_element_wise():
    """`sweep-inflight-stranded` carries a LIST of story keys, and a list of
    identifier-shaped strings is the one shape `scrub_json` passes through
    untouched — `scrub_json(["1-1-acme-auth"]) == ["1-1-acme-auth"]`, verbatim.

    So the plural field needs the same routing as the singular `story_key` beside
    it, which was already aliased: `_JOURNAL_KEYLIST_FIELDS` reduces a list
    element-wise, and the namespace selection has to send this one to `story` (not
    to `dw`, which is only `dw_ids`) or one dump would carry two different aliases
    for the same story. The epic lookup rides along, exactly as it does for `keys`.

    Ablation: drop `story_keys` from `_JOURNAL_KEYLIST_FIELDS` and the alias
    assertions redden with the raw keys coming back verbatim; flip the namespace
    selection back to `"story" if k == "keys" else "dw"` and the cross-field
    identity assertion reddens (a `dw-` alias for a story key).
    """
    other_key = "3.4-AcmeVaultRotation"
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    scrubbed = diagnostics._scrub_entry(
        {
            "ts": 2.0,
            "kind": "sweep-inflight-stranded",
            "story_keys": [STORY_KEY, other_key],
        },
        pseudo,
        {STORY_KEY: 1, other_key: 3},
        1.0,
    )
    # the SAME story, journalled singular by a neighbouring record, must resolve to
    # the same alias — that identity is the whole reason this is aliased not dropped
    singular = diagnostics._scrub_entry(
        {"ts": 3.0, "kind": "sweep-bundle-recovered", "story_key": STORY_KEY},
        pseudo,
        {STORY_KEY: 1},
        1.0,
    )

    assert scrubbed["story_keys"] == [singular["story_key"], scrubbed["story_keys"][1]]
    assert STORY_KEY not in scrubbed["story_keys"]
    assert other_key not in scrubbed["story_keys"]
    assert scrubbed["story_keys"][0] != scrubbed["story_keys"][1]
    # the epic lookup still applies: `Pseudonymizer.alias` prefixes a story alias
    # with `s<epic>`, so each element carries the epic it was looked up under —
    # drop the `epic=` argument from the keylist branch and both prefixes become a
    # bare `story-`
    assert [a.split("-")[0] for a in scrubbed["story_keys"]] == ["s1", "s3"]
    # …and nothing landed in the deferred-work namespace, which is where the old
    # `"story" if k == "keys" else "dw"` selection would have put both of them
    assert not [orig for ns, orig, _a in pseudo.entries() if ns == "dw"]

    rendered = json.dumps([scrubbed, singular])
    for canary in (STORY_KEY, other_key, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"


def test_scalar_story_keys_fails_closed_instead_of_shipping_verbatim():
    """`_JOURNAL_KEYLIST_FIELDS` routing was gated on `isinstance(v, list)`, so a
    SCALAR value on one of those names fell straight through to `scrub_json` — which
    is the identity on an identifier-shaped string. `story_keys="1-1-acme-auth"` came
    back verbatim.

    Every producer passes a list today, so this is latent rather than live. That is
    the argument FOR closing it rather than against: the routing decision would
    otherwise rest on a survey of producers staying true, and the neighbouring
    `story_keys` row above is graded on lists only, so nothing would notice.

    Asserted on the raw value's ABSENCE, not on the presence key alone — a
    presence-key assertion passes for every reason a value could be missing,
    including the field never having been read.

    Ablation: restore the `and isinstance(v, list)` gate on the `elif` and the
    absence assertions redden with the raw key coming back."""
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    scrubbed = diagnostics._scrub_entry(
        {"ts": 2.0, "kind": "sweep-inflight-stranded", "story_keys": STORY_KEY},
        pseudo,
        {STORY_KEY: 1},
        1.0,
    )

    assert "story_keys" not in scrubbed, "the unknown-shaped value survived under its own name"
    assert scrubbed["story_keys_present"] is True
    rendered = json.dumps(scrubbed)
    assert STORY_KEY not in rendered
    for canary in CANARIES:
        assert canary not in rendered, f"LEAK: {canary!r}"


def test_off_schema_preference_escalation_keys_are_collapsed_to_presence():
    """`engine._review_and_commit` splats `escalation.preference_escalations(rj)`
    into `journal.append`, and those entries come out of a session's own
    `result.json` — so an LLM chooses the journal FIELD NAMES. No by-name table can
    route a name nobody can enumerate, and `scrub_json` is the identity on an
    identifier-shaped scalar, so `customer="AcmeVault"` shipped byte-identical into
    a dump whose module docstring ends "the dump will be posted publicly".

    `_JOURNAL_KIND_SCHEMAS` declares the record to be `{type, severity, detail}` and
    collapses everything else on that kind. Both halves are graded here: the
    off-schema value must be GONE, and the declared fields must NOT be — a policy
    that flattened the whole record would pass an absence-only assertion while
    destroying the field the record is read for.

    ACCEPTED RESIDUAL, asserted so it stays honest rather than drifting: the key
    NAME still reaches the dump as `<name>_present`. That was decided on 2026-08-30
    over a name-free `unrouted_field_count` collapse; this row PINS it, so a future
    reader finds it recorded as a decision rather than re-discovering it as a bug.

    Ablation: drop the `preference-escalation` row from `_JOURNAL_KIND_SCHEMAS` and
    the `AcmeVault` absence assertions redden."""
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    scrubbed = diagnostics._scrub_entry(
        {
            "ts": 2.0,
            "kind": "preference-escalation",
            "story_key": STORY_KEY,
            "type": "preference",
            "severity": "MEDIUM",
            "detail": "CANARY_ESCALATION prose about " + PROPRIETARY,
            "customer": "AcmeVault",
        },
        pseudo,
        {STORY_KEY: 1},
        1.0,
    )

    # the off-schema key: collapsed, and its VALUE gone from the entry entirely
    assert "customer" not in scrubbed
    assert scrubbed["customer_present"] is True
    assert "AcmeVault" not in json.dumps(scrubbed), "LEAK: off-schema preference value"
    # the declared schema is NOT collapsed — these are why the record is read
    assert scrubbed["type"] == "preference"
    assert scrubbed["severity"] == "MEDIUM"
    # `detail` is in the schema but `_JOURNAL_DROP_FIELDS` reaches it first, which is
    # the intended precedence: a stricter table always wins over this one
    assert "detail" not in scrubbed and scrubbed["detail_present"] is True
    # the entry stays correlatable — the kind policy replaces the FALLBACK only, so
    # every routing rule above it still runs
    assert scrubbed["story_key"] != STORY_KEY and scrubbed["story_key"].startswith("s1-")

    rendered = json.dumps(scrubbed)
    for canary in (STORY_KEY, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"


def test_self_minted_fields_survive_a_declared_schema_kind():
    """`Journal.append` stamps `log_task`/`log_pos` onto EVERY entry with
    `setdefault`, including one whose kind carries a declared schema. They are
    engine-authored, not LLM-authored, so the fail-closed arm must not touch them.

    It did: `log_pos` is outside `{type, severity, detail}`, so a real
    `preference-escalation` rendered `log_pos_present: true` and the pane-log byte
    offset was gone — on exactly the records an operator opens a dump to trace.
    `log_task` was never affected, since aliasing reaches it first; `log_pos` was the
    only casualty, which is why a test naming the pair would have stayed green.

    The exemption reads `journal.SELF_MINTED_FIELDS` rather than restating the pair,
    so it cannot drift from the `setdefault` calls that create the fields.

    Ablation: drop the `k not in SELF_MINTED_FIELDS` clause from `_scrub_entry`'s
    fail-closed arm and the integer assertion reddens with `log_pos_present`."""
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    scrubbed = diagnostics._scrub_entry(
        {
            "ts": 2.0,
            "kind": "preference-escalation",
            "log_task": STORY_KEY,
            "log_pos": 4096,
            "type": "preference",
            "customer": "AcmeVault",
        },
        pseudo,
        {STORY_KEY: 1},
        1.0,
    )

    # the byte offset survives as an INTEGER — the whole point of the exemption
    assert scrubbed["log_pos"] == 4096
    assert "log_pos_present" not in scrubbed
    # the pane-log task pointer is still aliased, not dropped and not raw
    assert scrubbed["log_task"] != STORY_KEY and scrubbed["log_task"].startswith("s1-")
    # ...while the LLM-authored key on the same record is still collapsed, so the
    # exemption did not widen into a general escape from the fail-closed arm
    assert "customer" not in scrubbed and scrubbed["customer_present"] is True

    rendered = json.dumps(scrubbed)
    for canary in (STORY_KEY, *CANARIES):
        assert canary not in rendered, f"LEAK: {canary!r}"


def test_decision_pending_question_is_dropped_not_scrubbed():
    """`sweep.py` journals `question=decision.question` on `decision-pending`, and it
    was declared benign. A MULTI-WORD question does collapse — but only by accident
    of `_IDENTIFIER_RE` forbidding spaces, which is not a property to route on. A
    ONE-TOKEN question is identifier-shaped and shipped verbatim.

    So it joins `detail`/`reason`/`blocker`/`suggestion`/`note` in
    `_JOURNAL_DROP_FIELDS`, under the same free-text rule. No user-facing surface
    loses the text: `tui/data.py`'s `decision_pending` reads the RAW journal on the
    operator's own machine, not this dump.

    The one-token case is the load-bearing one — grade the multi-word case alone and
    the row stays green with the routing deleted, because the fallback happens to
    redact it.

    Ablation: drop `question` from `_JOURNAL_DROP_FIELDS` and the one-token absence
    assertion reddens (the multi-word one does not — which is the point)."""
    pseudo = sanitize.Pseudonymizer(salt=b"fixed")
    one_token = diagnostics._scrub_entry(
        {"ts": 2.0, "kind": "decision-pending", "dw_id": "DW-7", "question": "AcmeVault"},
        pseudo,
        {},
        1.0,
    )

    assert "question" not in one_token
    assert one_token["question_present"] is True
    assert "AcmeVault" not in json.dumps(one_token), "LEAK: one-token decision question"
    # the dw id beside it is untouched — it is the record's correlation handle
    assert one_token["dw_id"] == "DW-7"

    # an unset question still reports as absent rather than as set
    empty = diagnostics._scrub_entry(
        {"ts": 2.0, "kind": "decision-pending", "dw_id": "DW-7", "question": ""},
        pseudo,
        {},
        1.0,
    )
    assert empty["question_present"] is False

    rendered = json.dumps([one_token, empty])
    for canary in CANARIES:
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
    # ONE group, summed — the schema-versioned payload shape does not split.
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


def test_diag_surfaces_the_split_code_root_and_the_task_generation(project):
    """The two state fields this wave ADDED must be legible in a bug report.

    `RunState.repo_root` and `StoryTask.generation` are new here, and both name the
    exact conditions under which the re-anchored gates and the re-minted session ids
    behave differently — yet neither reached the projection. `repo` was simultaneously
    dropped from journal records (correctly: an absolute host path), which removed the
    last trace of a split root from a dump altogether.

    `generation` matters because without it a #705-class replay dumps as
    `rearmed=True, attempt=1, n_sessions=2` — byte-identical to a HEALTHY post-re-arm
    task, so the one field that separates "minted a fresh id" from "collided with the
    abandoned record" is the one a triager cannot see.

    Both are privacy-safe by construction and are asserted to be: a boolean in the
    `paused_reason_present` / `worktree_isolated` style, and a small counter. The path
    itself must NOT appear — that is what `_JOURNAL_DROP_FIELDS` drops.

    `escalations_resolved_upto` (DW-11) is projected on the same warrant and asserted
    here for the same reason: a task whose older escalations are filtered out of
    `context.json` dumps identically to one that only ever raised the entries shown,
    so a support bundle cannot explain a short resolve context without it. A counter
    too — it indexes `task.sessions`, so it carries no customer content.

    Ablation: delete `repo_root_diverges=` from `collect_run` (or `generation=` /
    `escalations_resolved_upto=` from `_task_diag`) and this reddens on the
    corresponding assertion; deleting the field from the dataclass reddens as a
    TypeError at construction.
    """
    run_dir = _seed_run(project.project)
    state = load_state(run_dir)
    state.repo_root = str(project.project / "code-tree")
    state.tasks[STORY_KEY].generation = 2
    state.tasks[STORY_KEY].escalations_resolved_upto = 3
    save_state(run_dir, state)

    diag, _pseudo, combined = _render_all([run_dir])
    (run,) = diag.runs

    assert run.repo_root_diverges is True
    assert run.tasks[0].generation == 2
    assert run.tasks[0].escalations_resolved_upto == 3
    # a presence flag, never the path — the same rule `repo` is dropped under
    assert "code-tree" not in combined


def test_diag_repo_root_diverges_is_false_for_the_ordinary_layout(project):
    """The flag distinguishes; it is not simply always on.

    Without this the assertion above passes for a hardcoded `True`, and the field
    stops carrying the one bit it exists to carry.
    """
    run_dir = _seed_run(project.project)
    diag, _pseudo, _combined = _render_all([run_dir])
    (run,) = diag.runs

    assert run.repo_root_diverges is False
    assert run.tasks[0].generation == 0
    assert run.tasks[0].escalations_resolved_upto == 0


def _md_task_row(md: str) -> list[str]:
    """The one task row of the report's task table, split into its cells."""
    (row,) = [ln for ln in md.splitlines() if ln.startswith("| `")]
    return [c.strip() for c in row.strip("|").split("|")]


def test_the_markdown_report_carries_the_split_root_and_the_generation(project):
    """Both fields must reach the report `diagnose` emits BY DEFAULT, not only `--json`.

    The sibling tests above grade the collector, which is what `--json` dumps whole via
    `asdict`. The markdown report is a different renderer that samples fields by hand,
    and it is the artifact an operator actually produces and hands a maintainer — the
    header says so ("Safe to share"). A field whose entire warrant is "a bug report that
    cannot show this cannot be triaged" is not delivered until it renders here, so the
    warrant is graded where it is spent.

    `generation` rides beside `attempt` because that is the column pair a #705-class
    replay turns on: a collided re-drive and a healthy post-re-arm task agree on every
    other cell in this row. DW-11's `escalations_resolved_upto` rides beside it on the
    same warrant, stated verbatim in its own field comment: it is the only field that
    separates "this story raised one escalation" from "its earlier ones are filtered
    out of `context.json` as already answered", and that question is asked of a bug
    report. Seeded to a value that is neither the attempt, the generation nor the
    review cycle, so a cell reading a NEIGHBOUR cannot pass.

    Ablation: drop the `code root differs from project` line from `render_markdown` and
    both this test and the sibling below redden on their first assertion. Drop
    `{t.generation}` from the row f-string together with its header and separator cells
    and this test reddens at `names[4]` (`"esc-upto" != "gen"`) while the sibling
    reddens at the row cell — the review cycle shifted left rather than a missing key,
    which is why the cell is read positionally and the three widths are compared. Drop
    `{t.escalations_resolved_upto}` the same way and this test reddens at `names[5]`
    (`"rev" != "esc-upto"`); drop ONLY the row cell and it reddens on the width
    comparison, which is what a skewed table actually looks like.
    """
    run_dir = _seed_run(project.project)
    state = load_state(run_dir)
    state.repo_root = str(project.project / "code-tree")
    state.tasks[STORY_KEY].generation = 2
    state.tasks[STORY_KEY].escalations_resolved_upto = 3
    save_state(run_dir, state)

    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect([run_dir], pseudo=pseudo, project=ANY_PROJECT)
    md = diagnostics.render_markdown(diag, pseudo=pseudo)

    assert "- **code root differs from project:** yes" in md
    cells = _md_task_row(md)
    (header,) = [ln for ln in md.splitlines() if ln.startswith("| alias |")]
    (rule,) = [ln for ln in md.splitlines() if ln.startswith("|---|")]
    names = [c.strip() for c in header.strip("|").split("|")]
    assert names[4] == "gen"
    assert names[5] == "esc-upto"
    # header, separator and row must agree on width or the table renders skewed
    assert len(cells) == len(names) == len(rule.strip("|").split("|")) == 13
    assert cells[3] == "2"  # attempt, seeded by `_seed_run`
    assert cells[4] == "2"  # generation — NOT the review cycle, which is 1
    assert cells[5] == "3"  # the DW-11 watermark, in its own column
    assert cells[6] == "1"  # review cycle, still in its own column
    # still a flag and a counter: the path itself never renders
    assert "code-tree" not in md


def test_the_markdown_report_says_no_for_the_ordinary_layout(project):
    """The rendered line distinguishes; a hardcoded "yes" would pass the test above."""
    run_dir = _seed_run(project.project)
    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect([run_dir], pseudo=pseudo, project=ANY_PROJECT)
    md = diagnostics.render_markdown(diag, pseudo=pseudo)

    assert "- **code root differs from project:** no" in md
    assert _md_task_row(md)[4] == "0"
