"""Tests for the orchestrator-owned sprint-status writer (generic-skill path)."""

import sys
from pathlib import Path

import pytest
import yaml

from bmad_loop import sprintstatus
from bmad_loop.platform_util import atomic_write_bytes as real_atomic_write_bytes

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


def test_advance_split_story_lifts_backlog_epic(tmp_path):
    # a split-story key (issue #144) must advance and lift its epic like any other
    text = (
        "last_updated: 01-06-2026 10:00\n"
        "development_status:\n"
        "  epic-2: backlog\n"
        "  2-6a-build-structure: backlog\n"
        "  2-6b-extend-structure: backlog\n"
    )
    p = tmp_path / "sprint-status.yaml"
    p.write_text(text, encoding="utf-8")
    out = sprintstatus.advance(p, "2-6a-build-structure", "in-progress")
    assert out == "in-progress"
    assert sprintstatus.story_status(p, "2-6a-build-structure") == "in-progress"
    assert sprintstatus.load(p).epics[2] == "in-progress"  # epic lifted
    assert sprintstatus.story_status(p, "2-6b-extend-structure") == "backlog"  # sibling untouched


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


def test_advance_confirms_a_parked_story_forward_to_done(tmp_path):
    """The exit move `bmad-loop confirm` will need: because `awaiting-operator`
    sits below `done` in STATUS_ORDER, completing a parked story is an ordinary
    forward advance through the sole writer — no invariant exception required."""
    p = _write(tmp_path)
    sprintstatus.advance(p, "3-2-digest-delivery", "awaiting-operator")
    assert sprintstatus.story_status(p, "3-2-digest-delivery") == "awaiting-operator"

    out = sprintstatus.advance(p, "3-2-digest-delivery", "done")

    assert out == "done"
    assert sprintstatus.story_status(p, "3-2-digest-delivery") == "done"


def test_advance_never_regresses_done_into_awaiting_operator(tmp_path):
    """The other half of the ordering: once a story is `done`, nothing walks the
    board back to `awaiting-operator`. This is a real hardening, not a restatement
    — before the token joined STATUS_ORDER it was unordered, so the never-regress
    guard's `target in STATUS_ORDER` arm short-circuited and this write went
    through. (Demoting a done story is Phase 4's `operator.on_review_demotion`
    question, and it will need its own deliberate, allowlisted writer.)"""
    p = _write(tmp_path)
    before = p.read_text()

    out = sprintstatus.advance(p, "3-1-login", "awaiting-operator")  # already done

    assert out == "done"
    assert p.read_text() == before


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


# ================================================= the value / comment split
#
# #366. `_set_mapping_value` decides where a line's scalar ends and a trailing
# inline comment begins, and NOTHING checks that guess afterwards: `advance` has
# no oracle at all, and the one a caller might reach for cannot see this class of
# error anyway — `yaml.safe_load` strips comments before it could compare, so a
# line rewritten with a comment invented out of the tail of a quoted value
# re-parses as a perfectly clean `3-2-x: done`. (Proven by ablation on the sibling
# defect, PR #365, whose three verification gates all passed the fabricated
# comment.) The pattern is therefore the gate here, and these tests hold it.
#
# Called directly rather than through `advance` wherever the shape under test is
# a REFUSAL: `advance` answers a refused line and a story already at target with
# the same unchanged status, so only the writer's own return separates them.
# Every assertion is on the FULL resulting text — a substring or a re-parse is
# blind to exactly the fabrication these are here to catch.


def test_a_hash_inside_a_quoted_value_never_becomes_a_comment(tmp_path):
    """The case #366 is about, end to end through the sole writer. `"a # b"`
    carries no comment — the `#` is scalar text — so a split guessed from the last
    ` #` on the line writes `3-2-x: done # b"`, promoting the tail of the value
    into a comment the board never had and truncating the value it came from. A
    quote-led remainder is taken whole instead, which drops nothing that was
    ever a comment.

    Compares the full resulting TEXT, not its bytes. Full-content equality is the
    point — a substring or a re-parse is blind to a fabricated comment — while
    the dedicated #576 rows below own byte-exact line-ending preservation. Keeping
    this oracle text-focused avoids putting a second, accidental contract on the
    row that guards only the value/comment split."""
    p = tmp_path / "sprint-status.yaml"
    board = (
        'last_updated: 01-06-2026 10:00\ndevelopment_status:\n  3-2-x: "a # b"\n  3-3-y: backlog\n'
    )
    p.write_text(board, encoding="utf-8")

    assert sprintstatus.advance(p, "3-2-x", "done") == "done"

    assert p.read_text(encoding="utf-8") == board.replace('"a # b"', "done")


def test_a_quoted_value_is_replaced_whole_with_no_comment_carried(tmp_path):
    """The writer's own half of the case above: the write SUCCEEDS (a quoted
    hand-edit is still a value the orchestrator owns and replaces), and what it
    leaves behind is the bare target and nothing else."""
    lines = ['  3-2-x: "a # b"  # real comment\n']

    assert sprintstatus._set_mapping_value(lines, "3-2-x", "done") is True

    # the trailing comment goes too: nothing here can tell a closing quote from
    # a quote inside the scalar, so a comment after one is dropped, not guessed.
    assert "".join(lines) == "  3-2-x: done\n"


def test_a_value_with_internal_spaces_is_matched_whole(tmp_path):
    """Why this board cannot borrow `frontmatter._VALUE_COMMENT_RE`'s
    conservative token class: `last_updated` is a bare scalar WITH SPACES, and a
    token gate would refuse it — the timestamp refresh would silently stop
    happening (`test_advance_refreshes_last_updated` is the advance-level half)."""
    lines = ["last_updated: 01-06-2026 10:00\n"]

    assert sprintstatus._set_mapping_value(lines, "last_updated", "22-06-2026 14:30") is True

    assert "".join(lines) == "last_updated: 22-06-2026 14:30\n"


def test_an_inline_comment_carries_with_its_authored_separator(tmp_path):
    """The preservation the split exists to make possible, unchanged by #366: an
    unquoted value cedes the FIRST whitespace-preceded `#`, and the whitespace
    that separates it comes through as authored (two spaces here), so a
    hand-aligned comment column is not reflowed by a status flip."""
    lines = ["  3-2-digest-delivery: backlog  # the next story\n"]

    assert sprintstatus._set_mapping_value(lines, "3-2-digest-delivery", "in-progress") is True

    assert "".join(lines) == "  3-2-digest-delivery: in-progress  # the next story\n"


def test_a_hash_glued_to_the_value_stays_part_of_the_value(tmp_path):
    """YAML needs whitespace before a `#` for it to open a comment, so
    `backlog#x` is the single scalar `backlog#x`. The value is replaced whole and
    `#x` is not carried forward as a comment the board never had."""
    lines = ["  3-2-x: backlog#x\n"]

    assert sprintstatus._set_mapping_value(lines, "3-2-x", "done") is True

    assert "".join(lines) == "  3-2-x: done\n"


def test_a_line_with_trailing_whitespace_and_no_comment_is_refused(tmp_path):
    """Characterization, not a requirement — but pinned so the split cannot
    change it by accident. Both arms end at a non-space character, so a value
    with trailing whitespace and no comment is a remainder neither can account
    for, and the line is left exactly as authored rather than rewritten a few
    invisible characters shorter. `advance` then reports the unchanged status
    (`test_advance_returns_current_when_line_not_rewritable` is that half)."""
    trailing = "  3-2-x: backlog  \n"
    quoted_trailing = "  3-2-x: 'backlog' \n"

    for line in (trailing, quoted_trailing):
        lines = [line]
        assert sprintstatus._set_mapping_value(lines, "3-2-x", "done") is False
        assert "".join(lines) == line


# --------------------------------------------------- line-ending preservation (#576)


def test_a_crlf_board_keeps_every_crlf_and_only_intended_values_change(tmp_path):
    """POSIX oracle for the raw-read, CRLF matcher, and per-line emit sites."""
    board = (
        b"# Sprint status\r\n"
        b"last_updated: 01-06-2026 10:00\r\n"
        b"development_status:\r\n"
        b"  epic-3: backlog\r\n"
        b"  3-1-login: backlog\r\n"
        b"  3-2-finished: done\r\n"
    )
    expected = board.replace(b"  epic-3: backlog\r\n", b"  epic-3: in-progress\r\n").replace(
        b"  3-1-login: backlog\r\n", b"  3-1-login: in-progress\r\n"
    )
    p = tmp_path / "sprint-status.yaml"
    p.write_bytes(board)

    assert sprintstatus.advance(p, "3-1-login", "in-progress") == "in-progress"

    actual = p.read_bytes()
    assert actual == expected
    assert b"\n" not in actual.replace(b"\r\n", b"")  # no bare LF was introduced
    assert sprintstatus.story_status(p, "3-1-login") == "in-progress"
    assert sprintstatus.load(p).epics[3] == "in-progress"


def test_an_lf_board_keeps_every_lf_and_only_intended_values_change(tmp_path):
    """Windows-CI oracle for the old translating text-writer half of #576."""
    board = (
        b"# Sprint status\n"
        b"last_updated: 01-06-2026 10:00\n"
        b"development_status:\n"
        b"  epic-3: backlog\n"
        b"  3-1-login: backlog\n"
        b"  3-2-finished: done\n"
    )
    expected = board.replace(b"  epic-3: backlog\n", b"  epic-3: in-progress\n").replace(
        b"  3-1-login: backlog\n", b"  3-1-login: in-progress\n"
    )
    p = tmp_path / "sprint-status.yaml"
    p.write_bytes(board)

    assert sprintstatus.advance(p, "3-1-login", "in-progress") == "in-progress"

    actual = p.read_bytes()
    assert actual == expected
    assert b"\r" not in actual


def test_a_mixed_ending_board_keeps_each_line_its_own_ending(tmp_path):
    """POSIX oracle spanning the raw-read and per-line emit sites with all endings."""
    board = (
        b"last_updated: 01-06-2026 10:00\r\n"
        b"development_status:\r\n"
        b"  epic-3: backlog\n"
        b"  3-1-login: backlog\r"
        b"  3-2-untouched: backlog\r\n"
    )
    now = "22-06-2026 14:30"
    expected = (
        board.replace(
            b"last_updated: 01-06-2026 10:00\r\n",
            b"last_updated: 22-06-2026 14:30\r\n",
        )
        .replace(b"  epic-3: backlog\n", b"  epic-3: in-progress\n")
        .replace(b"  3-1-login: backlog\r", b"  3-1-login: in-progress\r")
    )
    p = tmp_path / "sprint-status.yaml"
    p.write_bytes(board)

    assert sprintstatus.advance(p, "3-1-login", "in-progress", now=now) == "in-progress"

    actual = p.read_bytes()
    assert actual == expected
    assert sprintstatus.story_status(p, "3-1-login") == "in-progress"
    assert sprintstatus.load(p).epics[3] == "in-progress"
    assert yaml.safe_load(actual.decode("utf-8"))["last_updated"] == now


def test_advance_sends_bytes_to_the_atomic_writer(tmp_path, monkeypatch):
    """All-platform oracle for the atomic-writer binding and payload type site."""
    board = (
        b"last_updated: 01-06-2026 10:00\r\n"
        b"development_status:\r\n"
        b"  epic-3: backlog\r\n"
        b"  3-1-login: backlog\r\n"
    )
    p = tmp_path / "sprint-status.yaml"
    p.write_bytes(board)
    writes: list[bytes | str] = []

    def record(path: Path, payload: bytes | str, *, follow_symlinks: bool = True) -> None:
        writes.append(payload)
        raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        real_atomic_write_bytes(path, raw, follow_symlinks=follow_symlinks)

    # Wrap both names so the writer-choice ablation still reaches the payload
    # assertion instead of escaping through whichever module binding it restores.
    monkeypatch.setattr(sprintstatus, "atomic_write_bytes", record, raising=False)
    monkeypatch.setattr(sprintstatus, "atomic_write_text", record, raising=False)

    assert sprintstatus.advance(p, "3-1-login", "in-progress") == "in-progress"

    assert len(writes) == 1
    payload = writes[0]
    assert isinstance(payload, bytes)
    assert b"  epic-3: in-progress\r\n" in payload
    assert b"  3-1-login: in-progress\r\n" in payload


# ------------------------------------------------------------ atomic rewrite (#379)


def test_a_truncated_board_still_parses_and_yields_fewer_keys(tmp_path):
    """The PRE-FIX failure mode this phase exists to make unreachable, pinned as a
    property of the FILE FORMAT rather than of the writer — deliberately green on
    both sides of the fix, because nothing else in the suite states why the board
    needed atomicity more than the ledgers did.

    A ledger cut mid-write reads as an obviously shorter ledger. A board cut at a
    line boundary is still a valid YAML mapping with a valid `development_status`,
    so `load` RAISES NOTHING: the epics past the tear simply cease to exist, and
    AGENTS.md makes `advance` the sole write path, so nothing downstream is holding
    a second copy that would contradict the shortened one. The run walks off the end
    of the sprint instead of erroring."""
    p = _write(tmp_path)
    whole = sprintstatus.load(p)
    assert set(whole.epics) == {3, 4} and len(whole.stories) == 3

    torn = SPRINT[: SPRINT.index("  epic-4:")]  # a short write ending at a line boundary
    p.write_text(torn, encoding="utf-8")

    shrunk = sprintstatus.load(p)  # NOT a SprintStatusError — that is the whole problem
    assert set(shrunk.epics) == {3}  # epic 4 silently gone
    assert len(shrunk.stories) == 2 and sprintstatus.story_status(p, "4-1-thing") is None


def test_advance_write_failure_raises_and_leaves_the_board_entire(tmp_path, monkeypatch):
    """#379. `advance` is a read-modify-rewrite, so the truncating `write_text` it
    replaced could publish the prefix the row above characterizes. The helper writes
    a temp and replaces it, so a fault leaves the original whole, and the raise still
    reaches the caller — repair writes must raise (AGENTS.md).

    Patched at sprintstatus' OWN binding of the helper, never `Path.write_text`: the
    helper writes through an `mkstemp` fd via `os.fdopen`, so a `Path` patch never
    fires and this would pass having exercised nothing.

    Ablation: restore `path.write_text(...)` at the call site and this reddens
    alone."""
    p = _write(tmp_path)
    before = p.read_bytes()

    def boom(path, data: bytes, *, follow_symlinks=True):
        raise OSError("no space left on device")

    monkeypatch.setattr(sprintstatus, "atomic_write_bytes", boom)
    with pytest.raises(OSError, match="no space left"):
        sprintstatus.advance(p, "3-2-digest-delivery", "in-progress")

    assert p.read_bytes() == before
    assert b"3-2-digest-delivery: in-progress" not in p.read_bytes()  # the lost mutation


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_advance_writes_through_a_symlinked_board(tmp_path):
    """The row that grades this SITE's `follow_symlinks` argument — the DEFAULT
    here, unlike the three spec writers, which pass False to match the
    name-replacing `atomic_replace` they already had.

    The default is what preserves behaviour: `write_text` opened through a link, so
    a repo that keeps its board outside the tree and symlinks it in kept being a
    symlink. Replacing the NAME instead would silently orphan the real file on the
    first advance and leave the operator editing a board nothing reads.

    Ablation: pass `follow_symlinks=False` at the call and this reddens alone."""
    real = tmp_path / "elsewhere" / "sprint-status.yaml"
    real.parent.mkdir()
    real.write_text(SPRINT, encoding="utf-8")
    link = tmp_path / "sprint-status.yaml"
    link.symlink_to(real)

    assert sprintstatus.advance(link, "3-2-digest-delivery", "in-progress") == "in-progress"

    assert link.is_symlink()  # still a link, not turned into a regular file
    assert sprintstatus.story_status(real, "3-2-digest-delivery") == "in-progress"


def test_advanced_bytes_matches_what_advance_writes_to_a_file(tmp_path):
    """`advanced_bytes` must answer with the WRITER's bytes, not an imitation of them.

    Its caller compares the answer to a board it is about to commit and skips the
    commit when they differ, so any divergence from `advance` — an inline comment
    dropped, a terminator normalized, the epic lift missed — reads to that caller as
    "somebody else wrote this" and silently costs the carry its commit.

    Graded against a real advance of the same board rather than a literal, so the
    comparison stays honest when `advance`'s own output changes. `in-progress` is the
    target because it is the one that also lifts the parent epic, exercising the
    second write no single-line check would notice was missing.
    """
    board = tmp_path / "sprint-status.yaml"
    board.write_text(SPRINT, encoding="utf-8")
    source = board.read_bytes()

    assert sprintstatus.advance(board, "3-2-digest-delivery", "in-progress") == "in-progress"

    assert sprintstatus.advanced_bytes(source, "3-2-digest-delivery", "in-progress") == (
        board.read_bytes()
    )
    # ...and the source it was handed is untouched: the recomputation runs on a copy
    assert source != board.read_bytes()


def test_advanced_bytes_echoes_the_source_when_advance_would_not_write(tmp_path):
    """A row already AT the target is a no-op for `advance` and must be one here too.

    This is the shape the carry meets most often — a tracked board whose flip rode the
    merge — so an `advanced_bytes` that rewrote anything at all would make the guard
    refuse every ordinary carry.
    """
    board = tmp_path / "sprint-status.yaml"
    board.write_text(SPRINT, encoding="utf-8")
    source = board.read_bytes()
    sprintstatus.advance(board, "3-2-digest-delivery", "done")
    done = board.read_bytes()

    assert sprintstatus.advanced_bytes(done, "3-2-digest-delivery", "done") == done
    assert sprintstatus.advanced_bytes(source, "3-2-digest-delivery", "backlog") == source


def test_advanced_bytes_is_none_when_the_row_is_absent(tmp_path):
    """No intended content to compare against, and the caller must not read the
    absence as agreement — it fails closed on this answer."""
    board = tmp_path / "sprint-status.yaml"
    board.write_text(SPRINT, encoding="utf-8")

    assert sprintstatus.advanced_bytes(board.read_bytes(), "9-9-not-a-story", "done") is None


def test_advanced_bytes_preserves_crlf_and_inline_comments(tmp_path):
    """The two shapes a hand-rolled line edit gets wrong, and both are byte-visible.

    #576 (per-line terminators) and #366 (a value's trailing ` # comment`) are exactly
    the cases where a second implementation would diverge from `advance` — and a
    divergence here is a carry that stops committing on every board carrying one.
    """
    source = (
        "development_status:\r\n"
        "  epic-3: in-progress\r\n"
        "  3-2-digest-delivery: ready-for-dev # owner: pat\r\n"
    ).encode("utf-8")

    out = sprintstatus.advanced_bytes(source, "3-2-digest-delivery", "done")

    assert out is not None
    assert b"3-2-digest-delivery: done # owner: pat\r\n" in out
    assert out.count(b"\r\n") == source.count(b"\r\n")
