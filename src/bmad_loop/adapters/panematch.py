"""Wrap-tolerant pattern matching over a captured pane (#727).

The live pane gates (the parked gate's ``parked_prompt_patterns``, the usage
gates' ``usage_limit_patterns`` and ``usage_wait_patterns``) match profile regexes
against ``capture-pane -p`` output, which is one string per PHYSICAL row. A line
the CLI printed wider than the pane arrives split across rows, so a pattern
matched one row at a time misses it. Claude Code's dialog footer
(``Enter to confirm · Esc to cancel``, 33 columns with its indent) stops matching
below 33 columns, and the stall nudge's trailing Enter then answers the dialog.

Two ways a line breaks across rows, measured on tmux 3.4 and psmux 3.4.0:

- An autowrap splits mid-word at the right edge: ``… Esc to can`` / ``cel``. The
  rows rejoin with nothing between them: ``rstrip(a) + lstrip(b)``.
- A word-wrap breaks at a space, and capture-pane trims the trailing blank:
  ``… Esc`` / ``to cancel``. The rows rejoin with one space:
  ``rstrip(a) + " " + lstrip(b)``.

A row cannot tell which happened, so ``match_rows`` tries both at every
boundary. It first runs the plain row pass (rows outer, patterns inner), the
matcher as it was before wrap tolerance. Only when that finds nothing does it
run the window pass: for each start row, a run of 2 and then 3 adjacent rows,
joined at each boundary without and then with a space (a 3-row window can wrap
one way at its first boundary and the other way at its second). A hit counts
only when it starts in the window's first row; one that starts later belongs to
a later window, so ``Hit.line`` never carries an unrelated row in front. The
earliest start row wins, then the smaller window, then the earlier join. A
pattern still cannot reach across more rows than that.

The profiles' single-character classes (``mod[e]``, ``t[o]``) that keep a pattern
from matching its own source line survive both joins: joining only removes or
inserts whitespace, never the bracket.

``skip`` holds lines (compared stripped) that are never a match: the usage
gates pass the lines of quota waits the CLI already continued from
(``_QuotaWait.spent``). A spent line may be a joined window, so a window is
skipped when any of its rows is spent, or when any join of it contains, or is
contained in, a spent line. A window that only extends a spent wrapped line by a
row cannot re-match it.

Pure: no I/O, no adapter state. The caller captures the pane and records a
timeout.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

import regex

from .env_fault import _ANSI_RE, _excerpt

# Largest number of adjacent rows the window pass joins.
MAX_WINDOW_ROWS = 3


@dataclass(frozen=True)
class Hit:
    """A pattern matched. ``evidence`` quotes the pattern and an excerpt of
    ``line``, the row (or the joined rows) it matched."""

    evidence: str
    line: str


@dataclass(frozen=True)
class MatchTimeout:
    """A search of ``pattern`` exceeded its timeout, so the look at the pane
    failed. ``error`` is the ``TimeoutError`` rendered as ``Type: message``."""

    pattern: str
    error: str


def screen_rows(screen: str) -> list[str]:
    """``screen`` as its physical rows, with ANSI and terminal-control sequences
    stripped and a bare CR read as a row break."""
    return _ANSI_RE.sub("", screen.replace("\r", "\n")).split("\n")


def _joins(window: Sequence[str]) -> list[str]:
    """``window``'s rows joined every way: each boundary without, then with, one
    space, the earlier boundary varying slowest."""
    joins = [window[0]]
    for row in window[1:]:
        joins = [j.rstrip() + gap + row.lstrip() for j in joins for gap in ("", " ")]
    return joins


def _spent_join(joined: str, skip: Collection[str]) -> bool:
    stripped = joined.strip()
    return any(line and (stripped in line or line in stripped) for line in skip)


def _search(
    text: str, patterns: Sequence[regex.Pattern[str]], timeout: float, head: int | None = None
) -> Hit | MatchTimeout | None:
    """The first of ``patterns`` to match ``text``, counting only a hit that
    starts before ``head`` when one is given."""
    for pat in patterns:
        try:
            hit = pat.search(text, timeout=timeout)
        except TimeoutError as e:
            return MatchTimeout(pat.pattern, f"{type(e).__name__}: {e}")
        if hit is not None and (head is None or hit.start() < head):
            return Hit(f"pane matched {pat.pattern!r}: {_excerpt(text, hit.start())}", text)
    return None


def match_rows(
    rows: Sequence[str],
    patterns: Sequence[regex.Pattern[str]],
    *,
    timeout: float,
    skip: Collection[str] = frozenset(),
) -> Hit | MatchTimeout | None:
    """The first match of any of ``patterns`` in ``rows``: the row pass, then,
    only when it found nothing, the window pass (module docstring). A search
    that exceeds ``timeout`` seconds ends the look at once as a
    ``MatchTimeout``."""
    for line in rows:
        if line.strip() in skip:
            continue
        if (found := _search(line, patterns, timeout)) is not None:
            return found
    for start in range(len(rows)):
        head = len(rows[start].rstrip())
        for size in range(2, MAX_WINDOW_ROWS + 1):
            window = rows[start : start + size]
            if len(window) < size or any(row.strip() in skip for row in window):
                continue
            joins = _joins(window)
            if skip and any(_spent_join(joined, skip) for joined in joins):
                continue
            for joined in joins:
                if (found := _search(joined, patterns, timeout, head)) is not None:
                    return found
    return None
