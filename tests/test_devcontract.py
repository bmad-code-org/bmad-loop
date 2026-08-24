"""Tests for the generic bmad-dev-auto -> result.json translation shim."""

import os
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from bmad_loop import devcontract, platform_util, verify


def _spec(
    path: Path,
    *,
    status: str = "done",
    baseline_field: str = "baseline_revision",
    baseline: str = "abc123def456abc123def456abc123def456abcd",
    auto_run: str | None = "done",
    body_extra: str = "",
    followup: bool | None = None,
    actions: str | None = None,
) -> Path:
    fm = f"---\ntitle: 'x'\ntype: 'feature'\nstatus: '{status}'\n"
    if baseline:
        fm += f"{baseline_field}: '{baseline}'\n"
    if followup is not None:
        fm += f"followup_review_recommended: {str(followup).lower()}\n"
    if actions is not None:
        # raw YAML: these tests are ABOUT the container/item shapes, so the
        # fixture must be able to write a malformed one verbatim
        fm += f"operator_actions: {actions}\n"
    fm += "---\n\n## Intent\n\nwhatever\n"
    text = fm + body_extra
    if auto_run is not None:
        text += f"\n## Auto Run Result\n\n- Status: {auto_run}\n- did the thing\n"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------- parse section


def test_parse_absent():
    arr = devcontract.parse_auto_run_result("# spec\n\nno result here\n")
    assert not arr.present and arr.status == ""


def test_parse_bulleted_status():
    arr = devcontract.parse_auto_run_result("## Auto Run Result\n\n- Status: done\n- summary\n")
    assert arr.present and arr.status == "done" and "summary" in arr.detail


def test_parse_bold_status():
    arr = devcontract.parse_auto_run_result("## Auto Run Result\n\n**Status:** blocked\n\nreason\n")
    assert arr.status == "blocked"


def test_parse_last_section_wins():
    text = (
        "## Auto Run Result\n\nStatus: blocked\n\n"
        "## Spec Change Log\n\nx\n\n"
        "## Auto Run Result\n\nStatus: done\n"
    )
    arr = devcontract.parse_auto_run_result(text)
    assert arr.status == "done"


def test_parse_stops_at_next_heading():
    text = "## Auto Run Result\n\nStatus: done\n\n## Notes\n\nStatus: blocked\n"
    arr = devcontract.parse_auto_run_result(text)
    assert arr.status == "done" and "blocked" not in arr.detail


def test_parse_stops_at_bare_empty_heading():
    """Reviewer guard (#53, comment 3522512350): a bare `##` line is a valid empty
    CommonMark ATX heading, so `_next_heading_start` (`^##\\s`) bounding the section
    there is correct, not a premature truncation. Locks that intent: the `Status:`
    gate is parsed above the boundary and is unaffected, and tightening `\\s` to a
    space/tab delimiter would stop recognizing empty headings — a false-negative
    boundary that, on the destructive strip path, deletes MORE, not less."""
    text = "## Auto Run Result\n\nStatus: done\n\n##\n\nlater section body\n"
    arr = devcontract.parse_auto_run_result(text)
    assert arr.status == "done"
    assert "later section body" not in arr.detail


def test_parse_ignores_fence_quoted_heading():
    """A heading quoted inside a fenced example (a frozen intent showing the
    section format) is documentation, not a terminal section (#52)."""
    text = "## Intent\n\n```md\n## Auto Run Result\n\nStatus: done\n```\n\nbody\n"
    arr = devcontract.parse_auto_run_result(text)
    assert not arr.present and arr.status == ""


def test_parse_real_section_wins_over_later_fenced_example():
    """A fenced copy of the heading inside the real section's detail must not
    displace it as the 'last' section — the real outcome stays authoritative."""
    text = (
        "## Auto Run Result\n\nStatus: done\n\n"
        "the format appended was:\n\n```md\n## Auto Run Result\n\nStatus: blocked\n```\n"
    )
    arr = devcontract.parse_auto_run_result(text)
    assert arr.status == "done"


def test_parse_detail_spans_fenced_heading_line():
    """Column-0 `## ` lines inside a fenced block within the section (quoted
    shell comments, log output) are content, not the next-section boundary —
    the detail must not truncate there (#52)."""
    text = "## Auto Run Result\n\nStatus: done\n\n```sh\n## run tests\npytest -q\n```\n\ntrailing\n"
    arr = devcontract.parse_auto_run_result(text)
    assert arr.status == "done"
    assert "pytest -q" in arr.detail and "trailing" in arr.detail


def test_parse_ignores_heading_in_longer_outer_fence():
    """A shorter ``` line inside a longer ```` fence does NOT close it
    (CommonMark), so a `## Auto Run Result` after that inner line is still fenced
    documentation. A bare line-parity count would flip on the inner ``` and wrongly
    expose the heading as a real, terminal section."""
    text = (
        "## Intent\n\n"
        "````\n"  # open a 4-backtick fence
        "```\n"  # lone 3-backtick line — literal content, not a close
        "## Auto Run Result\n\nStatus: done\n"
        "````\n\nbody\n"  # the real close
    )
    arr = devcontract.parse_auto_run_result(text)
    assert not arr.present and arr.status == ""


def test_parse_ignores_heading_in_mismatched_fence_char():
    """A ``` line inside a ~~~ fence is content (a different fence char cannot
    close), so a `## Auto Run Result` after it stays fenced."""
    text = "## Intent\n\n" "~~~\n" "```\n" "## Auto Run Result\n\nStatus: done\n" "~~~\n\nbody\n"
    arr = devcontract.parse_auto_run_result(text)
    assert not arr.present and arr.status == ""


def test_parse_recognizes_real_heading_after_closed_longer_fence():
    """Positive control: after a properly-closed 4-backtick fence, a real
    `## Auto Run Result` IS recognized — the tracker must actually close, not
    over-correct into a fence that never ends."""
    text = "## Intent\n\n````\ncode\n````\n\n## Auto Run Result\n\nStatus: done\n"
    arr = devcontract.parse_auto_run_result(text)
    assert arr.present and arr.status == "done"


# ------------------------------------------------------------- synthesize_result


def test_synth_success_maps_baseline_revision(tmp_path):
    sp = _spec(tmp_path / "spec-1-1-a.md", status="done", auto_run="done")
    out = devcontract.synthesize_result(sp, story_key="1-1-a")
    assert out.status_consistent
    rj = out.result_json
    assert rj["workflow"] == "auto-dev"
    assert rj["status"] == "done"
    assert rj["spec_file"] == str(sp)
    assert rj["baseline_commit"] == "abc123def456abc123def456abc123def456abcd"
    assert rj["escalations"] == []
    assert "dw_ids" not in rj


def test_synth_blocked_frontmatter_becomes_critical(tmp_path):
    sp = _spec(tmp_path / "s.md", status="blocked", auto_run="blocked")
    out = devcontract.synthesize_result(sp, story_key="1-1-a")
    crits = out.result_json["escalations"]
    assert len(crits) == 1 and crits[0]["severity"] == "CRITICAL"
    assert crits[0]["type"] == "blocked"


def test_synth_blocked_prose_only_still_escalates(tmp_path):
    # frontmatter not yet flipped, but the prose says blocked: still PAUSE-worthy
    sp = _spec(tmp_path / "s.md", status="in-progress", auto_run="blocked")
    out = devcontract.synthesize_result(sp, story_key="1-1-a")
    assert any(e["severity"] == "CRITICAL" for e in out.result_json["escalations"])


def test_synth_not_terminal_returns_none(tmp_path):
    sp = _spec(tmp_path / "s.md", status="in-progress", auto_run=None)
    out = devcontract.synthesize_result(sp, story_key="1-1-a")
    assert out.result_json is None


def test_synth_status_inconsistent_flagged(tmp_path):
    # frontmatter done, prose says blocked -> caller must fail safe
    sp = _spec(tmp_path / "s.md", status="done", auto_run="blocked")
    out = devcontract.synthesize_result(sp, story_key="1-1-a")
    assert out.status_consistent is False


def test_synth_blank_frontmatter_status_falls_back_to_prose_done(tmp_path):
    """A present-but-blank `status:` (YAML null — the shape a bmad-dev-auto template
    leaves, per `_FM_STATUS_RE`'s own comment) must read as `""`, so the prose
    `done` fallback fires and the synthesis is self-consistent. A local
    `str(fm.get("status", ""))` made it the truthy token `"none"`, which pinned
    `status="none"` and `status_consistent=False` — and `status_consistent` is the
    gate on `_post_kill_reconcile`, whose docstring names this very shape as
    rescuable, so a session that finished real work was discarded (#369).

    Written verbatim rather than through `_spec`: that fixture quotes the value
    (`status: '{status}'`), which yields an empty *string*, not YAML-null, and so
    cannot express the failing shape at all.
    """
    sp = tmp_path / "spec-1-1-a.md"
    sp.write_text(
        "---\ntitle: 'x'\nstatus:\n---\n\n## Auto Run Result\n\n- Status: done\n",
        encoding="utf-8",
    )
    out = devcontract.synthesize_result(sp, story_key="1-1-a")

    assert out.result_json["status"] == "done"
    assert out.status_consistent is True


def test_synth_literal_none_status_still_blocks_the_prose_fallback(tmp_path):
    """The other half of the #369 fix: `status: none` written as a literal token is
    a deliberate custom status, not a blank. PyYAML resolves only `~`/`null`/
    `Null`/`NULL`/empty as null, so this stays the string `"none"` — truthy, so it
    wins over the prose and the disagreement is surfaced. Without this pin the fix
    could be "widened" into treating the token itself as blank."""
    sp = tmp_path / "spec-1-1-a.md"
    sp.write_text(
        "---\ntitle: 'x'\nstatus: none\n---\n\n## Auto Run Result\n\n- Status: done\n",
        encoding="utf-8",
    )
    out = devcontract.synthesize_result(sp, story_key="1-1-a")

    assert out.result_json["status"] == "none"
    assert out.status_consistent is False


def test_synth_dw_ids_included(tmp_path):
    sp = _spec(tmp_path / "spec-dw-x.md", status="done", auto_run="done")
    out = devcontract.synthesize_result(sp, story_key=None, dw_ids=["DW-1", "DW-2"])
    assert out.result_json["dw_ids"] == ["DW-1", "DW-2"]
    assert out.result_json["story_key"] is None


def test_synth_baseline_commit_field_also_accepted(tmp_path):
    sp = _spec(tmp_path / "s.md", baseline_field="baseline_commit", auto_run="done")
    out = devcontract.synthesize_result(sp, story_key="1-1-a")
    assert out.result_json["baseline_commit"].startswith("abc123")


def test_synth_followup_review_recommended_true(tmp_path):
    sp = _spec(tmp_path / "s.md", status="done", auto_run="done", followup=True)
    out = devcontract.synthesize_result(sp, story_key="1-1-a")
    assert out.result_json["followup_review_recommended"] is True


def test_synth_followup_review_recommended_defaults_false_on_done(tmp_path):
    # field absent on a done spec -> carried through as False, not omitted
    sp = _spec(tmp_path / "s.md", status="done", auto_run="done")
    out = devcontract.synthesize_result(sp, story_key="1-1-a")
    assert out.result_json["followup_review_recommended"] is False


def test_synth_followup_review_recommended_omitted_on_blocked(tmp_path):
    # the skill never recommends follow-up on a blocked exit; don't carry it
    sp = _spec(tmp_path / "s.md", status="blocked", auto_run="blocked", followup=True)
    out = devcontract.synthesize_result(sp, story_key="1-1-a")
    assert "followup_review_recommended" not in out.result_json


# ------------------------------------------- awaiting-operator terminal (#335)


def test_synth_awaiting_operator_is_terminal_and_folds_actions(tmp_path):
    """The park is a third terminal beside done/blocked, and its obligations
    travel with the result so the engine never re-reads the spec to learn them."""
    sp = _spec(
        tmp_path / "s.md",
        status="awaiting-operator",
        auto_run="awaiting-operator",
        actions="['buy example.com', 'publish the TXT record']",
    )
    out = devcontract.synthesize_result(sp, story_key="1-1-a")

    assert out.status_consistent
    rj = out.result_json
    assert rj is not None and rj["status"] == "awaiting-operator"
    assert rj["operator_actions"] == ["buy example.com", "publish the TXT record"]


def test_synth_awaiting_operator_synthesizes_no_escalation(tmp_path):
    """The distinction the whole state exists for: `blocked` means the session
    could not proceed and the run must halt; a park means it finished everything
    an agent could. A CRITICAL here would collapse the two into the pause
    channel. `followup_review_recommended` is likewise absent — a parked story
    does not route into the review loop at all."""
    sp = _spec(tmp_path / "s.md", status="awaiting-operator", auto_run=None, actions="['do it']")
    out = devcontract.synthesize_result(sp, story_key="1-1-a")

    assert out.result_json["escalations"] == []
    assert "followup_review_recommended" not in out.result_json


@pytest.mark.parametrize(
    "actions, expected, why",
    [
        ("['a', 'b']", ["a", "b"], "the normal shape"),
        ("[' a ', '', 'a']", ["a"], "blanks drop, duplicates collapse, order preserved"),
        ("[5]", ["5"], "a non-string scalar is str()-normalized, like closes_deferred ids"),
        ("buy the domain", [], "a bare string is the wrong CONTAINER, never one action"),
        ("[]", [], "an empty list declares nothing"),
        ("[{action: a, check: b}]", [], "the v2 object shape is refused, not str()-ed to junk"),
        ("[null]", [], "a null item is not the word 'None'"),
    ],
)
def test_synth_operator_actions_shapes(tmp_path, actions, expected, why):
    """Strict about the container, lenient about each scalar item — every
    malformed shape collapses to [], which verify's non-empty gate turns into one
    fixable retry naming the expected shape."""
    sp = _spec(tmp_path / "s.md", status="awaiting-operator", auto_run=None, actions=actions)
    out = devcontract.synthesize_result(sp, story_key="1-1-a")

    assert out.result_json["operator_actions"] == expected, why


@pytest.mark.parametrize("status", ["done", "blocked"])
def test_synth_operator_actions_folded_only_on_the_park_status(tmp_path, status):
    """An `operator_actions:` list left behind on a done or blocked spec is not a
    park. Carrying it would let a story register obligations no verify gate ever
    held it to — and, on `done`, would make _finalize_commit_phase's final-phase
    rule park a story the session declared finished."""
    sp = _spec(tmp_path / "s.md", status=status, auto_run=status, actions="['stale leftover']")
    out = devcontract.synthesize_result(sp, story_key="1-1-a")

    assert "operator_actions" not in out.result_json


def test_frontmatter_candidates_include_awaiting_operator(tmp_path):
    """The missing-marker fallback scan must find a parked spec too: the skill's
    marker append is intermittent on EVERY terminal path, and a park invisible to
    the harvest rides stall-nudges to timeout and loses its work."""
    sp = _spec(tmp_path / "spec-1-1-a.md", status="awaiting-operator", auto_run=None)

    assert devcontract.find_frontmatter_candidates(tmp_path, since_ns=0) == [sp]


# --------------------------------------------------- plan-halt expected-terminal


def test_synth_ready_for_dev_non_terminal_by_default(tmp_path):
    # Without the plan-halt directive, ready-for-dev is a died-mid-flight
    # non-terminal (still in RECONCILABLE_FROM) — nothing to translate yet.
    sp = _spec(tmp_path / "s.md", status="ready-for-dev", auto_run=None)
    out = devcontract.synthesize_result(sp, story_key="1")
    assert out.result_json is None and out.status_consistent


def test_synth_plan_halt_ready_for_dev_is_success_terminal(tmp_path):
    sp = _spec(tmp_path / "s.md", status="ready-for-dev", auto_run=None)
    out = devcontract.synthesize_result(sp, story_key="1", plan_halt=True)
    rj = out.result_json
    assert rj is not None and rj["status"] == "ready-for-dev"
    assert rj["plan_halt"] is True
    assert rj["escalations"] == []
    assert "followup_review_recommended" not in rj  # only carried on a done exit
    assert out.status_consistent


def test_synth_plan_halt_overrun_to_done_is_plain_done(tmp_path):
    # Plan-halt requested but the session ran on to done: treat as a normal done
    # (no plan_halt marker), carrying the followup flag as usual.
    sp = _spec(tmp_path / "s.md", status="done", auto_run="done", followup=True)
    out = devcontract.synthesize_result(sp, story_key="1", plan_halt=True)
    rj = out.result_json
    assert rj["status"] == "done" and "plan_halt" not in rj
    assert rj["followup_review_recommended"] is True


def test_synth_plan_halt_blocked_still_escalates(tmp_path):
    # A block during planning routes to PAUSE, not a plan-review pause — no marker.
    sp = _spec(tmp_path / "s.md", status="blocked", auto_run="blocked")
    out = devcontract.synthesize_result(sp, story_key="1", plan_halt=True)
    rj = out.result_json
    assert "plan_halt" not in rj
    assert any(e["severity"] == "CRITICAL" for e in rj["escalations"])


def test_plan_halt_composes_with_reconcile_guard(tmp_path):
    # The engine's _reconcile_generic_terminal_status only rewrites a spec whose
    # prose `## Auto Run Result` says done while the frontmatter lags. A plan-halt
    # ready-for-dev spec carries no such prose, so the reconcile guard no-ops and
    # this leg's ready-for-dev success outcome is never clobbered to done —
    # even though ready-for-dev is (for the died-mid-flight case) reconcilable-from.
    assert "ready-for-dev" in devcontract.RECONCILABLE_FROM
    sp = _spec(tmp_path / "s.md", status="ready-for-dev", auto_run=None)
    arr = devcontract.parse_auto_run_result(sp.read_text(encoding="utf-8"))
    reconcile_would_noop = not arr.present or arr.status != devcontract.DONE
    assert reconcile_would_noop


# ------------------------------------------------------------ find_result_artifact


def test_find_artifact_picks_newest_with_heading(tmp_path):
    old = _spec(tmp_path / "spec-old.md", auto_run="done")
    new = _spec(tmp_path / "spec-new.md", auto_run="blocked")
    import os

    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(new, ns=(2_000_000_000, 2_000_000_000))
    found = devcontract.find_result_artifact(tmp_path, since_ns=500_000_000)
    assert found == new


def test_find_artifact_respects_since_floor(tmp_path):
    old = _spec(tmp_path / "spec-old.md", auto_run="done")
    import os

    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    assert devcontract.find_result_artifact(tmp_path, since_ns=5_000_000_000) is None


def test_find_artifact_ignores_files_without_heading(tmp_path):
    (tmp_path / "plain.md").write_text("# nope\n", encoding="utf-8")
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) is None


def test_find_artifact_missing_dir(tmp_path):
    assert devcontract.find_result_artifact(tmp_path / "ghost", since_ns=0) is None


@pytest.mark.parametrize("prefix", ["bmad-build-auto-result-", "bmad-dev-auto-result-"])
def test_find_artifact_accepts_no_spec_fallback_prefix(tmp_path, prefix):
    # The no-spec fallback (intent too unclear to create a spec) carries a terminal
    # frontmatter status but NO `## Auto Run Result` heading — it is matched by its
    # `<skill>-result-` filename prefix instead. BOTH eras are matched: the artifact
    # is named after whichever skill wrote it (BMAD-METHOD #2651 renamed
    # bmad-dev-auto to bmad-build-auto), and a run can meet either.
    fallback = tmp_path / f"{prefix}unclear-1234.md"
    fallback.write_text(
        "---\nstatus: blocked\n---\n\nBlocking condition: unclear intent\n",
        encoding="utf-8",
    )
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) == fallback


def test_every_dev_primitive_has_a_fallback_result_prefix():
    """The name the orchestrator WRITES a completion marker under and the set of
    names it will MATCH are independent literals in two modules. Nothing derived
    one from the other until this test.

    The failure this prevents is silent and total: `engine` names the marker
    `f"{resolved_primitive}-result-{task_id}.md"`, and `find_result_artifact`
    only looks at names starting with one of these prefixes. A marker outside the
    set is not merely unmatched — it falls through to the `## Auto Run Result`
    heading branch, which the workflow-completion contract never writes, so the
    marker is invisible and every plugin workflow livelocks to
    `session_timeout_min` with no error anywhere.

    Subset, not equality, and the direction is the whole point: a primitive with
    no prefix is the unreadable-marker bug, while a prefix with no primitive is a
    RETIRED era deliberately kept matchable — the comment on
    `FALLBACK_RESULT_PREFIXES` asks for exactly that, so a resume can read an
    artifact written before an upstream upgrade.

    Reading the constants off the module rather than restating them is what makes
    this hold: a third `DEV_PRIMITIVE_*` name added tomorrow is enforced without
    anyone remembering this file exists."""
    from bmad_loop import install

    primitives = {
        v for n, v in vars(install).items() if n.startswith("DEV_PRIMITIVE_") and isinstance(v, str)
    }
    assert primitives, "no DEV_PRIMITIVE_* string constants found — has the naming changed?"
    missing = {f"{p}-result-" for p in primitives} - set(devcontract.FALLBACK_RESULT_PREFIXES)
    assert not missing, f"dev primitives whose completion marker cannot be read back: {missing}"


def test_find_artifact_ignores_fence_quoted_heading(tmp_path):
    """A spec whose only `## Auto Run Result` is a fenced example must not
    qualify as a terminal artifact, even with a fresh mtime — otherwise the
    agent's first save of such a spec reads as this session's result (#52)."""
    sp = tmp_path / "spec-1-1-a.md"
    sp.write_text(
        "---\nstatus: in-progress\n---\n\n## Intent\n\n"
        "```md\n## Auto Run Result\n\nStatus: done\n```\n\nbody\n",
        encoding="utf-8",
    )
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) is None


def test_find_artifact_ignores_heading_in_longer_outer_fence(tmp_path):
    """A `## Auto Run Result` fenced inside a 4-backtick block (past a lone inner
    ``` line) must not qualify the spec as a terminal artifact — the char+length
    tracker keeps the outer fence open where line-parity would not."""
    sp = tmp_path / "spec-1-1-a.md"
    sp.write_text(
        "---\nstatus: in-progress\n---\n\n## Intent\n\n"
        "````\n```\n## Auto Run Result\n\nStatus: done\n````\n\nbody\n",
        encoding="utf-8",
    )
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) is None


# The read-back decodes artifacts as UTF-8. A spec truncated mid-write (the CLI
# was killed) can end inside a multi-byte sequence; `read_text(encoding="utf-8")`
# then raises UnicodeDecodeError — a ValueError, NOT an OSError.
_BAD_UTF8 = b"\xff\xfe\x00\x01 not utf-8 \x80\x81"


def test_find_artifact_skips_non_utf8_spec(tmp_path):
    """A binary/truncated candidate cannot be shown to carry a terminal section, so
    it does not qualify — and must be skipped, not raised on, even though it is the
    newest file. An older qualifying spec still wins."""
    good = tmp_path / "spec-1-1-a.md"
    good.write_text("---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n")
    torn = tmp_path / "spec-1-1-b.md"
    torn.write_bytes(_BAD_UTF8)
    os.utime(good, ns=(1_000_000_000, 1_000_000_000))
    os.utime(torn, ns=(2_000_000_000, 2_000_000_000))  # newest, but unreadable
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) == good


def test_find_artifact_skips_only_candidate_when_non_utf8(tmp_path):
    (tmp_path / "spec-1-1-a.md").write_bytes(_BAD_UTF8)
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) is None


def test_synthesize_result_non_utf8_fallback_marker_is_not_terminal(tmp_path):
    """The no-spec fallback marker is matched by NAME, so the finder hands it back
    without ever reading it — the decode fault lands here instead. An unreadable
    spec carries no parseable result, so it reads exactly like one that has not
    terminated yet: no result_json, no crash."""
    marker = tmp_path / "bmad-dev-auto-result-unclear-1234.md"
    marker.write_bytes(_BAD_UTF8)
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) == marker
    sr = devcontract.synthesize_result(marker, story_key="1-1")
    assert sr.result_json is None
    assert sr.status_consistent is True


def test_synthesize_result_oserror_is_not_terminal(tmp_path, monkeypatch):
    """An unreadable spec is not evidence a session finished, so it reads exactly
    like one that has not terminated yet — no result_json, no crash. The caller
    keeps polling; a fault that outlives the grace window becomes a stall/timeout
    verdict that `_post_kill_reconcile` can still rescue. Before this, an OSError
    here took the whole run down (engine.run()'s `except Exception` → crash.txt).

    `devcontract` binds `read_frontmatter` by ``from .verify import``, so the name
    to patch is `devcontract.read_frontmatter` — patching `verify.read_frontmatter`
    would leave this module's already-bound reference untouched and the test would
    pass for the wrong reason."""
    spec = tmp_path / "spec-1-1-a.md"
    spec.write_text("---\nstatus: done\n---\n\n## Auto Run Result\n\n- Status: done\n")

    def boom(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(devcontract, "read_frontmatter", boom)
    sr = devcontract.synthesize_result(spec, story_key="1-1")
    assert sr.result_json is None
    assert sr.status_consistent is True


# ----------------------------------------------------------- reset_spec_status


def test_reset_status_preserves_quotes_and_inline_comment(tmp_path):
    sp = tmp_path / "spec.md"
    sp.write_text(
        "---\ntitle: 'x'\nstatus: 'done' # draft | ready-for-dev | done\n"
        "review_loop_iteration: 2\n---\n\n## Intent\n\nbody\n",
        encoding="utf-8",
    )
    assert devcontract.reset_spec_status(sp, "in-progress", confine_root=tmp_path) is True
    assert "status: 'in-progress' # draft | ready-for-dev | done\n" in sp.read_text()
    # nothing else moved
    assert "review_loop_iteration: 2\n" in sp.read_text()


def test_reset_status_unquoted(tmp_path):
    sp = tmp_path / "spec.md"
    sp.write_text("---\nstatus: done\n---\n\nbody\n", encoding="utf-8")
    assert devcontract.reset_spec_status(sp, "in-progress", confine_root=tmp_path) is True
    assert "status: in-progress\n" in sp.read_text()


def test_reset_status_leaves_prose_status_line_untouched(tmp_path):
    """Only the frontmatter status is rewritten — a `Status:` line in the
    ## Auto Run Result prose body must survive verbatim."""
    sp = _spec(tmp_path / "spec.md", status="done", auto_run="done")
    devcontract.reset_spec_status(sp, "in-progress", confine_root=tmp_path)
    text = sp.read_text()
    assert "status: 'in-progress'\n" in text  # frontmatter flipped
    assert "- Status: done\n" in text  # prose untouched


def test_reset_status_idempotent_no_write(tmp_path):
    sp = tmp_path / "spec.md"
    sp.write_text("---\nstatus: 'in-progress'\n---\n\nbody\n", encoding="utf-8")
    before = sp.stat().st_mtime_ns
    assert devcontract.reset_spec_status(sp, "in-progress", confine_root=tmp_path) is False
    assert sp.stat().st_mtime_ns == before  # no rewrite at all


def test_reset_status_no_frontmatter(tmp_path):
    sp = tmp_path / "spec.md"
    sp.write_text("# just a heading\n\nstatus: done\n", encoding="utf-8")
    assert devcontract.reset_spec_status(sp, "in-progress", confine_root=tmp_path) is False
    assert "status: done\n" in sp.read_text()  # body status not touched


def test_reset_status_preserves_crlf_across_the_whole_spec(tmp_path):
    """#357 part 1. The re-open path reads the spec the skill wrote; through
    `read_text` a CRLF spec was decoded to LF and `_atomic_write_spec` then
    persisted that — every ending in the file relaid by a repair contracted to
    move one value. `_render_status_line` already carried the `\\r` (it lands in
    the pattern's `rest` group), so only the read was wrong."""
    sp = tmp_path / "spec.md"
    original = (
        "---\r\ntitle: 'x'\r\nstatus: 'done' # keep me\r\n---\r\n\r\n## Intent\r\n\r\nbody\r\n"
    )
    sp.write_bytes(original.encode("utf-8"))
    assert devcontract.reset_spec_status(sp, "in-progress", confine_root=tmp_path) is True
    text = sp.read_bytes().decode("utf-8")
    assert text == original.replace("status: 'done'", "status: 'in-progress'")
    assert "\n" not in text.replace("\r\n", "")  # no bare LF introduced


def test_reset_status_inserts_a_missing_status_with_the_blocks_crlf(tmp_path):
    """The insert half of the same path: a template can omit `status:` entirely,
    and the appended line takes the block's own ending rather than a flat `\\n`."""
    sp = tmp_path / "spec.md"
    sp.write_bytes(b"---\r\ntitle: 'x'\r\n---\r\n\r\nbody\r\n")
    assert devcontract.reset_spec_status(sp, "in-progress", confine_root=tmp_path) is True
    text = sp.read_bytes().decode("utf-8")
    assert text == "---\r\ntitle: 'x'\r\nstatus: in-progress\r\n---\r\n\r\nbody\r\n"
    assert "\n" not in text.replace("\r\n", "")


def test_reset_status_no_ops_on_a_cr_only_spec(tmp_path):
    """Characterization of a behavior delta the byte-level read introduces, pinned
    rather than fixed. `_FRONTMATTER_RE` is line-oriented on `\\r?\\n`, so a
    CR-only spec now finds no frontmatter block and returns False; through
    `read_text` the CRs were translated to LFs first and the edit landed (writing
    the file back as LF, which was the defect). Its
    `frontmatter.set_frontmatter_status` sibling is `splitlines`-based and DOES
    rewrite this shape — the asymmetry is documented at both writers. Widening
    the pattern to `\\r` is a larger contract than this fix owns, and no BMAD tool
    authors CR-only files."""
    sp = tmp_path / "spec.md"
    original = "---\rstatus: 'done'\r---\r\rbody\r"
    sp.write_bytes(original.encode("utf-8"))
    assert devcontract.reset_spec_status(sp, "in-progress", confine_root=tmp_path) is False
    assert sp.read_bytes() == original.encode("utf-8")  # untouched, not half-written
    # ...and the splitlines-based sibling rewrites the very same bytes
    assert verify.set_frontmatter_status(sp, "in-progress", confine_root=tmp_path) is True
    assert sp.read_bytes() == b"---\rstatus: in-progress\r---\r\rbody\r"


def test_reset_spec_status_noop_when_spec_absent(tmp_path):
    """A re-drive against a spec that no longer exists on disk no-ops cleanly
    rather than raising (mirrors verify.set_frontmatter_status)."""
    sp = tmp_path / "missing.md"
    assert not sp.exists()
    assert devcontract.reset_spec_status(sp, "in-progress", confine_root=tmp_path) is False


@pytest.mark.parametrize(
    ("shape", "text"),
    [
        ("block-scalar-indicator", "---\nstatus: |\n  in-review\n---\n\nbody\n"),
        ("numeric-value", "---\nstatus: 123\n---\n\nbody\n"),
        ("flow-mapping", "---\n{status: in-review, keep: 1}\n---\n\nbody\n"),
    ],
    ids=["block-scalar-indicator", "numeric-value", "flow-mapping"],
)
def test_reset_status_refuses_the_shapes_its_own_regex_misreads(tmp_path, shape, text):
    """`_FM_STATUS_RE` reads only a `[A-Za-z-]*` value, so it used to fill
    `status: |` to `status: done|` and `status: 123` to `status: done123` — with
    a True return, on the repair path, silently. The shared verified edit
    re-parses each trial before keeping it, so these become a refusal and the
    spec is left exactly as authored."""
    sp = tmp_path / "spec.md"
    sp.write_bytes(text.encode("utf-8"))
    with pytest.raises(verify.FrontmatterWriteError):
        devcontract.reset_spec_status(sp, "done", confine_root=tmp_path)
    assert sp.read_bytes() == text.encode("utf-8")


def test_reset_status_inserts_rather_than_rewriting_a_nested_decoy(tmp_path):
    """The old `.sub(count=1)` rewrote the FIRST `status:`-looking line, so a
    `meta:` block carrying one took the write and the story's real status never
    appeared. The reader sees no top-level status here, and this writer's
    contract is to INSERT one — so the decoy survives verbatim and the real key
    is added."""
    sp = tmp_path / "spec.md"
    sp.write_text("---\nmeta:\n  status: draft\n---\n\nbody\n", encoding="utf-8")
    assert devcontract.reset_spec_status(sp, "done", confine_root=tmp_path) is True
    text = sp.read_text()
    assert "  status: draft\n" in text  # the decoy is not this story's status
    assert verify.status_of(verify.read_frontmatter(sp)) == "done"


# ----------------------------------------------------------- RECONCILABLE_FROM


def test_reconcilable_from_includes_in_review_excludes_terminal_statuses():
    """The allowlist contains only statuses a half-finalized generic spec can be
    reconciled FROM. `in-review` is included: on the sole generic `bmad-dev-auto`
    path it is the transient marker step-04 sets at its start, not a deliberate
    terminal (the legacy `bmad-loop-dev` review-handoff fork is retired). `done`
    and `blocked` are never reconciled (idempotent / must route to PAUSE)."""
    assert devcontract.RECONCILABLE_FROM == frozenset(
        {"", "draft", "ready-for-dev", "in-progress", "in-review"}
    )
    for deliberate in ("done", "blocked"):
        assert deliberate not in devcontract.RECONCILABLE_FROM


@pytest.mark.parametrize("frm", ["draft", "ready-for-dev", "in-progress", "in-review"])
def test_reset_status_from_each_reconcilable_value_to_done(tmp_path, frm):
    """reset_spec_status advances every line-valued reconcilable frontmatter status
    to done, rewriting only the frontmatter line."""
    sp = _spec(tmp_path / "spec.md", status=frm, auto_run="done")
    assert devcontract.reset_spec_status(sp, "done", confine_root=tmp_path) is True
    text = sp.read_text()
    assert "status: 'done'\n" in text  # frontmatter advanced
    assert "- Status: done\n" in text  # prose untouched


def test_reset_status_fills_empty_value(tmp_path):
    """The "" allowlist member: a present-but-empty `status:` is filled in place,
    leaving the prose Status line untouched."""
    sp = _spec(tmp_path / "spec.md", status="", auto_run="done")
    assert devcontract.reset_spec_status(sp, "done", confine_root=tmp_path) is True
    text = sp.read_text()
    assert "status: 'done'\n" in text  # empty value filled
    assert "- Status: done\n" in text  # prose untouched


def test_reset_status_fills_bare_yaml_null(tmp_path):
    """A bare `status:` (YAML null, no trailing space) is filled to a VALID
    `status: done` line — never `status:done`, which would drop the key on
    re-parse. Re-reading the frontmatter must yield the new status."""
    from bmad_loop import verify

    sp = tmp_path / "spec.md"
    sp.write_text(
        "---\ntitle: 'x'\nstatus:\n---\n\n## Auto Run Result\n\n- Status: done\n",
        encoding="utf-8",
    )
    assert devcontract.reset_spec_status(sp, "done", confine_root=tmp_path) is True
    text = sp.read_text()
    assert "status: done\n" in text  # space preserved -> valid YAML
    assert "status:done" not in text  # the corruption form is never written
    assert verify.status_of(verify.read_frontmatter(sp)) == "done"  # re-parses cleanly
    assert "- Status: done\n" in text  # prose untouched


def test_reset_status_blank_value_keeps_inline_comment(tmp_path):
    """A blank value with a trailing inline comment (`status: # tbd`, parsed as
    YAML-null) is filled without merging the comment into the scalar: the result
    must stay valid YAML re-parsing to the new status, comment preserved."""
    from bmad_loop import verify

    sp = tmp_path / "spec.md"
    sp.write_text(
        "---\ntitle: 'x'\nstatus: # intentionally blank\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert devcontract.reset_spec_status(sp, "done", confine_root=tmp_path) is True
    text = sp.read_text()
    assert "status: done # intentionally blank\n" in text  # space kept before `#`
    assert "done#" not in text  # never abut the value to the comment
    assert verify.status_of(verify.read_frontmatter(sp)) == "done"  # re-parses cleanly


def test_reset_status_inserts_missing_line(tmp_path):
    """A frontmatter block with NO `status:` line gets one inserted before the
    closing fence; existing keys survive and the prose body is untouched."""
    sp = tmp_path / "spec.md"
    sp.write_text(
        "---\ntitle: 'x'\nbaseline_revision: 'abc'\n---\n\n## Intent\n\nbody\n",
        encoding="utf-8",
    )
    assert devcontract.reset_spec_status(sp, "done", confine_root=tmp_path) is True
    text = sp.read_text()
    assert "status: done\n" in text  # inserted
    assert "title: 'x'\n" in text and "baseline_revision: 'abc'\n" in text  # kept
    assert "## Intent\n\nbody\n" in text  # body untouched


# ------------------------------------------------------- strip_auto_run_result


def test_strip_auto_run_result_removes_trailing_section(tmp_path):
    """The stale terminal section goes; frontmatter and body above it survive.
    The stripped spec must no longer qualify as a result artifact even with a
    fresh mtime — that is the whole point of stripping on re-arm."""
    sp = tmp_path / "spec.md"
    sp.write_text(
        "---\nstatus: in-progress\n---\n\n## Intent\n\nbody\n\n"
        "## Auto Run Result\n\nStatus: done\nAll done.\n",
        encoding="utf-8",
    )
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is True
    text = sp.read_text()
    assert "Auto Run Result" not in text and "All done." not in text
    assert "status: in-progress\n" in text and "## Intent\n\nbody\n" in text
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) is None


def test_strip_auto_run_result_stops_at_next_heading(tmp_path):
    """A section wedged mid-document is excised up to the next same-level
    heading; sub-headings inside the section are removed with it."""
    sp = tmp_path / "spec.md"
    sp.write_text(
        "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
        "### Detail\n\nstale\n\n## Change Log\n\nkept\n",
        encoding="utf-8",
    )
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is True
    text = sp.read_text()
    assert "Auto Run Result" not in text and "stale" not in text
    assert "## Change Log\n\nkept\n" in text


def test_strip_auto_run_result_stops_at_bare_empty_heading(tmp_path):
    """Reviewer guard (#53, comment 3522512350): a bare `##` line is a valid empty
    CommonMark heading, so the strip bounds the removed section there and keeps the
    empty-heading region after it. This is the safe direction on a destructive strip
    (truncate early -> delete less); requiring a space/tab delimiter instead of `\\s`
    would run the strip PAST the empty heading and over-delete."""
    sp = tmp_path / "spec.md"
    sp.write_text(
        "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n\n"
        "##\n\nkept after empty heading\n",
        encoding="utf-8",
    )
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is True
    text = sp.read_text()
    assert "Auto Run Result" not in text
    assert "##\n\nkept after empty heading\n" in text


def test_strip_auto_run_result_noop_without_section(tmp_path):
    sp = tmp_path / "spec.md"
    original = "---\nstatus: draft\n---\n\n## Intent\n\nbody\n"
    sp.write_text(original, encoding="utf-8")
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is False
    assert sp.read_text() == original


def test_strip_auto_run_result_noop_when_spec_absent(tmp_path):
    """The re-arm path calls strip after flipping frontmatter status; if the spec
    was removed out from under the run the strip no-ops cleanly rather than crashing
    the re-drive (only an absent file is guarded — a present-but-unreadable spec
    still raises so the stale section can't silently survive the re-open)."""
    sp = tmp_path / "missing.md"
    assert not sp.exists()
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is False


def test_strip_auto_run_result_raises_on_undecodable_spec(tmp_path):
    """Repair-write doctrine: a present-but-unreadable spec must RAISE, never
    no-op. Only an absent file is guarded; a spec that cannot be decoded is left
    to raise so the stale terminal section can't silently survive the re-open —
    the #160 review-launch strip site depends on that surfacing (silently skipping
    the strip recreates the exact bug it fixes)."""
    sp = tmp_path / "spec.md"
    sp.write_bytes(b"---\nstatus: done\n---\n\n\xff\xfe## Auto Run Result\n\nStatus: done\n")
    with pytest.raises(UnicodeDecodeError):
        devcontract.strip_auto_run_result(sp, confine_root=tmp_path)


def test_strip_auto_run_result_preserves_crlf_in_what_it_keeps(tmp_path):
    """#357 part 1. `append_auto_run_result` already wrote its section in the
    spec's own endings; a strip that read through `read_text` did not round-trip
    its own sibling's output — it removed the section and returned the surviving
    CRLF spec relaid to LF."""
    sp = tmp_path / "spec.md"
    kept = "---\r\nstatus: done\r\n---\r\n\r\n## Intent\r\n\r\nbody\r\n"
    sp.write_bytes((kept + "## Auto Run Result\r\n\r\nStatus: done\r\n").encode("utf-8"))
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is True
    text = sp.read_bytes().decode("utf-8")
    assert text == kept
    assert "\n" not in text.replace("\r\n", "")  # no bare LF introduced


def test_append_then_strip_round_trips_a_crlf_spec_byte_for_byte(tmp_path):
    """The pair, end to end: the LF round-trip is already pinned at
    `test_append_then_strip_roundtrip`, and before the byte-level read the CRLF
    one could not hold — the strip's own read decided the file's endings."""
    sp = tmp_path / "spec.md"
    original = "---\r\nstatus: done\r\n---\r\n\r\n## Intent\r\n\r\nbody\r\n"
    sp.write_bytes(original.encode("utf-8"))
    assert (
        devcontract.append_auto_run_result(
            sp, "done", detail="extra context", confine_root=tmp_path
        )
        is True
    )
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is True
    assert sp.read_bytes() == original.encode("utf-8")


def test_strip_auto_run_result_no_ops_on_a_cr_only_spec(tmp_path):
    """Sibling of `test_reset_status_no_ops_on_a_cr_only_spec`, same root cause:
    `AUTO_RUN_HEADING_RE`'s MULTILINE `^` only matches after a `\\n`, so a CR-only
    spec's heading is invisible to the scan and the strip no-ops. Through
    `read_text` the CRs were translated first and the section was removed. Pinned,
    not fixed — `^` would not follow a widened pattern anyway."""
    sp = tmp_path / "spec.md"
    original = "---\rstatus: done\r---\r\r## Auto Run Result\r\rStatus: done\r"
    sp.write_bytes(original.encode("utf-8"))
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is False
    assert sp.read_bytes() == original.encode("utf-8")


def test_strip_auto_run_result_ignores_heading_quoted_in_code_fence(tmp_path):
    """A spec whose frozen intent quotes the heading inside a fenced example must
    not lose that content — stripping is destructive, so fenced pseudo-headings
    are not sections."""
    sp = tmp_path / "spec.md"
    original = (
        "---\nstatus: draft\n---\n\n## Intent\n\n"
        "```md\n## Auto Run Result\n\nStatus: done\n```\n\nmore body\n"
    )
    sp.write_text(original, encoding="utf-8")
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is False
    assert sp.read_text() == original


def test_strip_auto_run_result_ignores_heading_in_indented_fence(tmp_path):
    """Fences may be indented up to three spaces (CommonMark) — a heading quoted
    inside one is still fenced content, not a section."""
    sp = tmp_path / "spec.md"
    original = (
        "---\nstatus: draft\n---\n\n## Intent\n\n"
        "  ```md\n## Auto Run Result\n\nStatus: done\n  ```\n\nmore body\n"
    )
    sp.write_text(original, encoding="utf-8")
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is False
    assert sp.read_text() == original


def test_strip_auto_run_result_ignores_list_indented_fence(tmp_path):
    """Reviewer guard (#53): a `## Auto Run Result` quoted inside a fence nested
    under list indentation (4+ absolute leading spaces) is co-indented with the
    fence. `_FENCE_LINE_RE` only recognizes 0-3-space fences, so this fence is not
    tracked — but the heading is likewise indented and can never match the
    column-0-anchored `AUTO_RUN_HEADING_RE`, so there is nothing to strip. Locks
    that symmetry: giving the heading regex any leading-space tolerance would
    reopen this as a destructive false-positive on quoted spec prose."""
    sp = tmp_path / "spec.md"
    original = (
        "---\nstatus: draft\n---\n\n## Intent\n\n"
        "- outer bullet\n  - inner bullet, fenced example:\n"
        "    ```md\n    ## Auto Run Result\n\n    Status: done\n    ```\n\nmore body\n"
    )
    sp.write_text(original, encoding="utf-8")
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is False
    assert sp.read_text() == original


def test_strip_auto_run_result_skips_fenced_boundary_lines(tmp_path):
    """Column-0 `## `/`# ` lines inside a fenced block within the section (quoted
    shell comments, log output) are not boundaries — the whole stale section goes."""
    sp = tmp_path / "spec.md"
    sp.write_text(
        "---\nstatus: done\n---\n\n## Intent\n\nbody\n\n"
        "## Auto Run Result\n\nStatus: done\n\n"
        "```sh\n## run tests\npytest -q\n```\n\ntrailing stale prose\n",
        encoding="utf-8",
    )
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is True
    text = sp.read_text()
    assert "Auto Run Result" not in text and "trailing stale prose" not in text
    assert "## Intent\n\nbody\n" in text


def test_strip_auto_run_result_ignores_heading_in_longer_outer_fence(tmp_path):
    """Destructive-op guard: a `## Auto Run Result` fenced inside a 4-backtick
    block that contains a lone inner ``` line must be preserved (no-op). Line
    parity would flip on the inner ``` and strip the fenced documentation."""
    sp = tmp_path / "spec.md"
    original = (
        "---\nstatus: draft\n---\n\n## Intent\n\n"
        "````\n```\n## Auto Run Result\n\nStatus: done\n````\n\nmore body\n"
    )
    sp.write_text(original, encoding="utf-8")
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is False
    assert sp.read_text() == original


def test_strip_auto_run_result_ignores_heading_in_mismatched_fence_char(tmp_path):
    """A ``` line inside a ~~~ fence is content, not a close — the fenced
    `## Auto Run Result` after it is documentation and must survive."""
    sp = tmp_path / "spec.md"
    original = (
        "---\nstatus: draft\n---\n\n## Intent\n\n"
        "~~~\n```\n## Auto Run Result\n\nStatus: done\n~~~\n\nmore body\n"
    )
    sp.write_text(original, encoding="utf-8")
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is False
    assert sp.read_text() == original


# ------------------------------- find_frontmatter_candidates (#224)
#
# The missing-marker fallback scan: terminal-frontmatter specs with NO real
# `## Auto Run Result` heading, written at/after the session-launch floor.


def _write(dirpath: Path, name: str, text: str, mtime_ns: int | None = None) -> Path:
    path = dirpath / name
    path.write_text(text, encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def test_frontmatter_candidates_finds_done_and_blocked(tmp_path):
    _write(tmp_path, "spec-a.md", "---\nstatus: done\n---\n\nbody\n", 2_000)
    _write(tmp_path, "spec-b.md", "---\nstatus: blocked\n---\n\nbody\n", 3_000)
    found = devcontract.find_frontmatter_candidates(tmp_path, since_ns=0)
    # most-recent first
    assert [p.name for p in found] == ["spec-b.md", "spec-a.md"]


def test_frontmatter_candidates_excludes_marker_bearing_spec(tmp_path):
    _write(
        tmp_path,
        "spec-a.md",
        "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n",
    )
    assert devcontract.find_frontmatter_candidates(tmp_path, since_ns=0) == []


def test_frontmatter_candidates_includes_fence_quoted_heading_only(tmp_path):
    """A heading quoted inside a fence is documentation (#52) — the spec has no
    REAL marker, so it IS a missing-marker candidate."""
    p = _write(
        tmp_path,
        "spec-a.md",
        "---\nstatus: done\n---\n\n```\n## Auto Run Result\n\nStatus: done\n```\n",
    )
    assert devcontract.find_frontmatter_candidates(tmp_path, since_ns=0) == [p]


@pytest.mark.parametrize("prefix", ["bmad-build-auto-result-", "bmad-dev-auto-result-"])
def test_frontmatter_candidates_excludes_no_spec_fallback_file(tmp_path, prefix):
    """`<primitive>-result-*.md` is the skill's no-spec fallback — matched by
    name on the normal scan, so the fallback scan must not double-claim it.

    Both eras, because this is the OTHER consumer of `FALLBACK_RESULT_PREFIXES`
    and it was pinned on the legacy spelling alone — which post-rename is the
    era a project is least likely to be on."""
    _write(tmp_path, f"{prefix}x.md", "---\nstatus: done\n---\n\nbody\n")
    assert devcontract.find_frontmatter_candidates(tmp_path, since_ns=0) == []


def test_frontmatter_candidates_enforces_mtime_floor(tmp_path):
    _write(tmp_path, "spec-a.md", "---\nstatus: done\n---\n\nbody\n", 1_000)
    assert devcontract.find_frontmatter_candidates(tmp_path, since_ns=2_000) == []


def test_frontmatter_candidates_excludes_non_terminal_status(tmp_path):
    for status in ("draft", "in-progress", "in-review", "ready-for-dev", ""):
        _write(tmp_path, "spec-a.md", f"---\nstatus: {status}\n---\n\nbody\n")
        assert devcontract.find_frontmatter_candidates(tmp_path, since_ns=0) == []


def test_frontmatter_candidates_skips_unreadable_file(tmp_path):
    (tmp_path / "spec-a.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8 \x80\x81")
    assert devcontract.find_frontmatter_candidates(tmp_path, since_ns=0) == []


def test_frontmatter_candidates_missing_dir_is_empty(tmp_path):
    assert devcontract.find_frontmatter_candidates(tmp_path / "nope", since_ns=0) == []


# ------------------------------- append_auto_run_result (#276 M3)
#
# The artifact-repair writer: the inverse of `strip_auto_run_result`. Appends the
# `## Auto Run Result` marker a missing-marker synthesis proved the session owed,
# bringing a marker-less terminal spec back into contract.


def test_append_auto_run_result_minimal_shape(tmp_path):
    """The appended section carries a parseable Status + the provenance note, and
    leaves the spec `synthesize_result`-consistent with its unchanged frontmatter."""
    sp = _write(
        tmp_path,
        "spec-a.md",
        "---\nstatus: done\nbaseline_revision: 'abc123'\n---\n\n## Intent\n\nbody\n",
    )
    assert devcontract.append_auto_run_result(sp, "done", confine_root=tmp_path) is True
    text = sp.read_text()
    arr = devcontract.parse_auto_run_result(text)
    assert arr.present and arr.status == "done"
    assert devcontract.ORCHESTRATOR_SYNTH_NOTE in arr.detail
    # frontmatter is untouched, so the prose Status must equal it → consistent
    assert "status: done\n" in text
    sr = devcontract.synthesize_result(sp, story_key="1-1-a")
    assert sr.status_consistent is True
    assert sr.result_json is not None and sr.result_json["status"] == "done"


def test_append_moves_spec_to_marker_scan_territory(tmp_path):
    """Before: a missing-marker candidate, invisible to the marker scan. After:
    gone from the fallback scan, found by the normal `find_result_artifact`."""
    sp = _write(tmp_path, "spec-a.md", "---\nstatus: done\n---\n\nbody\n")
    assert devcontract.find_frontmatter_candidates(tmp_path, since_ns=0) == [sp]
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) is None

    assert devcontract.append_auto_run_result(sp, "done", confine_root=tmp_path) is True

    assert devcontract.find_frontmatter_candidates(tmp_path, since_ns=0) == []
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) == sp


def test_append_refuses_existing_real_heading(tmp_path):
    """Idempotence: a real (non-fenced) marker already present blocks the append,
    and the bytes are left identical."""
    original = "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n"
    sp = _write(tmp_path, "spec-a.md", original)
    before = sp.read_bytes()
    assert devcontract.append_auto_run_result(sp, "done", confine_root=tmp_path) is False
    assert sp.read_bytes() == before


def test_append_missing_file_false(tmp_path):
    assert (
        devcontract.append_auto_run_result(tmp_path / "ghost.md", "done", confine_root=tmp_path)
        is False
    )


def test_append_raises_on_undecodable_spec(tmp_path):
    """Repair-write doctrine (shared with the strip): a present-but-unreadable spec
    RAISES, never no-ops — the caller imposes best-effort, not the writer."""
    sp = tmp_path / "spec-a.md"
    sp.write_bytes(b"---\nstatus: done\n---\n\n\xff\xfebody\n")
    with pytest.raises(UnicodeDecodeError):
        devcontract.append_auto_run_result(sp, "done", confine_root=tmp_path)


def test_append_over_fence_quoted_heading_appends(tmp_path):
    """#52 symmetry with the strip: a heading quoted inside a fence is
    documentation, not a real marker, so it does NOT block the append. Exactly one
    REAL heading exists afterward — the appended one — and it parses."""
    sp = _write(
        tmp_path,
        "spec-a.md",
        "---\nstatus: done\n---\n\n## Intent\n\n```md\n## Auto Run Result\n\nStatus: done\n```\n",
    )
    assert devcontract.append_auto_run_result(sp, "done", confine_root=tmp_path) is True
    text = sp.read_text()
    assert len(devcontract._section_headings(text)) == 1
    arr = devcontract.parse_auto_run_result(text)
    assert arr.present and arr.status == "done"


def test_append_handles_missing_trailing_newline_and_crlf(tmp_path):
    """A CRLF spec with no trailing newline: the heading lands on its own line
    (never glued to the last body line) and the file's CRLF ending is preserved —
    no bare LF is introduced into the appended block."""
    sp = tmp_path / "spec-a.md"
    sp.write_bytes(b"---\r\nstatus: done\r\n---\r\n\r\n## Intent\r\n\r\nbody")
    assert devcontract.append_auto_run_result(sp, "done", confine_root=tmp_path) is True
    text = sp.read_bytes().decode("utf-8")
    assert "body## Auto Run Result" not in text  # never glued
    assert "body\r\n## Auto Run Result\r\n" in text  # own line, trailing newline ensured
    assert "\r\nStatus: done\r\n" in text  # CRLF preserved in the block
    assert "\n" not in text.replace("\r\n", "")  # no bare LF introduced


def test_append_then_strip_roundtrip(tmp_path):
    """The strip removes exactly the appended section: a spec that ended in a
    newline round-trips byte-for-byte through append → strip, detail paragraph and
    all."""
    original = "---\nstatus: done\n---\n\n## Intent\n\nbody\n"
    sp = _write(tmp_path, "spec-a.md", original)
    assert (
        devcontract.append_auto_run_result(
            sp, "done", detail="extra context", confine_root=tmp_path
        )
        is True
    )
    assert "## Auto Run Result" in sp.read_text() and "extra context" in sp.read_text()
    assert devcontract.strip_auto_run_result(sp, confine_root=tmp_path) is True
    assert sp.read_text() == original


def test_append_bare_cr_terminated_spec_makes_heading_visible(tmp_path):
    """#276: a spec ending in a BARE ``\\r`` (no ``\\n``) must not glue an invisible
    heading. The append completes the CR to CRLF so ``## Auto Run Result`` lands on a
    line the scan's ``^``-anchored regex recognizes: the heading parses, exactly one
    real heading exists, and a second append is idempotent (proving the heading is
    visible to the scan — under the old logic it was glued after ``\\r`` and unseen)."""
    sp = tmp_path / "spec-a.md"
    sp.write_bytes(b"---\nstatus: done\n---\n\nbody\r")
    assert devcontract.append_auto_run_result(sp, "done", confine_root=tmp_path) is True
    text = sp.read_bytes().decode("utf-8")
    assert len(devcontract._section_headings(text)) == 1
    arr = devcontract.parse_auto_run_result(text)
    assert arr.present and arr.status == "done"
    assert (
        devcontract.append_auto_run_result(sp, "done", confine_root=tmp_path) is False
    )  # idempotent → visible


@pytest.mark.parametrize(
    "writer, original",
    [
        (
            lambda sp, root: devcontract.append_auto_run_result(sp, "done", confine_root=root),
            "---\nstatus: done\n---\n\n# Story\n\nbody\n",  # no marker → append writes
        ),
        (
            lambda sp, root: devcontract.strip_auto_run_result(sp, confine_root=root),
            "---\nstatus: done\n---\n\n## Auto Run Result\n\nStatus: done\n",  # marker → strip writes
        ),
        (
            lambda sp, root: devcontract.reset_spec_status(sp, "in-progress", confine_root=root),
            "---\nstatus: done\n---\n\nbody\n",  # status differs → reset writes
        ),
    ],
    ids=["append", "strip", "reset"],
)
def test_repair_write_failure_never_truncates_spec(tmp_path, monkeypatch, writer, original):
    """#276: the three in-place spec rewriters are atomic. A failed write (disk-full,
    interruption, short write) leaves the original spec byte-for-byte intact with no
    ``.tmp`` litter — the "a failed repair never loses work" invariant (fault
    injection on the old truncating write reduced a 46-byte spec to 12).

    #379 moved `_atomic_write_spec` onto `platform_util.atomic_write_bytes`, so the
    fault is injected at devcontract's OWN binding of that helper. The litter
    assertion still grades this module: it fires if a rewriter ever mints a temp of
    its own before delegating. The helper's internal unlink-on-raise is graded where
    it lives, by test_platform_util's `os.replace` boom rows.

    #593 moved the in-tree arm again, to `atomic_write_bytes_confined`, and that is
    the binding patched here: `sp` sits under `tmp_path`, so the chokepoint takes
    the confined arm. `devcontract.atomic_write_bytes` still EXISTS — the
    out-of-tree arm keeps it — so patching that name would install cleanly and
    simply never fire, a silent false green. `pytest.raises` is what catches it."""
    sp = _write(tmp_path, "spec-a.md", original)

    def boom(path, data, *, confine_root, require_writable_target=False):
        raise OSError("no space left on device")

    monkeypatch.setattr(devcontract, "atomic_write_bytes_confined", boom)
    with pytest.raises(OSError, match="no space left"):
        writer(sp, tmp_path)
    assert sp.read_text(encoding="utf-8") == original  # original untouched
    assert list(tmp_path.glob("*.tmp")) == []  # no temp minted here, no litter


def test_atomic_write_spec_hands_the_helper_bytes_not_text(tmp_path, monkeypatch):
    """#379 moved this writer onto `platform_util.atomic_write_bytes`. Grades the
    BYTES half of that choice platform-independently: `test_..._preserves_crlf`
    below already forbids relaying a CRLF spec, but `atomic_write_text` keeps
    `Path.write_text`'s translating newline default and on POSIX
    `os.linesep == "\\n"`, so swapping the text helper in reddens that row on
    WINDOWS ONLY and CI's Linux leg would call the swap green.

    So inspect the payload the helper is handed, upstream of any translation. The
    binding is WRAPPED rather than replaced, so the real write still happens.

    BOTH helper names are wrapped into the same list, the text one with
    `raising=False` since this module does not import it. That is what makes the
    `isinstance` line the assertion that fires: wrapping only the bytes name would
    grade "the bytes helper was called", so the swap would redden on an empty `seen`
    and this row would be claiming more than it checked.

    Ablation: swap `atomic_write_bytes` for `atomic_write_text` in
    `_atomic_write_spec` (dropping the `.encode`) and this reddens on every
    platform, on the `isinstance` row."""
    seen: list[bytes | str] = []
    real = devcontract.atomic_write_bytes_confined

    def record(path, data, *, confine_root, require_writable_target=False):
        seen.append(data)
        blob = data if isinstance(data, bytes) else data.encode("utf-8")
        real(
            path,
            blob,
            confine_root=confine_root,
            require_writable_target=require_writable_target,
        )

    # the CONFINED binding (#593) — the arm an in-tree spec takes. Patching
    # `devcontract.atomic_write_bytes` would still install (the out-of-tree arm
    # keeps that name) and record nothing; `len(seen) == 1` below is the backstop.
    monkeypatch.setattr(devcontract, "atomic_write_bytes_confined", record)
    monkeypatch.setattr(devcontract, "atomic_write_text_confined", record, raising=False)
    sp = tmp_path / "spec-a.md"
    sp.write_bytes(b"---\r\nstatus: done\r\n---\r\n\r\n# Story\r\n\r\nbody\r\n")

    devcontract.append_auto_run_result(sp, "done", confine_root=tmp_path)

    assert len(seen) == 1  # exactly one write — no retry loop crept in
    assert isinstance(seen[0], bytes)
    assert b"## Auto Run Result\r\n" in seen[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_atomic_write_spec_replaces_a_planted_symlink(tmp_path):
    """The row that grades this SITE's `follow_symlinks=False` argument rather than
    the helper's implementation of it (pinned in test_platform_util.py, where the
    helper is called directly).

    Behaviour-preserving, not a tightening: the `atomic_replace` this replaced never
    dereferenced its destination either, so passing the default True would have
    CHANGED what these four rewriters do. It is also the security choice — the spec
    path reaches them from a scan of a directory a driven session owns, so writing
    THROUGH a link planted at that name would hand the session a host-side write to
    any operator-writable path.

    Ablation: drop `follow_symlinks=False` in `_atomic_write_spec` and this reddens
    alone — alone in both directions, since it does not reach the two
    `frontmatter`-side writers of these same specs. Each of those passes its own
    literal and carries its own row (tests/test_frontmatter.py,
    tests/test_resolve.py)."""
    original = "---\nstatus: done\n---\n\nbody\n"
    real = _write(tmp_path, "someone-elses-file", original)
    link = tmp_path / "spec-a.md"
    link.symlink_to(real)

    assert devcontract.reset_spec_status(link, "in-progress", confine_root=tmp_path) is True

    assert not link.is_symlink()  # the NAME was replaced
    assert verify.read_frontmatter(link)["status"] == "in-progress"
    assert real.read_text(encoding="utf-8") == original  # not written through


# ------------------------------ append_operator_confirmation (#335 part 3)
#
# The audit section `bmad-loop confirm` writes when a human signs off a parked
# story. It is the whole record of the part of a story that happened OUTSIDE the
# repository — the commit shows what the agent did; nothing in git can show that
# someone bought the domain.

_PARKED = (
    "---\nstatus: awaiting-operator\nbaseline_revision: 'abc123'\n"
    "operator_actions:\n  - buy example.com\n  - publish the TXT record\n---\n\n"
    "## Intent\n\nbody\n"
)
_ACTIONS = ["buy example.com", "publish the TXT record"]


def test_operator_confirmation_records_the_actions_and_its_provenance(tmp_path):
    """The actions are RESTATED, not referenced: the frontmatter list is the claim
    and this is the acknowledgment, so a later reader comparing the two can see
    whether the spec was edited in between."""
    sp = _write(tmp_path, "spec-a.md", _PARKED)
    assert (
        devcontract.append_operator_confirmation(
            sp, _ACTIONS, date="2026-07-28", confine_root=tmp_path
        )
        is True
    )
    text = sp.read_text()
    assert "## Operator Confirmation" in text
    assert "Confirmed 2026-07-28:" in text
    assert "- buy example.com\n" in text and "- publish the TXT record\n" in text
    assert devcontract.OPERATOR_CONFIRM_NOTE in text
    # the writer records; it does not decide status — `confirm` flips that after
    assert "status: awaiting-operator\n" in text


def test_operator_confirmation_appends_after_the_existing_body(tmp_path):
    sp = _write(tmp_path, "spec-a.md", _PARKED)
    devcontract.append_operator_confirmation(sp, _ACTIONS, date="2026-07-28", confine_root=tmp_path)
    text = sp.read_text()
    assert text.index("## Intent") < text.index("## Operator Confirmation")
    assert text.startswith("---\n")  # frontmatter block untouched at the top


def test_operator_confirmation_accumulates_rather_than_no_opping(tmp_path):
    """Unlike `append_auto_run_result`, a second confirmation is a real event (a
    spec reverted to the park status and confirmed again). Dropping it would make
    the audit trail claim there was only ever one sign-off."""
    sp = _write(tmp_path, "spec-a.md", _PARKED)
    devcontract.append_operator_confirmation(sp, _ACTIONS, date="2026-07-28", confine_root=tmp_path)
    assert (
        devcontract.append_operator_confirmation(
            sp, ["the redone one"], date="2026-07-30", confine_root=tmp_path
        )
        is True
    )
    text = sp.read_text()
    assert text.count("## Operator Confirmation") == 2
    assert text.index("2026-07-28") < text.index("2026-07-30")  # newest last


def test_operator_confirmation_is_not_a_marker_the_result_scan_harvests(tmp_path):
    """The section must not make a parked spec look like it carries an
    `## Auto Run Result` — that marker is the dev session's completion contract,
    and a human sign-off is not one."""
    sp = _write(tmp_path, "spec-a.md", _PARKED)
    devcontract.append_operator_confirmation(sp, _ACTIONS, date="2026-07-28", confine_root=tmp_path)
    assert devcontract.parse_auto_run_result(sp.read_text()).present is False


def test_operator_confirmation_on_an_absent_spec_returns_false(tmp_path):
    assert (
        devcontract.append_operator_confirmation(
            tmp_path / "nope.md", _ACTIONS, date="2026-07-28", confine_root=tmp_path
        )
        is False
    )


def test_operator_confirmation_preserves_crlf(tmp_path):
    """An in-place write must not rewrite line endings it did not author."""
    sp = tmp_path / "spec-a.md"
    sp.write_bytes(_PARKED.replace("\n", "\r\n").encode("utf-8"))
    devcontract.append_operator_confirmation(sp, _ACTIONS, date="2026-07-28", confine_root=tmp_path)
    raw = sp.read_bytes().decode("utf-8")
    assert "## Operator Confirmation\r\n" in raw
    assert "\n" not in raw.replace("\r\n", "")  # every break is CRLF, none bare


def test_operator_confirmation_never_glues_onto_an_unterminated_body(tmp_path):
    sp = _write(tmp_path, "spec-a.md", "---\nstatus: awaiting-operator\n---\n\nno trailing nl")
    devcontract.append_operator_confirmation(sp, _ACTIONS, date="2026-07-28", confine_root=tmp_path)
    assert "no trailing nl## Operator" not in sp.read_text()
    assert "\n## Operator Confirmation" in sp.read_text()


@pytest.mark.parametrize(
    "tail",
    ["body\n", "body", "body\n\n"],
    ids=["terminated", "unterminated", "already-blank-separated"],
)
def test_operator_confirmation_leaves_a_blank_line_before_the_heading(tmp_path, tail):
    """The section is written to be READ. A heading welded to the paragraph above
    it renders as one run-on block and trips every markdown linter."""
    sp = _write(
        tmp_path, "spec-a.md", f"---\nstatus: awaiting-operator\n---\n\n## Intent\n\n{tail}"
    )
    devcontract.append_operator_confirmation(sp, _ACTIONS, date="2026-07-28", confine_root=tmp_path)
    text = sp.read_text()
    assert "\n\n## Operator Confirmation" in text
    assert "body\n\n\n" not in text  # exactly one blank line, never a growing gap


def test_operator_confirmation_write_failure_raises_and_keeps_the_spec(tmp_path, monkeypatch):
    """Repair-write doctrine: `confirm` is about to move the board to done, so a
    silently-skipped write would make it declare a story finished on a record it
    never made."""
    sp = _write(tmp_path, "spec-a.md", _PARKED)

    def boom(path, data, *, confine_root, require_writable_target=False):
        raise OSError("no space left on device")

    # the CONFINED binding: `sp` is under `tmp_path`, so the chokepoint takes that
    # arm (#593). `devcontract.atomic_write_bytes` survives for the out-of-tree arm,
    # so patching it here would never fire.
    monkeypatch.setattr(devcontract, "atomic_write_bytes_confined", boom)
    with pytest.raises(OSError, match="no space left"):
        devcontract.append_operator_confirmation(
            sp, _ACTIONS, date="2026-07-28", confine_root=tmp_path
        )
    assert sp.read_text(encoding="utf-8") == _PARKED
    assert list(tmp_path.glob("*.tmp")) == []


# ------------------------------------- confined parent (#593) + read-only (#597)
#
# `_atomic_write_spec` is the chokepoint for all four public writers, so these
# rows drive it through `reset_spec_status` and grade the branch itself: an
# in-tree spec takes the confined arm, an out-of-tree one keeps the plain
# no-follow write, a redirected parent inside the tree is refused, and a
# read-only spec earns a PermissionError.
#
# `confine_root` is always a real ANCESTOR of the spec's directory. The anchored
# walk covers the components strictly BELOW the root and opens the root itself
# without O_NOFOLLOW, so a `confine_root` naming the spec's own parent walks
# nothing, refuses nothing, and would leave the refusal row green with the escape
# still open.


def _spec_tree(tmp_path):
    root = tmp_path / "checkout"
    (root / "artifacts").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    return root, outside


def _spec_tap(label: str, seen: list[str], real):
    def record(path, data, **kw):
        seen.append(label)
        return real(path, data, **kw)

    return record


_RESET_SPEC = "---\nstatus: done\n---\n\nbody\n"


def test_atomic_write_spec_takes_the_confined_arm_for_an_in_tree_spec(tmp_path, monkeypatch):
    """Positive control, grading WHICH writer an in-tree spec reaches rather than
    only that the rewrite landed. Both bindings are wrapped and both keep the real
    write, so the reset below really lands on disk.

    Ablation: swap the two arms of the `is_relative_to` branch and this fails on
    `seen`, with the spec still correctly reset."""
    root, _ = _spec_tree(tmp_path)
    sp = _write(root / "artifacts", "spec-a.md", _RESET_SPEC)
    seen: list[str] = []
    monkeypatch.setattr(
        devcontract,
        "atomic_write_bytes_confined",
        _spec_tap("confined", seen, devcontract.atomic_write_bytes_confined),
    )
    monkeypatch.setattr(
        devcontract,
        "atomic_write_bytes",
        _spec_tap("plain", seen, devcontract.atomic_write_bytes),
    )

    assert devcontract.reset_spec_status(sp, "in-progress", confine_root=root) is True

    assert seen == ["confined"]
    assert "status: in-progress" in sp.read_text(encoding="utf-8")


def test_atomic_write_spec_keeps_the_plain_write_for_an_out_of_tree_spec(tmp_path, monkeypatch):
    """The else-arm, and the reason the chokepoint is a branch. These four writers
    are driven over `paths.implementation_artifacts`, which `bmadconfig` lets an
    operator configure OUTSIDE the checkout — `verify.spec_within_roots` trusts
    such a root, and a confined writer cannot vouch for a tree it was not given, so
    refusing there would break the repair path rather than close a hole.

    Ablation: call the confined writer unconditionally and this fails with
    `UnconfinedWriteError`, the spec never reset."""
    root, outside = _spec_tree(tmp_path)
    sp = _write(outside, "spec-a.md", _RESET_SPEC)
    seen: list[str] = []
    monkeypatch.setattr(
        devcontract,
        "atomic_write_bytes_confined",
        _spec_tap("confined", seen, devcontract.atomic_write_bytes_confined),
    )
    monkeypatch.setattr(
        devcontract,
        "atomic_write_bytes",
        _spec_tap("plain", seen, devcontract.atomic_write_bytes),
    )

    assert devcontract.reset_spec_status(sp, "in-progress", confine_root=root) is True

    assert seen == ["plain"]
    assert "status: in-progress" in sp.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_atomic_write_spec_refuses_a_symlinked_parent(tmp_path):
    """#593 at the chokepoint every one of the four writers funnels through. The
    `follow_symlinks=False` the row above grades stops at the FINAL component: the
    artifacts DIRECTORY was still resolved by name at both the `mkstemp` and the
    `os.replace`, so a link planted there landed the temp and the published spec
    outside the checkout entirely.

    The read half still resolves through the link, so the rewrite is computed and
    only the WRITE refuses — this reaches `_atomic_write_spec` rather than bailing
    out in the reader.

    Ablation: revert the call to
    `atomic_write_bytes(spec_path, payload, follow_symlinks=False)` and this fails
    `DID NOT RAISE`, with the victim spec reset out in `outside/`."""
    root, outside = _spec_tree(tmp_path)
    victim = _write(outside, "victim.md", _RESET_SPEC)
    (root / "artifacts").rmdir()
    (root / "artifacts").symlink_to(outside, target_is_directory=True)
    sp = root / "artifacts" / "victim.md"
    assert sp.is_file()  # the read still resolves through the planted link

    with pytest.raises(platform_util.UnconfinedWriteError):
        devcontract.reset_spec_status(sp, "in-progress", confine_root=root)

    assert victim.read_text(encoding="utf-8") == _RESET_SPEC  # not rewritten
    assert sorted(p.name for p in outside.iterdir()) == ["victim.md"]  # nor staged


def test_atomic_write_spec_refuses_a_readonly_spec(tmp_path):
    """#597 at this site. `os.replace` needs write permission on the parent
    DIRECTORY, never on the entry it replaces, so a spec an operator marked
    read-only was rewritten anyway and — because the mode is inherited — came back
    reading `0444`, with nothing in the permission bits to record it.

    `0o444` sets the READONLY attribute on win32 too, so this runs unskipped on
    both platforms; the chmod is on a file in this test's own tmp_path and is
    restored in a `finally` (Windows rmtree refuses a READONLY leftover).

    Ablation: drop `require_writable_target=True` from the confined call and this
    fails `DID NOT RAISE`, the spec reading `status: in-progress` and still
    `0444`."""
    root, _ = _spec_tree(tmp_path)
    sp = _write(root / "artifacts", "spec-a.md", _RESET_SPEC)
    sp.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            devcontract.reset_spec_status(sp, "in-progress", confine_root=root)
    finally:
        sp.chmod(0o644)

    assert sp.read_text(encoding="utf-8") == _RESET_SPEC
    assert list((root / "artifacts").glob("*.tmp")) == []  # a refusal stages nothing


# --------------------------------- has_operator_confirmation (#335 part 3)
#
# The section stopped being prose the moment `confirm` learned to RESUME an
# interrupted confirmation off it: the heading on disk is now the machine-readable
# acknowledgment. Writer literal and reader pattern must agree, and the reading
# must be fence-aware or a frozen intent quoting an example finishes a story
# nobody signed off.


def test_operator_confirm_heading_round_trips_through_its_own_reader(tmp_path):
    """The one test that would catch the writer and the reader drifting apart —
    they are two spellings of the same string and nothing else pins them."""
    assert devcontract.OPERATOR_CONFIRM_HEADING_RE.match(devcontract.OPERATOR_CONFIRM_HEADING)
    sp = _write(tmp_path, "spec-a.md", _PARKED)
    assert devcontract.has_operator_confirmation(sp) is False
    devcontract.append_operator_confirmation(sp, _ACTIONS, date="2026-07-28", confine_root=tmp_path)
    assert devcontract.has_operator_confirmation(sp) is True


def test_operator_confirmation_quoted_in_a_fence_is_not_an_acknowledgment(tmp_path):
    """#52's rule, on the path where getting it wrong is worst: a frozen intent
    showing what the section looks like is documentation. Reading it as structure
    would let `confirm` resume — advancing the board — for a human who never
    acknowledged anything."""
    sp = _write(
        tmp_path,
        "spec-a.md",
        _PARKED + "\n## Notes\n\n```markdown\n## Operator Confirmation\n\nexample\n```\n",
    )
    assert devcontract.has_operator_confirmation(sp) is False


def test_operator_confirmation_read_degrades_on_an_unreadable_spec(tmp_path):
    """Observation path: an absent or undecodable spec cannot be SHOWN to carry
    an acknowledgment, so it reads False and the caller's own read reports the
    real fault."""
    assert devcontract.has_operator_confirmation(tmp_path / "nope.md") is False
    binary = tmp_path / "spec-b.md"
    binary.write_bytes(b"---\nstatus: awaiting-operator\n---\n\n\xff\xfe## Operator Confirmation\n")
    assert devcontract.has_operator_confirmation(binary) is False


def test_auto_run_marker_is_not_read_as_an_operator_confirmation(tmp_path):
    """The two section readers share `_section_headings`; the pattern parameter is
    what keeps them distinct. Neither heading may answer for the other."""
    sp = _write(tmp_path, "spec-a.md", _PARKED)
    devcontract.append_auto_run_result(sp, "done", confine_root=tmp_path)
    assert devcontract.has_operator_confirmation(sp) is False


# ---------------------------------------- frontmatter `deferred:` harvest (#2640)


def _deferred_spec(path: Path, items) -> dict:
    """Read the real block-scalar serialization through the production parser."""
    from conftest import render_deferred

    path.write_text(
        f"---\nstatus: done\n{render_deferred(items)}---\n\n## Intent\n\ntest\n",
        encoding="utf-8",
    )
    return verify.read_frontmatter(path)


def test_parse_deferred_findings_absent_null_and_empty_are_empty(tmp_path):
    path = tmp_path / "absent.md"
    path.write_text("---\nstatus: done\n---\n\nbody\n", encoding="utf-8")
    assert "deferred" not in verify.read_frontmatter(path)
    assert devcontract.parse_deferred_findings(verify.read_frontmatter(path)) == ([], [])
    assert devcontract.parse_deferred_findings({"deferred": None}) == ([], [])
    assert devcontract.parse_deferred_findings(_deferred_spec(tmp_path / "empty.md", [])) == (
        [],
        [],
    )


def test_parse_deferred_findings_reads_real_block_scalar_shape(tmp_path):
    fm = _deferred_spec(
        tmp_path / "spec.md",
        [
            {
                "summary": "Retry loop can spin: no ceiling # really",
                "evidence": "first line\nsecond line: still evidence # data",
                "location": "src/retry.py:88",
                "severity": "medium",
            }
        ],
    )
    findings, malformed = devcontract.parse_deferred_findings(fm)
    assert malformed == []
    assert findings == [
        devcontract.DeferredFinding(
            summary="Retry loop can spin: no ceiling # really",
            evidence="first line second line: still evidence # data",
            location="src/retry.py:88",
            severity="medium",
            fingerprint=devcontract.harvest_fingerprint(
                "Retry loop can spin: no ceiling # really", "src/retry.py:88"
            ),
        )
    ]


def test_deferred_finding_is_frozen():
    finding = devcontract.DeferredFinding("summary", "evidence", "location", "high", "abc")
    with pytest.raises(FrozenInstanceError):
        setattr(finding, "summary", "changed")


def test_parse_deferred_findings_keeps_item_order(tmp_path):
    fm = _deferred_spec(
        tmp_path / "spec.md",
        [
            {"summary": "first", "evidence": "e1"},
            {"summary": "second", "evidence": "e2"},
            {"summary": "third", "evidence": "e3"},
        ],
    )
    findings, malformed = devcontract.parse_deferred_findings(fm)
    assert malformed == []
    assert [finding.summary for finding in findings] == ["first", "second", "third"]
    assert len({finding.fingerprint for finding in findings}) == 3


def test_parse_deferred_findings_defaults_optional_fields_and_coerces_scalars():
    findings, malformed = devcontract.parse_deferred_findings(
        {"deferred": [{"summary": 42}, {"summary": True, "evidence": False}]}
    )
    assert malformed == []
    assert [(f.summary, f.evidence, f.location, f.severity) for f in findings] == [
        ("42", "", "", ""),
        ("True", "False", "", ""),
    ]


@pytest.mark.parametrize("field", ["summary", "evidence", "location"])
@pytest.mark.parametrize("collection", [["nested", "values"], {"nested": "value"}])
def test_parse_deferred_findings_rejects_collection_valued_text_fields(field, collection):
    """A malformed optional field costs its whole item too: silently dropping
    it would harvest only part of an authored finding, while stringifying it
    would persist Python container repr in the ledger. Scalar siblings remain
    harvestable, including the numeric/bool normalization pinned above."""
    malformed_item = {"summary": "bad item", field: collection}
    findings, malformed = devcontract.parse_deferred_findings(
        {"deferred": [malformed_item, {"summary": "good sibling"}]}
    )

    assert [finding.summary for finding in findings] == ["good sibling"]
    assert malformed == [f"item 1: `{field}` is not a scalar (got {type(collection).__name__})"]


@pytest.mark.parametrize("field", ["summary", "evidence", "location"])
@pytest.mark.parametrize("value", ["before\0after", "x" * 1001 + "\0"])
def test_parse_deferred_findings_rejects_embedded_nul_in_text_fields(field, value):
    """Reject before clamping too, so a late NUL cannot evade the stated schema."""
    malformed_item = {"summary": "bad item", field: value}
    findings, malformed = devcontract.parse_deferred_findings(
        {"deferred": [malformed_item, {"summary": "good sibling"}]}
    )

    assert [finding.summary for finding in findings] == ["good sibling"]
    assert malformed == [f"item 1: `{field}` contains a NUL character"]


def test_parse_deferred_findings_normalizes_known_severity_and_drops_unknown(tmp_path):
    fm = _deferred_spec(
        tmp_path / "spec.md",
        [
            {"summary": "a", "severity": "blocker"},
            {"summary": "b", "severity": "Major"},
            {"summary": "c", "severity": "spicy"},
        ],
    )
    findings, malformed = devcontract.parse_deferred_findings(fm)
    assert malformed == []
    assert [finding.severity for finding in findings] == ["critical", "high", ""]


def test_parse_deferred_findings_isolates_each_malformed_item(tmp_path):
    fm = _deferred_spec(
        tmp_path / "spec.md",
        [
            {"summary": "good one", "evidence": "e"},
            "bare-scalar",
            {"evidence": "missing summary"},
            {"summary": "   ", "evidence": "blank summary"},
            {"summary": "good two", "evidence": "e"},
        ],
    )
    findings, malformed = devcontract.parse_deferred_findings(fm)
    assert [finding.summary for finding in findings] == ["good one", "good two"]
    assert malformed == [
        "item 2: not a mapping (got str)",
        "item 3: no usable `summary`",
        "item 4: no usable `summary`",
    ]


@pytest.mark.parametrize("raw", ["one finding", {"summary": "x"}, 3, True])
def test_parse_deferred_findings_reports_a_non_list_container(raw):
    findings, malformed = devcontract.parse_deferred_findings({"deferred": raw})
    assert findings == []
    assert malformed == [f"`deferred:` is not a list (got {type(raw).__name__})"]


def test_parse_deferred_findings_clamps_every_ledger_value(tmp_path):
    findings, malformed = devcontract.parse_deferred_findings(
        _deferred_spec(
            tmp_path / "spec.md",
            [{"summary": "s" * 500, "evidence": "e" * 4000, "location": "l" * 500}],
        )
    )
    assert malformed == []
    assert len(findings[0].summary) == 200
    assert len(findings[0].evidence) == 1000
    assert len(findings[0].location) == 200


def test_flatten_strips_a_join_space_at_the_clamp_boundary(tmp_path):
    padded = " ".join(["a" * 9] * 30)
    assert padded[:200].endswith(" ")
    assert devcontract._flatten(padded, 200) == padded[:200].strip()

    findings, _ = devcontract.parse_deferred_findings(
        _deferred_spec(tmp_path / "spec.md", [{"summary": "s", "location": padded}])
    )
    finding = findings[0]
    assert not finding.location.endswith(" ")
    assert finding.fingerprint == devcontract.harvest_fingerprint("s", finding.location)


def test_deferred_fingerprint_ignores_evidence_but_tracks_location(tmp_path):
    def fingerprint(path: Path, evidence: str, location: str) -> str:
        findings, _ = devcontract.parse_deferred_findings(
            _deferred_spec(
                path,
                [{"summary": "same finding", "evidence": evidence, "location": location}],
            )
        )
        return findings[0].fingerprint

    first = fingerprint(tmp_path / "a.md", "e1", "f.py:1")
    assert fingerprint(tmp_path / "b.md", "REWORDED", "f.py:1") == first
    assert fingerprint(tmp_path / "c.md", "e1", "f.py:2") != first


def test_deferred_fingerprint_uses_clamped_rendered_values(tmp_path):
    a, _ = devcontract.parse_deferred_findings(
        _deferred_spec(tmp_path / "a.md", [{"summary": "x" * 300 + "AAA"}])
    )
    b, _ = devcontract.parse_deferred_findings(
        _deferred_spec(tmp_path / "b.md", [{"summary": "x" * 300 + "BBB"}])
    )
    assert a[0].summary == b[0].summary
    assert a[0].fingerprint == b[0].fingerprint


def test_harvest_fingerprint_is_stable_and_nul_separates_parts():
    # Exact value pins SHA-1, NUL joining, and the 12-character truncation.
    assert devcontract.harvest_fingerprint("a", "b") == "4a3dec2d1f82"
    assert devcontract.harvest_fingerprint("ab", "c") != devcontract.harvest_fingerprint("a", "bc")
    assert len(devcontract.harvest_fingerprint("a", "b")) == 12


@pytest.mark.parametrize("parts", [("a\0b", "c"), ("a", "b\0c")])
def test_harvest_fingerprint_refuses_ambiguous_embedded_nul(parts):
    with pytest.raises(ValueError, match="must not contain NUL"):
        devcontract.harvest_fingerprint(*parts)


def test_harvest_fingerprint_marks_sha1_as_non_security_use(monkeypatch):
    real_sha1 = devcontract.hashlib.sha1
    seen = []

    def recording_sha1(payload, *, usedforsecurity):
        seen.append(usedforsecurity)
        return real_sha1(payload, usedforsecurity=usedforsecurity)

    monkeypatch.setattr(devcontract.hashlib, "sha1", recording_sha1)
    assert devcontract.harvest_fingerprint("a", "b") == "4a3dec2d1f82"
    assert seen == [False]
