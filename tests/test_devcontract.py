"""Tests for the generic bmad-dev-auto -> result.json translation shim."""

from pathlib import Path

import pytest

from bmad_loop import devcontract


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


def test_find_artifact_accepts_no_spec_fallback_prefix(tmp_path):
    # The no-spec fallback (intent too unclear to create a spec) carries a terminal
    # frontmatter status but NO `## Auto Run Result` heading — it is matched by its
    # `bmad-dev-auto-result-` filename prefix instead.
    fallback = tmp_path / "bmad-dev-auto-result-unclear-1234.md"
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
