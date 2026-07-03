"""Ledger parsing and editing: deferredwork.py."""

from pathlib import Path

from bmad_loop.deferredwork import (
    append_decision,
    append_entry,
    field_line_present,
    field_severity,
    has_legacy,
    mark_done,
    next_seq,
    open_ids,
    parse_ledger,
    parse_legacy,
)

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
    assert "source_spec: `spec-foo.md`" in body
    assert "severity: low" in body
    assert "follow-up still recommended for dw-x" in body


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
