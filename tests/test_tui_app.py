"""Coarse Pilot smoke tests for the dashboard and run control. Fine-grained
data correctness lives in test_tui_data.py, exact launch argv in
test_tui_launch.py; here we only prove the wiring: app mounts, the run table
populates and auto-selects the newest run, selection switches the task table,
the journal pane picks up appended events on a poll, and the r/s/e/a/v
bindings drive modals into tui.launch calls (monkeypatched — no real tmux)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import install_bmad_config, write_sprint
from rich.console import Console
from rich.text import Text
from textual.geometry import Offset
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

from bmad_loop.journal import Journal, save_state
from bmad_loop.model import Phase, RunState, StoryTask, TokenUsage
from bmad_loop.runs import RUNS_DIR
from bmad_loop.tui import data, launch
from bmad_loop.tui.app import BmadLoopApp
from bmad_loop.tui.screens.dashboard import DashboardScreen, _Snapshot
from bmad_loop.tui.screens.modals import (
    ConfirmModal,
    ConfirmResumeModal,
    DecisionModal,
    DeferredEntryModal,
    EscalationModal,
    SpecReviewModal,
    StartRunModal,
    StartSweepModal,
    StoryCheckpointModal,
    TextOutputModal,
)
from bmad_loop.tui.widgets import (
    _JOURNAL_CLOCK_WIDTH,
    _JOURNAL_COL_PAD,
    _JOURNAL_KIND_WIDTH,
    RunHeader,
    SelectableRichLog,
    SprintTree,
    StoriesTable,
    journal_line,
    pause_label,
    pause_tag,
    story_checkpoint_cell,
    story_state_cell,
)


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


def notifications(app: BmadLoopApp) -> list[str]:
    return [n.message for n in app._notifications]


async def until(pilot, condition, timeout: float = 10.0) -> None:
    """Wait for a predicate across thread-worker polls and their callbacks.

    The dashboard polls on a 1.0s interval and each tick hops through a thread
    worker and a UI callback, so several sequential waits can each need a few
    ticks; the timeout is generous and returns the instant the predicate holds.
    Genuine stalls (an exclusive poll worker repeatedly superseded under heavy
    CI IO before it applies a log jump) are handled by @pytest.mark.flaky on the
    affected smoke tests, which re-rolls the race — see the journal-jump tests."""
    waited = 0.0
    while not condition():
        if waited >= timeout:
            raise AssertionError("condition not met before timeout")
        await pilot.pause(0.05)
        waited += 0.05


async def ready(pilot, selector: str):
    """Wait until a modal widget is mounted *and* laid out on-screen, then return it.

    A screen-type `until` returns the instant push_screen swaps app.screen — before
    the modal's children mount (query NoMatches) or receive a layout region (click
    OutOfBounds, region still 0). Gating on a real on-screen region makes the
    following query_one / click / value-set safe on slow CI runners. A modal's
    widgets mount and lay out together, so one gate covers every field in it."""

    def _hit():
        hits = pilot.app.screen.query(selector)
        node = hits.first() if hits else None
        return node if node is not None and node.region.area > 0 else None

    await until(pilot, lambda: _hit() is not None)
    return _hit()


def dashboard(app: BmadLoopApp) -> DashboardScreen:
    assert isinstance(app.screen, DashboardScreen)
    return app.screen


async def test_empty_project_shows_hint(project):
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        assert screen.query_one("#runs", DataTable).row_count == 0
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "no runs found" in header


async def test_run_table_populates_and_selects_newest(project):
    root = project.project
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


async def test_task_table_shows_weighted_and_raw_tokens(project):
    root = project.project
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


async def test_zero_weighted_tokens_shows_zero_not_dash(project):
    """With cache_read_weight=0 a cache-read-only task has weighted==0 but nonzero raw.
    The tokens cell must render "0" (a real value), not "-" — which reads as missing
    data. "-" is reserved for a task with no tokens at all."""
    root = project.project
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


async def test_apply_snapshot_after_unmount_is_noop(project):
    """A poll worker hands its snapshot to `_apply` via `call_from_thread`; that call
    can land after the screen is unmounted (app shutdown / another screen popped at
    teardown), when the widgets it queries are gone. Applying to an unmounted screen
    must be a no-op, not a `NoMatches` crash on '#runs' — the flake seen when a
    settings screen is open as the app tears down."""
    root = project.project
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


async def test_token_weight_falls_back_to_default(project):
    root = project.project
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


async def test_journal_pane_updates_after_poll(project):
    root = project.project
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


async def test_log_pane_shows_emulated_content(project):
    from test_tui_data import ink_stream

    root = project.project
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


async def test_selectable_rich_log_get_selection(project):
    app = BmadLoopApp(project.project)
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


async def test_copy_pane_action_copies_log(project, monkeypatch):
    copied: list[str] = []
    app = BmadLoopApp(project.project)
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


async def test_copy_pane_wrong_tab_notifies(project):
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        assert screen.query_one("#tabs", TabbedContent).active == "tab-journal"  # default
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("Log or Attention tab" in m for m in notifications(app)),
        )


async def test_copy_pane_empty_notifies(project):
    app = BmadLoopApp(project.project)
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


@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_journal_enter_jumps_to_log_position(project):
    root = project.project
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


async def test_journal_enter_without_position_notifies(project):
    root = project.project
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


@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_journal_jump_pins_other_sessions_log(project):
    root = project.project
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


async def test_journal_jump_near_tail_does_not_chase_growing_log(project):
    # Regression for "pressing enter keeps sending me to the bottom": jumping to
    # an entry near the end lands the view at the tail, and the old code then
    # inferred "follow the tail" from that, dragging the view down on every poll
    # as the live log grew. A jump must anchor the position until esc is pressed.
    root = project.project
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
        await until(pilot, lambda: log.is_vertical_scroll_end)  # landed at the tail
        anchored, base_max = log.scroll_y, log.max_scroll_y
        # the live session keeps writing; a poll repaints the pane
        with (run_dir / "logs" / "story-1.log").open("ab") as f:
            for i in range(200, 260):
                f.write(f"row {i:03d}\r\n".encode())
        screen._tick(force_rescan=False)
        await until(pilot, lambda: log.max_scroll_y > base_max)  # new lines rendered
        assert round(log.scroll_y) == round(anchored)  # stayed put, did not chase the tail
        assert log.scroll_y < log.max_scroll_y


async def test_poll_skips_while_another_holds_the_lock(project):
    # Regression: exclusive=True cannot stop a running thread worker, so the
    # screen lock must make a second poll bail instead of mutating shared ctx
    # (two threads feeding ctx.log's pyte stream crashed the TUI).
    root = project.project
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
            before = list(ctx.entries)
            journal.append("checkpoint", log_task="story-1", log_pos=0)  # new entry on disk
            worker = screen._poll(ctx, screen._generation, False, None)
            await worker.wait()
            assert ctx.entries == before  # guarded body never ran
        finally:
            screen._poll_lock.release()


# ----------------------------------------------------------- sprint tree pane


async def test_sprint_tree_populates(project):
    install_bmad_config(project)
    write_sprint(
        project,
        {
            "epic-1": "in-progress",
            "1-1-auth": "done",
            "1-2-search": "backlog",
            "epic-1-retrospective": "optional",
            "epic-2": "backlog",
            "2-1-billing": "backlog",
        },
    )
    app = BmadLoopApp(project.project)
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


async def test_sprint_tree_preserves_expansion_across_refresh(project):
    install_bmad_config(project)
    write_sprint(project, {"epic-1": "in-progress", "1-1-auth": "in-progress"})
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        tree = screen.query_one("#sprint-tree", SprintTree)
        # wait past the initial placeholder for the real epic node
        await until(pilot, lambda: "Epic 1" in str(tree.root.children[0].label))
        node = tree.root.children[0]
        node.expand()
        write_sprint(project, {"epic-1": "in-progress", "1-1-auth": "done"})
        screen._tick(force_rescan=True)

        def story_checked() -> bool:
            children = tree.root.children[0].children
            return bool(children) and "✓" in str(children[0].label)

        await until(pilot, story_checked)
        assert tree.root.children[0] is node  # reconciled in place, not rebuilt
        assert node.is_expanded


async def test_sprint_tree_forgives_malformed_yaml(project):
    install_bmad_config(project)
    project.sprint_status.write_text("{ not valid yaml [")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        tree = screen.query_one("#sprint-tree", SprintTree)
        await pilot.pause(0.2)
        assert "sprint status unavailable" in str(tree.root.children[0].label)
        # the app keeps polling and recovers once the file is fixed
        write_sprint(project, {"epic-1": "backlog", "1-1-auth": "backlog"})
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


async def test_deferred_pane_lists_and_opens_modal(project):
    install_bmad_config(project)
    project.deferred_work.write_text(_LEDGER, encoding="utf-8")
    app = BmadLoopApp(project.project)
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


async def test_deferred_pane_preserves_highlight_across_refresh(project):
    install_bmad_config(project)
    project.deferred_work.write_text(_LEDGER, encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        deferred = screen.query_one("#deferred", OptionList)
        await until(pilot, lambda: deferred.option_count == 2)
        deferred.highlighted = 1  # DW-2
        project.deferred_work.write_text(
            _LEDGER.replace("status: open", "status: done 2026-06-12"), encoding="utf-8"
        )
        screen._tick(force_rescan=True)
        await until(pilot, lambda: "DW-1 ✓" in deferred_rows(deferred)[0])
        assert deferred.get_option_at_index(deferred.highlighted).id == "DW-2"


async def test_deferred_pane_shows_legacy_items(project):
    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "## Deferred from: epic 1 review (2026-04-06)\n\n"
        "- ~~**Old fixed thing** — was broken, then repaired~~ → fixed in 1.3\n"
        "- **Open legacy thing here** — still pending. [MAJOR]\n\n" + _LEDGER.split("\n\n", 1)[1],
        encoding="utf-8",
    )
    app = BmadLoopApp(project.project)
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


async def test_deferred_pane_placeholder_without_ledger(project):
    install_bmad_config(project)
    app = BmadLoopApp(project.project)
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
        await pilot.click(await ready(pilot, "#opt-1"))  # choose build
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
    assert decisions.load_pre_answers(project.project)["DW-1"]["effect"] == "build"


async def test_answer_decisions_none_notifies(project):
    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n### DW-1: done thing\n\norigin: t\nstatus: done 2026-06-01\n",
        encoding="utf-8",
    )
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("d")
        await until(pilot, lambda: any("no unanswered decisions" in m for m in notifications(app)))


def test_cli_tui_hint_without_textual(project, monkeypatch, capsys):
    """`bmad-loop tui` prints the install hint when the extra is missing."""
    import builtins

    from bmad_loop import cli

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.partition(".")[0] == "textual":
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(__import__("sys").modules, "bmad_loop.tui.app", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    rc = cli.main(["tui", "--project", str(project.project)])
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


# ------------------------------------------------------------- run control


async def test_start_run_modal_escape_cancels(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        assert not calls


async def test_start_run_modal_launches(project, monkeypatch):
    calls = {}
    monkeypatch.setattr(launch, "tmux_available", lambda: True)

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
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    (project.project / "src.txt").write_text("dirty\n")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: any("not clean" in m for m in notifications(app)))
        assert not calls


async def test_live_run_asks_for_confirmation(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    make_run(project.project, "20260611-100000-aaaa", alive=True)  # our pid: running
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.click(await ready(pilot, "#ok"))
        await until(
            pilot,
            lambda: (
                isinstance(app.screen, ConfirmModal)
                and not isinstance(app.screen, ConfirmResumeModal)
            ),
        )
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: bool(calls))


async def test_unknown_pid_run_asks_for_confirmation(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
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
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
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
    monkeypatch.setattr(launch, "tmux_available", lambda: True)

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


async def test_dry_run_shows_captured_output(project, monkeypatch):
    seen = {}
    monkeypatch.setattr(launch, "tmux_available", lambda: True)

    def fake_captured(tail):
        seen["tail"] = tail
        return 0, "would process 2 stories\n"

    monkeypatch.setattr(launch, "run_captured", fake_captured)
    app = BmadLoopApp(project.project)
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
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))


async def test_validate_shows_output_modal(project, monkeypatch):
    monkeypatch.setattr(launch, "run_captured", lambda tail: (1, "FAIL: no policy\n"))
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, TextOutputModal))
        await ready(pilot, "Label")  # body mounts a tick after the screen swaps
        labels = app.screen.query("Label")
        assert any("exit 1" in str(label.content) for label in labels)


async def test_resume_confirm_launches(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    make_run(
        project.project,
        "20260611-100000-aaaa",
        paused_stage="DEV_VERIFY",
        paused_reason="verify failed",
    )
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


async def test_resume_unknown_pid_warns(project, monkeypatch):
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "unknown")
    run_dir = make_run(
        project.project,
        "20260611-100000-aaaa",
        paused_stage="DEV_VERIFY",
        paused_reason="verify failed",
    )
    (run_dir / "engine.pid").write_text("4242 123.0", encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        assert "may still be live" in app.screen._warning


async def test_delete_unknown_pid_warns_but_does_not_block(project, monkeypatch):
    # 'unknown' liveness (a live-but-unreadable pid) must not block cleanup — the
    # deliberate runs.engine_alive invariant — but the irreversible delete confirm
    # must warn the run may still be live rather than imply it is safely dead.
    monkeypatch.setattr(data, "liveness", lambda run_dir: "unknown")
    run_dir = make_run(project.project, "20260611-100000-aaaa")
    (run_dir / "engine.pid").write_text("4242 123.0", encoding="utf-8")
    app = BmadLoopApp(project.project)
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

    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(runs, "prune_sessions", lambda _p: (["odd-1"], [], {"odd-1"}))
    monkeypatch.setattr(launch, "prune_ctl_windows", lambda _p: [])
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: any("unverifiable engine pid" in m for m in notifications(app)))
        assert any("removed 1 session(s)" in m for m in notifications(app))


async def test_resume_finished_run_refused(project, monkeypatch):
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    make_run(project.project, "20260611-100000-aaaa", finished=True)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("e")
        await until(pilot, lambda: any("already finished" in m for m in notifications(app)))
        assert isinstance(app.screen, DashboardScreen)


async def test_attach_without_tmux_notifies(project, monkeypatch):
    monkeypatch.setattr(launch, "tmux_available", lambda: False)
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("a")
        await until(pilot, lambda: any("tmux not found" in m for m in notifications(app)))


async def test_attach_without_agent_session_notifies(project, monkeypatch):
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: False)
    monkeypatch.setattr(launch, "ctl_window", lambda run_id: None)
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("a")
        await until(pilot, lambda: any("no live agent session" in m for m in notifications(app)))


# ------------------------------------------------------- sweep decision flow


async def test_decision_banner_shows_and_clears(project):
    run_dir = make_run(project.project, "20260611-100000-aaaa", run_type="sweep", alive=True)
    journal = Journal(run_dir)
    journal.append("sweep-start")
    journal.append("decision-pending", dw_id="DW-7", question="reopen the cache work?")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.decision_pending is not None)
        assert screen.decision_pending == ("DW-7", "reopen the cache work?")
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "decision needed: DW-7" in header
        assert "press a to attach and answer" in header
        # the toast is posted via self.notify() onto textual's async message pump,
        # so it lands in app._notifications a tick after _decision is set — wait
        # for it rather than asserting synchronously (matches the other notify tests)
        await until(pilot, lambda: any("reopen the cache work?" in m for m in notifications(app)))

        journal.append("decision-answered", dw_id="DW-7", key="a", effect="build")
        await until(pilot, lambda: screen.decision_pending is None)
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "decision needed" not in header


async def test_decision_footer_suppressed_for_crashed(project):
    # a crashed run tore its tmux session down, so the "press a to attach and
    # answer" hint would point at a dead session — suppress it even when a
    # decision is pending.
    run_dir = make_run(
        project.project,
        "20260611-100000-aaaa",
        crashed=True,
        crash_error="RuntimeError: boom",
    )
    journal = Journal(run_dir)
    journal.append("decision-pending", dw_id="DW-7", question="reopen the cache work?")
    app = BmadLoopApp(project.project)
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
    TUI pane id and capture return-pane stamps so no real tmux is touched and
    tests can assert which ctl window gets the switch-back target recorded."""
    calls: list[list[str]] = []
    stamps: list[tuple[str, str]] = []
    monkeypatch.setenv("TMUX", "/tmp/fake-tmux,1,0")
    monkeypatch.setattr(
        "bmad_loop.tui.app.subprocess.call", lambda argv: calls.append(list(argv)) or 0
    )
    monkeypatch.setattr(launch, "current_pane_id", lambda: "%9")
    monkeypatch.setattr(launch, "set_return_pane", lambda w, p: stamps.append((w, p)))
    return calls, stamps


async def test_attach_targets_ctl_window_when_decision_pending(project, monkeypatch):
    run_dir = make_run(project.project, "20260611-100000-aaaa", run_type="sweep", alive=True)
    Journal(run_dir).append("decision-pending", dw_id="DW-7", question="q?")
    selected: list[str] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: True)  # agent up too
    monkeypatch.setattr(launch, "ctl_window", lambda run_id: f"sweep-{run_id}")
    monkeypatch.setattr(launch, "select_ctl_window", lambda w: selected.append(w))
    calls, stamps = _patch_attach_exec(monkeypatch)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).decision_pending is not None)
        await pilot.press("a")
        await until(pilot, lambda: bool(calls))
    assert selected == ["sweep-20260611-100000-aaaa"]
    assert calls == [["tmux", "switch-client", "-t", "=bmad-loop-ctl"]]
    # the ctl window is stamped with our pane so it switches us back on exit
    assert stamps == [("=bmad-loop-ctl:sweep-20260611-100000-aaaa", "%9")]


async def test_attach_outside_tmux_stamps_detach(project, monkeypatch):
    # No TMUX: a throwaway client attaches under suspend, so the ctl window is
    # stamped to detach it on exit (returning to the suspended TUI) rather than
    # switch-client back to a pane we do not have.
    run_dir = make_run(project.project, "20260611-100000-aaaa", run_type="sweep", alive=True)
    Journal(run_dir).append("decision-pending", dw_id="DW-7", question="q?")
    monkeypatch.delenv("TMUX", raising=False)
    stamps: list[tuple[str, str]] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: True)
    monkeypatch.setattr(launch, "ctl_window", lambda run_id: f"sweep-{run_id}")
    monkeypatch.setattr(launch, "select_ctl_window", lambda w: None)
    monkeypatch.setattr(launch, "set_return_pane", lambda w, p: stamps.append((w, p)))
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).decision_pending is not None)
        await pilot.press("a")
        await until(pilot, lambda: bool(stamps))
    assert stamps == [("=bmad-loop-ctl:sweep-20260611-100000-aaaa", "detach")]


async def test_attach_prefers_agent_session_without_decision(project, monkeypatch):
    make_run(project.project, "20260611-100000-aaaa", alive=True)
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: True)
    monkeypatch.setattr(launch, "ctl_window", lambda run_id: f"run-{run_id}")
    calls, stamps = _patch_attach_exec(monkeypatch)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("a")
        await until(pilot, lambda: bool(calls))
    assert calls == [["tmux", "switch-client", "-t", "=bmad-loop-20260611-100000-aaaa"]]
    # attaching to a live agent session is not our parked window — nothing stamped
    assert stamps == []


async def test_attach_falls_back_to_ctl_window(project, monkeypatch):
    make_run(project.project, "20260611-100000-aaaa", alive=True)
    selected: list[str] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: False)
    monkeypatch.setattr(launch, "ctl_window", lambda run_id: f"run-{run_id}")
    monkeypatch.setattr(launch, "select_ctl_window", lambda w: selected.append(w))
    calls, stamps = _patch_attach_exec(monkeypatch)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("a")
        await until(pilot, lambda: bool(calls))
    assert selected == ["run-20260611-100000-aaaa"]
    assert calls == [["tmux", "switch-client", "-t", "=bmad-loop-ctl"]]
    assert stamps == [("=bmad-loop-ctl:run-20260611-100000-aaaa", "%9")]


async def test_resolve_escalation_launches_and_attaches(project, monkeypatch):
    launched: list[str] = []
    selected: list[str] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")

    def fake_start_resolve(proj, rid):
        launched.append(rid)
        return "@7"

    monkeypatch.setattr(launch, "start_resolve_detached", fake_start_resolve)
    monkeypatch.setattr(launch, "select_ctl_window_id", lambda w: selected.append(w))
    calls, stamps = _patch_attach_exec(monkeypatch)
    make_run(
        project.project,
        "20260611-100000-aaaa",
        paused_stage="escalation",
        paused_reason="CRITICAL escalation",
    )
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("R")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: bool(calls))
    assert launched == ["20260611-100000-aaaa"]
    assert selected == ["@7"]
    assert calls == [["tmux", "switch-client", "-t", "=bmad-loop-ctl"]]
    # resolve runs in the freshly launched ctl window (@7) — stamp it to return
    assert stamps == [("@7", "%9")]


async def test_resolve_unknown_pid_refused(project, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "unknown")
    monkeypatch.setattr(launch, "start_resolve_detached", lambda proj, rid: launched.append(rid))
    run_dir = make_run(
        project.project,
        "20260611-100000-aaaa",
        paused_stage="escalation",
        paused_reason="CRITICAL escalation",
    )
    (run_dir / "engine.pid").write_text("4242 123.0", encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("R")
        await until(pilot, lambda: any("may still be live" in m for m in notifications(app)))
    assert launched == []


async def test_resolve_refused_when_not_escalation(project, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(launch, "start_resolve_detached", lambda proj, rid: launched.append(rid))
    make_run(
        project.project,
        "20260611-100000-aaaa",
        paused_stage="spec-approval",
        paused_reason="awaiting approval",
    )
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("R")
        await until(pilot, lambda: any("escalation" in m for m in notifications(app)))
    assert launched == []  # warned, never launched


# ------------------------------------------------- stories mode: board + badges


def test_pause_tag_and_label_render():
    assert pause_tag("plan-checkpoint").plain == "plan"
    assert pause_tag("story-checkpoint").plain == "story"
    assert pause_tag("escalation").plain == "esc"
    assert pause_tag("").plain == ""  # not paused → no tag
    label, style = pause_label("escalation")
    assert label == "escalation" and "red" in style


def test_story_cells_render():
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


async def test_stories_mode_run_shows_board_and_attention(project):
    root = project.project
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


async def test_sprint_mode_run_keeps_sprint_tree(project):
    root = project.project
    install_bmad_config(project)
    write_sprint(project, {"epic-1": "in-progress", "1-1-a": "ready-for-dev"})
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
    story_key: str = "1",
    spec_status: str = "ready-for-dev",
    spec_checkpoint: bool = True,
    done_checkpoint: bool = False,
    commit_sha: str = "",
    review_cycle: int = 0,
    blocked_result: str = "",
    sentinel: bool = False,
) -> tuple[Path, Path]:
    """A stories-mode run paused at `stage`, with the id-keyed story spec on disk
    and a StoryTask pointing at it. Returns (run_dir, spec_path)."""
    import yaml

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
    task = StoryTask(story_key=story_key, epic=0, phase=Phase.DEV_VERIFY)
    task.spec_file = str(spec)
    task.review_cycle = review_cycle
    if commit_sha:
        task.commit_sha = commit_sha
    run_dir = make_run(
        root,
        "20260611-100000-aaaa",
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


async def test_plan_checkpoint_approve_resumes(project, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _stories_paused_run(project.project, stage="plan-checkpoint")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await pilot.click(await ready(pilot, "#act-approve"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


async def test_plan_checkpoint_replan_resets_and_resumes(project, monkeypatch):
    from bmad_loop import devcontract

    calls: list[str] = []
    resets: list[tuple] = []
    strips: list[Path] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        devcontract, "reset_spec_status", lambda p, s: resets.append((p, s)) or True
    )
    monkeypatch.setattr(devcontract, "strip_auto_run_result", lambda p: strips.append(p) or True)
    _run_dir, spec = _stories_paused_run(project.project, stage="plan-checkpoint")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await pilot.click(await ready(pilot, "#act-replan"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])
        assert resets == [(spec, "draft")]
        assert strips == [spec]


async def test_story_checkpoint_continue_resumes(project, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
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
        await pilot.click(await ready(pilot, "#act-continue"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


async def test_story_checkpoint_stop_marks_stopped(project, monkeypatch):
    from bmad_loop import runs

    stops: list[Path] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(runs, "stop_run", lambda rd: stops.append(rd) or True)
    monkeypatch.setattr(launch, "kill_ctl_window", lambda rid: None)
    _stories_paused_run(
        project.project,
        stage="story-checkpoint",
        spec_status="done",
        spec_checkpoint=False,
        done_checkpoint=True,
    )
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, StoryCheckpointModal)
        await pilot.click(await ready(pilot, "#act-stop"))
        await until(pilot, lambda: len(stops) == 1)


def test_checkpoint_gate_line_pluralization():
    # The gate line is derived, not hardcoded — and pluralizes the real cycle count.
    f = BmadLoopApp._checkpoint_gate_line
    assert f(0) == "verify + review gates passed · no follow-up review cycles"
    assert f(1) == "verify + review gates passed · 1 follow-up review cycle"
    assert f(3) == "verify + review gates passed · 3 follow-up review cycles"


async def test_story_checkpoint_card_surfaces_real_review_cycles(project, monkeypatch):
    # audit item 13: the card's gate line must reflect the task's real
    # review_cycle, never the old blanket "verification passed" string.
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
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


async def test_escalation_rearm_resumes_when_resolution_ready(project, monkeypatch):
    from bmad_loop import resolve, runs

    calls: list[str] = []
    rearms: list[str] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    monkeypatch.setattr(
        runs, "rearm_escalation", lambda rd, sk: rearms.append(sk) or "ready-for-dev"
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
        # story context + blocking condition were resolved from stories.yaml + the spec
        assert app.screen._description == "does a thing"
        assert "Auto Run Result" in app.screen._blocking
        await pilot.click(await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: rearms == ["1"] and calls == ["20260611-100000-aaaa"])


async def test_escalation_rearm_disabled_without_resolution(project, monkeypatch):
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    _stories_paused_run(
        project.project,
        stage="escalation",
        spec_status="blocked",
        spec_checkpoint=False,
        blocked_result="Blocked.",
    )
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        await ready(pilot, "#act-rearm")
        assert app.screen.query_one("#act-rearm", Button).disabled


async def test_gate_pause_resume(project, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    spec = project.project / "spec-1-1-a.md"
    spec.write_text("---\nstatus: ready-for-dev\n---\n# finalized spec\n", encoding="utf-8")
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEV_VERIFY)
    task.spec_file = str(spec)
    make_run(
        project.project,
        "20260611-100000-aaaa",
        paused_stage="spec-approval",
        paused_reason="awaiting spec approval",
        paused_story_key="1-1-a",
        tasks={"1-1-a": task},
    )
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, SpecReviewModal)
        await pilot.click(await ready(pilot, "#act-resume"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


async def test_start_run_modal_stories_source_launches(project, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(launch, "tmux_available", lambda: True)

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


async def test_start_run_modal_stories_preview_validates(project, monkeypatch):
    # action_start_run bails on _tmux_missing() before it can push the modal, and
    # the Windows CI matrix has no tmux on PATH — every StartRunModal test stubs
    # this out so the modal opens (its absence here was the all-Windows timeout).
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    _write_stories_fixture(project.project)  # epic-1 with two stories, 1 done
    app = BmadLoopApp(project.project)
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


async def test_start_run_modal_stories_source_blank_folder_errors(project, monkeypatch):
    calls: list = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    app = BmadLoopApp(project.project)
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
