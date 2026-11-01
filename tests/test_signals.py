import json
from pathlib import Path

import pytest

from bmad_loop.signals import (
    HookEvent,
    SessionAttribution,
    SignalWatcher,
    attribute_events,
    is_session_event,
    session_events,
)


def write_event(events_dir, ts, task_id, event, **extra):
    payload = {"ts": ts, "event": event, "task_id": task_id, **extra}
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"{ts}-{task_id}-{event}.json").write_text(json.dumps(payload))


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"notification_type": "permission_prompt"}, "permission_prompt"),
        ({"notification_type": 3}, None),  # a non-string is dropped, not coerced
        ({}, None),  # an older relay forwards no subtype at all
    ],
)
def test_parse_event_reads_the_notification_type(tmp_path, extra, expected):
    """DW-348: the relay's forwarded subtype lands on the HookEvent (str only)."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "Notification", **extra)
    (event,) = watcher.poll()
    assert event.event == "Notification"
    assert event.notification_type == expected


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"source": "clear"}, "clear"),
        ({"source": 3}, None),  # a non-string is dropped, not coerced
        ({}, None),  # an older vendored relay forwards no source at all
    ],
)
def test_parse_event_reads_the_session_start_source(tmp_path, extra, expected):
    """#767: the relay's forwarded SessionStart source lands on the HookEvent
    (str only)."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "SessionStart", **extra)
    (event,) = watcher.poll()
    assert event.event == "SessionStart"
    assert event.source == expected


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"model": "claude-opus-4-6[1m]"}, "claude-opus-4-6[1m]"),
        ({"model": 3}, None),  # a non-string is dropped, not coerced
        ({}, None),  # an older vendored relay forwards no model at all
    ],
)
def test_parse_event_reads_the_hook_model(tmp_path, extra, expected):
    """#859: the relay's forwarded hook `model` lands on the HookEvent (str only)."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "SessionStart", **extra)
    (event,) = watcher.poll()
    assert event.event == "SessionStart"
    assert event.model == expected


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"lineage": "match"}, "match"),
        ({"lineage": "mismatch"}, "mismatch"),
        ({"lineage": "unknown"}, "unknown"),
        ({"lineage": "MATCH"}, None),  # outside the tag set: dropped, not normalized
        ({"lineage": "maybe"}, None),
        ({"lineage": 1}, None),  # a non-string is dropped, not coerced
        ({"lineage": ["match"]}, None),
        ({}, None),  # an older vendored relay forwards no lineage at all
    ],
)
def test_parse_event_keeps_only_a_known_lineage_tag(tmp_path, extra, expected):
    """DW-507: the relay's lineage tag lands on the HookEvent only when it is one
    of LINEAGE_TAGS, so attribution never keys on a value it does not know.

    Ablation: drop the `in LINEAGE_TAGS` filter and the "MATCH"/"maybe" rows fail."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "Stop", **extra)
    (event,) = watcher.poll()
    assert event.lineage == expected


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"payload_error": "JSONDecodeError"}, "JSONDecodeError"),
        ({"payload_error": "not-an-object"}, "not-an-object"),
        ({"payload_error": 1}, None),  # a non-string is dropped, not coerced
        ({"payload_error": ["JSONDecodeError"]}, None),
        ({}, None),  # a readable payload (or an older relay) carries no tag
    ],
)
def test_parse_event_keeps_string_payload_error_only(tmp_path, extra, expected):
    """The relay's unreadable-payload tag lands on the HookEvent (str only), so
    the wait loop's crumb never journals a value the relay did not write.

    Ablation: drop the `isinstance(payload_error, str)` check and the non-string
    rows fail."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "Stop", **extra)
    (event,) = watcher.poll()
    assert event.payload_error == expected


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"fully_idle": True}, True),
        ({"fully_idle": False}, False),
        ({"fully_idle": "false"}, None),  # a non-boolean is dropped, not coerced
        ({"fully_idle": 0}, None),  # 0 is falsy, but not False: still dropped
        ({}, None),  # every CLI but agy (or an older relay) carries no key
    ],
)
def test_parse_event_keeps_boolean_fully_idle_only(tmp_path, extra, expected):
    """DW-548: agy's forwarded `fullyIdle` lands on the HookEvent only as a bool,
    so the wait loop's `is False` yield test never matches a stray value.

    Ablation: drop the `isinstance(fully_idle, bool)` check and the "false"/0
    rows fail."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "Stop", **extra)
    (event,) = watcher.poll()
    assert event.fully_idle is expected


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (
            {
                "stop_error": "billing_error",
                "stop_error_details": "402 Payment Required",
                "stop_message": "Credit balance is too low",
            },
            ("billing_error", "402 Payment Required", "Credit balance is too low"),
        ),
        ({"stop_error": "server_error"}, ("server_error", None, None)),
        # a non-string is dropped, not coerced
        (
            {"stop_error": 3, "stop_error_details": ["429"], "stop_message": {"m": 1}},
            (None, None, None),
        ),
        ({}, (None, None, None)),  # every other event, or an older relay
    ],
)
def test_parse_event_reads_the_stop_failure_fields(tmp_path, extra, expected):
    """#865: the relay's forwarded StopFailure fields land on the HookEvent only
    as strings, so the profile's `stop_failure_types` lookup never keys on a
    stray value.

    Ablation: drop any of the three `isinstance(..., str)` gates and the
    non-string row fails."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "StopFailure", **extra)
    (event,) = watcher.poll()
    assert (event.stop_error, event.stop_error_details, event.stop_message) == expected


@pytest.mark.parametrize("value", [["A", "B"], {"id": "A"}, 3], ids=["list", "dict", "int"])
def test_parse_event_drops_a_non_string_session_id(tmp_path, value):
    """#767: a non-string id reads as absent, so attribution never hashes it —
    a list-valued child start used to raise TypeError out of the wait."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "SessionStart", session_id="A")
    write_event(
        watcher.events_dir, 2, "t1", "SessionStart", session_id=value, transcript_path=value
    )
    events = watcher.poll()
    assert [(e.session_id, e.transcript_path) for e in events] == [("A", None), (None, None)]
    attribution = SessionAttribution()
    assert [attribution.admit(e) for e in events] == [True, True]


def test_poll_returns_new_events_once(tmp_path):
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 2, "t1", "Stop")
    write_event(watcher.events_dir, 1, "t1", "SessionStart")

    events = watcher.poll()
    assert [e.event for e in events] == ["SessionStart", "Stop"]  # sorted by ts
    assert watcher.poll() == []  # consumed


def test_poll_skips_malformed(tmp_path):
    watcher = SignalWatcher(tmp_path / "events")
    (watcher.events_dir / "bad.json").write_text("{nope")
    (watcher.events_dir / "ignored.tmp").write_text("{}")
    (watcher.events_dir / "incomplete.json").write_text(json.dumps({"event": "Stop"}))
    assert watcher.poll() == []


def test_wait_for_filters_task_and_kind(tmp_path):
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "other-task", "Stop")
    write_event(watcher.events_dir, 2, "t1", "PreCompact")
    write_event(watcher.events_dir, 3, "t1", "Stop", session_id="s-123")

    event = watcher.wait_for("t1", {"Stop", "SessionEnd"}, timeout_s=5)
    assert event is not None and event.event == "Stop" and event.session_id == "s-123"


def test_wait_for_buffers_batched_events(tmp_path):
    """SessionStart and Stop landing in one poll must BOTH be deliverable —
    regression test for events lost when several arrive between polls."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "SessionStart")
    write_event(watcher.events_dir, 2, "t1", "Stop")

    kinds = {"SessionStart", "Stop", "SessionEnd"}
    first = watcher.wait_for("t1", kinds, timeout_s=1)
    second = watcher.wait_for("t1", kinds, timeout_s=1)
    assert (first.event, second.event) == ("SessionStart", "Stop")


def test_wait_for_ignores_events_before_since_ns(tmp_path):
    """A re-armed run reuses the task_id; a fresh watcher must not replay the
    previous cycle's Stop (which would read a stale result.json)."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 100, "t1", "Stop", session_id="old")  # prior cycle
    write_event(watcher.events_dir, 200, "t1", "Stop", session_id="new")  # this launch

    event = watcher.wait_for("t1", {"Stop"}, timeout_s=1, since_ns=150)
    assert event is not None and event.session_id == "new"


def test_wait_for_since_ns_times_out_when_only_stale(tmp_path):
    """When the only matching event predates the floor, wait_for must not return
    it — the session is still running, so this is a timeout."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 100, "t1", "Stop", session_id="old")
    now = {"t": 0.0}

    def clock():
        return now["t"]

    def sleep(seconds):
        now["t"] += seconds

    out = watcher.wait_for("t1", {"Stop"}, timeout_s=5, clock=clock, sleep=sleep, since_ns=150)
    assert out is None


def test_wait_for_timeout_with_fake_clock(tmp_path):
    watcher = SignalWatcher(tmp_path / "events")
    now = {"t": 0.0}

    def clock():
        return now["t"]

    def sleep(seconds):
        now["t"] += seconds

    assert watcher.wait_for("t1", {"Stop"}, timeout_s=10, clock=clock, sleep=sleep) is None
    assert now["t"] >= 10


# ------------------------------------------------------ dual poll (#494 skew guard)


def test_poll_sees_an_event_written_only_to_the_legacy_dir(tmp_path):
    """THE version-skew case, and the reason the legacy dir is polled at all.

    The relay a target project runs is a COPY taken at init time, so an upgraded
    orchestrator routinely drives sessions whose hook knows only the pre-#494
    in-tree `<run_dir>/events`. Nothing in the new location, everything in the old
    one, and the Stop must still be observed — the alternative is that EVERY
    session under such a project stalls to `session_timeout_min`.

    Ablation guard: drop `legacy_dir` from `_dirs()` and this fails."""
    watcher = SignalWatcher(tmp_path / "state" / "events", tmp_path / "run" / "events")
    write_event(tmp_path / "run" / "events", 1, "t1", "Stop", session_id="legacy")

    event = watcher.wait_for("t1", {"Stop"}, timeout_s=1)
    assert event is not None and event.session_id == "legacy"


def test_poll_orders_both_dirs_by_ts(tmp_path):
    """Ordering is by the event's own `ts`, not by which directory it came from —
    a run mid-upgrade could take a SessionStart from one relay and a Stop from
    another, and `wait_for`'s buffering hands them out in poll order."""
    primary = tmp_path / "state" / "events"
    legacy = tmp_path / "run" / "events"
    watcher = SignalWatcher(primary, legacy)
    write_event(legacy, 3, "t1", "Stop")
    write_event(primary, 2, "t1", "PreCompact")
    write_event(legacy, 1, "t1", "SessionStart")

    assert [e.event for e in watcher.poll()] == ["SessionStart", "PreCompact", "Stop"]


def test_poll_tolerates_a_missing_legacy_dir(tmp_path):
    """The ordinary case once every relay is current: nothing ever creates the
    in-tree dir, so it simply is not there. That must not raise — and must not be
    papered over by creating it either (see the next test)."""
    primary = tmp_path / "state" / "events"
    watcher = SignalWatcher(primary, tmp_path / "run" / "events")
    write_event(primary, 1, "t1", "Stop", session_id="s1")

    assert [e.session_id for e in watcher.poll()] == ["s1"]


def test_only_the_primary_dir_is_created(tmp_path):
    """The whole point of #494 is that the run's control plane stops living in the
    project tree. An orchestrator that re-created `<run_dir>/events` to poll it
    would put the directory back in the operator's `git status` for nothing: a
    legacy relay makes it itself, and a current one never writes there."""
    legacy = tmp_path / "run" / "events"
    SignalWatcher(tmp_path / "state" / "events", legacy)

    assert (tmp_path / "state" / "events").is_dir()
    assert not legacy.exists()


def test_the_same_file_name_in_both_dirs_yields_both_events(tmp_path):
    """`_consumed` is keyed by (dir, name), so consuming a name from one directory
    cannot mask a different event of that name in the other. The names collide on
    (ts, task_id, event), which two independent relays can produce; a masked event
    here would be a lost Stop.

    Ablation guard: key `_consumed` on `entry.name` alone and this fails."""
    primary = tmp_path / "state" / "events"
    legacy = tmp_path / "run" / "events"
    watcher = SignalWatcher(primary, legacy)
    write_event(primary, 1, "t1", "Stop", session_id="from-primary")
    write_event(legacy, 1, "t1", "Stop", session_id="from-legacy")

    assert sorted(e.session_id for e in watcher.poll()) == ["from-legacy", "from-primary"]
    assert watcher.poll() == []  # both consumed


def test_poll_still_raises_when_the_primary_dir_is_gone(tmp_path):
    """The legacy dir's absence is expected; the primary's is not — this watcher
    created it, so something removed a live run's control plane out from under it.
    Unchanged behavior, pinned here so the tolerance added for the legacy dir is
    not quietly widened to both."""
    primary = tmp_path / "state" / "events"
    watcher = SignalWatcher(primary, tmp_path / "run" / "events")
    primary.rmdir()

    with pytest.raises(OSError):
        watcher.poll()


def test_session_events_keeps_only_this_attempt_across_both_channels(tmp_path):
    """The #752 diagnostic's read: one attempt's events from the primary AND legacy
    channels, oldest first, excluding another attempt's id and anything stamped
    before this attempt's launch floor (a resumed run's same-id leftovers).

    Ablation guard: drop the task-id or the floor term from `is_session_event`, or
    the legacy dir from the scan, and this fails."""
    primary, legacy = tmp_path / "state" / "events", tmp_path / "run" / "events"
    write_event(primary, 50, "t-2", "SessionStart")
    write_event(legacy, 60, "t-2", "Stop")
    write_event(primary, 55, "t-1", "Stop")  # another attempt, inside the window
    write_event(legacy, 5, "t-2", "Stop")  # this id, but before the launch floor

    events = session_events(primary, legacy, "t-2", since_ns=10)

    assert [(e.ts, e.event) for e in events] == [(50, "SessionStart"), (60, "Stop")]


def test_session_events_is_read_only_and_tolerates_missing_dirs(tmp_path):
    """A post-mortem read neither creates a channel nor consumes from one: a relay
    that never fired may have left no directory at all, and a live watcher must
    still see every event the diagnostic looked at."""
    primary, legacy = tmp_path / "state" / "events", tmp_path / "run" / "events"
    assert session_events(primary, legacy, "t1") == []
    assert not primary.exists() and not legacy.exists()

    watcher = SignalWatcher(primary, legacy)
    write_event(primary, 1, "t1", "Stop")
    assert len(session_events(primary, legacy, "t1")) == 1
    assert watcher.wait_for("t1", {"Stop"}, timeout_s=1) is not None


def test_session_events_raises_on_an_unreadable_channel(tmp_path):
    """Only absence reads as empty; any other fault surfaces for the caller to
    report as unreadable rather than pass as "no events"."""
    primary = tmp_path / "events"
    primary.write_text("not a directory")

    with pytest.raises(OSError):
        session_events(primary, None, "t1")


def test_is_session_event_is_the_rule_wait_for_matches_on(tmp_path):
    """One correlation rule, shared by the completion wait and the diagnostic."""
    write_event(tmp_path, 7, "t1", "Stop")
    (event,) = SignalWatcher(tmp_path).poll()

    assert is_session_event(event, "t1")
    assert is_session_event(event, "t1", since_ns=7)
    assert not is_session_event(event, "t1", since_ns=8)
    assert not is_session_event(event, "t2")


def _event(kind, session_id=None, ts=1, source=None, lineage=None):
    return HookEvent(
        ts=ts,
        event=kind,
        task_id="t1",
        session_id=session_id,
        transcript_path=None,
        path=Path("x"),
        source=source,
        lineage=lineage,
    )


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        pytest.param(
            [("SessionStart", "A"), ("Stop", "A")], [True, True], id="first-identified-start-binds"
        ),
        pytest.param(
            [("SessionStart", "A"), ("SessionStart", "A"), ("Stop", "A")],
            [True, True, True],
            id="same-id-restart-admitted",
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B"),
                ("Stop", "B"),
                ("SessionEnd", "B"),
                ("Stop", "A"),
            ],
            [True, False, False, False, True],
            id="announced-child-is-foreign",
        ),
        pytest.param(
            [("SessionStart", None), ("SessionStart", "B"), ("Stop", "B"), ("Stop", "A")],
            [True, False, False, True],
            id="anonymous-first-start-uses-the-parent-slot",
        ),
        pytest.param(
            [("SessionStart", "A"), ("Stop", "B"), ("SessionEnd", "B")],
            [True, True, True],
            id="unannounced-rotated-id-admitted",  # M1: /clear or compaction
        ),
        pytest.param(
            [("SessionEnd", "A")], [True], id="identified-end-before-any-start-admitted"
        ),  # B2: the #727 trust-dialog exit
        pytest.param(
            [("SessionStart", "A"), ("SessionStart", None), ("Stop", None), ("SessionEnd", None)],
            [True, True, True, True],
            id="id-less-events-always-admitted",
        ),
        pytest.param(
            [("SessionStart", "main"), ("Stop", "toolu_bdrk_x"), ("Stop", "main")],
            [True, True, True],
            id="never-announced-toolu-stop-admitted",  # the copilot subagent filter owns it
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B", "clear"),
                ("Stop", "B"),
                ("SessionStart", "C", "startup"),
                ("Stop", "C"),
                ("Stop", "B"),
            ],
            [True, True, True, False, False, True],
            id="clear-start-rebinds-then-startup-child-is-foreign",
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B", "compact"),
                ("Stop", "B"),
                ("SessionStart", "C", "startup"),
                ("Stop", "C"),
                ("Stop", "B"),
            ],
            [True, True, True, False, False, True],
            id="compact-start-rebinds-then-startup-child-is-foreign",
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B", "startup"),
                ("SessionStart", "B", "compact"),
                ("Stop", "B"),
                ("Stop", "A"),
            ],
            [True, False, False, False, True],
            id="foreign-id-compacting-stays-foreign",
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B", "startup"),
                ("SessionEnd", "B"),
                ("SessionStart", "C", "clear"),
                ("Stop", "C"),
                ("Stop", "A"),
            ],
            [True, False, False, False, False, True],
            id="clear-after-foreign-end-is-the-child-clearing",
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionEnd", "A"),
                ("SessionStart", "B", "clear"),
                ("Stop", "B"),
            ],
            [True, True, True, True],
            id="clear-after-own-end-rebinds",  # claude ends the old id before a clear start
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B", "startup"),
                ("SessionEnd", "A"),
                ("SessionEnd", "B"),
                ("SessionStart", "C", "clear"),
                ("Stop", "C"),
            ],
            [True, False, True, False, True, True],
            id="child-end-racing-the-parent-clear-still-rebinds",
        ),
        pytest.param(
            [("SessionStart", "A"), ("SessionStart", "B", "resume"), ("Stop", "B")],
            [True, False, False],
            id="resume-start-is-foreign",  # a nested child launched with --resume
        ),
        pytest.param(
            [("SessionStart", "A"), ("SessionStart", "B", None), ("Stop", "B")],
            [True, False, False],
            id="sourceless-start-is-foreign",  # an older relay: no rebind
        ),
    ],
)
def test_session_attribution_admits(sequence, expected):
    """#767: the deny-list rule, event by event. Only an id that announced its
    own SessionStart after the launched session's first one is dropped."""
    attribution = SessionAttribution()
    admitted = []
    for kind, sid, *source in sequence:  # an optional third item is the start's source
        admitted.append(attribution.admit(_event(kind, sid, source=source[0] if source else None)))
    assert admitted == expected


def test_session_attribution_clear_start_moves_the_binding():
    """#767: a clear/compact start with a new id is the launched session
    rotating its id, so the binding follows it rather than marking it foreign."""
    attribution = SessionAttribution()
    attribution.admit(_event("SessionStart", "A"))
    assert attribution.admit(_event("SessionStart", "B", source="clear"))
    assert attribution.bound_id == "B"
    assert attribution.foreign_ids == set()


def test_attribute_events_returns_admitted_and_foreign_ids():
    """The replay form the post-mortem diagnostic uses: admitted events in
    order, plus every id found foreign."""
    events = [
        _event("SessionStart", "A", ts=1),
        _event("SessionStart", "B", ts=2),
        _event("Stop", "B", ts=3),
        _event("SessionStart", "C", ts=4),
        _event("Stop", "A", ts=5),
    ]
    admitted, foreign = attribute_events(events)
    assert [(e.event, e.session_id) for e in admitted] == [("SessionStart", "A"), ("Stop", "A")]
    assert foreign == {"B", "C"}


# DW-505/508: attribution pinned to the id the adapter chose at launch. One test
# per row of the spec's I/O matrix; P is the pinned id.
P = "pinned-P"


def _replay(attribution, sequence):
    return [
        attribution.admit(_event(kind, sid, source=source[0] if source else None))
        for kind, sid, *source in sequence
    ]


def test_pinned_attribution_admits_the_own_start():
    attribution = SessionAttribution(pinned_id=P)
    assert attribution.bound_id == P  # the pin binds before any event
    assert _replay(attribution, [("SessionStart", P, "startup")]) == [True]
    assert attribution.bound_id == P
    assert attribution.foreign_ids == set()


def test_pinned_attribution_drops_an_unannounced_child_session_end():
    """DW-508: a child that never announced a SessionStart fires SessionEnd.
    Unpinned this reads as the parent's own end; pinned, X is foreign.

    Ablation guard: delete the pinned-only SessionEnd branch in `admit` and
    End(X) is admitted again."""
    attribution = SessionAttribution(pinned_id=P)
    assert _replay(attribution, [("SessionStart", P), ("SessionEnd", "X")]) == [True, False]
    assert attribution.foreign_ids == {"X"}
    assert attribution.foreign_ended and not attribution.bound_ended


def test_pinned_attribution_drops_an_unannounced_end_before_the_own_start():
    attribution = SessionAttribution(pinned_id=P)
    assert _replay(attribution, [("SessionEnd", "X")]) == [False]
    assert attribution.foreign_ids == {"X"}


def test_pinned_attribution_admits_the_own_pre_start_end():
    """#727 under pinning: the launched CLI exiting before its SessionStart
    fired still ends the session."""
    attribution = SessionAttribution(pinned_id=P)
    assert _replay(attribution, [("SessionEnd", P)]) == [True]
    assert attribution.bound_ended
    assert attribution.foreign_ids == set()


def test_pinned_attribution_drops_a_child_start_and_its_stop():
    attribution = SessionAttribution(pinned_id=P)
    sequence = [("SessionStart", P), ("SessionStart", "C", "startup"), ("Stop", "C")]
    assert _replay(attribution, sequence) == [True, False, False]
    assert attribution.foreign_ids == {"C"}


def test_pinned_attribution_follows_a_clear_rebind_and_admits_a_late_own_end():
    """The #767 rebind still works pinned, and every id ever bound stays the
    launched session's own: a late SessionEnd from the pre-clear id is admitted."""
    attribution = SessionAttribution(pinned_id=P)
    sequence = [
        ("SessionStart", P),
        ("SessionEnd", P),
        ("SessionStart", "N", "clear"),
        ("Stop", "N"),
        ("SessionEnd", P),  # late, from the rotated-away own id
    ]
    assert _replay(attribution, sequence) == [True] * 5
    assert attribution.bound_id == "N"
    assert attribution.foreign_ids == set()


def test_pinned_attribution_treats_a_clear_after_a_foreign_end_as_the_child():
    attribution = SessionAttribution(pinned_id=P)
    sequence = [("SessionStart", P), ("SessionEnd", "X"), ("SessionStart", "Y", "clear")]
    assert _replay(attribution, sequence) == [True, False, False]
    assert attribution.bound_id == P
    assert attribution.foreign_ids == {"X", "Y"}


def test_pinned_attribution_anonymous_start_does_not_unpin():
    attribution = SessionAttribution(pinned_id=P)
    assert _replay(attribution, [("SessionStart", None), ("SessionEnd", "X")]) == [True, False]
    assert attribution.bound_id == P
    assert attribution.foreign_ids == {"X"}


def test_pinned_attribution_a_first_start_from_another_id_is_foreign():
    """Pinned, the first SessionStart does not claim the parent slot: an id other
    than the pin announcing first is a child, and the pin still binds."""
    attribution = SessionAttribution(pinned_id=P)
    sequence = [("SessionStart", "C", "startup"), ("Stop", "C"), ("SessionStart", P)]
    assert _replay(attribution, sequence) == [False, False, True]
    assert attribution.bound_id == P
    assert attribution.foreign_ids == {"C"}


def test_pinned_attribution_follows_a_compact_rebind():
    attribution = SessionAttribution(pinned_id=P)
    sequence = [("SessionStart", P), ("SessionStart", "K", "compact"), ("Stop", "K")]
    assert _replay(attribution, sequence) == [True, True, True]
    assert attribution.bound_id == "K"
    assert attribution.foreign_ids == set()


def test_pinned_attribution_a_resume_start_from_a_new_id_is_foreign():
    """A nested child launched with --resume stays foreign under a pin."""
    attribution = SessionAttribution(pinned_id=P)
    sequence = [("SessionStart", P), ("SessionStart", "R", "resume"), ("Stop", "R")]
    assert _replay(attribution, sequence) == [True, False, False]
    assert attribution.bound_id == P
    assert attribution.foreign_ids == {"R"}


def test_pinned_attribution_a_second_clear_rebinds_and_keeps_every_own_id():
    """Two clears in a row: the binding follows each, and a late SessionEnd from
    either earlier own id (the pin, the first rebind) is still admitted."""
    attribution = SessionAttribution(pinned_id=P)
    sequence = [
        ("SessionStart", P),
        ("SessionEnd", P),
        ("SessionStart", "N", "clear"),
        ("SessionEnd", "N"),
        ("SessionStart", "M", "clear"),
        ("Stop", "M"),
        ("SessionEnd", P),  # late
        ("SessionEnd", "N"),  # late
    ]
    assert _replay(attribution, sequence) == [True] * len(sequence)
    assert attribution.bound_id == "M"
    assert attribution.foreign_ids == set()


def test_pinned_attribution_admits_id_less_events():
    attribution = SessionAttribution(pinned_id=P)
    assert _replay(attribution, [("SessionEnd", None), ("Stop", None)]) == [True, True]
    assert attribution.foreign_ids == set()


def test_pinned_attribution_admits_an_unannounced_stop():
    """Only SessionStart/SessionEnd go foreign on the pin: a never-announced id's
    Stop (a Copilot toolu_ subagent Stop) is still admitted."""
    attribution = SessionAttribution(pinned_id=P)
    assert _replay(attribution, [("SessionStart", P), ("Stop", "toolu_1")]) == [True, True]
    assert attribution.foreign_ids == set()


@pytest.mark.parametrize(
    "sequence",
    [
        pytest.param([("SessionEnd", "X")], id="unannounced-end-first"),
        pytest.param(
            [("SessionStart", "A"), ("SessionEnd", "X")], id="unannounced-end-after-start"
        ),
    ],
)
def test_unpinned_attribution_still_admits_an_unannounced_end(sequence):
    """Regression: without a pin the accepted limitation stands byte-for-byte."""
    attribution = SessionAttribution()
    assert _replay(attribution, sequence) == [True] * len(sequence)
    assert attribution.foreign_ids == set()
    assert not attribution.foreign_ended


# DW-507: relay-side lineage, calibrated on the first SessionStart. One test per
# row of the spec's Attribution matrix.


def _replay_tagged(attribution, sequence):
    """(kind, sid, source, lineage) tuples through `admit`."""
    return [
        attribution.admit(_event(kind, sid, source=source, lineage=lineage))
        for kind, sid, source, lineage in sequence
    ]


def test_lineage_match_first_start_trusts_and_drops_a_mismatch_child():
    """A `match` first start trusts lineage: a later `mismatch` event is foreign,
    identified (its id joins foreign_ids, its SessionEnd sets foreign_ended) or
    id-less."""
    sequence = [
        ("SessionStart", "A", "startup", "match"),
        ("Stop", "C", None, "mismatch"),
        ("Stop", None, None, "mismatch"),
        ("SessionEnd", "C", None, "mismatch"),
        ("Stop", "A", None, "match"),
    ]
    attribution = SessionAttribution()
    assert _replay_tagged(attribution, sequence) == [True, False, False, False, True]
    assert attribution.lineage_state == "trusted"
    assert attribution.foreign_ids == {"C"}
    assert attribution.foreign_ended and not attribution.bound_ended


@pytest.mark.parametrize(
    ("trusted_tag", "expected_admits", "expected_bound"),
    [
        pytest.param("match", [True, False, False, True], "A", id="trusted-drops-the-rotation"),
        pytest.param("unknown", [True, True, True, True], "B", id="unavailable-rebinds"),
        pytest.param(None, [True, True, True, True], "B", id="untagged-rebinds"),
    ],
)
def test_lineage_closes_the_child_clear_rotation_only_when_trusted(
    trusted_tag, expected_admits, expected_bound
):
    """The gap #767 left open: a nested child rotating its id with a `clear` start
    and no preceding SessionEnd rebinds on `source` alone. Under a trusted
    lineage the child's `mismatch` tag makes it foreign; with lineage
    unavailable (Windows, macOS, an older relay) the #767 rule stands and it
    rebinds exactly as before.

    Ablation: delete the trusted-mismatch check at the top of `admit` and the
    trusted row rebinds to B like the others."""
    attribution = SessionAttribution()
    sequence = [
        ("SessionStart", "A", "startup", trusted_tag),
        ("SessionStart", "B", "clear", "mismatch"),
        ("Stop", "B", None, "mismatch"),
        ("Stop", "A", None, trusted_tag),
    ]
    assert _replay_tagged(attribution, sequence) == expected_admits
    assert attribution.bound_id == expected_bound


def test_lineage_mismatch_first_start_is_miscalibrated_and_ignored():
    """A `mismatch` first start means this CLI's hook architecture defeats the
    relay heuristic: lineage is ignored for the attempt, so a later `mismatch`
    Stop (id-less or from an unannounced id) is admitted as before."""
    attribution = SessionAttribution()
    sequence = [
        ("SessionStart", "A", "startup", "mismatch"),
        ("Stop", None, None, "mismatch"),
        ("Stop", "A", None, "mismatch"),
    ]
    assert _replay_tagged(attribution, sequence) == [True, True, True]
    assert attribution.lineage_state == "miscalibrated"
    assert attribution.foreign_ids == set()


@pytest.mark.parametrize("tag", ["unknown", None], ids=["unknown", "untagged"])
def test_lineage_unknown_or_untagged_first_start_is_unavailable(tag):
    attribution = SessionAttribution()
    sequence = [("SessionStart", "A", "startup", tag), ("Stop", None, None, "mismatch")]
    assert _replay_tagged(attribution, sequence) == [True, True]
    assert attribution.lineage_state == "unavailable"


def test_trusted_lineage_admits_a_mismatch_event_carrying_an_own_id():
    """Fail toward acceptance: an event carrying one of the launched session's own
    ids is never made foreign by lineage — it goes through the normal rules."""
    attribution = SessionAttribution()
    sequence = [
        ("SessionStart", "A", "startup", "match"),
        ("SessionEnd", "A", None, "match"),
        ("SessionStart", "B", "clear", "match"),  # the parent's own rotation
        ("Stop", "B", None, "mismatch"),
        ("SessionEnd", "A", None, "mismatch"),  # late, from the rotated-away own id
    ]
    assert _replay_tagged(attribution, sequence) == [True] * 5
    assert attribution.bound_id == "B"
    assert attribution.foreign_ids == set()


def test_lineage_is_ignored_before_the_first_start():
    """The #727 path is unchanged: before the first SessionStart nothing is
    calibrated, so a `mismatch` pre-start SessionEnd is still the parent's."""
    attribution = SessionAttribution()
    assert _replay_tagged(attribution, [("SessionEnd", "A", None, "mismatch")]) == [True]
    assert attribution.lineage_state is None
    assert attribution.bound_ended is False  # nothing bound yet; admitted, not recorded


def test_trusted_mismatch_leaves_the_ended_flags_alone():
    """A trusted-mismatch SessionStart is dropped without resetting the `*_ended`
    evidence the normal SessionStart path clears: the parent's own end still
    vouches for its next clear start."""
    attribution = SessionAttribution()
    sequence = [
        ("SessionStart", "A", "startup", "match"),
        ("SessionEnd", "A", None, "match"),
        ("SessionStart", "C", "startup", "mismatch"),
    ]
    assert _replay_tagged(attribution, sequence) == [True, True, False]
    assert attribution.bound_ended is True
    assert attribution.foreign_ids == {"C"}


def test_pinned_and_trusted_lineage_drops_an_unannounced_mismatch_stop():
    """Pinned + trusted: an unannounced id's Stop — admitted by the pin rules alone
    (the Copilot toolu_ allowance) — is foreign once it is tagged `mismatch`,
    while the pin's own events pass whatever their tag."""
    attribution = SessionAttribution(pinned_id=P)
    sequence = [
        ("SessionStart", P, "startup", "match"),
        ("Stop", "X", None, "mismatch"),
        ("Stop", "toolu_1", None, "match"),
        ("Stop", P, None, "mismatch"),
    ]
    assert _replay_tagged(attribution, sequence) == [True, False, True, True]
    assert attribution.lineage_state == "trusted"
    assert attribution.foreign_ids == {"X"}


def test_pinned_lineage_calibrates_on_the_pinned_start_not_a_child_that_won_the_race():
    """Pinned, a child's start can reach the events dir before the parent's (a
    child launched by a parallel project SessionStart hook). The pin already names
    that start foreign, so its `mismatch` tag must not calibrate lineage: the
    parent's own `match` start does, and the child's later id-less `mismatch` Stop
    is dropped instead of completing the parent.

    Ablation: calibrate on the first SessionStart unconditionally and the state
    is "miscalibrated", so the id-less Stop is admitted."""
    attribution = SessionAttribution(pinned_id=P)
    sequence = [
        ("SessionStart", "C", "startup", "mismatch"),
        ("SessionStart", P, "startup", "match"),
        ("Stop", None, None, "mismatch"),
        ("Stop", P, None, "match"),
    ]
    assert _replay_tagged(attribution, sequence) == [False, True, False, True]
    assert attribution.lineage_state == "trusted"
    assert attribution.foreign_ids == {"C"}


def test_pinned_lineage_stays_uncalibrated_while_only_foreign_starts_arrive():
    """No start the pin admits, no calibration: lineage is ignored (fails toward
    acceptance) and the id-less event passes."""
    attribution = SessionAttribution(pinned_id=P)
    sequence = [
        ("SessionStart", "C", "startup", "match"),
        ("Stop", None, None, "mismatch"),
    ]
    assert _replay_tagged(attribution, sequence) == [False, True]
    assert attribution.lineage_state is None


def test_pinned_lineage_skips_an_anonymous_start_and_calibrates_on_the_pinned_one():
    """Pinned, an anonymous start is admitted (it may be the parent's) but proves
    nothing: a child's id-less `mismatch` start that wins the race must not
    calibrate lineage, so the parent's identified `match` start trusts it and the
    child's later id-less `mismatch` Stop is dropped.

    Ablation: calibrate on any admitted start and the state is "miscalibrated",
    so the id-less Stop is admitted."""
    attribution = SessionAttribution(pinned_id=P)
    sequence = [
        ("SessionStart", None, "startup", "mismatch"),
        ("SessionStart", P, "startup", "match"),
        ("Stop", None, None, "mismatch"),
        ("Stop", P, None, "match"),
    ]
    assert _replay_tagged(attribution, sequence) == [True, True, False, True]
    assert attribution.lineage_state == "trusted"


def test_unpinned_lineage_still_calibrates_on_an_anonymous_first_start():
    """Unpinned, the first start takes the parent's slot identified or not, and
    calibrates lineage exactly as before."""
    attribution = SessionAttribution()
    assert _replay_tagged(attribution, [("SessionStart", None, "startup", "match")]) == [True]
    assert attribution.lineage_state == "trusted"
