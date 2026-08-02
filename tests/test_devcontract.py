"""Tests for the generic bmad-dev-auto -> result.json translation shim."""

import os
from pathlib import Path

import pytest

from bmad_loop import devcontract
from bmad_loop.verify import read_frontmatter


def _spec(
    path: Path,
    *,
    status: str = "done",
    baseline_field: str = "baseline_revision",
    baseline: str = "abc123def456abc123def456abc123def456abcd",
    auto_run: str | None = "done",
    body_extra: str = "",
    followup: bool | None = None,
) -> Path:
    fm = f"---\ntitle: 'x'\ntype: 'feature'\nstatus: '{status}'\n"
    if baseline:
        fm += f"{baseline_field}: '{baseline}'\n"
    if followup is not None:
        fm += f"followup_review_recommended: {str(followup).lower()}\n"
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
    # `<skill>-result-` filename prefix instead. BOTH eras are matched: the
    # artifact is named after whichever skill wrote it (BMAD-METHOD #2651 renamed
    # bmad-dev-auto to bmad-build-auto), and a run can meet either.
    fallback = tmp_path / f"{prefix}unclear-1234.md"
    fallback.write_text(
        "---\nstatus: blocked\n---\n\nBlocking condition: unclear intent\n",
        encoding="utf-8",
    )
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) == fallback


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
    assert devcontract.reset_spec_status(sp, "in-progress") is True
    assert "status: 'in-progress' # draft | ready-for-dev | done\n" in sp.read_text()
    # nothing else moved
    assert "review_loop_iteration: 2\n" in sp.read_text()


def test_reset_status_unquoted(tmp_path):
    sp = tmp_path / "spec.md"
    sp.write_text("---\nstatus: done\n---\n\nbody\n", encoding="utf-8")
    assert devcontract.reset_spec_status(sp, "in-progress") is True
    assert "status: in-progress\n" in sp.read_text()


def test_reset_status_leaves_prose_status_line_untouched(tmp_path):
    """Only the frontmatter status is rewritten — a `Status:` line in the
    ## Auto Run Result prose body must survive verbatim."""
    sp = _spec(tmp_path / "spec.md", status="done", auto_run="done")
    devcontract.reset_spec_status(sp, "in-progress")
    text = sp.read_text()
    assert "status: 'in-progress'\n" in text  # frontmatter flipped
    assert "- Status: done\n" in text  # prose untouched


def test_reset_status_idempotent_no_write(tmp_path):
    sp = tmp_path / "spec.md"
    sp.write_text("---\nstatus: 'in-progress'\n---\n\nbody\n", encoding="utf-8")
    before = sp.stat().st_mtime_ns
    assert devcontract.reset_spec_status(sp, "in-progress") is False
    assert sp.stat().st_mtime_ns == before  # no rewrite at all


def test_reset_status_no_frontmatter(tmp_path):
    sp = tmp_path / "spec.md"
    sp.write_text("# just a heading\n\nstatus: done\n", encoding="utf-8")
    assert devcontract.reset_spec_status(sp, "in-progress") is False
    assert "status: done\n" in sp.read_text()  # body status not touched


def test_reset_spec_status_noop_when_spec_absent(tmp_path):
    """A re-drive against a spec that no longer exists on disk no-ops cleanly
    rather than raising (mirrors verify.set_frontmatter_status)."""
    sp = tmp_path / "missing.md"
    assert not sp.exists()
    assert devcontract.reset_spec_status(sp, "in-progress") is False


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
    assert devcontract.reset_spec_status(sp, "done") is True
    text = sp.read_text()
    assert "status: 'done'\n" in text  # frontmatter advanced
    assert "- Status: done\n" in text  # prose untouched


def test_reset_status_fills_empty_value(tmp_path):
    """The "" allowlist member: a present-but-empty `status:` is filled in place,
    leaving the prose Status line untouched."""
    sp = _spec(tmp_path / "spec.md", status="", auto_run="done")
    assert devcontract.reset_spec_status(sp, "done") is True
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
    assert devcontract.reset_spec_status(sp, "done") is True
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
    assert devcontract.reset_spec_status(sp, "done") is True
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
    assert devcontract.reset_spec_status(sp, "done") is True
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
    assert devcontract.strip_auto_run_result(sp) is True
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
    assert devcontract.strip_auto_run_result(sp) is True
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
    assert devcontract.strip_auto_run_result(sp) is True
    text = sp.read_text()
    assert "Auto Run Result" not in text
    assert "##\n\nkept after empty heading\n" in text


def test_strip_auto_run_result_noop_without_section(tmp_path):
    sp = tmp_path / "spec.md"
    original = "---\nstatus: draft\n---\n\n## Intent\n\nbody\n"
    sp.write_text(original, encoding="utf-8")
    assert devcontract.strip_auto_run_result(sp) is False
    assert sp.read_text() == original


def test_strip_auto_run_result_noop_when_spec_absent(tmp_path):
    """The re-arm path calls strip after flipping frontmatter status; if the spec
    was removed out from under the run the strip no-ops cleanly rather than crashing
    the re-drive (only an absent file is guarded — a present-but-unreadable spec
    still raises so the stale section can't silently survive the re-open)."""
    sp = tmp_path / "missing.md"
    assert not sp.exists()
    assert devcontract.strip_auto_run_result(sp) is False


def test_strip_auto_run_result_raises_on_undecodable_spec(tmp_path):
    """Repair-write doctrine: a present-but-unreadable spec must RAISE, never
    no-op. Only an absent file is guarded; a spec that cannot be decoded is left
    to raise so the stale terminal section can't silently survive the re-open —
    the #160 review-launch strip site depends on that surfacing (silently skipping
    the strip recreates the exact bug it fixes)."""
    sp = tmp_path / "spec.md"
    sp.write_bytes(b"---\nstatus: done\n---\n\n\xff\xfe## Auto Run Result\n\nStatus: done\n")
    with pytest.raises(UnicodeDecodeError):
        devcontract.strip_auto_run_result(sp)


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
    assert devcontract.strip_auto_run_result(sp) is False
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
    assert devcontract.strip_auto_run_result(sp) is False
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
    assert devcontract.strip_auto_run_result(sp) is False
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
    assert devcontract.strip_auto_run_result(sp) is True
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
    assert devcontract.strip_auto_run_result(sp) is False
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
    assert devcontract.strip_auto_run_result(sp) is False
    assert sp.read_text() == original


# ---------------------------------------- frontmatter `deferred:` harvest (#2640)
#
# Every case reads through the REAL `verify.read_frontmatter` the orchestrator
# uses, over a spec written by the real `conftest.render_deferred` block-scalar
# renderer. Parsing a hand-built dict instead would prove only that Python
# dictionaries have keys — the contract under test is that upstream's `>-`/`|-`
# serialization survives YAML into flat, ledger-shaped values.


def _deferred_spec(path: Path, items, status: str = "done") -> dict:
    from conftest import write_spec

    write_spec(path, status, "abc123", deferred=items)
    return read_frontmatter(path)


def test_parse_deferred_findings_absent_field_is_empty(tmp_path):
    """A pre-#2640 spec has no `deferred:` key at all. Indistinguishable from
    "nothing was deferred" on purpose — the harvest is keyed on content, never on
    which skill era wrote the spec."""
    from conftest import write_spec

    sp = tmp_path / "spec.md"
    write_spec(sp, "done", "abc123")
    assert "deferred" not in read_frontmatter(sp)
    assert devcontract.parse_deferred_findings(read_frontmatter(sp)) == ([], [])


def test_parse_deferred_findings_empty_and_null_lists_are_empty(tmp_path):
    """`deferred: []` (the spec template's default) and an explicit YAML null are
    both "nothing deferred", not malformed input."""
    assert devcontract.parse_deferred_findings(_deferred_spec(tmp_path / "a.md", [])) == ([], [])
    assert devcontract.parse_deferred_findings({"deferred": None}) == ([], [])


def test_parse_deferred_findings_single_item_full_shape(tmp_path):
    fm = _deferred_spec(
        tmp_path / "spec.md",
        [
            {
                "summary": "Retry loop can spin: no ceiling # really",
                "evidence": "the backoff doubles forever",
                "location": "src/retry.py:88",
                "severity": "medium",
            }
        ],
    )
    findings, malformed = devcontract.parse_deferred_findings(fm)
    assert malformed == []
    assert len(findings) == 1
    f = findings[0]
    # the block scalar kept `:` and `#` as data — they are not YAML structure
    assert f.summary == "Retry loop can spin: no ceiling # really"
    assert f.evidence == "the backoff doubles forever"
    assert f.location == "src/retry.py:88"
    assert f.severity == "medium"
    assert len(f.fingerprint) == 12


def test_parse_deferred_findings_multiple_items_keep_order(tmp_path):
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
    assert [f.summary for f in findings] == ["first", "second", "third"]
    # distinct summaries ⇒ distinct identities, so three ledger entries
    assert len({f.fingerprint for f in findings}) == 3


def test_parse_deferred_findings_flattens_multiline_block_scalars(tmp_path):
    """`|-` keeps newlines, which a single ledger field line cannot. Flatten to
    one whitespace-collapsed line: a raw newline inside `reason:` would end the
    field as far as every ledger reader is concerned."""
    fm = _deferred_spec(
        tmp_path / "spec.md",
        [{"summary": "summary line", "evidence": "first line\nsecond line\n\nfourth"}],
    )
    findings, _ = devcontract.parse_deferred_findings(fm)
    assert findings[0].evidence == "first line second line fourth"
    assert "\n" not in findings[0].evidence


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


def test_flatten_leaves_no_trailing_space_when_the_clamp_lands_on_one(tmp_path):
    """The clamp runs after the join, so the cut can land on a join space — the
    one way a "collapsed single line" comes back with trailing whitespace.

    This is not cosmetic, and it is not the ledger writer's problem to mop up:
    `location` is consumed TWICE from here — `harvest_fingerprint(summary,
    location)` becomes the entry's `origin:` dedup key, and the same value is
    written as the entry's `location:` line. `append_entry` strips what it
    writes, so cleaning it only there would leave the key derived from a string
    the ledger no longer contains — a fingerprint no reader of the entry could
    reproduce. Stripping at the source keeps both consumers on one value.

    The fixture has to build the collision by hand: it takes a value longer than
    `_LOCATION_LIMIT` whose 200th character is a join space, which no short
    location can express."""
    padded = " ".join(["a" * 9] * 30)  # 30 words -> the 200-char cut lands on a space
    assert devcontract._flatten(padded, devcontract._LOCATION_LIMIT) == padded[:200].strip()
    assert padded[:200].endswith(" "), "fixture no longer builds the failing shape"

    fm = _deferred_spec(tmp_path / "spec.md", [{"summary": "s", "location": padded}])
    findings, _ = devcontract.parse_deferred_findings(fm)
    assert not findings[0].location.endswith(" ")
    # the two consumers agree: the key is derivable from the value the ledger keeps
    assert findings[0].fingerprint == devcontract.harvest_fingerprint("s", findings[0].location)


def test_parse_deferred_findings_optional_fields_default_empty(tmp_path):
    fm = _deferred_spec(tmp_path / "spec.md", [{"summary": "only a summary"}])
    findings, malformed = devcontract.parse_deferred_findings(fm)
    assert malformed == []
    assert findings[0].evidence == "" and findings[0].location == ""
    assert findings[0].severity == ""


def test_parse_deferred_findings_normalizes_and_drops_severity(tmp_path):
    """Severity goes through the ledger's own alias table, so `blocker`/`Major`
    normalize and a token no ledger reader knows becomes "" (the format doc's
    documented "unspecified") rather than being written through verbatim."""
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
    assert [f.severity for f in findings] == ["critical", "high", ""]


def test_parse_deferred_findings_isolates_malformed_items(tmp_path):
    """A mangled item must cost only itself. Its well-formed siblings on BOTH
    sides still parse — the whole point of reporting malformed items separately
    instead of failing the list."""
    fm = _deferred_spec(
        tmp_path / "spec.md",
        [
            {"summary": "good one", "evidence": "e"},
            "a bare string, not a mapping",
            {"evidence": "an item with no summary"},
            {"summary": "   ", "evidence": "whitespace-only summary"},
            {"summary": "good two", "evidence": "e"},
        ],
    )
    findings, malformed = devcontract.parse_deferred_findings(fm)
    assert [f.summary for f in findings] == ["good one", "good two"]
    assert len(malformed) == 3
    assert "item 2" in malformed[0] and "not a mapping" in malformed[0]
    assert "item 3" in malformed[1] and "summary" in malformed[1]
    assert "item 4" in malformed[2]


def test_parse_deferred_findings_non_list_value_is_malformed(tmp_path):
    """The skill wrote a scalar (or a mapping) where the contract says list.
    Nothing is harvestable, but the loss must be reported, not silently zero."""
    findings, malformed = devcontract.parse_deferred_findings({"deferred": "one finding"})
    assert findings == []
    assert len(malformed) == 1 and "not a list" in malformed[0]
    assert devcontract.parse_deferred_findings({"deferred": {"summary": "x"}})[1] != []


def test_parse_deferred_findings_clamps_long_values(tmp_path):
    """The summary becomes a `### DW-n:` heading and the rest become field
    lines, so every value is bounded. Clamping happens before fingerprinting —
    see the stability test below."""
    fm = _deferred_spec(
        tmp_path / "spec.md",
        [{"summary": "s" * 500, "evidence": "e" * 4000, "location": "l" * 500}],
    )
    findings, _ = devcontract.parse_deferred_findings(fm)
    assert len(findings[0].summary) == 200
    assert len(findings[0].evidence) == 1000
    assert len(findings[0].location) == 200


def test_deferred_fingerprint_stable_across_reparse_and_evidence_edits(tmp_path):
    """The fingerprint is the ledger's dedup key, so it must be reproducible from
    a re-read of the same spec — and must NOT move when only the evidence prose
    changes, or a review pass rewording its own justification would re-file the
    same finding as a second entry."""
    first, _ = devcontract.parse_deferred_findings(
        _deferred_spec(
            tmp_path / "a.md", [{"summary": "same finding", "evidence": "e1", "location": "f.py:1"}]
        )
    )
    again, _ = devcontract.parse_deferred_findings(
        _deferred_spec(
            tmp_path / "b.md", [{"summary": "same finding", "evidence": "e1", "location": "f.py:1"}]
        )
    )
    reworded, _ = devcontract.parse_deferred_findings(
        _deferred_spec(
            tmp_path / "c.md",
            [{"summary": "same finding", "evidence": "REWORDED", "location": "f.py:1"}],
        )
    )
    moved, _ = devcontract.parse_deferred_findings(
        _deferred_spec(
            tmp_path / "d.md", [{"summary": "same finding", "evidence": "e1", "location": "f.py:2"}]
        )
    )
    assert first[0].fingerprint == again[0].fingerprint == reworded[0].fingerprint
    # location is part of the identity: the same complaint about a different site
    # is a different piece of work
    assert moved[0].fingerprint != first[0].fingerprint


def test_deferred_fingerprint_clamped_summaries_do_not_collide_by_accident(tmp_path):
    """Two summaries that differ only past the clamp point render as the SAME
    ledger entry, so they must share one identity — otherwise the ledger grows a
    fresh visually-identical entry on every harvest."""
    long_a = {"summary": "x" * 300 + "AAA"}
    long_b = {"summary": "x" * 300 + "BBB"}
    a, _ = devcontract.parse_deferred_findings(_deferred_spec(tmp_path / "a.md", [long_a]))
    b, _ = devcontract.parse_deferred_findings(_deferred_spec(tmp_path / "b.md", [long_b]))
    assert a[0].summary == b[0].summary
    assert a[0].fingerprint == b[0].fingerprint


def test_harvest_fingerprint_is_nul_joined(tmp_path):
    """Parts are NUL-separated so no part can impersonate a boundary: ("ab","c")
    and ("a","bc") are different identities."""
    assert devcontract.harvest_fingerprint("ab", "c") != devcontract.harvest_fingerprint("a", "bc")
    assert devcontract.harvest_fingerprint("a", "b") == devcontract.harvest_fingerprint("a", "b")
