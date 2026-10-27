"""The wrap-tolerant pane matcher (`adapters/panematch.py`, #727).

A line wider than the pane arrives as several physical rows. These cases pin the
two wraps measured on tmux 3.4 and psmux 3.4.0 (an autowrap that splits a word,
a word-wrap whose trailing blank capture-pane trimmed), the profile's own lines
never matching once wrapped, the spent-line skip surviving a wrap, and the
timeout doctrine."""

from __future__ import annotations

import textwrap
from importlib import resources

import pytest
import regex

from bmad_loop.adapters.panematch import Hit, MatchTimeout, match_rows, screen_rows
from bmad_loop.adapters.profile import get_profile

CLAUDE = get_profile("claude")
PARKED = tuple(regex.compile(p) for p in CLAUDE.parked_prompt_patterns)
USAGE = tuple(regex.compile(p) for p in CLAUDE.usage_limit_patterns + CLAUDE.usage_wait_patterns)
TIMEOUT = 2.0

BYPASS_HEADING = "WARNING: Claude Code running in Bypass Permissions " + "mode"
USAGE_SESSION_LINE = "⎿  You've hit your session limit · resets 7:20pm"


def _match(rows, patterns=PARKED, **kw):
    return match_rows(rows, patterns, timeout=TIMEOUT, **kw)


def _autowrap(line: str, width: int) -> list[str]:
    """A terminal's autowrap: cut at the right edge, mid-word or not; capture-pane
    trims each row's trailing blanks."""
    return [line[i : i + width].rstrip() for i in range(0, len(line), width)] or [""]


def _wordwrap(line: str, width: int) -> list[str]:
    """A renderer's word-wrap: break at spaces, the space itself dropped."""
    return textwrap.wrap(line, width=width) or [""]


@pytest.mark.parametrize(
    "rows",
    [
        # dracic's measured footer rows (#727): width 30 autowraps mid-word...
        [" Enter to confirm · Esc to can", "cel"],
        # ...width 24 word-wraps at a space capture-pane then trimmed
        [" Enter to confirm · Esc", "to cancel"],
    ],
)
def test_wrapped_footer_matches(rows):
    screen = "\r\n".join([" Do you trust the files in this folder?", "", *rows, ""])
    found = _match(screen_rows(screen))
    assert isinstance(found, Hit)
    assert found.line.strip() == "Enter to confirm · Esc " + "to cancel"
    assert found.evidence.startswith(f"pane matched {PARKED[1].pattern!r}: ")


@pytest.mark.parametrize("width", [20, 30, 40, 50])
@pytest.mark.parametrize("wrap", [_autowrap, _wordwrap])
def test_wrapped_bypass_heading_matches(width, wrap):
    rows = wrap(BYPASS_HEADING, width)
    assert len(rows) in (2, 3)
    found = _match(["", *rows, "", "❯ 1. No, exit"])
    assert isinstance(found, Hit)
    assert found.line.strip() == BYPASS_HEADING


def _pattern_lines() -> list[str]:
    text = (
        resources.files("bmad_loop.data")
        .joinpath("profiles")
        .joinpath("claude.toml")
        .read_text(encoding="utf-8")
    )
    lines = [ln for ln in text.splitlines() if "mod[e]" in ln or "t[o]" in ln]
    assert len(lines) >= 2
    return lines


@pytest.mark.parametrize("wrap", [_autowrap, _wordwrap])
def test_profile_pattern_lines_never_match_wrapped(wrap):
    """A session that prints or diffs claude.toml at any pane width must not read
    as parked: joining rows only adds or drops whitespace, so `mod[e]`/`t[o]`
    keep their brackets."""
    hits = [
        (width, line)
        for line in _pattern_lines()
        for width in range(10, 61)
        if _match(wrap(line, width)) is not None
    ]
    assert hits == []


def test_spent_joined_line_does_not_rematch():
    """`_QuotaWait.spent` stores the joined matched line. Neither its own window
    nor a larger one that merely extends it by a row re-matches it, while the
    same line printed again with a different reset still does."""
    rows = ["working…", "⎿  You've hit your session li", "mit · resets 7:20pm", "❯ "]
    first = _match(rows, USAGE)
    assert isinstance(first, Hit)
    assert first.line.strip() == USAGE_SESSION_LINE.strip()
    spent = {first.line.strip()}
    assert _match(rows, USAGE, skip=spent) is None

    again = [*rows, "⎿  You've hit your session li", "mit · resets 9pm"]
    fresh = _match(again, USAGE, skip=spent)
    assert isinstance(fresh, Hit)
    assert fresh.line.strip().endswith("resets 9pm")


def test_spent_row_does_not_leak_into_a_window():
    """A spent single row is skipped by the row pass; a window holding it is
    skipped too, so the next row joined onto it cannot bring it back."""
    rows = [USAGE_SESSION_LINE, "more", "❯ "]
    assert isinstance(_match(rows, USAGE), Hit)
    assert _match(rows, USAGE, skip={USAGE_SESSION_LINE.strip()}) is None


def test_row_pass_wins_over_window_pass():
    """A row that matches on its own is reported even when an earlier wrapped
    pair matches too: the window pass runs only after the row pass found nothing."""
    whole = " Enter to confirm · Esc " + "to cancel"
    found = _match([" Enter to confirm · Esc to can", "cel", "", whole])
    assert isinstance(found, Hit)
    assert found.line == whole


def test_evil_pattern_times_out():
    evil = (regex.compile(r"(a|aa)+$"),)
    found = match_rows(["a" * 60 + "b"], evil, timeout=0.01)
    assert isinstance(found, MatchTimeout)
    assert found.pattern == r"(a|aa)+$"
    assert found.error.startswith("TimeoutError")


def test_window_pass_timeout_returns_at_once():
    """A timeout in the window pass ends the look: no later window or pattern
    is searched."""
    searched: list[str] = []

    class _JoinedRunaway:
        pattern = "runaway"

        def search(self, text, timeout=None):
            searched.append(text)
            if text == "ab":
                raise TimeoutError
            return None

    found = match_rows(["a", "b", "c", "d"], (_JoinedRunaway(),), timeout=TIMEOUT)
    assert found == MatchTimeout("runaway", "TimeoutError: ")
    assert searched == ["a", "b", "c", "d", "ab"]


def test_no_match_is_none():
    assert _match(screen_rows("\x1b[1mhello\x1b[0m\r\nworld\n")) is None
