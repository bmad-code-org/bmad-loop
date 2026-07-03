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
