"""Coarse Pilot smoke tests for the dashboard and run control. Fine-grained
data correctness lives in test_tui_data.py, exact launch argv in
test_tui_launch.py; here we only prove the wiring: app mounts, the run table
populates and auto-selects the newest run, selection switches the task table,
the journal pane picks up appended events on a poll, and the r/s/e/a/v
bindings drive modals into tui.launch calls (monkeypatched — no real tmux)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest
from conftest import (
    BMAD_CONFIG_REL,
    READABLE_LEDGER,
    UNDECODABLE_LEDGER,
    assert_run_state_lock_held,
    git,
    install_bmad_config,
    make_validate_document,
    nested_repo_root_paths,
    refuse_to_resolve,
    write_sprint,
)
from rich.console import Console
from rich.text import Text
from textual.events import MouseMove
from textual.geometry import Offset, Region, Size
from textual.selection import Selection
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    OptionList,
    RichLog,
    Select,
    Static,
    TabbedContent,
)

from bmad_loop import bmadconfig, documents, platform_util
from bmad_loop import policy as policy_mod
from bmad_loop import runs as runs_mod
from bmad_loop import verify
from bmad_loop.adapters.multiplexer import MultiplexerError, TerminalMultiplexer
from bmad_loop.journal import UNREADABLE_LINE_KIND, Journal, save_state
from bmad_loop.model import (
    PAUSE_ENVIRONMENT,
    Phase,
    RunState,
    SessionRecord,
    StoryTask,
    TokenUsage,
)
from bmad_loop.runs import RUNS_DIR
from bmad_loop.tui import data, launch, widgets
from bmad_loop.tui.app import BmadLoopApp
from bmad_loop.tui.screens.dashboard import (
    _MIN_DETAIL,
    _MIN_SIDEBAR,
    DashboardScreen,
    _Snapshot,
)
from bmad_loop.tui.screens.modals import (
    ConfirmModal,
    ConfirmResumeModal,
    DecisionModal,
    DeferredEntryModal,
    EscalationModal,
    PauseReasonModal,
    SpecReviewModal,
    StartRunModal,
    StartSweepModal,
    StoryCheckpointModal,
    TextOutputModal,
    ValidateFindingsModal,
)
from bmad_loop.tui.widgets import (
    _FINDING_CHECK_WIDTH,
    _FINDING_COL_PAD,
    _FINDING_GLYPH_WIDTH,
    _JOURNAL_CLOCK_WIDTH,
    _JOURNAL_COL_PAD,
    _JOURNAL_KIND_WIDTH,
    RunHeader,
    SelectableRichLog,
    Splitter,
    SprintTree,
    StoriesTable,
    agent_label,
    journal_line,
    pause_label,
    pause_tag,
    sprint_story_label,
    story_checkpoint_cell,
    story_state_cell,
)


def _rearm_outcome(key: str, *entries: dict) -> runs_mod.RearmOutcome:
    notices = tuple(
        runs_mod.RearmNotice(*notice)
        for entry in entries
        if (notice := runs_mod.rearm_event_notice(entry)) is not None
    )
    # Mirrors `runs._RearmJournal.append`, first-wins included: the surface under test
    # renders the held record's own `next_step`, so a helper that dropped it would let
    # the hold toast pass on a step no record produced.
    held = next(
        (
            notice
            for entry in entries
            if runs_mod.rearm_holds_the_resume(entry)
            and (notice := runs_mod.rearm_event_notice(entry)) is not None
        ),
        None,
    )
    return runs_mod.RearmOutcome(
        key,
        notices,
        any(runs_mod.rearm_holds_the_resume(entry) for entry in entries),
        held[2] if held is not None else "",
    )


def _journal_rearm_outcome(run_dir: Path, key: str) -> runs_mod.RearmOutcome:
    return _rearm_outcome(key, *Journal(run_dir).entries())


def make_run(
    root: Path,
    run_id: str,
    *,
    finished: bool = False,
    run_type: str = "story",
    alive: bool = False,
    tasks: dict[str, StoryTask] | None = None,
    paused_stage: str | None = None,
    paused_reason: str | None = None,
    paused_story_key: str | None = None,
    crashed: bool = False,
    crash_error: str | None = None,
    policy_snapshot: dict | None = None,
    source: str = "sprint-status",
    spec_folder: str = "",
) -> Path:
    run_dir = root / RUNS_DIR / run_id
    state = RunState(
        run_id=run_id,
        project=str(root),
        started_at="2026-06-11T10:00:00",
        run_type=run_type,
        finished=finished,
        tasks=tasks or {},
        paused_stage=paused_stage,
        paused_reason=paused_reason,
        paused_story_key=paused_story_key,
        crashed=crashed,
        crash_error=crash_error,
        policy_snapshot=policy_snapshot or {},
        source=source,
        spec_folder=spec_folder,
    )
    save_state(run_dir, state)
    if alive:
        (run_dir / "engine.pid").write_text(str(os.getpid()), encoding="utf-8")
    return run_dir


@dataclasses.dataclass(frozen=True)
class Emission:
    """One `App.notify` call, as the app made it: text, severity, and the
    monotonic instant it was made."""

    message: str
    severity: str
    at: float


_EMITTED = "_test_emitted_notifications"
_WORKERS = "_test_started_workers"
# The harness's own clock, a module attribute so a test of the deadline can swap
# it (scoped to this module) without touching the process-wide `time` module.
_monotonic = time.monotonic
_TRACE: list[str] = []


def trace(event: str) -> None:
    """Note a test-observed event for the next failed wait's diagnostic."""
    _TRACE.append(f"{_monotonic():.3f} [{threading.current_thread().name}] {event}")


def emitted(app) -> list[Emission]:
    """Every notification the app emitted, in order, expired ones included."""
    return app.__dict__.setdefault(_EMITTED, [])


@pytest.fixture(autouse=True)
def _observe_tui(monkeypatch):
    """Record every `notify` and every app-level worker as it happens.

    Textual's own store is not a history: `App._notifications` reaps each entry
    once its five-second lifetime passes, on every read. A wait that reads it
    therefore loses a toast the app did emit whenever the runner is slow enough,
    and cannot say whether the toast was never emitted or merely expired. The
    spy forwards to the real `notify`, so rendering is unchanged; it only keeps
    what was said. Emission is not rendering: rows that pin what the operator
    sees read `rendered_toasts` too.

    Textual drops a worker from `app.workers` the moment it finishes, so the
    worker record is also kept here: a failed wait can then name a lifecycle
    worker that errored, was cancelled or never started."""
    _TRACE.clear()
    real_notify = BmadLoopApp.notify
    real_run_worker = BmadLoopApp.run_worker

    def notify(self, message, *, severity="information", **kwargs):
        emitted(self).append(Emission(str(message), severity, _monotonic()))
        trace(f"notify[{severity}] {message}")
        return real_notify(self, message, severity=severity, **kwargs)

    def run_worker(self, *args, **kwargs):
        worker = real_run_worker(self, *args, **kwargs)
        self.__dict__.setdefault(_WORKERS, []).append(worker)
        trace(f"worker started {worker.group}/{worker.name}")
        return worker

    monkeypatch.setattr(BmadLoopApp, "notify", notify)
    monkeypatch.setattr(BmadLoopApp, "run_worker", run_worker)
    yield
    _TRACE.clear()


def notifications(app: BmadLoopApp) -> list[str]:
    return [e.message for e in emitted(app)]


def notifications_with_severity(app: BmadLoopApp) -> list[tuple[str, str]]:
    """`notifications()` discards severity, so a refusal that softened from `error`
    to an information toast still matches on text alone. Rows that are ABOUT a
    refusal read this instead."""
    return [(e.message, e.severity) for e in emitted(app)]


def rendered_toasts(app: BmadLoopApp) -> list[tuple[str, str]]:
    """The toasts on screen now, as (text, severity), severity read from the
    widget's own `-<severity>` class. This is what the operator sees; it expires
    with the toast, so read it only right after the emission it renders.

    Only an app run under `run_test(notifications=True)` mounts a toast rack:
    Textual's test default is `False`, which is why every other row reads the
    emission record instead."""
    shown = []
    for toast in app.screen.query("Toast"):
        severity = next(
            (c[1:] for c in toast.classes if c in ("-information", "-warning", "-error")), ""
        )
        shown.append((str(toast.render()), severity))
    return shown


def ui_state(app) -> str:
    """What a failed wait reports: the screen, focus, workers, what the app
    emitted and what the test observed, so a missed click, a dead worker and a
    missing toast read differently."""
    try:
        screen = type(app.screen).__name__
    except Exception as e:  # a panicked app has no screen stack left to name
        screen = f"<unavailable: {e!r}>"
    started = [
        f"{w.group}/{w.name}={w.state.name}" + (f" error={w.error!r}" if w.error else "")
        for w in app.__dict__.get(_WORKERS, [])
    ]
    live = sorted(f"{w.group}/{w.name}={w.state.name}" for w in app.workers)
    lines = [
        f"screen={screen} focused={app.focused!r}",
        f"app workers started: {started or 'none'}",
        f"workers live now: {live or 'none'}",
        f"emitted: {notifications_with_severity(app) or 'nothing'}",
        f"app exception: {app._exception!r}",
        "trace:",
        *(f"  {line}" for line in _TRACE[-40:]),
    ]
    return "\n".join(lines)


def _describe(condition, what: str | None) -> str:
    if what is not None:
        return what
    code = getattr(condition, "__code__", None)
    if code is None:
        return repr(condition)
    return f"{condition.__qualname__} ({Path(code.co_filename).name}:{code.co_firstlineno})"


_STEP = 0.05
# What one step may take past the deadline before it is called a stall rather
# than an ordinary last step: `pause` drains the queue before it sleeps.
_STEP_GRACE = 1.0


async def _step(pilot, deadline: float, what: str) -> None:
    """One `pilot.pause`, bounded by the caller's deadline.

    `pause` first waits for every widget to drain its queued messages, with
    Textual's own 30-second bound, and only then sleeps the requested step, so
    a busy UI makes one step take far longer than the step. Bounding it by the
    deadline (plus one step's grace) keeps a stalled UI a failure of this wait,
    reported as such, instead of Textual's 30-second one."""
    try:
        await asyncio.wait_for(
            pilot.pause(_STEP), timeout=max(deadline - _monotonic(), 0.0) + _STEP_GRACE
        )
    except TimeoutError:
        raise AssertionError(
            f"UI did not drain its messages before the deadline, waiting for: {what}\n"
            + ui_state(pilot.app)
        ) from None


async def until(pilot, condition, timeout: float = 10.0, *, what: str | None = None) -> None:
    """Wait for a predicate across thread-worker polls and their callbacks.

    The dashboard polls on a 1.0s interval and each tick hops through a thread
    worker and a UI callback, so several sequential waits can each need a few
    ticks; the timeout is generous and returns the instant the predicate holds.
    A pending log jump survives skipped/starved ticks (each tick's _apply
    re-attempts it until it lands), so waiting on its effect is deterministic —
    no rerun markers needed on the journal-jump tests.

    The timeout is real elapsed time on a monotonic clock. Counting requested
    sleeps instead let a slow runner wait several times longer than it said,
    and a timeout said only "condition not met"; this one names the condition
    and the UI state (`ui_state`)."""
    deadline = _monotonic() + timeout
    while not condition():
        if _monotonic() >= deadline:
            raise AssertionError(
                f"not met within {timeout}s: {_describe(condition, what)}\n" + ui_state(pilot.app)
            )
        await _step(pilot, deadline, _describe(condition, what))


async def settle(pilot, timeout: float = 10.0) -> None:
    """Pump the message queue until the screen's layout stops moving.

    It is the message pump, not a sleep, that does the work: a pending stylesheet
    reapply, a deferred scroll, a resize of a widget the scroll just exposed —
    each is a message, and each `pause` drains a round. Requiring the regions to
    repeat lets a slow runner take as many frames as it needs, and a screen that
    never settles raises rather than proceeding. The timeout is real elapsed
    time, as in `until`.

    `ready()` calls this once the modal is mounted (#281). Call it again after
    anything that moves the layout, before reading a region or a click
    coordinate: a widget's `region` is served from the compositor map while
    that map is valid, and a scroll does not invalidate it — so the region
    holds the old geometry until the screen's next relayout runs (#360)."""

    def _layout():
        return tuple(w.region for w in pilot.app.screen.query("*"))

    deadline = _monotonic() + timeout
    previous, stable = None, 0
    while stable < 3:
        if _monotonic() >= deadline:
            raise AssertionError(
                f"screen layout never settled within {timeout}s\n" + ui_state(pilot.app)
            )
        await _step(pilot, deadline, "layout to settle")
        current = _layout()
        stable = stable + 1 if current == previous else 0
        previous = current


async def click(pilot, target) -> None:
    """`pilot.click` that fails when the click lands anywhere but its target.

    Pilot reports whether the final event hit the widget it aimed at, and a bare
    `await pilot.click(...)` drops that answer, so a click that fell on an
    overlay surfaced waits later as a missing outcome. This fails at the click,
    naming the widget under the pointer."""
    app = pilot.app
    widget = app.screen.query_one(target) if isinstance(target, str) else target
    try:
        hit = app.get_widget_at(widget.region.x, widget.region.y)[0]  # pilot's aim point
    except Exception as e:  # off-screen: pilot.click raises OutOfBounds itself
        hit = f"<nothing: {e!r}>"
    landed = await pilot.click(widget)
    trace(f"click {widget!r} landed={landed}")
    if not landed:
        raise AssertionError(f"click aimed at {widget!r} landed on {hit!r}\n" + ui_state(app))


async def ready(pilot, selector: str, timeout: float = 10.0):
    """Wait until a modal widget is mounted *and* laid out on-screen, then return it.

    A screen-type `until` returns the instant push_screen swaps app.screen — before
    the modal's children mount (query NoMatches) or receive a layout region (click
    OutOfBounds, region still 0). Gating on a real on-screen region makes the
    following query_one / click / value-set safe on slow CI runners. A modal's
    widgets mount and lay out together, so one gate covers every field in it.

    That gate alone stopped being enough once BaseDialog grew breakpoints (#281).
    The breakpoint class is on the screen by the time the first widget has a
    region — but the stylesheet reapply it triggers is still QUEUED, so the first
    laid-out pass carries the un-classed metrics and the real layout arrives a
    frame later. Measured on StoryCheckpointModal at 45 columns, the docked row
    reads Textual's default `Button min-width: 16` on that first pass
    (x=5/23/41, each 16 wide, so the last ends at column 57 — off a 45-column
    screen) and the `-narrow` metrics (14/10/7) on the next.

    So this also `settle`s the screen before returning, so that everything
    downstream — a reachability assert, a `scroll_visible` target, a click
    coordinate — is computed against the settled layout rather than a doomed
    intermediate one. Under load that is worth 2-3 failures per 25 runs on the
    tests it covers."""

    def _hit():
        hits = pilot.app.screen.query(selector)
        node = hits.first() if hits else None
        return node if node is not None and node.region.area > 0 else None

    await until(pilot, lambda: _hit() is not None, timeout, what=f"{selector} mounted and laid out")
    await settle(pilot, timeout)
    return _hit()


def dashboard(app: BmadLoopApp) -> DashboardScreen:
    assert isinstance(app.screen, DashboardScreen)
    return app.screen


# ------------------------------------------------------------ harness proofs


async def test_notification_record_outlives_textual_expiry(project_tree, monkeypatch):
    """A toast the app emitted stays observable after Textual reaps it, and one
    it never emitted is still absent. Textual's notification clock is frozen and
    then advanced past the lifetime, so the reap is certain, not raced.

    Ablation: read `app._notifications` in `notifications()` again and the
    expired row fails; drop the spy's record and the emitted row fails."""
    from textual import notifications as textual_notifications

    # `raised_at`'s default factory bound the real `time` at import, so only
    # `time_left`'s reading follows the frozen clock.
    now = [time.time()]
    monkeypatch.setattr(textual_notifications, "time", lambda: now[0])
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.notify("short-lived", severity="warning")
        await until(
            pilot,
            lambda: any(n.message == "short-lived" for n in app._notifications),
            what="Textual holds the notification",
        )
        # Past the toast's own expiry: `raised_at` is real time at `notify`, which
        # a slow app start can put well after any reading taken before it.
        (held,) = (n for n in app._notifications if n.message == "short-lived")
        now[0] = held.raised_at + held.timeout + 1
        assert not any(n.message == "short-lived" for n in app._notifications)  # reaped
        assert ("short-lived", "warning") in notifications_with_severity(app)
        with pytest.raises(AssertionError, match="not met within 0.3s: a toast never emitted"):
            await until(
                pilot, lambda: "never said" in notifications(app), 0.3, what="a toast never emitted"
            )


async def test_notify_spy_still_renders_the_toast(project_tree):
    """The spy forwards: the real `notify` still mounts a toast with the emitted
    severity. Ablation: return from the spy without calling the real method and
    this fails."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(notifications=True) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.notify("shown to the operator", severity="warning")
        await until(
            pilot,
            lambda: ("shown to the operator", "warning") in rendered_toasts(app),
            what="the warning toast rendered",
        )


async def test_until_deadline_counts_elapsed_time_not_requested_sleeps(project_tree, monkeypatch):
    """Each step here really takes a second, so a 3-second wait must give up
    after three steps. Counting the 0.05s each step asked for, the old loop took
    sixty — a wait that said ten seconds could run for minutes on a slow runner.

    Ablation: count `waited += _STEP` instead of reading the clock and this
    fails on the step count."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        clock = [0.0]
        monkeypatch.setattr(sys.modules[__name__], "_monotonic", lambda: clock[0])
        steps = []

        class _SlowPilot:
            app = pilot.app

            async def pause(self, delay=None):
                steps.append(delay)
                clock[0] += 1.0
                await pilot.pause()

        with pytest.raises(AssertionError, match=r"not met within 3.0s: never true"):
            await until(_SlowPilot(), lambda: False, 3.0, what="never true")
        assert len(steps) == 3


async def test_until_names_a_worker_that_finished_without_calling_back(project_tree):
    """A worker that ends without posting its toast fails the wait, and the
    failure says the worker succeeded and nothing was emitted — not a bare
    "condition not met". Ablation: drop the worker record from `ui_state` and
    the worker line fails."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        worker = app.run_worker(lambda: None, thread=True, name="silent", group="probe")
        await worker.wait()
        with pytest.raises(AssertionError) as caught:
            await until(pilot, lambda: "done" in notifications(app), 0.3, what="its toast")
    assert "probe/silent=SUCCESS" in str(caught.value)
    assert "emitted: nothing" in str(caught.value)


async def test_click_fails_when_it_lands_off_its_target(project_tree):
    """A click aimed at a widget the modal covers lands on the modal, and the
    helper says so at the click instead of letting a later wait time out.
    Ablation: ignore `pilot.click`'s answer in `click` and this fails."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        covered = dashboard(app).query_one("#runs", DataTable)
        app.push_screen(ConfirmModal("probe", "a modal over the run table?"))
        await ready(pilot, "#ok")
        with pytest.raises(AssertionError, match="landed on"):
            await click(pilot, covered)


async def test_empty_project_shows_hint(project_tree):
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        assert screen.query_one("#runs", DataTable).row_count == 0
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "no runs found" in header


async def test_dashboard_survives_project_root_resolve_refusal(project_tree, monkeypatch):
    """The app mounts and completes a poll while the project root is unavailable.

    INVERSE ablation: restore bare ``project.resolve()`` in ``BmadLoopApp.__init__``
    and construction raises the stubbed WinError 64 before Textual can start.
    """
    applied_polls = 0
    apply_snapshot = DashboardScreen._apply

    def track_poll(self, snapshot):
        nonlocal applied_polls
        apply_snapshot(self, snapshot)
        applied_polls += 1

    monkeypatch.setattr(DashboardScreen, "_apply", track_poll)
    refuse_to_resolve(monkeypatch, project_tree.project)
    app = BmadLoopApp(project_tree.project)

    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: applied_polls > 0)
        assert dashboard(app).is_running


async def test_run_table_populates_and_selects_newest(project_tree):
    root = project_tree.project
    make_run(root, "20260611-100000-aaaa", finished=True)
    make_run(root, "20260611-110000-bbbb", run_type="sweep", alive=True)
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        runs = screen.query_one("#runs", DataTable)
        await until(pilot, lambda: runs.row_count == 2)
        await until(pilot, lambda: screen.selected_run_id == "20260611-110000-bbbb")
        # The run's type + pid-liveness populate on an async refresh tick after
        # the row appears; wait for the fully-rendered header (not just the id)
        # so we don't race the placeholder ("? unknown / state unavailable").
        await until(
            pilot,
            lambda: all(
                tok in str(screen.query_one("#runheader", RunHeader).content)
                for tok in ("20260611-110000-bbbb", "[sweep]", "running")
            ),
        )
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "[sweep]" in header
        assert "running" in header  # our own pid is alive


async def test_selection_switches_task_table(project):
    root = project.project
    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.commit_sha = "abc1234def567890"
    make_run(root, "20260611-100000-aaaa", finished=True, tasks={"1-1-login": task})
    make_run(root, "20260611-110000-bbbb", alive=True)
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        runs = screen.query_one("#runs", DataTable)
        tasks_table = screen.query_one("#tasks", DataTable)
        await until(pilot, lambda: screen.selected_run_id == "20260611-110000-bbbb")
        assert tasks_table.row_count == 0  # newest run has no tasks
        runs.move_cursor(row=0)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        await until(pilot, lambda: tasks_table.row_count == 1)
        assert tasks_table.get_row_at(0)[0] == "1-1-login"


async def test_task_table_shows_weighted_and_raw_tokens(project_tree):
    root = project_tree.project
    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    # cache-read heavy: raw total is dominated by re-reads the budget discounts.
    task.tokens = TokenUsage(
        input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
    )
    # a non-default weight proves the number comes from the persisted snapshot,
    # not from the 0.1 fallback. weighted = 100+50+10+round(1000*0.5) = 660.
    make_run(
        root,
        "20260611-100000-aaaa",
        finished=True,
        tasks={"1-1-login": task},
        policy_snapshot={"limits": {"cache_read_weight": 0.5}},
    )
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        tasks_table = screen.query_one("#tasks", DataTable)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        await until(pilot, lambda: tasks_table.row_count == 1)
        assert tasks_table.get_cell("1-1-login", "tokens") == "660"
        assert tasks_table.get_cell("1-1-login", "raw") == "1,160"
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "660 tokens (1,160 raw)" in header


async def test_zero_weighted_tokens_shows_zero_not_dash(project_tree):
    """With cache_read_weight=0 a cache-read-only task has weighted==0 but nonzero raw.
    The tokens cell must render "0" (a real value), not "-" — which reads as missing
    data. "-" is reserved for a task with no tokens at all."""
    root = project_tree.project
    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.tokens = TokenUsage(cache_read_tokens=1000)  # only cache reads
    make_run(
        root,
        "20260611-100000-aaaa",
        finished=True,
        tasks={"1-1-login": task},
        policy_snapshot={"limits": {"cache_read_weight": 0.0}},  # fully discount cache reads
    )
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        tasks_table = screen.query_one("#tasks", DataTable)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        await until(pilot, lambda: tasks_table.row_count == 1)
        assert tasks_table.get_cell("1-1-login", "tokens") == "0"  # weighted 0, shown not hidden
        assert tasks_table.get_cell("1-1-login", "raw") == "1,000"


async def test_apply_snapshot_after_unmount_is_noop(project_tree):
    """A poll worker hands its snapshot to `_apply` via `call_from_thread`; that call
    can land after the screen is unmounted (app shutdown / another screen popped at
    teardown), when the widgets it queries are gone. Applying to an unmounted screen
    must be a no-op, not a `NoMatches` crash on '#runs' — the flake seen when a
    settings screen is open as the app tears down."""
    root = project_tree.project
    make_run(root, "20260611-100000-aaaa", finished=True, tasks={})
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: len(screen.query("#runs")) == 1)  # fully mounted
    # the app has shut down: the screen is no longer running and its widgets are gone
    assert not screen.is_running
    # a late poll delivering runs would query '#runs'; the guard makes it a no-op
    screen._apply(_Snapshot(generation=screen._generation, runs=[]))


async def test_token_weight_falls_back_to_default(project_tree):
    root = project_tree.project
    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.tokens = TokenUsage(
        input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=1000
    )
    # empty snapshot (e.g. a pre-feature run) -> default weight 0.1.
    # weighted = 100+50+10+round(1000*0.1) = 260.
    make_run(root, "20260611-100000-aaaa", finished=True, tasks={"1-1-login": task})
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        tasks_table = screen.query_one("#tasks", DataTable)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        await until(pilot, lambda: tasks_table.row_count == 1)
        assert tasks_table.get_cell("1-1-login", "tokens") == "260"
        assert tasks_table.get_cell("1-1-login", "raw") == "1,160"


def journal_rows(journal: OptionList) -> list[str]:
    # Journal prompts are Rich Table grids, so render them to plain text.
    console = Console(width=400)
    rows = []
    for i in range(journal.option_count):
        with console.capture() as capture:
            console.print(journal.get_option_at_index(i).prompt)
        rows.append(capture.get())
    return rows


def log_text(screen: DashboardScreen) -> str:
    return "\n".join(strip.text for strip in screen.query_one("#log", RichLog).lines)


async def test_journal_pane_updates_after_poll(project_tree):
    root = project_tree.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        Journal(run_dir).append("story-start", story_key="1-2-search")
        screen._tick(force_rescan=False)  # manual poll, no 1s wait
        journal = screen.query_one("#journal", OptionList)

        def has_entry() -> bool:
            return any("story-start" in row for row in journal_rows(journal))

        await until(pilot, has_entry)
        assert any("1-2-search" in row for row in journal_rows(journal))


def test_journal_line_renders_the_unreadable_marker_red_without_restyling_producers():
    """The reader-minted marker must not fall through to `dim`: a lost record is the
    one journal line an operator must not read as background noise.

    The rest is the trap the rule had to dodge. `_JOURNAL_STYLES` matches by
    SUBSTRING, first match wins, and four PRODUCER kinds already end in
    `-unreadable` — so a bare `("unreadable", "red")` rule would have silently
    restyled all of them (and, sitting first, overridden
    `deferred-close-declaration-unreadable`'s yellow). Spelling the full kind as a
    `_JOURNAL_STYLES` row would still match by containment, reddening any kind that
    merely CONTAINS it; `journal_line` therefore matches `UNREADABLE_LINE_KIND` by
    EQUALITY, ahead of the table. This pins both halves: the four producer kinds are
    untouched, and a longer lookalike stays dim.

    Ablation: change the equality check to a `_JOURNAL_STYLES` row spelling the full
    kind and the lookalike assertion reddens; change it to the bare substring
    `"unreadable"` and the producer-kind assertions redden — verified."""

    def kind_style(kind: str):
        console = Console(width=80)
        entry = {"ts": 1_750_000_000, "kind": kind}
        segments = [s for s in console.render(journal_line(entry)) if kind[:12] in s.text]
        assert segments, f"no rendered segment carried {kind!r}"
        return segments[0].style

    assert kind_style(UNREADABLE_LINE_KIND).color is not None
    assert kind_style(UNREADABLE_LINE_KIND).color.name == "red"

    # producer kinds that also end in "-unreadable" keep exactly the styling they
    # had before the marker rule existed
    assert kind_style("story-gate-unreadable").dim is True
    assert kind_style("stories-manifest-unreadable").dim is True
    assert kind_style("rollback-owned-spec-unreadable").dim is True
    declaration = kind_style("deferred-close-declaration-unreadable")
    assert declaration.color is not None and declaration.color.name == "yellow"

    # a kind that CONTAINS the marker kind is not the marker: equality, not
    # containment, is what keeps red meaning "this line was lost"
    assert kind_style(f"{UNREADABLE_LINE_KIND}-followup").dim is True


def test_journal_line_renders_an_adopted_escalation_green():
    """DW-386: `escalation-adopted` records an operator-approved completion, so it
    must not fall to the `"escalat"` -> red substring rule.

    Ablation, performed: drop its `_JOURNAL_STYLES` row and this reddens."""
    console = Console(width=80)
    entry = {"ts": 1_750_000_000, "kind": "escalation-adopted"}
    [segment] = [s for s in console.render(journal_line(entry)) if "escalation-ad" in s.text]
    assert segment.style.color is not None and segment.style.color.name == "green"


def test_journal_line_wraps_fields_with_hanging_indent():
    entry = {
        "ts": 1_750_000_000,
        "kind": "session-start",
        "task_id": "6-1-sound-as-information-audio-layer-dev-1",
        "role": "dev",
        "prompt": "/bmad-dev-auto 6-1-sound-as-information-audio-layer",
    }
    console = Console(width=60)
    with console.capture() as capture:
        console.print(journal_line(entry))
    lines = capture.get().splitlines()
    assert len(lines) > 1  # fields are long enough to wrap at width 60
    assert "session-start" in lines[0]
    # continuation lines stay in the fields column, never spilling back under
    # the clock/kind columns. The fields column's left edge is derived from the
    # same width constants journal_line lays the grid out with.
    indent = _JOURNAL_CLOCK_WIDTH + _JOURNAL_COL_PAD + _JOURNAL_KIND_WIDTH + _JOURNAL_COL_PAD
    for line in lines[1:]:
        assert line[:indent] == " " * indent
    # and the wrapped fields carry real content past the indent
    assert any(line[indent:].strip() for line in lines[1:])


# ------------------------------------------------ #210: validate --json renderer
#
# The pure seams: parsing a validate document and rendering it. No app is
# mounted here — these are the pieces the validate modal is built out of.

# The width the modal is laid out for.
_FINDING_WIDTH = 96


def render(renderable, width: int = _FINDING_WIDTH) -> str:
    """Rich renderable -> plain text, as journal_rows does for the journal.

    ``no_color=True`` so the capture is deterministic regardless of the ambient
    ``FORCE_COLOR``/``CLICOLOR_FORCE``: Rich otherwise honors a forced color mode
    even into a non-tty capture buffer, and the column-alignment assertions here
    slice raw strings that embedded ANSI escapes would shift out of position."""
    console = Console(width=width, no_color=True)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_renderer_pins_the_current_validate_schema_version():
    """Deliberate duplication: the TUI renderer must NOT import
    documents.VALIDATE_SCHEMA_VERSION — an import would auto-follow a CLI bump and
    silently render a v2 document as v1. On failure, re-read the renderer against
    the new document, then bump the literal."""
    assert widgets._RENDERS_VALIDATE_SCHEMA == documents.VALIDATE_SCHEMA_VERSION


def test_validate_document_accepts_a_real_document():
    doc = make_validate_document([("git.worktree-clean", "ok", "git worktree clean", None)])
    assert widgets.validate_document(json.dumps(doc)) == doc


@pytest.mark.parametrize(
    ("stdout", "why"),
    [
        ("", "empty stdout — the command produced no document at all"),
        ("not json{", "unparseable"),
        ("[]", "a JSON array is not a document"),
        ('"a string"', "a JSON scalar is not a document"),
        ('{"schema_version": 2, "ok": true, "counts": {}, "findings": []}', "a newer schema"),
        ('{"ok": true, "counts": {}, "findings": []}', "no schema_version at all"),
        (
            '{"schema_version": 1, "ok": true, "counts": {}, "findings": "nope"}',
            "findings not a list",
        ),
        ('{"schema_version": 1, "ok": true, "counts": [], "findings": []}', "counts not a dict"),
    ],
)
def test_validate_document_returns_none_for_anything_undrawable(stdout, why):
    """Never raises — the caller runs this on a worker thread, where an escaping
    exception takes the app down. Undrawable is a value, so the degrade is an
    `is None` check. A *newer* schema is the important row: it parses fine and its
    fields resolve, so only the version pin catches it."""
    assert widgets.validate_document(stdout) is None, why


def test_validate_findings_renders_every_detail_shape():
    """Depth-2 covers every shape the real check sites emit, and none of them
    reach the renderer as a Python repr.

    The shapes are taken from cli.py's validate gates and platform preflight and
    install.py's skill probes; the ids are real, so ValidationReport.add's assert
    would reject this fixture if one were invented."""
    doc = make_validate_document(
        [
            # dict of scalars, and a str
            ("bmad-config", "problem", "BMAD config OK", {"implementation_artifacts": "/a/b"}),
            # NESTED dict — the passing path's shape, the one that breaks naive renderers
            ("policy", "ok", "policy OK", {"gates_mode": "strict", "adapters": {"dev": "claude"}}),
            # ints
            ("queue.sprint-status", "ok", "sprint-status OK", {"stories": 4, "actionable": 2}),
            # bool + str|None
            (
                "mux.backend",
                "ok",
                "mux ok",
                {"backend": "Tmux", "available": True, "version": None},
            ),
            # list[dict], six keys per row
            (
                "mux.backends-detected",
                "ok",
                "mux backends: tmux*",
                {
                    "backends": [
                        {
                            "name": "tmux",
                            "matches_platform": True,
                            "available": True,
                            "version": "3.4",
                            "selected": True,
                            "reason": "default",
                        }
                    ]
                },
            ),
            # list[str]
            ("skills.base-incomplete", "problem", "incomplete", {"missing_markers": ["a", "b"]}),
            # None detail
            ("git.probe", "problem", "git check failed", None),
        ]
    )
    out = render(widgets.validate_findings(doc, details=True))

    assert "{'" not in out, "a Python repr leaked — some shape was str()'d, not modelled"
    assert "adapters: dev=claude" in out  # the nested dict, as readable pairs
    assert "stories: 4" in out
    assert "version: null" in out and "available: true" in out  # JSON's spelling, not Python's
    assert "missing_markers: a, b" in out
    assert "name=tmux" in out and "reason=default" in out  # list[dict], one line per entry
    for finding in doc["findings"]:
        assert finding["check"] in out


def test_validate_findings_detail_is_gated_on_severity_not_check_id():
    """Inline detail for warning/problem — what a reader opened the modal to act
    on — and everything under `details`. One severity rule, zero id matching."""
    doc = make_validate_document(
        [
            ("host.process", "ok", "process host: Posix", {"host": "PosixProcessHost"}),
            ("adapter.binary", "problem", "codex not found", {"binary": "codex"}),
            ("policy.model-qualified", "warning", "bare model", {"model": "haiku"}),
        ]
    )
    inline = render(widgets.validate_findings(doc, details=False))
    assert "binary: codex" in inline and "model: haiku" in inline
    assert "host: PosixProcessHost" not in inline, "an ok finding's detail is not inline"

    expanded = render(widgets.validate_findings(doc, details=True))
    assert "host: PosixProcessHost" in expanded


def test_validate_findings_survives_a_malformed_finding():
    """One bad finding costs its own row, not the modal. The document arrives from
    a subprocess, so 'this cannot happen' is not available."""
    doc = make_validate_document([("git.worktree-clean", "ok", "git worktree clean", None)])
    doc["findings"] = [
        "not a dict",
        None,
        {"check": "policy", "severity": "made-up", "message": "unknown severity is neutral"},
        *doc["findings"],
    ]
    out = render(widgets.validate_findings(doc, details=True))

    assert out.count("(unreadable finding)") == 2  # the string and the None
    assert "unknown severity is neutral" in out  # rendered, just without a style
    assert "git worktree clean" in out, "a good finding after a bad one still renders"


def test_validate_findings_multiline_message_keeps_column_alignment():
    """The fold trap: several problems are a bare str(e) carrying a PyYAML
    MarkedYAMLError, so `message` is multi-line. In a flat Text an embedded
    newline returns to column 0 and destroys every row below it; folding inside
    the message column is what keeps the grid a grid."""
    doc = make_validate_document(
        [
            ("policy", "problem", "while parsing a block\n  in policy.toml, line 3\n    ^", None),
            ("git.worktree-clean", "ok", "git worktree clean", None),
        ]
    )
    lines = render(widgets.validate_findings(doc, details=False)).splitlines()

    indent = _FINDING_GLYPH_WIDTH + _FINDING_COL_PAD + _FINDING_CHECK_WIDTH + _FINDING_COL_PAD
    body = [ln for ln in lines if "in policy.toml" in ln or "^" in ln]
    assert body, "the continuation lines rendered"
    for line in body:
        assert line[:indent] == " " * indent
        assert line[indent:].strip()
    # the row after the multi-line message is still in its own columns
    assert any(ln[indent:].startswith("git worktree clean") for ln in lines)


def test_validate_header_verdict_comes_from_ok_with_the_chained_gates_note():
    """The verdict is doc["ok"], never an exit code — rc conflates 'checks failed'
    with 'the command broke'. The chained-gates footer appears only when something
    failed, because that is when absence stops meaning 'passed'."""
    passing = make_validate_document([("git.worktree-clean", "ok", "clean", None)])
    failing = make_validate_document([("adapter.binary", "problem", "codex not found", None)])

    good = render(widgets.validate_header(passing))
    assert "validate passed" in good
    assert "1 ok" in good and "0 problem" in good
    assert "gates are chained" not in good

    bad = render(widgets.validate_header(failing))
    assert "validate failed" in bad
    assert "gates are chained" in bad


def test_validate_header_shows_mode_and_spec_folder_without_markup():
    """spec_folder is user-controlled and reaches a Static that defaults to
    markup=True, so it must arrive as Text. A folder with brackets would be a
    MarkupError if it were ever interpolated into a markup string."""
    doc = make_validate_document(
        [("git.worktree-clean", "ok", "clean", None)],
        stories_on=True,
        spec_folder="docs/[wip]-epic-3",
    )
    header = widgets.validate_header(doc)
    assert isinstance(header, Text)
    out = render(header)
    assert "mode: stories" in out
    assert "docs/[wip]-epic-3" in out, "brackets survive verbatim — nothing interpreted them"


def test_validate_header_tolerates_a_gutted_document():
    """validate_document gates the shape, but the header still never raises on a
    field that is present and of the wrong type."""
    out = render(widgets.validate_header({"ok": None, "counts": {"problem": "lots"}}))
    assert "verdict unknown" in out
    assert "gates are chained" not in out  # a non-int count is not a problem count


async def test_log_pane_shows_emulated_content(project_tree):
    from test_tui_data import ink_stream

    root = project_tree.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    (run_dir / "logs").mkdir()
    (run_dir / "logs" / "story-1.log").write_bytes(ink_stream())
    Journal(run_dir).append("session-start", task_id="story-1")
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        # a hidden RichLog defers all writes until it has a size — show the tab
        screen.query_one("#tabs", TabbedContent).active = "tab-log"
        await pilot.pause()
        screen._tick(force_rescan=False)  # manual poll, no 1s wait
        log = screen.query_one("#log", RichLog)

        def has_final_line() -> bool:
            return any("done in 3s" in strip.text for strip in log.lines)

        await until(pilot, has_final_line)
        text = "\n".join(strip.text for strip in log.lines)
        assert "— story-1.log —" in text
        assert "thinking" not in text  # repaint frames collapsed away
        assert "\x1b" not in text


# --------------------------------------------------------- text select & copy
# Use an empty project so no run is selected: the poll never rewrites #log, so
# the lines we write directly stay put for the assertions.


async def test_selectable_rich_log_get_selection(project_tree):
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        screen.query_one("#tabs", TabbedContent).active = "tab-log"  # give it a size
        await pilot.pause()
        log = screen.query_one("#log", SelectableRichLog)
        log.write(Text("first line"))
        log.write(Text("second line"))
        await pilot.pause()
        # whole-buffer selection returns every line's plain text
        assert log.get_selection(Selection(None, None))[0] == "first line\nsecond line"
        # a sub-range honours the start/end column+row offsets
        sel = Selection(Offset(6, 0), Offset(6, 1))
        assert log.get_selection(sel)[0] == "line\nsecond"


async def test_copy_pane_action_copies_log(project_tree, monkeypatch):
    copied: list[str] = []
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        monkeypatch.setattr(app, "copy_to_clipboard", lambda text: copied.append(text))
        screen.query_one("#tabs", TabbedContent).active = "tab-log"
        await pilot.pause()
        log = screen.query_one("#log", SelectableRichLog)
        log.write(Text("error: boom"))
        log.write(Text("at file.py:42"))
        await pilot.pause()
        await pilot.press("y")
        await until(pilot, lambda: bool(copied))
        assert copied == ["error: boom\nat file.py:42"]
        assert any("copied log pane" in m for m in notifications(app))


async def test_copy_pane_wrong_tab_notifies(project_tree):
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        assert screen.query_one("#tabs", TabbedContent).active == "tab-journal"  # default
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("Log or Attention tab" in m for m in notifications(app)),
        )


async def test_copy_pane_empty_notifies(project_tree):
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        screen.query_one("#tabs", TabbedContent).active = "tab-attention"
        await pilot.pause()
        await pilot.press("y")
        await until(pilot, lambda: any("nothing to copy" in m for m in notifications(app)))


# ------------------------------------------------------- journal -> log jump


def write_numbered_log(run_dir: Path, task_id: str, count: int = 200) -> list[int]:
    """`row NNN\\r\\n` lines; returns each row's starting byte offset."""
    (run_dir / "logs").mkdir(exist_ok=True)
    offsets, buf = [], b""
    for i in range(count):
        offsets.append(len(buf))
        buf += f"row {i:03d}\r\n".encode()
    (run_dir / "logs" / f"{task_id}.log").write_bytes(buf)
    return offsets


async def test_journal_enter_jumps_to_log_position(project_tree):
    root = project_tree.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    offsets = write_numbered_log(run_dir, "story-1")
    journal = Journal(run_dir)
    journal.set_active_log("story-1")
    journal.append("session-start", task_id="story-1")
    # a mid-log event: explicit log_pos wins over the stamped file size
    journal.append("checkpoint", log_task="story-1", log_pos=offsets[100])
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        journal_list = screen.query_one("#journal", OptionList)
        await until(pilot, lambda: journal_list.option_count == 2)
        journal_list.focus()
        await pilot.press("end", "enter")  # select the checkpoint entry
        tabs = screen.query_one("#tabs", TabbedContent)
        await until(pilot, lambda: tabs.active == "tab-log")
        log = screen.query_one("#log", RichLog)
        # scrolled into the middle of the log, not snapped to either end
        await until(pilot, lambda: 0 < log.scroll_y < log.max_scroll_y)
        assert "row 100" in log_text(screen)


async def test_journal_jump_survives_exhausted_scroll_retry_chain(project_tree):
    # Regression for #178: the hidden #log pane defers its writes, and on a
    # starved runner the flush can outlive _scroll_log_to's whole retry chain.
    # The old code gave up silently and lost the jump forever; now the pending
    # jump survives exhaustion and the next poll tick re-attempts it. Exhaust
    # the chain deterministically (attempts=0 against the unflushed pane)
    # instead of relying on a contended runner to starve it for real.
    root = project_tree.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    offsets = write_numbered_log(run_dir, "story-1")
    journal = Journal(run_dir)
    journal.set_active_log("story-1")
    journal.append("session-start", task_id="story-1")
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        # the poll renders the active log while #log is still hidden behind
        # tab-journal, so its RichLog writes stay deferred (virtual_size 0)
        await until(
            pilot,
            lambda: screen._displayed_log_task == "story-1" and screen._log_index is not None,
        )
        screen._pending_jump = ("story-1", offsets[100])
        screen._log_follow_tail = False
        screen._scroll_log_to(attempts=0)
        # chain exhausted against the unflushed pane: the jump must survive
        assert screen._pending_jump is not None
        screen.query_one("#tabs", TabbedContent).active = "tab-log"
        await until(pilot, lambda: screen._pending_jump is None)  # a tick rescued it
        log = screen.query_one("#log", RichLog)
        assert 0 < log.scroll_y < log.max_scroll_y
        assert "row 100" in log_text(screen)


async def test_journal_jump_retry_recomputes_line_after_same_task_repaint(project_tree):
    # A delayed retry must not reuse the line captured when the chain was
    # armed: a poll can repaint the same task's log mid-chain (history
    # eviction advances LogIndex.render_base), shifting the line a byte
    # offset maps to. The old code scrolled the stale line and cleared
    # _pending_jump, silencing the fresher chain. Each fire now recomputes
    # the line from the live index. Fully deterministic: the armed timer
    # callback is captured and invoked by hand — no reveal, no tick race.
    root = project_tree.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    offsets = write_numbered_log(run_dir, "story-1")
    journal = Journal(run_dir)
    journal.set_active_log("story-1")
    journal.append("session-start", task_id="story-1")
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        await until(
            pilot,
            lambda: screen._displayed_log_task == "story-1" and screen._log_index is not None,
        )
        log = screen.query_one("#log", RichLog)
        # arm one retry against the unflushed hidden pane, capturing its callback
        captured = []
        screen.set_timer = lambda delay, cb: captured.append(cb)
        screen._pending_jump = ("story-1", offsets[100])
        screen._log_follow_tail = False
        screen._scroll_log_to(attempts=1)
        del screen.set_timer
        assert len(captured) == 1 and screen._pending_jump is not None
        stale_line = screen._log_index.line_for_offset(offsets[100])
        # same-task repaint mid-chain: history eviction shifts render_base,
        # so the same offset now maps 7 lines earlier
        screen._log_index = dataclasses.replace(
            screen._log_index, render_base=screen._log_index.render_base + 7
        )
        fresh_line = screen._log_index.line_for_offset(offsets[100])
        assert fresh_line == stale_line - 7
        # open the height gate without a real Textual flush, record the scroll
        log.virtual_size = Size(80, 500)
        scrolls = []
        log.scroll_to = lambda *a, **kw: scrolls.append((a, kw))
        finalizes = []
        log.call_after_refresh = lambda cb, *a, **kw: finalizes.append(cb)
        captured[0]()  # the delayed retry fires
        viewport = max(1, log.scrollable_content_region.height)
        expected = max(0, (fresh_line + 1) - viewport // 2)
        stale = max(0, (stale_line + 1) - viewport // 2)
        assert scrolls == [((), {"y": expected, "animate": False})]
        assert expected != stale  # the recompute is what moved the target
        # the release rides the log's queue (stomp ordering) — still pending here
        assert screen._pending_jump is not None and len(finalizes) == 1
        finalizes[0]()  # the queued finalize fire re-scrolls and releases
        del log.scroll_to, log.call_after_refresh
        assert scrolls == [((), {"y": expected, "animate": False})] * 2
        assert screen._pending_jump is None  # landed: the jump is released


async def test_journal_jump_release_survives_flush_scroll_end_stomp(project_tree):
    # The reveal flush replays a hidden RichLog's deferred writes: virtual_size
    # grows synchronously (opening _scroll_log_to's height gate) but the
    # flushed write's scroll_end is only *queued* via call_after_refresh.
    # ScrollView.scroll_to applies immediately, so a fire in that window used
    # to land, release the jump, and then get stomped to the tail by the
    # queued scroll with nothing left to re-attempt — the win-py3.11 CI
    # failure. The release now rides the same queue: the finalize fire drains
    # after the stomp, re-scrolls to the recomputed target, then lets go.
    # Deterministic: the finalize callback is captured and the stomp is
    # replayed by hand between the immediate scroll and the finalize.
    root = project_tree.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    offsets = write_numbered_log(run_dir, "story-1")
    journal = Journal(run_dir)
    journal.set_active_log("story-1")
    journal.append("session-start", task_id="story-1")
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        await until(
            pilot,
            lambda: screen._displayed_log_task == "story-1" and screen._log_index is not None,
        )
        log = screen.query_one("#log", RichLog)
        screen._pending_jump = ("story-1", offsets[100])
        screen._log_follow_tail = False
        # the flush just wrote (gate open) but its scroll_end is still queued
        log.virtual_size = Size(80, 500)
        scrolls = []
        log.scroll_to = lambda *a, **kw: scrolls.append((a, kw))
        finalizes = []
        log.call_after_refresh = lambda cb, *a, **kw: finalizes.append(cb)
        screen._scroll_log_to(attempts=0)
        viewport = max(1, log.scrollable_content_region.height)
        line = screen._log_index.line_for_offset(offsets[100])
        expected = max(0, (line + 1) - viewport // 2)
        assert scrolls == [((), {"y": expected, "animate": False})]  # landed...
        assert screen._pending_jump is not None  # ...but the jump is not released
        assert len(finalizes) == 1
        # the queued flush scroll_end drains first and stomps to the tail
        log.scroll_y = 400
        finalizes[0]()  # FIFO on the log's pump: finalize fires after the stomp
        del log.scroll_to, log.call_after_refresh
        # the finalize fire re-scrolled to the recomputed target, then let go
        assert scrolls == [((), {"y": expected, "animate": False})] * 2
        assert screen._pending_jump is None  # only now is the jump released


async def test_journal_enter_without_position_notifies(project_tree):
    root = project_tree.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    Journal(run_dir).append("story-start", story_key="1-2-search")  # no session yet
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        journal_list = screen.query_one("#journal", OptionList)
        await until(pilot, lambda: journal_list.option_count == 1)
        journal_list.focus()
        await pilot.press("end", "enter")
        await until(pilot, lambda: any("no log position" in m for m in notifications(app)))
        assert screen.query_one("#tabs", TabbedContent).active == "tab-journal"


async def test_journal_jump_pins_other_sessions_log(project_tree):
    root = project_tree.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    write_numbered_log(run_dir, "story-1", count=30)
    write_numbered_log(run_dir, "story-2", count=30)
    journal = Journal(run_dir)
    journal.set_active_log("story-1")
    journal.append("session-start", task_id="story-1")
    journal.append("session-end", task_id="story-1")
    journal.set_active_log("story-2")
    journal.append("session-start", task_id="story-2")  # active session: story-2
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        await until(pilot, lambda: screen._displayed_log_task == "story-2")
        journal_list = screen.query_one("#journal", OptionList)
        await until(pilot, lambda: journal_list.option_count == 3)
        journal_list.focus()
        journal_list.highlighted = 1  # session-end of story-1
        await pilot.press("enter")
        await until(pilot, lambda: "— story-1.log — (pinned" in log_text(screen))
        await pilot.press("escape")  # unpin: back to following the active log
        await until(pilot, lambda: "— story-2.log —" in log_text(screen))
        assert "(pinned" not in log_text(screen)


async def test_journal_jump_near_tail_does_not_chase_growing_log(project_tree):
    # Regression for "pressing enter keeps sending me to the bottom": jumping to
    # an entry near the end lands the view at the tail, and the old code then
    # inferred "follow the tail" from that, dragging the view down on every poll
    # as the live log grew. A jump must anchor the position until esc is pressed.
    root = project_tree.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    offsets = write_numbered_log(run_dir, "story-1")
    journal = Journal(run_dir)
    journal.set_active_log("story-1")
    journal.append("session-start", task_id="story-1")
    journal.append("checkpoint", log_task="story-1", log_pos=offsets[-1])  # the last row
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        journal_list = screen.query_one("#journal", OptionList)
        await until(pilot, lambda: journal_list.option_count == 2)
        journal_list.focus()
        await pilot.press("end", "enter")  # jump to the near-tail checkpoint
        log = screen.query_one("#log", RichLog)
        # Wait for the jump to actually settle at the tail: max_scroll_y > 0 proves
        # the RichLog flushed its lines (an empty/unflushed pane is trivially "at
        # scroll end" with scroll_y == max == 0, which would sample anchored=0 before
        # the deferred _scroll_log_to timer runs, then fail when the jump lands late).
        await until(pilot, lambda: log.max_scroll_y > 0 and log.is_vertical_scroll_end)
        # Wait for the jump to land (landing releases _pending_jump); after that
        # the jump machinery is inert — armed retries abort on the cleared jump —
        # so sampling the anchor is race-free even against the growth below.
        await until(pilot, lambda: screen._pending_jump is None)
        assert log.is_vertical_scroll_end  # landed at the tail, not mid-log
        anchored, base_max = log.scroll_y, log.max_scroll_y
        # the live session keeps writing; a poll repaints the pane
        with (run_dir / "logs" / "story-1.log").open("ab") as f:
            for i in range(200, 260):
                f.write(f"row {i:03d}\r\n".encode())
        screen._tick(force_rescan=False)
        await until(pilot, lambda: log.max_scroll_y > base_max)  # new lines rendered
        assert round(log.scroll_y) == round(anchored)  # stayed put, did not chase the tail
        assert log.scroll_y < log.max_scroll_y


async def test_poll_skips_while_another_holds_the_lock(project_tree):
    # Regression: exclusive=True cannot stop a running thread worker, so the
    # screen lock must make a second poll bail instead of mutating shared ctx
    # (two threads feeding ctx.log's pyte stream crashed the TUI).
    #
    # Ablation target: delete the `if not self._poll_lock.acquire(blocking=False):
    # return` guard from `_poll` *and* neutralize its paired
    # `finally: self._poll_lock.release()` to `pass` — one guard, both halves,
    # not two gates. Dropping only the acquire makes every other tick release a
    # lock it never took, reddening the whole file on `RuntimeError: release
    # unlocked lock` instead. With both gone this test fails alone on
    # `assert ctx.entries == before` — the probe thread runs the body and
    # appends the checkpoint entry.
    root = project_tree.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    write_numbered_log(run_dir, "story-1", count=30)
    journal = Journal(run_dir)
    journal.set_active_log("story-1")
    journal.append("session-start", task_id="story-1")
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        ctx = screen._ctx
        assert ctx is not None
        await until(pilot, lambda: len(ctx.entries) == 1)
        # Stand in for an in-flight worker. Acquire without blocking and yield
        # to the loop until we win it — a blocking acquire on the event-loop
        # thread would deadlock against a real poll worker that holds the lock
        # while waiting on call_from_thread(_apply).
        await until(pilot, lambda: screen._poll_lock.acquire(blocking=False))
        try:
            gen = screen._generation
            before = list(ctx.entries)
            journal.append("checkpoint", log_task="story-1", log_pos=0)  # new entry on disk
            # Run the undecorated body as our own thread worker, in a group of
            # our own. Calling the @work-decorated _poll enters group "poll" on
            # this same node, and the next 1s interval tick's poll cancels that
            # group on arrival (add_worker -> cancel_group), marking this worker
            # CANCELLED — so worker.wait() raced the tick and raised
            # WorkerCancelled on slow Windows runners (#581). A private group is
            # never a cancel_group candidate, so this awaits to completion;
            # thread=True keeps it a real second thread entering the guarded body
            # while the lock is held, which is the point of the test.
            # exit_on_error=False surfaces a body exception as WorkerFailed at
            # the await instead of tearing the app down mid-test.
            worker = screen.run_worker(
                lambda: DashboardScreen._poll.__wrapped__(screen, ctx, gen, False, None),
                thread=True,
                group="poll-probe-581",
                exit_on_error=False,
            )
            await worker.wait()
            assert ctx.entries == before  # guarded body never ran
        finally:
            screen._poll_lock.release()


# ----------------------------------------------------------- sprint tree pane


async def test_sprint_tree_populates(project_tree):
    install_bmad_config(project_tree)
    write_sprint(
        project_tree,
        {
            "epic-1": "in-progress",
            "1-1-auth": "done",
            "1-2-search": "backlog",
            "epic-1-retrospective": "optional",
            "epic-2": "backlog",
            "2-1-billing": "backlog",
        },
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        tree = screen.query_one("#sprint-tree", SprintTree)
        await until(pilot, lambda: len(tree.root.children) == 2)
        epic1, epic2 = tree.root.children
        assert "Epic 1" in str(epic1.label) and "1/2" in str(epic1.label)
        assert "Epic 2" in str(epic2.label)
        assert not epic1.is_expanded  # epics start collapsed
        epic1.expand()
        labels = [str(c.label) for c in epic1.children]
        assert any("✓ 1-auth" in label for label in labels)  # done story, checked
        assert any("2-search" in label for label in labels)
        assert any("retrospective" in label for label in labels)
        done_label = next(c.label for c in epic1.children if "auth" in str(c.label))
        assert done_label.style == "green"


async def test_sprint_tree_preserves_expansion_across_refresh(project_tree):
    install_bmad_config(project_tree)
    write_sprint(project_tree, {"epic-1": "in-progress", "1-1-auth": "in-progress"})
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        tree = screen.query_one("#sprint-tree", SprintTree)
        # wait past the initial placeholder for the real epic node
        await until(pilot, lambda: "Epic 1" in str(tree.root.children[0].label))
        node = tree.root.children[0]
        node.expand()
        write_sprint(project_tree, {"epic-1": "in-progress", "1-1-auth": "done"})
        screen._tick(force_rescan=True)

        def story_checked() -> bool:
            children = tree.root.children[0].children
            return bool(children) and "✓" in str(children[0].label)

        await until(pilot, story_checked)
        assert tree.root.children[0] is node  # reconciled in place, not rebuilt
        assert node.is_expanded


async def test_sprint_tree_forgives_malformed_yaml(project_tree):
    install_bmad_config(project_tree)
    project_tree.sprint_status.write_text("{ not valid yaml [")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        tree = screen.query_one("#sprint-tree", SprintTree)
        await pilot.pause(0.2)
        assert "sprint status unavailable" in str(tree.root.children[0].label)
        # the app keeps polling and recovers once the file is fixed
        write_sprint(project_tree, {"epic-1": "backlog", "1-1-auth": "backlog"})
        screen._tick(force_rescan=True)
        await until(pilot, lambda: "Epic 1" in str(tree.root.children[0].label))


# ---------------------------------------------------------- deferred work pane


_LEDGER = (
    "# Deferred Work\n\n"
    "### DW-1: Fix flaky retry\n\n"
    "origin: test, 2026-06-01\nlocation: a.py:1\n"
    "severity: high\nreason: test.\nstatus: open\n\n"
    "### DW-2: Polish help text\n\n"
    "origin: test, 2026-06-01\nlocation: b.py:2\n"
    "severity: low\nreason: test.\nstatus: done 2026-06-10\n"
)


def deferred_rows(deferred: OptionList) -> list[str]:
    return [str(deferred.get_option_at_index(i).prompt) for i in range(deferred.option_count)]


async def test_deferred_pane_lists_and_opens_modal(project_tree):
    install_bmad_config(project_tree)
    project_tree.deferred_work.write_text(_LEDGER, encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        deferred = screen.query_one("#deferred", OptionList)
        await until(pilot, lambda: deferred.option_count == 2)
        rows = deferred_rows(deferred)
        assert "DW-1" in rows[0] and "Fix flaky retry" in rows[0]
        assert "DW-2 ✓" in rows[1]  # done entry, checked
        done_prompt = deferred.get_option_at_index(1).prompt
        assert all(span.style == "green" for span in done_prompt.spans)
        deferred.focus()
        deferred.highlighted = 0
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, DeferredEntryModal))
        await ready(pilot, "Static")  # body mounts a tick after the screen swaps
        statics = app.screen.query("Static")
        assert any("location: a.py:1" in str(s.content) for s in statics)
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))


async def test_deferred_pane_preserves_highlight_across_refresh(project_tree):
    install_bmad_config(project_tree)
    project_tree.deferred_work.write_text(_LEDGER, encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        deferred = screen.query_one("#deferred", OptionList)
        await until(pilot, lambda: deferred.option_count == 2)
        deferred.highlighted = 1  # DW-2
        project_tree.deferred_work.write_text(
            _LEDGER.replace("status: open", "status: done 2026-06-12"), encoding="utf-8"
        )
        screen._tick(force_rescan=True)
        await until(pilot, lambda: "DW-1 ✓" in deferred_rows(deferred)[0])
        assert deferred.get_option_at_index(deferred.highlighted).id == "DW-2"


async def test_deferred_pane_shows_legacy_items(project_tree):
    install_bmad_config(project_tree)
    project_tree.deferred_work.write_text(
        "# Deferred Work\n\n"
        "## Deferred from: epic 1 review (2026-04-06)\n\n"
        "- ~~**Old fixed thing** — was broken, then repaired~~ → fixed in 1.3\n"
        "- **Open legacy thing here** — still pending. [MAJOR]\n\n" + _LEDGER.split("\n\n", 1)[1],
        encoding="utf-8",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        deferred = screen.query_one("#deferred", OptionList)
        await until(pilot, lambda: deferred.option_count == 4)
        rows = deferred_rows(deferred)
        assert "L1 ✓ Old fixed thing" in rows[0] and "·legacy" in rows[0]
        assert "Open legacy thing here" in rows[1] and "·legacy" in rows[1]
        assert "DW-1" in rows[2] and "·legacy" not in rows[2]
        option = deferred.get_option_at_index(1)
        assert option.id.startswith("legacy:")
        deferred.focus()
        deferred.highlighted = 1
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, DeferredEntryModal))
        await ready(pilot, "Static")  # body mounts a tick after the screen swaps
        statics = app.screen.query("Static")
        assert any("legacy — converted to DW format" in str(s.content) for s in statics)
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))


async def test_deferred_pane_placeholder_without_ledger(project_tree):
    install_bmad_config(project_tree)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        deferred = screen.query_one("#deferred", OptionList)
        await until(pilot, lambda: deferred.option_count == 1)
        assert "deferred ledger unavailable" in deferred_rows(deferred)[0]
        assert deferred.get_option_at_index(0).disabled


def _write_triage_decision(run_dir: Path, dw_id: str = "DW-1") -> None:
    import json

    (run_dir / "triage.json").write_text(
        json.dumps(
            {
                "workflow": "deferred-sweep-triage",
                "open_ids": [dw_id],
                "already_resolved": [],
                "bundles": [],
                "blocked": [],
                "skip": [],
                "decisions": [
                    {
                        "id": dw_id,
                        "question": "Renegotiate the API signature?",
                        "context": "ctx",
                        "options": [
                            {"key": "1", "label": "Widen", "effect": "build", "intent": "widen it"},
                            {"key": "2", "label": "Keep", "effect": "keep-open"},
                        ],
                        "recommendation": "1",
                    }
                ],
                "escalations": [],
            }
        ),
        encoding="utf-8",
    )


async def test_missed_decision_count_and_answer_via_modal(project):
    from bmad_loop import decisions

    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n### DW-1: Renegotiate API\n\n"
        "origin: test, 2026-06-01\nlocation: a.py:1\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    _write_triage_decision(make_run(project.project, "20260101-000000-aaaa", run_type="sweep"))
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        deferred = dashboard(app).query_one("#deferred", OptionList)
        await until(pilot, lambda: "1 to answer" in str(deferred.border_title))
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, DecisionModal))
        await click(pilot, await ready(pilot, "#opt-1"))  # choose build
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
    assert decisions.load_pre_answers(project.project)["DW-1"]["effect"] == "build"


async def test_answer_decisions_none_notifies(project_tree):
    install_bmad_config(project_tree)
    project_tree.deferred_work.write_text(
        "# Deferred Work\n\n### DW-1: done thing\n\norigin: t\nstatus: done 2026-06-01\n",
        encoding="utf-8",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("d")
        await until(pilot, lambda: any("no unanswered decisions" in m for m in notifications(app)))


async def test_answer_decisions_read_fault_toasts_the_fault_not_none(project_tree):
    """DW-473: with no loadable BMAD config nothing could be read, so `d` toasts the
    fault as an error and never claims "no unanswered decisions"; the Deferred Work
    badge says unreadable rather than showing no count.

    Ablation: treat `missed.fault` as an empty answer in `action_answer_decisions`
    and the "could not read" wait times out."""
    app = BmadLoopApp(project_tree.project)  # no install_bmad_config: config not found
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        deferred = dashboard(app).query_one("#deferred", OptionList)
        await until(pilot, lambda: "decisions unreadable" in str(deferred.border_title))
        await pilot.press("d")
        await until(
            pilot,
            lambda: any(
                "could not read past sweeps' decisions" in m and sev == "error"
                for m, sev in notifications_with_severity(app)
            ),
        )
        assert not any("no unanswered decisions" in m for m in notifications(app))


# ------------------------------------- #275 modal bodies scroll, buttons stay


def _on_screen(app, w) -> bool:
    """A widget's laid-out region is non-empty and fully inside the screen —
    i.e. the button is reachable, not clipped off the visible area (#275)."""
    r = w.region
    return r.width > 0 and r.height > 0 and app.screen.region.contains_region(r)


def _long_decision():
    from bmad_loop.sweep import Decision, DecisionOption

    options = tuple(
        DecisionOption(
            key=str(i),
            label=f"option {i} — " + "a wordy option label that keeps going " * 3,
            effect="build",
            intent="a long intent describing what building this bundle would do " * 2,
        )
        for i in range(1, 9)
    )
    return Decision(
        id="DW-1",
        question="a decision question that is itself fairly wordy " * 3,
        context="\n".join(f"context line {i} with some detail" for i in range(60)),
        options=options,
        recommendation="1",
    )


async def test_decision_modal_scrolls_when_content_long(project_tree):
    """A long question + 60-line context + 8 options overflow the dialog, but the
    body scrolls so the docked skip button stays reachable AND the last option can
    be scrolled into view and activated — proving real access, not just overflow."""
    app = BmadLoopApp(project_tree.project)
    chosen: list = []
    async with app.run_test(size=(90, 16)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(DecisionModal(_long_decision()), chosen.append)
        await until(pilot, lambda: isinstance(app.screen, DecisionModal))
        body = await ready(pilot, "#body")
        assert body.max_scroll_y > 0  # content overflows yet scrolls
        assert _on_screen(app, app.screen.query_one("#cancel", Button))  # skip reachable
        # the last option starts below the fold; scroll it in, confirm it is on
        # screen, then click it — the whole point of the scroll fix.
        opt8 = app.screen.query_one("#opt-8", Button)
        opt8.scroll_visible(animate=False)
        # `scroll_visible` only queues the scroll. It runs straight through to
        # the container's `Widget.scroll_to`, which defers the offset write via
        # `call_after_refresh`: an InvokeLater the pump forwards to the screen,
        # where it lands on `Screen._callbacks`. Draining that queue always
        # costs a later pump hop. The screen's idle handler drains it, but only
        # once the screen is clean — a dirty one resumes the update timer and
        # returns — and `_on_timer_update` only `call_next`s the drain rather
        # than running it. So `pilot.pause()` does not synchronize on the
        # write: its barrier covers messages queued at call time, and the
        # `_on_timer_update` it ends with relayouts whatever scroll state
        # exists right then. Lose that hop and `scroll_y` is still 0,
        # so the relayout reflows the old offset and `region` keeps its
        # pre-scroll geometry, putting the option below the fold (#360). Gate
        # on the write landing — `body.scroll_y` is the one observable here not
        # read through the compositor map — then let the relayout it triggers
        # settle before reading a region.
        await until(pilot, lambda: body.scroll_y > 0)
        await settle(pilot)
        assert _on_screen(app, opt8)
        await pilot.click("#opt-8")
        await until(pilot, lambda: bool(chosen))
        assert chosen[0].key == "8"  # the eighth option was actually returned


async def test_escalation_modal_scrolls_when_description_long(project_tree):
    """A long escalation description overflows; the body scrolls and both the
    Resolve and close buttons stay on-screen."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(90, 16)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(
            EscalationModal(
                story_key="e-1-s",
                title="t",
                description="X\n" * 80,
                blocking="b",
                sentinel_kind="",
                resolution_ready=False,
                engine_live=False,
            )
        )
        await until(pilot, lambda: isinstance(app.screen, EscalationModal))
        body = await ready(pilot, "#body")
        assert body.max_scroll_y > 0
        assert _on_screen(app, app.screen.query_one("#act-resolve", Button))
        assert _on_screen(app, app.screen.query_one("#cancel", Button))


async def test_confirm_modal_scrolls_long_body(project_tree):
    """A ConfirmModal (covers ConfirmResumeModal by inheritance) with a long body
    scrolls it so the confirm/cancel buttons stay reachable, and the ⚠ warning is
    docked outside the scroll region so it stays on-screen with the buttons — a
    warning that gates the enabled confirm must never scroll off (#280 review)."""
    app = BmadLoopApp(project_tree.project)
    # height 16 clears the frame floor now that the warning is a docked row (a
    # sibling of #body); the 80-line body still overflows the 60%-capped #body.
    async with app.run_test(size=(64, 16)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(ConfirmModal("t", "line\n" * 80, warning="w"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        body = await ready(pilot, "#body")
        assert body.max_scroll_y > 0  # the long body still overflows and scrolls
        assert _on_screen(app, app.screen.query_one("#ok", Button))
        assert _on_screen(app, app.screen.query_one("#cancel", Button))
        # the warning is a sibling of #body, not a child — visible whenever #ok is
        assert _on_screen(app, app.screen.query_one("#warning", Static))


async def test_start_sweep_and_checkpoint_buttons_reachable(project):
    """On a short terminal the bounded modals keep their docked action buttons
    on-screen: the body scrolls to absorb the overflow instead of pushing the
    button row off the bottom. The height (14) sits just above the frame floor,
    so the assertion isolates the body-scroll fix.

    The frame floor is not a chrome count and it is not one number. The chrome is
    10 rows — 2 border, 2 padding, 1 title, 1 title margin, 1 button-row margin
    and 3 for the button row, Textual's Button being `border: tall`. `#dialog` is
    then capped at `max-height: 90%`, which turns those 10 rows into a per-modal
    TERMINAL-height floor of 12-14: ConfirmModal 12, StartSweep 13,
    StoryCheckpoint 13, ConfirmModal-with-a-warning 14. 14 was chosen because it
    clears the 13-row floor of the two modals this test drives (#281 measured)."""
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(64, 14)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(StartSweepModal())
        await until(pilot, lambda: isinstance(app.screen, StartSweepModal))
        body = await ready(pilot, "#body")
        assert body.max_scroll_y > 0  # options overflow the shrunk body, so it scrolls
        assert _on_screen(app, app.screen.query_one("#ok", Button))
        assert _on_screen(app, app.screen.query_one("#cancel", Button))
        app.pop_screen()

        # genuinely long checkpoint content (not just a tiny terminal): the body
        # must scroll to absorb it while the action buttons stay docked on-screen.
        app.push_screen(
            StoryCheckpointModal(
                story_key="e-1-s",
                title="t\n" * 80,
                commit="abc123",
                verify_line="v",
                tokens="0",
            )
        )
        await until(pilot, lambda: isinstance(app.screen, StoryCheckpointModal))
        body = await ready(pilot, "#body")
        assert body.max_scroll_y > 0  # the long title overflows and the body scrolls
        assert _on_screen(app, app.screen.query_one("#act-continue", Button))
        assert _on_screen(app, app.screen.query_one("#cancel", Button))


async def test_short_confirm_modal_stays_compact(project_tree):
    """The bounded modals keep BaseDialog #dialog at height: auto on purpose, so a
    short body sizes to content instead of filling the screen. Guards the compact
    tier against a definite `#dialog` height (#280): on a tall terminal a one-line
    confirm must stay a handful of rows, not balloon to the 90% cap — a definite
    height takes this modal from 7 rows to 23."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(64, 40)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(ConfirmModal("t", "Stop the run?"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        dialog = await ready(pilot, "#dialog")
        # content-driven height, nowhere near the 90% cap (36 rows at this size)
        assert dialog.region.height < 12


async def test_escalation_rearm_warning_stays_on_screen(project_tree):
    """When a restore patch is recorded the escalation warns that Re-arm re-drives
    from scratch and drops it. Re-arm is enabled, so that warning must stay docked
    on-screen (a sibling of #body) even when a long description scrolls the body
    (#280 review — the warning must not be reachable-only by scrolling)."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(90, 16)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(
            EscalationModal(
                story_key="e-1-s",
                title="t",
                description="X\n" * 80,
                blocking="b",
                sentinel_kind="",
                resolution_ready=True,
                engine_live=False,
                restore_recorded=True,
            )
        )
        await until(pilot, lambda: isinstance(app.screen, EscalationModal))
        body = await ready(pilot, "#body")
        assert body.max_scroll_y > 0  # the long description overflows and scrolls
        rearm = app.screen.query_one("#act-rearm", Button)
        assert not rearm.disabled  # the destructive action is clickable...
        assert _on_screen(app, rearm)
        assert _on_screen(
            app, app.screen.query_one("#hint", Static)
        )  # ...so its warning is visible


async def test_resume_confirm_rechecks_liveness(project_tree, monkeypatch):
    """The resume confirm callback re-checks engine liveness at click time rather
    than launching blind: with a possibly-live engine (unknown liveness + a pid),
    confirming resume is refused and never calls resume_detached (#280 review)."""
    calls: list = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "unknown")
    run_dir = make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="DEV_VERIFY",
        paused_reason="verify failed",
    )
    (run_dir / "engine.pid").write_text("4242 123.0", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any("may still be live" in m for m in notifications(app)))
        assert calls == []  # the callback re-checked and refused; nothing launched


@pytest.mark.parametrize("blocked", ["textual", "rich", "tomlkit", "pyte"])
def test_cli_tui_hint_without_extra_dependency(project_tree, monkeypatch, capsys, blocked):
    """`bmad-loop tui` prints the install hint whichever `[tui]` dependency is missing.

    The guard is failure-gated rather than allowlisted (#678): `rich` and `pyte`
    import *before* `textual` on the TUI chain, so an allowlist naming only textual
    and tomlkit let those two escape as a traceback.

    Evicting the whole `bmad_loop.tui.*` subtree is load-bearing, not tidiness: the
    rich/pyte/tomlkit chains run through `tui.data`/`tui.settings`/`tui.screens.*`,
    which this file's own module-level imports have already cached, and a cached
    module returns without re-executing -- no third-party import would ever fire.

    INVERSE ablation: restore the ("textual", "tomlkit") allowlist and the rich/pyte
    params redden -- the error escapes to main's broad backstop as "No module named
    'rich.text'" / "No module named 'pyte'" with no hint, while textual/tomlkit stay
    green (rc stays 1 either way, which is why the hint is the assertion that matters).
    """
    import builtins
    import sys

    import bmad_loop
    from bmad_loop import cli

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.partition(".")[0] == blocked:
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return real_import(name, *args, **kwargs)

    # Evicting the subtree alone leaks: re-importing `bmad_loop.tui` rebinds the
    # `tui` attribute on the *parent package object* to the new (doomed) module, and
    # restoring sys.modules does not undo that rebinding. Pin the attribute through
    # monkeypatch so the original comes back with it -- otherwise every later
    # `monkeypatch.setattr("bmad_loop.tui.app....")` in this file resolves against a
    # package that no longer has an `app` attribute.
    monkeypatch.setattr(bmad_loop, "tui", sys.modules["bmad_loop.tui"])
    for mod in [m for m in sys.modules if m == "bmad_loop.tui" or m.startswith("bmad_loop.tui.")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    rc = cli.main(["tui", "--project", str(project_tree.project)])
    assert rc == 1
    assert "bmad-loop[tui]" in capsys.readouterr().err


async def test_settings_binding_opens_editor(project):
    """g opens the settings screen (template-backed when no policy.toml) and
    escape returns; editor behavior itself lives in test_tui_settings.py."""
    from bmad_loop.tui.screens.settings_screen import SettingsScreen

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("g")
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
        await pilot.press("g")  # no double-push
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))


# ---- #281 modal dialogs shrink to fit narrow terminals (horizontal axis)
#
# Measured minimum terminal WIDTH at which every docked button is fully
# on-screen — before this fix / after it: DecisionModal 83 -> 12,
# EscalationModal 87 -> 39, ConfirmModal 61 -> 22, StartSweepModal 61 -> 20,
# StoryCheckpointModal 61 -> 37. EscalationModal's 87 means a standard
# 80-column terminal clipped it.


async def test_decision_modal_clamps_to_narrow_terminal(project_tree):
    """A 50-column terminal is narrower than DecisionModal's declared width: 86.
    `max-width: 100%` on the shared BaseDialog #dialog rule clamps it to the
    screen, so the docked skip button is reachable instead of being laid out past
    the right edge (#281). The width assertion pins the clamp; the reachability
    assertion is what the user actually feels."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(50, 30)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(DecisionModal(_long_decision()))
        await until(pilot, lambda: isinstance(app.screen, DecisionModal))
        await ready(pilot, "#body")
        # clamped to the terminal, not laid out at its declared 86 columns
        assert app.screen.query_one("#dialog").region.width == 50
        assert _on_screen(app, app.screen.query_one("#cancel", Button))


async def test_escalation_modal_three_buttons_reachable_when_narrow(project_tree):
    """The three-button escalation row at 45 columns — the case the clamp alone
    does NOT fix, so this is the test that earns the `-narrow` rule.

    At 45 columns the clamped dialog has 45 - 2 (thick border) - 4 (padding) = 39
    columns of content, while Textual's default `Button min-width: 16` plus
    BaseDialog's `margin-left: 2` demands 3*16 + 3*2 = 54 for three buttons — the
    row overflows and the right-most button is clipped. Measured: with the clamp
    but WITHOUT `BaseDialog.-narrow .buttons Button`, this modal still needs 58
    columns. So this test covers the `-narrow` rule, not merely `max-width` —
    deleting that rule must redden this test, and T1 does not cover it."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(45, 30)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(
            EscalationModal(
                story_key="e-1-s",
                title="t",
                description="d",
                blocking="b",
                sentinel_kind="",
                resolution_ready=True,
                engine_live=False,
                restore_recorded=True,
            )
        )
        await until(pilot, lambda: isinstance(app.screen, EscalationModal))
        await ready(pilot, "#body")
        for bid in ("#act-resolve", "#act-rearm", "#cancel"):
            assert _on_screen(app, app.screen.query_one(bid, Button)), bid


async def test_story_checkpoint_three_buttons_reachable_when_narrow(project):
    """The other three-button row, same 45-column bound as the escalation case:
    measured at 57 columns with the clamp alone, so this too rests on `-narrow`."""
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(45, 30)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(
            StoryCheckpointModal(
                story_key="e-1-s",
                title="t",
                commit="abc123",
                verify_line="v",
                tokens="0",
            )
        )
        await until(pilot, lambda: isinstance(app.screen, StoryCheckpointModal))
        await ready(pilot, "#body")
        for bid in ("#act-continue", "#act-stop", "#cancel"):
            assert _on_screen(app, app.screen.query_one(bid, Button)), bid


async def test_wide_terminal_dialog_width_unchanged(project_tree):
    """The clamp must not shrink a dialog that already fits: at 120 columns a
    ConfirmModal still lays out at its declared 64, and 120 is above the 60-column
    `-narrow` breakpoint so the button row keeps today's sizing. Guards the fix
    against becoming a visible regression for normal-width terminals (#281)."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 30)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(ConfirmModal("t", "body"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await ready(pilot, "#body")
        assert app.screen.query_one("#dialog").region.width == 64
        assert "-narrow" not in app.screen.classes  # the breakpoint did not engage


# ---- #281 modal dialogs degrade to a compact layout on short terminals
#      (vertical axis)
#
# Measured minimum terminal HEIGHT at which a modal's title, one row of body and
# every docked control (buttons + any docked warning) are fully on-screen —
# before this fix / after it, at 90-100 columns: ConfirmModal 12 -> 4,
# ConfirmModal-with-a-warning 14 -> 5, StartSweepModal 13 -> 4,
# StoryCheckpointModal 13 -> 4, EscalationModal 12 -> 6, DecisionModal 9 -> 4.
# EscalationModal's floor is width-dependent because its #hint warning wraps:
# at 39 columns (phase 1's narrowest measured width) it is 15 -> 9, which is the
# 39x9 pair docs/tui-guide.md records as measured, not as a minimum.


async def test_compact_layout_makes_a_short_terminal_usable(project_tree):
    """The payoff test for the vertical axis: at 8 rows a ConfirmModal with a
    docked warning is fully operable.

    8 is well BELOW this exact modal's pre-fix floor of 14 rows (measured at 64
    columns, the same modal and body), so a green assertion here cannot be
    explained by a roomy terminal — it is the `-short` compact layout doing the
    work. Post-fix the same modal bottoms out at 5 rows. The warning is included
    on purpose: it gates a destructive confirm (ConfirmResumeModal inherits it),
    is docked outside #body, and is what pushes this modal's floor above the
    other bounded modals'."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(64, 8)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(ConfirmModal("t", "line\n" * 80, warning="w"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await ready(pilot, "#body")
        assert _on_screen(app, app.screen.query_one("#ok", Button))
        assert _on_screen(app, app.screen.query_one("#cancel", Button))
        assert _on_screen(app, app.screen.query_one("#warning", Static))


async def test_short_breakpoint_engages_only_below_the_threshold(project_tree):
    """Pins the mechanism itself, so deleting `VERTICAL_BREAKPOINTS` fails loudly
    instead of drifting the layout: Textual puts the matching class on the Screen,
    and BaseDialog IS a ModalScreen, so `-short`/`-tall` land on the dialog screen
    where the CSS selects them. 19 and 20 are the two sides of the threshold."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(64, 19)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(ConfirmModal("t", "Stop the run?"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await ready(pilot, "#body")
        assert "-short" in app.screen.classes
        assert "-tall" not in app.screen.classes

    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(64, 40)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(ConfirmModal("t", "Stop the run?"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await ready(pilot, "#body")
        assert "-tall" in app.screen.classes
        assert "-short" not in app.screen.classes


async def test_tall_terminal_dialog_height_unchanged(project_tree):
    """The compact rules must not leak upward. At 40 rows a one-line confirm lays
    out at exactly 11 — 2 border + 2 padding + 1 title + 1 title margin + 1 body
    + 1 button-row margin + 3 button — which is what it measures both with and
    without this fix. `test_short_confirm_modal_stays_compact` does NOT cover
    this: it asserts `< 12`, and a leaked `-short` (which takes this dialog to 5)
    would satisfy that bound too. The equality is the point."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(64, 40)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(ConfirmModal("t", "Stop the run?"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await ready(pilot, "#body")
        assert app.screen.query_one("#dialog").region.height == 11


# ---- #281 the measured 39x9 pair, where BOTH breakpoints are engaged
#
# Everything above exercises one axis at a time. This is the corner where
# `-narrow` and `-short` apply together, and it is the only test of the pair
# docs/tui-guide.md records — so a rule that is correct on each axis alone but
# wrong at the intersection reddens here and nowhere else. It also keeps the
# published figure honest: it is asserted for every dialog it covers, so a modal
# cannot quietly stop meeting the size the guide says it was measured at.
#
# Caller text cannot move the pair. Titles, headers, subtitles and paths dock
# OUTSIDE the scrolling body, and each of their lines is held to one row with an
# ellipsis (BaseDialog `.title`; the SpecReviewModal and PauseReasonModal
# subtitles; `_TailPath`), so a docked block costs its LINE count in rows, never
# its length (DW-358). Docked warnings and hints are module text and are NOT so
# held: they are sized to fit instead — every EscalationModal hint arm stays
# within 3 rows at 35 columns, and ConfirmResumeModal's `-short` body yields
# its rows to the warning (DW-414, DW-415). The spec viewer's action row wraps
# into a two-column grid below 80 columns instead of clipping its right-most
# buttons (DW-359). The rows below pin each face:
#
# - every covered dialog at 39x9, including the spec viewer (long subtitle and
#   path), the pause-reason viewer (long subtitle), the validate viewer, every
#   escalation hint arm and the resume confirmation's double-drive warning over
#   a long pause reason: title, one body row and every docked control visible;
# - a 300- and 2000-character ledger heading at 39x9: a one-row `.title`, and
#   the done/legacy markers on lines of their own;
# - a 63- and 300-character validate spec folder at 39x9: a three-row header
#   that still holds the full folder;
# - the spec viewer at 39/43/59/60/79 columns (grid, whole 3-row buttons) and at
#   80 (one row, no `-narrow`), with a 300-character path;
# - `_TailPath` keeping the file name when the path is cut, and the whole path
#   when it fits.
#
# Visibility is asserted with `_fully_visible`, which reads the compositor's
# clip: a widget cut by `#dialog` is still inside the SCREEN, so `_on_screen`
# alone passes for a button the dialog has already clipped away.

_MIN_COLS, _MIN_ROWS = 39, 9


def _fully_visible(app, w) -> bool:
    """A widget's region is non-empty and entirely visible — the compositor's
    clipped region (screen AND every ancestor container, `#dialog` included)
    equals its laid-out region, so nothing of it is cut off."""
    r = w.region
    return r.width > 0 and r.height > 0 and app.screen.find_widget(w).visible_region == r


_LONG_SPEC_PATH = "/" + "/".join(["a-long-directory-name"] * 13) + "/spec-epic-1-story-2.md"
assert len(_LONG_SPEC_PATH) > 290

_PLAN_CHECKPOINT_ACTIONS = [
    ("approve", "Approve & resume", "primary"),
    ("replan", "Request replan", "warning"),
]

_LONG_SUBTITLE = ("a long stories.yaml story title " * 10).strip()
assert len(_LONG_SUBTITLE) > 290

_MIN_SIZE_CASES = (
    "confirm",
    "confirm-warning",
    # `confirm-warning` passes a one-character warning; this is the real
    # double-drive warning, which wraps to three rows at 39 columns (DW-415)
    "confirm-resume-warning",
    "start-run",
    "start-sweep",
    "decision",
    "deferred-entry",
    "story-checkpoint",
    # One row per `#hint` arm (DW-414): at 39 columns the three-button row must
    # fit 35 content columns, and 2 border + title + 1 body row + hint + 1 button
    # must fit the 8-row (90%-of-9) dialog, so no arm may pass 3 rows.
    "escalation",
    "escalation-unreadable",
    "escalation-ready",
    "escalation-plain",
    "pause-reason",
    "text-output",
    "spec-review",
    "validate-findings",
)


def _minimum_size_case(name: str, project):
    """(modal, docked controls that must stay reachable, its scrolling body).

    The controls are the ones docked OUTSIDE the body — buttons plus any warning
    docked beside them — because those are what the doc promises stay on-screen.
    `DecisionModal`'s per-option `opt-N` buttons are deliberately not listed:
    they live inside the scrolling `#body` and are reached by scrolling, which
    test_decision_modal_scrolls_when_content_long already covers."""
    if name == "confirm":
        return ConfirmModal("t", "line\n" * 80), ("#ok", "#cancel"), "#body"
    if name == "confirm-warning":
        # the docked warning gates a destructive confirm (ConfirmResumeModal
        # inherits it), so losing it off-screen is a safety defect, not cosmetic
        return (
            ConfirmModal("t", "line\n" * 80, warning="w"),
            ("#ok", "#cancel", "#warning"),
            "#body",
        )
    if name == "confirm-resume-warning":
        # the real modal and its real warning, over a pause reason far longer
        # than the body's rows, so the body has to scroll rather than push the
        # warning and buttons out of the dialog
        state = RunState(
            run_id="r1",
            project=str(project.project),
            started_at="now",
            paused_stage="dev",
            paused_reason="a long pause reason " * 20,
        )
        return (
            ConfirmResumeModal("r1", state, engine_alive=True),
            ("#ok", "#cancel", "#warning"),
            "#body",
        )
    if name == "start-run":
        return StartRunModal(project.project), ("#ok", "#cancel"), "#fields"
    if name == "start-sweep":
        return StartSweepModal(), ("#ok", "#cancel"), "#body"
    if name == "decision":
        return DecisionModal(_long_decision()), ("#cancel",), "#body"
    if name == "deferred-entry":
        item = data.DeferredItem(
            id="DW-1",
            title="a deferred item",
            status="open",
            done=False,
            severity="high",
            body="line\n" * 40,
        )
        return DeferredEntryModal(item), ("#ok",), "#entry"
    if name == "story-checkpoint":
        return (
            StoryCheckpointModal(
                story_key="e-1-s", title="t", commit="abc123", verify_line="v", tokens="0"
            ),
            ("#act-continue", "#act-stop", "#cancel"),
            "#body",
        )
    if name.startswith("escalation"):
        # `escalation` is the restore-patch arm, the one that gates an enabled
        # Re-arm; the suffixed rows are the other three arms of the same hint
        arm = {
            "escalation": {"resolution_ready": True, "restore_recorded": True},
            "escalation-unreadable": {"resolution_ready": False, "unreadable": True},
            "escalation-ready": {"resolution_ready": True},
            "escalation-plain": {"resolution_ready": False},
        }[name]
        return (
            EscalationModal(
                story_key="e-1-s",
                title="t",
                description="d",
                blocking="b",
                sentinel_kind="",
                engine_live=False,
                **arm,
            ),
            ("#act-resolve", "#act-rearm", "#cancel", "#hint"),
            "#body",
        )
    if name == "pause-reason":
        return (
            PauseReasonModal(title="t", subtitle=_LONG_SUBTITLE, reason="line\n" * 80),
            ("#act-resume", "#cancel"),
            "#reason",
        )
    if name == "spec-review":
        # the widest action set the modal is given, and a path far longer than
        # the row: both faces of this dialog's floor at once
        return (
            SpecReviewModal(
                title="review the spec",
                # a multi-line stories.yaml title: two lines, one row
                subtitle=_LONG_SUBTITLE + "\nsecond line",
                spec_path=Path(_LONG_SPEC_PATH),
                spec_text="line\n" * 40,
                actions=_PLAN_CHECKPOINT_ACTIONS,
            ),
            ("#copy-path", "#act-approve", "#act-replan", "#cancel"),
            "#spec",
        )
    if name == "validate-findings":
        return (
            ValidateFindingsModal(
                make_validate_document([("bmad-config", "problem", "a finding", None)])
            ),
            ("#ok",),
            "#findings",
        )
    assert name == "text-output", name
    return TextOutputModal("validate", 0, "out\n" * 40), ("#ok",), "#output"


async def test_resume_confirm_without_warning_stays_compact_on_a_short_terminal(project_tree):
    """DW-415's `-short` fix gives the WARNED resume confirm a `1fr` body, which
    grows the auto dialog to the full screen. Without the warning it is a
    bounded-tier confirm that already fits, so it must keep its content height
    rather than balloon to the 15 rows available here."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(64, 15)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        state = RunState(run_id="r1", project=str(project_tree.project), started_at="now")
        modal = ConfirmResumeModal("r1", state, engine_alive=False)
        app.push_screen(modal)
        await until(pilot, lambda: app.screen is modal)
        await ready(pilot, "#body")
        assert "-short" in app.screen.classes
        # 2 border + title + 2 body rows + 1-row button row
        assert app.screen.query_one("#dialog").region.height == 6


@pytest.mark.parametrize("case", _MIN_SIZE_CASES)
async def test_measured_terminal_size_keeps_dialogs_operable(project_tree, case):
    """At the 39x9 pair the guide records as measured, every covered dialog still
    shows its title, a row of body and all of its docked controls, fully — not
    merely inside the screen but unclipped by `#dialog` too (#281, DW-358,
    DW-359).

    Both breakpoint classes are asserted present first, so the test fails loudly
    if a future threshold change means this size no longer exercises the compact
    layout at all — otherwise the assertions below could pass for the wrong
    reason, on a dialog that simply never engaged either rule."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(_MIN_COLS, _MIN_ROWS)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        modal, controls, body = _minimum_size_case(case, project_tree)
        app.push_screen(modal)
        await until(pilot, lambda: app.screen is modal)
        await ready(pilot, body)
        assert "-narrow" in app.screen.classes, case
        assert "-short" in app.screen.classes, case
        assert _fully_visible(app, app.screen.query(".title").first()), case
        body_widget = app.screen.query_one(body)
        assert app.screen.find_widget(body_widget).visible_region.height >= 1, case
        # inside any frame too: a bordered body can fill its slot with border alone
        assert body_widget.content_size.height >= 1, case
        for selector in controls:
            assert _fully_visible(app, app.screen.query_one(selector)), f"{case} {selector}"


_SPEC_REVIEW_ACTION_SETS = pytest.mark.parametrize(
    ("actions", "controls"),
    [
        (
            [("resume", "Approve & resume", "primary")],
            ("#copy-path", "#act-resume", "#cancel"),
        ),
        (
            _PLAN_CHECKPOINT_ACTIONS,
            ("#copy-path", "#act-approve", "#act-replan", "#cancel"),
        ),
    ],
    ids=["gate", "plan-checkpoint"],
)


def _spec_review_modal(actions, spec_path=_LONG_SPEC_PATH) -> SpecReviewModal:
    return SpecReviewModal(
        title="review the spec",
        # a long, multi-line stories.yaml title: `.subtitle` must still be 1 row
        subtitle=_LONG_SUBTITLE + "\nsecond line",
        spec_path=Path(spec_path),
        spec_text="line\n" * 40,
        actions=actions,
    )


@_SPEC_REVIEW_ACTION_SETS
async def test_spec_review_modal_operable_on_a_standard_terminal(project_tree, actions, controls):
    """At 80 columns the widest action row — copy path + Approve & resume +
    Request replan + close, 74 columns with margins — exactly fills the dialog's
    content region (80 - 2 border - 4 padding), so the modal keeps its one-row
    layout: no `-narrow`, every button on the same row, all fully visible, with
    a 300-character spec path held to one row above the body (DW-358, DW-359).
    One column less and the row would clip, which is what the narrow-width test
    below pins."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(80, 24)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        modal = _spec_review_modal(actions)
        app.push_screen(modal)
        await until(pilot, lambda: app.screen is modal)
        await ready(pilot, "#spec")
        assert "-narrow" not in app.screen.classes
        assert app.screen.query_one(".path").region.height == 1
        rows = {app.screen.query_one(selector).region.y for selector in controls}
        assert len(rows) == 1, rows
        for selector in controls:
            assert _fully_visible(app, app.screen.query_one(selector)), selector


@_SPEC_REVIEW_ACTION_SETS
@pytest.mark.parametrize("rows", [20, 24])
@pytest.mark.parametrize("cols", [_MIN_COLS, 43, 59, 60, 79])
async def test_spec_review_modal_action_row_wraps_below_80_columns(
    project_tree, actions, controls, cols, rows
):
    """Below 80 columns the one-row action row cannot fit (see the 80x24 test),
    so the modal's own `-narrow` turns `.buttons` into a two-column grid (DW-359).
    43 is where the longest label first fits a half-width cell; 59 and 60
    straddle BaseDialog's threshold, which this modal overrides; 79 is one column
    under its own.

    The buttons must actually sit in two rows of at most two — a one-row
    `1fr` strip would also keep every button visible, just too narrow to read.
    Each button must keep its full 3-row height: a label that wrapped would make
    its grid row taller and push the next row off the dialog, so the height is
    the check that the labels stay on one line. From 43 columns a half-width
    cell holds the longest label whole, so there every label is asserted
    unclipped; below it "Approve & resume" may lose its tail to `…`, which is
    accepted — the button stays whole and operable.

    20 rows is the shortest terminal without `-short`: the grid's second row of
    3-row buttons must still fit, with a row of spec showing.

    Ablations: delete the `SpecReviewModal.-narrow .buttons` grid rule and every
    row here fails on the two-row assertion; drop `.subtitle` from the modal's
    one-row selector, or that rule's `max-height: 1`, and rows fail on the
    subtitle height; drop `-narrow` from the modal's `height: 100%` selector and
    the 20-row cases fail."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(cols, rows)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        modal = _spec_review_modal(actions)
        app.push_screen(modal)
        await until(pilot, lambda: app.screen is modal)
        await ready(pilot, "#spec")
        assert "-narrow" in app.screen.classes
        assert "-short" not in app.screen.classes
        for selector in (".title", ".subtitle", ".path"):
            widget = app.screen.query_one(selector)
            assert widget.region.height == 1, selector
            assert _fully_visible(app, widget), selector
        assert app.screen.query_one("#spec").content_size.height >= 1, (cols, rows)
        buttons = [app.screen.query_one(selector, Button) for selector in controls]
        rows = sorted({button.region.y for button in buttons})
        assert len(rows) == 2, (cols, rows)
        for row in rows:
            assert sum(button.region.y == row for button in buttons) <= 2, (cols, row)
        for selector, button in zip(controls, buttons):
            assert _fully_visible(app, button), f"{cols} {selector}"
            assert button.region.height == 3, f"{cols} {selector}"
            if cols >= 43:
                label = str(button.label)
                assert button.content_size.width >= len(label), f"{cols} {selector}"


@pytest.mark.parametrize(
    ("cols", "path"),
    [(_MIN_COLS, _LONG_SPEC_PATH), (80, "/specs/spec-epic-1-story-2.md")],
    ids=["cut-keeps-file-name", "fits-whole"],
)
async def test_spec_review_path_keeps_its_file_name(project_tree, cols, path):
    """`text-overflow: ellipsis` cuts a path's END, which is its file name — the
    part that says which spec this is. `_TailPath` cuts the head instead: a path
    wider than its row renders as `…` plus a tail that fills the row exactly and
    ends in the file name, and a path that fits renders whole (DW-358).

    Ablation: make `_TailPath.render` return the whole path unconditionally and
    the cut row fails on the missing leading `…`."""
    path = str(Path(path))  # the modal takes a `Path`: platform separators
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(cols, 24)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        modal = _spec_review_modal([("resume", "Approve & resume", "primary")], path)
        app.push_screen(modal)
        await until(pilot, lambda: app.screen is modal)
        await ready(pilot, "#spec")
        widget = app.screen.query_one(".path")
        assert str(widget.content) == path  # the cut is render-only
        rendered = str(widget.render())
        width = widget.content_size.width
        if len(path) > width:
            assert rendered.startswith("…"), rendered
            assert rendered.endswith(f"{os.sep}spec-epic-1-story-2.md"), rendered
            assert len(rendered) == width, (rendered, width)
            assert path.endswith(rendered[1:])
        else:
            assert rendered == path


@pytest.mark.parametrize("chars", [63, 300])
async def test_validate_findings_modal_fixed_floor_with_long_spec_folder(project_tree, chars):
    """`.title` is `widgets.validate_header(doc)`: a verdict line, a meta line
    carrying the user-controlled `spec: <spec_folder>`, and — a problem being
    present — the dim gates footer. Each line is held to one row, so the header
    is three rows whatever the folder's length and the dialog meets the 39x9 pair
    like every other (DW-358). The ellipsis is render-only: the widget's content
    still holds the whole folder.

    Ablations: delete BaseDialog `.title`'s `text-wrap: nowrap` and both rows fail
    on the header height; delete `ValidateFindingsModal.-short #dialog`'s
    `height: 100%` and both fail on `#ok`, clipped by the 7-row dialog."""
    folder = ("docs/specs/epics/epic-1/stories/generated/" * 10)[: chars - 1] + "x"
    assert len(folder) == chars
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(_MIN_COLS, _MIN_ROWS)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        modal = ValidateFindingsModal(
            make_validate_document(
                [("bmad-config", "problem", "a finding", None)],
                stories_on=True,
                spec_folder=folder,
            )
        )
        app.push_screen(modal)
        await until(pilot, lambda: app.screen is modal)
        await ready(pilot, "#findings")
        header = app.screen.query(".title").first()
        assert header.region.height == 3
        assert _fully_visible(app, header)
        assert _fully_visible(app, app.screen.query_one("#ok"))
        assert folder in str(header.content)


@pytest.mark.parametrize("chars", [300, 2000])
async def test_long_docked_title_holds_one_row(project_tree, chars):
    """`DeferredEntryModal` renders the ledger heading as the docked `.title`,
    outside the scrolling `#entry`, and `parse_ledger` does not bound that text.
    Unbounded, a ~300-character heading wrapped to nine rows at 39 columns and
    pushed `#ok` off a nine-row screen. Held to one row it cannot: the title is
    one row and fully visible, `#ok` too, at the 39x9 pair (DW-358). The full
    heading stays in the widget, and the scrolling body repeats it.

    Ablation: delete BaseDialog `.title`'s `text-wrap: nowrap` and both rows fail
    on the title height."""
    title = ("word " * 500)[:chars].strip()
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(_MIN_COLS, _MIN_ROWS)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        modal = DeferredEntryModal(
            data.DeferredItem(
                id="DW-1",
                title=title,
                status="open",
                done=False,
                severity="high",
                body="line\n" * 40,
            )
        )
        app.push_screen(modal)
        await until(pilot, lambda: app.screen is modal)
        await ready(pilot, "#entry")
        heading = app.screen.query(".title").first()
        assert heading.region.height == 1
        assert _fully_visible(app, heading)
        assert _fully_visible(app, app.screen.query_one("#ok"))
        assert title in str(heading.content)


async def test_long_docked_title_keeps_its_markers(project_tree):
    """The `✓ done` and legacy markers used to follow the heading on its line, so
    a heading long enough to ellipsize cut them away — and they appear nowhere
    else. Each now opens a line of its own in the title, so at 39x9 the rendered
    title rows show both (DW-358). Read off the rendered strips, not `.content`:
    the content always held them; the question is whether they reach the screen.

    Ablation: append the markers after the heading on its line again and this
    test fails on the missing `✓ done`."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(_MIN_COLS, _MIN_ROWS)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        modal = DeferredEntryModal(
            data.DeferredItem(
                id="DW-1",
                title=("word " * 60).strip(),
                status="done",
                done=True,
                severity="high",
                body="line\n" * 40,
                legacy=True,
            )
        )
        app.push_screen(modal)
        await until(pilot, lambda: app.screen is modal)
        await ready(pilot, "#entry")
        heading = app.screen.query(".title").first()
        assert heading.region.height == 3
        assert _fully_visible(app, heading)
        assert _fully_visible(app, app.screen.query_one("#ok"))
        size = heading.size
        rows = [strip.text for strip in heading.render_lines(Region(0, 0, size.width, size.height))]
        assert any("✓ done" in row for row in rows), rows
        assert any("· legacy" in row for row in rows), rows


# ------------------------------------------------------------- run control


async def test_start_run_modal_escape_cancels(project_tree, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        assert not calls


async def test_start_run_modal_launches(project, monkeypatch):
    calls = {}
    monkeypatch.setattr(launch, "mux_available", lambda: True)

    def fake_start(proj, run_id, *, spec=None, epic, story, max_stories):
        calls.update(
            project=proj, run_id=run_id, spec=spec, epic=epic, story=story, max_stories=max_stories
        )

    monkeypatch.setattr(launch, "start_run_detached", fake_start)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await ready(pilot, "#ok")
        app.screen.query_one("#epic", Input).value = "2"
        app.screen.query_one("#max-stories", Input).value = "3"
        await pilot.click("#ok")
        await until(pilot, lambda: bool(calls))
        assert calls["project"] == project.project
        assert calls["epic"] == 2
        assert calls["story"] is None
        assert calls["max_stories"] == 3
        screen = dashboard(app)
        # the launched run is pre-selected and shown as starting
        assert screen._pending_run == calls["run_id"]
        assert screen.selected_run_id == calls["run_id"]
        await until(
            pilot,
            lambda: "starting" in str(screen.query_one("#runheader", RunHeader).content),
        )


async def test_dirty_worktree_blocks_launch(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    (project.project / "src.txt").write_text("dirty\n")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any("not clean" in m for m in notifications(app)))
        assert not calls


def _split_root_tui_project(project, shape="inside"):
    """The #414 pair, written where the guard reads them: `isolation = "worktree"`
    beside a DISJOINT `repo_root` — inside the project (``git-root``, created so the
    clean-tree probe of it answers rather than raising) or a sibling git checkout.
    Deliberately left UNCOMMITTED: the guard is ordered ahead of the clean-tree gate
    exactly as `cmd_run` orders it, and a committed fixture could not tell the two
    orders apart."""
    install_bmad_config(project)
    if shape == "sibling":
        code_root = project.project.parent / f"{project.project.name}-code"
        code_root.mkdir()
        git(code_root, "init", "-q")
        spelled = code_root.as_posix()
    else:
        (project.project / "git-root").mkdir()
        spelled = "{project-root}/git-root"
    cfg = project.project / "_bmad" / "bmm" / "config.yaml"
    cfg.write_text(cfg.read_text() + f"repo_root: '{spelled}'\n", encoding="utf-8")
    (project.project / ".bmad-loop").mkdir(parents=True, exist_ok=True)
    (project.project / ".bmad-loop" / "policy.toml").write_text(
        '[adapter]\nname = "claude"\n\n[scm]\nisolation = "worktree"\n', encoding="utf-8"
    )


@pytest.mark.parametrize("shape", ["inside", "sibling"])
async def test_worktree_isolation_under_a_repo_root_override_blocks_launch(
    project, monkeypatch, shape
):
    """#414: the TUI launches a detached CLI, and that CLI refuses this combination
    itself — this guard exists so the operator gets a toast instead of a pane that
    dies immediately. Asserted against the sole producer of the text rather than a
    literal, so a reworded message cannot drift this test away from the CLI's. Both
    disjoint layouts refuse."""
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    _split_root_tui_project(project, shape)
    expected = bmadconfig.worktree_isolation_conflict(
        bmadconfig.load_paths(project.project), "worktree"
    )
    assert expected is not None, "the fixture really does carry the conflicting pair"

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: expected in notifications(app))
        # The tree is dirty, so this also pins the ORDER: the clean-tree gate would
        # otherwise have spoken first and sent the operator to commit something
        # that is not the problem.
        assert not any("not clean" in m for m in notifications(app))
        assert not calls


async def test_unreadable_policy_falls_through_the_isolation_guard(project, monkeypatch):
    """The guard's deliberate blind spot, and the one branch where a wrong `except`
    tuple silently disables it. It cannot tell "no conflict" from "could not look",
    so it defers to the detached CLI, which reads the same two files and fails
    loudly on whichever it cannot parse. The bytes here are undecodable rather than
    merely malformed: `read_text` raises `UnicodeDecodeError`, which is a ValueError
    and NOT an OSError, so it escapes the obvious tuple and would take the TUI down
    instead of launching.

    Committed, unlike the sibling above: this one asserts the launch actually
    HAPPENS, so the clean-tree gate downstream has to be satisfied or it would
    block for an unrelated reason and the fall-through would go unwitnessed."""
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    _split_root_tui_project(project)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "split roots")
    (project.project / ".bmad-loop" / "policy.toml").write_bytes(b'[scm]\nisolation = "\xff\xfe"\n')

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: calls)
        assert not any("isolation" in m for m in notifications(app))


async def test_worktree_isolation_beside_a_nested_repo_root_launches(project, monkeypatch):
    """DW-379: the nested layout (`<repo>/app`, `repo_root` the checkout) is supported,
    so the guard lets the launch through — graded on the detached launch happening.

    Ablation: widen `worktree_isolation_conflict` back to "any `repo_root` override"
    and the guard toasts the refusal instead of launching."""
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    paths = nested_repo_root_paths(project)
    (paths.project / ".bmad-loop").mkdir(parents=True, exist_ok=True)
    (paths.project / ".bmad-loop" / "policy.toml").write_text(
        '[adapter]\nname = "claude"\n\n[scm]\nisolation = "worktree"\n', encoding="utf-8"
    )
    assert verify.worktree_clean(paths.repo_root, project=paths.project), "premise: clean"

    app = BmadLoopApp(paths.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: calls)
        assert not any("needs the project directory" in m for m in notifications(app))


async def test_a_nested_projects_policy_edit_does_not_block_launch(project, monkeypatch):
    """The launch guard's clean probe of `repo_root` excludes the PROJECT's policy.toml
    at its offset (`app/.bmad-loop/policy.toml`), as run/sweep/validate do, so a
    settings edit under nested roots still launches.

    Ablation: drop `project=` from `_guarded`'s `worktree_clean` call and the guard
    toasts "not clean" instead of launching."""
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    paths = nested_repo_root_paths(project)
    policy_file = paths.project / ".bmad-loop" / "policy.toml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text('[adapter]\nname = "claude"\n', encoding="utf-8")
    git(paths.repo_root, "add", "-f", "app/.bmad-loop/policy.toml")
    git(paths.repo_root, "commit", "-qm", "track the nested policy")
    policy_file.write_text(policy_file.read_text() + "# edited\n", encoding="utf-8")
    assert not verify.worktree_clean(paths.repo_root), "premise: the edit is a real change"

    app = BmadLoopApp(paths.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: calls)
        assert not any("not clean" in m for m in notifications(app))


async def test_a_dirty_code_root_blocks_launch_under_a_nested_project(project, monkeypatch):
    """The launch guard probes the CODE root, as `cmd_run`/`cmd_sweep` do (DW-379): in
    the nested monorepo layout the project `app/` is clean while a tracked file OUTSIDE
    it — in the checkout the run's git work happens in — is dirty, and the guard must
    refuse exactly as the detached CLI's `worktree_clean(paths.repo_root)` would.

    Ablation: probe `self.project` in `_guarded` instead of the loaded `repo_root` and
    this fails — `worktree_clean` is pathspec-scoped to its cwd, so `app/` reads clean
    and the launch goes through."""
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    paths = nested_repo_root_paths(project)
    outer = paths.repo_root / "src.txt"
    assert git(paths.repo_root, "ls-files", "--error-unmatch", "src.txt"), "premise: tracked"
    outer.write_text("dirty outside the project\n", encoding="utf-8")
    assert verify.worktree_clean(paths.project), "premise: the project itself is clean"
    assert not verify.worktree_clean(paths.repo_root)

    app = BmadLoopApp(paths.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any("not clean" in m for m in notifications(app)))
        assert not calls


def _fake_tui_git_version(monkeypatch, reported=None, *, boom=None):
    """Answer `git version` at the `git_bytes` seam and pass every other git call
    through to the real one. Both halves are load-bearing: `_commit_subject` shares
    this seam, and the guard's own clean-tree gate has to keep working or a blocked
    launch could be blocked for the wrong reason."""
    real = verify.git_bytes

    def fake(repo, *args, timeout_s=None):
        if args == ("version",):
            if boom is not None:
                raise boom
            return subprocess.CompletedProcess(
                args=["git", "version"], returncode=0, stdout=reported.encode(), stderr=b""
            )
        return real(repo, *args, timeout_s=timeout_s)

    monkeypatch.setattr(verify, "git_bytes", fake)


async def test_an_under_floor_git_blocks_launch(project, monkeypatch):
    """The host floor, mirrored where the other pre-launch refusals already are.
    The detached CLI refuses this too and is the authority; without the mirror the
    operator's only signal was the dashboard's generic "launch may have failed"
    toast 10s later, which names neither git nor the floor.

    Asserted against the sole producer of the text rather than a literal, like the
    #414 sibling above — that is what keeps the toast and the CLI's abort from
    drifting into two different findings about one host.

    The fixture carries the #414 conflicting pair AND leaves the tree dirty, so
    this pins the ORDER too: either of those gates speaking first would send the
    operator to fix a project when the problem is the machine."""
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    _split_root_tui_project(project)
    _fake_tui_git_version(monkeypatch, "git version 2.25.1\n")
    expected = verify.under_floor_git_message("git version 2.25.1")

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: expected in notifications(app))
        assert not any("isolation" in m for m in notifications(app))
        assert not any("not clean" in m for m in notifications(app))
        assert not calls


async def test_a_git_that_cannot_be_probed_falls_through_the_floor_guard(project, monkeypatch):
    """The guard's deliberate blind spot, and the reason it has one: this probe runs
    on the event loop, so it carries a 5s bound the detached CLI does not share. A
    git slow enough to miss that bound but fast enough for the CLI's would be
    refused by a toast on a host that runs fine, so "could not look" falls through
    and lets the CLI answer — where `_reject_under_floor_git` fails CLOSED on the
    same fault, in the process that actually matters.

    `probes` is the positive control: `calls` alone would go green if the guard
    stopped probing at all, which is the opposite change."""
    probes = []
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))

    real = verify.git_bytes

    def hung(repo, *args, timeout_s=None):
        if args == ("version",):
            probes.append(timeout_s)
            raise verify.GitTimeoutError("git version timed out after 5s")
        return real(repo, *args, timeout_s=timeout_s)

    monkeypatch.setattr(verify, "git_bytes", hung)

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: bool(calls))
        assert probes == [5], "the guard must ask, and must ask with its own deadline"


async def test_live_run_asks_for_confirmation(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    make_run(project.project, "20260611-100000-aaaa", alive=True)  # our pid: running
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(
            pilot,
            lambda: (
                isinstance(app.screen, ConfirmModal)
                and not isinstance(app.screen, ConfirmResumeModal)
            ),
        )
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: bool(calls))


async def test_unknown_pid_run_asks_for_confirmation(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "unknown")
    run_dir = make_run(project.project, "20260611-100000-aaaa")
    (run_dir / "engine.pid").write_text("4242 123.0", encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.click("#ok")
        await until(
            pilot,
            lambda: (
                isinstance(app.screen, ConfirmModal)
                and not isinstance(app.screen, ConfirmResumeModal)
            ),
        )
        assert "unknown" in app.screen._body.plain
        assert not calls


async def test_legacy_pidless_but_live_run_asks_for_confirmation(project, monkeypatch):
    # A legacy run has no engine.pid but is provably alive via its mux session
    # (liveness == "alive"). The launch guard must still catch it — the pid gate
    # alone would skip a running engine and allow a conflicting launch.
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    make_run(project.project, "20260611-100000-aaaa")  # no engine.pid: legacy run
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.click("#ok")
        await until(
            pilot,
            lambda: (
                isinstance(app.screen, ConfirmModal)
                and not isinstance(app.screen, ConfirmResumeModal)
            ),
        )
        assert not calls


async def test_start_sweep_modal_launches(project, monkeypatch):
    calls = {}
    monkeypatch.setattr(launch, "mux_available", lambda: True)

    def fake_sweep(proj, run_id, *, no_prompt, decisions_only, max_bundles):
        calls.update(
            run_id=run_id,
            no_prompt=no_prompt,
            decisions_only=decisions_only,
            max_bundles=max_bundles,
        )

    monkeypatch.setattr(launch, "start_sweep_detached", fake_sweep)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, StartSweepModal))
        await ready(pilot, "#ok")
        app.screen.query_one("#no-prompt", Checkbox).value = True
        await pilot.click("#ok")
        await until(pilot, lambda: bool(calls))
        assert calls["no_prompt"] is True
        assert calls["decisions_only"] is False
        assert calls["max_bundles"] is None
        assert dashboard(app)._pending_run == calls["run_id"]


async def test_dry_run_shows_captured_output(project_tree, monkeypatch):
    seen = {}
    monkeypatch.setattr(launch, "mux_available", lambda: True)

    def fake_captured(tail):
        seen["tail"] = tail
        return 0, "would process 2 stories\n"

    monkeypatch.setattr(launch, "run_captured", fake_captured)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await ready(pilot, "#ok")
        app.screen.query_one("#dry-run", Checkbox).value = True
        await pilot.click("#ok")
        await until(pilot, lambda: isinstance(app.screen, TextOutputModal))
        assert seen["tail"][0] == "run"
        assert "--dry-run" in seen["tail"]
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))


async def test_dry_run_worker_survives_a_raising_subprocess(project_tree, monkeypatch):
    """The twin of test_validate_worker_survives_a_raising_subprocess: this worker
    is a @work(thread=True) body too, so a subprocess that cannot be spawned takes
    the whole app down unless run_captured is guarded. Both go through
    _run_captured_guarded, and this is the leg that proves the shared guard."""
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(
        launch,
        "run_captured_streams",
        lambda tail: (_ for _ in ()).throw(OSError("no such file")),
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await ready(pilot, "#ok")
        app.screen.query_one("#dry-run", Checkbox).value = True
        await pilot.click("#ok")
        await until(pilot, lambda: isinstance(app.screen, TextOutputModal))
        await ready(pilot, "#output Static")
        body = render(app.screen.query_one("#output Static").content)
        assert "no such file" in body, "the modal carries the reason, not a blank panel"
        assert app.is_running, "the app survived a dry run it could not spawn"


# ---------------------------------------------------------- #210: validate wiring
#
# `v` renders the --json document; anything undrawable degrades to the text modal.
# These tests mix real documents (via the conftest builder, for what actually gets
# rendered) with hand-rolled stdout strings (for the degrades) on purpose, not out
# of inconsistency: the builder goes through ValidationReport, so it can only ever
# produce a *valid* document, and every degrade case is by definition one it cannot
# express.


def stub_validate(monkeypatch, *, stdout: str = "", rc: int = 0, text: str = "FAIL: no policy\n"):
    """Stub **both** legs and record which ran.

    Stubbing only run_captured_streams leaves the degrade's run_captured live, so
    every degrade test would spawn a real `bmad-loop validate` subprocess — slow,
    and asserting the host's preflight rather than the code under test."""
    seen: dict[str, list[str]] = {}

    def fake_streams(tail):
        seen["json_tail"] = tail
        return rc, stdout, ""

    def fake_captured(tail):
        seen["text_tail"] = tail
        return rc, text

    monkeypatch.setattr(launch, "run_captured_streams", fake_streams)
    monkeypatch.setattr(launch, "run_captured", fake_captured)
    return seen


def grid_text(app: BmadLoopApp) -> str:
    """The findings grid as it draws at the modal's width."""
    return render(app.screen.query_one("#grid", Static).content)


async def test_validate_shows_findings_modal(project_tree, monkeypatch):
    """The migrated test_validate_shows_output_modal. `v` renders the document
    now, so the old run_captured stub is dead — and a dead stub is a real
    subprocess, not a failure."""
    doc = make_validate_document(
        [
            ("git.worktree-clean", "ok", "git worktree clean", None),
            ("adapter.binary", "problem", "codex not found on PATH", {"binary": "codex"}),
        ]
    )
    seen = stub_validate(monkeypatch, stdout=json.dumps(doc), rc=1)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, ValidateFindingsModal))
        await ready(pilot, "#grid")
        assert "--json" in seen["json_tail"]
        assert "text_tail" not in seen, "the JSON leg drew it; nothing re-ran in text mode"

        body = grid_text(app)
        assert "git.worktree-clean" in body and "adapter.binary" in body
        assert "binary: codex" in body, "a problem's detail is inline"
        header = str(app.screen.query_one(".title", Static).content)
        assert "validate failed" in header

        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))


async def test_validate_detail_toggle_expands_every_finding(project_tree, monkeypatch):
    """`d` re-renders the same document with detail on."""
    doc = make_validate_document(
        [("host.process", "ok", "process host: Posix", {"host": "PosixProcessHost"})]
    )
    stub_validate(monkeypatch, stdout=json.dumps(doc))
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, ValidateFindingsModal))
        await ready(pilot, "#grid")
        assert "host: PosixProcessHost" not in grid_text(app), "an ok detail starts collapsed"

        await pilot.press("d")
        await until(pilot, lambda: "host: PosixProcessHost" in grid_text(app))
        await pilot.press("d")
        await until(pilot, lambda: "host: PosixProcessHost" not in grid_text(app))


async def test_validate_verdict_comes_from_the_document_not_the_exit_code(
    project_tree, monkeypatch
):
    """rc conflates "checks failed" with "the command broke"; the document's `ok`
    does not. Both legs are rendered here with rc deliberately disagreeing with
    what the old code would have inferred from it."""
    failing = make_validate_document([("adapter.binary", "problem", "codex not found", None)])
    stub_validate(monkeypatch, stdout=json.dumps(failing), rc=1)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, ValidateFindingsModal))
        await ready(pilot, "#grid")
        header = str(app.screen.query_one(".title", Static).content)
        assert "validate failed" in header
        assert "gates are chained" in header, "a failure says the later gates may not have run"

    # A passing document at rc 0 — same wiring, opposite verdict, and the header
    # says so without the modal ever seeing the exit code.
    passing = make_validate_document([("git.worktree-clean", "ok", "git worktree clean", None)])
    stub_validate(monkeypatch, stdout=json.dumps(passing), rc=0)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, ValidateFindingsModal))
        await ready(pilot, "#grid")
        header = str(app.screen.query_one(".title", Static).content)
        assert "validate passed" in header
        assert "gates are chained" not in header


@pytest.mark.parametrize(
    ("stdout", "why"),
    [
        ("", "the command produced no document at all"),
        ("not json{", "unparseable stdout"),
        ('{"schema_version": 2, "ok": true, "counts": {}, "findings": []}', "a newer schema"),
        ('{"schema_version": 1, "ok": true, "counts": {}, "findings": "nope"}', "wrong shape"),
    ],
)
async def test_validate_degrades_to_the_text_modal(project_tree, monkeypatch, stdout, why):
    """Every undrawable document RE-RUNS validate in text mode. Showing the
    captured JSON instead would hand the reader a wall of `{"schema_version": ...}`
    at the exact moment the structural rendering failed; re-running costs one
    subprocess and makes the degrade byte-for-byte the pre-#210 behavior."""
    seen = stub_validate(monkeypatch, stdout=stdout, rc=1)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, TextOutputModal), timeout=10.0)
        await ready(pilot, "Label")  # body mounts a tick after the screen swaps
        labels = app.screen.query("Label")
        assert any("exit 1" in str(label.content) for label in labels), why
        assert "--json" not in seen["text_tail"], "the text re-run is the plain command"


async def test_validate_worker_survives_a_raising_subprocess(project_tree, monkeypatch):
    """@work(thread=True) defaults to exit_on_error=True, so anything escaping the
    worker body takes the whole app down rather than this one modal. The guard is
    an except, not a set of condition checks — the raise here is not a shape the
    checks could have caught.

    Only run_captured_streams is stubbed, on purpose: run_captured *calls* it, so
    a spawn failure is not a JSON-leg failure that the text re-run recovers from
    — it is the same failure twice. Stubbing the two legs to opposite outcomes
    would model a split production cannot produce, and would leave the degrade's
    own raise escaping the worker unnoticed. It raises in place of the spawn, so
    no real subprocess runs either."""
    monkeypatch.setattr(
        launch,
        "run_captured_streams",
        lambda tail: (_ for _ in ()).throw(OSError("no such file")),
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, TextOutputModal))
        await ready(pilot, "#output Static")
        body = render(app.screen.query_one("#output Static").content)
        assert "no such file" in body, "the modal carries the reason, not a blank panel"
        assert app.is_running, "the app survived a failure BOTH legs hit"


async def test_resume_confirm_launches(project_tree, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="DEV_VERIFY",
        paused_reason="verify failed",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


async def test_resume_uncaptured_window_id_warns(project_tree, monkeypatch):
    # The resume itself is running; only the #482 disambiguation record is lost,
    # so attach/stop may target an older same-run_id window. The success toast
    # must not mask that (the resolve path already errors on this condition).
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: None)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="DEV_VERIFY",
        paused_reason="verify failed",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(
            pilot, lambda: any("window id was not recorded" in m for m in notifications(app))
        )


async def test_resume_unknown_pid_warns(project_tree, monkeypatch):
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "unknown")
    run_dir = make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="DEV_VERIFY",
        paused_reason="verify failed",
    )
    (run_dir / "engine.pid").write_text("4242 123.0", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        assert "may still be live" in app.screen._warning


async def test_delete_unknown_pid_warns_but_does_not_block(project_tree, monkeypatch):
    # 'unknown' liveness (a live-but-unreadable pid) must not block cleanup — the
    # deliberate runs.engine_alive invariant — but the irreversible delete confirm
    # must warn the run may still be live rather than imply it is safely dead.
    monkeypatch.setattr(data, "liveness", lambda run_dir: "unknown")
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa")
    (run_dir / "engine.pid").write_text("4242 123.0", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert "may still be live" in app.screen._warning  # not blocked, but flagged
        assert "cannot be undone" in app.screen._warning


async def test_cleanup_unknown_sessions_notifies(project, monkeypatch):
    # cleanup still prunes 'unknown' sessions (unknown never blocks cleanup) but
    # must say so instead of silently killing a possibly-live engine's session.
    from bmad_loop import runs

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(runs, "prune_sessions", lambda _p: (["odd-1"], [], {"odd-1"}))
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _p: ([], [], []))
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any("unverifiable engine pid" in m for m in notifications(app)))
        # `until`, not a bare assert: the summary toast is marshalled from the
        # worker AFTER the pid warning, so waiting on the earlier one does not
        # guarantee this one has reached the message pump yet (Windows flake).
        await until(pilot, lambda: any("removed 1 session(s)" in m for m in notifications(app)))


@pytest.mark.parametrize(
    "fault, toast",
    [
        (MultiplexerError("ctl window probe unreachable"), "ctl window probe unreachable"),
        # a strict-POSIX decode fault from a scan probe that does not normalize
        # to the seam type (#380) must fail just as soft — an escape kills the
        # worker thread instead of toasting (the cli cleanup arm's twin).
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), "invalid start byte"),
    ],
)
async def test_cleanup_sessions_mux_error_notifies(project, monkeypatch, fault, toast):
    # prune_ctl_windows probes has_session on the shared ctl session (raiser-side),
    # so it can raise on a server-backed backend. The worker must marshal the error
    # to a toast via call_from_thread without crashing on an unhandled worker
    # exception — AND, because prune_sessions already killed the agent sessions
    # before prune_ctl_windows ran, it must still report that completed work (the
    # "removed N session(s)" summary and the unknown-pid warning), not swallow it.
    from bmad_loop import runs

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(runs, "prune_sessions", lambda _p: (["odd-1"], [], {"odd-1"}))

    def boom(_p):
        raise fault

    monkeypatch.setattr(launch, "prune_ctl_windows", boom)
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any(toast in m for m in notifications(app)))
        # the ctl-window failure is surfaced, but the session pruning that already
        # completed is still reported — not swallowed by an early return
        await until(pilot, lambda: any("unverifiable engine pid" in m for m in notifications(app)))
        # `until`, not a bare assert: the summary toast is marshalled from the
        # worker AFTER the pid warning, so waiting on the earlier one does not
        # guarantee this one has reached the message pump yet (Windows flake).
        await until(pilot, lambda: any("removed 1 session(s)" in m for m in notifications(app)))
        assert isinstance(app.screen, DashboardScreen)  # worker failed soft, no crash


@pytest.mark.parametrize(
    "fault, toast",
    [
        (MultiplexerError("PSMUX_DATA_DIR='' is not an absolute path"), "not an absolute path"),
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), "invalid start byte"),
    ],
)
async def test_cleanup_sessions_session_prune_error_notifies(project, monkeypatch, fault, toast):
    """The session half is raiser-side too, and the worker must fail as soft.

    The psmux backend refuses a registry root that would fail its pre-spawn
    absoluteness gate, and that raise happens before the tolerant listing
    wrapper can degrade it — so `prune_sessions` can raise where every other
    caller has a backstop that names the error. A worker thread has none, and
    an escape takes the whole dashboard down (Textual's `exit_on_error`).

    The opposite conclusion to its ctl-window twin above, on purpose: nothing
    has been killed yet, so there is no completed work to keep reporting and
    the worker stops. A summary toast here would claim a sweep that never ran.

    Ablate the guard (call `prune_sessions` outside the try) and the app is no
    longer on the dashboard — the worker's exception took it down."""
    from bmad_loop import runs

    monkeypatch.setattr(launch, "mux_available", lambda: True)

    def boom(_p):
        raise fault

    monkeypatch.setattr(runs, "prune_sessions", boom)
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _p: ([], [], []))
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any(toast in m for m in notifications(app)))
        assert isinstance(app.screen, DashboardScreen)  # worker failed soft, no crash
        # nothing ran, so nothing is summarised as having run
        assert not any("removed" in m and "session(s)" in m for m in notifications(app))


async def test_cleanup_warns_about_sessions_left_in_the_legacy_registry(project, monkeypatch):
    """The cli cleanup arm's stderr line, as a toast.

    The summary below it counts only what this registry's sweep removed, so a
    tagged pre-upgrade session the migration pass declined to claim is silently
    absent from it — and a count that quietly excludes them reads as "all
    clean". Read after the prune, so it names what is left standing.

    Ablate the toast and this fails; the twin CLI assertion lives in
    `test_cli.py`, and the reader itself is unit-tested in `test_runs.py`."""
    from bmad_loop import runs

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(runs, "prune_sessions", lambda _p: ([], [], set()))
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _p: ([], [], []))
    monkeypatch.setattr(
        runs,
        "legacy_registry_leftovers",
        lambda _p: (
            {
                runs.DEFAULT_REGISTRY_LABEL: ["bmad-loop-ctl"],
                r"D:	heir-own-registry": ["bmad-loop-old-1"],
            },
            [],
        ),
    )
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        # One toast per registry, each naming its own — the CLI arm's twin.
        # A single toast calling both "the default registry" sent an operator
        # whose sessions are in their own displaced root to the wrong place.
        await until(
            pilot,
            lambda: any(
                f"1 session(s) left in {runs.DEFAULT_REGISTRY_LABEL}" in m
                and "bmad-loop-ctl" in m
                and "bmad-loop-old-1" not in m
                for m in notifications(app)
            ),
        )
        await until(
            pilot,
            lambda: any(
                r"1 session(s) left in D:	heir-own-registry" in m and "bmad-loop-old-1" in m
                for m in notifications(app)
            ),
        )


async def test_cleanup_warns_about_a_legacy_registry_that_could_not_be_asked(project, monkeypatch):
    """DW-469, the cli arm's twin: a registry whose listing raised used to toast
    exactly what an empty one toasts — nothing. Ablate the `unverified` toast and
    this fails."""
    from bmad_loop import runs

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(runs, "prune_sessions", lambda _p: ([], [], set()))
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _p: ([], [], []))
    monkeypatch.setattr(
        runs,
        "legacy_registry_leftovers",
        lambda _p: ({}, ["/reg/broken: could not be listed: no server"]),
    )
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(
            pilot,
            lambda: any(
                "not checked" in m and "/reg/broken" in m and "no server" in m
                for m in notifications(app)
            ),
        )


async def test_dashboard_names_an_incomplete_run_listing(project_tree, monkeypatch):
    """DW-468: an empty runs table over an unreadable runs dir is not "no runs" —
    the border title says the listing is incomplete and a toast names the fault.
    Ablate the fault arm in `_apply_runs` and this fails."""
    from bmad_loop import runs

    monkeypatch.setattr(
        runs, "list_run_dirs", lambda _p: ([], "/x/runs: cannot list the runs dir: EACCES")
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        table = dashboard(app).query_one("#runs", DataTable)
        await until(pilot, lambda: "listing incomplete" in str(table.border_title))
        await until(
            pilot,
            lambda: any(
                "run listing incomplete" in m and "EACCES" in m for m in notifications(app)
            ),
        )


async def test_launch_asks_before_launching_over_an_incomplete_run_listing(project, monkeypatch):
    """DW-468: a run the listing could not read may be a live engine, so with no
    readable live run the launch guard still asks, naming the fault, rather than
    reading the listing as "none live" and launching a second engine unprompted.
    Ablate `or listing_fault is not None` in `_guarded` and this fails — the
    launch goes straight through."""
    from bmad_loop import runs

    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(
        runs, "list_run_dirs", lambda _p: ([], "/x/runs: cannot list the runs dir: EACCES")
    )
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        body = app.screen._body.plain
        assert "live or unknown: none readable" in body
        assert "run listing incomplete: /x/runs: cannot list the runs dir: EACCES" in body
        assert not calls
        # the ask is a real gate: confirming launches
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: calls)


async def test_cleanup_warns_about_ctl_windows_that_survived_the_kill(project, monkeypatch):
    # The summary counts only verified removals now (#435), so a window that
    # outlived its kill would otherwise just be missing from the toast with
    # nothing anywhere saying it is still there. Survived and unverifiable get
    # separate toasts: one is evidence the window is still open, the other is the
    # absence of evidence — merging them reports the first as the second.
    from bmad_loop import runs

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(runs, "prune_sessions", lambda _p: ([], [], set()))
    monkeypatch.setattr(
        launch, "prune_ctl_windows", lambda _p: (["gone-1"], ["stuck-1"], ["dunno-1"])
    )
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(
            pilot,
            lambda: any("still open after the kill: stuck-1" in m for m in notifications(app)),
        )
        await until(
            pilot, lambda: any("outcome unverifiable: dunno-1" in m for m in notifications(app))
        )
        await until(
            pilot, lambda: any("removed 0 session(s), 1 window(s)" in m for m in notifications(app))
        )


async def test_resume_finished_run_refused(project_tree, monkeypatch):
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    make_run(project_tree.project, "20260611-100000-aaaa", finished=True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("e")
        await until(pilot, lambda: any("already finished" in m for m in notifications(app)))
        assert isinstance(app.screen, DashboardScreen)


async def test_attach_without_mux_notifies(project, monkeypatch):
    monkeypatch.setattr(launch, "mux_available", lambda: False)
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("a")
        await until(
            pilot,
            lambda: any("multiplexer backend unavailable" in m for m in notifications(app)),
        )


async def test_attach_without_agent_session_notifies(project, monkeypatch):
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: False)
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, run_id: None)
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("a")
        await until(pilot, lambda: any("no live agent session" in m for m in notifications(app)))


async def test_attach_multiplexer_error_notifies(project, monkeypatch):
    # attach_target_argv is a server round-trip on server-backed backends (e.g.
    # the external herdr adapter), so it can raise after the availability/session
    # pre-gates pass (server died or the workspace was torn down in between); the
    # TUI must surface the error as a toast, not crash the app.
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: True)
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, run_id: None)

    def boom(_target):
        raise MultiplexerError("backend server not reachable")

    monkeypatch.setattr("bmad_loop.tui.app.runs.attach_target_argv", boom)
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("a")
        await until(
            pilot, lambda: any("backend server not reachable" in m for m in notifications(app))
        )
        assert isinstance(app.screen, DashboardScreen)  # the action failed soft


async def test_attach_session_probe_error_notifies(project, monkeypatch):
    # session_exists probes has_session, a raiser-side call: on a server-backed
    # backend it can raise after the availability pre-gate (server unreachable /
    # torn down in between). action_attach routes it through _mux_guarded, so the
    # TUI toasts the error and aborts the attach instead of crashing the app.
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, run_id: None)

    def boom(_session):
        raise MultiplexerError("session probe unreachable")

    monkeypatch.setattr(launch, "session_exists", boom)
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("a")
        await until(
            pilot, lambda: any("session probe unreachable" in m for m in notifications(app))
        )
        assert isinstance(app.screen, DashboardScreen)  # the action failed soft


# ------------------------------------------------------- sweep decision flow


async def test_decision_banner_shows_and_clears(project_tree):
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", run_type="sweep", alive=True)
    journal = Journal(run_dir)
    journal.append("sweep-start")
    journal.append("decision-pending", dw_id="DW-7", question="reopen the cache work?")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.decision_pending is not None)
        assert screen.decision_pending == ("DW-7", "reopen the cache work?")
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "decision needed: DW-7" in header
        assert "press a to attach and answer" in header
        # the toast is posted via self.notify() onto textual's async message pump,
        # so it is emitted a tick after _decision is set — wait
        # for it rather than asserting synchronously (matches the other notify tests)
        await until(pilot, lambda: any("reopen the cache work?" in m for m in notifications(app)))

        journal.append("decision-answered", dw_id="DW-7", key="a", effect="build")
        await until(pilot, lambda: screen.decision_pending is None)
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "decision needed" not in header


async def test_decision_footer_suppressed_for_crashed(project_tree):
    # a crashed run tore its tmux session down, so the "press a to attach and
    # answer" hint would point at a dead session — suppress it even when a
    # decision is pending.
    run_dir = make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        crashed=True,
        crash_error="RuntimeError: boom",
    )
    journal = Journal(run_dir)
    journal.append("decision-pending", dw_id="DW-7", question="reopen the cache work?")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.decision_pending is not None)
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "engine crashed" in header
        assert "press a to attach and answer" not in header


def _patch_attach_exec(monkeypatch) -> tuple[list[list[str]], list[tuple[str, str]]]:
    """Route the final attach exec into a list: pretend we are inside tmux so
    action_attach takes the plain subprocess.call(switch-client) path. Stub the
    TUI return target and capture return-pane stamps so no real tmux is touched
    and tests can assert which ctl window gets the switch-back target recorded."""
    calls: list[list[str]] = []
    stamps: list[tuple[str, str]] = []
    monkeypatch.setenv("TMUX", "/tmp/fake-tmux,1,0")
    monkeypatch.setattr(
        "bmad_loop.tui.app.subprocess.call", lambda argv: calls.append(list(argv)) or 0
    )
    monkeypatch.setattr(launch, "current_return_target", lambda: "=main:%9")
    monkeypatch.setattr(launch, "set_return_pane", lambda w, p: stamps.append((w, p)))
    return calls, stamps


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_attach_targets_ctl_window_when_decision_pending(project_tree, monkeypatch):
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", run_type="sweep", alive=True)
    Journal(run_dir).append("decision-pending", dw_id="DW-7", question="q?")
    selected: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: True)  # agent up too
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, run_id: "@5")
    monkeypatch.setattr(launch, "select_ctl_window_id", lambda w: selected.append(w))
    calls, stamps = _patch_attach_exec(monkeypatch)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).decision_pending is not None)
        await pilot.press("a")
        await until(pilot, lambda: bool(calls))
    assert selected == ["@5"]
    assert calls == [["tmux", "switch-client", "-t", "=bmad-loop-ctl"]]
    # the ctl window is stamped with our pane so it switches us back on exit
    assert stamps == [("@5", "=main:%9")]


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_attach_uses_the_recorded_ctl_window(project_tree, monkeypatch):
    # The one attach test that does NOT replace ctl_window_id, so it pins the
    # seam every other one stubs out: that the TUI hands it the same project root
    # the launch recorded the window under (#482). Point app.py at anything else
    # — the run dir, an unresolved path — and the record is unfindable under that
    # root, so these untagged rows prove nothing and the lookup answers None
    # (#531). session_exists is stubbed True here, so `a` then takes the live
    # agent session instead of the ctl window: nothing is selected and nothing is
    # stamped, and both assertions below fail.
    import subprocess as _subprocess

    from bmad_loop.adapters import tmux_base

    rid = "20260611-100000-aaaa"
    run_dir = make_run(project_tree.project, rid, run_type="sweep", alive=True)
    Journal(run_dir).append("decision-pending", dw_id="DW-7", question="q?")
    # id + the pane pid its mint read: an untagged row needs both (#750)
    (run_dir / launch._CTL_WINDOW_FILE).write_text("@2\n4242", encoding="utf-8")
    selected: list[str] = []

    def fake(argv, **kwargs):
        # rows as `window_id, window_name, tag, pane_pid` — the lookup's fields
        out = (
            f"@1\trun-{rid}\t\t11\n@2\tresume-{rid}\t\t4242\n" if argv[1] == "list-windows" else ""
        )
        return _subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(launch, "session_exists", lambda session: True)
    monkeypatch.setattr(launch, "select_ctl_window_id", lambda w: selected.append(w))
    calls, stamps = _patch_attach_exec(monkeypatch)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).decision_pending is not None)
        await pilot.press("a")
        await until(pilot, lambda: bool(calls))
    assert selected == ["@2"]
    assert stamps == [("@2", "=main:%9")]


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_attach_outside_tmux_stamps_detach(project_tree, monkeypatch):
    # No TMUX: a throwaway client attaches under suspend, so the ctl window is
    # stamped to detach it on exit (returning to the suspended TUI) rather than
    # switch-client back to a pane we do not have.
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", run_type="sweep", alive=True)
    Journal(run_dir).append("decision-pending", dw_id="DW-7", question="q?")
    monkeypatch.delenv("TMUX", raising=False)
    stamps: list[tuple[str, str]] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: True)
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, run_id: "@5")
    monkeypatch.setattr(launch, "select_ctl_window_id", lambda w: None)
    monkeypatch.setattr(launch, "set_return_pane", lambda w, p: stamps.append((w, p)))
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).decision_pending is not None)
        await pilot.press("a")
        await until(pilot, lambda: bool(stamps))
    assert stamps == [("@5", "detach")]


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_attach_prefers_agent_session_without_decision(project_tree, monkeypatch):
    make_run(project_tree.project, "20260611-100000-aaaa", alive=True)
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: True)
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, run_id: "@5")
    calls, stamps = _patch_attach_exec(monkeypatch)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("a")
        await until(pilot, lambda: bool(calls))
    assert calls == [["tmux", "switch-client", "-t", "=bmad-loop-20260611-100000-aaaa"]]
    # attaching to a live agent session is not our parked window — nothing stamped
    assert stamps == []


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_attach_falls_back_to_ctl_window(project_tree, monkeypatch):
    make_run(project_tree.project, "20260611-100000-aaaa", alive=True)
    selected: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: False)
    monkeypatch.setattr(launch, "ctl_window_id", lambda proj, run_id: "@5")
    monkeypatch.setattr(launch, "select_ctl_window_id", lambda w: selected.append(w))
    calls, stamps = _patch_attach_exec(monkeypatch)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("a")
        await until(pilot, lambda: bool(calls))
    assert selected == ["@5"]
    assert calls == [["tmux", "switch-client", "-t", "=bmad-loop-ctl"]]
    assert stamps == [("@5", "=main:%9")]


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_resolve_escalation_launches_and_attaches(project_tree, monkeypatch):
    launched: list[str] = []
    selected: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")

    def fake_start_resolve(proj, rid):
        launched.append(rid)
        return "@7"

    monkeypatch.setattr(launch, "start_resolve_detached", fake_start_resolve)
    monkeypatch.setattr(launch, "select_ctl_window_id", lambda w: selected.append(w))
    calls, stamps = _patch_attach_exec(monkeypatch)
    # The healthy path: the lookup answers the window the launch minted, so no
    # warning. Stubbed at the same seam every other attach test stubs — the
    # helper's own listing/record logic is pinned in tests/test_tui_launch.py.
    monkeypatch.setattr(launch, "ctl_window_recorded", lambda proj, rid, wid: True)
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="escalation",
        paused_reason="CRITICAL escalation",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("R")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: bool(calls))
        assert not any("was not recorded" in m for m in notifications(app))
    assert launched == ["20260611-100000-aaaa"]
    assert selected == ["@7"]
    assert calls == [["tmux", "switch-client", "-t", "=bmad-loop-ctl"]]
    # resolve runs in the freshly launched ctl window (@7) — stamp it to return
    assert stamps == [("@7", "=main:%9")]


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_resolve_warns_when_the_record_did_not_survive(project_tree, monkeypatch):
    # The resolve path kept the captured id (it attaches with it) but never
    # asked whether the record landed, so a failed write left `a`/`x` on the
    # ambiguous scan behind a clean attach. Warn, and attach anyway: this
    # window is reached by the id in hand, only later verbs are degraded.
    selected: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(launch, "start_resolve_detached", lambda proj, rid: "@7")
    monkeypatch.setattr(launch, "ctl_window_recorded", lambda proj, rid, wid: False)
    monkeypatch.setattr(launch, "select_ctl_window_id", lambda w: selected.append(w))
    calls, _stamps = _patch_attach_exec(monkeypatch)
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="escalation",
        paused_reason="CRITICAL escalation",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("R")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any("was not recorded" in m for m in notifications(app)))
    assert selected == ["@7"]  # still attached to the window it minted
    assert calls == [["tmux", "switch-client", "-t", "=bmad-loop-ctl"]]


async def test_resolve_unknown_pid_refused(project_tree, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "unknown")
    monkeypatch.setattr(launch, "start_resolve_detached", lambda proj, rid: launched.append(rid))
    run_dir = make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="escalation",
        paused_reason="CRITICAL escalation",
    )
    (run_dir / "engine.pid").write_text("4242 123.0", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("R")
        await until(pilot, lambda: any("may still be live" in m for m in notifications(app)))
    assert launched == []


async def test_resolve_refused_when_not_escalation(project_tree, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(launch, "start_resolve_detached", lambda proj, rid: launched.append(rid))
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="spec-approval",
        paused_reason="awaiting approval",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("R")
        await until(pilot, lambda: any("escalation" in m for m in notifications(app)))
    assert launched == []  # warned, never launched


async def test_resolve_refused_on_environment_pause_with_resume_hint(project_tree, monkeypatch):
    """DW-523: an environment pause is lifted by resume, not resolve — R names that
    remedy instead of the generic escalation-only refusal, and launches nothing."""
    launched: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(launch, "start_resolve_detached", lambda proj, rid: launched.append(rid))
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage=PAUSE_ENVIRONMENT,
        paused_reason="environment fault before dev session dispatch",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("R")
        await until(
            pilot,
            lambda: any("an environment pause needs no resolve" in m for m in notifications(app)),
        )
        hint = next(m for m in notifications(app) if "needs no resolve" in m)
        assert "`bmad-loop resume 20260611-100000-aaaa`" in hint
        assert not any("only available" in m for m in notifications(app))
        assert isinstance(app.screen, DashboardScreen), "no confirm / escalation viewer"
    assert launched == []  # warned, never launched


# ------------------------------------------------- stories mode: board + badges


def test_pause_tag_and_label_render():
    assert pause_tag("plan-checkpoint").plain == "plan"
    assert pause_tag("story-checkpoint").plain == "story"
    assert pause_tag("escalation").plain == "esc"
    assert pause_tag("story-gate").plain == "gate"
    assert pause_tag("epic-boundary").plain == "epic"
    assert pause_tag("").plain == ""  # not paused → no tag
    label, style = pause_label("escalation")
    assert label == "escalation" and "red" in style
    # the gate viewers title themselves from pause_label, so these three strings
    # are load-bearing UI, not just badge text (#515)
    assert pause_label("story-gate") == ("story gate", "yellow")
    assert pause_label("epic-boundary")[0] == "epic gate"
    assert pause_label("spec-approval")[0] == "spec-approval gate"


def test_pause_tag_and_label_render_environment():
    """DW-523: an environment pause gets its own badge — the gate viewer titles
    itself from pause_label, so the label is load-bearing UI too."""
    tag = pause_tag(PAUSE_ENVIRONMENT)
    assert tag.plain == "env" and "red" in str(tag.style)
    assert pause_label(PAUSE_ENVIRONMENT) == ("environment fault", "bold red")


def test_stopping_tag_renders():
    # glyph + style match STOPPED (the end state a graceful stop lands in)
    tag = widgets.stopping_tag()
    assert isinstance(tag, Text)
    assert "stop" in tag.plain
    assert tag.style == widgets.STATUS_STYLES[data.STOPPED]


def test_agent_label():
    # name·model, or just the name when no explicit model was recorded ("")
    assert agent_label("claude", "opus") == "claude·opus"
    assert agent_label("claude", "") == "claude"
    assert agent_label("codex", "gpt-5") == "codex·gpt-5"


def test_sprint_story_label_split_suffix():
    # split halves (issue #144) must render distinctly: 6a-… / 6b-…, not both 6-…
    from bmad_loop.sprintstatus import Story

    whole = Story(key="2-5-intact", epic=2, num=5, slug="intact", status="done")
    half = Story(key="2-6a-build", epic=2, num=6, slug="build", status="backlog", suffix="a")
    assert sprint_story_label(whole).plain == "✓ 5-intact"
    assert sprint_story_label(half).plain == "· 6a-build"


def test_sprint_glyphs_cover_every_lifecycle_status():
    """Both maps are read through `.get(..., "?")` — deliberately, because the
    board is LLM-maintained and an unknown token must render, not raise. The cost
    is that a lifecycle status nobody added a glyph for renders a silent `?` and
    no test notices. Scope the coverage claim to STATUS_ORDER, which is exactly
    the set the orchestrator itself writes, and leave the fallback for the rest.
    """
    from bmad_loop.sprintstatus import STATUS_ORDER
    from bmad_loop.tui.widgets import SPRINT_GLYPHS, SPRINT_STYLES

    assert set(STATUS_ORDER) <= set(SPRINT_GLYPHS)
    assert set(SPRINT_GLYPHS) == set(SPRINT_STYLES)  # never a glyph without a color


def test_sprint_story_label_awaiting_operator():
    from bmad_loop.sprintstatus import Story

    parked = Story(key="2-7-dns", epic=2, num=7, slug="dns", status="awaiting-operator")
    label = sprint_story_label(parked)
    assert label.plain == "⏸ 7-dns"
    # not the "?"/dim unknown-token fallback
    assert label.style == "yellow"


def test_story_cells_render():
    assert story_state_cell("awaiting-operator").plain == "⏸ awaiting-operator"
    assert story_state_cell("done").plain == "✓ done"
    assert story_state_cell("sentinel:unresolved").plain.startswith("⚠")
    assert story_checkpoint_cell(True, False).plain == "S·"
    assert story_checkpoint_cell(False, True).plain == "·D"
    assert story_checkpoint_cell(True, True).plain == "SD"
    assert story_checkpoint_cell(False, False).plain == "··"


def _write_stories_fixture(root: Path) -> None:
    import yaml

    folder = root / "epic-1"
    (folder / "stories").mkdir(parents=True)
    (folder / "SPEC.md").write_text("# Epic 1\n", encoding="utf-8")
    (folder / "stories.yaml").write_text(
        yaml.safe_dump(
            [
                {"id": "1", "title": "First story", "description": "d", "spec_checkpoint": True},
                {"id": "2", "title": "Second story", "description": "d"},
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (folder / "stories" / "1-slug.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")


async def test_stories_mode_run_shows_board_and_attention(project_tree):
    root = project_tree.project
    _write_stories_fixture(root)
    make_run(
        root,
        "20260611-100000-aaaa",
        source="stories",
        spec_folder="epic-1",
        paused_stage="plan-checkpoint",
        paused_reason="plan checkpoint for 2",
    )
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        stories_table = screen.query_one("#stories-table", StoriesTable)
        sprint_tree = screen.query_one("#sprint-tree", SprintTree)
        # the stories board replaces the sprint tree for a stories-mode run
        await until(pilot, lambda: stories_table.display and not sprint_tree.display)
        await until(pilot, lambda: stories_table.row_count == 2)
        # global attention indicator + per-run pause badge
        runs = screen.query_one("#runs", DataTable)
        assert "need attention" in str(runs.border_title)
        note = runs.get_cell("20260611-100000-aaaa", "note")
        assert note.plain == "plan"


async def test_sprint_mode_run_keeps_sprint_tree(project_tree):
    root = project_tree.project
    install_bmad_config(project_tree)
    write_sprint(project_tree, {"epic-1": "in-progress", "1-1-a": "ready-for-dev"})
    make_run(root, "20260611-100000-aaaa", finished=True)
    app = BmadLoopApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        stories_table = screen.query_one("#stories-table", StoriesTable)
        sprint_tree = screen.query_one("#sprint-tree", SprintTree)
        await until(pilot, lambda: sprint_tree.display and not stories_table.display)


# ---------------------------------------------------- HITL pause review viewers


def _stories_paused_run(
    root: Path,
    *,
    stage: str,
    run_id: str = "20260611-100000-aaaa",
    story_key: str = "1",
    spec_status: str = "ready-for-dev",
    spec_checkpoint: bool = True,
    done_checkpoint: bool = False,
    commit_sha: str = "",
    review_cycle: int = 0,
    blocked_result: str = "",
    sentinel: bool = False,
    worktree_path: str = "",
    spec_outside_worktree: bool = False,
) -> tuple[Path, Path]:
    """A stories-mode run paused at `stage`, with the id-keyed story spec on disk
    and a StoryTask pointing at it. Returns (run_dir, spec_path).

    `worktree_path` expresses the worktree-isolation shape: the run's own copy of the
    spec is written under that tree while the main checkout keeps a TWIN at the same
    relative path, and `task.spec_file` is the absolute worktree path — which
    `StoryTask.to_dict` persists RELATIVE to the mount, so `load_state` hands the app
    back the bare relpath production actually stores. The returned spec path is then
    the worktree's copy; the twin is the decoy a cwd-anchored resolve lands on.

    `spec_outside_worktree` keeps the mount but leaves the spec at the main-checkout
    path — the shape a shared artifact dir produces, where
    `_serialized_worktree_path`'s `relative_to` raises and the ABSOLUTE path is
    persisted verbatim beside a set `worktree_path`."""
    import yaml

    # The two parameters are one shape, not two: "outside the worktree" is meaningless
    # without a worktree, and the combination silently built a non-isolated run that
    # graded nothing while reading like an isolated row.
    if spec_outside_worktree and not worktree_path:
        raise ValueError("spec_outside_worktree requires worktree_path")

    folder = root / "epic-1"
    (folder / "stories").mkdir(parents=True, exist_ok=True)
    (folder / "SPEC.md").write_text("# Epic 1\n", encoding="utf-8")
    (folder / "stories.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": story_key,
                    "title": f"Story {story_key}",
                    "description": "does a thing",
                    "spec_checkpoint": spec_checkpoint,
                    "done_checkpoint": done_checkpoint,
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    slug = "unresolved" if sentinel else "slug"
    spec = folder / "stories" / f"{story_key}-{slug}.md"
    body = f"---\nstatus: {spec_status}\n---\n\n# plan for {story_key}\n"
    if blocked_result:
        body += f"\n## Auto Run Result\n\n- Status: blocked\n\n{blocked_result}\n"
    spec.write_text(body, encoding="utf-8")
    task = StoryTask(
        story_key=story_key,
        epic=0,
        phase=Phase.ESCALATED if stage == "escalation" else Phase.DEV_VERIFY,
    )
    task.spec_file = str(spec)
    if worktree_path:
        task.worktree_path = worktree_path
    if worktree_path and not spec_outside_worktree:
        # The isolated shape. The body differs per tree so "the worktree copy was
        # read/written" is checkable against "the main-checkout twin was not" — with
        # identical payloads either assertion could pass on the wrong file.
        twin = spec  # the main checkout keeps today's body, at the same relpath
        spec = Path(worktree_path) / twin.relative_to(root)
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(body.replace("# plan for", "# worktree plan for"), encoding="utf-8")
        task.spec_file = str(spec)  # to_dict re-persists this RELATIVE to the mount
    if worktree_path:
        # the mount's mint-time identity (DW-446), as `run_isolated` records it
        task.worktree_identity = platform_util.root_identity_record(Path(worktree_path))
    task.review_cycle = review_cycle
    if commit_sha:
        task.commit_sha = commit_sha
    run_dir = make_run(
        root,
        run_id,
        source="stories",
        spec_folder="epic-1",
        paused_stage=stage,
        paused_reason=f"{stage} for {story_key}",
        paused_story_key=story_key,
        tasks={story_key: task},
    )
    return run_dir, spec


async def _open_review(app, pilot, modal_type):
    await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
    await until(pilot, lambda: dashboard(app).selected_run_id is not None)
    await pilot.press("p")
    await until(pilot, lambda: isinstance(app.screen, modal_type))


async def test_plan_checkpoint_approve_resumes(project_tree, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _stories_paused_run(project_tree.project, stage="plan-checkpoint")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-approve"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


async def test_plan_checkpoint_replan_resets_and_resumes(project_tree, monkeypatch):
    from bmad_loop import devcontract

    calls: list[str] = []
    resets: list[tuple] = []
    strips: list[tuple[Path, Path]] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        devcontract,
        "reset_spec_status",
        lambda p, s, **kw: resets.append((p, s, kw["confine_root"])) or True,
    )
    monkeypatch.setattr(
        devcontract,
        "strip_auto_run_result",
        lambda p, **kw: strips.append((p, kw["confine_root"])) or True,
    )
    _run_dir, spec = _stories_paused_run(project_tree.project, stage="plan-checkpoint")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-replan"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])
        # the root is captured, not just the path: `_do_replan` has to pass the
        # project it built `run_dir` from, and a `confine_root` naming the spec's
        # own parent would be lexically confined and behaviourally inert (#593).
        assert resets == [(spec, "draft", project_tree.project)]
        assert strips == [(spec, project_tree.project)]


async def test_plan_checkpoint_replan_restores_preimage_when_result_strip_fails(
    project, monkeypatch
):
    """The reset and result strip are one TUI transaction, including confinement.

    The real status reset commits first; the injected second-stage fault then forces
    the helper to restore the byte-for-byte preimage. Using an isolated worktree also
    pins that both the forward status write and rollback carry the caller's owning
    root into the confined atomic writer.

    Ablation: delete the rollback write in ``reset_spec_for_replan`` and this reddens
    on byte identity because the spec remains at ``status: draft``.
    """
    from bmad_loop import devcontract

    calls: list[str] = []
    roots: list[Path] = []
    real_atomic_write = devcontract._atomic_write_spec

    writes = 0

    def fail_second_atomic_write(p, text, **kw):
        nonlocal writes
        writes += 1
        roots.append(kw["confine_root"])
        if writes == 2:
            raise OSError("injected result-strip write failure")
        return real_atomic_write(p, text, **kw)

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(devcontract, "_atomic_write_spec", fail_second_atomic_write)
    wt = _unit_worktree(project.project)
    _run_dir, spec = _stories_paused_run(
        project.project,
        stage="plan-checkpoint",
        worktree_path=str(wt),
        blocked_result="stale terminal result",
    )
    original = (
        b"---\r\nstatus: ready-for-dev\r\n---\r\n\r\n# worktree plan for 1\r\n"
        b"\r\n## Auto Run Result\r\n\r\n- Status: blocked\r\n\r\nstale terminal result\r\n"
    )
    spec.write_bytes(original)
    monkeypatch.chdir(project.project)

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-replan"))
        await until(
            pilot,
            lambda: any("injected result-strip write failure" in m for m in notifications(app)),
        )
        assert app.is_running
    assert spec.read_bytes() == original
    assert calls == []
    assert roots == [wt, wt, wt]


async def test_plan_checkpoint_replan_does_not_strip_when_reset_refuses(project, monkeypatch):
    """An unchanged reset must not independently commit the result-strip half."""
    calls: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _run_dir, spec = _stories_paused_run(
        project.project,
        stage="plan-checkpoint",
        spec_status="draft",
        blocked_result="stale terminal result",
    )
    original = spec.read_bytes()

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-replan"))
        await until(pilot, lambda: any("could not reset" in m for m in notifications(app)))
    assert spec.read_bytes() == original
    assert calls == []


async def test_plan_checkpoint_replan_rollback_failure_stays_loud(project_tree, monkeypatch):
    """A failed undo escapes to the TUI error path and still cannot resume."""
    from bmad_loop import devcontract

    calls: list[str] = []
    writes = 0
    real_atomic_write = devcontract._atomic_write_spec

    def fail_rollback(p, text, **kw):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected rollback failure")
        return real_atomic_write(p, text, **kw)

    def fail_strip(p, **kw):
        raise OSError("injected result-strip failure")

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(devcontract, "_atomic_write_spec", fail_rollback)
    monkeypatch.setattr(devcontract, "strip_auto_run_result", fail_strip)
    _run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="plan-checkpoint",
        blocked_result="stale terminal result",
    )

    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-replan"))
        await until(
            pilot,
            lambda: any("injected rollback failure" in m for m in notifications(app)),
        )
        assert app.is_running
    assert calls == []


def _unit_worktree(root: Path, run_id: str = "20260611-100000-aaaa", unit: str = "1") -> Path:
    """The UNRESOLVED spelling of where `workspace.open_unit_workspace` mounts a unit.

    Production stores `unresolved_wt.resolve()`, so on a symlinked temp root (macOS
    `/tmp` -> `/private/tmp`) this and the real mount differ. Deliberately not resolved
    here: that `.resolve()` divergence is the one way an isolated spec lands outside
    `project`, which `runs.task_spec_root` treats as its own case, and pinning the
    lexical spelling keeps these rows measuring the anchor rather than the sandbox.
    """
    return root / RUNS_DIR / run_id / "worktrees" / unit


async def test_plan_checkpoint_replan_writes_the_worktree_spec_not_the_main_twin(
    project, monkeypatch
):
    """Under isolation the replan must reset the spec the RUN owns, not its twin.

    `StoryTask._serialized_worktree_path` persists an isolated unit's `spec_file`
    RELATIVE to the mounted worktree and `from_dict` reads it back raw, so
    `_paused_spec`'s bare `Path(task.spec_file)` resolved against the TUI process cwd
    — the project root, which carries the very same `epic-1/stories/...` layout. Both
    destructive writers then landed on the MAIN CHECKOUT's twin: `confine_root` (the
    project) accepted it because it genuinely is under `project`, `reset_spec_status`
    answered True, the operator got a "plan reset to draft" notice and the run
    resumed — while the worktree's real spec kept its terminal status, so the next
    dispatch did not re-plan, and an unrelated tracked file was rewritten.

    The cwd is set EXPLICITLY: pytest does not run from the sandbox, so without the
    `chdir` the reverted code would merely fail to resolve the relpath and this row
    would pass for the wrong reason instead of reproducing the hazard. The two copies
    carry distinguishable bodies for the same reason — "the right file was written"
    has to be checkable against "the other one was not".

    `confine_root` is captured as well as graded on bytes, because the two halves are
    not one ablation: the worktree here is UNDER `project` (that is where
    `workspace.open_unit_workspace` mounts it), so a root reverted to `self.project`
    still lands on the right file — it just silently drops both writers off the
    confined arm and loses its O_NOFOLLOW walk (#593), with no signal at all.

    Ablations: revert `_paused_spec` to `Path(task.spec_file)` and this reddens on
    the worktree copy's status AND on the twin's byte-identity; pass `self.project`
    as `_do_replan`'s `confine_root` and it reddens on the captured roots.
    """
    from bmad_loop import devcontract

    calls: list[str] = []
    roots: list[Path] = []
    real_reset, real_strip = devcontract.reset_spec_status, devcontract.strip_auto_run_result

    def spy_reset(p, s, **kw):
        roots.append(kw["confine_root"])
        return real_reset(p, s, **kw)

    def spy_strip(p, **kw):
        roots.append(kw["confine_root"])
        return real_strip(p, **kw)

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(devcontract, "reset_spec_status", spy_reset)
    monkeypatch.setattr(devcontract, "strip_auto_run_result", spy_strip)
    wt = _unit_worktree(project.project)
    _run_dir, spec = _stories_paused_run(
        project.project,
        stage="plan-checkpoint",
        worktree_path=str(wt),
        blocked_result="stale terminal result",
    )
    twin = project.project / spec.relative_to(wt)
    untouched = twin.read_bytes()
    monkeypatch.chdir(project.project)  # what the TUI actually runs from

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-replan"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])
    assert verify.read_frontmatter(spec)["status"] == "draft"
    assert "## Auto Run Result" not in spec.read_text(encoding="utf-8")
    assert twin.read_bytes() == untouched
    assert roots == [wt, wt]


@pytest.mark.skipif(
    not platform_util.DIR_FD_ANCHORED_WRITES or sys.platform == "win32",
    reason="dir-fd anchoring and POSIX symlinks",
)
async def test_plan_checkpoint_replan_refuses_a_worktree_mount_swapped_for_a_link(
    project_tree, monkeypatch
):
    """DW-423 end-to-end: the isolated run's mount is replaced by a link to an outside
    tree carrying the same spec subpath while the review modal is open. The replan
    must refuse — error notice, no resume — and the outside spec copy is unchanged.

    Ablation: pass `None` instead of `_paused_spec_root_identity(state)` from `done()`
    (or drop the forward to `reset_spec_for_replan`) and the replan resets the outside
    copy to draft and resumes."""
    import shutil

    calls: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    wt = _unit_worktree(project_tree.project)
    _run_dir, spec = _stories_paused_run(
        project_tree.project,
        stage="plan-checkpoint",
        worktree_path=str(wt),
        blocked_result="stale terminal result",
    )
    outside = project_tree.project.parent / "outside-mount"
    outside_spec = outside / spec.relative_to(wt)
    monkeypatch.chdir(project_tree.project)

    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        shutil.copytree(wt, outside)
        wt.rename(wt.with_name(wt.name + "-aside"))
        wt.symlink_to(outside, target_is_directory=True)
        untouched = outside_spec.read_bytes()
        await click(pilot, await ready(pilot, "#act-replan"))
        await until(pilot, lambda: any("replan failed" in m for m in notifications(app)))
    assert calls == []
    assert outside_spec.read_bytes() == untouched


async def test_plan_checkpoint_replan_refuses_a_legacy_mount_with_no_identity_record(
    project_tree, monkeypatch
):
    """DW-446: a paused isolated run whose state.json predates the mint-time record
    (``worktree_identity`` absent) cannot replan from the TUI — the observer never
    writes the record, so the pin has nothing to compare against and refuses:
    error notice, no resume, the spec untouched. A resume or re-arm records it.

    Ablation: map a missing record to a fresh `lstat` in `runs.mount_root_identity`
    and the replan resets the spec to draft and resumes."""
    calls: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    wt = _unit_worktree(project_tree.project)
    run_dir, spec = _stories_paused_run(
        project_tree.project,
        stage="plan-checkpoint",
        worktree_path=str(wt),
        blocked_result="stale terminal result",
    )
    raw = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    for task in raw["tasks"].values():
        task.pop("worktree_identity")  # the pre-DW-446 shape
    (run_dir / "state.json").write_text(json.dumps(raw), encoding="utf-8")
    untouched = spec.read_bytes()
    monkeypatch.chdir(project_tree.project)

    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-replan"))
        await until(pilot, lambda: any("replan failed" in m for m in notifications(app)))
    assert calls == []
    assert spec.read_bytes() == untouched
    assert (
        "worktree_identity"
        not in json.loads((run_dir / "state.json").read_text(encoding="utf-8"))["tasks"]["1"]
    )  # the observer wrote no record


async def test_plan_checkpoint_replan_confines_on_the_project_for_an_out_of_mount_spec(
    project, monkeypatch
):
    """Matrix row 5 end-to-end: the root is the tree that can CONFINE the spec.

    An absolute `spec_file` beside a set `worktree_path` means the spec sits outside
    the mount (`_serialized_worktree_path` keeps it verbatim exactly when
    `relative_to` raises) — a shared artifact dir. The path passes through unchanged,
    but the mount can never contain it, so a `confine_root` naming the worktree sends
    both writers to the plain no-follow arm and drops #593's O_NOFOLLOW walk.

    The captured root is the ONLY discriminator at this layer, and deliberately so:
    both roots land the write here (the confined gate is lexical, and its else-branch
    still writes), so the reset-to-draft assertion below cannot tell them apart. It is
    kept because the replan must still actually work for this shape, not to grade the
    root.

    Ablation: revert `task_spec_root` to `Path(task.worktree_path or state.project)`
    and this reddens on the captured roots — they become the mount.
    """
    from bmad_loop import devcontract

    calls: list[str] = []
    roots: list[Path] = []
    real_reset, real_strip = devcontract.reset_spec_status, devcontract.strip_auto_run_result

    def spy_reset(p, s, **kw):
        roots.append(kw["confine_root"])
        return real_reset(p, s, **kw)

    def spy_strip(p, **kw):
        roots.append(kw["confine_root"])
        return real_strip(p, **kw)

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(devcontract, "reset_spec_status", spy_reset)
    monkeypatch.setattr(devcontract, "strip_auto_run_result", spy_strip)
    _run_dir, spec = _stories_paused_run(
        project.project,
        stage="plan-checkpoint",
        worktree_path=str(_unit_worktree(project.project)),
        spec_outside_worktree=True,
    )
    assert not spec.is_relative_to(_unit_worktree(project.project))  # the shape under test

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-replan"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])
    assert roots == [project.project, project.project]
    assert verify.read_frontmatter(spec)["status"] == "draft"


async def test_plan_checkpoint_renders_the_worktree_spec_under_isolation(project_tree, monkeypatch):
    """The read half of the same anchor: the viewers show the spec the run used.

    Pre-fix the raw relpath resolved against the TUI's cwd and the modal rendered the
    main checkout's twin — same layout, different file, nothing on screen to say so.
    The `chdir` and the per-tree bodies are load-bearing for the same reasons the
    replan row documents.

    Ablation: revert `_paused_spec` to `Path(task.spec_file)` and this reddens — the
    body is the twin's "# plan for 1".
    """
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _stories_paused_run(
        project_tree.project,
        stage="plan-checkpoint",
        worktree_path=str(_unit_worktree(project_tree.project)),
    )
    monkeypatch.chdir(project_tree.project)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        body = render(app.screen.query_one("#spec Static", Static).content)
        assert "# worktree plan for 1" in body
        assert "# plan for 1" not in body  # the main-checkout twin's body


async def test_spec_approval_gate_renders_the_worktree_spec_under_isolation(
    project_tree, monkeypatch
):
    """The same anchor on the surface the matrix row names: the GATE viewer.

    `_paused_spec` has three consumers and they reach it by different stages —
    plan-checkpoint (`_review_plan_checkpoint`), the spec-approval / epic-boundary /
    story-gate trio (`_review_gate`), and escalation (`_review_escalation`). The
    replan rows above only reach the first, so this pins the gate arm: an operator
    approving a frozen spec must be looking at the spec the run actually froze, not
    the main checkout's twin at the same relpath.

    Ablation: revert `_paused_spec` to `Path(task.spec_file)` and this reddens — the
    body is the twin's "# plan for 1".
    """
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _stories_paused_run(
        project_tree.project,
        stage="spec-approval",
        worktree_path=str(_unit_worktree(project_tree.project)),
    )
    monkeypatch.chdir(project_tree.project)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        body = render(app.screen.query_one("#spec Static", Static).content)
        assert "# worktree plan for 1" in body
        assert "# plan for 1" not in body  # the main-checkout twin's body


async def test_paused_spec_undecodable_spec_does_not_crash_the_dashboard(project, monkeypatch):
    """A non-UTF-8 spec degrades one byte, not the whole document — and never raises.

    `read_text(encoding="utf-8")` raises `UnicodeDecodeError`, which is a ValueError and
    so escaped the `except OSError` arm entirely; all three review surfaces call
    `_paused_spec` from the Textual event loop, where an escaping raise kills the
    dashboard instead of rendering the fault. Closed the way `_commit_subject` closes it
    — `errors="replace"` — rather than by widening the except arm, because replacing the
    entire body with a failure sentence cost the reviewer the WHOLE spec at a gate whose
    only purpose is reading it. The failure body is now reserved for ABSENCE, which is
    the case the anchoring argument is actually about
    (`test_paused_spec_missing_at_the_anchor_reads_as_not_found`).

    Ablation: restore `path.read_text(encoding="utf-8")` and this reddens — the modal
    never opens, because the worker raised.
    """
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _run_dir, spec = _stories_paused_run(project.project, stage="plan-checkpoint")
    spec.write_bytes(b"---\nstatus: ready-for-dev\n---\n\n# plan caf\xe9 for 1\n")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        body = render(app.screen.query_one("#spec Static", Static).content)
        assert "(empty spec)" not in body
        assert "could not be read" not in body
        assert "plan caf" in body  # the readable remainder survived the bad byte
        # a decode fault is not an unreviewable spec, so the actions stay live
        assert not app.screen.query_one("#act-approve", Button).disabled


async def test_replan_on_an_undecodable_spec_does_not_crash_the_dashboard(
    project_tree, monkeypatch
):
    """The read-side fix made this button REACHABLE; the write side had to catch up.

    `devcontract.reset_spec_status` decodes strictly (`read_bytes().decode("utf-8")`),
    and `_do_replan` caught only `(OSError, FrontmatterWriteError)` —
    `UnicodeDecodeError` is a ValueError, so it escaped both. Before this change the
    dashboard died earlier, at render, so the operator never got here. Once `_paused_spec`
    began degrading a non-UTF-8 spec in place, the modal opens, the button is live, and
    pressing it raised inside a Textual worker: the same event-loop crash the read-side
    fix exists to prevent, moved one click later.

    Ablation: drop `UnicodeDecodeError` from `_do_replan`'s except tuple and this reddens
    — the worker raises instead of notifying, and the run never fails safe.
    """
    calls: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _run_dir, spec = _stories_paused_run(project_tree.project, stage="plan-checkpoint")
    spec.write_bytes(b"---\nstatus: ready-for-dev\n---\n\n# plan caf\xe9 for 1\n")

    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-replan"))
        await pilot.pause()
        assert app.is_running  # the dashboard survived the failed write
    assert calls == []  # and the run was NOT resumed on an unreplanned spec


async def test_unreadable_spec_refuses_the_destructive_actions(project_tree, monkeypatch):
    """A spec nobody could read is a gate nobody reviewed.

    `_paused_spec` reports the read failure as the body so it cannot be confused with
    "(empty spec)", but the modal still rendered it in the style reserved for the spec's
    own words and still offered `Approve & resume` — which resumes the run past a gate
    whose whole purpose is a human reading the file. The verb is refused at the source
    rather than left to fail downstream (replan was safe only by accident: the reset
    returns False and the "could not reset" branch declines).

    Ablation: drop `disabled=self._unreadable` from `SpecReviewModal.compose` and this
    reddens on the button state.
    """
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _run_dir, spec = _stories_paused_run(project_tree.project, stage="plan-checkpoint")
    spec.unlink()  # absent at the anchored path

    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        body = render(app.screen.query_one("#spec Static", Static).content)
        assert "could not be read" in body
        assert app.screen.query_one("#act-approve", Button).disabled
        assert app.screen.query_one("#act-replan", Button).disabled


async def test_escalation_modal_reads_the_worktree_spec_under_isolation(project_tree, monkeypatch):
    """Matrix row 3's THIRD consumer — the one the operator re-arms from.

    `_paused_spec` feeds `_blocking_condition`, whose `## Auto Run Result` block is the
    terminal verdict an operator reads before deciding to re-arm or resolve. The plan-
    checkpoint and gate surfaces were graded under isolation; this one was not, so the
    pre-fix bug — showing the MAIN CHECKOUT's verdict for a run whose real halt is in
    the mount — had no row at all.

    Ablation: revert `_paused_spec` to `Path(task.spec_file)` and this reddens on the
    blocking condition — the modal reports the decoy twin's.
    """
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    wt = _unit_worktree(project_tree.project)
    _run_dir, spec = _stories_paused_run(
        project_tree.project, stage="escalation", worktree_path=str(wt), blocked_result="decoy halt"
    )
    # the fixture copies one body into both trees; the halt text has to differ for
    # "read the run's tree" to be checkable against "did not read the other one"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace("decoy halt", "the mounts real halt"),
        encoding="utf-8",
    )
    monkeypatch.chdir(project_tree.project)  # what the TUI actually runs from

    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        body = render(app.screen.query_one("#blocking Static", Static).content)
        assert "the mounts real halt" in body
        assert "decoy halt" not in body


async def test_sentinel_indicator_reads_the_worktree_under_isolation(project_tree, monkeypatch):
    """The other half of the same modal had to move with it.

    `_sentinel_kind` scanned `self.project` while `_paused_spec` anchored on the run's
    tree, and BOTH feed one `EscalationModal`. Under isolation the engine writes the
    sentinel into the mount (`stories_engine._stories_folder` IS the worktree during a
    driven story), so a modal built from two trees could show the mount's spec text
    beside "no sentinel" — a pre-planning wedge presenting as an ordinary escalation,
    which is a different operator decision.

    The main checkout's copy is removed so the two anchors give different answers;
    with a twin present, both spellings find a sentinel and nothing is graded.

    Ablation: revert `_sentinel_kind` to `stories.resolve_spec_folder(self.project, ...)`
    and this reddens — the indicator disappears.
    """
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    wt = _unit_worktree(project_tree.project)
    _run_dir, spec = _stories_paused_run(
        project_tree.project, stage="escalation", worktree_path=str(wt), sentinel=True
    )
    (project_tree.project / spec.relative_to(wt)).unlink()  # only the mount has the sentinel
    monkeypatch.chdir(project_tree.project)

    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        shown = " ".join(render(s.content) for s in app.screen.query(Static))
        assert "pre-planning-halt sentinel" in shown


def test_paused_spec_root_without_a_task_answers_the_live_project(tmp_path):
    """Both arms of `_paused_spec_root` make ONE claim about the project.

    The delegate (`runs.live_spec_root`) carries the recorded anchor onto the tree
    the dashboard was opened against — `self.project`, the same mapping `_do_rearm`
    hands `rearm_escalation` — while `state.project` is the string the run persisted
    at launch. The two differ after a project move, so a no-task arm answering
    `state.project` raw was a second claim for a future caller to trip on: a confine
    root naming the OLD tree for a path `_paused_spec` now anchors on the new one.

    Graded directly because the arm is unreachable from the write path today:
    `_review_plan_checkpoint`'s `done()` refuses a `None` `spec_path` before calling
    `_do_replan`, and `_paused_spec` returns `None` exactly when there is no task. An
    end-to-end row could not reach it, so this calls the method.

    Ablation: return `Path(state.project)` from the no-task arm and this reddens —
    the two directories are deliberately different here.
    """
    app = BmadLoopApp(tmp_path / "opened-here")
    state = RunState(
        run_id="20260611-100000-aaaa",
        project=str(tmp_path / "persisted-at-launch"),
        started_at="2026-06-11T10:00:00",
    )
    assert state.paused_story_key is None  # the no-task arm
    assert app._paused_spec_root(state) == app.project
    assert app._paused_spec_root(state) != tmp_path / "persisted-at-launch"


def test_paused_spec_follows_a_moved_project_to_the_tree_the_rearm_writes(tmp_path):
    """The escalation modal's READ anchor and `_do_rearm`'s WRITE anchor name one
    file. `_do_rearm` hands `rearm_escalation` `project_root=self.project`, so after a
    project move the re-arm flips the copy under the live tree; anchored on the
    recorded `state.project` alone, the modal showed the OLD tree's copy — unreadable
    once that tree is gone, which disabled the very re-arm the live mapping exists
    for, and when both exist the operator reviewed one spec and re-armed another.

    The old tree is absent here on purpose: an anchor that did not move reads as
    "could not be read", so the row cannot pass by finding a stale twin.

    Ablations: revert `_paused_spec` to `runs.task_spec_path` and this reddens on
    `readable`; revert `_paused_spec_root` to `runs.task_spec_root` and it reddens on
    the confine root, which must be the live tree the path sits under."""
    recorded = tmp_path / "project-before-rename"
    live = tmp_path / "project-after-rename"
    rel = Path("_bmad-output") / "implementation-artifacts" / "spec-1-1-a.md"
    (live / rel).parent.mkdir(parents=True)
    (live / rel).write_text(
        "---\nstatus: escalated\n---\n\n# Story\n", encoding="utf-8", newline="\n"
    )
    assert not recorded.exists()  # the tree the run recorded is gone
    app = BmadLoopApp(live)
    state = RunState(
        run_id="20260611-100000-aaaa",
        project=str(recorded),
        started_at="2026-06-11T10:00:00",
        paused_story_key="1-1-a",
    )
    state.tasks["1-1-a"] = StoryTask(story_key="1-1-a", epic=1, spec_file=str(recorded / rel))

    spec_path, spec_text, readable = app._paused_spec(state)

    assert readable is True
    assert spec_path == live / rel
    assert spec_text.startswith("---\nstatus: escalated")
    assert app._paused_spec_root(state) == live


def test_story_context_and_sentinel_follow_a_moved_project_to_the_tree_the_rearm_writes(
    tmp_path,
):
    """The modal's OTHER two readers move with `_paused_spec`. `_story_context` and
    `_sentinel_kind` located the stories folder from `runs.task_stories_root`, whose
    no-mount arm is the recorded `state.project`, while `_do_rearm` clears the sentinel
    under the live tree (`rearm_escalation(..., project_root=self.project)`). After a
    project move the modal showed the spec from the live tree beside a title,
    description and sentinel indicator from the old one — omitted once that tree was
    gone, stale while it lingered.

    The old tree is absent here on purpose: an anchor that did not move finds no
    manifest and no sentinel, so the row cannot pass on a stale twin.

    Ablations: revert `_story_context` to `runs.task_stories_root(...)` and this
    reddens on the title (`('', '') == ('Story 1', 'does a thing')`); revert
    `_sentinel_kind` the same way and it reddens on the kind (`'' == 'unresolved'`)."""
    import yaml

    from bmad_loop import stories

    recorded = tmp_path / "project-before-rename"
    live = tmp_path / "project-after-rename"
    folder = live / "epic-1"
    (folder / "stories").mkdir(parents=True)
    (folder / "stories.yaml").write_text(
        yaml.safe_dump(
            [{"id": "1", "title": "Story 1", "description": "does a thing"}], sort_keys=False
        ),
        encoding="utf-8",
        newline="\n",
    )
    spec = folder / "stories" / "1-unresolved.md"
    spec.write_text("---\nstatus: escalated\n---\n", encoding="utf-8", newline="\n")
    assert stories.resolve_story_spec(folder, "1").kind == stories.KIND_SENTINEL
    assert not recorded.exists()  # the tree the run recorded is gone
    app = BmadLoopApp(live)
    state = RunState(
        run_id="20260611-100000-aaaa",
        project=str(recorded),
        started_at="2026-06-11T10:00:00",
        source="stories",
        spec_folder="epic-1",
        paused_story_key="1",
    )
    state.tasks["1"] = StoryTask(
        story_key="1", epic=0, spec_file=str(recorded / "epic-1" / "stories" / "1-unresolved.md")
    )

    assert app._story_context(state, "1") == ("Story 1", "does a thing")
    assert app._sentinel_kind(state, "1") == "unresolved"


async def test_paused_spec_missing_at_the_anchor_reads_as_not_found(project_tree, monkeypatch):
    """An absent spec at the ANCHORED path is the signal that the anchoring failed, so
    it must not render as `SpecReviewModal`'s `(empty spec)` — which is also what a
    spec that read fine and is blank renders as. Ablation: return `path, ""` from
    `_paused_spec`'s degrade arm and this reddens on the `(empty spec)` assertion."""
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _run_dir, spec = _stories_paused_run(project_tree.project, stage="plan-checkpoint")
    spec.unlink()
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        body = render(app.screen.query_one("#spec Static", Static).content)
        assert "(empty spec)" not in body
        assert "could not be read" in body


async def test_plan_checkpoint_replan_refuses_a_control_alias_run_before_mutating(
    project_tree, monkeypatch
):
    """Through the ENTRY POINT (the modal's Replan button): a run persisted by
    an older release under `ctl` must not have its spec reset to draft ahead
    of the child `bmad-loop resume`'s refusal — the TUI is a second frontend
    onto the same state, and it kept the mutate-then-refuse shape after the
    CLI entry gates closed it.

    Ablate `_blocked_by_control_alias` in `_do_replan` and this fails: the
    spec is reset and the resume child is launched."""
    from bmad_loop import devcontract

    calls: list[str] = []
    resets: list[tuple] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        devcontract, "reset_spec_status", lambda p, s, **kw: resets.append((p, s)) or True
    )
    monkeypatch.setattr(devcontract, "strip_auto_run_result", lambda p, **kw: True)
    _stories_paused_run(project_tree.project, stage="plan-checkpoint", run_id="ctl")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-replan"))
        await until(pilot, lambda: not isinstance(app.screen, SpecReviewModal))
        await pilot.pause()
        assert resets == []  # the spec was NOT rewritten ahead of the refusal
        assert calls == []  # and no resume child was launched to bounce off the CLI gate


async def test_tui_rearm_refuses_a_control_alias_run_before_mutating(project_tree, monkeypatch):
    """The re-arm path (`_do_rearm`, resolve-modal Re-arm & resume) gates
    ahead of `rearm_escalation` — the pre-launch mutation the launcher's own
    chokepoint gate cannot protect. Direct method drive inside a running app
    — the modal wiring is pinned by the existing checkpoint tests, and the
    launch paths themselves (resume, resolve, and any future button) are
    gated at their convergence, `launch.start_detached`, graded in
    test_tui_launch.py.

    Ablate `_blocked_by_control_alias` in `_do_rearm` and the rearm recorder
    fills."""
    from bmad_loop import runs

    rearms: list[tuple] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(runs, "rearm_escalation", lambda rd, sk, **kw: rearms.append((rd, sk)))
    run_dir, _spec = _stories_paused_run(
        project_tree.project, stage="plan-checkpoint", run_id="ctl"
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app._do_rearm("ctl", run_dir, "1")
        await pilot.pause()
        assert rearms == []


@pytest.mark.parametrize("fault", ["decode", "os"])
async def test_resume_confirm_refuses_unreadable_sweep_ledger(project_tree, monkeypatch, fault):
    """DW-270: plain resume displays the real probe's refusal on the dashboard.

    Ablation: remove the ledger refusal block from `_do_resume`; both rows fail
    because the detached-launch recorder fills.
    Ablation: remove `markup=False` from the refusal toast; its rendered text
    loses the literal `[red]` path component.
    """
    install_bmad_config(project_tree)
    config = project_tree.project / BMAD_CONFIG_REL
    config.write_text(
        config.read_text().replace("implementation-artifacts'", "implementation-artifacts/[red]'"),
        encoding="utf-8",
    )
    ledger = project_tree.implementation_artifacts / "[red]" / "deferred-work.md"
    ledger.parent.mkdir()
    ledger.write_bytes(UNDECODABLE_LEDGER if fault == "decode" else READABLE_LEDGER)
    read_refused = True
    if fault == "os":
        read_text = Path.read_text

        def refused_read(path, *args, **kwargs):
            if path == ledger and read_refused:
                raise PermissionError(13, "Permission denied", str(path))
            return read_text(path, *args, **kwargs)

        # Refuse only this sandbox file's text read, through the real reader and
        # probe. chmod is not reliable under root or on Windows.
        monkeypatch.setattr(Path, "read_text", refused_read)

    resumes = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: resumes.append(rid) or "@1")
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    run_dir = _escalated_sweep_run(project_tree.project)
    original_state = (run_dir / "state.json").read_bytes()
    refusal = runs_mod.unreadable_sweep_ledger(project_tree.project, run_dir)
    assert refusal is not None
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(notifications=True) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id == run_dir.name)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        await click(pilot, await ready(pilot, "#ok"))
        await pilot.pause()
        assert resumes == []
        assert (run_dir / "state.json").read_bytes() == original_state
        assert (refusal, "error") in notifications_with_severity(app)
        assert str(app.screen.query_one("Toast", Static).render()) == refusal
        assert str(ledger) in refusal
        assert "bmad-loop sweep" in refusal
        assert "stays resumable" in refusal
        if fault == "os":
            assert "permissions or storage" in refusal

        # Repair and retry in this same dashboard, retaining the paused run.
        read_refused = False
        ledger.write_bytes(READABLE_LEDGER)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: resumes == [run_dir.name])
        await pilot.pause()
        assert resumes == [run_dir.name]


@pytest.mark.parametrize("case", ["readable", "story", "absent", "config", "state"])
async def test_resume_confirm_ledger_probe_preserves_handoff(project_tree, monkeypatch, case):
    """The real probe permits readable sweeps and declines outside its scope.

    Unavailable state is introduced AFTER opening the modal so the confirmation
    reaches the probe; action_resume_run owns the earlier state-read refusal.
    """
    if case != "config":
        install_bmad_config(project_tree)
    if case != "absent":
        project_tree.deferred_work.write_bytes(
            READABLE_LEDGER if case == "readable" else UNDECODABLE_LEDGER
        )
    resumes = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: resumes.append(rid) or "@1")
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    run_dir = make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        run_type="story" if case == "story" else "sweep",
        paused_stage="DEV_VERIFY",
        paused_reason="verify failed",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id == run_dir.name)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        if case == "state":
            (run_dir / "state.json").unlink()
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: resumes == [run_dir.name])
        await pilot.pause()
        assert any(f"resume of {run_dir.name} launched" in m for m in notifications(app))


@pytest.mark.parametrize("guard", ["mux", "alive", "unknown"])
async def test_resume_confirm_guards_precede_ledger_probe(project_tree, monkeypatch, guard):
    """Earlier guards win even if the sweep ledger is unreadable.

    Ablation: move the ledger block above the mux/liveness guards in `_do_resume`;
    the probe recorder fills instead of the existing guard owning the refusal.
    """
    install_bmad_config(project_tree)
    project_tree.deferred_work.write_bytes(UNDECODABLE_LEDGER)
    probes = []
    resumes = []
    probe = runs_mod.unreadable_sweep_ledger

    def record_probe(root, run_dir):
        probes.append(run_dir)
        return probe(root, run_dir)

    monkeypatch.setattr(runs_mod, "unreadable_sweep_ledger", record_probe)
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: resumes.append(rid) or "@1")
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    run_dir = _escalated_sweep_run(project_tree.project)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id == run_dir.name)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        # Change the guard at confirmation time to exercise `_do_resume` itself.
        if guard == "mux":
            monkeypatch.setattr(launch, "mux_available", lambda: False)
            message, severity = "multiplexer backend unavailable", "error"
        else:
            monkeypatch.setattr(data, "liveness", lambda run_dir: guard)
            if guard == "unknown":
                (run_dir / "engine.pid").write_text("4242 123.0", encoding="utf-8")
            message, severity = "may still be live", "warning"
        await click(pilot, await ready(pilot, "#ok"))
        await pilot.pause()
        assert probes == []
        assert resumes == []
        assert any(message in m and s == severity for m, s in notifications_with_severity(app))


def _escalated_sweep_run(root: Path, run_id: str = "20260611-100000-aaaa") -> Path:
    """A SWEEP run paused at a CRITICAL escalation — the only run shape
    `runs.unreadable_sweep_ledger` is scoped to, and the shape `_do_rearm`'s ledger
    probe is graded on. Sweeps really do park at `PAUSE_ESCALATION` (an escalated
    bundle resolves like a story escalation), so this is a reachable state, not a
    fixture-only one."""
    from bmad_loop.model import PAUSE_ESCALATION

    return make_run(
        root,
        run_id,
        run_type="sweep",
        tasks={"s1": StoryTask(story_key="s1", epic=1, phase=Phase.ESCALATED)},
        paused_stage=PAUSE_ESCALATION,
        paused_reason="CRITICAL escalation",
        paused_story_key="s1",
    )


async def test_tui_rearm_refuses_a_sweep_run_whose_ledger_does_not_decode(
    project_tree, monkeypatch
):
    """DW-230: the readable-ledger refusal `cli.cmd_resume`/`cli.cmd_resolve` make,
    made HERE too.

    This gesture re-arms and then hands off to `launch.resume_detached`, so without
    the probe the detached child refuses for the same reason — but only after
    `rearm_escalation` has spent the escalation, and into a pane nobody opens. The
    toast is the point: on screen, with the escalation still armed.

    Ablation: delete the `runs.unreadable_sweep_ledger` block from `_do_rearm` and the
    re-arm recorder fills instead of returning the ledger refusal. The persisted
    phase assertion complements that recorder; the stub itself does not mutate state."""
    from bmad_loop import runs
    from bmad_loop.journal import load_state

    install_bmad_config(project_tree)
    project_tree.implementation_artifacts.mkdir(parents=True, exist_ok=True)
    project_tree.deferred_work.write_bytes(
        UNDECODABLE_LEDGER
    )  # conftest's, so the CLI rows share it

    rearms: list = []
    resumes: list = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: resumes.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        runs,
        "rearm_escalation",
        lambda rd, sk, **kw: rearms.append((rd, sk)) or _rearm_outcome(sk),
    )
    run_dir = _escalated_sweep_run(project_tree.project)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app._do_rearm("20260611-100000-aaaa", run_dir, "s1")
        await pilot.pause()
        assert rearms == []  # the escalation is not spent
        assert resumes == []  # and no detached child to bounce off the CLI gate
        assert load_state(run_dir).tasks["s1"].phase == Phase.ESCALATED
        # Severity, not just text: a refusal softened to an information toast reads
        # as advice beside a gesture that appeared to work, and matching on wording
        # alone would not notice.
        toast, severity = next(
            (m, s) for m, s in notifications_with_severity(app) if "bmad-loop sweep" in m
        )
        assert severity == "error"
        assert str(project_tree.deferred_work) in toast
        assert "stays resumable" in toast


async def test_tui_rearm_proceeds_on_a_readable_ledger(project_tree, monkeypatch):
    """The probe's happy path at this call site: a ledger that decodes is not the
    fault it screens for, so the gesture re-arms and resumes exactly as before.

    Without this row the refusal row above passes just as well against a `_do_rearm`
    that refused EVERY escalated sweep. Ablation: make the probe return a refusal
    unconditionally and this reddens on both recorders."""
    from bmad_loop import runs

    install_bmad_config(project_tree)
    project_tree.implementation_artifacts.mkdir(parents=True, exist_ok=True)
    project_tree.deferred_work.write_bytes(READABLE_LEDGER)

    rearms: list = []
    resumes: list = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: resumes.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        runs,
        "rearm_escalation",
        lambda rd, sk, **_k: rearms.append(sk) or _rearm_outcome(sk),
    )
    run_dir = _escalated_sweep_run(project_tree.project)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app._do_rearm("20260611-100000-aaaa", run_dir, "s1")
        # `_do_rearm` is synchronous, so its recorders fill before the message pump has
        # delivered a single toast — pump first, or `notifications(app)` reads empty and
        # every "no such toast" assertion below passes for the wrong reason.
        await pilot.pause()
        await until(pilot, lambda: rearms == ["s1"] and resumes == ["20260611-100000-aaaa"])
        assert any("re-armed s1" in m for m in notifications(app))  # toasts ARE captured
        assert not any("bmad-loop sweep" in m for m in notifications(app))


async def test_tui_rearm_ledger_gate_declines_when_it_cannot_answer(project_tree, monkeypatch):
    """The probe cannot locate a ledger without the BMAD config, so it declines and
    the gesture proceeds — the fault's own owner answers for it (here `_do_rearm`'s
    existing "cannot read the project config" warning, which re-arms against the
    root the run recorded). A gate that answered for it would report a ledger it
    never managed to find."""
    from bmad_loop import runs

    # The corrupt ledger IS on disk; what is missing is _bmad/bmm/config.yaml, so
    # `bmadconfig.load_paths` raises inside the probe before it can resolve a path.
    project_tree.implementation_artifacts.mkdir(parents=True, exist_ok=True)
    project_tree.deferred_work.write_bytes(UNDECODABLE_LEDGER)

    rearms: list = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: None)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        runs,
        "rearm_escalation",
        lambda rd, sk, **_k: rearms.append(sk) or _rearm_outcome(sk),
    )
    run_dir = _escalated_sweep_run(project_tree.project)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app._do_rearm("20260611-100000-aaaa", run_dir, "s1")
        await pilot.pause()  # deliver the toasts the synchronous call queued
        await until(pilot, lambda: rearms == ["s1"])
        assert any("cannot read the project config" in m for m in notifications(app))
        assert not any("bmad-loop sweep" in m for m in notifications(app))


async def test_tui_rearm_live_refusal_wins_over_the_ledger_gate(project_tree, monkeypatch):
    """The probe sits AFTER the alias/liveness gates, and that ordering is load-bearing
    rather than incidental: a provably-live engine is the stronger fact (re-driving one
    corrupts the run itself, while an unreadable ledger only costs an arm), and
    repairing the ledger would not make THIS re-arm safe. Mirrors
    `test_resolve_live_refusal_wins_over_the_ledger_gate` on the CLI side.

    Without this row, hoisting the probe above `_resolve_blocked_by_liveness` keeps the
    whole suite green. Ablation: move the probe block above that gate and this reddens
    on the ledger toast it must not produce. An early probe whose result is discarded
    instead fails the read recorder while preserving the live-engine toast."""
    from bmad_loop import deferredwork, runs
    from bmad_loop.journal import load_state

    install_bmad_config(project_tree)
    project_tree.implementation_artifacts.mkdir(parents=True, exist_ok=True)
    project_tree.deferred_work.write_bytes(UNDECODABLE_LEDGER)

    ledger_reads = []
    read_for_write = deferredwork.read_for_write

    def record_read(path):
        ledger_reads.append(path)
        return read_for_write(path)

    monkeypatch.setattr(deferredwork, "read_for_write", record_read)

    rearms: list = []
    resumes: list = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: resumes.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    monkeypatch.setattr(runs, "rearm_escalation", lambda rd, sk, **kw: rearms.append((rd, sk)))
    run_dir = _escalated_sweep_run(project_tree.project)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app._do_rearm("20260611-100000-aaaa", run_dir, "s1")
        await pilot.pause()
        assert any("may still be live" in m for m in notifications(app))
        assert not any("bmad-loop sweep" in m for m in notifications(app))
        assert ledger_reads == []  # includes probes whose refusal was discarded
        assert rearms == []
        assert resumes == []
        assert load_state(run_dir).tasks["s1"].phase == Phase.ESCALATED


async def test_tui_rearm_refuses_an_os_refused_ledger_read_with_the_probe_route(
    project_tree, monkeypatch
):
    """The DW-234 row on the TUI surface. `runs.unreadable_sweep_ledger` now refuses an
    OS-refused read itself, with the permissions-or-storage repair and the
    `bmad-loop sweep` route, so `_do_rearm`'s stopgap `except OSError` (which toasted
    the bare fault "rather than waiting for DW-234") is gone: the probe's refusal
    reaches the error toast through the same `refusal is not None` arm the decode
    refusal takes, and nothing is re-armed or launched.

    Ablation: delete the probe's `except OSError` arm and the row fails on the
    PermissionError escaping `_do_rearm` — there is no local catch left to absorb it,
    which is the point: one arm, in the shared helper, for all three entry points."""
    from bmad_loop import deferredwork, runs
    from bmad_loop.journal import load_state

    install_bmad_config(project_tree)
    project_tree.implementation_artifacts.mkdir(parents=True, exist_ok=True)
    # DECODABLE bytes on disk: the refused read is the only fault in play. Monkeypatched
    # rather than chmod'd — a mode bit does not hold as root and does not exist on
    # Windows, so the row would silently stop testing anything.
    project_tree.deferred_work.write_bytes(READABLE_LEDGER)

    def _refused(path):
        raise PermissionError(13, "Permission denied", str(path))

    rearms: list = []
    resumes: list = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: resumes.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(runs, "rearm_escalation", lambda rd, sk, **kw: rearms.append((rd, sk)))
    monkeypatch.setattr(deferredwork, "read_for_write", _refused)
    run_dir = _escalated_sweep_run(project_tree.project)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app._do_rearm("20260611-100000-aaaa", run_dir, "s1")  # must not raise
        await pilot.pause()
        toast, severity = next(
            (m, s) for m, s in notifications_with_severity(app) if "Errno 13" in m
        )
        assert severity == "error"
        # The probe's own refusal, route included — not a locally-worded toast.
        assert str(project_tree.deferred_work) in toast
        assert "permissions or storage" in toast
        assert "bmad-loop sweep" in toast
        assert "stays resumable" in toast
        assert rearms == []
        assert resumes == []
        assert load_state(run_dir).tasks["s1"].phase == Phase.ESCALATED


async def test_story_checkpoint_continue_resumes(project, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _stories_paused_run(
        project.project,
        stage="story-checkpoint",
        spec_status="done",
        spec_checkpoint=False,
        done_checkpoint=True,
        commit_sha="abc1234def5678",
    )
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, StoryCheckpointModal)
        await click(pilot, await ready(pilot, "#act-continue"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


async def test_story_checkpoint_stop_marks_stopped(project_tree, monkeypatch):
    from bmad_loop import runs

    stops: list[Path] = []
    kills: list[tuple[Path, str]] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(runs, "stop_run", lambda rd: stops.append(rd) or True)
    monkeypatch.setattr(launch, "kill_ctl_window", lambda proj, rid: kills.append((proj, rid)))
    _stories_paused_run(
        project_tree.project,
        stage="story-checkpoint",
        spec_status="done",
        spec_checkpoint=False,
        done_checkpoint=True,
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, StoryCheckpointModal)
        await click(pilot, await ready(pilot, "#act-stop"))
        await until(pilot, lambda: len(kills) == 1)
    assert stops == [project_tree.project / runs.RUNS_DIR / "20260611-100000-aaaa"]
    assert kills == [(project_tree.project, "20260611-100000-aaaa")]


def test_checkpoint_gate_line_pluralization():
    # The gate line is derived, not hardcoded — and pluralizes the real cycle count.
    f = BmadLoopApp._checkpoint_gate_line
    assert f(0) == "verify + review gates passed · no follow-up review cycles"
    assert f(1) == "verify + review gates passed · 1 follow-up review cycle"
    assert f(3) == "verify + review gates passed · 3 follow-up review cycles"


def test_commit_subject_routes_the_chokepoint_and_replaces_undecodable_bytes(tmp_path, monkeypatch):
    """`_commit_subject` consults `verify.git_bytes` and decodes with replace: a
    subject byte invalid in UTF-8 degrades to U+FFFD instead of raising. The
    pre-#390 bare spawn decoded strictly, and its `(OSError, SubprocessError)`
    guard covered neither `UnicodeDecodeError` nor the GitError the chokepoint
    turns it into — one odd byte crashed the story-checkpoint modal. The fake
    also pins `timeout_s=5`: this call sits on the event loop, so the pre-#390
    five-second deadline must survive the reroute (a stalled git degrades a
    label, never freezes the UI for the 120s module default). Ablation: revert
    to the bare `subprocess.run` and this fails on the routing half alone — the
    fake is never consulted, tmp_path is no repo, and "" comes back."""

    def latin1_subject(repo, *args, timeout_s=None):
        assert repo == tmp_path
        assert args == ("log", "-1", "--format=%s", "abc123")
        assert timeout_s == 5
        return subprocess.CompletedProcess(["git", *args], 0, b"caf\xe9 fix\n", b"")

    monkeypatch.setattr(verify, "git_bytes", latin1_subject)
    app = BmadLoopApp(tmp_path)
    assert app._commit_subject("abc123") == "caf� fix"


@pytest.mark.parametrize("fault", [verify.GitError, verify.GitSpawnError])
def test_commit_subject_degrades_on_a_chokepoint_fault(tmp_path, monkeypatch, fault):
    """A timeout or failed spawn arrives as GitError / its GitSpawnError subclass —
    the subject degrades to empty and the modal still renders, mirroring the
    rc-nonzero arm an unknown sha already takes."""

    def unanswerable(repo, *args, timeout_s=None):
        raise fault("git log did not answer")

    monkeypatch.setattr(verify, "git_bytes", unanswerable)
    app = BmadLoopApp(tmp_path)
    assert app._commit_subject("abc123") == ""


# ------------------------------------------------- hard stop (x) & archive (A)


async def test_stop_run_stops_and_kills_ctl_window(project_tree, monkeypatch):
    # x on a live run confirms, then the worker runs BOTH halves of the hard stop:
    # runs.stop_run (signal + mark) and launch.kill_ctl_window (the run's #482
    # ctl window). Monkeypatched at the same seam the graceful-stop tests use, so
    # nothing signals a real engine and no multiplexer is touched.
    from bmad_loop import runs

    stops: list[Path] = []
    kills: list[tuple[Path, str]] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    monkeypatch.setattr(runs, "stop_run", lambda rd: stops.append(rd) or True)
    monkeypatch.setattr(launch, "kill_ctl_window", lambda proj, rid: kills.append((proj, rid)))
    make_run(project_tree.project, "20260611-100000-aaaa", alive=True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("x")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        needle = "run 20260611-100000-aaaa stopped"
        await until(pilot, lambda: any(needle in m for m in notifications(app)))
    assert stops == [project_tree.project / RUNS_DIR / "20260611-100000-aaaa"]
    assert kills == [(project_tree.project, "20260611-100000-aaaa")]


@pytest.mark.parametrize("live", ["dead", "unknown"])
async def test_stop_run_not_live_warns_without_calling(project, monkeypatch, live):
    """`x` is the hard stop: it only ever fires at a *provably alive* engine, so the
    gate is `== "alive"` and an unverifiable ('unknown') pid is refused alongside a
    dead one — deliberately stricter than `S`'s `!= "dead"` gate, because killing
    the agent window on a pid we cannot identify is not recoverable the way a
    control-file request is. Neither helper is called and no confirm modal opens.

    Ablation target: delete the `if not data.liveness(run_dir) == "alive":`
    warn-and-return block from `action_stop_run` and both rows fail at the toast
    wait — the confirm modal opens instead."""
    from bmad_loop import runs

    stops: list[Path] = []
    kills: list[tuple[Path, str]] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: live)
    monkeypatch.setattr(runs, "stop_run", lambda rd: stops.append(rd) or True)
    monkeypatch.setattr(launch, "kill_ctl_window", lambda proj, rid: kills.append((proj, rid)))
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("x")
        await until(pilot, lambda: any("is not live" in m for m in notifications(app)))
        assert stops == []
        assert kills == []
        assert not isinstance(app.screen, ConfirmModal)


async def test_archive_run_archives_and_forgets(project_tree, monkeypatch):
    # A on a concluded run confirms, then the worker archives via the runs helper
    # and tells the dashboard to forget the now-gone run dir (selection drop +
    # rescan) before toasting the destination.
    from bmad_loop import runs

    archived: list[tuple[Path, Path]] = []
    forgotten: list[str] = []
    dest = project_tree.project / ".bmad-loop" / "archive" / "20260611-100000-aaaa.tar.gz"
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        runs, "archive_run", lambda proj, rd, **_kw: archived.append((proj, rd)) or dest
    )
    monkeypatch.setattr(DashboardScreen, "forget_run", lambda self, rid: forgotten.append(rid))
    make_run(project_tree.project, "20260611-100000-aaaa", finished=True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("A")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any(str(dest) in m for m in notifications(app)))
    assert archived == [
        (project_tree.project, project_tree.project / RUNS_DIR / "20260611-100000-aaaa")
    ]
    assert forgotten == ["20260611-100000-aaaa"]


async def test_archive_live_run_refused_without_calling(project_tree, monkeypatch):
    """Archiving compresses the run dir and removes the original, so a live engine's
    open run dir is refused up front — the same guard `D` applies — rather than
    racing the writer. The helper is never called and no confirm modal opens.
    ('unknown' is not blocked here, only warned inside the confirm; see
    test_delete_unknown_pid_warns_but_does_not_block for the sibling gate.)

    Ablation target: delete the `if live == "alive":` warn-and-return block from
    `action_archive_run` and this fails at the toast wait — the archive confirm
    opens on a live run instead."""
    from bmad_loop import runs

    archived: list[tuple[Path, Path]] = []
    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    monkeypatch.setattr(
        runs, "archive_run", lambda proj, rd, **_kw: archived.append((proj, rd)) or proj
    )
    make_run(project_tree.project, "20260611-100000-aaaa", alive=True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("A")
        await until(pilot, lambda: any("is live — stop it first" in m for m in notifications(app)))
        assert archived == []
        assert not isinstance(app.screen, ConfirmModal)


def run_worker_body(app: BmadLoopApp, worker: str, *args) -> list[tuple[str, tuple, dict]]:
    """Run a `@work` method's undecorated body on this thread and return every
    callback it marshalled to the UI, in order, as ``("app.notify", args,
    kwargs)``-shaped records.

    `call_from_thread` is a worker's only route to the UI, so the recorder sees
    exactly what the real worker posts, and nothing runs: no app is started. This
    is the `__wrapped__` entry `_poll`'s lock test uses; the Pilot rows below keep
    the keybinding, modal, dispatch and rendering in the loop."""
    posted: list[tuple[str, tuple, dict]] = []
    owners = {id(app): "app", id(app._dashboard): "dashboard"}

    def record(callback, *a, **kw):
        posted.append((f"{owners[id(callback.__self__)]}.{callback.__name__}", a, kw))

    app.call_from_thread = record
    getattr(BmadLoopApp, worker).__wrapped__(app, *args)
    return posted


def observe_helper(monkeypatch, name: str) -> list[str]:
    """Forward `runs.<name>`, recording how each call ended, so a Pilot row can
    tell a worker that never reached the helper from one whose outcome never
    reached the UI."""
    real = getattr(runs_mod, name)
    outcomes: list[str] = []

    def forward(*args, **kwargs):
        trace(f"runs.{name} entered")
        try:
            result = real(*args, **kwargs)
        except BaseException as e:
            outcomes.append(f"raised {e!r}")
            trace(f"runs.{name} raised {e!r}")
            raise
        outcomes.append(f"returned {result!r}")
        trace(f"runs.{name} returned {result!r}")
        return result

    monkeypatch.setattr(runs_mod, name, forward)
    return outcomes


async def confirm_lifecycle(pilot, key: str, helper: str, outcomes: list[str]) -> None:
    """Drive a D/A through its keybinding and confirm modal, then wait on each
    stage in turn — modal open, click landed, modal dismissed, the worker's
    helper call finished — so a failure names the stage that stalled."""
    app = pilot.app
    await pilot.press(key)
    await until(pilot, lambda: isinstance(app.screen, ConfirmModal), what=f"{key} opens its modal")
    await click(pilot, await ready(pilot, "#ok"))
    await until(
        pilot,
        lambda: not isinstance(app.screen, ConfirmModal),
        what="the click dismisses the modal",
    )
    await until(pilot, lambda: bool(outcomes), what=f"the worker's runs.{helper} call to finish")


_LIFECYCLE_WORKERS = {"D": "_delete_run_worker", "A": "_archive_run_worker"}


@pytest.mark.parametrize(
    "key, helper, failure, expected",
    [
        ("D", "delete_run", runs_mod.LiveEngineError("engine resumed"), "delete failed"),
        (
            "D",
            "delete_run",
            runs_mod.StateRootError("no usable state root"),
            "delete failed",
        ),
        ("A", "archive_run", runs_mod.LiveEngineError("engine resumed"), "archive failed"),
        (
            "A",
            "archive_run",
            runs_mod.StateRootError("no usable state root"),
            "archive failed",
        ),
    ],
)
def test_lifecycle_workers_report_authoritative_failures_and_keep_the_run_visible(
    project_tree, monkeypatch, key, helper, failure, expected
):
    """The modal's liveness sample is advisory. A later lifecycle or state-lock
    refusal is toasted from the worker as an error, and the dashboard forget —
    what drops the run from the table and the selection — is posted only on
    success. The Pilot twin below keeps one row per action in the running app.

    Ablation: omit either new exception type from the worker catch and its rows
    raise out of the worker body instead of posting the toast. Verified."""

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(runs_mod, helper, fail)
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", finished=True)
    app = BmadLoopApp(project_tree.project)

    posted = run_worker_body(app, _LIFECYCLE_WORKERS[key], run_dir.name, run_dir)

    assert posted == [("app.notify", (f"{expected}: {failure}",), {"severity": "error"})]
    assert run_dir.is_dir()


@pytest.mark.parametrize(
    "key, helper, expected",
    [("D", "delete_run", "delete failed"), ("A", "archive_run", "archive failed")],
    ids=["delete", "archive"],
)
async def test_lifecycle_refusal_is_an_error_toast_and_keeps_the_run_selected(
    project_tree, monkeypatch, key, helper, expected
):
    """The authoritative refusal through the running app: keybinding, modal, the
    worker's dispatch and its error toast, with the run still selected. The
    exception-type matrix lives in the worker-body rows above.

    Ablation: drop `LiveEngineError` from either worker's catch and its row
    fails — the worker error takes the app down under run_test. Verified."""
    monkeypatch.setattr(data, "liveness", lambda _run_dir: "dead")

    def fail(*_args, **_kwargs):
        raise runs_mod.LiveEngineError("engine resumed")

    monkeypatch.setattr(runs_mod, helper, fail)
    outcomes = observe_helper(monkeypatch, helper)
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", finished=True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == run_dir.name)
        await confirm_lifecycle(pilot, key, helper, outcomes)
        assert outcomes == ["raised LiveEngineError('engine resumed')"]
        await until(
            pilot,
            lambda: (f"{expected}: engine resumed", "error") in notifications_with_severity(app),
            what="the refusal's error toast",
        )
        assert dashboard(app).selected_run_id == run_dir.name

    assert run_dir.is_dir()


class _UnaskableMux:
    """A backend whose session listing raises — the out-of-tree seam shape the
    removal guard's DW-466 arm exists for. Tmux-shaped otherwise."""

    def session_name_key(self, name):
        return name

    def has_registry_namespace(self):
        return False

    def list_sessions(self):
        raise MultiplexerError("simulated transport failure")

    list_sessions_reporting = TerminalMultiplexer.list_sessions_reporting


def _unselectable_mux():
    raise MultiplexerError("[mux] backend = 'ghost' matches no registered backend")


def _removed_toast(key: str, run_dir: Path, project: Path) -> str:
    if key == "D":
        return f"run {run_dir.name} deleted"
    return f"run {run_dir.name} archived to {project / '.bmad-loop' / 'archive'}"


def _spy_prints(monkeypatch, *modules) -> list[str]:
    # Textual redirects stderr while an app runs, so capsys cannot see a print
    # made under it; spy on each module's own `print` instead.
    printed: list[str] = []
    for module in modules:
        monkeypatch.setattr(
            module, "print", lambda *a, **_kw: printed.append(" ".join(map(str, a))), raising=False
        )
    return printed


@pytest.mark.parametrize(
    "select, what",
    [
        (_unselectable_mux, "the multiplexer backend could not be selected"),
        (_UnaskableMux, "the session listing raised"),
    ],
    ids=["unselectable", "listing-raised"],
)
@pytest.mark.parametrize("key", ["D", "A"], ids=["delete", "archive"])
def test_lifecycle_workers_toast_an_unasked_session_guard(
    project_tree, monkeypatch, select, what, key
):
    """The #419 guard could not ask the multiplexer, so the removal went ahead
    as if no session were live (DW-466). The CLI says so on stderr, but Textual
    captures stderr for the app's whole run, so under the TUI that print reached
    nobody: the run dir vanished with nothing shown. The worker now hands
    `delete_run`/`archive_run` a sink and toasts the note as a warning — ahead of
    the forget and the removal toast — and the stderr print does not fire on top
    of it (one route per frontend).

    Drives the real removal helpers in the real worker bodies; only the
    multiplexer seam is faked. The running-app twin is
    test_lifecycle_guard_warning_renders_through_the_running_app.

    Ablation: drop `_notify_guard_notes` from either worker and its rows fail on
    the posted sequence (the removal toast arrives, the warning never does).
    Verified."""
    monkeypatch.setattr(runs_mod, "get_multiplexer", select)
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", finished=True)
    printed = _spy_prints(monkeypatch, runs_mod)
    note = f"run 20260611-100000-aaaa: could not check for a live agent session — {what}: "
    app = BmadLoopApp(project_tree.project)

    posted = run_worker_body(app, _LIFECYCLE_WORKERS[key], run_dir.name, run_dir)

    assert [(name, kw) for name, _args, kw in posted] == [
        ("app.notify", {"severity": "warning"}),
        ("dashboard.forget_run", {}),
        ("app.notify", {}),
    ]
    assert posted[0][1][0].startswith(note)
    assert posted[1][1] == (run_dir.name,)
    assert posted[2][1][0].startswith(_removed_toast(key, run_dir, project_tree.project))
    assert not run_dir.exists()
    assert not [p for p in printed if "could not check for a live agent session" in p]


@pytest.mark.parametrize(
    "stderr, faulted",
    [
        ("error connecting to /tmp/tmux-1001/default (Permission denied)\n", True),
        ("no server running on /tmp/tmux-1001/default\n", False),
    ],
    ids=["unproven", "no-server"],
)
@pytest.mark.parametrize("key", ["D", "A"], ids=["delete", "archive"])
def test_lifecycle_workers_toast_a_folded_session_listing(
    project_tree, monkeypatch, stderr, faulted, key
):
    """The stock backends never raise from the listing: a failed `list-sessions`
    folds into `[]` with a stderr warning (DW-458), and Textual captures stderr,
    so a D/A past it removed the run dir with nothing shown. The guard now
    reads the listing with the worker's sink, so the fault is toasted like the
    DW-466 raise — and neither the backend nor the guard prints on top of it.
    A gone server is an answer, not a fault: no warning toast.

    Drives the real removal helpers, the real tmux-family fold and the real
    worker bodies; only the spawn is faked (see test_runs._folding_tmux). The
    sink-to-toast wiring in the running app is pinned by
    test_lifecycle_guard_warning_renders_through_the_running_app.

    Ablation: pass `on_fault=None` unconditionally from the guard and the
    `unproven` rows fail on the posted sequence. Verified."""
    from test_runs import _folding_tmux

    from bmad_loop.adapters import tmux_base

    monkeypatch.setattr(runs_mod, "get_multiplexer", lambda: _folding_tmux(stderr))
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", finished=True)
    printed = _spy_prints(monkeypatch, runs_mod, tmux_base)
    note = (
        "run 20260611-100000-aaaa: could not check for a live agent session — the "
        f"session listing failed: {sys.executable} list-sessions exited 1 without "
        "proving the session gone: error connecting to"
    )
    app = BmadLoopApp(project_tree.project)

    posted = run_worker_body(app, _LIFECYCLE_WORKERS[key], run_dir.name, run_dir)

    warnings = [a[0] for name, a, kw in posted if kw == {"severity": "warning"}]
    assert warnings == ([warnings[0]] if faulted else [])
    if faulted:
        assert warnings[0].startswith(note)
    assert [(name, kw) for name, _args, kw in posted if kw != {"severity": "warning"}] == [
        ("dashboard.forget_run", {}),
        ("app.notify", {}),
    ]
    assert posted[-1][1][0].startswith(_removed_toast(key, run_dir, project_tree.project))
    assert not run_dir.exists()
    assert printed == []


@pytest.mark.parametrize("key", ["D", "A"], ids=["delete", "archive"])
async def test_lifecycle_guard_warning_renders_through_the_running_app(
    project_tree, monkeypatch, key
):
    """The DW-466 degrade end to end in the running app, one row per action:
    keybinding, modal, the worker's real removal helper, its warning toast and
    removal toast, the forget and the removed run dir — each awaited as its own
    stage (`confirm_lifecycle`), so a timeout names the stage that stalled rather
    than "condition not met". The warning is read twice: as emitted, and as the
    toast widget the operator actually sees, severity class included — which
    needs `run_test(notifications=True)`: Textual's test default mounts no toasts.

    The guard/listing matrices run against the worker bodies above.

    Ablation: drop `_notify_guard_notes` from either worker and its row fails at
    the warning wait; emit the note at `information` and the rendered-severity
    wait fails. Verified."""
    helper = {"D": "delete_run", "A": "archive_run"}[key]
    monkeypatch.setattr(data, "liveness", lambda _run_dir: "dead")
    monkeypatch.setattr(runs_mod, "get_multiplexer", _UnaskableMux)
    outcomes = observe_helper(monkeypatch, helper)
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", finished=True)
    printed = _spy_prints(monkeypatch, runs_mod)
    note = (
        "run 20260611-100000-aaaa: could not check for a live agent session — "
        "the session listing raised: "
    )
    removed = _removed_toast(key, run_dir, project_tree.project)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(notifications=True) as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == run_dir.name)
        await confirm_lifecycle(pilot, key, helper, outcomes)
        assert len(outcomes) == 1 and outcomes[0].startswith("returned"), outcomes
        await until(
            pilot,
            lambda: any(
                m.startswith(note) and sev == "warning"
                for m, sev in notifications_with_severity(app)
            ),
            what="the guard's warning emitted",
        )
        await until(
            pilot,
            lambda: any(m.startswith(removed) for m in notifications(app)),
            what="removal toast",
        )
        await until(
            pilot,
            lambda: any(t.startswith(note) and sev == "warning" for t, sev in rendered_toasts(app)),
            what="the guard's warning rendered as a warning toast",
        )
        await until(
            pilot, lambda: dashboard(app).selected_run_id != run_dir.name, what="the run forgotten"
        )

    assert not run_dir.exists()
    assert not [p for p in printed if "could not check for a live agent session" in p]


# ------------------------------------------------------------ graceful stop (S)


async def test_graceful_stop_requests_via_helper(project_tree, monkeypatch):
    # S writes the graceful-stop control file via the runs helper and toasts. No
    # multiplexer is touched (no _mux_missing gate, mux_available left unset), no
    # shell-out — the worker only calls runs.request_graceful_stop.
    from bmad_loop import runs

    calls: list[Path] = []
    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    monkeypatch.setattr(runs, "request_graceful_stop", lambda rd: calls.append(rd) or "requested")
    make_run(project_tree.project, "20260611-100000-aaaa", alive=True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: len(calls) == 1)
        assert calls[0].name == "20260611-100000-aaaa"
        await until(pilot, lambda: any("graceful stop requested" in m for m in notifications(app)))


@pytest.mark.parametrize(
    "token, needle",
    [
        ("already-pending", "already has a stop request pending"),
        ("requested-unverifiable", "could not confirm a live engine"),
    ],
)
async def test_graceful_stop_token_messages(project_tree, monkeypatch, token, needle):
    # The worker translates each status token from the helper into its own toast.
    from bmad_loop import runs

    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    monkeypatch.setattr(runs, "request_graceful_stop", lambda rd: token)
    make_run(project_tree.project, "20260611-100000-aaaa", alive=True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any(needle in m for m in notifications(app)))


async def test_graceful_stop_write_failure_notifies_instead_of_crashing(project_tree, monkeypatch):
    """The worker catches `OSError` the way the CLI's `stop --graceful` does. The
    confined lodge (#593) raises `UnconfinedWriteError` — an `OSError` — on a
    planted parent, and Textual workers default to `exit_on_error=True`, so
    without the catch pressing `S` in that scenario tore the whole dashboard
    down instead of reporting the refusal.

    Ablation: drop the worker's `except OSError` arm and this reddens — the
    worker error kills the app under run_test and the toast never arrives."""
    from bmad_loop import runs

    def boom(rd):
        raise runs.UnconfinedWriteError("cannot reach .bmad-loop/runs without a redirect")

    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    monkeypatch.setattr(runs, "request_graceful_stop", boom)
    make_run(project_tree.project, "20260611-100000-aaaa", alive=True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any("could not be written" in m for m in notifications(app)))
        assert app.is_running  # the dashboard survived the refusal


async def test_graceful_stop_not_live_warns_without_calling(project, monkeypatch):
    # Only a *provably dead* engine is refused at the liveness gate — the helper is
    # never called and no confirm modal opens. Unlike the hard-stop gate, an
    # unverifiable ('unknown') pid is allowed through (see the next test).
    from bmad_loop import runs

    calls: list[Path] = []
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(runs, "request_graceful_stop", lambda rd: calls.append(rd) or "requested")
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("S")
        await until(pilot, lambda: any("is not live" in m for m in notifications(app)))
        assert calls == []
        assert not isinstance(app.screen, ConfirmModal)


async def test_graceful_stop_unknown_liveness_proceeds(project, monkeypatch):
    # An unverifiable ('unknown') pid — a win32 access-denied pid, a psmux backend,
    # a run on another host — is NOT dead, so the graceful gate lets it through to
    # the confirm modal and the helper (which returns 'requested-unverifiable': the
    # request stands and fires if an engine is in fact running). This mirrors the
    # CLI, whose stop --graceful gate is likewise != "dead".
    from bmad_loop import runs

    calls: list[Path] = []
    monkeypatch.setattr(data, "liveness", lambda run_dir: "unknown")
    monkeypatch.setattr(
        runs, "request_graceful_stop", lambda rd: calls.append(rd) or "requested-unverifiable"
    )
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: len(calls) == 1)
        assert calls[0].name == "20260611-100000-aaaa"
        needle = "could not confirm a live engine"
        await until(pilot, lambda: any(needle in m for m in notifications(app)))


async def test_graceful_stop_error_toasts(project_tree, monkeypatch):
    # A GracefulStopError out of the helper (finished/dead run) surfaces as an
    # error toast rather than escaping the worker (exit_on_error would kill the app).
    from bmad_loop import runs

    def boom(rd):
        raise runs.GracefulStopError("run has already finished — nothing to stop")

    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    monkeypatch.setattr(runs, "request_graceful_stop", boom)
    make_run(project_tree.project, "20260611-100000-aaaa", alive=True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await click(pilot, await ready(pilot, "#ok"))
        await until(pilot, lambda: any("nothing to stop" in m for m in notifications(app)))


async def test_graceful_stop_pending_shows_in_header_and_note(project_tree, monkeypatch):
    # End to end: a RUNNING run with the control file present paints the header
    # pending line and the runs-table stop tag (data -> snapshot -> apply).
    from bmad_loop.runs import STOP_REQUEST_FILE

    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", alive=True)
    (run_dir / STOP_REQUEST_FILE).write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        header = screen.query_one("#runheader", RunHeader)
        await until(pilot, lambda: "graceful stop pending" in str(header.content))
        runs_table = screen.query_one("#runs", DataTable)
        await until(
            pilot,
            lambda: "stop" in runs_table.get_cell("20260611-100000-aaaa", "note").plain,
        )


async def test_header_counts_parked_stories_only_when_there_are_any(project_tree):
    """The header's done/deferred/escalated trio is the TUI's mirror of
    RunSummary's counts; a parked story belongs to none of them, so without its
    own cell it would show up in `tasks N` and nowhere else. Conditional for the
    same reason the summary clause is: the line is already at the width the
    narrowest supported pane holds.

    Drives `show_run` directly — the count line is a pure function of the state it
    is handed, so routing a second run through the dashboard's polling only adds
    scheduling to what this is actually asserting."""
    done = StoryTask(story_key="1-2-beta", epic=1, phase=Phase.DONE)
    parked = StoryTask(story_key="1-1-alpha", epic=1, phase=Phase.AWAITING_OPERATOR)

    def _state(tasks):
        return RunState(
            run_id="r1", project=str(project_tree.project), started_at="now", tasks=tasks
        )

    app = BmadLoopApp(project_tree.project)
    async with app.run_test():
        header = dashboard(app).query_one("#runheader", RunHeader)

        header.show_run("r1", "finished", _state({"1-2-beta": done}))
        assert "awaiting" not in str(header.content)

        header.show_run("r1", "finished", _state({"1-2-beta": done, "1-1-alpha": parked}))
        content = str(header.content)
        assert "awaiting 1" in content
        assert "tasks 2" in content
        assert "done 1" in content  # the park did not absorb the done story


async def test_header_shows_a_refused_auto_sweep_apart_from_one_that_ran(project_tree):
    """DW-366: a refused auto-sweep gets its own warning line (reason slug plus the
    `bmad-loop sweep` hint), and a delivered one gets only a dim `ran` line, so
    the two never look the same. A run with neither shows no sweep line at all.

    Ablation: drop the `sweeps.refused` branch in `show_run` — the first block's
    `not run` assert fails while the triggered-only block still passes."""

    def _state(**kw):
        return RunState(run_id="r1", project=str(project_tree.project), started_at="now", **kw)

    def style_at(content, needle: str) -> str:
        start = str(content).index(needle)
        styles = [str(s.style) for s in content.spans if s.start <= start < s.end]
        assert len(styles) == 1, styles
        return styles[0]

    app = BmadLoopApp(project_tree.project)
    async with app.run_test():
        header = dashboard(app).query_one("#runheader", RunHeader)

        # run-end carries the engine's `failed` shape: latched (child started),
        # then refused (child failed) — it must read "not run", never "ran".
        header.show_run(
            "r1",
            data.FINISHED,
            _state(sweeps_triggered=["epic-1", "run-end"], sweeps_refused={"run-end": "failed"}),
        )
        content = str(header.content)
        assert "⚠ auto-sweep not run: run-end (failed) — deferred work is untouched" in content
        assert "run `bmad-loop sweep` with a clean worktree" in content
        assert "auto-sweep ran: epic-1\n" in content + "\n"
        assert "run-end" not in content.split("auto-sweep ran:")[1]  # refused is not "ran"
        # "differs" is the style too, not just the words: warning vs dim
        assert style_at(header.content, "⚠ auto-sweep not run") == "bold yellow"
        assert style_at(header.content, "auto-sweep ran:") == "dim"

        header.show_run("r1", data.FINISHED, _state(sweeps_triggered=["epic-1", "run-end"]))
        content = str(header.content)
        assert "auto-sweep ran: epic-1, run-end" in content
        assert "not run" not in content
        assert "bmad-loop sweep" not in content

        header.show_run("r1", data.FINISHED, _state())
        assert "auto-sweep" not in str(header.content)


async def test_refused_auto_sweep_reaches_the_dashboard_header(project_tree):
    # End to end: a refusal in state.json flows RunWatcher -> snapshot -> header.
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", finished=True)
    state = RunState(
        run_id=run_dir.name,
        project=str(project_tree.project),
        started_at="2026-06-11T10:00:00",
        finished=True,
        sweeps_refused={"run-end": "dirty"},
    )
    save_state(run_dir, state)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        header = screen.query_one("#runheader", RunHeader)
        await until(pilot, lambda: "auto-sweep not run: run-end (dirty)" in str(header.content))


async def test_tui_pause_surfaces_bound_critical_reason_and_name_the_spec(project_tree):
    from bmad_loop.escalation import CRITICAL_DISPLAY_MAX, display_pause_reason

    spec = project_tree.implementation_artifacts / "spec-1-1-alpha.md"
    task = StoryTask(
        story_key="1-1-alpha",
        epic=1,
        phase=Phase.ESCALATED,
        spec_file=str(spec),
    )
    tail = "RECOVERY-TAIL"
    reason = "CRITICAL escalation from dev session: " + "x" * 2500 + tail
    state = RunState(
        run_id="r1",
        project=str(project_tree.project),
        started_at="now",
        tasks={task.story_key: task},
        paused_stage="escalation",
        paused_reason=reason,
        paused_story_key=task.story_key,
    )

    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        header = dashboard(app).query_one("#runheader", RunHeader)
        header.show_run("r1", data.PAUSED, state)
        rendered_header = str(header.content)
        assert "[… truncated; full detail in journal.jsonl]" in rendered_header
        assert f"[recovery trail: {spec}]" in rendered_header
        assert tail not in rendered_header

        app.push_screen(ConfirmResumeModal("r1", state, False))
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        await ready(pilot, "#body Static")
        modal_body = app.screen._body.plain
        assert "[… truncated; full detail in journal.jsonl]" in modal_body
        assert f"[recovery trail: {spec}]" in modal_body
        assert tail not in modal_body
        assert len(display_pause_reason(state)) <= CRITICAL_DISPLAY_MAX


async def test_active_agent_shows_in_header_and_task_cell(project_tree, monkeypatch):
    # End to end: a RUNNING run with an open, adapter-stamped session-start paints
    # the header's live agent line and the task row's agent cell
    # (data.active_agent -> snapshot -> apply). Story key matches STORY_RE.
    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    task = StoryTask(story_key="1-1-alpha", epic=1, phase=Phase.DEV_RUNNING)
    run_dir = make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        alive=True,
        tasks={"1-1-alpha": task},
        policy_snapshot={"adapter": {"name": "claude", "model": "opus"}},
    )
    Journal(run_dir).append(
        "session-start",
        task_id="1-1-alpha-dev-1",
        role="dev",
        adapter="claude",
        model="opus",
        story_key="1-1-alpha",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        header = screen.query_one("#runheader", RunHeader)
        await until(pilot, lambda: "claude · opus · dev" in str(header.content))
        tasks_table = screen.query_one("#tasks", DataTable)
        await until(
            pilot,
            lambda: (
                tasks_table.row_count == 1
                and tasks_table.get_cell("1-1-alpha", "agent") == "claude·opus"
            ),
        )


async def test_header_agent_line_shows_open_idle_stretch(project_tree, monkeypatch):
    """#680: with a `session-idle` open for the live session the agent line ends
    `· idle <age>`; after the matching `session-active` the text is gone. Drives
    `show_run` with an `ActiveAgent` directly (the derivation is
    `test_tui_data`'s) and pins the age formatter's two shapes.

    ABLATION E: drop the `idle_since` branch in `show_run` and the first
    assertion reddens; the negative rows hold on their own only because the
    positive one passes."""
    monkeypatch.setattr(widgets.time, "time", lambda: 10_000.0)
    state = RunState(
        run_id="r1",
        project=str(project_tree.project),
        started_at="now",
        tasks={"1-1-alpha": StoryTask(story_key="1-1-alpha", epic=1, phase=Phase.DEV_RUNNING)},
    )
    working = data.ActiveAgent(
        task_id="1-1-alpha-dev-1", story_key="1-1-alpha", role="dev", name="claude", model="opus"
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test():
        header = dashboard(app).query_one("#runheader", RunHeader)
        header.show_run("r1", data.RUNNING, state, agent=working)
        assert "claude · opus · dev" in str(header.content)
        assert "idle" not in str(header.content)

        idle = dataclasses.replace(working, idle_since=10_000.0 - 12 * 60 - 5)
        header.show_run("r1", data.RUNNING, state, agent=idle)
        assert "claude · opus · dev · idle 12m" in str(header.content)

        long_idle = dataclasses.replace(working, idle_since=10_000.0 - 65 * 60)
        header.show_run("r1", data.RUNNING, state, agent=long_idle)
        assert "· idle 1h05m" in str(header.content)

        # a stamp from the future (a clock stepped back between the adapter's
        # stamp and this render) clamps to 0m rather than rendering a minus sign
        future = dataclasses.replace(working, idle_since=10_000.0 + 5)
        header.show_run("r1", data.RUNNING, state, agent=future)
        assert "· idle 0m" in str(header.content)

        header.show_run("r1", data.RUNNING, state, agent=working)  # session-active
        assert "idle" not in str(header.content)
        header.show_run("r1", data.RUNNING, state, agent=None)  # session-end
        assert "idle" not in str(header.content)


async def test_header_marks_stale_state_unreadable_agent_and_read_faults(project_tree):
    """DW-472/474/475 at the header: a stale last-good state gets its own warning
    line, a state never parsed names why, an unreadable agent says so instead of
    falling back to the configured-agents line, and each read fault is listed.

    Ablation: drop any one branch in `show_run` and its assertion reddens."""
    state = RunState(
        run_id="r1",
        project=str(project_tree.project),
        started_at="now",
        policy_snapshot={"adapter": {"name": "claude", "model": "opus"}},
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test():
        header = dashboard(app).query_one("#runheader", RunHeader)

        header.show_run("r1", data.RUNNING, state)
        content = str(header.content)
        assert "stale" not in content and "unreadable" not in content and "⚠" not in content

        header.show_run("r1", data.RUNNING, state, state_fault="state.json unreadable (X: y)")
        assert "⚠ state stale — state.json unreadable (X: y); showing the last good read" in str(
            header.content
        )

        header.show_run("r1", data.UNKNOWN, None, state_fault="state.json unreadable (X: y)")
        assert "state unavailable — state.json unreadable (X: y)" in str(header.content)

        header.show_run("r1", data.RUNNING, state, agent=data.UnreadableAgent("bad entry"))
        content = str(header.content)
        assert "agent unreadable — bad entry" in content
        assert "agents claude" not in content  # not the no-session fallback

        header.show_run(
            "r1",
            data.RUNNING,
            state,
            read_faults=("journal.jsonl cannot be stat'd (E)", "ATTENTION unreadable (F)"),
        )
        content = str(header.content)
        assert "⚠ journal.jsonl cannot be stat'd (E)" in content
        assert "⚠ ATTENTION unreadable (F)" in content


async def test_stale_state_reaches_the_dashboard_header(project_tree):
    # End to end (DW-472): a state.json that stops parsing flows RunWatcher ->
    # snapshot -> header as a stale marker, while the last good read stays shown.
    run_dir = make_run(project_tree.project, "20260611-100000-aaaa", finished=True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        header = screen.query_one("#runheader", RunHeader)
        await until(pilot, lambda: "started 2026-06-11T10:00:00" in str(header.content))
        (run_dir / "state.json").write_text("{ broken", encoding="utf-8")
        await until(pilot, lambda: "⚠ state stale" in str(header.content))
        assert "started 2026-06-11T10:00:00" in str(header.content)


async def test_idle_run_shows_configured_agents_and_cell_falls_back(project_tree, monkeypatch):
    # No session open (session-start then a matching session-end): the header shows
    # the configured adapters from the snapshot (dev/review differ, so the full
    # "agents dev … review …" form renders), and the agent cell falls back to the
    # last adapter-stamped SessionRecord rather than the (absent) live agent.
    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    task = StoryTask(
        story_key="1-1-alpha",
        epic=1,
        phase=Phase.DONE,
        sessions=[
            SessionRecord(
                task_id="1-1-alpha-dev-1",
                role="dev",
                status="completed",
                adapter="claude",
                model="haiku",
            )
        ],
    )
    run_dir = make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        alive=True,
        tasks={"1-1-alpha": task},
        policy_snapshot={
            "adapter": {"name": "claude", "model": "opus", "review": {"name": "codex"}}
        },
    )
    journal = Journal(run_dir)
    journal.append(
        "session-start",
        task_id="1-1-alpha-dev-1",
        role="dev",
        adapter="claude",
        model="haiku",
        story_key="1-1-alpha",
    )
    journal.append("session-end", task_id="1-1-alpha-dev-1")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        header = screen.query_one("#runheader", RunHeader)
        # configured line, not a live "agent …" line (no session is open)
        await until(pilot, lambda: "agents dev claude·opus review codex" in str(header.content))
        assert "\nagent " not in str(header.content)
        tasks_table = screen.query_one("#tasks", DataTable)
        # cell reads the stamped record's model (haiku), distinct from the config
        await until(
            pilot,
            lambda: (
                tasks_table.row_count == 1
                and tasks_table.get_cell("1-1-alpha", "agent") == "claude·haiku"
            ),
        )


async def test_story_checkpoint_card_surfaces_real_review_cycles(project, monkeypatch):
    # audit item 13: the card's gate line must reflect the task's real
    # review_cycle, never the old blanket "verification passed" string.
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _stories_paused_run(
        project.project,
        stage="story-checkpoint",
        spec_status="done",
        spec_checkpoint=False,
        done_checkpoint=True,
        commit_sha="abc1234def5678",
        review_cycle=2,
    )
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, StoryCheckpointModal)
        line = app.screen._verify_line
        assert "verify + review gates passed" in line
        assert "2 follow-up review cycles" in line
        assert "verification passed" not in line


def test_tui_rearm_refuses_an_alive_run_before_any_mutation(project_tree, monkeypatch):
    """The liveness helper's result must control the re-arm, not merely be observed."""
    from bmad_loop import runs

    notes: list[str] = []
    rearms: list[str] = []
    run_id = "20260611-100000-aaaa"
    run_dir = project_tree.project / RUNS_DIR / run_id
    app = BmadLoopApp(project_tree.project)

    def fail_if_rearm_continues(_path):
        raise AssertionError("continued past liveness gate")

    monkeypatch.setattr(data, "liveness", lambda _run_dir: "alive")
    monkeypatch.setattr(app, "notify", lambda message, **_kwargs: notes.append(message))
    monkeypatch.setattr(policy_mod, "load", fail_if_rearm_continues)
    monkeypatch.setattr(
        runs,
        "rearm_escalation",
        lambda _run_dir, story_key, **_kwargs: rearms.append(story_key),
    )

    app._do_rearm(run_id, run_dir, "1")

    assert rearms == []
    assert notes == [f"run {run_id} may still be live — stop it first"]


async def test_escalation_rearm_resumes_when_resolution_ready(project_tree, monkeypatch):
    from bmad_loop import resolve, runs

    calls: list[str] = []
    rearms: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        runs,
        "rearm_escalation",
        lambda rd, sk, **_k: rearms.append(sk) or _rearm_outcome(sk),
    )
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        # story context + blocking condition were resolved from stories.yaml + the spec
        assert app.screen._description == "does a thing"
        assert "Auto Run Result" in app.screen._blocking
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: rearms == ["1"] and calls == ["20260611-100000-aaaa"])


async def test_tui_rearm_does_not_move_the_escalation_watermark(project, monkeypatch):
    """DW-11, on the one re-arm surface a stale `resolution.json` actively invites.

    Every other TUI row here monkeypatches `runs.rearm_escalation` away, so none can
    observe what it stamps — this one lets the REAL function run. The marker on disk is
    the shape that matters: `resolve.run_session` is the only thing in `src/` that
    unlinks it and this gesture never calls it, so the marker survived the CLI cycle
    that consumed it, and `resolution_ready` (the sole enabler of this button) still
    reads True. `_do_rearm` therefore has to declare `resolution_recorded=False` from
    what it KNOWS — it ran no session — rather than from what is on disk, which is
    exactly the verdict `_restore_recorded` already records for this surface.

    The watermark is seeded to 1 over a two-record trail so "did not move" is
    distinguishable from "was never set"; `generation` is the positive control that the
    re-arm really ran.

    Ablation: pass `resolution_recorded=True` from `_do_rearm` (or gate the stamp on
    `resolution_path(...).is_file()` inside `rearm_escalation`) and this reddens at
    2 != 1."""
    from bmad_loop import resolve
    from bmad_loop.engine import _session_task_id
    from bmad_loop.journal import load_state

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: None)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    run_dir, _spec = _stories_paused_run(
        project.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    state = load_state(run_dir)
    task = state.tasks["1"]
    task.phase = Phase.ESCALATED
    task.sessions.clear()
    for seq in (1, 2):
        task.record_session(
            SessionRecord(
                task_id=_session_task_id("1", "review", seq, 0), role="dev", status="completed"
            )
        )
    task.escalations_resolved_upto = 1  # an earlier CLI cycle answered the first record
    save_state(run_dir, state)
    # the marker that cycle's agent wrote — nothing deleted it at its re-arm
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app._do_rearm("20260611-100000-aaaa", run_dir, "1")
        await pilot.pause()

    rearmed = load_state(run_dir).tasks["1"]
    assert rearmed.escalations_resolved_upto == 1  # NOT len(sessions) == 2
    assert rearmed.generation == 1  # positive control: the re-arm ran


async def test_escalation_rearm_hands_the_rearm_the_live_isolation_mode(project_tree, monkeypatch):
    """The mode `runs.rearm_escalation` needs comes from policy.toml, read HERE.

    It decides three things the operator acts on — which ref a correction has to reach,
    whether the working-tree flip reaches the re-drive at all, whether a restore latch
    can be honored — and run state cannot answer any of them: `scm.isolation` is re-read
    at every resume, and a mid-run change is journalled rather than refused, so the
    recorded `task.worktree_path` describes only the attempt that already ran. This
    gesture re-arms BEFORE it resumes, so nothing downstream can supply the value later.

    The LIVE PROJECT rides the same argument list and for the same reason: nothing
    re-stamps `state.project`, so a re-arm left to the recorded value writes the spec
    into the tree this dashboard is no longer looking at. `self.project` rather than
    `paths.project`, because the `load_paths` arm above may degrade without binding
    `paths` at all.

    Ablation: pass a literal `isolated_redrive=False` at the call site and this reddens
    — the modes stop tracking policy.toml and every isolated run gets the in-place
    answers. Drop `project_root=self.project` and it reddens on the second list.
    """
    from bmad_loop import resolve, runs

    bmad = project_tree.project / ".bmad-loop"
    bmad.mkdir(parents=True, exist_ok=True)
    (bmad / "policy.toml").write_text('[scm]\nisolation = "worktree"\n', encoding="utf-8")
    seen: list[bool] = []
    roots: list[object] = []

    def fake_rearm(rd, sk, *, isolated_redrive, resolution_recorded, project_root=None):
        seen.append(isolated_redrive)
        roots.append(project_root)
        return _rearm_outcome(sk)

    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: None)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(runs, "rearm_escalation", fake_rearm)
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: seen == [True])
    assert roots == [project_tree.project]  # the live tree, not the recorded one

    # ...and the other mode is not a constant: the same gesture on `none` says so
    (bmad / "policy.toml").write_text('[scm]\nisolation = "none"\n', encoding="utf-8")
    seen.clear()
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: seen == [False])


async def test_escalation_rearm_refuses_when_the_policy_cannot_be_read(project_tree, monkeypatch):
    """An unreadable policy.toml REFUSES this gesture — a deliberate departure from how
    this surface treats every other read of that file.

    The launch guard and this block's own conflict check both fall through on an
    unreadable policy, and correctly: they cannot tell "no conflict" from "could not
    look", and the detached CLI re-reads the same file and fails loudly on it. That
    reasoning does not extend to an INPUT of a repair write. Without the mode the re-arm
    would still flip the spec and then name a tree chosen by a default — and a re-arm
    CONSUMES the escalation, so the story is no longer ESCALATED for `resolve` to
    correct. Refusing costs the operator one fix-and-retry; proceeding costs them the
    escalation.

    Graded on the re-arm not running at all, not merely on the notice: the message is
    the trace, the un-consumed escalation is the property.

    Ablation: restore the fall-through (default the mode instead of returning) and this
    reddens on `rearms` — the gesture re-arms against a guessed isolation mode.
    """
    from bmad_loop import resolve, runs

    bmad = project_tree.project / ".bmad-loop"
    bmad.mkdir(parents=True, exist_ok=True)
    # bytes no UTF-8 decoder accepts
    (bmad / "policy.toml").write_bytes(b'[scm]\nisolation = "\xff\xfe"\n')
    rearms: list[str] = []
    notes: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: None)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        runs, "rearm_escalation", lambda rd, sk, **_k: rearms.append(sk) or _rearm_outcome(sk)
    )
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: notes.append(str(msg)) or orig_notify(self, msg, **kw),
    )
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: any("isolation mode" in n for n in notes))

    assert rearms == []  # the escalation is NOT consumed
    assert not any("re-armed" in n for n in notes)


def test_restore_recorded_helper(tmp_path):
    """review F8: absent marker / no restore field -> False; a recorded
    restore_patch -> True; an UNREADABLE marker -> True (it may carry one, so
    the warning must err toward surfacing)."""
    from bmad_loop import resolve

    assert BmadLoopApp._restore_recorded(tmp_path, "1") is False  # absent
    marker = resolve.resolution_path(tmp_path, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    assert BmadLoopApp._restore_recorded(tmp_path, "1") is False  # no restore field
    marker.write_text('{"restore_patch": "artifacts/a.patch"}', encoding="utf-8")
    assert BmadLoopApp._restore_recorded(tmp_path, "1") is True
    marker.write_text('{"restore_patch": "artifacts/a.patch",}', encoding="utf-8")
    assert BmadLoopApp._restore_recorded(tmp_path, "1") is True  # corrupt -> conservative


async def test_escalation_rearm_warns_when_restore_recorded(project_tree, monkeypatch):
    """review F8: a resolution.json carrying restore_patch still enables Re-arm
    (it IS a recorded resolution) but the modal flags it and the re-arm notifies
    that the restore is NOT honored here — only `bmad-loop resolve` applies a
    latch — so the human's confirmed decision is never dropped silently."""
    from bmad_loop import resolve, runs

    calls: list[str] = []
    rearms: list[str] = []
    notes: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        runs,
        "rearm_escalation",
        lambda rd, sk, **_k: rearms.append(sk) or _rearm_outcome(sk),
    )
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: notes.append(str(msg)) or orig_notify(self, msg, **kw),
    )
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: intent gap; saved patch: artifacts/attempt.patch",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"restore_patch": "artifacts/attempt.patch"}', encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        assert app.screen._restore_recorded is True  # the modal shows the warning hint
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: rearms == ["1"] and calls == ["20260611-100000-aaaa"])
    assert any("NOT honored" in n for n in notes)  # the drop was surfaced, not silent


async def test_escalation_rearm_surfaces_a_failed_baseline_advance(project_tree, monkeypatch):
    """The TUI re-arm RESUMES in the same gesture, so a degrade it does not surface
    is a degrade the operator acts on without seeing.

    `cli._echo_rearm_events` prints these to stderr on the other re-arm path; both
    records are warn-only by contract (a project that is not a git repo must not
    fail re-arm), so a journal line in a scrolling panel was the only trace here. A
    failed advance means the re-drive rebuilds against the tree as it stood BEFORE
    the resolve — the invisibility #640(b) exists to end, not to relocate to the
    other caller.

    Ablation: delete the journal read-back loop in `_do_rearm` and this reddens,
    while the plain `re-armed 1` notice still fires.
    """
    from bmad_loop import resolve, runs
    from bmad_loop.journal import Journal

    calls: list[str] = []
    notes: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")

    def fake_rearm(rd, sk, *, isolated_redrive=False, resolution_recorded=False, project_root=None):
        Journal(rd).append(
            "rearm-baseline-advance-failed",
            story_key=sk,
            repo=str(rd),
            baseline="a" * 40,
            error="GitError: not a git repository",
        )
        return _journal_rearm_outcome(rd, sk)

    monkeypatch.setattr(runs, "rearm_escalation", fake_rearm)
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: notes.append(str(msg)) or orig_notify(self, msg, **kw),
    )
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])
    assert any("could not advance the re-drive baseline" in n for n in notes)
    assert any("re-armed 1" in n for n in notes)  # the ordinary notice still fires


async def test_escalation_rearm_aims_the_code_root_before_it_rearms(project_tree, monkeypatch):
    """Parity with `cli.cmd_resolve`, on the seam that has the same ordering.

    This gesture re-arms and RESUMES in one click, and `runs.rearm_escalation` reads the
    code tree out of the run state — so only a process that has just read config.yaml can
    tell whether a `repo_root:` edit made while the run was paused moved it. Resume
    re-stamps the mirror, but that is downstream of the re-arm here too: without this the
    re-arm would advance the attempt baseline in the tree the run has left while the
    resumed engine reset and measured in the new one.

    Ablation: delete the `runs.restamp_code_root(...)` call from `_do_rearm` and this
    reddens on the stale root; drop the `self.notify(moved, ...)` and it reddens on the
    missing warning while the root assertion still passes.
    """
    from bmad_loop import resolve, runs
    from bmad_loop.journal import load_state, save_state

    calls: list[str] = []
    notes: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    seen: list = []

    def fake_rearm(rd, sk, *, isolated_redrive=False, resolution_recorded=False, project_root=None):
        seen.append(load_state(rd).code_root)
        return _rearm_outcome(sk)

    monkeypatch.setattr(runs, "rearm_escalation", fake_rearm)
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: notes.append(str(msg)) or orig_notify(self, msg, **kw),
    )
    install_bmad_config(project_tree)
    moved = project_tree.project / "moved-code"
    moved.mkdir()
    cfg = project_tree.project / "_bmad" / "bmm" / "config.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8") + f"repo_root: '{moved.as_posix()}'\n", encoding="utf-8"
    )
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    state = load_state(run_dir)
    state.repo_root = str(project_tree.project / "old-code")
    save_state(run_dir, state)
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])

    assert seen == [moved.resolve()]
    assert any("the code root in the BMAD config has changed" in n for n in notes)


def test_escalation_rearm_rechecks_liveness_inside_state_lock(project_tree, monkeypatch):
    """Ablation: delete _do_rearm's second liveness check and the TUI re-arms after
    a rival resume published its pid while this gesture waited for the state lock."""
    from bmad_loop import runs

    install_bmad_config(project_tree)
    run_dir = project_tree.project / ".bmad-loop" / "runs" / "20260611-100000-aaaa"
    checks: list[str] = []

    def liveness_gate(_self, _run_id, _run_dir):
        checks.append("checked")
        return len(checks) == 2

    monkeypatch.setattr(BmadLoopApp, "_resolve_blocked_by_liveness", liveness_gate)
    monkeypatch.setattr(runs, "rearm_escalation", lambda *_a, **_k: pytest.fail("double re-armed"))

    BmadLoopApp(project_tree.project)._do_rearm(run_dir.name, run_dir, "1")

    assert checks == ["checked", "checked"]


def test_escalation_rearm_declines_a_run_whose_state_lock_is_held(project_tree, monkeypatch):
    """A contended run is REFUSED with a toast, not queued behind its holder.

    `_do_rearm` runs ON Textual's message loop: it carries no `@work`, and its only
    caller is the synchronous `push_screen` dismiss callback, so the acquisition
    happens inline and the whole dashboard freezes for however long the holder keeps
    the lock — unbounded on POSIX, where `fcntl.flock` never times out. The realistic
    holder is a rival `resume`, which takes the lock FIRST and publishes its pid LAST,
    so the modal's `_engine_possibly_live` gate reads dead for that entire window (the
    same window `cmd_clean` documents) and does not head the freeze off.

    The contention is real — the run's own canonical sidecar — and taken through
    `platform_util.file_lock` rather than `journal.state_lock` for the reason
    test_cleanup states: `state_lock`'s reentrancy guard is thread-local, so acquiring
    it here would let `_do_rearm` RE-ENTER the lock and pass for the wrong reason. The
    holder sits on a background thread with a BOUNDED hold so that a regression to a
    blocking acquire reddens on `elapsed` instead of hanging the suite.

    Ablations, both of which must redden this: (1) MOVE the `except
    LockUnavailableError` arm below the existing `except (RearmError, OSError,
    runs.StateRootError)` — the subclass makes the moved arm dead and contention is
    filed as "re-arm failed"; (2) drop `blocking=False` and the gesture queues for the
    holder's full hold.
    """
    import threading
    import time

    from bmad_loop import platform_util
    from bmad_loop.journal import STATE_FILE

    install_bmad_config(project_tree)
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: a rival process holds the run state.",
    )
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(BmadLoopApp, "_resolve_blocked_by_liveness", lambda *_a: False)
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda _self, message, **kwargs: notes.append(
            (str(message), str(kwargs.get("severity", "information")))
        ),
    )
    monkeypatch.setattr(
        runs_mod, "rearm_escalation", lambda *_a, **_k: pytest.fail("re-armed a locked run")
    )

    lock_path = runs_mod.lock_path_for(run_dir / STATE_FILE, follow_final_symlink=False)
    hold_s = 3.0
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with platform_util.file_lock(lock_path):
            held.set()
            release.wait(hold_s)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert held.wait(30), "the rival never acquired the run's state lock"
        started = time.monotonic()
        BmadLoopApp(project_tree.project)._do_rearm(run_dir.name, run_dir, "1")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(timeout=30)

    assert elapsed < hold_s / 2  # refused at once, not queued behind the holder
    # One toast, and it is the contention one: equality over the list covers both
    # negatives at once — no "re-arm failed" fault report, and no residue echo
    # crediting this gesture with journal records only the HOLDER can have written.
    assert [severity for _message, severity in notes] == ["warning"]
    assert "run state locked by another process" in notes[0][0]
    assert "re-arm failed" not in notes[0][0]


def test_rearm_contention_arm_precedes_the_generic_oserror_arm():
    """`LockUnavailableError` SUBCLASSES `OSError`, so `_do_rearm`'s contention arm is
    correct only in that ORDER: below the existing `except (RearmError, OSError,
    runs.StateRootError)` it is unreachable and every contention is reported as a
    fault, leaving the non-blocking acquire buying nothing.

    The behavioral test above reddens on the same swap; this one names the property, so
    the failure says what the invariant is instead of leaving the subclass relation to
    be rediscovered from a toast.

    Ablation: move the `except LockUnavailableError` arm after the OSError arm.
    """
    import ast
    import inspect
    import textwrap

    from bmad_loop import platform_util
    from bmad_loop.tui import app as app_mod

    assert issubclass(platform_util.LockUnavailableError, OSError)  # the whole hazard

    def caught(handler: ast.ExceptHandler) -> set[str]:
        if handler.type is None:
            return {"BaseException"}
        names: set[str] = set()
        for node in ast.walk(handler.type):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        return names

    tree = ast.parse(textwrap.dedent(inspect.getsource(app_mod.BmadLoopApp._do_rearm)))
    paired = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        arms = [caught(h) for h in node.handlers]
        narrow = [i for i, names in enumerate(arms) if "LockUnavailableError" in names]
        wide = [
            i
            for i, names in enumerate(arms)
            if "LockUnavailableError" not in names and names & {"OSError", "BaseException"}
        ]
        if not narrow or not wide:
            continue
        paired += 1
        assert max(narrow) < min(wide), (
            "_do_rearm catches LockUnavailableError after a wider OSError arm — the "
            "subclass makes the later arm unreachable, so contention is misreported"
        )
    assert paired == 1, "_do_rearm no longer pairs a LockUnavailableError arm with an OSError arm"


def test_escalation_rearm_contention_does_not_echo_the_holders_journal_records(
    project_tree, monkeypatch
):
    """A refused acquisition claims none of the HOLDER's journal records.

    `_do_rearm`'s `finally` recovers the re-arm records a raised call had already
    written — abort-only diagnostic recovery, as its docstring says. A refused
    acquisition is not that abort: it ran nothing, so every record appended between
    the pre-lock read and the refusal was written by the process that HOLDS the lock,
    and echoing it credits this gesture with a rival's re-arm.

    The rival's append is injected through `journal_entries_or_none` rather than raced
    on a real thread because the window is the microseconds between the pre-lock read
    and a non-blocking refusal — a real race would be a coin flip, and a negative
    assertion that only sometimes has anything to be negative about proves nothing.
    The exclusion itself is still the real sidecar lock, so the refusal is genuine.

    Ablation: drop `and not contended` from the `finally` and the holder's record is
    toasted here as this gesture's own residue.
    """
    import threading

    from bmad_loop import platform_util
    from bmad_loop.journal import STATE_FILE

    install_bmad_config(project_tree)
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: a rival process holds the run state.",
    )
    notes: list[str] = []
    monkeypatch.setattr(BmadLoopApp, "_resolve_blocked_by_liveness", lambda *_a: False)
    monkeypatch.setattr(
        BmadLoopApp, "notify", lambda _self, message, **_kwargs: notes.append(str(message))
    )

    reads = 0

    def racing_entries(_run_dir):
        # First read is the pre-lock watermark; any later one would be the `finally`,
        # by when the holder has appended a re-arm record of its own.
        nonlocal reads
        reads += 1
        if reads == 1:
            return []
        return [{"ts": 0.0, "kind": "stale-restore-excluded", "files": ["rival.py"]}]

    monkeypatch.setattr(runs_mod, "journal_entries_or_none", racing_entries)

    lock_path = runs_mod.lock_path_for(run_dir / STATE_FILE, follow_final_symlink=False)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with platform_util.file_lock(lock_path):
            held.set()
            release.wait(3.0)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert held.wait(30), "the rival never acquired the run's state lock"
        BmadLoopApp(project_tree.project)._do_rearm(run_dir.name, run_dir, "1")
    finally:
        release.set()
        holder.join(timeout=30)

    assert not any("excluded the abandoned restore" in note for note in notes)
    assert [note for note in notes if "run state locked by another process" in note]
    assert reads == 1  # the `finally` never took the second read at all


def test_escalation_rearm_reloads_state_before_restamping(project_tree, monkeypatch):
    """Ablation: delete _do_rearm's fresh state check and the TUI restamps a run
    whose escalation a rival already consumed while this gesture waited for the lock."""
    import contextlib

    from bmad_loop import runs
    from bmad_loop.journal import load_state, save_state
    from bmad_loop.tui import app as app_mod

    install_bmad_config(project_tree)
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: rival resolved this escalation.",
    )
    notes: list[str] = []
    monkeypatch.setattr(BmadLoopApp, "_resolve_blocked_by_liveness", lambda *_a: False)
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda _self, message, **_kwargs: notes.append(str(message)),
    )

    @contextlib.contextmanager
    def rival_first(_run_dir, **_kwargs):
        rival = load_state(run_dir)
        rival.tasks["1"].phase = Phase.PENDING
        save_state(run_dir, rival)
        yield

    monkeypatch.setattr(app_mod, "state_lock", rival_first)
    monkeypatch.setattr(runs, "restamp_code_root", lambda *_a: pytest.fail("stale restamp"))
    monkeypatch.setattr(runs, "rearm_escalation", lambda *_a, **_k: pytest.fail("double re-armed"))

    BmadLoopApp(project_tree.project)._do_rearm(run_dir.name, run_dir, "1")

    assert any("no longer paused at escalation" in note for note in notes)


def test_escalation_rearm_refuses_a_newer_generation_from_an_open_review(project_tree, monkeypatch):
    """An old modal must not consume a later escalation for the same story.

    Ablation: delete ``_do_rearm``'s generation comparison and the rival's newer
    escalation reaches ``rearm_escalation`` even though the modal never displayed it.
    """
    import contextlib

    from bmad_loop import runs
    from bmad_loop.journal import load_state, save_state
    from bmad_loop.tui import app as app_mod

    install_bmad_config(project_tree)
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: the original escalation.",
    )
    expected_generation = load_state(run_dir).tasks["1"].generation
    notes: list[str] = []
    monkeypatch.setattr(BmadLoopApp, "_resolve_blocked_by_liveness", lambda *_a: False)
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda _self, message, **_kwargs: notes.append(str(message)),
    )

    @contextlib.contextmanager
    def rival_first(_run_dir, **_kwargs):
        rival = load_state(run_dir)
        rival.tasks["1"].generation = expected_generation + 1
        save_state(run_dir, rival)
        yield

    monkeypatch.setattr(app_mod, "state_lock", rival_first)
    monkeypatch.setattr(runs, "restamp_code_root", lambda *_a: pytest.fail("stale restamp"))
    monkeypatch.setattr(runs, "rearm_escalation", lambda *_a, **_k: pytest.fail("stale rearm"))

    BmadLoopApp(project_tree.project)._do_rearm(
        run_dir.name,
        run_dir,
        "1",
        expected_generation=expected_generation,
    )

    assert any("changed while its review was open" in note for note in notes)


def test_escalation_rearm_retains_outer_lock_through_rearm_call(project_tree, monkeypatch):
    from bmad_loop import runs

    install_bmad_config(project_tree)
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision.",
    )
    rearms: list[Path] = []
    monkeypatch.setattr(BmadLoopApp, "_resolve_blocked_by_liveness", lambda *_a: False)

    def checked_rearm(rd, key, **_kwargs):
        assert_run_state_lock_held(rd)
        rearms.append(rd)
        return _rearm_outcome(key)

    monkeypatch.setattr(runs, "rearm_escalation", checked_rearm)
    app = BmadLoopApp(project_tree.project)
    monkeypatch.setattr(app, "notify", lambda *_a, **_k: None)
    monkeypatch.setattr(app, "_do_resume", lambda _run_id: None)

    app._do_rearm(run_dir.name, run_dir, "1")

    assert rearms == [run_dir]


@pytest.mark.parametrize(
    "failure",
    [OSError("lock file could not be created"), runs_mod.StateRootError("no usable state root")],
)
def test_escalation_rearm_reports_state_lock_failures(project_tree, monkeypatch, failure):
    """The other half of the contention split: an acquisition fault that is NOT a
    holder still reports "re-arm failed". Neither parameter may be a
    `LockUnavailableError` — that subclass is contention, routed to its own arm — and
    a plain `OSError` is exactly what `platform_util.file_lock` raises when the
    sidecar cannot be PROVISIONED, which its docstring keeps deliberately unwrapped
    because a broken path is not "someone is using this run"."""
    import contextlib

    from bmad_loop import runs
    from bmad_loop.tui import app as app_mod

    install_bmad_config(project_tree)
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision.",
    )
    notes: list[tuple[str, str | None]] = []
    monkeypatch.setattr(BmadLoopApp, "_resolve_blocked_by_liveness", lambda *_a: False)
    monkeypatch.setattr(runs, "rearm_escalation", lambda *_a, **_k: pytest.fail("wrote unlocked"))

    @contextlib.contextmanager
    def refusing_lock(_run_dir, **_kwargs):
        raise failure
        yield

    monkeypatch.setattr(app_mod, "state_lock", refusing_lock)
    app = BmadLoopApp(project_tree.project)
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **kwargs: notes.append((str(message), kwargs.get("severity"))),
    )

    app._do_rearm(run_dir.name, run_dir, "1")

    assert notes == [(f"re-arm failed: {failure}", "error")]


@pytest.mark.parametrize("shape", ["inside", "sibling"])
async def test_escalation_rearm_refuses_the_isolation_conflict_before_it_mutates(
    project, monkeypatch, shape
):
    """Parity with `cli.cmd_resolve` on the hoisted refusal, and for the same reason this
    surface needed the re-stamp parity above: it re-arms and resumes in ONE click.

    The detached CLI refuses `isolation = "worktree"` beside a `repo_root` override, but
    it does so in `_resume_paused_run` — downstream of everything this gesture has
    already written. So the re-stamp persisted the unsupported root, `rearm_escalation`
    advanced the attempt baseline against it, the operator was toasted "re-armed 1", and
    only then did the resumed pane refuse. The story was PENDING by then, and `resolve`
    needs an ESCALATED story, so the escalation could not be recovered by re-running it.

    Asserted against the sole producer of the text rather than a literal, matching the
    launch guard's row, so a reworded message cannot drift this away from the CLI's.

    Ablation: delete the `conflict is not None` arm from `_do_rearm` and this reddens on
    the re-arm that must not happen; move it below the `runs.restamp_code_root(...)` call
    and it reddens on the persisted root instead.
    """
    from bmad_loop import bmadconfig, resolve, runs
    from bmad_loop.journal import load_state, save_state

    calls: list[str] = []
    notes: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        runs,
        "rearm_escalation",
        lambda *a, **k: pytest.fail("re-armed under a configuration the run refuses"),
    )
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: notes.append(str(msg)) or orig_notify(self, msg, **kw),
    )
    _split_root_tui_project(project, shape)
    expected = bmadconfig.worktree_isolation_conflict(
        bmadconfig.load_paths(project.project), "worktree"
    )
    assert expected is not None, "the fixture really does carry the conflicting pair"
    run_dir, _spec = _stories_paused_run(
        project.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    recorded = str(project.project / "old-code")
    state = load_state(run_dir)
    state.repo_root = recorded
    save_state(run_dir, state)
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: expected in notes)

    assert not calls  # the resume folded into this gesture never fired
    assert load_state(run_dir).repo_root == recorded  # the mirror was never re-pointed


def test_escalation_rearm_proceeds_beside_a_nested_repo_root(project_tree, monkeypatch):
    """DW-379: the re-arm guard's copy of the refusal stays silent for a `repo_root`
    that CONTAINS the project (here its parent), so the gesture re-arms — with
    `isolated_redrive=True`, the live worktree mode — graded on the re-arm happening.

    Ablation: widen `worktree_isolation_conflict` back to "any `repo_root` override"
    and this reddens: the guard toasts the refusal and never re-arms."""
    from conftest import write_repo_root_override

    from bmad_loop import runs

    install_bmad_config(project_tree)
    write_repo_root_override(project_tree, project_tree.project.parent)
    (project_tree.project / ".bmad-loop").mkdir(parents=True, exist_ok=True)
    (project_tree.project / ".bmad-loop" / "policy.toml").write_text(
        '[adapter]\nname = "claude"\n\n[scm]\nisolation = "worktree"\n', encoding="utf-8"
    )
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision.",
    )
    rearms: list[bool] = []
    notes: list[str] = []
    monkeypatch.setattr(BmadLoopApp, "_resolve_blocked_by_liveness", lambda *_a: False)

    def recording_rearm(_rd, key, *, isolated_redrive=False, **_kwargs):
        rearms.append(isolated_redrive)
        return _rearm_outcome(key)

    monkeypatch.setattr(runs, "rearm_escalation", recording_rearm)
    app = BmadLoopApp(project_tree.project)
    monkeypatch.setattr(app, "notify", lambda message, **_k: notes.append(str(message)))
    monkeypatch.setattr(app, "_do_resume", lambda _run_id: None)

    app._do_rearm(run_dir.name, run_dir, "1")

    assert rearms == [True]
    assert not any("needs the project directory" in note for note in notes)


async def test_escalation_rearm_surfaces_the_kinds_it_used_to_drop(project, monkeypatch):
    """Every kind the shared table routes reaches this surface — not the three the
    TUI's own copy of the chain happened to handle.

    That copy carried `rearm-baseline-*` only and silently dropped the whole
    `stale-restore-*` family (and would have dropped `rearm-commits-probe-failed`,
    the record that says the commits probe could not answer at all), including
    `stale-restore-commits` — the record
    `cli._echo_rearm_events`' docstring calls the one a human must act on, and the
    one whose whole point is that nothing else will tell them. All of it is
    warn-only by contract, so a toast is the only place this path can ever show it,
    and this path RESUMES in the same gesture: a dropped record is a degrade the
    operator acts on without ever seeing. Routing both surfaces through
    `runs.rearm_event_notice` only buys anything if the TUI is graded against the
    table's whole vocabulary, so this walks a record of every arm the old copy
    missed plus the new spec-flip skip.

    Two renderings, not two tables: the severity map is graded here too (`note` is
    Textual's `information`, `warning` stays `warning`), as is the deliberate drop
    of `next_step` — its imperative reads "... before resuming" and the resume is
    already queued behind this toast.

    Ablation: make `runs.rearm_event_notice` return None for any one of these kinds
    and this reddens on that kind's message alone. Drop the remedy
    ("restore it from git or from your own copy") from the `rearm-aborted` `failed`
    MESSAGE while keeping it in that arm's `next_step` and only the remedy assertion
    reddens — which is the point of grading it here rather than on the CLI, where the
    dropped half is still printed.
    """
    from bmad_loop import resolve, runs
    from bmad_loop.journal import Journal

    calls: list[str] = []
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")

    def fake_rearm(rd, sk, *, isolated_redrive=False, resolution_recorded=False, project_root=None):
        journal = Journal(rd)
        journal.append(
            "stale-restore-commits",
            story_key=sk,
            old_baseline="f" * 40,
            commits=["c1", "c2"],
        )
        journal.append("stale-restore-excluded", story_key=sk, patch="a.patch", files=["new.txt"])
        # The commits record's TWIN: the probe that could not answer at all. It rides
        # this walk because the pair is the whole point — the record above is written
        # only when the probe answered, so without this one its absence reads as
        # "clean" on the surface that resumes in the same gesture (DW-81).
        journal.append(
            "rearm-commits-probe-failed",
            story_key=sk,
            old_baseline="e" * 40,
            error=f"GitError: git rev-list {'e' * 40}..HEAD failed in /code: fatal",
        )
        journal.append(
            "rearm-baseline-restamp-skipped",
            story_key=sk,
            spec_file="wt/specs/s1.md",
            baseline="c" * 40,
        )
        journal.append(
            "rearm-spec-flip-skipped",
            story_key=sk,
            spec_file="wt/specs/s1.md",
            status="ready-for-dev",
        )
        # `rearm-aborted` is journalled by `runs._rollback_rearm` from the transaction
        # guard's error path. It rides this walk for its ROUTING, which is what this test
        # grades — the rendering path is real on this surface either way, since a genuine
        # abort reaches `_do_rearm`'s `finally` (and so this echo) BEFORE its
        # `except RearmError` arm returns. The `failed` outcome is the one chosen on
        # purpose: it is the single re-arm kind whose imperative is NOT moot here, because
        # this path does not go on to resume, and this surface drops `next_step` — so the
        # restore-from-git remedy has to survive in the MESSAGE or a TUI operator never
        # gets it at all.
        journal.append(
            "rearm-aborted",
            story_key=sk,
            spec_file="wt/specs/s1.md",
            error="OSError: [Errno 28] No space left on device",
            rollback="failed",
        )
        return _journal_rearm_outcome(rd, sk)

    monkeypatch.setattr(runs, "rearm_escalation", fake_rearm)
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: (
            notes.append((str(msg), str(kw.get("severity", "information"))))
            or orig_notify(self, msg, **kw)
        ),
    )
    run_dir, _spec = _stories_paused_run(
        project.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])

    def severity_of(fragment: str) -> str:
        hits = [n for n in notes if fragment in n[0]]
        assert len(hits) == 1, f"{fragment!r} not surfaced exactly once: {notes}"
        return hits[0][1]

    # the one a human must act on — dropped entirely by the pre-table copy
    assert severity_of("2 commit(s) sit below the re-drive's new baseline (ffffffffffff..)") == (
        "warning"
    )
    # ...and its twin, the probe that could not answer — advisory, so a toast is the
    # only place this surface can ever show it
    assert severity_of("could not list the commits above the abandoned attempt's baseline") == (
        "warning"
    )
    # the range is carried in the MESSAGE, because this surface drops `next_step`
    assert any("git log eeeeeeeeeeee..HEAD" in n[0] for n in notes), notes
    assert not any("e" * 40 in n[0] for n in notes), notes
    assert severity_of("is not a readable file from here") == "warning"
    assert severity_of("could not be re-opened to `ready-for-dev`") == "warning"
    # `note` maps onto Textual's own channel name, not through unchanged
    assert severity_of("excluded the abandoned restore's new files") == "information"
    # the abort record, and specifically the half that only the MESSAGE can carry on a
    # surface with no `next_step`: without it a TUI operator is told the spec may be
    # part-written and given no remedy for it
    assert severity_of("may be left part-written") == "warning"
    # the WHOLE remedy, not its first three words: the message names a second source
    # because an untracked or out-of-checkout spec has no committed copy, and asserting
    # only the "from git" prefix passes for a message that never gained the rest
    assert any("restore it from git or from your own copy" in n[0] for n in notes), notes
    # the CLI's trailing imperative is omitted here: the resume is already queued
    assert not any("before resuming" in n[0] for n in notes), notes
    assert any("re-armed 1" in n[0] for n in notes)  # the ordinary notice still fires
    ordered_messages = (
        "2 commit(s) sit below the re-drive's new baseline",
        "excluded the abandoned restore's new files",
        "could not list the commits above the abandoned attempt's baseline",
        "is not a readable file from here",
        "could not be re-opened to `ready-for-dev`",
        "may be left part-written",
    )
    positions = [
        next(i for i, note in enumerate(notes) if message in note[0])
        for message in ordered_messages
    ]
    assert positions == sorted(positions)


async def test_escalation_rearm_holds_the_resume_it_folds_in(project, monkeypatch):
    """This surface's whole gesture is re-arm + resume, so the hold has to break it.

    `rearm-spec-write-unreachable` fires only once the re-arm has proven the committed
    spec does not carry the status the re-drive routes on — and this path drops the
    table's `next_step` on every ADVISORY toast precisely because it resumes in the same
    gesture. That silenced the one record whose remedy MUST land first in BOTH halves:
    the imperative was dropped as moot, and the resume it was warning against happened
    anyway, mounting a fresh worktree onto the still-terminal committed spec.

    The re-arm itself is kept — the story is armed and persisted — and the hold toast
    carries the held record's OWN `next_step` (`RearmOutcome.hold_next_step`), which on
    this record is the commit. Since the hold is what stops the fold-in, "before
    resuming" is finally true on this surface when it renders here. The
    `rearm-baseline-restamp-skipped` control keeps this a narrowing rather than
    "warnings stop resumes": it is a warning on the same walk, and the resume still fires.

    Ablation: drop the `if hold_resume:` arm from `_do_rearm` and the first leg reddens
    on `calls == []`, with the resume firing behind the warning it was told to wait for.
    Discard `_echo_rearm_events`' return and it reddens the same way.
    """
    from bmad_loop import resolve, runs
    from bmad_loop.journal import Journal

    calls: list[str] = []
    notes: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")

    def fake_rearm(rd, sk, *, isolated_redrive=False, resolution_recorded=False, project_root=None):
        Journal(rd).append(
            "rearm-spec-write-unreachable",
            story_key=sk,
            spec_file="wt/specs/s1.md",
            status="ready-for-dev",
        )
        Journal(rd).append(  # a warning on the same walk that must NOT hold the resume
            "rearm-baseline-restamp-skipped",
            story_key=sk,
            spec_file="wt/specs/s1.md",
            baseline="c" * 40,
        )
        return _journal_rearm_outcome(rd, sk)

    monkeypatch.setattr(runs, "rearm_escalation", fake_rearm)
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: notes.append(str(msg)) or orig_notify(self, msg, **kw),
    )
    run_dir, _spec = _stories_paused_run(
        project.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: any("not resuming" in n for n in notes))

    assert calls == []  # the resume this gesture folds in did NOT fire
    assert any("re-armed 1" in n for n in notes)  # ...while the re-arm itself stands
    assert any(
        "not resuming in this gesture. Commit the corrected spec with "
        "`status: ready-for-dev` before resuming — the run stays paused and resumable "
        "from this screen." in n
        for n in notes
    )
    # the record that proved it still renders, and its warning sibling did not hold
    assert any("land in a tree it discards" in n for n in notes)
    assert any("is not a readable file from here" in n for n in notes)


@pytest.mark.parametrize("redrive", ["in-place", "isolated"])
async def test_escalation_rearm_hold_names_the_holding_record_s_own_remedy(
    project, monkeypatch, redrive
):
    """The hold toast must carry the HELD record's remedy, not one hardcoded literal.

    Four records hold this surface's fold-in resume and their remedies differ. The
    newest — `rearm-spec-flip-skipped` on its `reaches_redrive and not refused` arm —
    is journalled with `refused = spec_path.is_file() and write_reaches_the_redrive`,
    so the holding arm ENTAILS `spec_path.is_file()` is False: there is no corrected
    spec at that path to commit, and on the isolated arm the path can be a shared
    artifact directory outside the project that is not a Git repository at all. The
    hardcoded "commit the corrected spec" was therefore not merely unhelpful there, it
    was impossible. Both re-drive modes reach this arm, so both are asserted.

    The negative half is the point and is matched case-insensitively: the fallback
    literal only differs from the ablated one by its leading capital, and a negative
    assertion that a capitalization slipped past would pass for the wrong reason.

    Ablation: revert `_do_rearm`'s hold branch to the hardcoded literal and BOTH legs
    redden — the positive on the missing path remedy, the negative on the commit
    imperative that cannot be obeyed.
    """
    from bmad_loop import resolve, runs
    from bmad_loop.journal import Journal

    calls: list[str] = []
    notes: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")

    def fake_rearm(rd, sk, *, isolated_redrive=False, resolution_recorded=False, project_root=None):
        Journal(rd).append(
            "rearm-spec-flip-skipped",
            story_key=sk,
            spec_file="/srv/artifacts/specs/s1.md",
            status="ready-for-dev",
            refused=False,
            reaches_redrive=True,
            redrive=redrive,
        )
        return _journal_rearm_outcome(rd, sk)

    monkeypatch.setattr(runs, "rearm_escalation", fake_rearm)
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: notes.append(str(msg)) or orig_notify(self, msg, **kw),
    )
    run_dir, _spec = _stories_paused_run(
        project.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: any("not resuming" in n for n in notes))

    assert calls == []  # this arm holds, so the folded-in resume did NOT fire
    assert any("re-armed 1" in n for n in notes)  # ...while the re-arm itself stands
    held = [n for n in notes if "not resuming" in n]
    assert len(held) == 1
    assert (
        "not resuming in this gesture. Restore the recorded spec path with "
        "`status: ready-for-dev` before resuming — the run stays paused and resumable "
        "from this screen." == held[0]
    )
    # the remedy the record CANNOT have: the holding arm proves the path is not a file
    assert "commit the corrected spec" not in held[0].lower()


async def test_escalation_rearm_holds_without_a_renderable_notice(project_tree, monkeypatch):
    """The authoritative hold is independent of whether there is a toast to render."""
    from bmad_loop import resolve, runs

    calls: list[str] = []
    notes: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        runs,
        "rearm_escalation",
        lambda rd, sk, **kwargs: runs.RearmOutcome(sk, (), True),
    )
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: notes.append(str(msg)) or orig_notify(self, msg, **kw),
    )
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: any("not resuming" in note for note in notes))

    assert calls == []
    assert any("re-armed 1" in note for note in notes)


async def test_escalation_rearm_echoes_residue_when_the_rearm_aborts(project, monkeypatch):
    """An aborted re-arm still surfaces what it already journalled — the CLI parity gap.

    `runs._stale_restore_residue` journals BEFORE the re-stamp block that raises
    `RearmError`, so on that path the records exist and the operator has to decide what
    to do with the tree. `cli.cmd_resolve` echoes them from a `finally`; this surface
    used to `return` inside the `except` and drop the whole family — including
    `stale-restore-commits`, which `cli._echo_rearm_events`' own docstring calls the one
    record a human must act on. The two surfaces had been unified on ROUTING while
    still drifting on the abort path, and `docs/FEATURES.md` claimed they could not
    drift at all.

    Ablation: move the `self._echo_rearm_events(...)` call out of the `finally` and back
    below the `try`, and this reddens — the commits warning never fires — while
    `test_escalation_rearm_survives_a_corrupt_journal` still passes.
    """
    from bmad_loop import resolve, runs
    from bmad_loop.journal import Journal
    from bmad_loop.runs import RearmError

    notes: list[str] = []
    calls: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")

    def fake_rearm(rd, sk, *, isolated_redrive=False, resolution_recorded=False, project_root=None):
        # exactly the real ordering: residue journalled, THEN the abort
        Journal(rd).append(
            "stale-restore-commits", story_key=sk, old_baseline="f" * 40, commits=["c1"]
        )
        raise RearmError("cannot re-stamp baseline_revision on /x/spec.md")

    monkeypatch.setattr(runs, "rearm_escalation", fake_rearm)
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: notes.append(str(msg)) or orig_notify(self, msg, **kw),
    )
    run_dir, _spec = _stories_paused_run(
        project.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: any("re-arm failed" in n for n in notes))
    # the abort is reported AND the residue it already wrote is surfaced
    assert any("commit(s) sit below" in n for n in notes), notes
    # ... and an aborted re-arm does not resume the run
    assert calls == [], calls


async def test_escalation_rearm_survives_a_corrupt_journal(project_tree, monkeypatch):
    """An undecodable journal cannot suppress a successful authoritative hold.

    `_do_rearm` reads the journal twice to diff what the re-arm appended, and before
    that echo existed it read it not at all — so `Journal.entries()`' strict UTF-8
    decode would have turned a corrupt journal into a re-arm the operator can no
    longer perform. That is strictly worse than the missing echo it was added to
    fix, and a regression against the gesture's own history. `runs.journal_entries_or_none`
    (shared with `cli.cmd_resolve`) answers `None`, and `_echo_rearm_events` skips the
    echo when either end of the diff is unreadable rather than replaying the journal
    from zero; the dashboard already reads this same file with `errors="replace"`
    everywhere else.

    Ablation: call `Journal(run_dir).entries()` directly in `_do_rearm` and this
    reddens — the UnicodeDecodeError escapes into the Textual worker and no
    `re-armed 1` notice ever fires.
    """
    from bmad_loop import resolve, runs
    from bmad_loop.journal import JOURNAL_FILE

    calls: list[str] = []
    notes: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")

    def fake_rearm(rd, sk, *, isolated_redrive=False, resolution_recorded=False, project_root=None):
        return runs.RearmOutcome(
            sk,
            (
                runs.RearmNotice(
                    "warning", "authoritative hold from the successful re-arm", "ignored"
                ),
            ),
            True,
        )

    monkeypatch.setattr(runs, "rearm_escalation", fake_rearm)
    orig_notify = BmadLoopApp.notify
    monkeypatch.setattr(
        BmadLoopApp,
        "notify",
        lambda self, msg, **kw: notes.append(str(msg)) or orig_notify(self, msg, **kw),
    )
    run_dir, _spec = _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked: needs a human decision on the auth scheme.",
    )
    # a real corruption shape: a valid line, then a byte no UTF-8 decoder accepts
    (run_dir / JOURNAL_FILE).write_bytes(
        b'{"ts": 1.0, "kind": "session-start", "task_id": "t1"}\n\xff\xfe not utf-8\n'
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await click(pilot, await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: any("not resuming" in n for n in notes))
    # the re-arm ran, its outcome rendered, and the authoritative hold stopped resume
    assert any("re-armed 1" in n for n in notes)
    assert not any("re-arm failed" in n for n in notes), notes
    assert any("authoritative hold from the successful re-arm" in n for n in notes), notes
    assert calls == []


async def test_escalation_rearm_disabled_without_resolution(project_tree, monkeypatch):
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _stories_paused_run(
        project_tree.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked.",
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await ready(pilot, "#act-rearm")
        assert app.screen.query_one("#act-rearm", Button).disabled


async def test_gate_pause_resume(project_tree, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    spec = project_tree.project / "spec-1-1-a.md"
    spec.write_text("---\nstatus: ready-for-dev\n---\n# finalized spec\n", encoding="utf-8")
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_VERIFY)
    task.spec_file = str(spec)
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="spec-approval",
        paused_reason="awaiting spec approval",
        paused_story_key="1-1-a",
        tasks={"1-1-a": task},
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await click(pilot, await ready(pilot, "#act-resume"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


# ------------------------------- #515: spec-less gate pauses show the reason
#
# A story gate fires BEFORE the story is registered in state.tasks (deliberate —
# a resume re-picks the story and re-asks the ledger) and an epic boundary has no
# story key at all, so _paused_spec returns (None, "") and the spec viewer had
# nothing to show. These pin that the pause reason — which names the blocking
# entries and the remedy — is what the operator gets instead.

_GATE_REASON = (
    "1-1 is gated by unlanded deferred work: DW-1 (gate: 1-1) — close the entry in "
    "deferred-work.md, or clear its gate, then resume."
)


@pytest.mark.parametrize(
    ("story_key", "tasks"),
    [
        # the engine gate: the story is not in state.tasks yet at all
        ("1-1", {}),
        # sweep's ledger-migration gate (sweep.py): the task IS registered, it just
        # has no spec_file — the other arm of _paused_spec's (None, "") return
        ("sweep-migrate", {"sweep-migrate": StoryTask(story_key="sweep-migrate", epic=0)}),
        # DW-243: a sweep bundle re-armed after a dev escalation KEEPS its spec_file,
        # and its intent-regeneration refusal pauses at the story gate. The gate is
        # about the ledger, never the spec, so the reason (with its repair steer)
        # must show — not the spec viewer's "Approve & resume". Ablation: drop the
        # story-gate shortcut before `_paused_spec` in `_review_gate`
        # and this case attempts the forbidden spec read.
        ("dw-fix", {"dw-fix": "with-spec-file"}),
    ],
    ids=["task-unregistered", "task-without-spec-file", "task-with-spec-file"],
)
async def test_story_gate_pause_shows_reason_and_resumes(
    project_tree, monkeypatch, story_key, tasks
):
    spec_reads: list[bool] = []

    def unused_spec_read(*args):
        spec_reads.append(True)
        return None, "", True

    monkeypatch.setattr(BmadLoopApp, "_paused_spec", unused_spec_read)
    calls: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    if tasks.get("dw-fix") == "with-spec-file":
        spec = project_tree.implementation_artifacts / "spec-dw-fix.md"
        spec.write_text("# spec-dw-fix\n", encoding="utf-8")
        tasks = {
            "dw-fix": StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
        }
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="story-gate",
        paused_reason=_GATE_REASON,
        paused_story_key=story_key,
        tasks=tasks,
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, PauseReasonModal)  # routed away from the spec viewer
        assert spec_reads == [], "a story-gate reason viewer must not read the unused spec"
        await ready(pilot, "#reason Static")
        body = render(app.screen.query_one("#reason Static", Static).content)
        assert "gated by unlanded deferred work" in body
        assert "DW-1" in body, "the reason names the blocking entry, not a blank pane"
        await click(pilot, await ready(pilot, "#act-resume"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


_ENV_REASON = (
    "environment fault before dev session dispatch — probe failed (rc=3): probe\n"
    "no session was started and nothing was charged; fix the environment, then run "
    "`bmad-loop resume 20260611-100000-aaaa` (the probes re-run first)"
)


async def test_review_pause_environment_opens_resume_viewer(project_tree, monkeypatch):
    """DW-523: an environment pause is spec-less like a story gate — the failed probe
    and its remedy ARE the reason, so it shows even when the paused task carries a
    spec_file. Ablation: drop PAUSE_ENVIRONMENT from `_review_gate`'s spec-less tuple
    (spec read) or from `action_review_pause`'s routing (no viewer at all)."""
    spec_reads: list[bool] = []

    def unused_spec_read(*args):
        spec_reads.append(True)
        return None, "", True

    monkeypatch.setattr(BmadLoopApp, "_paused_spec", unused_spec_read)
    calls: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    spec = project_tree.implementation_artifacts / "spec-1-1-a.md"
    spec.write_text("# spec-1-1-a\n", encoding="utf-8")
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage=PAUSE_ENVIRONMENT,
        paused_reason=_ENV_REASON,
        paused_story_key="1-1-a",
        tasks={"1-1-a": StoryTask(story_key="1-1-a", epic=1, spec_file=str(spec))},
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, PauseReasonModal)  # routed away from the spec viewer
        assert spec_reads == [], "an environment-pause viewer must not read the spec"
        title = render(app.screen.query_one(".title", Static).content)
        assert "environment fault" in title
        await ready(pilot, "#reason Static")
        # wide enough that the remedy's command does not wrap mid-string
        body = render(app.screen.query_one("#reason Static", Static).content, width=400)
        assert "probe failed (rc=3)" in body
        assert "bmad-loop resume 20260611-100000-aaaa" in body
        await click(pilot, await ready(pilot, "#act-resume"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


async def test_epic_boundary_pause_shows_reason_and_run_id_subtitle(project_tree, monkeypatch):
    """An epic boundary raises with no story key, so the old viewer subtitled it
    "?". The run id is the only identity there is — assert it positively, so a
    regression back to _story_subtitle's placeholder reddens this."""
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="epic-boundary",
        paused_reason="epic 1 boundary — `bmad-loop resume <id>` to continue with epic 2",
        paused_story_key=None,
        tasks={},
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, PauseReasonModal)
        await ready(pilot, "#reason Static")
        assert "epic 1 boundary" in render(app.screen.query_one("#reason Static", Static).content)
        subtitle = render(app.screen.query_one("#subtitle", Static).content)
        assert "run 20260611-100000-aaaa" in subtitle


async def test_spec_approval_unreadable_spec_still_uses_spec_viewer(project_tree, monkeypatch):
    """An unreadable spec file still returns its PATH from _paused_spec — a spec that
    exists in the task and cannot be read, not a spec-less gate. It keeps the spec
    viewer, which pins the branch as `spec_path is None` rather than `not spec_text`.
    The body is now the read failure rather than "" (an absent spec at the anchored
    path is the signal that anchoring failed, so it must not render as "(empty spec)"
    — see `test_paused_spec_missing_at_the_anchor_reads_as_not_found`); this row
    grades only that the viewer, not the reason-only modal, is chosen."""
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_VERIFY)
    task.spec_file = str(project_tree.project / "gone" / "spec-1-1-a.md")
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="spec-approval",
        paused_reason="awaiting spec approval",
        paused_story_key="1-1-a",
        tasks={"1-1-a": task},
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)


async def test_story_gate_empty_reason_renders_fallback(project_tree, monkeypatch):
    """RunState.paused is `paused_reason is not None`, so an empty reason is a
    reachable pause — the viewer says so rather than showing an empty pane."""
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="story-gate",
        paused_reason="",
        paused_story_key="1-1",
        tasks={},
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, PauseReasonModal)
        await ready(pilot, "#reason Static")
        body = render(app.screen.query_one("#reason Static", Static).content)
        assert "(no pause reason recorded)" in body


async def test_start_run_modal_stories_source_launches(project, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(launch, "mux_available", lambda: True)

    def fake_start(proj, run_id, *, spec=None, epic, story, max_stories):
        calls.update(spec=spec, epic=epic, story=story)

    monkeypatch.setattr(launch, "start_run_detached", fake_start)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await ready(pilot, "#ok")
        app.screen.query_one("#source", Select).value = "stories"
        app.screen.query_one("#spec-folder", Input).value = "epic-1"
        await pilot.pause()
        await pilot.click("#ok")
        await until(pilot, lambda: bool(calls))
        assert calls["spec"] == "epic-1"


async def test_start_run_modal_stories_preview_validates(project_tree, monkeypatch):
    # action_start_run bails on _mux_missing() before it can push the modal, and
    # the Windows CI matrix has no tmux on PATH — every StartRunModal test stubs
    # this out so the modal opens (its absence here was the all-Windows timeout).
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    _write_stories_fixture(project_tree.project)  # epic-1 with two stories, 1 done
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await ready(pilot, "#preview-body")
        modal = app.screen
        body = modal.query_one("#preview-body", Static)

        # Switch to stories mode with a spec folder. A programmatic reactive
        # `.value` set posts its Changed from outside the app's message pump, so
        # invoke the same handler the framework routes to directly — the preview
        # projection is asserted synchronously, with no dependence on async Changed
        # delivery, and the on_input_changed/on_select_changed routing is covered.
        modal.query_one("#source", Select).value = "stories"
        spec_input = modal.query_one("#spec-folder", Input)
        spec_input.value = "epic-1"
        modal.on_input_changed(Input.Changed(spec_input, "epic-1"))
        rendered = str(body.render())
        assert "2 stories" in rendered
        # checkpoint markers + live disk state surfaced in the preview
        assert "(done)" in rendered  # story 1's on-disk spec status
        assert "[spec]" in rendered  # story 1's spec_checkpoint marker

        # a Changed from an unrelated input is ignored (route guard), and the
        # source select drives the preview back to the sprint-mode default.
        modal.on_input_changed(Input.Changed(modal.query_one("#epic", Input), "9"))
        assert "2 stories" in str(body.render())
        source = modal.query_one("#source", Select)
        source.value = "sprint-status"
        modal.on_select_changed(Select.Changed(source, "sprint-status"))
        assert "sprint mode" in str(body.render())


async def test_start_run_modal_stories_source_blank_folder_errors(project_tree, monkeypatch):
    calls: list = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await ready(pilot, "#ok")
        app.screen.query_one("#source", Select).value = "stories"  # no spec folder
        await pilot.pause()
        await pilot.click("#ok")
        await until(pilot, lambda: any("needs a spec folder" in m for m in notifications(app)))
        assert not calls


def _write_undecodable_policy(root: Path, text: str) -> Path:
    """Write `text` to policy.toml followed by bytes no UTF-8 decoder accepts.

    0xff/0xfe are illegal as UTF-8 lead bytes anywhere in a stream, so
    `read_text(encoding="utf-8")` — what `policy.load` calls — raises
    `UnicodeDecodeError` over the whole file however valid the leading TOML is.
    Real bytes, not a monkeypatched raiser: the decode is the thing under test."""
    bmad = root / ".bmad-loop"
    bmad.mkdir(parents=True, exist_ok=True)
    path = bmad / "policy.toml"
    path.write_bytes(text.encode("utf-8") + b"# \xff\xfe\n")
    with pytest.raises(UnicodeDecodeError):  # the fixture is genuinely undecodable
        path.read_text(encoding="utf-8")
    return path


async def test_start_run_modal_prefill_degrades_on_undecodable_policy(project_tree, monkeypatch):
    """Pins the `except (PolicyError, OSError)` handler in BmadLoopApp._stories_defaults
    against a policy.toml whose *bytes* won't decode: the modal prefills sprint mode.

    `UnicodeDecodeError` is a `ValueError`, so before `policy.load` converted it to
    `PolicyError` it walked past that handler untouched. It never even got that far in
    practice — BmadLoopApp.__init__ eagerly builds DashboardScreen, which loads the same
    file, so the failure mode was a crash at app CONSTRUCTION rather than a degraded
    prefill: the operator could not reach the modal at all. This is the direct oracle for
    the prefill half; the dashboard half is test_dashboard_survives_undecodable_policy_bytes."""
    text = '[stories]\nsource = "stories"\nspec_folder = "_bmad-output/epic-1"\n'
    # Precondition: decodable, this file would prefill stories mode + that folder. So the
    # sprint-mode assertions below show the *decode* was refused, not an inert fixture.
    pol = policy_mod.loads(text)
    assert (pol.stories.source, pol.stories.spec_folder) == ("stories", "_bmad-output/epic-1")
    _write_undecodable_policy(project_tree.project, text)
    # action_start_run bails on _mux_missing() before it can push the modal — see the
    # comment on test_start_run_modal_stories_preview_validates.
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await ready(pilot, "#ok")
        modal = app.screen
        assert modal.query_one("#source", Select).value == "sprint-status"
        assert modal.query_one("#spec-folder", Input).value == ""


# --------------------------------------------------------------- pane resizing


async def _seeded(pilot, app: BmadLoopApp) -> DashboardScreen:
    """Mount the dashboard and wait for the first-layout geometry seed."""
    await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
    screen = dashboard(app)
    await until(pilot, lambda: screen._seeded)
    return screen


async def _drag(pilot, selector: str, dx: int, dy: int) -> None:
    """Mouse-drag a splitter by (dx, dy) cells: down on it, one move offset from
    its own origin, then up. Capture routes the move to the splitter regardless."""
    await pilot.mouse_down(selector)
    await pilot._post_mouse_events([MouseMove], selector, offset=(dx, dy))
    await pilot.mouse_up(selector)
    await pilot.pause()


async def test_resize_mode_widens_and_narrows_sidebar(project_tree):
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        left = screen.query_one("#left")
        detail = screen.query_one("#detail")
        w0, d0 = left.size.width, detail.size.width
        await pilot.press("ctrl+w")
        assert screen._resize_mode
        for _ in range(5):
            await pilot.press("right")
        await pilot.pause()
        assert left.size.width == w0 + 5
        assert detail.size.width == d0 - 5  # #detail (1fr) absorbs the change
        for _ in range(3):
            await pilot.press("left")
        await pilot.pause()
        assert left.size.width == w0 + 2
        await pilot.press("escape")
        assert not screen._resize_mode


async def test_resize_mode_grows_left_panes_and_cycles(project_tree):
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        runs = screen.query_one("#runs")
        deferred = screen.query_one("#deferred")
        r0, f0 = runs.size.height, deferred.size.height
        await pilot.press("ctrl+w")
        # Down on the Runs|Sprint boundary grows Runs; Sprint (the flex) shrinks.
        for _ in range(3):
            await pilot.press("down")
        await pilot.pause()
        assert screen._left_frozen
        assert runs.size.height == r0 + 3
        assert deferred.size.height == f0  # untouched boundary stays put
        # Tab moves the active boundary to Sprint|Deferred; Up grows Deferred.
        await pilot.press("tab")
        assert screen._active_hsplit == 1
        for _ in range(2):
            await pilot.press("up")
        await pilot.pause()
        assert deferred.size.height == f0 + 2
        assert runs.size.height == r0 + 3  # Runs boundary unaffected


async def test_resize_mode_reverse_cycles_left_panes(project_tree):
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        runs = screen.query_one("#runs")
        deferred = screen.query_one("#deferred")
        r0, f0 = runs.size.height, deferred.size.height
        await pilot.press("ctrl+w")
        # Shift+Tab walks the same ring the other way: from Runs|Sprint it wraps
        # backwards onto the last boundary (Tasks|Tabs, in the detail column).
        await pilot.press("shift+tab")
        assert screen._active_hsplit == 2
        # One more step back lands on Sprint|Deferred; Up grows Deferred.
        await pilot.press("shift+tab")
        assert screen._active_hsplit == 1
        for _ in range(2):
            await pilot.press("up")
        await pilot.pause()
        assert screen._left_frozen
        assert deferred.size.height == f0 + 2
        assert runs.size.height == r0  # untouched boundary stays put
        # A third step back closes the ring at Runs|Sprint; Down grows Runs.
        await pilot.press("shift+tab")
        assert screen._active_hsplit == 0
        for _ in range(3):
            await pilot.press("down")
        await pilot.pause()
        assert runs.size.height == r0 + 3
        assert deferred.size.height == f0 + 2  # Deferred boundary unaffected


async def test_arrows_and_tab_untouched_outside_resize_mode(project_tree):
    root = project_tree.project
    make_run(root, "20260611-100000-aaaa", finished=True)
    make_run(root, "20260611-110000-bbbb", finished=True)
    app = BmadLoopApp(root)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        runs = screen.query_one("#runs", DataTable)
        await until(pilot, lambda: runs.row_count == 2)
        runs.focus()
        await pilot.pause()
        assert runs.cursor_row == 1  # newest auto-selected (bottom row)
        await pilot.press("up")  # not resizing: arrow drives the table cursor
        await pilot.pause()
        assert runs.cursor_row == 0
        assert screen.query_one("#left").size.width == 34  # geometry untouched
        await pilot.press("tab")  # not resizing: tab moves focus
        await pilot.pause()
        assert screen.focused is not runs


async def test_mouse_drag_resizes_sidebar_and_left_pane(project_tree):
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        w0 = screen.query_one("#left").size.width
        await _drag(pilot, "#split-main", 6, 0)
        assert screen.query_one("#left").size.width == w0 + 6
        r0 = screen.query_one("#runs").size.height
        await _drag(pilot, "#split-runs", 0, 2)  # drag the bar down: Runs grows
        assert screen._left_frozen
        assert screen.query_one("#runs").size.height == r0 + 2


async def test_mouse_drag_resizes_tasks_and_tabs(project_tree):
    """The detail-column boundary: dragging #split-tasks grows Tasks and shrinks
    the Tabs pane (which flexes). Regressed the whole boundary being unusable."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        tasks = screen.query_one("#tasks", DataTable)
        tabs = screen.query_one("#tabs", TabbedContent)
        # First drag freezes the column and pins Tasks to an explicit height (an
        # empty table's `auto` height sits below _MIN_TASKS, so the seed floors it
        # rather than matching the rendered height — measure after it settles).
        await _drag(pilot, "#split-tasks", 0, 2)
        assert screen._detail_frozen
        t0, b0 = tasks.size.height, tabs.size.height
        await _drag(pilot, "#split-tasks", 0, 3)  # drag the bar down: Tasks grows
        assert tasks.size.height == t0 + 3
        assert tabs.size.height == b0 - 3  # #tabs (1fr) absorbs the change


async def test_persisted_tall_tasks_height_survives_max_height_cap(project_tree):
    """Regression: a persisted tasks_height above the CSS `max-height: 35%`
    default must render at full height, not be silently re-clamped to 35% —
    which froze the boundary (story-maker: tasks_height=30, no run selected)."""
    root = project_tree.project
    bmad = root / ".bmad-loop"
    bmad.mkdir(parents=True, exist_ok=True)
    # No run selected: the detail column is in its empty state, so the CSS 35%
    # cap is the only thing that could clamp the persisted height.
    (bmad / "policy.toml").write_text("[tui]\ntasks_height = 30\n", encoding="utf-8")
    app = BmadLoopApp(root)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        await pilot.pause()
        assert screen.selected_run_id is None
        assert screen._detail_frozen
        tasks = screen.query_one("#tasks", DataTable)
        detail_h = screen.query_one("#detail").size.height
        # The pane renders at the governed height, well past 35% of the column.
        assert tasks.size.height == screen.tasks_height
        assert tasks.size.height > 0.35 * detail_h
        # And the boundary is live: dragging the bar up shrinks Tasks / grows Tabs.
        tabs = screen.query_one("#tabs", TabbedContent)
        b0 = tabs.size.height
        await _drag(pilot, "#split-tasks", 0, -5)
        assert tabs.size.height > b0


async def test_sidebar_width_is_clamped(project_tree):
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        screen.left_width = 9999  # absurd: clamps to width - _MIN_DETAIL - splitter
        await pilot.pause()
        hi = 120 - _MIN_DETAIL - 1
        assert screen.left_width == hi
        assert screen.query_one("#left").size.width == hi
        assert screen.query_one("#detail").size.width >= _MIN_DETAIL
        screen.left_width = 1  # below the floor
        await pilot.pause()
        assert screen.left_width == _MIN_SIDEBAR


async def test_geometry_persists_and_restores(project_tree):
    root = project_tree.project
    policy_path = root / ".bmad-loop" / "policy.toml"
    assert not policy_path.is_file()
    app = BmadLoopApp(root)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        await pilot.press("ctrl+w")
        for _ in range(4):
            await pilot.press("right")  # widen sidebar
        for _ in range(2):
            await pilot.press("down")  # grow Runs (freezes the left column)
        await pilot.press("escape")  # exits resize mode -> persists
        await pilot.pause()
        want = (
            screen.query_one("#left").size.width,
            screen.query_one("#runs").size.height,
            screen.query_one("#deferred").size.height,
        )
    assert policy_path.is_file()
    saved = policy_mod.load(policy_path).tui
    assert saved.left_width > 34 and saved.runs_height > 0 and saved.deferred_height > 0

    # A fresh app in the same project restores the identical rendered geometry.
    app2 = BmadLoopApp(root)
    async with app2.run_test(size=(120, 40)) as pilot:
        screen2 = await _seeded(pilot, app2)
        got = (
            screen2.query_one("#left").size.width,
            screen2.query_one("#runs").size.height,
            screen2.query_one("#deferred").size.height,
        )
    assert got == want


async def test_untouched_layout_writes_nothing_and_keeps_defaults(project_tree):
    """No resize -> no policy file, panes at their CSS defaults, columns still
    flex (unfrozen)."""
    root = project_tree.project
    app = BmadLoopApp(root)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        assert screen.query_one("#left").size.width == 34
        assert not screen._left_frozen and not screen._detail_frozen
        # Entering and leaving resize mode with no change must not create a file.
        await pilot.press("ctrl+w")
        await pilot.press("escape")
        await pilot.pause()
    assert not (root / ".bmad-loop" / "policy.toml").is_file()


async def test_split_runs_label_tracks_sprint_vs_stories(project_tree):
    """The splitter above the middle slot carries its section title, swapping
    Sprint<->Stories with the selected run's board mode."""
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        bar = screen.query_one("#split-runs", Splitter)
        assert bar.label == "Sprint"
        screen._apply_board(_Snapshot(generation=screen._generation, stories_mode=True, stories=[]))
        await pilot.pause()
        assert bar.label == "Stories"


async def test_dashboard_survives_policy_read_oserror(project_tree, monkeypatch):
    """A transient read failure (permissions, race after the is_file check) while
    loading policy at construction degrades to default geometry instead of
    crashing the TUI at startup."""

    def boom(path):
        raise OSError("permission denied")

    monkeypatch.setattr(policy_mod, "load", boom)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        assert screen._tui_policy == policy_mod.TuiPolicy()
        assert screen.query_one("#left").size.width == 34  # CSS default, unseeded
        assert not screen._left_frozen and not screen._detail_frozen


async def test_dashboard_survives_undecodable_policy_bytes(project_tree):
    """Pins the `except (PolicyError, OSError)` handler in DashboardScreen.__init__
    against a policy.toml whose *bytes* won't decode.

    `UnicodeDecodeError` is a `ValueError`, so before `policy.load` converted it to
    `PolicyError` it walked straight past that handler — and since BmadLoopApp.__init__
    eagerly constructs DashboardScreen, the failure was a crash at app CONSTRUCTION,
    before run_test ever mounted a screen or a key was pressed. Not a degraded render:
    no render at all. The sibling OSError test monkeypatches its raiser; this one uses
    real bytes, which is what makes it a decode test rather than a duplicate."""
    text = "[tui]\nleft_width = 50\n"
    # Precondition: decodable, this file would seed a 50-column sidebar. So asserting
    # the CSS default below shows the *decode* was refused, not that the file was inert.
    assert policy_mod.loads(text).tui.left_width == 50
    _write_undecodable_policy(project_tree.project, text)
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        assert screen._tui_policy == policy_mod.TuiPolicy()
        assert screen.query_one("#left").size.width == 34  # CSS default, not the file's 50
        assert not screen._left_frozen and not screen._detail_frozen


async def test_dashboard_survives_a_wrong_typed_policy_value(project_tree):
    """The same `except (PolicyError, OSError)` handler, reached by a policy file that
    is perfectly readable — valid UTF-8, valid TOML — and wrong only in a VALUE.

    The two siblings above fault at the FILE level (a monkeypatched OSError raiser,
    then undecodable bytes). This is the first one to fault at a KEY. `max_parallel`
    was a bare `int()` until #440, so `"x"` left `policy.load` as a raw ValueError,
    and a ValueError is neither a PolicyError nor an OSError — it walked past this
    handler exactly as the undecodable bytes did, crashing at app CONSTRUCTION before
    run_test could mount a screen. Note the fault sits in [scm], a section the
    dashboard never reads: `load` parses the whole document, so a wrong-typed key
    anywhere in the file took the TUI down."""
    text = '[tui]\nleft_width = 50\n[scm]\nmax_parallel = "x"\n'
    # Precondition: decodable AND well-formed TOML — that is what makes this a value
    # test rather than a second copy of the two above.
    assert tomllib.loads(text)["scm"]["max_parallel"] == "x"
    # Precondition: the [tui] half alone would seed a 50-column sidebar, so asserting
    # the CSS default below shows the file was REFUSED, not that it was inert.
    assert policy_mod.loads("[tui]\nleft_width = 50\n").tui.left_width == 50
    with pytest.raises(policy_mod.PolicyError):  # and the whole document is refused
        policy_mod.loads(text)
    bmad = project_tree.project / ".bmad-loop"
    bmad.mkdir(parents=True, exist_ok=True)
    (bmad / "policy.toml").write_text(text, encoding="utf-8")
    app = BmadLoopApp(project_tree.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        assert screen._tui_policy == policy_mod.TuiPolicy()
        assert screen.query_one("#left").size.width == 34  # CSS default, not the file's 50
        assert not screen._left_frozen and not screen._detail_frozen


async def test_first_geometry_save_writes_only_tui_keys(project_tree):
    """A geometry save on a project without policy.toml must create a minimal
    [tui]-only file — not materialise POLICY_TEMPLATE, which would freeze every
    default setting (gates, limits, ...) into the fresh file."""
    root = project_tree.project
    policy_path = root / ".bmad-loop" / "policy.toml"
    assert not policy_path.is_file()
    app = BmadLoopApp(root)
    async with app.run_test(size=(120, 40)) as pilot:
        await _seeded(pilot, app)
        await pilot.press("ctrl+w")
        for _ in range(3):
            await pilot.press("right")  # widen the sidebar only
        await pilot.press("escape")  # exits resize mode -> persists
        await pilot.pause()
    doc = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    assert set(doc) == {"tui"}
    assert doc["tui"] == {"left_width": 37}  # 34 + 3; untouched dims stay unset


async def test_quit_in_resize_mode_persists_geometry(project_tree):
    """Quitting the app mid-resize-mode still persists the new geometry:
    keyboard bumps only save on mode exit, and quit stays live in the mode."""
    root = project_tree.project
    app = BmadLoopApp(root)
    async with app.run_test(size=(120, 40)) as pilot:
        await _seeded(pilot, app)
        await pilot.press("ctrl+w")
        for _ in range(4):
            await pilot.press("right")
        # Leave without Escape: shutdown unmounts the screen, which persists.
    saved = policy_mod.load(root / ".bmad-loop" / "policy.toml").tui
    assert saved.left_width == 38  # 34 + 4


def test_run_tui_trips_forced_warning_before_app_capture(monkeypatch, capsys, tmp_path):
    """The forced-backend usability warning is once-per-process on stderr, and
    Textual captures sys.stderr for the app's whole run — so run_tui must trip
    the warning (and its latch) BEFORE App.run, or a first firing inside the
    app (any observer gate) consumes the single emission invisibly."""
    from bmad_loop.adapters import multiplexer as mux_mod
    from bmad_loop.tui import app as tui_app

    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "tmux")
    monkeypatch.setattr(mux_mod, "_usable", lambda mux: False)
    monkeypatch.setattr(mux_mod, "_FORCED_UNUSABLE_WARNED", False)
    mux_mod.get_multiplexer.cache_clear()

    stderr_at_run: list[str] = []

    class _StubApp:
        def __init__(self, _project):
            pass

        def run(self):
            # snapshot what already reached stderr when the app takes over
            stderr_at_run.append(capsys.readouterr().err)

    monkeypatch.setattr(tui_app, "BmadLoopApp", _StubApp)
    try:
        assert tui_app.run_tui(tmp_path) == 0
    finally:
        mux_mod.get_multiplexer.cache_clear()  # don't leak the forced pick
    assert stderr_at_run and "forced multiplexer backend" in stderr_at_run[0]


def test_run_tui_survives_junk_forced_backend(monkeypatch, tmp_path):
    """A junk forced name makes selection raise MultiplexerError; the preflight
    must swallow it (the same junk name still fails loudly at every real mux
    call site) so the TUI itself can still come up."""
    from bmad_loop.adapters import multiplexer as mux_mod
    from bmad_loop.tui import app as tui_app

    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "no-such-backend")
    mux_mod.get_multiplexer.cache_clear()

    ran: list[bool] = []

    class _StubApp:
        def __init__(self, _project):
            pass

        def run(self):
            ran.append(True)

    monkeypatch.setattr(tui_app, "BmadLoopApp", _StubApp)
    try:
        assert tui_app.run_tui(tmp_path) == 0
    finally:
        mux_mod.get_multiplexer.cache_clear()
    assert ran == [True]


def _write_two_triage_decisions(run_dir: Path) -> None:
    """A sweep triage carrying TWO decisions, so a walk has somewhere to continue to."""
    import json

    (run_dir / "triage.json").write_text(
        json.dumps(
            {
                "workflow": "deferred-sweep-triage",
                "open_ids": ["DW-1", "DW-2"],
                "already_resolved": [],
                "bundles": [],
                "blocked": [],
                "skip": [],
                "decisions": [
                    {
                        "id": dw_id,
                        "question": f"what about {dw_id}?",
                        "context": "ctx",
                        "options": [
                            {"key": "1", "label": "Widen", "effect": "build", "intent": "widen it"},
                            {"key": "2", "label": "Keep", "effect": "keep-open"},
                        ],
                        "recommendation": "1",
                    }
                    for dw_id in ("DW-1", "DW-2")
                ],
                "escalations": [],
            }
        ),
        encoding="utf-8",
    )


async def test_decision_modal_survives_lock_and_state_root_failures(project, monkeypatch):
    """Both ledger-lock failures degrade to a per-decision toast, and the walk
    carries on to the next decision (#286/#469).

    `apply_pre_answer` now takes a cross-process lock whose sidecar path is
    derived from the state root, which gives it two new ways to fail: `OSError`
    from the acquisition itself, and `runs.StateRootError` from deriving the
    path. The second is NOT an `OSError`, and here that distinction is not
    cosmetic — an uncaught exception in this callback does not print a traceback
    and exit, it escapes into the Textual event loop and takes the dashboard
    down mid-walk, with the human's remaining answers unrecorded and no window
    left to type them into.

    Both are raised, in that order, across two pending decisions: the first
    grades the arm that already existed, the second grades the widened tuple.
    The second modal appearing at all is what says the walk continued rather
    than stopping at the first failure, and it is keyed on the modal's own
    decision id so a first modal that simply never dismissed cannot satisfy it.

    Ablation: drop `runs.StateRootError` from `_record_decision`'s catch tuple.
    The DW-1 toast still lands; the DW-2 assertions red, with the exception
    coming out of `run_test` instead of arriving as a notification.
    """
    from bmad_loop import decisions as decisions_mod
    from bmad_loop import runs

    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "### DW-1: first thing\n\norigin: t\nlocation: a.py:1\nreason: t.\nstatus: open\n\n"
        "### DW-2: second thing\n\norigin: t\nlocation: b.py:1\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    _write_two_triage_decisions(make_run(project.project, "20260101-000000-aaaa", run_type="sweep"))

    failures = iter(
        [
            OSError(11, "Resource deadlock avoided"),
            runs.StateRootError("no usable state root"),
        ]
    )

    def boom(*_args, **_kwargs):
        raise next(failures)

    # `bmad_loop.tui.app` holds the module, not the function, so patching the
    # attribute here is what the TUI call site resolves.
    monkeypatch.setattr(decisions_mod, "apply_pre_answer", boom)

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, DecisionModal))
        await click(pilot, await ready(pilot, "#opt-1"))

        # OSError: toast, no crash.
        await until(pilot, lambda: any("failed to record DW-1" in m for m in notifications(app)))
        # ...and the walk moved on, rather than ending on the first failure.
        await until(
            pilot,
            lambda: isinstance(app.screen, DecisionModal) and app.screen._decision.id == "DW-2",
        )
        await click(pilot, await ready(pilot, "#opt-1"))

        # StateRootError: same degradation, and it is not an OSError.
        await until(pilot, lambda: any("failed to record DW-2" in m for m in notifications(app)))
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))

        toasts = [n for n in emitted(app) if "failed to record" in n.message]
        assert len(toasts) == 2
        assert {n.severity for n in toasts} == {"error"}
        assert "Resource deadlock avoided" in toasts[0].message
        assert "no usable state root" in toasts[1].message
        # Survived both: still running, still on the dashboard, with the whole
        # walk behind it. The `run_test` context exiting without raising is the
        # other half — an escape into the event loop surfaces there, not here.
        assert app.is_running


async def test_decision_modal_survives_a_ledger_corrupted_while_it_is_open(project):
    """The same degradation for the ledger-read fault DW-146 retyped, reproduced
    without patching anything: the modal blocks on the human, so the ledger can go
    undecodable *between* the read that found this decision pending and the write
    that records the answer.

    That fault used to arrive as a `ValueError` — a `UnicodeDecodeError` is one —
    and `_record_decision`'s tuple caught it. DW-146 retyped it to
    `deferredwork.LedgerReadError`, a plain `Exception` deliberately, which dropped
    it out of every `except OSError`/`except ValueError` in the tree including this
    one. Here that is not cosmetic: an uncaught raise in this callback escapes into
    the Textual event loop and takes the dashboard down mid-walk.

    Ablation: drop `deferredwork.LedgerReadError` from `_record_decision`'s catch
    tuple and this reddens, with the exception coming out of `run_test` instead of
    arriving as a notification.
    Byte-preservation ablation: rewrite the heading before raising LedgerReadError;
    the exact-byte assertion fails even though the status line remains open.
    """
    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "### DW-1: first thing\n\norigin: t\nlocation: a.py:1\nreason: t.\nstatus: open\n\n"
        "### DW-2: second thing\n\norigin: t\nlocation: b.py:1\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    _write_two_triage_decisions(make_run(project.project, "20260101-000000-aaaa", run_type="sweep"))

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, DecisionModal))
        # The human is looking at the modal; the ledger goes bad underneath them.
        corrupted = b"# Deferred Work\n\n### DW-1: bad \xff byte\n\nstatus: open\n"
        project.deferred_work.write_bytes(corrupted)
        await click(pilot, await ready(pilot, "#opt-1"))

        await until(pilot, lambda: any("failed to record DW-1" in m for m in notifications(app)))
        toasts = [n for n in emitted(app) if "failed to record" in n.message]
        assert toasts and toasts[0].severity == "error"
        assert "not valid UTF-8" in toasts[0].message
        # The walk carried on rather than ending on the failure...
        await until(
            pilot,
            lambda: isinstance(app.screen, DecisionModal) and app.screen._decision.id == "DW-2",
        )
        # ...and nothing was written to the ledger it could not read.
        assert project.deferred_work.read_bytes() == corrupted
        assert app.is_running


async def test_decision_modal_counts_the_answers_the_ledger_did_take(project):
    """The positive half of the same boolean (DW-198), and the row that keeps its
    negative sibling below honest: a walk whose entries are all there records both
    answers and says so. Without this, an assertion that no `decision(s)`
    notification appears would also pass for a notification that simply changed
    wording or stopped firing at all.

    Nothing is patched here either — the ledger really holds both ids, so
    `record_decision` writes both `decision:` lines and answers True twice."""
    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "### DW-1: first thing\n\norigin: t\nlocation: a.py:1\nreason: t.\nstatus: open\n\n"
        "### DW-2: second thing\n\norigin: t\nlocation: b.py:1\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    _write_two_triage_decisions(make_run(project.project, "20260101-000000-aaaa", run_type="sweep"))

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, DecisionModal))
        await click(pilot, await ready(pilot, "#opt-1"))
        await until(
            pilot,
            lambda: isinstance(app.screen, DecisionModal) and app.screen._decision.id == "DW-2",
        )
        await click(pilot, await ready(pilot, "#opt-1"))
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))

        await until(pilot, lambda: any("recorded 2 decision(s)" in m for m in notifications(app)))
        assert not any("no decision line was written" in m for m in notifications(app))
        # ...and both lines really did land on the entries the ledger still held.
        ledger = project.deferred_work.read_text(encoding="utf-8")
        assert ledger.count("decision:") == 2
        assert app.is_running


@pytest.mark.parametrize("effect", ["build", "close"])
@pytest.mark.parametrize("ledger_missing", [False, True], ids=["retired-entry", "absent-ledger"])
async def test_decision_modal_toasts_a_ledger_that_took_no_decision_line(
    project, effect, ledger_missing
):
    """The lie DW-198 removes on this surface: `apply_pre_answer` discarded
    `record_decision`'s False, so `_record_decision` returned True for a write that
    never happened and `_walk_decisions` folded it into `recorded N decision(s)` —
    the dashboard announcing closures the ledger never took.

    Nothing is patched, deliberately (the sibling rows above patch a raise; this
    hazard needs no fault at all). The modal blocks on the human, so a rival writer
    really can retire the entries between the read that found them pending and the
    click that records the answer — and `record_decision` then answers False off a
    perfectly good read, with no exception for the existing `except` arm to catch.

    Both entries are retired so the walk ends having recorded nothing, which is what
    makes the absent `recorded ... decision(s)` notification an assertion rather than
    an accident of ordering. `warning`, not `error`, and worded apart from the raise
    arm: nothing failed here — for these `build` options the pre-answer store write
    landed; the toast makes no promise about future sweep execution. Close options
    save no store answer, so their warnings must not claim one was saved.

    Ablation: hardcode `recorded=True` on `apply_pre_answer`'s `PreAnswerResult`,
    or drop `_record_decision`'s `if not result.recorded` arm, and this reddens on
    both the toast and the `recorded 2 decision(s)` that then appears.
    """
    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "### DW-1: first thing\n\norigin: t\nlocation: a.py:1\nreason: t.\nstatus: open\n\n"
        "### DW-2: second thing\n\norigin: t\nlocation: b.py:1\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    import json

    from bmad_loop import decisions

    run_dir = make_run(project.project, "20260101-000000-aaaa", run_type="sweep")
    _write_two_triage_decisions(run_dir)
    if effect == "close":
        triage_path = run_dir / "triage.json"
        triage = json.loads(triage_path.read_text(encoding="utf-8"))
        for decision in triage["decisions"]:
            decision["options"][0] = {
                "key": "1",
                "label": "Close",
                "effect": "close",
                "resolution": "superseded",
            }
        triage_path.write_text(json.dumps(triage), encoding="utf-8")

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, DecisionModal))
        # A rival writer removes both targets while the human reads the modal.
        if ledger_missing:
            project.deferred_work.unlink()
        else:
            project.deferred_work.write_text(
                "# Deferred Work\n\n"
                "### DW-9: unrelated\n\norigin: t\nlocation: c.py:1\nreason: t.\nstatus: open\n",
                encoding="utf-8",
            )
        await click(pilot, await ready(pilot, "#opt-1"))

        await until(
            pilot,
            lambda: any("DW-1: no decision line was written" in m for m in notifications(app)),
        )
        # The walk carried on rather than ending on the non-write...
        await until(
            pilot,
            lambda: isinstance(app.screen, DecisionModal) and app.screen._decision.id == "DW-2",
        )
        await click(pilot, await ready(pilot, "#opt-1"))
        await until(
            pilot,
            lambda: any("DW-2: no decision line was written" in m for m in notifications(app)),
        )
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))

        toasts = [n for n in emitted(app) if "no decision line was written" in n.message]
        assert len(toasts) == 2
        assert {n.severity for n in toasts} == {"warning"}
        # Ablation: make the saved-answer suffix unconditional in _record_decision;
        # the close variants fail because no store write occurred.
        # Independently remove apply_pre_answer's non-close store-write guard;
        # these variants fail on the empty-store assertion.
        if effect == "close":
            assert all("saved to the pre-answer store" not in n.message for n in toasts)
            assert decisions.load_pre_answers(project.project) == {}
        else:
            assert all("saved to the pre-answer store" in n.message for n in toasts)
            assert set(decisions.load_pre_answers(project.project)) == {"DW-1", "DW-2"}
        assert not any("build" in n.message for n in toasts)
        # ...and worded apart from the raise arm, which is an `error` about a fault.
        assert not any("failed to record" in m for m in notifications(app))
        # The whole point: nothing claims these two were recorded.
        assert not any("decision(s)" in m for m in notifications(app))
        assert app.is_running


@pytest.mark.parametrize(
    "cause,error",
    [
        ("target-absent", None),
        ("target-unreadable", "cannot open /tmp/[/]/[red]/decisions.json"),
        ("target-not-a-file", None),  # DW-211/228: present, wrong type, no fault text
        (
            "target-undecodable",
            "deferred-work.md is not valid UTF-8",
        ),  # DW-237: ledger decode fault
    ],
)
async def test_decision_modal_toast_carries_both_a_non_write_and_a_refusal(
    project, monkeypatch, cause, error
):
    """The COMBINED arm, which neither sibling reaches: the ledger took no
    `decision:` line AND the one operand this call did write — the pre-answer
    store, since the effect is not `close` — could not be published.

    Both facts belong in the ONE toast the walk raises for that decision. Its
    siblings each exercise a single clause (one patches nothing, so there is no
    refusal; the other keeps the entries open, so `recorded` is True and control
    takes the standalone-toast arm), which left `{unpublished}` in the non-write
    f-string deletable with `-k decision` fully green.

    Ablation: delete `{unpublished}` from `_record_decision`'s non-write
    `self.notify(...)` and this reds on the `not committed to git` clause, while
    both sibling rows still pass.
    """
    # Ablation: remove markup=False from this toast arm; the bracketed fault
    # then raises MarkupError in the real notification renderer.
    from bmad_loop import verify

    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "### DW-1: first thing\n\norigin: t\nlocation: a.py:1\nreason: t.\nstatus: open\n\n"
        "### DW-2: second thing\n\norigin: t\nlocation: b.py:1\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    _write_two_triage_decisions(make_run(project.project, "20260101-000000-aaaa", run_type="sweep"))
    monkeypatch.setattr(verify, "unpublishable_target", lambda _t, _f: (cause, error))

    detail = cause if error is None else f"{cause}: {error}"
    app = BmadLoopApp(project.project)
    async with app.run_test(notifications=True) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, DecisionModal))
        # A rival writer retires both entries while the human reads the modal, so
        # no `decision:` line lands — while the `build` answer still writes the
        # store, which is then refused publication.
        project.deferred_work.write_text(
            "# Deferred Work\n\n"
            "### DW-9: unrelated\n\norigin: t\nlocation: c.py:1\nreason: t.\nstatus: open\n",
            encoding="utf-8",
        )
        await click(pilot, await ready(pilot, "#opt-1"))
        await until(
            pilot,
            lambda: any("DW-1: no decision line was written" in m for m in notifications(app)),
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, DecisionModal) and app.screen._decision.id == "DW-2",
        )
        await click(pilot, await ready(pilot, "#opt-1"))
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))

        toasts = [n for n in emitted(app) if "DW-1" in n.message]
        assert len(toasts) == 1  # ONE toast, not the non-write one plus a second
        [toast] = toasts
        assert toast.severity == "warning"
        assert "no decision line was written to the ledger" in toast.message
        assert "your answer was saved to the pre-answer store" in toast.message
        assert f"not committed to git: decisions.json ({detail})" in toast.message
        # Neither answer was recorded, so nothing claims otherwise.
        assert not any("decision(s)" in m for m in notifications(app))
        assert app.is_running


@pytest.mark.parametrize(
    "cause,error",
    [
        ("target-absent", None),
        ("target-unreadable", "cannot open /tmp/[/]/[red]/decisions.json"),
        ("target-not-a-file", None),  # DW-211/228: present, wrong type, no fault text
        (
            "target-undecodable",
            "deferred-work.md is not valid UTF-8",
        ),  # DW-237: ledger decode fault
    ],
)
async def test_decision_modal_toasts_an_answer_it_could_not_publish(
    project, monkeypatch, cause, error
):
    """DW-209/213 on this surface. `apply_pre_answer` commits only the operands the
    call actually wrote, so a publishable-target refusal means an answer that really
    landed on disk went unpublished — its own `warning` toast, orthogonal to the
    non-write one.

    What it must NOT change is the count: the ledger line landed, so the answer is
    answered, and `recorded 2 decision(s)` still fires. The walk advances the same
    way, and nothing reads as an `error`.

    Ablation: drop `_record_decision`'s `if note is not None:` arm and this reddens
    on the toast while `recorded 2 decision(s)` still passes; hand the refusal to
    the `recorded` boolean instead and it reddens on the count.
    """
    # Ablation: remove markup=False from this toast arm; the bracketed fault
    # then raises MarkupError in the real notification renderer.
    from bmad_loop import verify

    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "### DW-1: first thing\n\norigin: t\nlocation: a.py:1\nreason: t.\nstatus: open\n\n"
        "### DW-2: second thing\n\norigin: t\nlocation: b.py:1\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    _write_two_triage_decisions(make_run(project.project, "20260101-000000-aaaa", run_type="sweep"))
    # The race the guard exists for: the written operands go unpublishable between
    # the write and the staging.
    monkeypatch.setattr(verify, "unpublishable_target", lambda _t, _f: (cause, error))

    detail = cause if error is None else f"{cause}: {error}"
    app = BmadLoopApp(project.project)
    async with app.run_test(notifications=True) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, DecisionModal))
        await click(pilot, await ready(pilot, "#opt-1"))
        await until(
            pilot,
            lambda: any("DW-1: not committed to git" in m for m in notifications(app)),
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, DecisionModal) and app.screen._decision.id == "DW-2",
        )
        await click(pilot, await ready(pilot, "#opt-1"))
        # Wait for DW-2's toast as the sibling no-decision-line row waits for its
        # own, not merely for the dashboard: `Screen.dismiss` swaps `app.screen`
        # and hands the result callback to `call_next`, so the modal is gone one
        # message before `_record_decision` runs for DW-2 — a window the Windows
        # runners hit (`assert 1 == 2`, DW-1's toast alone).
        await until(
            pilot,
            lambda: any("DW-2: not committed to git" in m for m in notifications(app)),
        )
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))

        toasts = [n for n in emitted(app) if "not committed to git" in n.message]
        assert len(toasts) == 2
        assert {n.severity for n in toasts} == {"warning"}
        assert all(f"deferred-work.md ({detail})" in n.message for n in toasts)
        # The line DID land, so the answer is answered: the refusal changes neither
        # the count nor the walk, and it is not the non-write toast.
        await until(pilot, lambda: any("recorded 2 decision(s)" in m for m in notifications(app)))
        assert not any("no decision line was written" in m for m in notifications(app))
        assert not any("failed to record" in m for m in notifications(app))
        assert app.is_running


async def test_decision_modal_toasts_an_answer_git_could_not_commit(project, monkeypatch):
    """DW-226 on this surface — the mirror of the refusal row beside it, for the
    OTHER unpublished lane. `verify.commit_paths` raises `GitError` (a gitignored
    operand, a parent in no repository, git absent), which used to be swallowed
    silently, so an answer written to disk and missing from git history reached
    neither the CLI nor this dashboard.

    What it must NOT change is the count: the ledger line landed, so the answer is
    answered and `recorded 2 decision(s)` still fires. The walk advances the same
    way, and nothing reads as an `error` — a failed publish is a degrade, not a
    fault of the recording.

    Ablation: drop `_record_decision`'s `if note is not None:` arm and this reds on
    the toast while `recorded 2 decision(s)` still passes; hand the failure to the
    `recorded` boolean instead and it reds on the count; restore
    `except verify.GitError: pass` in `apply_pre_answer` and it reds on both toasts.
    """
    # Ablation: remove markup=False from this toast arm; the bracketed git error
    # then raises MarkupError in the real notification renderer — git's own stderr
    # is where the brackets come from, so this is the arm that needs it most.
    from bmad_loop import verify

    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "### DW-1: first thing\n\norigin: t\nlocation: a.py:1\nreason: t.\nstatus: open\n\n"
        "### DW-2: second thing\n\norigin: t\nlocation: b.py:1\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    _write_two_triage_decisions(make_run(project.project, "20260101-000000-aaaa", run_type="sweep"))
    # Multi-line, as git's stderr is: `publish_note` renders on ONE line, so the
    # collapse is part of what this row grades.
    error = "git add failed:\n  The following paths are ignored by [red]one[/red] of your .gitignore files"

    def boom(*_a, **_k):
        raise verify.GitError(error)

    monkeypatch.setattr(verify, "commit_paths", boom)
    collapsed = " ".join(error.split())

    app = BmadLoopApp(project.project)
    async with app.run_test(notifications=True) as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, DecisionModal))
        await click(pilot, await ready(pilot, "#opt-1"))
        await until(
            pilot,
            lambda: any("DW-1: not committed to git" in m for m in notifications(app)),
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, DecisionModal) and app.screen._decision.id == "DW-2",
        )
        await click(pilot, await ready(pilot, "#opt-1"))
        # DW-2's toast, not merely the dashboard: see the sibling row above for
        # the `dismiss`/`call_next` window the Windows runners hit.
        await until(
            pilot,
            lambda: any("DW-2: not committed to git" in m for m in notifications(app)),
        )
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))

        toasts = [n for n in emitted(app) if "not committed to git" in n.message]
        assert len(toasts) == 2
        assert {n.severity for n in toasts} == {"warning"}
        assert all(
            f"deferred-work.md (commit-unavailable: {collapsed})" in n.message for n in toasts
        )
        assert all(f"decisions.json (commit-unavailable: {collapsed})" in n.message for n in toasts)
        assert not any("\n" in n.message for n in toasts)  # one line at this surface
        # The line DID land, so the answer is answered: the failure changes neither
        # the count nor the walk, and it is not the non-write toast.
        await until(pilot, lambda: any("recorded 2 decision(s)" in m for m in notifications(app)))
        assert not any("no decision line was written" in m for m in notifications(app))
        assert not any("failed to record" in m for m in notifications(app))
        assert app.is_running


async def test_decision_walk_counts_only_the_answer_the_ledger_took(project):
    """A mixed walk records DW-1, then retires DW-2 before its answer is written.

    The zero-of-two sibling catches unconditional counting. This row additionally
    requires a partial-success notification to retain the one recorded answer.

    Ablation: hardcode `recorded=True` on `apply_pre_answer`'s `PreAnswerResult`,
    or drop `_record_decision`'s `if not result.recorded` arm, and this reddens on
    `recorded 2 decision(s)`.
    """
    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "### DW-1: first thing\n\norigin: t\nlocation: a.py:1\nreason: t.\nstatus: open\n\n"
        "### DW-2: second thing\n\norigin: t\nlocation: b.py:1\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    _write_two_triage_decisions(make_run(project.project, "20260101-000000-aaaa", run_type="sweep"))

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("d")
        # DW-1 comes first and records against an intact ledger: this one counts.
        await until(
            pilot,
            lambda: isinstance(app.screen, DecisionModal) and app.screen._decision.id == "DW-1",
        )
        await click(pilot, await ready(pilot, "#opt-1"))

        # ...then a rival writer retires DW-2 while its modal is up: this one must not.
        await until(
            pilot,
            lambda: isinstance(app.screen, DecisionModal) and app.screen._decision.id == "DW-2",
        )
        project.deferred_work.write_text(
            "# Deferred Work\n\n"
            "### DW-1: first thing\n\norigin: t\nlocation: a.py:1\nreason: t.\n"
            "status: open\n\ndecision: 2026-06-13 Widen — widen it\n",
            encoding="utf-8",
        )
        await click(pilot, await ready(pilot, "#opt-1"))

        await until(
            pilot,
            lambda: any("DW-2: no decision line was written" in m for m in notifications(app)),
        )
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))

        # ONE of the two, not both and not neither.
        await until(pilot, lambda: any("recorded 1 decision(s)" in m for m in notifications(app)))
        assert not any("recorded 2 decision(s)" in m for m in notifications(app))
        assert not any("DW-1: no decision line was written" in m for m in notifications(app))
        assert app.is_running


async def test_gate_unreadable_spec_refuses_approve_and_resume(project_tree, monkeypatch):
    """The GATE arm of the same refusal — its sibling row grades plan-checkpoint only.

    `_review_gate` and `_review_plan_checkpoint` both build a `SpecReviewModal` and both
    forward `unreadable=not readable`, but the verbs differ: the checkpoint offers
    `#act-approve`/`#act-replan` and the gate offers `#act-resume`. Only the checkpoint
    pair was pinned, so `unreadable=` could be dropped from `_review_gate` with
    `tests/test_tui_app.py` fully green — and `Approve & resume` at a spec-approval gate
    is the verb that carries the run PAST the gate whose only purpose is a human reading
    that file.

    Ablation: pass `unreadable=False` in `_review_gate` and this reddens on the button
    state.
    """
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_VERIFY)
    task.spec_file = str(project_tree.project / "gone" / "spec-1-1-a.md")
    make_run(
        project_tree.project,
        "20260611-100000-aaaa",
        paused_stage="spec-approval",
        paused_reason="awaiting spec approval",
        paused_story_key="1-1-a",
        tasks={"1-1-a": task},
    )
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        body = render(app.screen.query_one("#spec Static", Static).content)
        assert "could not be read" in body
        assert app.screen.query_one("#act-resume", Button).disabled


async def test_escalation_unreadable_spec_refuses_rearm_but_keeps_resolve(
    project_tree, monkeypatch
):
    """The escalation modal discarded the read verdict entirely.

    `_review_escalation` bound `_readable` and dropped it, so an unreadable spec reached
    `_blocking_condition` — a `find("## Auto Run Result")` that answers "" for the read-
    failure sentence exactly as it does for any spec without a halt block. The modal
    then rendered "(no blocking condition recorded)", BYTE-IDENTICAL to a spec that was
    read fine and simply halted without one, while `Re-arm & resume` stayed live. Re-arm
    flips the spec's frontmatter, strips its `## Auto Run Result` and re-stamps the
    baseline, so that is a destructive write driven from a modal reporting evidence
    nobody could read.

    The refusal is asymmetric, and deliberately so. `Re-arm` is refused: it flips the
    spec's frontmatter, strips its result and re-stamps the baseline. `Resolve` is NOT —
    it opens an interactive agent and writes nothing itself, it is precisely what repairs
    a bad anchor, and gating it left `close` as the modal's only action while the `R`
    binding (`action_resolve_run`, which has no readability check) reached the same agent
    anyway, making the refusal advisory rather than enforced.

    Ablation: drop `unreadable=` from `_review_escalation`'s `EscalationModal(...)` and
    this reddens on the notice, the re-arm button and the hint.
    """
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _run_dir, spec = _stories_paused_run(project_tree.project, stage="escalation")
    spec.unlink()  # absent at the anchored path

    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        rendered = " ".join(
            render(s.content) for s in app.screen.query("#blocking Static").results(Static)
        )
        # the distinguishing claim: unknown, NOT absent
        assert "could not be read" in rendered
        # and the lie is GONE, not merely outvoted by a warning above it. The unreadable
        # arm used to prepend its notice and then fall through to the shared body render,
        # which answers "" for the failure sentence — so the modal showed the warning and
        # "(no blocking condition recorded)" together, the second denying the first.
        assert "no blocking condition recorded" not in rendered
        assert app.screen.query_one("#act-rearm", Button).disabled
        # Resolve stays OPEN — the non-destructive remedy for the failure on screen
        assert not app.screen.query_one("#act-resolve", Button).disabled
        # The hint explains THIS refusal. Unasserted, it could silently revert to the
        # restore-latch or "re-arm unlocks once..." text — both of which explain a
        # condition that is not why the button is dark — while the button state stayed
        # green.
        hint = render(app.screen.query_one("#hint", Static).content)
        assert "unreadable" in hint
        assert "bmad-loop resolve" in hint  # the CLI fallback is named, not just refused


async def test_replan_on_a_spec_that_vanished_after_render_names_the_anchored_path(
    project_tree, monkeypatch
):
    """`_do_replan`'s absent-spec branch, which no row reached.

    The branch is narrow by construction — the same absence that produces it also
    disables `#act-replan`, so only a spec deleted BETWEEN render and click gets here —
    but it is the arm that distinguishes "absent at the anchor" from "present with no
    frontmatter status", and `reset_spec_status` answers False to both. Driven directly
    because the TOCTOU window cannot be opened through the modal.

    Ablation: delete the `is_file()` branch and this reddens — the shared "could not
    reset the plan to draft" notice takes over and never names the path consulted.
    """
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    run_dir, spec = _stories_paused_run(project_tree.project, stage="plan-checkpoint")
    run_id = run_dir.name
    spec.unlink()  # vanished after the modal rendered

    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        app._do_replan(run_id, spec, project_tree.project)
        await pilot.pause()
        assert any(f"no spec at {spec}" in m for m in notifications(app))
        assert not any("could not reset" in m for m in notifications(app))


@pytest.mark.parametrize("key", ["R", "p"], ids=["resolve", "review"])
async def test_resolve_on_a_deferred_pause_points_at_cli_reverify(project_tree, monkeypatch, key):
    """DW-522: a run paused for manual recovery on a DEFERRED story has no
    escalation to resolve — both the resolve verb and the pause viewer notify the
    CLI's `resolve --reverify` / `resume` choice instead of launching the agent or
    opening the escalation modal. Ablation, performed: drop the
    `_deferred_pause_notified` check from either path and its row fails."""
    launched: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(launch, "start_resolve_detached", lambda proj, rid: launched.append(rid))
    run_id = "20260611-100000-aaaa"
    make_run(
        project_tree.project,
        run_id,
        paused_stage="escalation",
        paused_reason="ACTION REQUIRED — manual recovery",
        paused_story_key="1-1-a",
        tasks={"1-1-a": StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEFERRED)},
    )
    hint = f"bmad-loop resolve {run_id} --reverify"
    app = BmadLoopApp(project_tree.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press(key)
        await until(pilot, lambda: any(hint in m for m in notifications(app)))
        assert isinstance(app.screen, DashboardScreen)
    assert launched == []
    [message] = [m for m in notifications(app) if hint in m]
    assert "1-1-a was deferred, not escalated" in message
    assert f"bmad-loop resume {run_id}" in message
