"""Deterministic reading and editing of the deferred-work ledger.

The ledger (`{implementation_artifacts}/deferred-work.md`) is append-only
markdown in the canonical form documented at
bmad-loop-sweep/deferred-work-format.md: `### DW-<seq>: <title>` headings with
`origin:`/`location:`/`reason:`/`status:` field lines. The inner bmad-dev-auto
session appends flatter entries that the orchestrator normalizes on sweep. The
orchestrator never trusts an LLM to have edited it — status flips and decision
records happen here, and gates re-read the file from disk.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

HEADING_RE = re.compile(r"^### (DW-\d+): (.+?)\s*$", re.MULTILINE)
ANY_HEADING_RE = re.compile(r"^#{1,6} ", re.MULTILINE)
STATUS_RE = re.compile(r"^status:[ \t]*(.*)$", re.MULTILINE)


@dataclass(frozen=True)
class DWEntry:
    id: str
    title: str
    status: str  # the status field value, "" when the line is missing
    body: str  # full entry text including the heading
    span: tuple[int, int]  # char offsets of the entry in the ledger text

    @property
    def open(self) -> bool:
        return self.status.split()[0] == "open" if self.status else False


def parse_ledger(text: str) -> list[DWEntry]:
    """Extract DW entries; non-conforming sections are skipped, an entry
    without a status line parses with status "" (not open)."""
    entries = []
    headings = list(HEADING_RE.finditer(text))
    for i, m in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        # an entry also ends at any intervening heading (e.g. a "## Deferred
        # from:" section header between freeform and DW-format content)
        other = ANY_HEADING_RE.search(text, m.end(), end)
        if other:
            end = other.start()
        body = text[m.start() : end]
        status_m = STATUS_RE.search(body)
        entries.append(
            DWEntry(
                id=m.group(1),
                title=m.group(2),
                status=status_m.group(1).strip() if status_m else "",
                body=body,
                span=(m.start(), end),
            )
        )
    return entries


def open_ids(text: str) -> set[str]:
    return {e.id for e in parse_ledger(text) if e.open}


def _find_entry(text: str, dw_id: str) -> DWEntry | None:
    for entry in parse_ledger(text):
        if entry.id == dw_id:
            return entry
    return None


def _insert_after_status(text: str, entry: DWEntry, line: str) -> str:
    """Insert a field line right after the entry's status line (or at the end
    of the entry when no status line exists)."""
    status_m = STATUS_RE.search(entry.body)
    if status_m:
        pos = entry.span[0] + status_m.end()
        return text[:pos] + "\n" + line + text[pos:]
    insert_at = entry.span[0] + len(entry.body.rstrip())
    return text[:insert_at] + "\n" + line + text[insert_at:]


def mark_done(path: Path, dw_id: str, date: str, note: str) -> bool:
    """Flip one entry to `status: done <date>` and record a resolution note.
    Returns False (no write) when the entry is missing or already done."""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    entry = _find_entry(text, dw_id)
    if entry is None or not entry.open:
        return False
    status_m = STATUS_RE.search(entry.body)
    assert status_m is not None  # open implies a status line
    start = entry.span[0] + status_m.start()
    end = entry.span[0] + status_m.end()
    text = text[:start] + f"status: done {date}" + text[end:]
    entry = _find_entry(text, dw_id)
    assert entry is not None
    text = _insert_after_status(text, entry, f"resolution: {note}")
    path.write_text(text, encoding="utf-8")
    return True


def append_decision(path: Path, dw_id: str, date: str, label: str, detail: str) -> bool:
    """Record a human decision on an entry without changing its status."""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    entry = _find_entry(text, dw_id)
    if entry is None:
        return False
    detail_part = f" — {detail}" if detail else ""
    text = _insert_after_status(text, entry, f"decision: {date} {label}{detail_part}")
    path.write_text(text, encoding="utf-8")
    return True


DW_ID_RE = re.compile(r"\bDW-(\d+)\b")


def next_seq(text: str) -> int:
    """The next free DW sequence number — one past the highest DW-<n> anywhere
    in the ledger (malformed entries included, so a number is never reused and
    the sweep numbering check stays satisfied)."""
    nums = [int(m.group(1)) for m in DW_ID_RE.finditer(text)]
    return (max(nums) + 1) if nums else 1


def field_line_present(body: str, field: str, value: str) -> bool:
    """True when `body` has a `field:` line whose value is exactly `value`,
    matching the shapes append_entry writes (plain, or backtick-wrapped as for
    `source_spec:`). Anchored per-line so an incidental substring elsewhere in
    the body (e.g. inside `reason:`) never counts as a match."""
    v = re.escape(value)
    return re.search(rf"(?m)^{re.escape(field)}:[ \t]*`?{v}`?[ \t]*$", body) is not None


def append_entry(
    path: Path,
    *,
    title: str,
    origin: str,
    source_spec: str,
    reason: str,
    status: str = "open",
    severity: str | None = None,
) -> str | None:
    """Append a new canonical `### DW-<seq>` entry numbered past the highest
    existing DW id, returning the new id (e.g. "DW-42").

    Idempotent: returns None without writing when an open entry already carries
    the same `origin:` marker and `source_spec:` — so re-running the same defer
    (e.g. a second sweep of the same story) never duplicates the entry. Creates
    the ledger (and parent dir) if it does not yet exist."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for entry in parse_ledger(text):
        if (
            entry.open
            and field_line_present(entry.body, "origin", origin)
            and field_line_present(entry.body, "source_spec", source_spec)
        ):
            return None
    dw_id = f"DW-{next_seq(text)}"
    lines = [f"### {dw_id}: {title}", f"origin: {origin}", f"source_spec: `{source_spec}`"]
    if severity:
        lines.append(f"severity: {severity}")
    lines.append(f"reason: {reason}")
    lines.append(f"status: {status}")
    block = "\n".join(lines) + "\n"
    # exactly one blank line between the previous content and the new entry
    if text == "" or text.endswith("\n\n"):
        sep = ""
    elif text.endswith("\n"):
        sep = "\n"
    else:
        sep = "\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + sep + block, encoding="utf-8")
    return dw_id


# ------------------------------------------------------------------- legacy
#
# Ledgers written before the DW format (older BMAD-method projects) are
# freeform markdown: "## Deferred from: ..." sections holding id'd or
# strikethrough bullets, "### D-1.2-003: title — RESOLVED" entry headings,
# topic sections closed with "(... — DONE)". parse_legacy() reads them
# tolerantly so the TUI can display them and a sweep can migrate them; the
# strict DW contract above is untouched — legacy items have no status line
# to flip, so mark_done/open_ids never see them.

# Severity is extracted forgivingly (the ledger is LLM-written): a
# `severity:`/`priority:` field line in any case, plain or bold-bulleted
# ("- **Severity:** high"), common synonyms accepted.
SEVERITY_ALIASES = {
    "critical": "critical",
    "blocker": "critical",
    "high": "high",
    "major": "high",
    "medium": "medium",
    "med": "medium",
    "moderate": "medium",
    "low": "low",
    "minor": "low",
    "trivial": "low",
}

SEVERITY_FIELD_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?(?:severity|priority)[ \t]*:[ \t]*(?:\*\*)?[ \t]*"
    r"([A-Za-z][\w-]*)",
    re.IGNORECASE | re.MULTILINE,
)


def field_severity(body: str) -> str | None:
    m = SEVERITY_FIELD_RE.search(body)
    return SEVERITY_ALIASES.get(m.group(1).lower()) if m else None


@dataclass(frozen=True)
class LegacyEntry:
    key: str  # stable content-derived identity, unique within the file
    id: str  # native id ("W2", "D-CAP-001", "0-1"), "" when the item has none
    title: str  # cleaned one-line title (markers/strikethrough stripped)
    done: bool
    severity: str | None  # normalized critical/high/medium/low, None unknown
    body: str  # the bullet/heading block verbatim
    section: str  # enclosing ##/### heading text, "" at top level
    span: tuple[int, int]  # char offsets in the ledger text


_DONE_WORDS = r"(?:DONE|RESOLVED|CLOSED|VERIFIED|DOCUMENTED|FIXED)"
_LINE_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
# a single whitespace-free digit-bearing token before ":" or "—" makes a
# heading an entry ("### D-CAP-001: title", "## D-8.6-001 — title");
# "## Epic 0: ..." has a space and "## 2026-06-09 — ..." is a date, so: section
_ENTRY_HEADING_RE = re.compile(r"^(~~)?([^\s:*~]*\d[^\s:*~]*)(?::[ \t]+|[ \t]+[—–][ \t]+)(.+)$")
_DATE_TOKEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SECTION_DONE_RE = re.compile(rf"(?:—|–|-|\()[ \t]*{_DONE_WORDS}\b[^)]*\)?[ \t]*$")
_TITLE_DONE_SUFFIX_RE = re.compile(rf"[ \t]*(?:—|–|-)[ \t]*{_DONE_WORDS}[ \t]*$")
_BARE_DONE_SUFFIX_RE = re.compile(rf"[ \t]*{_DONE_WORDS}\b.*$")
_BOLD_DONE_RE = re.compile(rf"\*\*{_DONE_WORDS}\b[^*]*\*\*")
_BRACKET_DONE_RE = re.compile(rf"\[{_DONE_WORDS}\]")
_DONE_PREFIX_RE = re.compile(rf"^\*\*{_DONE_WORDS}\b[^*]*\*\*:?[ \t]*")
# "- W-1.2-c — CLOSED: ..." / "CLOSED 2026-06-11 (story 1.11). ..."
_LEAD_DONE_RE = re.compile(rf"^{_DONE_WORDS}\b")
_LEAD_DONE_STRIP_RE = re.compile(
    rf"^{_DONE_WORDS}\b(?:[ \t]+\d{{4}}-\d{{2}}-\d{{2}})?(?:[ \t]*\([^)]*\))?[ \t]*[:.—–-]?[ \t]*"
)
# The generic bmad-dev-auto review appender writes a flat block per finding:
#   - source_spec: `spec-foo.md`
#     summary: <one sentence>
#     evidence: <why this is real>
# We recognize it so the `summary` becomes the title (not the source_spec path)
# and the entry migrates cleanly into the canonical `### DW-<seq>` shape.
_FLAT_SOURCE_RE = re.compile(r"^source_spec:[ \t]", re.IGNORECASE)
_FLAT_SUMMARY_RE = re.compile(r"^[ \t]*summary:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)
_BULLET_RE = re.compile(r"^[-*][ \t]+(.*)$")
_ITEM_ID_RE = re.compile(
    r"^(?:\*\*)?([^\s:*~]*\d[^\s:*~]*)(?:\*\*)?(?:[ \t]*[—–][ \t]+|:[ \t]+|[ \t]+-[ \t]+)"
)
_BRACKET_TOKEN_RE = re.compile(r"\[([A-Za-z]+)[^\]]*\]")
_LEAD_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*")
_STRUCK_LINE_RE = re.compile(r"^~~(.*)~~")
_TRAIL_BRACKET_RE = re.compile(r"[ \t]*\[[^\]]+\][ \t.]*$")


def _bracket_severity(s: str) -> str | None:
    for m in _BRACKET_TOKEN_RE.finditer(s):
        sev = SEVERITY_ALIASES.get(m.group(1).lower())
        if sev:
            return sev
    return None


def _clean_title(s: str) -> str:
    return " ".join(s.replace("**", "").split())


def _item_entry(first: str, body: str, section: str, section_done: bool) -> dict:
    """Interpret one bullet item; returns the pre-key entry fields."""
    if _FLAT_SOURCE_RE.match(first):
        # generic bmad-dev-auto flat appender block: title is the `summary`
        sm = _FLAT_SUMMARY_RE.search(body)
        summary = sm.group(1).strip() if sm else ""
        return {
            "id": "",
            "title": _clean_title(summary) if summary else _clean_title(first),
            "done": section_done,
            "severity": field_severity(body),
            "section": section,
        }
    content = first
    struck = False
    m = _STRUCK_LINE_RE.match(content)
    if m:  # "~~text~~ DONE" / "~~text~~ → resolution" on the first line
        struck = True
        content = m.group(1)
    elif content.startswith("~~") and "~~" in body[2:]:
        struck = True  # strikethrough closes on a later line
        content = content[2:]
    item_id = ""
    m = _ITEM_ID_RE.match(content)
    if m:
        item_id = m.group(1)
        content = content[m.end() :]
    done = (
        struck
        or section_done
        or bool(_LEAD_DONE_RE.match(content))
        or bool(_BOLD_DONE_RE.search(body))
        or bool(_BRACKET_DONE_RE.search(body))
    )
    content = _DONE_PREFIX_RE.sub("", content)
    content = _LEAD_DONE_STRIP_RE.sub("", content)
    while True:  # trailing "[MINOR]" / "[CLOSED]" tokens are not title text
        trimmed = _TRAIL_BRACKET_RE.sub("", content)
        if trimmed == content:
            break
        content = trimmed
    bold = _LEAD_BOLD_RE.match(content)
    if bold and len(bold.group(1).split()) >= 3:
        title = bold.group(1)  # notey: the bold phrase is the title
    else:
        title = content
    return {
        "id": item_id,
        "title": _clean_title(title),
        "done": done,
        "severity": _bracket_severity(body) or field_severity(body),
        "section": section,
    }


def _heading_entry(struck: bool, hid: str, rest: str, body: str, section: str) -> dict:
    """Interpret one '### D-1: title' entry heading (story-maker shape)."""
    title = rest
    done = struck
    m = _TITLE_DONE_SUFFIX_RE.search(title)
    if m:
        done = True
        title = title[: m.start()]
    if struck:
        title = _BARE_DONE_SUFFIX_RE.sub("", title.replace("~~", ""))
    return {
        "id": hid,
        "title": _clean_title(title),
        "done": done,
        "severity": field_severity(body) or _bracket_severity(body),
        "section": section,
    }


def parse_legacy(text: str) -> list[LegacyEntry]:
    """Extract legacy (non-DW) deferred items. Canonical DW entries are
    masked out first, so mixed ledgers parse both ways without overlap."""
    masked = text
    for e in parse_ledger(text):
        s, t = e.span
        masked = masked[:s] + re.sub(r"[^\n]", " ", masked[s:t]) + masked[t:]

    found: list[tuple[dict, tuple[int, int]]] = []
    section = ""
    section_done = False
    # a done section with no items yet: emitted as its own done entry unless
    # bullets, an entry heading, or a deeper child heading claim it first
    pending: dict | None = None  # {"level", "fields", "span"}
    item: dict | None = None  # accumulating bullet or entry heading

    def close_item(end: int) -> None:
        nonlocal item
        if item is None:
            return
        body = text[item["start"] : end].rstrip()
        span = (item["start"], item["start"] + len(body))
        if item["kind"] == "item":
            fields = _item_entry(item["first"], body, item["section"], item["section_done"])
        else:
            fields = _heading_entry(
                item["struck"], item["hid"], item["rest"], body, item["section"]
            )
        found.append((fields, span))
        item = None

    def emit_pending() -> None:
        nonlocal pending
        if pending is not None:
            found.append((pending["fields"], pending["span"]))
            pending = None

    offset = 0
    for line in text.splitlines(keepends=True):
        masked_line = masked[offset : offset + len(line)].rstrip("\n")
        hm = _LINE_HEADING_RE.match(masked_line)
        if hm:
            level = len(hm.group(1))
            close_item(offset)
            if level == 1:
                emit_pending()
                section, section_done = "", False
            elif level in (2, 3):
                if pending is not None and level > pending["level"]:
                    pending = None  # a child heading: the parent is structure
                else:
                    emit_pending()
                em = _ENTRY_HEADING_RE.match(hm.group(2))
                if em and _DATE_TOKEN_RE.fullmatch(em.group(2)):
                    em = None  # "## 2026-06-09 — ..." is a dated section
                if em:
                    pending = None
                    item = {
                        "kind": "heading",
                        "start": offset,
                        "struck": bool(em.group(1)),
                        "hid": em.group(2),
                        "rest": em.group(3),
                        "section": section,
                        "section_done": section_done,
                    }
                else:
                    htext = hm.group(2)
                    struck = htext.startswith("~~") and "~~" in htext[2:]
                    section = _clean_title(htext.replace("~~", ""))
                    section_done = struck or bool(_SECTION_DONE_RE.search(htext))
                    if section_done:
                        pending = {
                            "level": level,
                            "span": (offset, offset + len(line.rstrip("\n"))),
                            "fields": {
                                "id": "",
                                "title": section,
                                "done": True,
                                "severity": None,
                                "section": "",
                            },
                        }
            offset += len(line)
            continue
        if item is not None and item["kind"] == "heading":
            if masked_line.strip() == "---" or (masked_line.strip() == "" and line.strip() != ""):
                close_item(offset)  # rule, or a masked canonical entry
            offset += len(line)
            continue
        bm = _BULLET_RE.match(masked_line)
        if bm:
            close_item(offset)
            pending = None
            item = {
                "kind": "item",
                "start": offset,
                "first": bm.group(1),
                "section": section,
                "section_done": section_done,
            }
        elif masked_line.strip() in ("", "---"):
            # a masked canonical entry reads as blank: it still bounds the item
            if masked_line.strip() == "---" or line.strip() != masked_line.strip():
                close_item(offset)
        elif masked_line[0] in " \t":
            pass  # indented continuation of the current item
        else:
            close_item(offset)  # column-0 prose ends an item, emits nothing
        offset += len(line)
    close_item(len(text))
    emit_pending()

    entries: list[LegacyEntry] = []
    counts: dict[str, int] = {}
    for fields, span in found:
        base = hashlib.sha1(
            f"{fields['section']}\0{fields['id'] or fields['title']}".encode(),
            usedforsecurity=False,  # display/identity key, not a credential
        ).hexdigest()[:10]
        n = counts.get(base, 0) + 1
        counts[base] = n
        entries.append(
            LegacyEntry(
                key=base if n == 1 else f"{base}-{n}",
                id=fields["id"],
                title=fields["title"],
                done=fields["done"],
                severity=fields["severity"],
                body=text[span[0] : span[1]],
                section=fields["section"],
                span=span,
            )
        )
    return entries


def has_legacy(text: str) -> bool:
    return bool(parse_legacy(text))
