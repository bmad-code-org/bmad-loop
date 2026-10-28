"""Parse the ``resets …`` clause of a coding CLI's usage-limit line (#610).

Pure: no clock reads, no I/O beyond the zone database (the system's, or the
``tzdata`` package zoneinfo falls back to, a Windows dependency) and the host's
local-time rules. The caller passes ``now`` and the full pane line that a
``usage_limit_patterns`` / ``usage_wait_patterns`` entry matched; the result
feeds the pause reason, ``heartbeat.json`` (``quota_resets_at``), the
``session-end`` journal field ``usage_limit_resets`` and the quota wait's
reset-beyond-cap rule.

Grammar, after the literal ``resets ``::

    [<Weekday> | <Mon> <D> at ]H[:MM](am|pm)[ (<Area/City>)]

with weekdays ``Mon``..``Sun`` and months ``Jan``..``Dec``, which covers the
forms seen in the field: ``7:20pm``, ``12:50pm (Asia/Ho_Chi_Minh)``,
``6pm (Europe/Warsaw)``, ``Mon 12:00am``, ``Sep 15 at 7pm``. The reset is the
next occurrence of that wall time strictly after ``now``: in the named zone
when the clause names one, else in ``now``'s own zone. A naive ``now`` means
the host's local zone, resolved per candidate through its DST rules; an aware
``now`` from ``astimezone()`` carries only today's fixed offset, which a reset
across a DST change would get an hour wrong.

Degrade contract: :func:`parse_reset` never raises. A line with no clause is
``None``. A clause it cannot resolve keeps its raw text with ``at=None`` and
names why in ``unresolved``: ``"zone-unavailable"`` only when neither the
system zone database nor the ``tzdata`` package provides the named zone, and
``"unparsed"`` for any other shape. The caller makes that degrade visible (the
generic adapter writes a ``usage-reset-unparsed`` crumb and carries the raw
text); it is never read as "no reset".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from datetime import timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["UsageReset", "parse_reset"]

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# The clause: `resets ` followed by at least one non-space character. Anything
# shorter is no clause at all.
_CLAUSE_RE = re.compile(r"\bresets (?P<rest>\S.*)")
_TIME_RE = re.compile(
    r"(?:(?P<wd>" + "|".join(_WEEKDAYS) + r") "
    r"|(?P<mon>" + "|".join(_MONTHS) + r") (?P<day>\d{1,2}) at )?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm)(?![A-Za-z0-9])"
    r"(?: \((?P<zone>[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)+)\))?"
)
# The longest raw text kept for a clause that did not parse.
_RAW_MAX = 80
# How many years ahead a `<Mon> <D>` clause may land (Feb 29 needs up to 4).
_YEARS_AHEAD = 5


@dataclass(frozen=True)
class UsageReset:
    """One parsed reset clause. ``raw`` is the clause text after ``resets``
    (the matched span when it parsed, else the line's remainder, truncated).
    ``at`` is the resolved reset, aware, in the named zone or ``now``'s; None
    when unresolved, and then ``unresolved`` names why."""

    raw: str
    at: datetime | None
    unresolved: str | None

    @property
    def value(self) -> str:
        """What the surfaces carry: ISO-8601 when resolved, else the raw text."""
        return self.at.isoformat() if self.at is not None else self.raw


def parse_reset(line: str, now: datetime) -> UsageReset | None:
    """The reset clause in ``line``, resolved against ``now``; None when the
    line has no ``resets `` clause. Never raises (module docstring)."""
    clause = _CLAUSE_RE.search(line)
    if clause is None:
        return None
    rest = clause.group("rest").rstrip()
    m = _TIME_RE.match(rest)
    if m is None or (m.group("zone") is None and rest[m.end() :].startswith(" (")):
        # No time, or a parenthesised zone of a shape the grammar does not
        # know: resolving it in the local zone would be silently wrong.
        return _unresolved(rest[:_RAW_MAX], "unparsed")
    raw = m.group(0)
    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return _unresolved(raw, "unparsed")
    hour = hour % 12 + (12 if m.group("ampm") == "pm" else 0)
    zone_name = m.group("zone")
    tz: tzinfo | None = now.tzinfo
    if zone_name is not None:
        try:
            tz = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return _unresolved(raw, "zone-unavailable")
    try:
        local_now = now.astimezone(tz)
        at = _next_occurrence(m, dtime(hour, minute), local_now, tz)
    except (ValueError, OverflowError, OSError):
        return _unresolved(raw, "unparsed")
    if at is None:
        return _unresolved(raw, "unparsed")
    return UsageReset(raw=raw, at=at, unresolved=None)


def _unresolved(raw: str, why: str) -> UsageReset:
    return UsageReset(raw=raw, at=None, unresolved=why)


def _next_occurrence(
    m: re.Match[str], wall: dtime, now: datetime, tz: tzinfo | None
) -> datetime | None:
    """The first ``wall`` time strictly after ``now`` on a date the clause
    allows. Dates are built with ``datetime.combine`` in ``tz`` (or as host
    local wall times when ``tz`` is None), so a DST change between now and the
    reset keeps the stated wall time."""

    def at(day: date) -> datetime:
        if tz is None:
            return datetime.combine(day, wall).astimezone()
        return datetime.combine(day, wall, tzinfo=tz)

    today = now.date()
    if m.group("mon") is not None:
        month = _MONTHS.index(m.group("mon")) + 1
        day = int(m.group("day"))
        for year in range(today.year, today.year + _YEARS_AHEAD):
            try:
                candidate = at(date(year, month, day))
            except ValueError:
                continue
            if candidate > now:
                return candidate
        return None
    if m.group("wd") is not None:
        ahead = (_WEEKDAYS.index(m.group("wd")) - today.weekday()) % 7
        candidate = at(today + timedelta(days=ahead))
        return candidate if candidate > now else at(today + timedelta(days=ahead + 7))
    candidate = at(today)
    return candidate if candidate > now else at(today + timedelta(days=1))
