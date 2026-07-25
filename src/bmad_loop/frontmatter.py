"""Pure spec-frontmatter parsing: read the YAML ``---``…``---`` block, normalize
the status token, and rewrite ``status:`` in place.

Zero git/subprocess dependencies (only stdlib + PyYAML) so pure domain modules
(``stories``, ``devcontract``) can read spec status without importing ``verify``
and dragging in its whole git surface (assessment finding F-1). ``verify``
re-exports these names, so every existing ``verify.<name>`` / ``from .verify
import <name>`` call site stays valid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _split_frontmatter(text: str) -> tuple[str, str, str] | None:
    """Split a document into ``(before, block, after)`` around its YAML
    frontmatter, where ``before + block + after == text`` exactly.

    The opening and closing ``---`` are recognized ONLY as standalone delimiter
    lines (``line.rstrip() == "---"``), so a ``---`` substring inside a scalar
    value (e.g. ``title: 'restore --- review'``) is never mistaken for the
    closing boundary — the flaw a plain ``text.split("---", 2)`` has. ``before``
    is the opening delimiter line, ``block`` is the YAML content between the
    delimiters, and ``after`` begins with the closing delimiter line; callers
    rewrite ``block`` and reconstruct the file byte-for-byte. Returns ``None``
    when the text has no opening delimiter line or no closing delimiter line.
    """
    lines = text.splitlines(keepends=True)  # "".join(lines) == text
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return lines[0], "".join(lines[1:i]), "".join(lines[i:])
    return None


def read_frontmatter_or_none(path: Path) -> dict[str, Any] | None:
    """Like :func:`read_frontmatter`, but distinguishes *unreadable* from *absent*:
    ``None`` when the content exists and could not be parsed, ``{}`` when there is
    genuinely no frontmatter (or no file) to read.

    Every status gate wants the flattening — a spec that cannot be parsed reads as
    status ``""`` and retries or repairs — so :func:`read_frontmatter` keeps it.
    One caller needs the difference: the deferred-work close reads a declaration
    it will otherwise fall back to a verified capture for, and ``{}`` there says
    "this spec declares nothing", which is a *retraction*. Collapsing an
    unparseable spec into that retraction destroyed the fallback for exactly the
    fault class it exists to survive (#284 round-6 review, finding 5)."""
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # A non-UTF-8 file carries no readable frontmatter (UnicodeDecodeError is
        # a ValueError, so it slips past callers' except-OSError guards).
        return None
    split = _split_frontmatter(text)
    if split is None:
        return {}  # no frontmatter block is an answer, not a failure to read one
    try:
        doc = yaml.safe_load(split[1])
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else {}


def read_frontmatter(path: Path) -> dict[str, Any]:
    """A spec's frontmatter as a dict, with anything unreadable flattened to ``{}``.

    Every status gate then reads status "" and returns a clean retry/repair
    outcome instead of crashing mid-verify. A caller that must tell an unparseable
    spec from one carrying no such field wants
    :func:`read_frontmatter_or_none` instead."""
    return read_frontmatter_or_none(path) or {}


def status_of(fm: dict[str, Any]) -> str:
    """Normalized spec status from a frontmatter dict: stripped + lowercased.

    The single point all spec-frontmatter status gates read through, so casing
    never decides a gate — the spec template and sprint-status tokens are
    lowercase, so a stray ``Done``/``In-Review`` from a hand-edited spec still
    matches. (``devcontract`` keeps its own lowercasing; it parses skill-written
    prose where casing genuinely varies.)
    """
    return str(fm.get("status", "")).strip().lower()


def set_frontmatter_status(path: Path, status: str) -> bool:
    """Rewrite the `status:` field in a spec's `---`…`---` frontmatter block.

    A minimal in-place line replacement (not a YAML round-trip) so the spec's
    formatting, comments, and field order survive — only the status value
    changes. Returns True when the file was rewritten, False when it has no
    frontmatter or already carries `status`. Idempotent.
    """
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    split = _split_frontmatter(text)
    if split is None:
        return False
    before, block, after = split
    block_lines = block.splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(block_lines):
        stripped = line.lstrip()
        if stripped.startswith("status:") and not stripped.startswith("status_"):
            indent = line[: len(line) - len(stripped)]
            newline = "\n" if line.endswith("\n") else ""
            block_lines[i] = f"{indent}status: {status}{newline}"
            replaced = True
            break
    if not replaced:
        return False
    rebuilt = before + "".join(block_lines) + after
    if rebuilt == text:  # already at the target value — idempotent no-op
        return False
    path.write_text(rebuilt, encoding="utf-8")
    return True
