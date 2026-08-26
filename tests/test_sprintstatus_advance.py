"""Tests for the orchestrator-owned sprint-status writer (generic-skill path)."""

import contextlib
import sys
from pathlib import Path

import pytest
import yaml

from bmad_loop import runs, sprintstatus
from bmad_loop.platform_util import atomic_write_bytes as real_atomic_write_bytes
from bmad_loop.platform_util import file_lock as real_file_lock

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

    def record(
        path: Path,
        payload: bytes | str,
        *,
        follow_symlinks: bool = True,
        require_writable_target: bool = False,
    ) -> None:
        writes.append(payload)
        raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        real_atomic_write_bytes(
            path,
            raw,
            follow_symlinks=follow_symlinks,
            require_writable_target=require_writable_target,
        )

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

    def boom(path, data: bytes, *, follow_symlinks=True, require_writable_target=False):
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


def test_advance_refuses_a_readonly_board(tmp_path):
    """#597's headline regression, restored. AGENTS.md makes `advance` the
    orchestrator's SOLE write path to sprint-status.yaml, so a read-only board is
    the only way an operator can say "stop rewriting this" — and it has to mean
    something. Before #590 it did, as a side effect of `write_bytes` opening the
    file. Going atomic silently took that away: `os.replace` needs write permission
    on the parent DIRECTORY, never on the entry it replaces, so the board was
    rewritten anyway and — because the mode is inherited — came back reading
    `0444`, leaving nothing in the permission bits to record the change.

    This site keeps `follow_symlinks` at the default (the row above), so it is NOT
    a confined writer; `require_writable_target=True` is the entire change, and
    this row is what grades it.

    `0o444` sets the READONLY attribute on win32 too, where `O_WRONLY` then fails
    with ERROR_ACCESS_DENIED, so this runs unskipped on both platforms. The chmod
    is on a file in this test's own tmp_path and is restored in a `finally` —
    never the session `project` template, and Windows rmtree refuses a READONLY
    leftover.

    Ablation: drop `require_writable_target=True` at the call and this fails
    `DID NOT RAISE`, with the board advanced and still reading `0444`."""
    p = _write(tmp_path)
    before = p.read_bytes()
    p.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            sprintstatus.advance(p, "3-2-digest-delivery", "in-progress")
    finally:
        p.chmod(0o644)

    assert p.read_bytes() == before  # the board is entire, and unadvanced
    assert list(tmp_path.glob("*.tmp")) == []  # a refusal stages nothing


def test_advance_still_advances_a_writable_board(tmp_path):
    """The positive control for the refusal above, stated as its own row because
    the flag is a REFUSAL and a refusal that fires too eagerly is the failure #597
    itself argued against: the owner of a `0444` file can legitimately replace it
    today, and an `os.access`-style check would have refused this ordinary write.
    The probe is a real `os.open(target, O_WRONLY)`, so the kernel answers and the
    normal path is untouched.

    Ablation: make `_refuse_unwritable_target` refuse unconditionally and this
    reddens while the row above stays green — the pair is what pins the boundary,
    not either alone."""
    p = _write(tmp_path)

    assert sprintstatus.advance(p, "3-2-digest-delivery", "in-progress") == "in-progress"

    assert sprintstatus.story_status(p, "3-2-digest-delivery") == "in-progress"


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


def test_status_in_bytes_reads_a_row_exactly_as_story_status_does(tmp_path):
    """`status_in_bytes` must be the READER, not a second reading of the board.

    Its caller holds one side of the comparison as a git blob and the other as a file,
    and treats a difference as "somebody else wrote this row" — so a reading that
    diverges from `story_status` on either side manufactures a difference that is not
    there, and costs an ordinary carry its advance.

    Graded against `story_status` on the same bytes rather than against a literal, so
    the two cannot drift apart silently. The row carries an inline comment, which is
    where a line-regex reading would take ` # the next story` for part of the value.
    """
    board = _write(tmp_path)
    source = board.read_bytes()

    for key in ("3-2-digest-delivery", "3-1-login", "4-1-thing"):
        assert sprintstatus.status_in_bytes(source, key) == sprintstatus.story_status(board, key)
    assert sprintstatus.status_in_bytes(source, "3-2-digest-delivery") == "backlog"


def test_status_in_bytes_folds_the_legacy_spelling_like_the_reader(tmp_path):
    """`drafted` and `ready-for-dev` are ONE status, and the fold is why this goes
    through `story_status` instead of reading the line.

    The comparison this feeds is between a row at HEAD and the same row on disk. A
    board committed before the rename and normalized since holds both spellings across
    those two sides, and a reading that kept them apart would call an untouched row
    foreign — refusing a carry over a change of vocabulary.
    """
    legacy = b"development_status:\n  epic-3: backlog\n  3-2-digest-delivery: drafted\n"
    current = b"development_status:\n  epic-3: backlog\n  3-2-digest-delivery: ready-for-dev\n"

    assert sprintstatus.status_in_bytes(legacy, "3-2-digest-delivery") == "ready-for-dev"
    assert sprintstatus.status_in_bytes(legacy, "3-2-digest-delivery") == (
        sprintstatus.status_in_bytes(current, "3-2-digest-delivery")
    )


def test_status_in_bytes_is_none_when_the_row_is_absent(tmp_path):
    """An absent row is a real answer — the caller reads it as "nothing here to
    protect" and lets `advance` report the missing row itself."""
    assert sprintstatus.status_in_bytes(_write(tmp_path).read_bytes(), "9-9-nope") is None


def test_status_in_bytes_raises_rather_than_calling_an_unreadable_board_absent(tmp_path):
    """The distinction the caller's fail-closed handling rests on.

    A board that does not parse is not a board whose row is gone, and collapsing the
    two would hand the caller a None it reads as "no row to protect" — authorizing the
    very write the check exists to withhold.
    """
    for source in (b"development_status: []\n", b"{ this is not: [valid yaml\n"):
        with pytest.raises(sprintstatus.SprintStatusError):
            sprintstatus.status_in_bytes(source, "3-2-digest-delivery")


# --- the board lock (#286/#469) ------------------------------------------------
#
# `advance` is the board's SOLE writer, but sole-writer is not mutual exclusion:
# a second orchestrator process runs the same sole writer, and `advance` is a
# read-modify-write of the whole file. These rows grade the sidecar lock that
# makes two of them serialize rather than trade last-write-wins.


def test_the_reads_that_decide_the_published_bytes_are_inside_the_lock(tmp_path, monkeypatch):
    """The hold spans every read that decides the bytes — and only those (#736).

    Formerly `test_advance_holds_the_lock_across_every_read_and_the_write`, which
    pinned the stricter claim that ALL THREE reads sit inside the hold. #736
    relaxed it deliberately: `advance` now runs ONE advisory pre-lock read to
    answer the calls that would write nothing, so an idempotent replay no longer
    fails on contention for work it was never going to do. What survives — and is
    the whole protection — is that the reads feeding the published bytes still
    happen after the acquisition.

    A lock taken around the atomic write alone excludes nobody that matters: the
    bytes being published were computed from a read that happened OUTSIDE it, so
    a rival's advance can land in between and be overwritten wholesale. The
    ordering is recorded from the calls themselves rather than inferred from the
    result, because a lost update leaves a board that looks perfectly well-formed.

    `load` is the probe for three of the four reads — the advisory probe, the
    inside-the-lock `story_status` never-regress read, and the epic-lift read all
    go through it — and the writer spy is the fourth event. Advancing a `backlog`
    story of a `backlog` epic is what makes the epic lift fire, and a `backlog`
    row is exactly what the advisory probe declines to answer, so the fall-through
    and all three inside events are present.

    Ablation A: make `_advance_locked` reuse the probe's answer instead of its own
    read (hoist the `current = story_status(...)` out and pass it in) and this
    reddens — the inside segment loses a `load`, and with it the guarantee that
    the never-regress decision saw the board the write is applied to.

    Ablation B: move `with _board_lock(path):` down to wrap only the
    `atomic_write_bytes` call and this reddens — the two deciding `load` events
    sort ahead of `lock-enter`, so the prefix is no longer the single advisory
    read."""
    p = _write(tmp_path)
    events: list[str] = []
    real_lock, real_load = sprintstatus._board_lock, sprintstatus.load

    @contextlib.contextmanager
    def spy_lock(path):
        events.append("lock-enter")
        with real_lock(path):
            yield
        events.append("lock-exit")

    def spy_load(path):
        events.append("load")
        return real_load(path)

    def spy_write(path, data, **kwargs):
        events.append("write")
        return real_atomic_write_bytes(path, data, **kwargs)

    monkeypatch.setattr(sprintstatus, "_board_lock", spy_lock)
    monkeypatch.setattr(sprintstatus, "load", spy_load)
    monkeypatch.setattr(sprintstatus, "atomic_write_bytes", spy_write)

    assert sprintstatus.advance(p, "3-2-digest-delivery", "in-progress") == "in-progress"

    assert events.count("lock-enter") == 1 and events.count("lock-exit") == 1
    enter = events.index("lock-enter")
    assert events[-1] == "lock-exit"
    assert events[:enter] == ["load"]  # exactly one advisory read, and nothing else
    assert events[enter + 1 : -1] == ["load", "load", "write"]  # the deciding reads, inside


def test_a_racing_writers_flip_survives_a_concurrent_advance(tmp_path):
    """The lost update #469 names, reproduced deterministically and refused.

    A rival process completes its whole advance while this one is still queued
    for the lock. Because every read happens after the acquisition, this call
    computes its bytes from the board the rival left behind, and both flips
    survive. Read the board before the lock instead and the rival's row is
    absent from the bytes published over it — gone, with no error anywhere.

    The rival runs from inside the spy, BEFORE it enters the real lock, which is
    what a second process actually gets to do; running it after would nest a
    blocking acquisition on a second fd and self-deadlock. The one-shot latch
    stops the rival's own `advance` from recursing into another rival.

    The rival's row must be one its advance really REWRITES — `4-1-thing` moves
    `review` -> `done`. A rival whose advance never-regresses writes nothing, and
    there is then no update available to lose: the test would pass against every
    ablation, including no lock at all.

    Ablation: hoist `text = path.read_bytes()...` (read#2) above the lock — e.g.
    drop the `with _board_lock(path):` in `advance` and re-wrap the
    `atomic_write_bytes` call alone — and this reddens, `4-1-thing` coming back
    `review`. Hoisting read#1 (`story_status`) alone does NOT redden this row:
    that read decides never-regress, it does not produce the bytes published over
    the rival, and its position is graded by the ordering row above instead."""
    p = _write(tmp_path)
    real_lock = sprintstatus._board_lock
    raced: list[str | None] = []

    @contextlib.contextmanager
    def racing_lock(path):
        if not raced:
            # Latch FIRST: the rival's own `advance` re-enters this spy, and a
            # latch set only on the way out would stage a rival per rival.
            raced.append(None)
            # a rival orchestrator gets the lock first and finishes its advance
            raced[0] = sprintstatus.advance(path, "4-1-thing", "done")
        with real_lock(path):
            yield

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sprintstatus, "_board_lock", racing_lock)
        assert sprintstatus.advance(p, "3-2-digest-delivery", "in-progress") == "in-progress"

    assert raced == ["done"]  # the rival really wrote, so there was an update to lose
    assert sprintstatus.story_status(p, "3-2-digest-delivery") == "in-progress"  # ours
    assert sprintstatus.story_status(p, "4-1-thing") == "done"  # ...and the rival's, NOT lost


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_board_lock_excludes_a_second_acquirer(tmp_path, monkeypatch):
    """The sidecar is a real OS lock, and its identity is the board's identity.

    Two claims, because either alone is satisfiable by a lock that excludes
    nobody. The `blocking=False` probe says the acquisition is genuine — a
    `_board_lock` that yielded without taking anything would pass every ordering
    assertion above and serialize nothing. The symlink half says two spellings of
    one board rendezvous on ONE sidecar, without which a repo that keeps its
    board outside the tree and symlinks it in would have its two callers exclude
    each other not at all.

    Probed with `blocking=False` rather than a sleep: `file_lock` is per-open-fd,
    so a second acquirer in this same process contends exactly as another process
    would, and the refusal is immediate and deterministic under `-n logical`.

    Ablation: drop the `.resolve()` in `runs.lock_path_for` and this reddens on
    the one-sidecar assertion — the two spellings hash to different digests."""
    real = tmp_path / "elsewhere" / "sprint-status.yaml"
    real.parent.mkdir()
    real.write_text(SPRINT, encoding="utf-8")
    link = tmp_path / "sprint-status.yaml"
    link.symlink_to(real)
    sidecars: list[Path] = []

    @contextlib.contextmanager
    def spy_file_lock(path, **kwargs):
        sidecars.append(path)
        with real_file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(sprintstatus, "file_lock", spy_file_lock)

    assert sprintstatus.advance(link, "3-2-digest-delivery", "in-progress") == "in-progress"
    assert sprintstatus.advance(real, "3-2-digest-delivery", "review") == "review"

    assert len(sidecars) == 2 and sidecars[0] == sidecars[1]  # one board, one sidecar
    assert sidecars[0] == runs.lock_path_for(link) == runs.lock_path_for(real)
    with real_file_lock(sidecars[0]):
        with pytest.raises(OSError):
            with real_file_lock(sidecars[0], blocking=False):
                pass  # pragma: no cover — the acquisition above must refuse


def test_advanced_bytes_never_touches_the_real_boards_lock(tmp_path, monkeypatch):
    """The shadow advance neither contends on the board nor strands a lock file.

    `advanced_bytes` recomputes an advance by running the real writer's body
    against a throwaway copy. Routing that through `advance` would take a lock
    keyed on the shadow's own path — harmless for exclusion, since nobody else
    can name a private TemporaryDirectory, but `file_lock` never removes a
    sidecar while the TemporaryDirectory removes only the shadow. Every
    ownership computation would then strand one more dead file under
    `<state root>/locks`, without bound. So it calls `_advance_locked` directly
    and takes no lock at all.

    Ablation: put `advance(shadow, ...)` back in place of `_advance_locked` —
    the spy fires and a sidecar is left behind, so both rows red."""
    board = _write(tmp_path)
    # Snapshot the bytes as they actually landed, rather than re-encoding SPRINT:
    # `_write` writes in text mode, so on Windows the newlines on disk are CRLF
    # while `SPRINT.encode` is LF, and the comparison would fail there for a
    # reason that has nothing to do with the lock.
    before = board.read_bytes()
    sidecars: list[Path] = []

    @contextlib.contextmanager
    def spy_file_lock(path, **kwargs):
        sidecars.append(path)
        with real_file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(sprintstatus, "file_lock", spy_file_lock)
    locks_dir = runs.state_root() / "locks"
    before_locks = set(locks_dir.glob("*")) if locks_dir.is_dir() else set()

    out = sprintstatus.advanced_bytes(board.read_bytes(), "3-2-digest-delivery", "in-progress")

    assert out is not None and b"3-2-digest-delivery: in-progress" in out
    assert sidecars == []  # no acquisition at all — the shadow is private
    after_locks = set(locks_dir.glob("*")) if locks_dir.is_dir() else set()
    assert after_locks == before_locks  # and nothing was stranded under the state root
    assert board.read_bytes() == before  # and the real board is untouched


def test_advance_lock_failure_raises_oserror(tmp_path, monkeypatch):
    """A board that could not be serialized is not rewritten unlocked.

    Parity with the write-failure row above: acquisition faults propagate on the
    channel every caller already routes `advance`'s raises through — the engine's
    crash/escalation handling, the CLI's failure exit — rather than degrading to
    an unserialized write. `file_lock`'s Windows branch gives up after ~10 s and
    raises `OSError`, so this is a reachable production shape, not a hypothetical.

    Ablation: swallow the acquisition error and proceed without the lock and this
    fails `DID NOT RAISE`, with the row advanced."""
    p = _write(tmp_path)
    before = p.read_bytes()

    @contextlib.contextmanager
    def unavailable(path):
        # the shape `msvcrt.locking` raises when the ~10 s blocking retry runs out
        raise OSError(11, "Resource deadlock avoided")
        yield  # pragma: no cover — unreachable; keeps this a generator function

    monkeypatch.setattr(sprintstatus, "_board_lock", unavailable)

    with pytest.raises(OSError, match="Resource deadlock avoided"):
        sprintstatus.advance(p, "3-2-digest-delivery", "in-progress")

    assert p.read_bytes() == before  # entire, and unadvanced
    assert list(tmp_path.glob("*.tmp")) == []


def test_advance_takes_no_lock_for_a_missing_board(tmp_path, monkeypatch):
    """Asking about a board that does not exist leaves no sidecar behind.

    The missing-board answer is `None` and it predates the lock, so a probe for a
    story on a project that has no board never mkdirs a locks dir nor creates a
    lockfile for a path nothing will ever write. The locked half rechecks anyway
    — a delete can land between the two.

    Ablation: move the pre-lock `is_file` check inside `_board_lock` and this
    reddens on the acquisition count."""
    entered: list[Path] = []

    @contextlib.contextmanager
    def spy_file_lock(path, **kwargs):
        entered.append(path)  # pragma: no cover — must not be reached
        with real_file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(sprintstatus, "file_lock", spy_file_lock)

    assert sprintstatus.advance(tmp_path / "nope.yaml", "3-1-login", "done") is None

    assert entered == []


def test_advance_takes_no_lock_when_the_row_is_already_at_or_past_target(tmp_path, monkeypatch):
    """A never-regress no-op answers from an advisory read, without acquiring (#736).

    The defect this closes: `advance` acquired before `_advance_locked` could
    discover there was nothing to write, so an idempotent replay — `bmad-loop
    confirm` against a story the board already records as done is a DESIGNED
    path, not an error — could fail on lock contention for work it never had.
    Both shapes of the comparison are exercised: a row strictly PAST target
    (`4-1-thing` sits at `review`, asked for `in-progress`) and a row exactly AT
    it (`3-1-login` is `done`, asked for `done`).

    The board bytes are the second oracle. A probe that answered the no-op but
    still went on to rewrite the file would satisfy the acquisition count alone,
    and "no lock" would then be describing an unserialized write rather than a
    no-op.

    Ablation: delete the probe from `advance` and this reddens on the count —
    both calls acquire, since discovering the no-op is the locked body's job
    again."""
    p = _write(tmp_path)
    before = p.read_bytes()  # as they landed, not `SPRINT.encode()` — CRLF on Windows
    entered: list[Path] = []

    @contextlib.contextmanager
    def spy_file_lock(path, **kwargs):
        entered.append(path)  # pragma: no cover — must not be reached
        with real_file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(sprintstatus, "file_lock", spy_file_lock)

    assert sprintstatus.advance(p, "4-1-thing", "in-progress") == "review"  # past target
    assert sprintstatus.advance(p, "3-1-login", "done") == "done"  # exactly at target

    assert entered == []
    assert p.read_bytes() == before  # a no-op, not an unserialized write


def test_advance_takes_no_lock_for_an_absent_row(tmp_path, monkeypatch):
    """A story the board does not carry is answered before the lock too (#736).

    Sibling of the missing-board row above, one level in: the board exists, so
    the `is_file` guard passes, but the story is not on it. `advance`'s contract
    is `None` there, and a `None` return writes nothing, so there is no reason to
    mint a sidecar — or to fail on one — for a row that does not exist. The
    engine asks about stories it has not confirmed are on the board.

    Ablation: delete the probe's `if current is None: return None` early-out and
    this reddens on the count — the absent-row answer moves back under the
    hold."""
    p = _write(tmp_path)
    entered: list[Path] = []

    @contextlib.contextmanager
    def spy_file_lock(path, **kwargs):
        entered.append(path)  # pragma: no cover — must not be reached
        with real_file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(sprintstatus, "file_lock", spy_file_lock)

    assert sprintstatus.advance(p, "9-9-ghost", "done") is None

    assert entered == []


def test_a_probe_satisfied_noop_succeeds_when_no_state_root_is_derivable(tmp_path, monkeypatch):
    """The no-op stops risking a failure mode it had no work to earn (#736).

    `_board_lock` names its sidecar through `runs.lock_path_for`, which raises
    `StateRootError` when no state root can be derived — and `StateRootError` is
    NOT an `OSError`, so it escapes on its own taxonomy. Before the probe, that
    made an already-done board's replay fail outright. Now it cannot: the answer
    is reached without ever asking for a lock path.

    The second half is the load-bearing half. A probe that made the lock
    optional, rather than unnecessary, would be a far worse bug than the one
    being fixed — so the same fault on a call that really does write must still
    surface. `3-2-digest-delivery` is `backlog`, so advancing it to `in-progress`
    is a genuine write and has to raise.

    Patching the module attribute reaches the real call site because
    `_board_lock`'s `from . import runs` is deliberately lazy (the import cycle
    runs → verify → sprintstatus forbids a top-level one), so the lookup happens
    per call against the patched module.

    Ablation: delete the probe and the FIRST call raises `StateRootError` — the
    behavior #736 filed."""
    p = _write(tmp_path)

    def no_state_root(data_path):
        raise runs.StateRootError("no state root")

    monkeypatch.setattr(runs, "lock_path_for", no_state_root)

    assert sprintstatus.advance(p, "3-1-login", "done") == "done"  # probe-satisfied, no lock

    with pytest.raises(runs.StateRootError):
        sprintstatus.advance(p, "3-2-digest-delivery", "in-progress")  # a real write still needs it


def test_a_malformed_board_probe_falls_through_and_raises_from_under_the_lock(
    tmp_path, monkeypatch
):
    """The probe is advisory: a fault in it decides nothing (#736).

    An unreadable board makes `story_status` raise inside the probe, and the
    probe swallows it — deliberately broadly, because narrowing the catch would
    let the probe invent a failure the locked path does not have. The call then
    falls through and the locked body raises `SprintStatusError` on exactly the
    channel `cli.py`'s error routing already expects.

    The ACQUISITION COUNT is the oracle, not the raise. `pytest.raises` alone
    survives removing the try/except entirely — the probe's own uncaught
    `SprintStatusError` is the same class, from the same reader, and would pass
    this row while never having reached the lock at all. Only `len(entered) == 1`
    tells the two apart.

    Ablation: remove the probe's `try`/`except Exception` and this reddens on the
    count — `entered == []`, because the probe raised before the acquisition."""
    p = tmp_path / "sprint-status.yaml"
    p.write_text("development_status: []\n", encoding="utf-8")
    entered: list[Path] = []

    @contextlib.contextmanager
    def spy_file_lock(path, **kwargs):
        entered.append(path)
        with real_file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(sprintstatus, "file_lock", spy_file_lock)

    with pytest.raises(sprintstatus.SprintStatusError):
        sprintstatus.advance(p, "3-2-digest-delivery", "in-progress")

    assert len(entered) == 1  # it raised from UNDER the hold, not instead of taking it


def test_the_authoritative_never_regress_decision_is_made_under_the_lock(tmp_path):
    """The probe's row may be stale by acquisition time, and is then discarded (#736).

    The probe reads outside all exclusion, so between it and the hold a rival can
    move the row anywhere — including PAST the target this call is carrying. If
    that stale answer were carried into `_advance_locked` instead of being
    re-read, the never-regress test would be applied to a status the board no
    longer has and the rival's forward progress would be rewritten backwards. The
    published bytes are decided under the hold precisely so this cannot happen.

    The rival runs from inside the `_board_lock` spy, BEFORE it enters the real
    lock, which is what a second process actually gets to do; running it after
    would nest a blocking acquisition on a second fd and self-deadlock. The
    one-shot latch stops the rival's own `advance` from recursing into another
    rival. Both calls target the SAME row — that is the point, unlike the
    lost-update row above, which needs two different rows.

    Ablation: thread the probe's answer into `_advance_locked` (hoist its
    `current = story_status(...)` read and pass the probe's value in) and this
    reddens — the call decides against the stale `backlog`, writes, and the board
    comes back `in-progress` with the rival's `done` gone."""
    p = _write(tmp_path)
    real_lock = sprintstatus._board_lock
    raced: list[str | None] = []

    @contextlib.contextmanager
    def racing_lock(path):
        if not raced:
            # Latch FIRST: the rival's own `advance` re-enters this spy, and a
            # latch set only on the way out would stage a rival per rival.
            raced.append(None)
            # a rival takes the row all the way to `done` while we are still queued
            raced[0] = sprintstatus.advance(path, "3-2-digest-delivery", "done")
        with real_lock(path):
            yield

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sprintstatus, "_board_lock", racing_lock)
        # our probe saw `backlog`; by the time we hold the lock the row is `done`
        assert sprintstatus.advance(p, "3-2-digest-delivery", "in-progress") == "done"

    assert raced == ["done"]  # the rival really wrote, so there was progress to lose
    assert sprintstatus.story_status(p, "3-2-digest-delivery") == "done"  # NOT regressed
