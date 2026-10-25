"""Tests for usagereset.py: the usage-limit `resets …` clause parse (#610).

None of these depend on the host's zone database: the named-zone route is
pinned by monkeypatching the module's `ZoneInfo` lookup to a fixed offset, and
the one real-zone test skips when the zone is absent."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from bmad_loop import usagereset
from bmad_loop.usagereset import UsageReset, parse_reset

UTC = timezone.utc
PLUS7 = timezone(timedelta(hours=7), "+07")
PLUS2 = timezone(timedelta(hours=2), "+02")
# Sunday 2026-10-25 15:00 UTC.
NOW = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)


@pytest.fixture
def fixed_zones(monkeypatch: pytest.MonkeyPatch) -> dict[str, tzinfo]:
    """The module's zone lookup, answered from a fixed table (no tzdata)."""
    zones: dict[str, tzinfo] = {"Asia/Ho_Chi_Minh": PLUS7, "Europe/Warsaw": PLUS2}

    def lookup(name: str) -> tzinfo:
        try:
            return zones[name]
        except KeyError:
            raise ZoneInfoNotFoundError(name) from None

    monkeypatch.setattr(usagereset, "ZoneInfo", lookup)
    return zones


def _at(line: str, now: datetime = NOW) -> datetime:
    reset = parse_reset(line, now)
    assert reset is not None and reset.unresolved is None, reset
    assert reset.at is not None
    return reset.at


# --- every field form --------------------------------------------------------


def test_time_with_minutes_resolves_in_nows_zone() -> None:
    reset = parse_reset("limit · resets 7:20pm", NOW)
    assert reset == UsageReset(
        raw="7:20pm", at=datetime(2026, 10, 25, 19, 20, tzinfo=UTC), unresolved=None
    )
    assert reset.value == "2026-10-25T19:20:00+00:00"


def test_time_with_named_zone(fixed_zones: dict[str, tzinfo]) -> None:
    reset = parse_reset("limit · resets 12:50pm (Asia/Ho_Chi_Minh)", NOW)
    assert reset is not None
    assert reset.raw == "12:50pm (Asia/Ho_Chi_Minh)"
    # 15:00 UTC is 22:00 in +07: 12:50pm there is tomorrow.
    assert reset.at == datetime(2026, 10, 26, 12, 50, tzinfo=PLUS7)
    assert reset.value == "2026-10-26T12:50:00+07:00"


def test_hour_only_with_named_zone(fixed_zones: dict[str, tzinfo]) -> None:
    # 15:00 UTC is 17:00 in +02: 6pm there is today.
    assert _at("resets 6pm (Europe/Warsaw)") == datetime(2026, 10, 25, 18, 0, tzinfo=PLUS2)


def test_weekday_form() -> None:
    # NOW is a Sunday: Mon 12:00am is midnight starting Monday 2026-10-26.
    assert _at("resets Mon 12:00am") == datetime(2026, 10, 26, 0, 0, tzinfo=UTC)


def test_month_day_form() -> None:
    assert _at("resets Sep 15 at 7pm") == datetime(2027, 9, 15, 19, 0, tzinfo=UTC)
    assert _at("resets Nov 2 at 7pm") == datetime(2026, 11, 2, 19, 0, tzinfo=UTC)


def test_half_past_noon() -> None:
    assert _at("resets 12:30pm") == datetime(2026, 10, 26, 12, 30, tzinfo=UTC)


def test_trailing_pane_text_is_not_part_of_the_clause() -> None:
    reset = parse_reset("resets 7:20pm… esc to interrupt", NOW)
    assert reset is not None and reset.raw == "7:20pm"


# --- rollover ----------------------------------------------------------------


def test_time_already_past_today_rolls_past_midnight() -> None:
    late = datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
    assert _at("resets 1:15am", late) == datetime(2026, 10, 26, 1, 15, tzinfo=UTC)


def test_the_reset_is_strictly_after_now() -> None:
    exact = datetime(2026, 10, 25, 19, 20, tzinfo=UTC)
    assert _at("resets 7:20pm", exact) == datetime(2026, 10, 26, 19, 20, tzinfo=UTC)


def test_same_weekday_already_past_rolls_a_week() -> None:
    sunday_noon = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    assert _at("resets Sun 9am", sunday_noon) == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    assert _at("resets Sun 1pm", sunday_noon) == datetime(2026, 10, 25, 13, 0, tzinfo=UTC)


def test_weekday_later_this_week() -> None:
    assert _at("resets Wed 8am") == datetime(2026, 10, 28, 8, 0, tzinfo=UTC)


def test_month_day_past_rolls_into_next_year() -> None:
    new_years_eve = datetime(2026, 12, 31, 22, 0, tzinfo=UTC)
    assert _at("resets Jan 1 at 9am", new_years_eve) == datetime(2027, 1, 1, 9, 0, tzinfo=UTC)
    assert _at("resets Dec 31 at 9pm", new_years_eve) == datetime(2027, 12, 31, 21, 0, tzinfo=UTC)


def test_feb_29_lands_on_the_next_leap_year() -> None:
    assert _at("resets Feb 29 at 9am") == datetime(2028, 2, 29, 9, 0, tzinfo=UTC)


# --- 12am / 12pm -------------------------------------------------------------


@pytest.mark.parametrize(
    ("clause", "hour"),
    [("12am", 0), ("12:05am", 0), ("12pm", 12), ("12:05pm", 12), ("1am", 1), ("11pm", 23)],
)
def test_twelve_oclock_edges(clause: str, hour: int) -> None:
    at = _at(f"resets {clause}", datetime(2026, 10, 25, 0, 0, 0, 1, tzinfo=UTC))
    assert at.hour == hour


# --- no clause / garbage -----------------------------------------------------


@pytest.mark.parametrize(
    "line",
    ["", "You've used 90% of your limit", "presets 7pm", "resets", "resets ", "the resets"],
)
def test_no_clause_is_none(line: str) -> None:
    assert parse_reset(line, NOW) is None


@pytest.mark.parametrize(
    ("line", "raw"),
    [
        ("resets soon", "soon"),
        ("resets 7:2", "7:2"),
        ("resets 19:20", "19:20"),
        ("resets 13pm", "13pm"),
        ("resets 0am", "0am"),
        ("resets 7:61pm", "7:61pm"),
        ("resets Sep 31 at 7pm", "Sep 31 at 7pm"),
        ("resets 7pm (GMT+7)", "7pm (GMT+7)"),
        ("resets 7pmx", "7pmx"),
    ],
)
def test_garbage_is_unparsed_and_keeps_raw(line: str, raw: str) -> None:
    reset = parse_reset(line, NOW)
    assert reset == UsageReset(raw=raw, at=None, unresolved="unparsed")
    assert reset.value == raw


def test_unparsed_raw_is_truncated() -> None:
    reset = parse_reset("resets " + "x" * 500, NOW)
    assert reset is not None and reset.unresolved == "unparsed"
    assert len(reset.raw) == 80


# --- zone unavailable --------------------------------------------------------


def test_zone_not_found_is_zone_unavailable(fixed_zones: dict[str, tzinfo]) -> None:
    reset = parse_reset("resets 7pm (Mars/Olympus_Mons)", NOW)
    assert reset == UsageReset(
        raw="7pm (Mars/Olympus_Mons)", at=None, unresolved="zone-unavailable"
    )
    assert reset.value == "7pm (Mars/Olympus_Mons)"


@pytest.mark.parametrize("exc", [ZoneInfoNotFoundError("x"), ValueError("bad key"), OSError()])
def test_any_zone_lookup_fault_is_zone_unavailable(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    def lookup(name: str) -> tzinfo:
        raise exc

    monkeypatch.setattr(usagereset, "ZoneInfo", lookup)
    reset = parse_reset("resets 6pm (Europe/Warsaw)", NOW)
    assert reset is not None
    assert (reset.at, reset.unresolved) == (None, "zone-unavailable")


def test_real_zone_database_when_present() -> None:
    """The unpatched lookup, end to end, over a DST change (skipped without
    tzdata): Warsaw leaves summer time on 2026-10-25, so 6pm there today is
    17:00 UTC."""
    try:
        ZoneInfo("Europe/Warsaw")
    except ZoneInfoNotFoundError:
        pytest.skip("no tzdata for Europe/Warsaw on this host")
    at = _at("resets 6pm (Europe/Warsaw)", datetime(2026, 10, 25, 10, 0, tzinfo=UTC))
    assert at.astimezone(UTC) == datetime(2026, 10, 25, 17, 0, tzinfo=UTC)


# --- a naive now resolves through the host zone's DST rules -----------------

# Central European rules as a POSIX TZ string (no tzdata needed): +01 in winter,
# +02 from the last Sunday of March 02:00 to the last Sunday of October 03:00.
_CET = "CET-1CEST,M3.5.0,M10.5.0/3"


@pytest.fixture
def host_zone() -> Iterator[Callable[[str], None]]:
    """Point the process's local zone at a TZ string, restored afterwards."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only")
    saved = os.environ.get("TZ")

    def set_zone(tz: str) -> None:
        os.environ["TZ"] = tz
        time.tzset()

    yield set_zone
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def test_naive_now_crosses_spring_forward(host_zone: Callable[[str], None]) -> None:
    """00:30 on the night summer time starts: 6am is 06:00+02, 4.5 hours away —
    not 06:00+01 (today's offset), which would read 5.5 and trip the cap."""
    host_zone(_CET)
    now = datetime(2026, 3, 29, 0, 30)
    at = _at("resets 6am", now)
    assert at.utcoffset() == timedelta(hours=2)
    assert at - now.astimezone() == timedelta(hours=4, minutes=30)


def test_naive_now_weekday_crosses_fall_back(host_zone: Callable[[str], None]) -> None:
    """A weekly reset across the end of summer time keeps its wall time."""
    host_zone(_CET)
    at = _at("resets Mon 12:00am", datetime(2026, 10, 23, 12, 0))
    assert at.utcoffset() == timedelta(hours=1)
    assert at.astimezone(UTC) == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)


def test_adapter_local_now_resolves_through_dst(
    host_zone: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic adapter's `_local_now` feeds this resolution: it must stay
    naive, or the reset is pinned to today's offset again."""
    from bmad_loop.adapters import generic

    host_zone(_CET)
    # 2026-03-28 23:30 UTC = 00:30 CET on the night summer time starts.
    monkeypatch.setattr(time, "time", lambda: datetime(2026, 3, 28, 23, 30, tzinfo=UTC).timestamp())
    at = _at("resets 6am", generic._local_now())
    assert at.astimezone(UTC) == datetime(2026, 3, 29, 4, 0, tzinfo=UTC)
