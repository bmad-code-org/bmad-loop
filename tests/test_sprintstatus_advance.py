"""Tests for the orchestrator-owned sprint-status writer (generic-skill path)."""

from pathlib import Path

from bmad_loop import sprintstatus

SPRINT = """\
# Sprint status — do not hand-edit casually
generated: 01-06-2026 10:00
last_updated: 01-06-2026 10:00

# STATUS DEFINITIONS
#   backlog -> ready-for-dev -> in-progress -> review -> done
development_status:
  epic-3: backlog
  3-1-login: done
  3-2-digest-delivery: backlog  # the next story
  epic-4: in-progress
  4-1-thing: review

# WORKFLOW NOTES
# keep these comments
"""


def _write(tmp_path: Path) -> Path:
    p = tmp_path / "sprint-status.yaml"
    p.write_text(SPRINT, encoding="utf-8")
    return p


def test_advance_to_in_progress_lifts_backlog_epic(tmp_path):
    p = _write(tmp_path)
    out = sprintstatus.advance(p, "3-2-digest-delivery", "in-progress")
    assert out == "in-progress"
    assert sprintstatus.story_status(p, "3-2-digest-delivery") == "in-progress"
    assert sprintstatus.load(p).epics[3] == "in-progress"  # epic lifted


def test_advance_preserves_comments_and_structure(tmp_path):
    p = _write(tmp_path)
    sprintstatus.advance(p, "3-2-digest-delivery", "in-progress")
    text = p.read_text()
    assert "# STATUS DEFINITIONS" in text
    assert "# WORKFLOW NOTES" in text
    assert "# the next story" in text  # inline comment survived
    assert "# keep these comments" in text


def test_advance_never_regresses(tmp_path):
    p = _write(tmp_path)
    out = sprintstatus.advance(p, "4-1-thing", "in-progress")  # currently review
    assert out == "review"
    assert sprintstatus.story_status(p, "4-1-thing") == "review"


def test_advance_returns_current_when_line_not_rewritable(tmp_path):
    """A quoted story key parses via YAML (story_status finds it) but the line-edit
    writer can't rewrite it. advance() must report the unchanged status, not falsely
    claim it reached target, and must leave the file untouched."""
    text = (
        "last_updated: 01-06-2026 10:00\n"
        "development_status:\n"
        "  epic-5: in-progress\n"
        "  '5-1-quoted': ready-for-dev\n"
    )
    p = tmp_path / "sprint-status.yaml"
    p.write_text(text, encoding="utf-8")
    before = p.read_text()

    out = sprintstatus.advance(p, "5-1-quoted", "in-progress", now="02-06-2026 09:00")

    assert out == "ready-for-dev"  # current status, not the requested target
    assert p.read_text() == before  # nothing rewritten — not even last_updated


def test_advance_idempotent_done(tmp_path):
    p = _write(tmp_path)
    out = sprintstatus.advance(p, "3-1-login", "done")  # already done
    assert out == "done"
    assert sprintstatus.story_status(p, "3-1-login") == "done"


def test_advance_to_review(tmp_path):
    p = _write(tmp_path)
    out = sprintstatus.advance(p, "3-2-digest-delivery", "review")
    assert out == "review"
    assert sprintstatus.story_status(p, "3-2-digest-delivery") == "review"
    # epic NOT lifted for non-in-progress targets
    assert sprintstatus.load(p).epics[3] == "backlog"


def test_advance_done_does_not_touch_epic(tmp_path):
    p = _write(tmp_path)
    sprintstatus.advance(p, "3-2-digest-delivery", "done")
    assert sprintstatus.load(p).epics[3] == "backlog"


def test_advance_epic_not_lifted_when_not_backlog(tmp_path):
    p = _write(tmp_path)
    sprintstatus.advance(p, "4-1-thing", "in-progress")  # regresses -> no-op anyway
    # epic-4 was in-progress; ensure unchanged
    assert sprintstatus.load(p).epics[4] == "in-progress"


def test_advance_refreshes_last_updated(tmp_path):
    p = _write(tmp_path)
    sprintstatus.advance(p, "3-2-digest-delivery", "in-progress", now="22-06-2026 14:30")
    text = p.read_text()
    assert "last_updated: 22-06-2026 14:30" in text
    assert "generated: 01-06-2026 10:00" in text  # generated untouched


def test_advance_story_not_found(tmp_path):
    p = _write(tmp_path)
    assert sprintstatus.advance(p, "9-9-ghost", "in-progress") is None


def test_advance_missing_file(tmp_path):
    assert sprintstatus.advance(tmp_path / "ghost.yaml", "3-2-x", "in-progress") is None
