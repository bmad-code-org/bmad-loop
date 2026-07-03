import json

from bmad_loop.signals import SignalWatcher


def write_event(events_dir, ts, task_id, event, **extra):
    payload = {"ts": ts, "event": event, "task_id": task_id, **extra}
    (events_dir / f"{ts}-{task_id}-{event}.json").write_text(json.dumps(payload))


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
