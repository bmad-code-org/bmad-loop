"""Ledger parsing and editing: deferredwork.py."""

from pathlib import Path

import pytest

from bmad_loop import deferredwork, fences
from bmad_loop.deferredwork import (
    _ISO_DATE_RE,
    ARCHIVE_REL,
    LINE_BREAK_RE,
    SEVERITY_ALIASES,
    append_decision,
    append_entry,
    archive_closed,
    classify,
    field_line_present,
    field_severity,
    gates,
    has_legacy,
    mark_done,
    mark_done_many,
    mark_done_many_reopenable,
    mark_open,
    next_seq,
    open_ids,
    parse_declaration,
    parse_ledger,
    parse_legacy,
)

OPERATION_ID = "run-20260803T120000/dw-fix"

LEDGER = """\
# Deferred Work

### DW-1: Harden unicode handling

origin: quick-dev split of spec-3-2-digest.md, 2026-06-01
location: src/strings.py:40
reason: out of scope for the digest story.
status: open

### DW-2: Old closed item

origin: code review of spec-1-1.md, 2026-05-20
location: src/foo.py:10
reason: pre-existing.
status: done 2026-05-25

### DW-3: Needs human decision

origin: code review of spec-2-2.md, 2026-06-05
location: src/retry.py:12
reason: auto-mode: needs human decision
status: open
seen-again: 2026-06-08 (code review of spec-2-3.md)
"""


def write_ledger(tmp_path: Path, text: str = LEDGER) -> Path:
    path = tmp_path / "deferred-work.md"
    path.write_text(text, encoding="utf-8")
    return path


def close_reopenable(
    path: Path,
    dw_id: str,
    note: str,
    date: str = "2026-06-11",
    operation_id: str = OPERATION_ID,
) -> None:
    assert mark_done_many_reopenable(path, [dw_id], date, note, operation_id) == [dw_id]


def test_parse_ledger_entries():
    entries = parse_ledger(LEDGER)
    assert [e.id for e in entries] == ["DW-1", "DW-2", "DW-3"]
    assert entries[0].title == "Harden unicode handling"
    assert entries[0].open
    assert not entries[1].open
    assert entries[1].status == "done 2026-05-25"
    assert entries[2].open  # seen-again line does not affect status


def test_open_ids():
    assert open_ids(LEDGER) == {"DW-1", "DW-3"}


def test_parse_tolerates_freeform_sections():
    text = (
        "## Deferred from: code review of story 0.3 (2026-06-08)\n\n"
        "- W1-b — some freeform item with no DW format\n\n" + LEDGER
    )
    assert open_ids(text) == {"DW-1", "DW-3"}


def test_entry_ends_at_next_section_heading():
    text = LEDGER + "\n## Notes\n\nstatus: open\n"
    entries = parse_ledger(text)
    # the stray status line under "## Notes" must not leak into DW-3
    assert entries[-1].id == "DW-3"
    assert "## Notes" not in entries[-1].body


def test_entry_without_status_is_not_open():
    text = "### DW-7: Malformed entry\n\norigin: somewhere\n"
    entries = parse_ledger(text)
    assert entries[0].status == ""
    assert not entries[0].open
    assert open_ids(text) == set()


def test_mark_done_touches_only_target(tmp_path):
    path = write_ledger(tmp_path)
    assert mark_done(path, "DW-1", "2026-06-11", "guards added in src/strings.py")
    text = path.read_text(encoding="utf-8")
    entries = {e.id: e for e in parse_ledger(text)}
    assert entries["DW-1"].status == "done 2026-06-11"
    assert "resolution: guards added in src/strings.py" in entries["DW-1"].body
    assert entries["DW-3"].open
    assert "resolution:" not in entries["DW-3"].body
    assert entries["DW-2"].status == "done 2026-05-25"


def test_mark_done_idempotent(tmp_path):
    path = write_ledger(tmp_path)
    assert mark_done(path, "DW-1", "2026-06-11", "fixed")
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_done(path, "DW-1", "2026-06-12", "fixed again")
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_done_missing_entry(tmp_path):
    path = write_ledger(tmp_path)
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_done(path, "DW-99", "2026-06-11", "n/a")
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_open_round_trips_one_reopenable_close_character_for_character(tmp_path):
    path = write_ledger(tmp_path)
    before = path.read_text(encoding="utf-8")
    note = "resolved by sweep bundle dw-fix"
    close_reopenable(path, "DW-1", note)
    assert mark_open(path, "DW-1", note, OPERATION_ID)
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "status_line",
    ["status:   open", "status: open # keep this annotation", "status:\topen   # annotated"],
)
def test_mark_open_restores_the_original_parser_accepted_status_line(tmp_path, status_line):
    text = f"### DW-1: formatted\n\norigin: session\n{status_line}\n"
    path = write_ledger(tmp_path, text)

    close_reopenable(path, "DW-1", "by dw-a")
    assert mark_open(path, "DW-1", "by dw-a", OPERATION_ID)

    assert path.read_text(encoding="utf-8") == text


def test_reopenable_close_persists_adjacent_resolution_and_undo_marker(tmp_path):
    path = write_ledger(tmp_path)
    close_reopenable(path, "DW-1", "by dw-a")

    lines = parse_ledger(path.read_text(encoding="utf-8"))[0].body.splitlines()
    status_at = lines.index("status: done 2026-06-11")
    assert lines[status_at + 1] == "resolution: by dw-a"
    assert lines[status_at + 2].startswith("resolution-undo: ")
    assert len(lines[status_at + 2].split()) == 4


def test_standard_close_retains_its_existing_ledger_shape(tmp_path):
    path = write_ledger(tmp_path, "### DW-1: standard\n\nstatus: open\n")
    assert mark_done_many(path, ["DW-1"], "2026-06-11", "by dw-a") == ["DW-1"]

    assert path.read_text(encoding="utf-8") == (
        "### DW-1: standard\n\nstatus: done 2026-06-11\nresolution: by dw-a\n"
    )


def test_reopenable_close_refuses_an_original_status_with_a_splitlines_break(tmp_path):
    text = "### DW-1: unsafe\n\nstatus: open\u2028injected: value\n"
    path = write_ledger(tmp_path, text)

    assert mark_done_many_reopenable(path, ["DW-1"], "2026-06-11", "by dw-a", OPERATION_ID) == []
    assert path.read_text(encoding="utf-8") == text


def test_mark_open_touches_only_the_target_entry(tmp_path):
    path = write_ledger(tmp_path)
    close_reopenable(path, "DW-1", "by dw-a")
    close_reopenable(path, "DW-3", "by dw-a")
    assert mark_open(path, "DW-1", "by dw-a", OPERATION_ID)

    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert entries["DW-1"].open
    assert "resolution:" not in entries["DW-1"].body
    assert entries["DW-2"].status == "done 2026-05-25"
    assert entries["DW-3"].status == "done 2026-06-11"


def test_mark_open_refuses_a_missing_ledger(tmp_path):
    assert not mark_open(tmp_path / "nope.md", "DW-1", "by dw-a", OPERATION_ID)


def test_mark_open_refuses_a_missing_entry(tmp_path):
    path = write_ledger(tmp_path)
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_open(path, "DW-99", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_open_refuses_an_entry_that_is_still_open(tmp_path):
    """The open guard is independent of note matching: an LLM-written open entry
    can carry a matching resolution line, but the orchestrator must not delete it."""
    path = write_ledger(
        tmp_path,
        "### DW-1: partially handled\n\nstatus: open\nresolution: by dw-a\n",
    )
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_open_refuses_a_status_less_entry(tmp_path):
    """parse_ledger tolerates this shape. Without the explicit guard, a future
    call from _defer raises AttributeError and crashes instead of deferring."""
    path = write_ledger(
        tmp_path,
        "### DW-1: malformed but tolerated\n\norigin: session\nresolution: by dw-a\n",
    )
    snapshot = path.read_text(encoding="utf-8")
    assert parse_ledger(snapshot)[0].status == ""
    assert not mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == snapshot


@pytest.mark.parametrize("status", ["malformed", "done someday", "done 2026-02-30"])
def test_mark_open_refuses_a_noncanonical_status(tmp_path, status):
    path = write_ledger(
        tmp_path,
        f"### DW-1: malformed but tolerated\n\nstatus: {status}\nresolution: by dw-a\n",
    )
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_open_refuses_a_closed_entry_without_a_resolution(tmp_path):
    path = write_ledger(tmp_path)
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_open(path, "DW-2", "", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_open_refuses_a_legacy_close_with_the_same_note_but_no_marker(tmp_path):
    path = write_ledger(tmp_path)
    assert mark_done(path, "DW-1", "2026-06-11", "by dw-a")
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_open_reopens_only_closes_from_this_operation(tmp_path):
    """A reused story note does not make an earlier run's close ours to undo."""
    path = write_ledger(tmp_path)
    note = "resolved by story 1-1"
    close_reopenable(path, "DW-1", note, operation_id="earlier-run/dw-fix")

    marked = mark_done_many_reopenable(path, ["DW-1", "DW-3"], "2026-06-11", note, OPERATION_ID)
    assert marked == ["DW-3"]
    assert not mark_open(path, "DW-1", note, OPERATION_ID)
    assert mark_open(path, "DW-3", note, OPERATION_ID)

    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert entries["DW-1"].status == "done 2026-06-11"
    assert entries["DW-3"].open


def test_mark_open_refuses_the_wrong_operation_id(tmp_path):
    path = write_ledger(tmp_path)
    close_reopenable(path, "DW-1", "by dw-a")
    snapshot = path.read_text(encoding="utf-8")

    assert not mark_open(path, "DW-1", "by dw-a", "another-run/dw-fix")
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_open_recomputes_operation_identity_after_a_crash(tmp_path):
    """No return-side receipt is needed after the close has reached disk."""
    path = write_ledger(tmp_path)
    operation_id = f"run-20260803T120000/{'dw-fix'}"
    assert mark_done_many_reopenable(path, ["DW-1"], "2026-06-11", "by dw-a", operation_id) == [
        "DW-1"
    ]

    # Simulate rehydration from already-persisted run + task identity. The marker
    # on disk, not the close call's returned list, carries the original line.
    replay_operation_id = "/".join(["run-20260803T120000", "dw-fix"])
    assert mark_open(path, "DW-1", "by dw-a", replay_operation_id)


def test_operation_id_line_breaks_are_encoded_inside_the_marker(tmp_path):
    path = write_ledger(tmp_path)
    operation_id = "run-20260803\nreview\u2028dw-fix"
    close_reopenable(path, "DW-1", "by dw-a", operation_id=operation_id)
    text = path.read_text(encoding="utf-8")

    assert operation_id not in text
    assert sum(line.startswith("resolution-undo: ") for line in text.splitlines()) == 1
    assert mark_open(path, "DW-1", "by dw-a", operation_id)


def test_reopenable_close_refuses_an_empty_operation_id_without_writing(tmp_path):
    path = write_ledger(tmp_path)
    snapshot = path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="operation_id must not be empty"):
        mark_done_many_reopenable(path, ["DW-1"], "2026-06-11", "by dw-a", "")
    assert path.read_text(encoding="utf-8") == snapshot


@pytest.mark.parametrize(
    "prior_status_payload",
    [
        "not-hex",
        b"\xff".hex(),
        "status: done 2026-06-11".encode().hex(),
        "status: open\ninjected: value".encode().hex(),
        "status: open\u2028injected: value".encode().hex(),
    ],
)
def test_mark_open_refuses_a_malformed_or_non_open_prior_status_payload(
    tmp_path, prior_status_payload
):
    path = write_ledger(tmp_path)
    close_reopenable(path, "DW-1", "by dw-a")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    marker_at = next(i for i, line in enumerate(lines) if line.startswith("resolution-undo: "))
    marker_prefix = lines[marker_at].rstrip("\n").rsplit(" ", 1)[0]
    lines[marker_at] = f"{marker_prefix} {prior_status_payload}\n"
    path.write_text("".join(lines), encoding="utf-8")
    snapshot = path.read_text(encoding="utf-8")

    assert not mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == snapshot


@pytest.mark.parametrize("tamper", ["status-date", "marker-date", "marker-adjacency"])
def test_mark_open_refuses_a_tampered_done_status_or_marker(tmp_path, tamper):
    path = write_ledger(tmp_path)
    close_reopenable(path, "DW-1", "by dw-a")
    text = path.read_text(encoding="utf-8")
    if tamper == "status-date":
        text = text.replace("status: done 2026-06-11", "status: done 2026-06-12", 1)
    elif tamper == "marker-date":
        marker = next(line for line in text.splitlines() if line.startswith("resolution-undo: "))
        text = text.replace(marker, marker.replace("2026-06-11", "2026-06-12"), 1)
    else:
        text = text.replace(
            "\nresolution-undo:", "\ndecision: 2026-06-11 keep\nresolution-undo:", 1
        )
    path.write_text(text, encoding="utf-8")
    snapshot = path.read_text(encoding="utf-8")

    assert not mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_open_requires_the_resolution_directly_below_status(tmp_path):
    """A later matching resolution belongs to a different annotation. Searching
    the whole entry would turn this specific undo into a general reopen."""
    path = write_ledger(tmp_path, "### DW-1: closed elsewhere\n\nstatus: open\n")
    close_reopenable(path, "DW-1", "by dw-a")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "\nresolution: by dw-a", "\ndecision: 2026-06-11 keep\nresolution: by dw-a", 1
        ),
        encoding="utf-8",
    )
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_open_tolerates_reformatted_resolution_whitespace(tmp_path):
    path = write_ledger(tmp_path, "### DW-1: closed then reflowed\n\nstatus: open\n")
    close_reopenable(path, "DW-1", "by dw-a")
    path.write_text(
        path.read_text(encoding="utf-8").replace("resolution: by dw-a", "resolution:   by dw-a  "),
        encoding="utf-8",
    )
    assert mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    entry = parse_ledger(path.read_text(encoding="utf-8"))[0]
    assert entry.open and "resolution:" not in entry.body


@pytest.mark.parametrize("note", ["  padded note  ", "line one\nline two"])
def test_mark_open_uses_the_same_note_normalization_as_mark_done(tmp_path, note):
    """Main's mark_done flattens line breaks and permits padded clean text. The
    undo accepts the original caller value, not only the rendered ledger line."""
    path = write_ledger(tmp_path)
    before = path.read_text(encoding="utf-8")
    close_reopenable(path, "DW-1", note)
    assert mark_open(path, "DW-1", note, OPERATION_ID)
    assert path.read_text(encoding="utf-8") == before


def test_mark_open_is_idempotent_after_the_undo(tmp_path):
    path = write_ledger(tmp_path)
    close_reopenable(path, "DW-1", "by dw-a")
    assert mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    snapshot = path.read_text(encoding="utf-8")
    assert not mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == snapshot


def test_mark_open_write_failure_raises_and_keeps_the_closed_entry(tmp_path, monkeypatch):
    path = write_ledger(tmp_path)
    close_reopenable(path, "DW-1", "by dw-a")
    closed = path.read_text(encoding="utf-8")

    def boom(path, text):
        raise OSError("no space left")

    monkeypatch.setattr(deferredwork, "atomic_write_text", boom)
    with pytest.raises(OSError, match="no space left"):
        mark_open(path, "DW-1", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == closed


def test_reopenable_close_write_failure_raises_and_keeps_the_open_entry(tmp_path, monkeypatch):
    path = write_ledger(tmp_path)
    before = path.read_text(encoding="utf-8")

    def boom(path, text):
        raise OSError("no space left")

    monkeypatch.setattr(deferredwork, "atomic_write_text", boom)
    with pytest.raises(OSError, match="no space left"):
        mark_done_many_reopenable(path, ["DW-1", "DW-3"], "2026-06-11", "by dw-a", OPERATION_ID)
    assert path.read_text(encoding="utf-8") == before


def test_append_decision(tmp_path):
    path = write_ledger(tmp_path)
    assert append_decision(path, "DW-3", "2026-06-11", "Keep cap", "frozen intent stands")
    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert "decision: 2026-06-11 Keep cap — frozen intent stands" in entries["DW-3"].body
    assert entries["DW-3"].open  # decision alone does not close
    assert "decision:" not in entries["DW-1"].body


def test_append_decision_then_mark_done(tmp_path):
    path = write_ledger(tmp_path)
    assert append_decision(path, "DW-3", "2026-06-11", "Close", "")
    assert mark_done(path, "DW-3", "2026-06-11", "closed by human decision")
    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert entries["DW-3"].status == "done 2026-06-11"
    assert "decision: 2026-06-11 Close" in entries["DW-3"].body


def test_append_decision_missing_file(tmp_path):
    assert not append_decision(tmp_path / "nope.md", "DW-1", "2026-06-11", "x", "y")
    assert not mark_done(tmp_path / "nope.md", "DW-1", "2026-06-11", "x")


def test_append_decision_write_failure_raises_and_keeps_the_ledger(tmp_path, monkeypatch):
    """#328. `Path.write_text` opens `'w'` (truncate) and only THEN encodes, so a
    failure anywhere in that window left the whole ledger at zero bytes. The
    atomic helper builds the replacement beside the target, so a raise leaves the
    original exactly as it was."""
    path = write_ledger(tmp_path)
    before = path.read_bytes()

    def boom(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(deferredwork, "atomic_write_text", boom)
    with pytest.raises(OSError, match="disk full"):
        append_decision(path, "DW-3", "2026-06-11", "Keep cap", "frozen intent stands")
    assert path.read_bytes() == before


# ------------------------------------------------- line-break injection (#305)
#
# The ledger is line-oriented and every mutator interpolates its arguments, so a
# value carrying a break injects ledger lines. Free text is SANITIZED, never
# refused, and nothing upstream rejects it either: the close paths call these
# writers bare (sweep._close_resolved, decisions.apply_pre_answer), so a raise
# ends the sweep run as crashed. Only the orchestrator-owned enumerables (date,
# status, severity) raise.

# Derived from the oracle, never typed. A literal U+2028 in a source file is
# trivially normalized to a plain space by an editor or a tool, and the two are
# then indistinguishable by inspection — which silently turns a line-break case
# into a whitespace case that passes for the wrong reason. The bound is safe:
# the exhaustive test below proves the whole split set lives under it.
BREAK_CHARS = tuple(chr(c) for c in range(0x2030) if len(("a" + chr(c) + "b").splitlines()) > 1)
# Multi-character runs, which `BREAK_CHARS` cannot reach: every entry there is
# one character, and the break-only cases collapse to "" before the quantifier
# matters. Without these, deleting `+` from LINE_BREAK_RE leaves the suite
# green while every CRLF value silently gains a second space — and CRLF is the
# common real-world break, being what a Windows-authored result.json carries.
BREAK_RUNS = ("\r\n", "\n\n")


def test_line_break_set_is_exactly_what_str_splitlines_splits_on():
    """`LINE_BREAK_RE` must cover every character `str.splitlines()` splits on,
    because `parse_legacy` scans the ledger with it while `parse_ledger` matches
    with `re.MULTILINE` — a member missed here splits an entry for one reader and
    is invisible to the other.

    Checked by enumeration rather than by listing the members: written as source
    escapes these are easy to get wrong, and a `\\u2028` silently normalized to a
    plain space would widen the class to collapse ordinary spaces — quietly
    breaking the byte-identity guarantee instead of the injection guard."""
    splits = {chr(c) for c in range(0x110000) if len(("a" + chr(c) + "b").splitlines()) > 1}
    matches = {chr(c) for c in range(0x110000) if LINE_BREAK_RE.fullmatch(chr(c))}
    assert matches == splits
    assert " " not in matches and "\t" not in matches  # ordinary whitespace is not a break


@pytest.mark.parametrize("brk", [*BREAK_CHARS, *BREAK_RUNS])
def test_mark_done_many_sanitizes_an_injected_note(tmp_path, brk):
    """Driven through `mark_done_many`, not the `mark_done` wrapper: that is the
    entry point `Engine._apply_deferred_closes` uses, so a guard placed on the
    wrapper would be inert for every story close."""
    path = write_ledger(tmp_path)
    before = len(parse_ledger(path.read_text(encoding="utf-8")))

    note = f"fixed{brk}### DW-99: injected{brk}status: open"

    assert mark_done_many(path, ["DW-1"], "2026-06-11", note) == ["DW-1"]

    text = path.read_text(encoding="utf-8")
    entries = {e.id: e for e in parse_ledger(text)}
    assert len(entries) == before  # no phantom DW-99 minted
    assert set(entries) == {"DW-1", "DW-2", "DW-3"}
    assert entries["DW-1"].status == "done 2026-06-11"
    # the injected text survives as prose on one line — sanitized, not dropped
    assert "resolution: fixed ### DW-99: injected status: open" in entries["DW-1"].body


@pytest.mark.parametrize("brk", [*BREAK_CHARS, *BREAK_RUNS])
def test_mark_done_sanitizes_a_note_that_would_double_the_status_line(tmp_path, brk):
    """The quiet half of the bug, and the half a status assertion cannot see:
    `STATUS_RE` takes the FIRST match, so an injected `status:` line leaves both
    `entry.status` and `entry.open` reporting exactly what they should. The damage
    is structural — the entry ends up carrying two status lines, so the ledger no
    longer says one thing about it, and which line wins depends on which reader
    looks. Asserted by counting lines the way `str.splitlines()` does, which is
    why the U+2028 case belongs here too."""
    path = write_ledger(tmp_path)

    assert mark_done(path, "DW-1", "2026-06-11", f"fixed{brk}status: open")

    (entry,) = [e for e in parse_ledger(path.read_text(encoding="utf-8")) if e.id == "DW-1"]
    assert entry.status == "done 2026-06-11"
    assert [line for line in entry.body.splitlines() if line.startswith("status:")] == [
        "status: done 2026-06-11"
    ]


@pytest.mark.parametrize("run", BREAK_RUNS)
def test_a_multi_character_break_run_collapses_to_exactly_one_space(tmp_path, run):
    """`LINE_BREAK_RE`'s `+` quantifier, which nothing else pins: a run of breaks
    must become ONE space, not one per character. CRLF is the case that matters —
    it is what a Windows-authored `result.json` carries, so it is the most likely
    break to reach these writers at all."""
    path = write_ledger(tmp_path)

    assert mark_done(path, "DW-1", "2026-06-11", f"fixed{run}in src/foo.py")

    (entry,) = [e for e in parse_ledger(path.read_text(encoding="utf-8")) if e.id == "DW-1"]
    assert "resolution: fixed in src/foo.py" in entry.body


def test_mark_done_sanitizes_a_note_that_would_truncate_the_entry(tmp_path):
    """The sharpest shape: a break followed by the flat appender's opening line.
    `FLAT_ENTRY_RE` bounds a canonical entry on exactly that line (#304), so an
    unsanitized note cuts DW-1's span short there and the tail re-surfaces as a
    phantom *legacy* item — visible to the sweep's migration trigger, `--dry-run`
    leftovers and the TUI as work nobody filed."""
    path = write_ledger(tmp_path)

    assert mark_done(path, "DW-1", "2026-06-11", "fixed\n- source_spec: `x.md`")

    text = path.read_text(encoding="utf-8")
    (entry,) = [e for e in parse_ledger(text) if e.id == "DW-1"]
    assert "resolution: fixed - source_spec: `x.md`" in entry.body
    assert parse_legacy(text) == []


@pytest.mark.parametrize(
    "date",
    [
        "nope",
        "2026-6-11",
        "20260611",
        "2026-02-30",
        "2026-06-11\nx",
        "\u0662\u0660\u0662\u0666-\u0660\u0666-\u0660\u0669",
    ],
    ids=["prose", "unpadded", "compact", "impossible-day", "with-break", "arabic-indic-digits"],
)
def test_mark_done_many_raises_on_a_bad_date_without_writing(tmp_path, date):
    """`date` is orchestrator-owned (`Engine._today()`), so a bad value is a
    programmer bug — the one place in these writers that raises. The digits are
    pinned to ASCII by the pattern itself (`[0-9]`, not `\\d`), so the shape
    check refuses an Arabic-Indic date rather than leaning on `fromisoformat`."""
    path = write_ledger(tmp_path)
    snapshot = path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        mark_done_many(path, ["DW-1"], date, "fixed")

    assert path.read_text(encoding="utf-8") == snapshot


def test_the_date_pattern_itself_refuses_non_ascii_digits():
    """`\\d` matches Arabic-Indic, fullwidth and mathematical digit forms; `[0-9]`
    does not. `date.fromisoformat` refuses them a moment later either way, so this
    pins the *pattern* rather than the outcome — the docstring's claim is about
    the regex, and only a direct assertion can hold it to that."""
    assert _ISO_DATE_RE.fullmatch("2026-06-09")
    for foreign in (
        "\u0662\u0660\u0662\u0666-\u0660\u0666-\u0660\u0669",
        "\uff12\uff10\uff12\uff16-\uff10\uff16-\uff10\uff19",
    ):
        assert not _ISO_DATE_RE.fullmatch(foreign)


def test_bad_date_raises_even_with_no_ledger_on_disk(tmp_path):
    """Validated at function entry, ahead of the `is_file()` short-circuit: a
    guard that only fires when the ledger happens to exist is one an absent
    fixture hides."""
    missing = tmp_path / "nope.md"

    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        mark_done_many(missing, ["DW-1"], "nope", "fixed")
    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        append_decision(missing, "DW-1", "nope", "Keep", "detail")


def test_append_decision_sanitizes_label_and_detail(tmp_path):
    path = write_ledger(tmp_path)

    assert append_decision(
        path, "DW-3", "2026-06-11", "Keep\ncap", f"Keep{BREAK_CHARS[-1]}status: done 2026-01-01"
    )

    text = path.read_text(encoding="utf-8")
    entries = {e.id: e for e in parse_ledger(text)}
    assert "decision: 2026-06-11 Keep cap — Keep status: done 2026-01-01" in entries["DW-3"].body
    assert len([line for line in text.splitlines() if line.startswith("decision:")]) == 1
    assert entries["DW-3"].open  # the injected `status:` did not close it


@pytest.mark.parametrize("detail", [*BREAK_CHARS, " \n ", "\r\n\t"])
def test_append_decision_drops_the_separator_for_a_break_only_detail(tmp_path, detail):
    """A detail that sanitizes to nothing must take the ` — ` with it. The
    ordering is the guard: sanitizing after the emptiness test would leave the
    entry carrying a dangling separator promising a detail that is not there."""
    path = write_ledger(tmp_path)

    assert append_decision(path, "DW-3", "2026-06-11", "Keep cap", detail)

    (line,) = [
        ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("decision:")
    ]
    assert line == "decision: 2026-06-11 Keep cap"
    assert "—" not in line


def test_writers_leave_a_clean_value_byte_identical(tmp_path):
    """No reformatting of values that never carried a break — the sanitizer must
    not become an incidental whitespace normalizer for existing ledgers."""
    path = write_ledger(tmp_path)
    padded = "  fixed  in  src/foo.py\t"

    assert mark_done(path, "DW-1", "2026-06-11", padded)
    assert append_decision(path, "DW-3", "2026-06-11", " Keep ", padded)

    text = path.read_text(encoding="utf-8")
    assert f"resolution: {padded}" in text
    assert f"decision: 2026-06-11  Keep  — {padded}" in text


# ------------------------------------------------------------------- legacy
#
# Fixtures are condensed verbatim from four real pre-DW project ledgers,
# one per shape the parser must handle.

# id'd bullets under "## Deferred from:" sections; bold/bracket done markers
LIGHTS_OUT = """\

## Deferred from: code review of gdd.md (2026-06-08)

- W1 — **RESOLVED 2026-06-09**: Validate the dual-clock squeeze is mathematically survivable. Epic-0 tuning gate. [MAJOR] — harness PASS and human **GO** decision. [CLOSED]
- W2 — Gloom engagement/retention once Top-Gloom is out of reach. Playtest watch (already a Success Metric). [MINOR]

## Deferred from: code review of story 0.3 (2026-06-08)

- W1 — duplicate native id in another section. [MINOR→MAJOR if missed]
- **W-1.2-c** — CLOSED: `moveSpeed` finite > 0 law (+ test).

## 2026-06-09 — epics-review absorb, 3-layer code review (spec-apply-epics-review)

- D-1 — `ShouldForceReturnToPool` cap-equality boundary unpinned. [MINOR]
"""

# "### D-CAP-001: title — RESOLVED" entry headings with field bullets
STORY_MAKER = """\
# Deferred Work

## From Epic 8 capstone live run (2026-06-11)

### D-CAP-001: claim entity references never resolved to canonical bible entity_ids — RESOLVED
- **Severity:** high (V0-blocker — broke approve→merge)
- **Detail:** Story 7.1 `extract_claims` derives references from draft text alone.
- **Resolution:** `spec-dw-cap-001-entity-id-resolution` — deterministic resolver added.

### D-CAP-002: an *ambiguous* claim reference is labeled `[NEW]` to the proposal composer
- **Severity:** low (rare — needs two bible entities sharing a slug)
- **Detail:** the label set is binary (KNOWN/NEW).

---

## D-8.6-001 — fact_key prefix not enforced against the character's entity_id (Story 8.6)

- **Surfaced by:** Story 8.6 retro review (Blind Hunter), 2026-06-04.
- **Severity:** low — a mis-prefixed key still lands in this character's own delta file.
"""

# strikethrough sections/bullets, bold-titled open bullets, no native ids
NOTEY = """\
# Deferred Work

## ~~Deferred from: Epic 1 — Instant Note Capture (2026-04-03)~~ DONE

### ~~Cluster 2: Frontend Core (Stories 1.6–1.10)~~ DONE

~~Depends on: Backend Foundation (Stories 1.1–1.5)~~

- ~~**Story 1.6** — Design Token System (CSS Custom Properties)~~

### ~~Cluster 3: Window & Daemon (Stories 1.11–1.14)~~ DONE

### Deferred from: code review of 3-2-full-text-search-tauri-command (2026-04-06) — 1 open item remaining

- ~~**Snippet `<mark>` HTML tags — XSS risk** — raw `<mark>` HTML injected.~~ → Verified safe: rendered as React text nodes
- **`i64`-to-`number` precision loss** — Specta maps Rust `i64` to JS `number`. IDs beyond 2^53 lose precision silently.
"""

# topic sections with status-suffixed headings and marker-suffixed bullets
MUDCEPTION = """\
# Deferred Work

## Epic 0: Validation (remaining stories)

- ~~**0-1**: Project scaffold — monorepo structure, workspace initialization~~ DONE
- ~~**0-2**: Hello-world SpacetimeDB module — room/exit tables~~ DONE

## Auth Improvements (deferred from 0-6 review)

- ~~OIDC id_token stored as plaintext in user:// — consider OS keychain~~ DOCUMENTED
- Add client_disconnected reducer to clean up orphaned player rows

## Notification Table Visibility (deferred from Story 1.5 review — RESOLVED)

## Config Externalization (split from Epic 0 deferred — DONE)

- ~~Move OIDC client ID to external config file~~ DONE (web already had .env)
"""


def test_legacy_lights_out_shape():
    entries = parse_legacy(LIGHTS_OUT)
    assert [e.id for e in entries] == ["W1", "W2", "W1", "W-1.2-c", "D-1"]
    w1, w2, w1_dup, w12c, d1 = entries
    assert w1.done and w1.severity == "high"  # [MAJOR], **RESOLVED**/[CLOSED]
    assert w1.title.startswith("Validate the dual-clock squeeze")
    assert not w2.done and w2.severity == "low"  # [MINOR]
    assert not w1_dup.done and w1_dup.severity == "low"  # [MINOR→MAJOR ...]
    assert w1.key != w1_dup.key  # same native id, different sections
    assert w12c.done  # plain "CLOSED:" prefix after the bold id
    assert w12c.title.startswith("`moveSpeed` finite > 0 law")
    assert not d1.done
    assert "epics-review absorb" in d1.section  # dated heading is a section


def test_legacy_story_maker_shape():
    entries = parse_legacy(STORY_MAKER)
    assert [e.id for e in entries] == ["D-CAP-001", "D-CAP-002", "D-8.6-001"]
    cap1, cap2, d86 = entries
    assert cap1.done  # "— RESOLVED" heading suffix
    assert cap1.title.endswith("bible entity_ids")  # suffix trimmed
    assert cap1.severity == "high"  # from "- **Severity:** high"
    assert not cap2.done and cap2.severity == "low"  # [NEW] is not a severity
    assert not d86.done and d86.severity == "low"  # "—"-separated heading id
    # field bullets are entry body, not standalone items
    assert "Surfaced by" in d86.body
    assert "**Detail:**" in cap1.body


def test_legacy_notey_shape():
    entries = parse_legacy(NOTEY)
    assert len(entries) == 4
    story16, cluster3, xss, i64 = entries
    assert story16.done  # struck bullet under a struck section
    assert story16.title == "Story 1.6 — Design Token System (CSS Custom Properties)"
    # item-less done section emits itself; the parent epic heading (which has
    # child headings) does not
    assert cluster3.done and "Cluster 3" in cluster3.title
    assert not any("Epic 1" in e.title for e in entries)
    assert xss.done and xss.title == "Snippet `<mark>` HTML tags — XSS risk"
    assert not i64.done  # the one open bullet in a "1 open item remaining" section
    assert i64.title == "`i64`-to-`number` precision loss"
    assert i64.id == ""


def test_legacy_mudception_shape():
    entries = parse_legacy(MUDCEPTION)
    assert len(entries) == 6
    assert [e.id for e in entries[:2]] == ["0-1", "0-2"]
    assert all(e.done for e in entries[:2])  # "Epic 0:" is a section, not an id
    oidc, reducer, notif, config = entries[2:]
    assert oidc.done  # ~~...~~ DOCUMENTED
    assert not reducer.done  # plain bullet in an open section
    assert notif.done and "Notification Table Visibility" in notif.title
    assert config.done  # bullet under a "(... — DONE)" section


def test_legacy_ignores_canonical_ledger():
    assert parse_legacy(LEDGER) == []
    assert not has_legacy(LEDGER)
    assert has_legacy(NOTEY)


def test_mixed_ledger_keeps_both_views_separate():
    mixed = (
        LEDGER + "\n## Deferred from: code review of spec-9-9 (2026-06-12)\n\n"
        "- ~~**Old fixed thing** — was broken, now fixed~~ → fixed in 9.9\n"
        "- **New open thing** — `parser.py` mishandles em-dashes — needs a guard\n"
    )
    assert open_ids(mixed) == {"DW-1", "DW-3"}  # strict view unchanged
    entries = parse_legacy(mixed)
    assert [e.done for e in entries] == [True, False]
    assert all("DW-" not in e.body for e in entries)


def test_flat_append_after_canonical_stays_visible_to_legacy_parser():
    text = (
        "# Deferred Work\n\n"
        "### DW-1: canonical\n\n"
        "origin: test\nlocation: n/a\nreason: test\nstatus: open\n\n"
        "- source_spec: `spec-next.md`\n"
        "  summary: later flat finding\n"
        "  evidence: must not be swallowed by DW-1\n"
    )

    (canonical,) = parse_ledger(text)
    (legacy,) = parse_legacy(text)

    assert "later flat finding" not in canonical.body
    assert legacy.title == "later flat finding"


# The canonical-span boundary has to recognize every flat shape parse_legacy
# does (#304): a boundary keyed on the full three-line block leaves the bug in
# place for the partial ones — and those are not hypothetical, they are the
# shapes test_flat_appender_missing_summary_falls_back and
# test_flat_appender_in_done_section_is_done already pin. Each case asserts both
# halves of the fix: the block reaches parse_legacy AND DW-1 keeps its status.
FLAT_TAIL_SHAPES = {
    "full-block": "- source_spec: `s.md`\n  summary: finding\n  evidence: e\n",
    "no-summary": "- source_spec: `s.md`\n  evidence: orphaned note\n",
    "no-evidence": "- source_spec: `s.md`\n  summary: finding\n",
    "swapped-order": "- source_spec: `s.md`\n  evidence: e\n  summary: finding\n",
    "field-interleaved": "- source_spec: `s.md`\n  severity: high\n  summary: finding\n",
    "star-bullet": "* source_spec: `s.md`\n  summary: finding\n  evidence: e\n",
    "tab-after-marker": "-\tsource_spec: `s.md`\n  summary: finding\n  evidence: e\n",
    "padded-marker": "-   source_spec: `s.md`\n  summary: finding\n  evidence: e\n",
    "uppercase-field": "- SOURCE_SPEC: `s.md`\n  summary: finding\n  evidence: e\n",
}


@pytest.mark.parametrize("shape", sorted(FLAT_TAIL_SHAPES), ids=sorted(FLAT_TAIL_SHAPES))
def test_flat_append_visible_for_every_shape_parse_legacy_accepts(shape):
    text = (
        "# Deferred Work\n\n### DW-1: canonical\n\n"
        "origin: test\nlocation: n/a\nreason: test\nstatus: open\n\n" + FLAT_TAIL_SHAPES[shape]
    )

    (canonical,) = parse_ledger(text)
    (legacy,) = parse_legacy(text)

    assert canonical.open, "the boundary must not cost DW-1 its status"
    assert "source_spec" not in canonical.body
    assert legacy.body.lstrip().startswith(("- ", "* ", "-\t"))


def test_flat_boundary_never_truncates_above_the_entry_status_line():
    """A flat-shaped bullet *inside* an entry, ahead of its `status:`, must not
    move the span end above that status: an entry parsing as neither open nor
    done is a lost tracked entry, the same failure class in the other direction
    (open_ids() drops it, classify() reports it malformed)."""
    text = (
        "### DW-1: canonical\n\norigin: test\nlocation: n/a\n"
        "reason: the appender writes this shape, quoted here as evidence\n"
        "- source_spec: `quoted-example.md`\n"
        "  summary: quoted, not a real finding\n"
        "  evidence: prose inside DW-1, not an appended block\n"
        "status: open\n"
    )

    (canonical,) = parse_ledger(text)

    assert canonical.status == "open" and canonical.open
    assert "quoted-example.md" in canonical.body
    assert open_ids(text) == {"DW-1"}
    assert parse_legacy(text) == []


def test_flat_boundary_still_applies_after_a_quoted_block_inside_the_entry():
    """The in-entry bullet does not grant immunity to what follows it: a real
    appended block after the entry's `status:` is still bounded out."""
    text = (
        "### DW-1: canonical\n\norigin: test\nreason: prose\n"
        "- source_spec: `quoted-example.md`\n  summary: quoted\n  evidence: e\n"
        "status: open\n\n"
        "- source_spec: `real.md`\n  summary: real finding\n  evidence: e\n"
    )

    (canonical,) = parse_ledger(text)
    (legacy,) = parse_legacy(text)

    assert canonical.open
    assert "quoted-example.md" in canonical.body and "real finding" not in canonical.body
    assert legacy.title == "real finding"


def test_flat_boundary_exposes_the_block_when_the_entry_has_no_status_line():
    """No status line means nothing to protect — the entry is already malformed,
    so bounding the block out of it costs nothing and rescues the finding."""
    text = "# Deferred Work\n\n### DW-1: malformed\n\norigin: test\nreason: test\n\n" + (
        "- source_spec: `s.md`\n  summary: rescued finding\n  evidence: e\n"
    )

    (canonical,) = parse_ledger(text)
    (legacy,) = parse_legacy(text)

    assert canonical.status == "" and not canonical.open
    assert legacy.title == "rescued finding"


def test_flat_append_boundary_accepts_crlf():
    text = "\r\n".join(
        [
            "# Deferred Work",
            "",
            "### DW-1: canonical",
            "",
            "origin: test",
            "location: n/a",
            "reason: test",
            "status: open",
            "",
            "- source_spec: `spec-next.md`",
            "  summary: later flat finding",
            "  evidence: must not be swallowed by DW-1",
            "",
        ]
    )

    (canonical,) = parse_ledger(text)
    (legacy,) = parse_legacy(text)

    assert canonical.open
    assert "later flat finding" not in canonical.body
    assert legacy.title == "later flat finding"


@pytest.mark.parametrize("gap", ["\n", ""], ids=["blank-line", "no-blank-line"])
def test_flat_append_between_two_canonical_entries(gap):
    """The realistic sequence: the inner session defers (flat) and the
    orchestrator then refiles a canonical entry at EOF, sandwiching the block."""
    text = (
        "# Deferred Work\n\n### DW-1: a\n\norigin: t\nreason: t\nstatus: open\n"
        + gap
        + "- source_spec: `s.md`\n  summary: sandwiched finding\n  evidence: e\n"
        + "\n### DW-2: b\n\norigin: t\nreason: t\nstatus: open\n"
    )

    assert open_ids(text) == {"DW-1", "DW-2"}
    (legacy,) = parse_legacy(text)
    assert legacy.title == "sandwiched finding"


def test_two_flat_blocks_after_the_last_entry_are_both_visible():
    block = "- source_spec: `s.md`\n  summary: %s\n  evidence: e\n"
    text = (
        "# Deferred Work\n\n### DW-1: a\n\norigin: t\nreason: t\nstatus: open\n\n"
        + (block % "first")
        + (block % "second")
    )

    assert open_ids(text) == {"DW-1"}
    assert [e.title for e in parse_legacy(text)] == ["first", "second"]


def test_flat_append_followed_by_a_legacy_section_keeps_both():
    text = (
        "# Deferred Work\n\n### DW-1: a\n\norigin: t\nreason: t\nstatus: open\n\n"
        "- source_spec: `s.md`\n  summary: flat finding\n  evidence: e\n"
        "\n## Deferred from: an older review (2026-06-01)\n\n"
        "- W1 — a freeform legacy item\n"
    )

    assert open_ids(text) == {"DW-1"}
    assert [e.title for e in parse_legacy(text)] == ["flat finding", "a freeform legacy item"]


def test_legacy_item_does_not_swallow_masked_canonical_neighbor():
    text = (
        "## Deferred from: somewhere (2026-06-01)\n\n"
        "- open legacy item directly above a DW entry\n"
        "### DW-9: Canonical\n\nstatus: open\n"
    )
    entries = parse_legacy(text)
    assert len(entries) == 1
    assert "DW-9" not in entries[0].body and "status:" not in entries[0].body
    assert open_ids(text) == {"DW-9"}


def test_legacy_keys_stable_under_unrelated_edits():
    before = {e.title: e.key for e in parse_legacy(MUDCEPTION)}
    edited = MUDCEPTION.replace(
        "- ~~**0-1**: Project scaffold — monorepo structure, workspace initialization~~ DONE\n",
        "",
    )
    after = {e.title: e.key for e in parse_legacy(edited)}
    for title, key in after.items():
        assert before[title] == key


def test_legacy_prose_and_rules_are_not_items():
    text = (
        "# Deferred Work\n\nSome intro prose, not an item.\n\n---\n\n"
        "## Open section\n\nNarrative paragraph under a section.\n\n- real item one\n"
    )
    entries = parse_legacy(text)
    assert [e.title for e in entries] == ["real item one"]


def test_field_severity_forms():
    assert field_severity("severity: HIGH") == "high"
    assert field_severity("- **Severity:** medium (scoped)") == "medium"
    assert field_severity("priority: blocker") == "critical"
    assert field_severity("severity: n/a") is None
    assert field_severity("no field here") is None


# the generic bmad-dev-auto review appender flat shape (step-04 deferral)
FLAT_APPENDER = """\
# Deferred Work

## Deferred from: spec-3-2-digest (2026-06-20)

- source_spec: `spec-3-2-digest.md`
  summary: Digest scheduler ignores the user timezone offset
  evidence: `schedule.py` hardcodes UTC; surfaced while reviewing the diff
- source_spec: `spec-3-2-digest.md`
  summary: No retry on transient SMTP failures
  evidence: send() raises and the run aborts with no backoff
"""


def test_flat_appender_uses_summary_as_title():
    entries = parse_legacy(FLAT_APPENDER)
    assert len(entries) == 2
    tz, smtp = entries
    assert tz.title == "Digest scheduler ignores the user timezone offset"
    assert smtp.title == "No retry on transient SMTP failures"
    # flat entries are freshly-appended findings: open, no native id, no severity
    assert not tz.done and not smtp.done
    assert tz.id == "" and smtp.id == ""
    assert tz.severity is None
    # source_spec / evidence stay in the body for the migrating session to read
    assert "source_spec" in tz.body and "evidence" in tz.body
    assert has_legacy(FLAT_APPENDER)


def test_flat_appender_missing_summary_falls_back():
    text = "## Deferred\n\n- source_spec: `spec-x.md`\n  evidence: orphaned note\n"
    (entry,) = parse_legacy(text)
    assert entry.title.startswith("source_spec:")  # no summary → keep the raw line
    assert not entry.done


def test_flat_appender_in_done_section_is_done():
    text = (
        "## Deferred from: old review (2026-06-01) — DONE\n\n"
        "- source_spec: `spec-y.md`\n  summary: Already handled upstream\n"
    )
    (entry,) = parse_legacy(text)
    assert entry.done and entry.title == "Already handled upstream"


# ------------------------------------------------------- append_entry / next_seq


def test_next_seq_past_highest():
    text = "### DW-3: a\nstatus: open\n\n### DW-7: b\nstatus: done 2026-01-01\n"
    assert next_seq(text) == 8


def test_next_seq_empty_starts_at_one():
    assert next_seq("") == 1
    assert next_seq("# Deferred Work\n") == 1


def test_append_entry_numbers_and_writes(tmp_path):
    p = tmp_path / "deferred-work.md"
    p.write_text("# Deferred Work\n\n### DW-4: existing\norigin: test\nstatus: open\n")
    new_id = append_entry(
        p,
        title="follow-up still recommended for dw-x",
        origin="review-budget-followup",
        source_spec="spec-foo.md",
        reason="review budget exhausted, work committed",
        severity="low",
    )
    assert new_id == "DW-5"
    entries = {e.id: e for e in parse_ledger(p.read_text())}
    assert "DW-5" in entries and entries["DW-5"].open
    body = entries["DW-5"].body
    assert "origin: review-budget-followup" in body
    assert "location: n/a" in body
    assert "source_spec: `spec-foo.md`" in body
    assert "severity: low" in body
    assert "follow-up still recommended for dw-x" in body


def test_append_entry_preserves_a_supplied_location(tmp_path):
    """`n/a` is only the default the orchestrator's own refiles take — the field
    is documented as `<file:line or component>` and must round-trip verbatim."""
    p = tmp_path / "deferred-work.md"
    assert (
        append_entry(
            p,
            title="follow-up",
            origin="code-review",
            source_spec="spec-foo.md",
            reason="still open",
            location="src/foo.py:12",
        )
        == "DW-1"
    )
    (entry,) = parse_ledger(p.read_text())
    assert "location: src/foo.py:12" in entry.body


def test_append_entry_idempotent_for_open_origin_and_spec(tmp_path):
    p = tmp_path / "deferred-work.md"
    p.write_text("# Deferred Work\n")
    first = append_entry(
        p, title="t", origin="review-budget-followup", source_spec="spec-foo.md", reason="r"
    )
    assert first == "DW-1"
    again = append_entry(
        p, title="t2", origin="review-budget-followup", source_spec="spec-foo.md", reason="r2"
    )
    assert again is None  # an open entry with the same origin+spec already exists
    assert len(parse_ledger(p.read_text())) == 1
    # a different source_spec is not blocked
    other = append_entry(
        p, title="t3", origin="review-budget-followup", source_spec="spec-bar.md", reason="r3"
    )
    assert other == "DW-2"


def test_append_entry_not_blocked_when_prior_is_done(tmp_path):
    p = tmp_path / "deferred-work.md"
    p.write_text(
        "### DW-1: t\norigin: review-budget-followup\n"
        "source_spec: `spec-foo.md`\nstatus: done 2026-01-01\n"
    )
    new_id = append_entry(
        p, title="t2", origin="review-budget-followup", source_spec="spec-foo.md", reason="r"
    )
    assert new_id == "DW-2"  # prior entry is done, not open → re-file allowed


def test_append_entry_creates_missing_ledger(tmp_path):
    p = tmp_path / "sub" / "deferred-work.md"
    new_id = append_entry(p, title="t", origin="o", source_spec="s.md", reason="r")
    assert new_id == "DW-1" and p.is_file()


def test_append_entry_idempotency_ignores_incidental_substring(tmp_path):
    """An unrelated open entry that merely *mentions* the origin marker and the
    spec filename in its `reason:` prose must not suppress a legitimately new
    entry — dedup matches the canonical field lines, not raw body substrings."""
    p = tmp_path / "deferred-work.md"
    p.write_text(
        "### DW-1: unrelated\norigin: code review\n"
        "reason: see the origin: review-budget-followup note re spec-foo.md for context\n"
        "status: open\n"
    )
    new_id = append_entry(
        p, title="t", origin="review-budget-followup", source_spec="spec-foo.md", reason="r"
    )
    assert new_id == "DW-2"  # not suppressed by the incidental mentions


@pytest.mark.parametrize("field", ["title", "origin", "source_spec", "reason", "location"])
@pytest.mark.parametrize("brk", [*BREAK_CHARS, *BREAK_RUNS])
def test_append_entry_sanitizes_free_text_into_one_line(tmp_path, field, brk):
    """One canonical entry, every field on its own line — no injected heading and
    no second `status:` line, whichever field carried the break."""
    p = tmp_path / "deferred-work.md"
    values = {
        "title": "follow-up",
        "origin": "code-review",
        "source_spec": "spec-foo.md",
        "reason": "still open",
        "location": "src/foo.py:12",
    }
    values[field] += f"{brk}### DW-99: injected{brk}status: done 2026-01-01"

    assert append_entry(p, **values) == "DW-1"

    text = p.read_text(encoding="utf-8")
    (entry,) = parse_ledger(text)  # one entry: no phantom DW-99 heading
    assert entry.id == "DW-1" and entry.open  # not closed by the injected status
    lines = text.splitlines()
    assert len(lines) == 6  # heading + origin/location/source_spec/reason/status
    assert sum(line.startswith("### ") for line in lines) == 1
    assert sum(line.startswith("status:") for line in lines) == 1


@pytest.mark.parametrize("title", [*BREAK_CHARS, *BREAK_RUNS, " \n ", "\r\n\t", "  ", "\t", "\xa0"])
def test_append_entry_names_an_entry_with_no_usable_title(tmp_path, title):
    """Two routes to the same unusable heading. A break-only title collapses to
    nothing, and `### DW-1: ` is a heading `HEADING_RE`'s `(.+?)` does not match:
    the caller is handed an id no reader can find, with `next_seq` already past
    it. A whitespace-only title never reaches the sanitizer at all — no break, so
    the byte-identity fast path returns it unchanged and still truthy — and
    parses to a heading that renders blank in `status`, `--json` and the TUI.
    Both get a name; neither gets a space."""
    p = tmp_path / "deferred-work.md"

    assert append_entry(p, title=title, origin="o", source_spec="s.md", reason="r") == "DW-1"

    text = p.read_text(encoding="utf-8")
    (entry,) = parse_ledger(text)  # findable, and the id was not burned
    assert entry.id == "DW-1"
    assert entry.title == "(untitled DW-1)"  # identifiable, not blank
    assert next_seq(text) == 2
    assert len(text.splitlines()) == 6


def test_append_entry_leaves_an_already_empty_title_as_it_was(tmp_path):
    """The substitution is scoped to a title that *had* content and lost it to
    sanitizing. An empty title in, empty title out — that hole predates this
    guard, and widening the fix to cover it would change bytes for a value that
    never carried a break."""
    p = tmp_path / "deferred-work.md"

    assert append_entry(p, title="", origin="o", source_spec="s.md", reason="r") == "DW-1"

    assert p.read_text(encoding="utf-8").startswith("### DW-1: \n")


@pytest.mark.parametrize(
    ("origin", "source_spec"),
    [
        ("review-budget-followup", "spec-foo.md\nstatus: open"),
        ("review\ud800followup", "spec-foo.md"),
        ("review-budget-followup", "spec-\udfff-foo.md"),
    ],
    ids=["line-break", "surrogate-origin", "surrogate-source-spec"],
)
def test_append_entry_idempotence_survives_sanitizing(tmp_path, origin, source_spec):
    """Sanitizing must happen BEFORE the idempotence scan: that scan compares the
    caller's value against the stored line via `field_line_present`, so
    sanitizing afterwards would compare raw against sanitized and append a fresh
    entry on every replay of the same defer.

    Both sanitizing passes are in front of that scan, so both are covered here.
    The surrogate rows would otherwise regress the same way the break row does —
    a replayed defer whose origin carries a `\\ud800` matches nothing on disk
    (the ledger stores the U+FFFD) and burns a fresh id every run."""
    p = tmp_path / "deferred-work.md"

    first = append_entry(p, title="t", origin=origin, source_spec=source_spec, reason="r")
    again = append_entry(p, title="t2", origin=origin, source_spec=source_spec, reason="r2")

    assert first == "DW-1"
    assert again is None
    assert len(parse_ledger(p.read_text(encoding="utf-8"))) == 1


# ------------------------- surrogate neutralization at the sanitizer chokepoint (#329)
# Every row here calls a real writer with no monkeypatching: the value has to
# survive `_one_line`, `atomic_write_text`'s strict UTF-8 encode, and the strict
# read back. Delete the `neutralize_surrogates` call in `_one_line` and each one
# fails with `UnicodeEncodeError` — the crash #329 filed, arriving from a close
# path that calls these writers bare.


def test_append_entry_writes_a_finding_carrying_a_lone_surrogate(tmp_path):
    """A lone surrogate is not a line break, so it sailed past the break collapse
    untouched and detonated in the encode. Note the title stays truthy — `�` is a
    visible replacement, so `append_entry`'s `(untitled DW-<n>)` substitution
    deliberately does NOT fire for it."""
    p = tmp_path / "deferred-work.md"

    dw_id = append_entry(p, title="\ud800", origin="o", source_spec="s.md", reason="x\udfffy")

    assert dw_id == "DW-1"
    text = p.read_text(encoding="utf-8")  # strict read: unencodable text never got here
    (entry,) = parse_ledger(text)
    assert entry.title == "�"  # replaced, not vanished into `(untitled DW-1)`
    assert "reason: x�y" in entry.body


def test_append_decision_writes_a_label_and_detail_carrying_a_lone_surrogate(tmp_path):
    """`decisions.apply_pre_answer` calls this bare, so the raise would end the
    sweep. Both interpolated fields go through the chokepoint."""
    path = write_ledger(tmp_path)

    assert append_decision(path, "DW-3", "2026-06-11", "\ud800", "a\ud800b")

    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert "decision: 2026-06-11 � — a�b" in entries["DW-3"].body


def test_mark_done_writes_a_note_carrying_a_lone_surrogate(tmp_path):
    """The exact string `sweep._close_resolved` builds — `f"already resolved:
    {entry.evidence}"` — where `evidence` is the field the cached triage JSON
    revives a surrogate into."""
    path = write_ledger(tmp_path)

    assert mark_done(path, "DW-1", "2026-06-11", "already resolved: \ud800evidence")

    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert entries["DW-1"].status == "done 2026-06-11"
    assert "resolution: already resolved: �evidence" in entries["DW-1"].body


@pytest.mark.parametrize("status", ["", "done", "closed", "open\n### DW-99: injected", "OPEN"])
def test_append_entry_raises_on_a_noncanonical_status_without_writing(tmp_path, status):
    p = tmp_path / "deferred-work.md"

    with pytest.raises(ValueError, match="status must be"):
        append_entry(p, title="t", origin="o", source_spec="s.md", reason="r", status=status)

    assert not p.exists()


def test_append_entry_raises_on_an_impossible_done_date_without_writing(tmp_path):
    p = tmp_path / "deferred-work.md"

    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        append_entry(
            p, title="t", origin="o", source_spec="s.md", reason="r", status="done 2026-02-30"
        )

    assert not p.exists()


@pytest.mark.parametrize("severity", ["urgent", "blocker", "low\nseverity: high"])
def test_append_entry_raises_on_a_noncanonical_severity_without_writing(tmp_path, severity):
    """The whitelist is `SEVERITY_ALIASES`'s normalization targets. `blocker` is
    an inbound alias the *parser* accepts from LLM-written ledgers, not a value
    this writer may emit — so it raises like any other programmer bug."""
    p = tmp_path / "deferred-work.md"

    with pytest.raises(ValueError, match="severity must be one of"):
        append_entry(p, title="t", origin="o", source_spec="s.md", reason="r", severity=severity)

    assert not p.exists()


def test_append_entry_accepts_every_canonical_severity(tmp_path):
    """Pins the whitelist against its source: a `SEVERITY_ALIASES` value that the
    writer refuses would mean the two have drifted."""
    for i, severity in enumerate(sorted(set(SEVERITY_ALIASES.values())), start=1):
        p = tmp_path / f"ledger-{i}.md"
        assert (
            append_entry(
                p, title="t", origin="o", source_spec="s.md", reason="r", severity=severity
            )
            == "DW-1"
        )
        assert f"severity: {severity}" in p.read_text(encoding="utf-8")


def test_append_entry_leaves_a_clean_value_byte_identical(tmp_path):
    p = tmp_path / "deferred-work.md"

    assert (
        append_entry(
            p,
            title="follow-up  for  dw-x",
            origin="review-budget-followup",
            source_spec="spec-foo.md",
            reason="review budget exhausted, work committed",
            location="src/foo.py:12",
            severity="low",
        )
        == "DW-1"
    )

    assert p.read_text(encoding="utf-8") == (
        "### DW-1: follow-up  for  dw-x\n"
        "origin: review-budget-followup\n"
        "location: src/foo.py:12\n"
        "source_spec: `spec-foo.md`\n"
        "severity: low\n"
        "reason: review budget exhausted, work committed\n"
        "status: open\n"
    )


def test_append_entry_write_failure_raises_and_keeps_the_ledger(tmp_path, monkeypatch):
    """#328, the `append_entry` half — see the `append_decision` twin above."""
    path = write_ledger(tmp_path)
    before = path.read_bytes()

    def boom(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(deferredwork, "atomic_write_text", boom)
    with pytest.raises(OSError, match="disk full"):
        append_entry(
            path,
            title="new finding",
            origin="review of spec-foo.md",
            source_spec="spec-foo.md",
            reason="out of scope",
        )
    assert path.read_bytes() == before


def test_append_entry_encode_failure_cannot_truncate_the_ledger(tmp_path, monkeypatch):
    """#328's worst case, reached without any injected OSError: an unencodable
    value raises from inside the encode step itself. Under a bare `write_text`
    the file is already truncated by then, so the raise and the data loss arrive
    together — the exact compounding #329 describes."""
    path = write_ledger(tmp_path)
    before = path.read_bytes()
    # Patching the sanitizer to the identity is now LOAD-BEARING: since #329,
    # `_one_line` neutralizes surrogates, so without this patch the value would
    # reach the write already encodable and nothing would raise. Keeping it pins
    # the WRITE layer's defense independently of the sanitizer's — without it
    # this row would quietly stop testing #328 and become a second test of
    # #329's fix. Do not remove it.
    monkeypatch.setattr(deferredwork, "_one_line", lambda v: v)

    with pytest.raises(UnicodeEncodeError):
        append_entry(
            path,
            title="\ud800",
            origin="review of spec-foo.md",
            source_spec="spec-foo.md",
            reason="out of scope",
        )

    # The raise is NOT the invariant under test — a bare `write_text` raises here
    # too. The bytes are: this is the assertion that reddens without the fix.
    assert path.read_bytes() == before


def test_field_line_present_matches_field_not_substring():
    body = (
        "### DW-1: x\norigin: review-budget-followup\n"
        "source_spec: `spec-foo.md`\nreason: mentions spec-foobar.md and review-budget-followup-x\n"
        "status: open\n"
    )
    # exact field-line matches (plain and backtick-wrapped)
    assert field_line_present(body, "origin", "review-budget-followup")
    assert field_line_present(body, "source_spec", "spec-foo.md")
    # a superstring value must not match the shorter field line
    assert not field_line_present(body, "origin", "review-budget")
    # a value that only appears incidentally inside `reason:` is not a field line
    assert not field_line_present(body, "source_spec", "spec-foobar.md")


# ------------------------------ closes_deferred declaration primitives (#234)


def test_parse_declaration_normalizes_items_and_dedupes():
    """Lenient about items, exactly as the id parser is: an LLM-authored manifest
    may emit an unquoted id as a string and a bare number as an int."""
    ids, error = parse_declaration(["DW-1", " DW-2 ", 5, "", "DW-1"])
    assert ids == ("DW-1", "DW-2", "5")  # stripped, blanks dropped, order-preserving dedupe
    assert error is None


def test_parse_declaration_defaults_empty_when_absent():
    assert parse_declaration(None) == ((), None)
    assert parse_declaration([]) == ((), None)


def test_parse_declaration_rejects_a_non_list_container():
    """A string is iterable, so a lenient reading would silently turn one id into
    a list of characters. Callers pick the severity; the reading is shared."""
    ids, error = parse_declaration("DW-1")
    assert ids == ()
    assert error is not None and "must be a list" in error and "str" in error


def test_classify_partitions_open_done_unknown_and_malformed():
    """Four outcomes, not two: `not open` hides a satisfied declaration (a resume
    re-driving a landed close, which must stay silent) and an entry whose status
    line cannot be read at all (which must not)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: a\nstatus: open\n\n"
        "### DW-2: b\nstatus: done 2026-06-01\n\n"
        "### DW-3: c\nstatus: in-progress\n\n"
        "### DW-4: d\nreason: no status line at all\n"
    )
    declared = classify(text, ["DW-1", "DW-2", "DW-3", "DW-4", "DW-99"])

    assert declared.open_ids == ("DW-1",)
    assert declared.already_done == ("DW-2",)
    assert declared.unknown == ("DW-99",)
    assert declared.malformed == ("DW-3", "DW-4")


DUPLICATE_OPEN_FIRST = (
    "# Deferred Work\n\n"
    "### DW-1: the first copy\nstatus: open\n\n"
    "### DW-1: the second copy\nstatus: done 2026-06-01\n"
)
DUPLICATE_DONE_FIRST = (
    "# Deferred Work\n\n"
    "### DW-1: the first copy\nstatus: done 2026-06-01\n\n"
    "### DW-1: the second copy\nstatus: open\n"
)


@pytest.mark.parametrize(
    ("text", "expected_open", "expected_done"),
    [
        (DUPLICATE_OPEN_FIRST, ("DW-1",), ()),
        (DUPLICATE_DONE_FIRST, (), ("DW-1",)),
    ],
    ids=["open-first", "done-first"],
)
def test_classify_reads_the_same_duplicate_the_mutation_writes(text, expected_open, expected_done):
    """`classify` must name the entry `_find_entry` acts on. Indexing last-wins
    while the mutation takes the first made them disagree, and BOTH orders then
    closed nothing while saying nothing: done-first classified `open`, then
    `_apply_done` refused the done copy it found first (so nothing was marked and
    not even an unmatched warning followed); open-first classified `already_done`
    and never attempted the write (#284 round-6 review, finding 4)."""
    declared = classify(text, ["DW-1"])

    assert declared.open_ids == expected_open
    assert declared.already_done == expected_done
    assert declared.unknown == ()
    assert declared.duplicates == ("DW-1",)  # reported either way — the ledger is corrupt


@pytest.mark.parametrize(
    ("text", "expected", "after"),
    [
        (DUPLICATE_OPEN_FIRST, ["DW-1"], ["done", "done"]),
        (DUPLICATE_DONE_FIRST, [], ["done", "open"]),
    ],
    ids=["open-first", "done-first"],
)
def test_mark_done_many_agrees_with_classify_on_a_duplicated_id(tmp_path, text, expected, after):
    """The other half of the same contract: what `classify` calls open is exactly
    what the write marks, so an id is never reported closed without being closed —
    nor, as before, classified open and then silently left open.

    The done-first ledger still ends with an open copy nobody touched. That is the
    honest outcome of a corrupt ledger (#286) and it is why the close is journaled
    as a duplicate rather than passed over."""
    p = tmp_path / "deferred-work.md"
    p.write_text(text, encoding="utf-8")
    declared = classify(text, ["DW-1"])

    assert list(mark_done_many(p, declared.open_ids, "2026-07-24", "note")) == expected

    entries = parse_ledger(p.read_text(encoding="utf-8"))
    assert [e.status.split()[0] for e in entries] == after


def test_classify_reports_no_duplicates_for_a_well_formed_ledger():
    declared = classify(LEDGER, ["DW-1", "DW-2"])

    assert declared.duplicates == ()


def test_mark_done_many_writes_once_and_reports_what_landed(tmp_path):
    p = tmp_path / "deferred-work.md"
    p.write_text(LEDGER, encoding="utf-8")

    # DW-2 is already done in the fixture, DW-99 is absent — both skipped, neither
    # an error: only the entries that were open get marked, in the order given.
    marked = mark_done_many(
        p, ["DW-1", "DW-3", "DW-2", "DW-99"], "2026-07-24", "resolved by story 1"
    )

    assert marked == ["DW-1", "DW-3"]
    entries = {e.id: e for e in parse_ledger(p.read_text(encoding="utf-8"))}
    for dw in ("DW-1", "DW-3"):
        assert entries[dw].status == "done 2026-07-24"
        assert "resolution: resolved by story 1" in entries[dw].body


def test_mark_done_many_is_all_or_nothing_on_a_write_failure(tmp_path, monkeypatch):
    """A per-id read-modify-write loop leaves earlier marks on disk when it raises
    partway through — a half-applied closure the caller never gets to journal, so
    the ledger claims resolutions the run has no record of."""
    p = tmp_path / "deferred-work.md"
    p.write_text(LEDGER, encoding="utf-8")
    before = p.read_bytes()
    monkeypatch.setattr(
        "bmad_loop.deferredwork.atomic_write_text",
        lambda path, text: (_ for _ in ()).throw(OSError("disk full")),
    )

    # the raise must REACH the caller, not just leave the file alone: a swallowed
    # error returning [] reads as "nothing declared was open" — the engine disarms
    # its restore snapshot and no journal line ever records the failed write
    with pytest.raises(OSError):
        mark_done_many(p, ["DW-1", "DW-3"], "2026-07-24", "note")

    assert p.read_bytes() == before  # nothing partially applied


def test_mark_done_many_skips_an_already_done_entry(tmp_path):
    """Idempotent for a resume that re-drives a close that already landed: no
    second resolution line, and the id is not reported as newly marked."""
    p = tmp_path / "deferred-work.md"
    p.write_text(LEDGER, encoding="utf-8")
    mark_done_many(p, ["DW-1"], "2026-07-24", "resolved by story 1")

    again = mark_done_many(p, ["DW-1"], "2026-07-25", "resolved by story 1")

    assert again == []
    body = next(e for e in parse_ledger(p.read_text(encoding="utf-8")) if e.id == "DW-1").body
    assert body.count("resolution: resolved by story 1") == 1


def _gated(*lines: str):
    text = (
        "# Deferred Work\n\n### DW-1: gated entry\n\n"
        "origin: test\nlocation: n/a\nreason: test\nstatus: open\n"
        + "".join(f"{x}\n" for x in lines)
    )
    (entry,) = parse_ledger(text)
    return deferredwork.gates(entry)


def test_gates_unions_every_line_and_splits_on_commas():
    """Several `gate:` lines are one claim, not competing ones: a line-oriented
    file gives an author no reason to prefer one line over three. Duplicates
    collapse and a trailing separator is not a token."""
    g = _gated("gate: 3-2, 3-3", "gate:\t4-1,3-2,")

    assert g.tokens == ("3-2", "3-3", "4-1")
    assert g.malformed == ()
    assert not g.inert


@pytest.mark.parametrize("line", ["gate:", "gate: ", "gate: ,", "gate: , ,"])
def test_gates_reports_a_line_that_names_nothing(line):
    """`gate:` with nothing usable after it is a claim made inertly, and the
    parse alone cannot tell it apart from an entry that never gated anything —
    `lines` is what keeps it reportable. Left silent, it reads to anyone scanning
    the entry as a gate already in force."""
    g = _gated(line)

    assert g.tokens == () and g.malformed == ()
    assert g.inert
    assert g.empty == 1


def test_gates_counts_an_empty_line_beside_a_valid_one():
    """An entry can gate one story and name nothing on the next line. `inert` is an
    entry-wide verdict and answers False here — the entry does have a token — so the
    empty line needs its own count or the operator who wrote it is never told the
    second gate holds nothing back."""
    g = _gated("gate: 3-2", "gate:")

    assert g.tokens == ("3-2",)
    assert g.lines == 2
    assert not g.inert  # the entry-wide verdict cannot express this case...
    assert g.empty == 1  # ...which is why the per-line count exists


def test_gates_reports_a_token_that_cannot_name_a_story():
    """The separator is a comma and only a comma. Reading `3-2 3-3` leniently
    would guess at one the format never promised, so it lands in `malformed` —
    surfaced by validate rather than silently gating nothing."""
    g = _gated("gate: 3-2 3-3, ../etc, 4-1")

    assert g.tokens == ("4-1",)
    assert g.malformed == ("3-2 3-3", "../etc")


@pytest.mark.parametrize("fence", ["```markdown", "```", "~~~"])
def test_a_fenced_example_is_not_a_gate_declaration(fence):
    """An entry whose subject IS this field quotes it, and a quoted example sits in
    column 0 — right where the strict field anchor looks. The sibling
    `HARD_GATE_PROSE_RE` already needed quote guards for exactly this (its comment
    records the warning firing on entries documenting the convention, this repo's
    own docs included), and `gate:` is worse off: the answer here is a refusal, so
    an entry explaining the field would fail validate and pause a run."""
    close = "```" if fence.startswith("`") else "~~~"
    g = _gated(fence, "gate: 3-2", close)

    assert g.tokens == () and g.near_miss == 0 and g.lines == 0


@pytest.mark.parametrize(("outer", "inner"), [("```", "~~~"), ("~~~", "```")])
def test_a_stray_opener_above_the_heading_does_not_mask_a_live_gate(outer, inner):
    """The two views of the same line, and the reason `_quoted` asks at FILE scope.

    The stray `outer` opener never closes (`inner` is the other fence char and
    cannot close it), and at whole-file scope `unclosed_hides_rest=False` reads it
    as ordinary text — which is why the heading below it still carves an entry. A
    body slice starts at that heading, cannot see the opener, and so reads the
    matched `inner` pair as a real fence, masking the `gate:` between them into an
    example. That drops a live gate in silence, which is the failure the field
    exists to end; `parse_ledger` already reads headings and `status:` at file
    scope for exactly this reason. Found by differential fuzz against the
    whole-file predicate, not by inspection."""
    text = f"# Deferred Work\n\n{outer}\n### DW-2: title\n{inner}\ngate: 3-2\n{inner}\n"

    (entry,) = parse_ledger(text)

    assert deferredwork.gates(entry).tokens == ("3-2",)


def test_a_stray_opener_above_the_heading_does_not_mask_a_prose_gate():
    """The prose scan shares `_quoted`, so it shares the file-scope question too —
    pinned separately because the two scans reach it by different call paths."""
    text = "# Deferred Work\n\n```\n### DW-2: title\n~~~\nHARD GATE: before 3-2\n~~~\n"

    (entry,) = parse_ledger(text)

    assert deferredwork.declares_prose_gate(entry) is True


def test_a_fence_hides_only_itself():
    """The mask must not reach past the block. A real declaration on either side of
    a quoted example still gates — otherwise the fix for a false refusal would have
    bought a lost gate, which is the worse of the two."""
    g = _gated("gate: 4-1", "```", "gate: 3-2", "```", "gate: 5-1")

    assert g.tokens == ("4-1", "5-1")


def test_an_unclosed_fence_swallows_no_gate():
    """The deliberate asymmetry. Masking an unterminated fence to end-of-entry
    would let one stray ``` silently disable every gate below it — the exact
    silent miss this field exists to end. A malformed-markdown entry keeping a
    readable gate is the cheaper wrong answer."""
    g = _gated("```", "an example nobody closed", "gate: 4-1")

    assert g.tokens == ("4-1",)


def test_a_line_with_an_info_string_does_not_close_a_fence():
    """A closer carries no info string (CommonMark), and the rule has to be tested
    at EQUAL fence length or the length rule answers first and the assertion
    measures nothing. Here every line is a 3-backtick run: without the
    info-string requirement, the ```python would close the block early, re-expose
    `gate: 4-1`, and leave the trailing ``` opening an unclosed fence."""
    g = _gated("```", "gate: 3-2", "```python", "gate: 4-1", "```")

    assert g.tokens == ()


def test_a_four_space_backtick_run_is_indented_code_and_cannot_silence_a_gate():
    """CommonMark indents a fence up to three spaces; at four it is indented code,
    not a delimiter. `FENCE_LINE_RE`'s ` {0,3}` is the only thing enforcing that,
    and it enforces it in the fail-OPEN direction: were the run accepted, two such
    lines would wrap a real column-0 `gate:` and mask it out of existence — a gate
    lost in silence, the exact failure this field exists to end. The devcontract
    half already reasoned this through (reviewer guard #53): the limit is safe
    because fenced content in a list is co-indented and can never match a
    column-0 anchor, so only the delimiter rule needs pinning."""
    g = _gated("    ```", "gate: 4-1", "    ```")

    assert g.tokens == ("4-1",)


def test_a_fenced_worked_example_is_not_an_entry():
    """A complete example — heading, status and gate inside one fence — is the
    shape `deferred-work-format.md` ships for authors to copy, so a ledger quoting
    it is expected rather than exotic. Read entry-locally it used to become a real
    entry: `HEADING_RE` split the file first, stranding the opening fence in the
    PREVIOUS entry, so the example's body saw no open fence and its `gate:` went
    live — a phantom entry refusing a story nobody deferred."""
    text = (
        "# Deferred Work\n\n### DW-01: a real entry\nstatus: open\n\n"
        "The format, for reference:\n\n"
        "```markdown\n### DW-99: worked example\nstatus: open\ngate: 3-2\n```\n"
    )

    (entry,) = parse_ledger(text)

    assert entry.id == "DW-01"
    assert open_ids(text) == {"DW-01"}
    assert deferredwork.gates(entry).tokens == ()


def test_a_fenced_heading_does_not_bound_the_entry_that_quotes_it():
    """The other half of skipping fenced headings, and the one that fails OPEN.
    `ANY_HEADING_RE` ends an entry at any intervening heading; left fence-blind it
    would end this one at the quoted `### DW-99`, dropping the real `gate:` below
    the example out of the span entirely. Trading a phantom entry for a lost gate
    would have been the worse of the two bugs."""
    text = (
        "# Deferred Work\n\n### DW-01: a real entry\nstatus: open\n\n"
        "```markdown\n### DW-99: worked example\nstatus: done 2026-01-01\n```\n\n"
        "gate: 3-2\n"
    )

    (entry,) = parse_ledger(text)

    assert deferredwork.gates(entry).tokens == ("3-2",)


def test_a_fenced_flat_bullet_does_not_bound_the_entry_that_quotes_it():
    """Same failure through the #304 flat-appender boundary: a quoted bullet is an
    example of the appender's shape, not an appended block, and bounding the entry
    at it would again strand the `gate:` below. The real block must still be
    bounded out — `test_flat_boundary_still_applies_after_a_quoted_block_inside_the_entry`
    holds that end."""
    text = (
        "# Deferred Work\n\n### DW-01: a real entry\nstatus: open\n\n"
        "```markdown\n- source_spec: `example.md`\n  summary: quoted\n"
        "  evidence: e\n```\n\ngate: 3-2\n"
    )

    (entry,) = parse_ledger(text)

    assert deferredwork.gates(entry).tokens == ("3-2",)


def test_a_stray_unclosed_fence_does_not_erase_the_entries_below_it():
    """Why `_example` asks with `unclosed_hides_rest=False`. Under the opposite
    answer one unterminated fence would swallow every heading after it, and those
    entries would vanish from `open_ids()` — real open work reported as landed, in
    silence. A phantom entry from a stray opener is today's behaviour and is
    visible on the page; a disappeared ledger is neither."""
    text = (
        "# Deferred Work\n\n### DW-01: oops\nstatus: open\n```\n\n"
        "### DW-02: still real\nstatus: open\ngate: 3-2\n"
    )

    first, second = parse_ledger(text)

    assert (first.id, second.id) == ("DW-01", "DW-02")
    assert open_ids(text) == {"DW-01", "DW-02"}
    assert deferredwork.gates(second).tokens == ("3-2",)


def test_a_fenced_status_line_is_not_the_status_of_the_entry_quoting_it():
    """Skipping fenced headings moves the example INSIDE the quoting entry instead
    of splitting it off, which hands `STATUS_RE` a second candidate it never used
    to see. An entry with no status of its own must not inherit the example's:
    reading `done` there would drop live work out of `open_ids()` on the strength
    of a quotation."""
    text = (
        "# Deferred Work\n\n### DW-01: no status of its own\n\norigin: test\n\n"
        "```markdown\n### DW-99: worked example\nstatus: done 2026-01-01\n```\n"
    )

    (entry,) = parse_ledger(text)

    assert entry.status == ""
    assert not entry.done and not entry.open


def test_a_backtick_run_carrying_backticks_is_inline_code_not_a_fence():
    """CommonMark forbids a backtick anywhere in a BACKTICK fence's info string,
    exactly so a line of inline code does not open a block. Without the rule this
    line opens one, the trailing ``` closes it, and the `gate:` between them reads
    as a quoted example — a gate lost in silence, which is the failure this field
    exists to end, not the spurious refusal the fenced path is allowed to make."""
    g = _gated("```gate:``` is the field name.", "gate: 3-2", "```")

    assert g.tokens == ("3-2",)


def test_a_tilde_fence_may_carry_backticks_in_its_info_string():
    """The other arm of the same rule, and the reason it is not simply "no
    backticks in an info string": the restriction is backtick-only, because a
    tilde run cannot appear in inline code. Dropping the fence-char test would
    leave this example live and refuse a story the entry only documented."""
    g = _gated("~~~ `inline`", "gate: 3-2", "~~~")

    assert g.tokens == ()


def test_a_fenced_example_is_not_a_legacy_finding_either():
    """The far side of skipping fenced headings (#514). While `parse_ledger` read a
    quoted example as a phantom canonical entry, that entry's span masked the
    quotation out of this reader by accident; removing the phantom removed the
    accident, and the same bullet surfaced here as a legacy finding instead. The
    real block below must still parse — over-masking would lose a tracked item,
    which is the failure `parse_legacy` exists to prevent."""
    text = (
        "# Deferred Work\n\nThe format, for reference:\n\n"
        "```markdown\n### DW-1: wire the blob-storage credentials\nstatus: open\n"
        "gate: 3-2\n- source_spec: `example.md`\n  summary: quoted\n  evidence: e\n```\n\n"
        "- source_spec: `real.md`\n  summary: real finding\n  evidence: e\n"
    )

    (legacy,) = parse_legacy(text)

    assert legacy.title == "real finding"
    assert parse_ledger(text) == []


def test_a_stray_unclosed_fence_does_not_hide_legacy_findings_below_it():
    """`parse_legacy` asks `fences.fenced_spans` with `unclosed_hides_rest=False`
    for the reason the canonical side does: under the opposite answer one
    unterminated fence would blank every finding after it out of the ledger, and
    a lost legacy item is as silent as a lost entry."""
    text = "# Deferred Work\n\n```\n\n- source_spec: `real.md`\n  summary: real finding\n  evidence: e\n"

    (legacy,) = parse_legacy(text)

    assert legacy.title == "real finding"


QUOTED_STATUS_LEDGER = """\
# Deferred Work

### DW-1: an entry that quotes the format in an example

summary: shows an operator what an entry looks like
evidence: e

```
status: open
```

status: open
gate: 3-2
"""


def test_a_close_rewrites_the_live_status_and_not_a_quoted_one(tmp_path):
    """The reader picks the status with a fence-aware lookup, so a writer that ran
    `STATUS_RE.search(body)` again would pick the *first* raw match — the quoted
    one. That split is worse than either half alone: `mark_done_many` reports the
    id as closed while `open_ids` still lists it, so a sweep or story close can
    never actually close the entry, and a `gate:` it carries refuses its story on
    every following pass. Asserted on `open_ids` rather than on the return value,
    because the return value is exactly what the bug got right."""
    path = write_ledger(tmp_path, QUOTED_STATUS_LEDGER)

    assert mark_done_many(path, ["DW-1"], "2026-06-11", "fixed") == ["DW-1"]

    text = path.read_text(encoding="utf-8")
    assert open_ids(text) == set()
    (entry,) = parse_ledger(text)
    assert entry.status == "done 2026-06-11"
    # the quoted example is documentation, and a close must not edit it
    assert "```\nstatus: open\n```" in text


def test_a_reopen_restores_the_live_status_of_an_entry_that_quotes_an_example(tmp_path):
    """The undo path reads its marker at an offset taken from the status line, so
    it has to start from the same line the close wrote. Round-tripped rather than
    asserted field-by-field: the close and the reopen must agree about *which*
    line they own, and only the round trip pins that they do."""
    path = write_ledger(tmp_path, QUOTED_STATUS_LEDGER)
    close_reopenable(path, "DW-1", "fixed")
    assert open_ids(path.read_text(encoding="utf-8")) == set()

    assert mark_open(path, "DW-1", "fixed", OPERATION_ID) is True

    assert path.read_text(encoding="utf-8") == QUOTED_STATUS_LEDGER


def test_a_decision_lands_after_the_live_status_of_an_entry_that_quotes_an_example(
    tmp_path,
):
    """`_insert_after_status` is the third writer that used to re-derive the status
    line. Inserting after the quoted one would bury the decision inside the fenced
    example, where every reader — the parser and the human — treats it as prose."""
    path = write_ledger(tmp_path, QUOTED_STATUS_LEDGER)

    assert append_decision(path, "DW-1", "2026-06-11", "keep", "still worth doing") is True

    text = path.read_text(encoding="utf-8")
    assert "```\nstatus: open\n```" in text
    assert "status: open\ndecision: 2026-06-11 keep — still worth doing" in text


def test_the_example_index_answers_exactly_at_its_span_bounds():
    """The binary search replaced a linear `any(s <= offset < e)` test, and the
    whole suite passes with its upper bound moved by one character — no heading or
    field line ever begins at that offset, so no behavioural test can reach it.
    Pinned directly for the same reason a differential fuzz found it and the tests
    did not: an index that is right about every real offset and wrong about the
    boundary is one refactor away from being wrong about a real one."""
    text = "before\n```\nquoted\n```\nafter\n"
    examples = deferredwork._example_spans(text)

    ((start, end),) = examples.spans
    assert examples.covers(start - 1) is False
    assert examples.covers(start) is True
    assert examples.covers(end - 1) is True
    assert examples.covers(end) is False
    # and the index must not answer for a ledger that quotes nothing
    assert deferredwork._example_spans("### DW-1: t\nstatus: open\n").covers(0) is False


def test_parse_ledger_walks_the_fences_once_however_many_entries(monkeypatch):
    """Asserted as a call count, not a duration: the property is that the fence
    walk is hoisted out of the per-offset checks, and a timing threshold would
    both flake and stop meaning that. Reading fence state per offset made
    `parse_ledger` quadratic in entries, and `Engine._refuse_gated_story` re-parses
    before every story dispatch, so a mature ledger paid it on the dispatch path.

    Both bindings are patched because the module imported the name at import time
    (`from .fences import fenced_spans`), so patching only `fences` would miss the
    direct call and only `deferredwork` would miss any walk reached via
    `fences.fenced`."""
    walks: list[int] = []
    real = fences.fenced_spans

    def counting(text: str, **kw: object):
        walks.append(len(text))
        return real(text, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(fences, "fenced_spans", counting)
    monkeypatch.setattr(deferredwork, "fenced_spans", counting)
    text = "# Deferred Work\n" + "".join(
        f"\n### DW-{i}: entry {i}\n\norigin: o\nreason: r\nstatus: open\n" for i in range(1, 26)
    )

    assert len(parse_ledger(text)) == 25
    assert len(walks) == 1


def test_gate_token_shape_copy_agrees_with_the_stories_id_it_mirrors():
    """`_STORIES_ID_RE` is a copy of `stories.ID_RE`, taken because `stories`
    imports this module and the reverse would cycle. Pinned to the original rather
    than to a comment: if the manifest ever admits a new id shape, a gate on one
    would start reporting `malformed` and refuse nothing."""
    from bmad_loop import stories

    assert deferredwork._STORIES_ID_RE.pattern == stories.ID_RE.pattern


@pytest.mark.parametrize("token", ["3.2", "3_2", "3.2-invite", "3_2-invite"])
def test_gates_reject_a_token_no_story_key_can_carry(token):
    """Shape-valid and unmatchable. `GATE_TOKEN_RE` admits `.` and `_` because a
    sprint slug may contain them, so `gate: 3.2` used to land in `tokens` — where
    it matched nothing, gated nothing, and reported a green `ok` for doing so.
    A `.`/`_` in the *number* prefix is what no legal key can carry."""
    g = _gated(f"gate: {token}")

    assert g.tokens == ()
    assert g.malformed == (token,)


@pytest.mark.parametrize("token", ["3-2-foo.bar", "3-2-a_b", "authz-login", "3"])
def test_gates_keep_a_token_a_sprint_slug_can_actually_spell(token):
    """The trap in the fix above: `sprintstatus.STORY_RE`'s slug is unconstrained,
    so `3-2-foo.bar` and `3-2-a_b` are LEGAL keys that gate correctly. Banning `.`
    and `_` outright — the obvious reading of "reject 3.2" — would refuse real
    gates, turning a fail-open into a false refusal."""
    g = _gated(f"gate: {token}")

    assert g.tokens == (token,)
    assert g.malformed == ()


@pytest.mark.parametrize("line", ["Gate: 3-2", "GATE: 3-2", "  gate: 3-2", "\tgate: 3-2"])
def test_gates_count_a_line_the_field_anchor_will_never_read(line):
    """`GATE_RE` is a lowercase `gate:` in column 0. Every other spelling produced
    ZERO findings — no gate, no warning, nothing — which is the field failing open,
    where a missed `status:` now fails closed. Counted, not parsed: accepting an
    indented line would read a fenced example inside an entry as a live gate."""
    g = _gated(line)

    assert g.tokens == ()  # deliberately NOT enforced...
    assert g.near_miss == 1  # ...but no longer silent
    assert g.lines == 0


def test_gates_do_not_count_the_canonical_spelling_as_a_near_miss():
    """The near-miss pattern is a superset of the field pattern, so the canonical
    line matches both. Counting it would warn about every gate that works."""
    g = _gated("gate: 3-2")

    assert g.tokens == ("3-2",) and g.near_miss == 0


@pytest.mark.parametrize(
    ("status", "is_open", "is_done"),
    [
        ("open", True, False),
        ("done 2026-08-01", False, True),
        ("opne", False, False),  # a typo is neither, and must not read as landed
        ("", False, False),  # no status line at all
    ],
)
def test_entry_status_is_a_tri_state_not_a_boolean(status, is_open, is_done):
    """`done` is deliberately not `not open`. The readers want opposite answers
    about an unreadable status — `open_ids` drops it, a gate on it has to hold —
    and deriving one from the other let `status: opne` disable a gate silently."""
    line = f"status: {status}\n" if status else ""
    (entry,) = parse_ledger(f"# DW\n\n### DW-1: t\n\norigin: t\nreason: t\n{line}")

    assert entry.open is is_open
    assert entry.done is is_done


def test_gates_stop_at_the_canonical_span_boundary():
    """A `gate:` line below a flat-append bullet belongs to that block, not to the
    entry above it — the same boundary `status:` is read within. Absorbing it would
    let an unrelated appended finding block a story nobody gated."""
    text = (
        "# Deferred Work\n\n### DW-1: canonical\n\n"
        "origin: test\nlocation: n/a\nreason: test\nstatus: open\n\n"
        "- source_spec: `s.md`\n  summary: finding\ngate: 3-2\n"
    )

    (entry,) = parse_ledger(text)

    assert deferredwork.gates(entry).tokens == ()


# ------------------------------------------- ATX heading boundary shapes (#516)


@pytest.mark.parametrize(
    ("heading", "bounds"),
    [
        ("## Notes", True),  # control: the column-zero shape that always bounded
        (" ## Notes", True),  # CommonMark allows one...
        ("  ## Notes", True),  # ...two...
        ("   ## Notes", True),  # ...or three spaces of indent
        ("    ## Notes", False),  # a fourth space is an indented code block
        ("\t## Notes", False),  # a leading tab is four columns, so also code
        ("##\tNotes", True),  # a tab is a legal separator
        ("##", True),  # an empty heading is legal; end of line separates it
        ("## ", True),  # control: a separator and no title, already bounded
        ("## Notes ##", True),  # control: a closed ATX heading, already bounded
        ("##Notes", False),  # no separator at all, so not a heading
        ("####### Notes", False),  # more than six hashes is not a heading
    ],
)
def test_an_entry_ends_at_every_commonmark_atx_heading_shape(heading, bounds):
    """`ANY_HEADING_RE` is where a canonical entry stops, so a shape it fails to
    recognize is one whose section then reads as part of the entry above it: the
    `gate: 3-2` below lands in an open DW-1 that never named a story, and dispatch
    hard-pauses on an entry the operator will find no gate in (#516).

    The four ABSORBS rows are the load-bearing half of this table. Each names a
    line CommonMark says is NOT a heading, and together they are what stops a
    later simplification of the regex to `^ *#+` — which would bound entries at
    section headers that do not exist. They are controls, not oversights; a
    session tidying this table must keep them."""
    text = (
        "### DW-1: an unlanded entry\nstatus: open\nsummary: s\nevidence: e\n\n"
        f"{heading}\n\ngate: 3-2\n"
    )

    (entry,) = parse_ledger(text)

    assert deferredwork.gates(entry).tokens == (() if bounds else ("3-2",))


@pytest.mark.parametrize("heading", [" ## Notes", "   ## Notes", "##\tNotes", "##"])
def test_a_fenced_heading_of_a_new_shape_does_not_bound_the_entry_either(heading):
    """The shapes #516 added inherit the fence rule
    `test_a_fenced_heading_does_not_bound_the_entry_that_quotes_it` pins for the
    column-zero one: a heading quoted inside an example is documentation, not a
    boundary, so the live `gate:` below the fence still reaches the entry.

    Asserted against the unfenced control in the same row, because the fenced
    half alone would pass for the wrong reason — before #516 these shapes bounded
    nothing anywhere, so a green fenced assertion proved only that the regex had
    never seen them. The pair is what makes the fence the difference."""
    quoted = (
        "# Deferred Work\n\n### DW-01: a real entry\nstatus: open\n\n"
        f"```markdown\n{heading}\nstatus: done 2026-01-01\n```\n\ngate: 3-2\n"
    )
    live = (
        "# Deferred Work\n\n### DW-01: a real entry\nstatus: open\n\n"
        f"{heading}\nstatus: done 2026-01-01\n\ngate: 3-2\n"
    )

    (fenced_entry,) = parse_ledger(quoted)
    (live_entry,) = parse_ledger(live)

    assert deferredwork.gates(fenced_entry).tokens == ("3-2",)
    assert deferredwork.gates(live_entry).tokens == ()


def test_a_status_under_an_indented_heading_is_the_section_s_and_not_the_entry_s():
    """A visible behavior change, pinned so it stands as a decision on the record
    rather than turning up as a surprise. Widening `ANY_HEADING_RE` (#516) ends
    DW-1's span at the indented heading, which is above the `status:` line — and
    read against the documented format that is the right answer, because a
    `status:` below a heading belongs to that section and not to the entry before
    it. The entry parses with status "" and leaves `open_ids` entirely; it used to
    read as open."""
    text = "### DW-1: e\n\n  ## Notes\n\nstatus: open\n"

    (entry,) = parse_ledger(text)

    assert entry.status == ""
    assert entry.open is False
    assert open_ids(text) == set()


def test_a_tail_below_an_indented_heading_reaches_the_legacy_parser():
    """The second visible change, and the reason the two parsers cannot be moved
    independently: `parse_legacy` blanks out every span `parse_ledger` returns
    before it scans, so one boundary serves both and moving it moves text between
    them. Before #516 DW-1's span ran past the indented heading and masked the
    bullet below, hiding a real legacy finding from every reader of the ledger.
    With the boundary where CommonMark puts it, the tail is legacy's again."""
    text = "### DW-1: e\nstatus: open\n\n  ## Old stuff\n\n- D-1: a legacy finding\n"

    (legacy,) = parse_legacy(text)

    assert legacy.title == "a legacy finding"


@pytest.mark.parametrize(
    ("token", "story_key", "gated"),
    [
        ("3-2", "3-2", True),  # stories-mode id: the token IS the key
        ("3-2", "3-2-invite-link-student-surface", True),  # sprint key: `-` prefix
        ("3-2", "3-20-later-story", False),  # the boundary the `-` buys
        # A split story is still the gated story. STORY_RE lets breakdown turn an
        # oversized 3-2 into 3-2a/3-2b, and a token that only knew `-` would lose
        # its gate at exactly that moment — silently, which is the one thing a
        # gate must never do.
        ("3-2", "3-2a-split-half", True),
        ("3-2", "3-2b-other-half", True),
        ("3-2", "3-2A-upper", False),  # the split suffix is lowercase ASCII
        ("3-2", "3-2ab-two-letters", False),  # exactly one letter, or it is a slug
        ("3-2", "3-2a", False),  # the `-` after the letter is required
        ("3-2", "9-9a-elsewhere", False),  # the split arm still needs the prefix
        ("3", "3-2-invite-link", True),  # a whole epic is a legal token
        ("3-2-invite", "3-2-invite-link", True),
        ("3-2-invite", "3-2-invited", False),
        # The split arm needs the token to end at a story NUMBER, because that is
        # the only place STORY_RE can attach a split letter. `stories.ID_RE` admits
        # word ids, so without the digit guard the arm read the `z` of `authz` as a
        # split and FAILED validate for a story nobody gated — the one way this
        # check can be worse than the prose it replaced.
        ("auth", "authz-login", False),
        ("api", "apis-v2", False),
        # ...and "ends in a digit" was that same guard written too loosely: the
        # digit can belong to a slug, so the arm read a slug boundary as a split
        # and refused keys the entry never named.
        ("3-2-v2", "3-2-v2a-followup", False),  # `2` closes the slug `v2`, not a story
        ("3", "3a-task", False),  # a distinct stories id, not a split of `3`
        ("3-2-v2", "3-2-v2-followup", True),  # the plain `-` arm is untouched by that
        ("3-2a", "3-2ab-x", False),  # a token already carrying a split letter
        ("3-2a", "3-2a-x", True),  # ...still gates its own `-` boundary
        ("", "a-b", False),  # an empty token names nothing, so it gates nothing
    ],
)
def test_gates_story_matches_on_key_boundaries(token, story_key, gated):
    assert deferredwork.gates_story(token, story_key) is gated


@pytest.mark.parametrize(
    ("body", "declared"),
    [
        ("HARD GATE: must land before 3-2", True),  # the bare convention
        ("reason: wired late. HARD GATE: must land before 3-2", True),  # hard-wrapped prose
        ('reason: an entry naming a "HARD GATE: before X" is enforced by nothing', False),
        ("reason: an entry naming a 'HARD GATE: before X'", False),
        ("reason: «HARD GATE: before X» is only prose", False),
        ("reason: this HARD GATE is textual only, nothing enforces it", False),
        # A ledger is markdown, so the backtick is the citation form an author
        # reaches for first, and an LLM-written entry curls its quotes. Both used
        # to warn, so an entry documenting the convention accused itself.
        ("reason: a `HARD GATE:` is prose only", False),
        ("reason: cites “HARD GATE: before X” only", False),
        ("reason: cites ‘HARD GATE: before X’ only", False),
        # KNOWN LIMIT, pinned rather than left to surprise someone: the lookbehind
        # is one character wide, so a citation that spaces its opening quote off
        # the phrase — the French convention, `«` + U+00A0 — still reads as a
        # declaration. The remedy for such an entry is a `gate:` line, which
        # silences the warning either way.
        ("reason: «\u00a0HARD GATE: before X\u00a0» is only prose", True),
    ],
)
def test_hard_gate_prose_detects_a_declaration_not_a_citation(body, declared):
    """Matched anywhere on a line, because real ledgers hard-wrap `reason:` and the
    declaration lands mid-line. The quote lookbehind is what keeps that honest — an
    entry *citing* the phrase is discussion — and the colon excludes prose that
    merely talks about a hard gate."""
    assert bool(deferredwork.HARD_GATE_PROSE_RE.search(body)) is declared


def _prose_gated(*lines: str) -> bool:
    text = (
        "# Deferred Work\n\n### DW-1: gated entry\n\n"
        "origin: test\nlocation: n/a\nreason: test\nstatus: open\n"
        + "".join(f"{x}\n" for x in lines)
    )
    (entry,) = parse_ledger(text)
    return deferredwork.declares_prose_gate(entry)


@pytest.mark.parametrize("fence", ["```markdown", "```", "~~~"])
def test_a_fenced_prose_gate_is_not_a_declaration(fence):
    """The block form of the citation the quote lookbehind already handles inline.
    Nothing precedes a line inside a fence, so an entry documenting the old
    convention in an example was told to convert a gate it was not declaring —
    the same rule `gates()` applies to `gate:`, left half-applied."""
    close = "```" if fence.startswith("`") else "~~~"
    body = f"{fence}\nHARD GATE: must land before 3-2\n{close}\n"

    # the pattern itself still matches: the mask is what answers, not a lucky miss
    assert deferredwork.HARD_GATE_PROSE_RE.search(body)
    assert _prose_gated(fence, "HARD GATE: must land before 3-2", close) is False


def test_a_fence_hides_only_the_prose_gate_it_quotes():
    """Masking must not reach past the block, or the fix for a spurious warning
    would buy a missed one — an entry that both explains the convention and uses
    it is exactly the entry this warning is for."""
    assert _prose_gated("```", "HARD GATE: an example", "```", "HARD GATE: for real") is True


def test_an_unclosed_fence_swallows_no_prose_gate():
    """Parity with `gates()`: `unclosed_hides_rest=False`, so one stray ``` cannot
    silence every declaration below it."""
    assert _prose_gated("```", "an example nobody closed", "HARD GATE: for real") is True


# ------------------------------------------------------- archive_closed (#706)
#
# Closed entries are moved verbatim to a sibling archive file; a minimal
# stub (heading + status + archived line) replaces each in the live ledger
# so parse_ledger reads it as done, open_ids drops it, and a subsequent run
# skips it by the `archived:` line rather than re-archiving the stub.


def test_archive_all_done(tmp_path):
    """Done entries are moved verbatim to the archive file and replaced with
    minimal stubs that parse as done; open entries are untouched."""
    path = write_ledger(tmp_path)
    archived = archive_closed(path, archive_date="2026-08-24")
    assert archived == ["DW-2"]

    text = path.read_text(encoding="utf-8")
    entries = {e.id: e for e in parse_ledger(text)}
    # The stub still parses as done, with the original close date
    assert entries["DW-2"].done
    assert entries["DW-2"].status == "done 2026-05-25"
    assert "reason: pre-existing." not in entries["DW-2"].body  # body was moved
    assert "location: src/foo.py:10" not in entries["DW-2"].body
    # Load-bearing field lines survive in the stub (engine replay dedupe)
    assert "origin: code review of spec-1-1.md" in entries["DW-2"].body
    assert entries["DW-2"].title == "Old closed item"  # heading preserved
    assert "archived: 2026-08-24" in entries["DW-2"].body  # stub marker
    # Open entries untouched
    assert entries["DW-1"].open
    assert "origin: quick-dev" in entries["DW-1"].body
    assert entries["DW-3"].open
    assert "seen-again" in entries["DW-3"].body

    # The archive file has the full body with an `archived:` line
    archive_path = path.parent / ARCHIVE_REL
    assert archive_path.is_file()
    archive_text = archive_path.read_text(encoding="utf-8")
    assert "### DW-2: Old closed item" in archive_text
    assert "origin: code review of spec-1-1.md" in archive_text
    assert "status: done 2026-05-25" in archive_text
    assert "archived: 2026-08-24" in archive_text


def test_archive_before_cutoff(tmp_path):
    """--before archives only entries closed strictly before that date."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: May item\n\norigin: a\nstatus: done 2026-05-15\n\n"
        "### DW-2: july item\n\norigin: b\nstatus: done 2026-07-01\n\n"
        "### DW-3: still open\n\norigin: c\nstatus: open\n"
    )
    path = write_ledger(tmp_path, text)
    archived = archive_closed(path, before="2026-06-01", archive_date="2026-08-24")
    assert archived == ["DW-1"]

    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert entries["DW-1"].done  # stub
    assert "archived: 2026-08-24" in entries["DW-1"].body
    assert "origin: a" in entries["DW-1"].body  # preserved field line
    # July entry untouched — not before the cutoff
    assert entries["DW-2"].status == "done 2026-07-01"
    assert "origin: b" in entries["DW-2"].body
    assert entries["DW-3"].open


def test_archive_dry_run(tmp_path):
    """Dry run returns the ids that would be archived but writes nothing."""
    path = write_ledger(tmp_path)
    before = path.read_text(encoding="utf-8")
    archived = archive_closed(path, dry_run=True)
    assert archived == ["DW-2"]
    assert path.read_text(encoding="utf-8") == before
    assert not (path.parent / ARCHIVE_REL).exists()


def test_archive_no_done_entries(tmp_path):
    """A ledger with only open entries returns an empty list and writes nothing."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: open one\n\norigin: a\nstatus: open\n\n"
        "### DW-2: open two\n\norigin: b\nstatus: open\n"
    )
    path = write_ledger(tmp_path, text)
    before = path.read_text(encoding="utf-8")
    assert archive_closed(path) == []
    assert path.read_text(encoding="utf-8") == before
    assert not (path.parent / ARCHIVE_REL).exists()


def test_archive_no_ledger(tmp_path):
    """A missing ledger file returns an empty list."""
    path = tmp_path / "deferred-work.md"
    assert archive_closed(path) == []
    assert not (path.parent / ARCHIVE_REL).exists()


def test_archive_done_without_date_skipped(tmp_path):
    """An entry with `status: done` (no date) is skipped — there is no close
    date to compare against a cutoff or to stamp the stub with."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: closed without date\n\norigin: a\nstatus: done\n\n"
        "### DW-2: closed with date\n\norigin: b\nstatus: done 2026-05-25\n"
    )
    path = write_ledger(tmp_path, text)
    archived = archive_closed(path, archive_date="2026-08-24")
    assert archived == ["DW-2"]

    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert entries["DW-1"].status == "done"  # untouched
    assert "origin: a" in entries["DW-1"].body  # body still there
    assert entries["DW-2"].done  # stub


def test_archive_rerun_appends_to_existing(tmp_path):
    """A second run appends new entries to the existing archive without
    overwriting, and skips stubs left by the first run."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: first done\n\norigin: a\nstatus: done 2026-05-15\n\n"
        "### DW-2: second done\n\norigin: b\nstatus: done 2026-06-01\n\n"
        "### DW-3: open\n\norigin: c\nstatus: open\n"
    )
    path = write_ledger(tmp_path, text)
    # First run: archive only the May entry
    archive_closed(path, before="2026-06-01", archive_date="2026-08-24")
    archive_path = path.parent / ARCHIVE_REL
    first = archive_path.read_text(encoding="utf-8")
    assert "DW-1" in first
    assert "DW-2" not in first

    # Close DW-3 and archive the rest — DW-1's stub is skipped
    mark_done(path, "DW-3", "2026-07-01", "resolved")
    archived = archive_closed(path, archive_date="2026-08-25")
    assert set(archived) == {"DW-2", "DW-3"}

    second = archive_path.read_text(encoding="utf-8")
    # Both old and new entries are in the archive
    assert "DW-1" in second and "DW-2" in second and "DW-3" in second
    # The first run's stamp is preserved (not overwritten)
    assert "archived: 2026-08-24" in second
    assert "archived: 2026-08-25" in second


def test_archive_stub_preserves_id_and_parses_as_done(tmp_path):
    """The stub's heading and status line let parse_ledger read it as done
    and open_ids exclude it, while the DW- id stays findable for grep."""
    path = write_ledger(tmp_path)
    archive_closed(path, archive_date="2026-08-24")

    text = path.read_text(encoding="utf-8")
    entries = {e.id: e for e in parse_ledger(text)}
    assert entries["DW-2"].id == "DW-2"
    assert entries["DW-2"].done
    assert not entries["DW-2"].open
    assert "DW-2" not in open_ids(text)


def test_archive_open_entries_untouched(tmp_path):
    """Open entries are never modified — their bodies stay byte-identical."""
    path = write_ledger(tmp_path)
    before_open = {e.id: e.body for e in parse_ledger(path.read_text(encoding="utf-8")) if e.open}
    archive_closed(path, archive_date="2026-08-24")
    after = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    for dw_id, body in before_open.items():
        assert after[dw_id].body == body


def test_archive_validates_before_date(tmp_path):
    """An invalid --before date raises ValueError without writing anything."""
    path = write_ledger(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        archive_closed(path, before="not-a-date")
    assert path.read_text(encoding="utf-8") == before
    assert not (path.parent / ARCHIVE_REL).exists()


def test_archive_validates_archive_date(tmp_path):
    """An invalid archive_date raises ValueError without writing anything."""
    path = write_ledger(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        archive_closed(path, archive_date="2026-13-01")
    assert path.read_text(encoding="utf-8") == before


def test_archive_bad_date_raises_even_with_no_ledger(tmp_path):
    """Validated at function entry, ahead of the is_file short-circuit."""
    path = tmp_path / "nope.md"
    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        archive_closed(path, before="nope")
    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        archive_closed(path, archive_date="nope")


def test_archive_skips_already_archived_stubs(tmp_path):
    """A second run with no new closures finds only stubs (which carry an
    `archived:` line) and archives nothing."""
    path = write_ledger(tmp_path)
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-2"]
    assert archive_closed(path, archive_date="2026-08-25") == []
    archive_path = path.parent / ARCHIVE_REL
    assert "archived: 2026-08-25" not in archive_path.read_text(encoding="utf-8")


def test_archive_default_archive_date_is_today(tmp_path):
    """When archive_date is not supplied, the stub and archive carry today's date."""
    from datetime import date as calendar_date

    path = write_ledger(tmp_path)
    archive_closed(path)
    today = calendar_date.today().isoformat()

    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert f"archived: {today}" in entries["DW-2"].body
    archive_path = path.parent / ARCHIVE_REL
    assert f"archived: {today}" in archive_path.read_text(encoding="utf-8")


def test_archive_multi_entry_reparse(tmp_path):
    """Archiving 2+ done entries in one call leaves stubs that re-parse with
    correct id, title, status, and archived line; open entries are byte-identical."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: first done\n\norigin: a\nstatus: done 2026-05-15\n\n"
        "### DW-2: open one\n\norigin: b\nstatus: open\n\n"
        "### DW-3: second done\n\norigin: c\nstatus: done 2026-06-01\n\n"
        "### DW-4: open two\n\norigin: d\nstatus: open\n"
    )
    path = write_ledger(tmp_path, text)
    before_open = {e.id: e.body for e in parse_ledger(path.read_text(encoding="utf-8")) if e.open}
    archived = archive_closed(path, archive_date="2026-08-24")
    assert set(archived) == {"DW-1", "DW-3"}

    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    for dw_id, title, close_date in [
        ("DW-1", "first done", "2026-05-15"),
        ("DW-3", "second done", "2026-06-01"),
    ]:
        assert entries[dw_id].id == dw_id
        assert entries[dw_id].title == title
        assert entries[dw_id].done
        assert entries[dw_id].status == f"done {close_date}"
        assert "archived: 2026-08-24" in entries[dw_id].body
    for dw_id, body in before_open.items():
        assert entries[dw_id].body == body


def test_archive_before_boundary_excludes_cutoff_date(tmp_path):
    """An entry closed exactly on the cutoff date is NOT archived (strict <)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: on the boundary\n\norigin: a\nstatus: done 2026-06-01\n\n"
        "### DW-2: before the boundary\n\norigin: b\nstatus: done 2026-05-31\n"
    )
    path = write_ledger(tmp_path, text)
    archived = archive_closed(path, before="2026-06-01", archive_date="2026-08-24")
    assert archived == ["DW-2"]  # DW-1 is ON the cutoff, excluded by strict <


def test_archive_rejects_status_with_extra_tokens(tmp_path):
    """A status like `done 2026-05-25 junk` is not a close date — exactly two
    tokens are required, so the entry is skipped rather than archived on a
    garbage date (#706 review)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: extra tokens\n\norigin: a\nstatus: done 2026-05-25 junk\n\n"
        "### DW-2: clean close\n\norigin: b\nstatus: done 2026-05-25\n"
    )
    path = write_ledger(tmp_path, text)
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-2"]
    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert "origin: a" in entries["DW-1"].body  # untouched


def test_archive_rejects_impossible_calendar_date(tmp_path):
    """A well-shaped impossible day (2026-02-30) passes the ISO regex but no
    calendar carries it — skipped, not archived (#706 review)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: feb 30\n\norigin: a\nstatus: done 2026-02-30\n\n"
        "### DW-2: real date\n\norigin: b\nstatus: done 2026-05-25\n"
    )
    path = write_ledger(tmp_path, text)
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-2"]
    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert "origin: a" in entries["DW-1"].body  # untouched


def test_archive_crash_recovery_no_duplicate_bodies(tmp_path):
    """Crash between the archive write and the ledger write leaves full entries
    in the ledger with bodies already archived. A retry must stub the ledger
    entries (completing the operation) without appending duplicate bodies
    (#706 review)."""
    path = write_ledger(tmp_path)
    # Simulate the crashed first run: the body landed in the archive, but the
    # ledger was never trimmed (still holds the full DW-2 entry).
    archive_path = path.parent / ARCHIVE_REL
    archive_path.write_text(
        "### DW-2: Old closed item\n\n"
        "origin: code review of spec-1-1.md, 2026-05-20\n"
        "location: src/foo.py:10\n"
        "reason: pre-existing.\n"
        "status: done 2026-05-25\n"
        "archived: 2026-08-24\n",
        encoding="utf-8",
    )
    archived = archive_closed(path, archive_date="2026-08-25")
    assert archived == ["DW-2"]  # the operation completes: stub written

    # The ledger now holds the stub...
    entries = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}
    assert entries["DW-2"].done
    assert "reason: pre-existing." not in entries["DW-2"].body  # body was moved, stub left

    # ...and the archive carries the body exactly once, with the FIRST run's
    # stamp preserved — no duplicate append, no re-stamp.
    archive_text = archive_path.read_text(encoding="utf-8")
    assert archive_text.count("### DW-2:") == 1
    assert "archived: 2026-08-24" in archive_text
    assert "archived: 2026-08-25" not in archive_text


def test_archive_crash_recovery_stub_keeps_the_archived_body_stamp(tmp_path):
    """A stub recovered from a crashed run is stamped with the date already on
    its archived body, not with the retry's date. The stamp is what picks one
    of an id's several archive blocks — including once `mark_open` demotes it
    into the `archived-body:` pointer — so a stub naming a date no block
    carries resolves to nothing (#711 review)."""
    path = write_ledger(tmp_path)
    archive_path = path.parent / ARCHIVE_REL
    archive_path.write_text(
        "### DW-2: Old closed item\n\n"
        "origin: code review of spec-1-1.md, 2026-05-20\n"
        "location: src/foo.py:10\n"
        "reason: pre-existing.\n"
        "status: done 2026-05-25\n"
        "archived: 2026-08-24\n",
        encoding="utf-8",
    )
    assert archive_closed(path, archive_date="2026-08-25") == ["DW-2"]

    stub = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-2"]
    assert "archived: 2026-08-24" in stub.body  # the body's own stamp
    assert "archived: 2026-08-25" not in stub.body  # not the retry's

    # Resolve the stub the way a reader must: its stamp names exactly one block.
    stamp = deferredwork._archived_stamp(stub)
    archive_text = archive_path.read_text(encoding="utf-8")
    blocks = [
        e for e in parse_ledger(archive_text) if e.id == "DW-2" and f"archived: {stamp}" in e.body
    ]
    assert len(blocks) == 1
    assert "reason: pre-existing." in blocks[0].body


def test_archive_fresh_stub_keeps_this_runs_stamp(tmp_path):
    """The recovered-stamp carry-over is scoped to entries the crash-recovery
    skip fired for: an entry archived normally is stamped with this run's date
    on both sides (#711 review)."""
    path = write_ledger(tmp_path)
    assert archive_closed(path, archive_date="2026-08-25") == ["DW-2"]
    stub = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-2"]
    assert "archived: 2026-08-25" in stub.body
    assert "archived: 2026-08-25" in (path.parent / ARCHIVE_REL).read_text(encoding="utf-8")


def test_archive_crash_recovery_fenced_example_does_not_suppress(tmp_path):
    """A fenced worked example in the archive quoting `### DW-2:` is not a
    real archived body — the crash-recovery skip must be fence-aware, so the
    live DW-2 is still archived rather than silently kept (#706 review)."""
    path = write_ledger(tmp_path)
    # An archive whose only DW-2 mention is inside a fenced example entry:
    # the example carries no live `archived:` field, so it must not count.
    archive_path = path.parent / ARCHIVE_REL
    archive_path.write_text(
        "# Archived Deferred Work\n\n"
        "```markdown\n"
        "### DW-2: Old closed item\n\n"
        "origin: quoted example, not a real body\n"
        "status: done 2026-05-25\n"
        "```\n",
        encoding="utf-8",
    )
    archived = archive_closed(path, archive_date="2026-08-24")
    assert archived == ["DW-2"]  # not suppressed by the fenced heading

    # The real body was appended; the fenced example survives verbatim above it
    archive_text = archive_path.read_text(encoding="utf-8")
    assert archive_text.count("### DW-2:") == 2  # quoted + real
    assert "origin: code review of spec-1-1.md" in archive_text
    assert "quoted example" in archive_text


# ------------------------------------------- archive follow-up review (#706, pass 2)


def test_archive_stub_preserves_gate_origin_source_spec(tmp_path):
    """The stub keeps gate:/origin:/source_spec: lines — validate's closed-gate
    report and the engine's status-agnostic replay dedupe both key on them
    regardless of status."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: gated close\n\n"
        "origin: spec-harvest fingerprint-abc\n"
        "source_spec: specs/spec-1-1.md\n"
        "gate: 1-2\n"
        "location: src/foo.py\n"
        "status: done 2026-05-25\n"
    )
    path = write_ledger(tmp_path, text)
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    stub = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-1"]
    assert "gate: 1-2" in stub.body
    assert "origin: spec-harvest fingerprint-abc" in stub.body
    assert "source_spec: specs/spec-1-1.md" in stub.body
    assert "location: src/foo.py" not in stub.body  # the rest moved
    assert gates(stub).tokens == ("1-2",)  # still speaks for validate


def test_archive_stub_preserves_reopenable_undo_tail(tmp_path):
    """A reopenable close's resolution/resolution-undo tail survives into the
    stub, so a later sweep-bundle rollback can still undo the close."""
    text = "# Deferred Work\n\n### DW-1: bundle close\n\norigin: a\nlocation: b\nstatus: open\n"
    path = write_ledger(tmp_path, text)
    mark_done_many_reopenable(path, ["DW-1"], "2026-05-25", "sweep bundle", "op-1")
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    # The stub still reads done...
    stub_ledger = path.read_text(encoding="utf-8")
    assert "DW-1" not in open_ids(stub_ledger)
    # ...and mark_open can still undo it (the tail is intact and adjacent)
    assert mark_open(path, "DW-1", "sweep bundle", "op-1") is True
    assert "DW-1" in open_ids(path.read_text(encoding="utf-8"))


def test_archive_hand_written_archived_line_still_archives(tmp_path):
    """A done entry carrying a stray unfenced `archived:` line but a real body
    is NOT mistaken for a stub — shape, not one line, decides (#706 pass 2)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: real entry, stray field\n\n"
        "origin: a\nlocation: src/x.py\nreason: still real work\n"
        "status: done 2026-05-25\narchived: 2026-01-01\n"
    )
    path = write_ledger(tmp_path, text)
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    assert "reason: still real work" in (path.parent / ARCHIVE_REL).read_text(encoding="utf-8")


def test_archive_is_archived_fence_filter_pinned(tmp_path):
    """A done entry documenting `archived:` only inside a fenced example is
    not treated as already-archived — the `_quoted` filter is load-bearing."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: documents the field\n\n"
        "origin: a\nlocation: b\n"
        "```\n"
        "archived: 2026-01-01\n"
        "```\n"
        "status: done 2026-05-25\n"
    )
    path = write_ledger(tmp_path, text)
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    stub = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-1"]
    assert "archived: 2026-08-24" in stub.body  # real stamp, not the quoted one


def test_archive_legacy_entries_untouched(tmp_path):
    """Legacy (flat/pre-DW-format) content is never modified by archiving."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: done canonical\n\norigin: a\nstatus: done 2026-05-25\n\n"
        "- source_spec: specs/spec-2-1.md — legacy flat finding, RESOLVED 2026-04-01\n\n"
        "## Deferred from: review of spec-2-1.md\n\n"
        "Some legacy freeform prose that predates the DW format.\n"
    )
    path = write_ledger(tmp_path, text)
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    after = path.read_text(encoding="utf-8")
    assert "- source_spec: specs/spec-2-1.md — legacy flat finding, RESOLVED 2026-04-01" in after
    assert "## Deferred from: review of spec-2-1.md" in after
    assert "legacy freeform prose" in after


def test_archive_stub_is_grep_resolvable_and_classifies_clean(tmp_path):
    """The stub keeps the heading so `grep DW-2` finds it, and a post-archive
    ledger classifies with no malformed entries."""
    path = write_ledger(tmp_path)
    archive_closed(path, archive_date="2026-08-24")
    text = path.read_text(encoding="utf-8")
    assert "DW-2" in text  # grep-resolvable
    declared = classify(text, ids=["DW-2"])
    assert declared.malformed == ()  # stub reads done, not malformed
    assert "DW-2" in declared.already_done


def test_archive_reclose_after_archive_appends_new_body(tmp_path):
    """An entry reopened and re-closed after its first body was archived gets
    its second body appended — id equivalence alone must not suppress it."""
    text = "# Deferred Work\n\n### DW-1: closes twice\n\norigin: a\nstatus: done 2026-05-25\n"
    path = write_ledger(tmp_path, text)
    archive_closed(path, archive_date="2026-06-01")
    # Reopen (undo the stub) and re-close with a different date and body
    reopened = text.replace("status: done 2026-05-25", "status: open\nreason: reopened")
    reopened = reopened.replace("archived: 2026-06-01\n", "")
    path.write_text(reopened, encoding="utf-8")
    mark_done(path, "DW-1", "2026-07-01", "second close")
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    archive_text = (path.parent / ARCHIVE_REL).read_text(encoding="utf-8")
    assert archive_text.count("### DW-1:") == 2  # both closures preserved
    assert "status: done 2026-07-01" in archive_text


def test_archive_crash_recovery_edited_entry_keeps_both_bodies(tmp_path):
    """Crash-recovery skip keys on id AND close date: an entry edited between
    the crash and the retry is re-archived, not silently dropped."""
    path = write_ledger(tmp_path)
    # First (crashed) run archived the original body
    archive_path = path.parent / ARCHIVE_REL
    archive_path.write_text(
        "### DW-2: Old closed item\n\n"
        "origin: code review of spec-1-1.md, 2026-05-20\n"
        "location: src/foo.py:10\n"
        "reason: pre-existing.\n"
        "status: done 2026-05-25\n"
        "archived: 2026-08-24\n",
        encoding="utf-8",
    )
    # The ledger entry was then re-closed later (different close date)
    edited = LEDGER.replace("status: done 2026-05-25", "status: done 2026-06-10")
    path.write_text(edited, encoding="utf-8")
    assert archive_closed(path, archive_date="2026-08-25") == ["DW-2"]
    archive_text = archive_path.read_text(encoding="utf-8")
    assert archive_text.count("### DW-2:") == 2
    assert "status: done 2026-06-10" in archive_text


def test_archive_default_date_flake_fixed(tmp_path, monkeypatch):
    """`calendar_date` is patched to a fixed clock (both `today()` and
    `fromisoformat()`), so a midnight rollover mid-call cannot fail the
    assertion (docs/testing.md flake policy)."""
    from datetime import date as real_date

    class FixedDate(real_date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 24)

    monkeypatch.setattr(deferredwork, "calendar_date", FixedDate)
    path = write_ledger(tmp_path)
    archive_closed(path)
    assert f"archived: {FixedDate.today().isoformat()}" in (path.parent / ARCHIVE_REL).read_text(
        encoding="utf-8"
    )


def test_archive_fenced_heading_in_archive_does_not_suppress_reclose(tmp_path):
    """A `### DW-2:` mention inside an archived body's fenced example must not
    read as that id's archived twin — the false-positive direction of the
    crash-recovery membership check."""
    path = write_ledger(tmp_path)
    # An archive holding a fenced worked example that quotes DW-2's heading
    archive_path = path.parent / ARCHIVE_REL
    archive_path.write_text(
        "### DW-9: documents the format\n\n"
        "```\n"
        "### DW-2: quoted example\n"
        "status: done 2026-05-25\n"
        "archived: 2026-01-01\n"
        "```\n"
        "status: done 2026-01-02\n"
        "archived: 2026-01-03\n",
        encoding="utf-8",
    )
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-2"]
    archive_text = archive_path.read_text(encoding="utf-8")
    assert "origin: code review of spec-1-1.md" in archive_text  # real body landed


# --------------------------------------------- archive reopen cycle (#711 review)
#
# A DW id outlives any one closure: `mark_open` reopens, a re-close follows, and
# `append_decision` writes to a closed entry without reading its status. The
# crash-recovery skip therefore cannot key on id + close date alone, the stub
# shape must survive the spacing `_MARK_DONE_TAIL_RE` tolerates, and a reopen
# must demote the `archived:` stamp that no longer describes the entry without
# severing the reopened entry from the body that stamp was pointing at.


def test_archive_same_date_reclose_preserves_new_body(tmp_path):
    """Reopened and re-closed on the SAME date with a new resolution, the entry
    is archived again rather than stubbed over its own content — id + close date
    names a closure slot, not the body that filled it (#711 review)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: closes twice on one day\n\n"
        "origin: a\nlocation: src/x.py:1\nreason: first pass\nstatus: open\n"
    )
    path = write_ledger(tmp_path, text)
    close_reopenable(path, "DW-1", "first close")
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    assert mark_open(path, "DW-1", "first close", OPERATION_ID) is True
    assert mark_done(path, "DW-1", "2026-06-11", "second close, a different note") is True

    assert archive_closed(path, archive_date="2026-08-25") == ["DW-1"]
    archive_text = (path.parent / ARCHIVE_REL).read_text(encoding="utf-8")
    # The returned id is truthful: the second body really reached the archive.
    assert "second close, a different note" in archive_text
    assert archive_text.count("### DW-1:") == 2
    # ...and it is not hiding in the ledger either — the stub carries no note.
    stub = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-1"]
    assert "second close, a different note" not in stub.body


def test_archive_decision_on_stub_preserved(tmp_path):
    """`append_decision` writes to a closed entry without reading its status, so
    a decision can land on a stub; the next archive run must carry it across
    instead of overwriting the stub with a fresh one (#711 review)."""
    path = write_ledger(tmp_path)  # DW-2 is done 2026-05-25
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-2"]
    assert append_decision(path, "DW-2", "2026-08-25", "keep", "still relevant") is True

    assert archive_closed(path, archive_date="2026-08-26") == ["DW-2"]
    archive_text = (path.parent / ARCHIVE_REL).read_text(encoding="utf-8")
    assert "decision: 2026-08-25 keep — still relevant" in archive_text
    assert archive_text.count("### DW-2:") == 2


def test_mark_open_strips_archived_line(tmp_path):
    """Reopening drops the entry's live `archived:` stamp — the body is back in
    the ledger, so the line is a lie — demoting it to `archived-body:`, while a
    fenced example of the field is left alone (#711 review)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: documents the field it also carries\n\n"
        "origin: a\n"
        "```\n"
        "archived: 2026-01-01\n"
        "```\n"
        "status: open\n"
    )
    path = write_ledger(tmp_path, text)
    close_reopenable(path, "DW-1", "bundle close")
    # Stamp it the way a stub is stamped: after the close's undo tail.
    closed = path.read_text(encoding="utf-8")
    path.write_text(closed.rstrip("\n") + "\narchived: 2026-08-24\n", encoding="utf-8")

    assert mark_open(path, "DW-1", "bundle close", OPERATION_ID) is True
    entry = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-1"]
    assert entry.open
    assert "archived: 2026-08-24" not in entry.body  # the live stamp is gone
    assert "archived-body: 2026-08-24" in entry.body  # demoted, not deleted
    assert "archived: 2026-01-01" in entry.body  # the fenced example is not a stamp
    assert "archived-body: 2026-01-01" not in entry.body  # ...so it was not demoted
    assert "origin: a" in entry.body  # nothing else was cut


def test_mark_open_leaves_a_pointer_to_the_archived_body(tmp_path):
    """A reopened stub stays triage-resolvable. Its `location:`/`reason:` are in
    the archive file — the stub preserves neither — so the demoted
    `archived-body:` line has to name the block holding them. Deleting the
    stamp outright left triage a heading and nothing to triage (#711 review)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: reopened after archiving\n\n"
        "origin: a\n"
        "location: src/x.py:1\n"
        "reason: waiting on the codec seam\n"
        "status: open\n"
    )
    path = write_ledger(tmp_path, text)
    close_reopenable(path, "DW-1", "bundle close")
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    assert mark_open(path, "DW-1", "bundle close", OPERATION_ID) is True

    entry = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-1"]
    assert entry.open
    assert "reason: waiting on the codec seam" not in entry.body  # the body did move out
    pointers = [ln for ln in entry.body.splitlines() if ln.startswith("archived-body:")]
    assert pointers == ["archived-body: 2026-08-24"]  # and this is what says where to

    # Walk the pointer the way a triage session must: its date picks the block,
    # since one id owns several once a divergent re-closure is archived too.
    stamp = pointers[0].split(":", 1)[1].strip()
    archive_text = (path.parent / ARCHIVE_REL).read_text(encoding="utf-8")
    blocks = [
        e for e in parse_ledger(archive_text) if e.id == "DW-1" and f"archived: {stamp}" in e.body
    ]
    assert len(blocks) == 1
    assert "location: src/x.py:1" in blocks[0].body
    assert "reason: waiting on the codec seam" in blocks[0].body


def test_archive_reopened_stub_recloses_and_archives(tmp_path):
    """Full cycle: archive, reopen, re-close reopenably at a later date. Without
    the reopen-side strip the re-close rebuilds the exact stub shape and
    `_is_stub` traps the entry outside every future archive (#711 review)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: full reopen cycle\n\n"
        "origin: a\nlocation: src/x.py:1\nreason: first pass\nstatus: open\n"
    )
    path = write_ledger(tmp_path, text)
    close_reopenable(path, "DW-1", "first close")
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    assert mark_open(path, "DW-1", "first close", OPERATION_ID) is True
    close_reopenable(path, "DW-1", "second close", date="2026-07-01")

    entry = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-1"]
    assert not deferredwork._is_stub(entry)  # the reopen cycle broke the stub shape
    assert archive_closed(path, archive_date="2026-08-25") == ["DW-1"]
    archive_text = (path.parent / ARCHIVE_REL).read_text(encoding="utf-8")
    assert archive_text.count("### DW-1:") == 2  # both closures preserved
    assert "status: done 2026-07-01" in archive_text
    assert "resolution: second close" in archive_text


def test_archive_same_day_reclosures_resolve_by_append_order(tmp_path):
    """Two closures of one id archived on the SAME day share a stamp, so the
    stamp narrows rather than identifies. The tie-break the format documents is
    the archive's append order — later block, later closure — and that is a
    property of how `archive_closed` writes, not a convention a reader can only
    hope for (#711 review)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: closed twice in one day\n\n"
        "origin: a\nlocation: src/x.py:1\nreason: first pass\nstatus: open\n"
    )
    path = write_ledger(tmp_path, text)
    close_reopenable(path, "DW-1", "first close", date="2026-06-11")
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    assert mark_open(path, "DW-1", "first close", OPERATION_ID) is True
    close_reopenable(path, "DW-1", "second close", date="2026-06-12")
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]

    stub = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-1"]
    stamp = deferredwork._archived_stamp(stub)
    archive_text = (path.parent / ARCHIVE_REL).read_text(encoding="utf-8")
    blocks = [
        e for e in parse_ledger(archive_text) if e.id == "DW-1" and f"archived: {stamp}" in e.body
    ]
    # Both closures carry the same stamp — the ambiguity the tie-break exists for
    assert len(blocks) == 2
    # ...and file order is closure order, so the LAST one is what the stub points at
    assert "resolution: first close" in blocks[0].body
    assert "resolution: second close" in blocks[-1].body
    assert "status: done 2026-06-11" in blocks[0].body
    assert "status: done 2026-06-12" in blocks[-1].body


def test_archive_tab_resolution_stub_converges(tmp_path):
    """A tab-separated undo tail is copied into the stub verbatim, so the stub
    shape must tolerate the spacing `_MARK_DONE_TAIL_RE` accepts. Otherwise the
    stub reads as a live entry and every run re-archives it (#711 review)."""
    text = (
        "# Deferred Work\n\n"
        "### DW-1: tab-separated tail\n\n"
        "origin: a\n"
        "location: src/x.py:1\n"
        "reason: prose that does not survive into the stub\n"
        "status: done 2026-05-25\n"
        "resolution:\ttabbed note\n"
        "resolution-undo:\t" + "a" * 64 + "\t2026-05-25\t7374617475733a206f70656e\n"
    )
    path = write_ledger(tmp_path, text)
    assert archive_closed(path, archive_date="2026-08-24") == ["DW-1"]
    # The stub kept the tail verbatim, tabs and all...
    stub = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-1"]
    assert "resolution:\ttabbed note" in stub.body
    # ...and a second run recognizes it as a stub and settles.
    assert archive_closed(path, archive_date="2026-08-25") == []
    archive_text = (path.parent / ARCHIVE_REL).read_text(encoding="utf-8")
    assert archive_text.count("### DW-1:") == 1
    assert "archived: 2026-08-25" not in archive_text


def test_archive_fenced_archived_line_in_twin_does_not_suppress(tmp_path):
    """An archive entry whose ONLY `archived:` line sits inside a fenced example
    is not an archived twin — the crash-recovery skip reads the field through the
    fence filter, so the live entry is archived rather than stubbed over its own
    body (#711 review, finding 5).

    The twin's body is byte-identical to the ledger entry's, which is what makes
    this test decide the fence filter and nothing else. Body equivalence is the
    other half of the skip, and it is satisfied here in BOTH directions: with the
    filter ablated the same fenced line is stripped from both sides, so the
    bodies still compare equal and only `_is_archived` changes its answer.

    Ablation: drop the `_quoted` guard from `_archived_line_spans` and the count
    below reads 1 — the fenced example reads as a real stamp and the body never
    reaches the archive."""
    entry = (
        "### DW-2: documents the archive field\n\n"
        "origin: code review of spec-1-1.md, 2026-05-20\n"
        "location: src/foo.py:10\n"
        "reason: pre-existing.\n"
        "```markdown\n"
        "archived: 2026-01-01\n"
        "```\n"
        "status: done 2026-05-25\n"
    )
    path = write_ledger(tmp_path, "# Deferred Work\n\n" + entry)
    archive_path = path.parent / ARCHIVE_REL
    archive_path.write_text("# Archived Deferred Work\n\n" + entry, encoding="utf-8")

    assert archive_closed(path, archive_date="2026-08-24") == ["DW-2"]
    archive_text = archive_path.read_text(encoding="utf-8")
    assert archive_text.count("### DW-2:") == 2  # the quoted stamp suppressed nothing
    assert archive_text.count("archived: 2026-08-24") == 1  # the real stamp, once
    # ...and the body left the ledger for the archive rather than being dropped.
    stub = {e.id: e for e in parse_ledger(path.read_text(encoding="utf-8"))}["DW-2"]
    assert "reason: pre-existing." not in stub.body
