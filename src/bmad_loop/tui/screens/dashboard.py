"""Live read-only dashboard: run picker, run detail, journal/log/sprint tails.

Polling model: a 1s interval kicks an exclusive thread worker that does all
filesystem I/O through the stat-gated readers in tui.data and produces an
immutable snapshot; the snapshot is applied to widgets back on the event loop.
Selecting a run replaces the whole poll context and bumps a generation
counter, so a stale in-flight snapshot for the previous run is dropped on
arrival rather than painted over the new one. The run list itself is
selection-independent and is always applied.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    OptionList,
    RichLog,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option, OptionDoesNotExist

from ... import policy as policy_mod
from ... import sprintstatus, stories
from ...model import RunState, StoryTask
from ...runs import RUNS_DIR
from .. import data
from ..widgets import (
    DeferredEntryOption,
    JournalEntryOption,
    RunHeader,
    SelectableRichLog,
    Splitter,
    SprintTree,
    StoriesTable,
    agent_label,
    pause_tag,
    status_cell,
    stopping_tag,
)
from .modals import DeferredEntryModal

# Resizable-pane geometry, in terminal cells. The MIN_* floors keep a pane from
# collapsing to nothing when a boundary is dragged or the terminal shrinks; they
# mirror the `min-height: 4` backstops still in the CSS.
_MIN_SIDEBAR = 20
_MIN_DETAIL = 30
_MIN_PANE = 4  # a stacked left-column pane (Runs / Deferred)
_MIN_TASKS = 3
_MIN_TABS = 5
# The horizontal splitters cycled by Tab in resize mode, top to bottom.
_HSPLITS = ("#split-runs", "#split-deferred", "#split-tasks")
# Resize-mode actions gated off (key passes through to the focused widget) unless
# the mode is active. Tab/Shift+Tab are intentionally absent: they stay live and
# delegate to focus movement when not resizing.
_RESIZE_ACTIONS = frozenset(
    {"resize_up", "resize_down", "resize_left", "resize_right", "resize_done"}
)

# Keep at most this many parsed journal entries per run for active-task
# tracking; the visible pane is bounded separately per widget.
_MAX_ENTRIES = 500

_MAX_JOURNAL_OPTIONS = 2000  # visible journal rows kept in the OptionList

_RESCAN_EVERY = 3  # run-list + sprint rescan cadence, in 1s ticks

_LAUNCH_TIMEOUT = 10.0  # seconds before a pending launch is presumed failed

_UNAPPLIED: Any = object()  # "no snapshot applied yet" for the identity gates


def _transcript_for_task(state: RunState, task_id: str) -> str | None:
    """The agent JSONL transcript recorded for a log task (the per-session
    task id matching the `<task>.log` name), newest session wins. Used to point
    a fullscreen-capture notice at the complete session record."""
    found: str | None = None
    for story in state.tasks.values():
        for rec in story.sessions:
            if rec.task_id == task_id and rec.transcript_path:
                found = rec.transcript_path
    return found


class _PollContext:
    """Mutable state for polling one selected run. Constructed on the UI
    thread (constructors do no I/O), then mutated only inside the poll worker.

    Forced ticks (journal jumps, run select) reuse self._ctx, so a superseded
    worker can still hold THIS object — and exclusive=True cannot stop a
    running thread, only mark it cancelled. The screen's _poll_lock therefore
    serializes worker bodies so this state (and ctx.log's pyte stream) is never
    mutated by two threads at once."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.watcher = data.RunWatcher(run_dir)
        self.journal = data.JournalTail(run_dir)
        self.entries: list[dict[str, Any]] = []
        self.log: data.LogView | None = None
        self.log_task: str | None = None
        self.attention_seen = 0
        self.first_poll = True
        self.decision_toasted: str | None = None  # dw_id already announced


@dataclass
class _Snapshot:
    generation: int
    runs: list[data.RunInfo] | None = None  # None: no rescan this tick
    project_refreshed: bool = False  # sprint + deferred rescanned this tick
    missed_decisions: int = 0  # decisions past sweeps left unanswered
    sprint: sprintstatus.SprintStatus | None = None
    deferred: list[data.DeferredItem] | None = None
    has_run: bool = False
    run_id: str = ""
    status: str = data.UNKNOWN
    stopping: bool = False  # selected run has a graceful stop pending (RUNNING only)
    agent: data.ActiveAgent | None = None  # agent driving the selected run, live only
    state: RunState | None = None
    stories_mode: bool = False  # selected run is stories mode (source == "stories")
    stories: list[stories.StoryRow] | None = None  # stories board rows, when stories_mode
    new_entries: list[dict[str, Any]] = field(default_factory=list)
    log_task: str | None = None
    log_reset: bool = False
    log_lines: Text | None = None  # full re-render; None = unchanged this tick
    log_index: data.LogIndex | None = None  # rebuilt alongside log_lines
    log_pinned: bool = False
    log_altscreen: bool = False  # the capture entered a fullscreen (alt-screen) TUI
    log_transcript: str | None = None  # agent JSONL transcript, when an altscreen log has one
    attention_reset: bool = False
    new_attention: str = ""
    toast_attention: bool = False
    decision: tuple[str, str] | None = None  # (dw_id, question) awaiting a human
    toast_decision: bool = False


class DashboardScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "unpin_log", "follow log", show=False),
        Binding("y", "copy_pane", "copy"),
        Binding("ctrl+w", "resize_mode", "resize"),
        # Priority so they beat the focused table/tree/list in resize mode;
        # check_action() disables them (key passes through) when not resizing.
        Binding("up", "resize_up", show=False, priority=True),
        Binding("down", "resize_down", show=False, priority=True),
        Binding("left", "resize_left", show=False, priority=True),
        Binding("right", "resize_right", show=False, priority=True),
        Binding("enter", "resize_done", show=False, priority=True),
        # Tab stays live: it cycles the active boundary in resize mode and falls
        # back to focus movement otherwise (see action_resize_cycle).
        Binding("tab", "resize_cycle(1)", show=False, priority=True),
        Binding("shift+tab", "resize_cycle(-1)", show=False, priority=True),
    ]

    # Persisted pane geometry (cells); 0 = unset -> keep the CSS default. Seeded
    # after first layout (and from policy) and mutated by drags / resize mode.
    left_width: reactive[int] = reactive(0, init=False)
    runs_height: reactive[int] = reactive(0, init=False)
    deferred_height: reactive[int] = reactive(0, init=False)
    tasks_height: reactive[int] = reactive(0, init=False)

    def __init__(self, project: Path):
        super().__init__()
        self.project = project
        # Persisted pane sizes to seed on first layout; a malformed or unreadable
        # policy file degrades to defaults rather than blocking the dashboard.
        try:
            self._tui_policy = policy_mod.load(project / policy_mod.POLICY_FILE).tui
        except (policy_mod.PolicyError, OSError):
            self._tui_policy = policy_mod.TuiPolicy()
        self._seeded = False
        self._left_frozen = False  # Runs/Deferred switched to explicit heights
        self._detail_frozen = False  # Tasks switched to an explicit height
        self._resize_mode = False
        self._active_hsplit = 0  # index into _HSPLITS for Up/Down in resize mode
        self._saved_subtitle = ""
        self._generation = 0
        self._ctx: _PollContext | None = None
        self._tick_count = 0
        self._run_rows: list[str] = []  # row keys, table order (oldest first)
        self._task_rows: set[str] = set()
        self._pending_run: str | None = None  # just-launched run, no state.json yet
        self._pending_deadline = 0.0
        self._decision: tuple[str, str] | None = None
        # identity gates: the stat-gated readers return the same object while
        # the file is unchanged, so `is` detects "nothing to repaint"; the
        # sentinel makes the first snapshot always paint (even a None one)
        self._last_sprint: Any = _UNAPPLIED
        self._last_deferred: Any = _UNAPPLIED
        # serializes the poll worker body: exclusive=True marks superseded
        # thread workers cancelled but cannot stop them, so without this two
        # threads could feed ctx.log's pyte stream at once (crash)
        self._poll_lock = threading.Lock()
        # journal -> log jump state, all owned by the UI thread
        self._log_index: data.LogIndex | None = None
        self._displayed_log_task: str | None = None
        self._pin_task: str | None = None  # show this task's log instead of the active one
        self._pending_jump: tuple[str, int] | None = None  # (task_id, log_pos)
        self._log_follow_tail = True  # stick to newest log lines until a jump pins us

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                runs = DataTable(id="runs", cursor_type="row")
                runs.border_title = "Runs"  # first pane: keeps its own titled border
                yield runs
                # The section titles that used to ride each pane's border-top now
                # ride the splitter bar above that pane (its border-top is dropped
                # in CSS). split-runs' label tracks the sprint/stories toggle.
                yield Splitter(
                    horizontal=True,
                    label="Sprint",
                    apply=self._resize_runs,
                    on_release=self._persist_geometry,
                    id="split-runs",
                )
                yield SprintTree("sprint", id="sprint-tree")
                yield StoriesTable(id="stories-table")
                yield Splitter(
                    horizontal=True,
                    label="Deferred Work",
                    apply=self._resize_deferred,
                    on_release=self._persist_geometry,
                    id="split-deferred",
                )
                yield OptionList(id="deferred")
            yield Splitter(
                horizontal=False,
                apply=self._resize_left,
                on_release=self._persist_geometry,
                id="split-main",
            )
            with Vertical(id="detail"):
                yield RunHeader(id="runheader")
                yield DataTable(id="tasks", cursor_type="row")
                yield Splitter(
                    horizontal=True,
                    apply=self._resize_tasks,
                    on_release=self._persist_geometry,
                    id="split-tasks",
                )
                with TabbedContent(id="tabs"):
                    with TabPane("Journal", id="tab-journal"):
                        yield OptionList(id="journal")
                    with TabPane("Log", id="tab-log"):
                        # headroom over the render's 2000-line history cap so
                        # the header row is never silently dropped at capacity
                        yield SelectableRichLog(id="log", max_lines=2048, auto_scroll=True)
                    with TabPane("Attention", id="tab-attention"):
                        yield SelectableRichLog(id="attention", max_lines=500, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        runs = self.query_one("#runs", DataTable)
        runs.add_column("st", key="st", width=2)
        runs.add_column("run", key="run")
        runs.add_column("type", key="type")
        runs.add_column("note", key="note", width=6)  # pause-kind badge for paused runs
        # the stories board shares the sprint-tree slot; hidden until a stories-mode
        # run is selected (the poll worker toggles both by the run's source).
        self.query_one("#stories-table", StoriesTable).display = False
        tasks = self.query_one("#tasks", DataTable)
        tasks.add_column("story", key="story", width=30)
        tasks.add_column("phase", key="phase", width=16)
        tasks.add_column("agent", key="agent", width=13)
        tasks.add_column("dev", key="dev", width=5)
        tasks.add_column("review", key="review", width=6)
        tasks.add_column("tokens", key="tokens", width=12)
        tasks.add_column("info", key="info")
        tasks.add_column("raw", key="raw", width=13)
        self.query_one("#runheader", RunHeader).show_empty(self.project)
        self.set_interval(1.0, self._tick)
        self._tick()
        # Seed pane geometry once the first layout has real sizes to read.
        self.call_after_refresh(self._seed_geometry)

    # ------------------------------------------------------------- selection

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "runs" and event.row_key is not None:
            self._select_run(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "runs" and event.row_key is not None:
            self._select_run(str(event.row_key.value))

    @property
    def selected_run_id(self) -> str | None:
        return self._ctx.run_dir.name if self._ctx else None

    @property
    def decision_pending(self) -> tuple[str, str] | None:
        """(dw_id, question) the selected run's sweep is blocked on, if any —
        the attach action uses this to target the orchestrator window."""
        return self._decision

    def _select_run(self, run_id: str) -> None:
        if self._ctx is not None and self._ctx.run_dir.name == run_id:
            return
        self._generation += 1
        self._ctx = _PollContext(self.project / RUNS_DIR / run_id)
        self._decision = None
        self._log_index = None
        self._displayed_log_task = None
        self._pin_task = None
        self._pending_jump = None
        self._log_follow_tail = True
        tasks = self.query_one("#tasks", DataTable)
        tasks.clear()
        self._task_rows.clear()
        self.query_one("#journal", OptionList).clear_options()
        for log_id in ("#log", "#attention"):
            self.query_one(log_id, RichLog).clear()
        self.query_one("#runheader", RunHeader).show_run(run_id, data.UNKNOWN, None)
        self._tick(force_rescan=False)

    def forget_run(self, run_id: str) -> None:
        """A run dir was just removed (delete/archive): drop the selection when
        it was the gone run and rescan so the table rebuilds and re-selects."""
        if self._ctx is not None and self._ctx.run_dir.name == run_id:
            self._ctx = None
        self._tick(force_rescan=True)

    def expect_run(self, run_id: str) -> None:
        """A launch just happened: select the run before its dir exists, show
        a 'starting' header until state.json appears, and complain past the
        launch timeout."""
        self._pending_run = run_id
        self._pending_deadline = time.monotonic() + _LAUNCH_TIMEOUT
        self._select_run(run_id)
        self.query_one("#runheader", RunHeader).show_starting(run_id)

    # ----------------------------------------------------- journal -> log jump

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "journal":
            entry = getattr(event.option, "entry", None)
            if entry is not None:
                self._jump_to_log_event(entry)
        elif event.option_list.id == "deferred":
            item = getattr(event.option, "item", None)
            if item is not None:
                self.app.push_screen(DeferredEntryModal(item))

    def _jump_to_log_event(self, entry: dict[str, Any]) -> None:
        task, pos = entry.get("log_task"), entry.get("log_pos")
        if not task or not isinstance(pos, (int, float)):
            self.notify(
                "no log position recorded for this entry (older run?)",
                severity="warning",
            )
            return
        task, pos = str(task), int(pos)
        self.query_one("#tabs", TabbedContent).active = "tab-log"
        self._log_follow_tail = False  # anchor on the jump target, stop chasing the tail
        # _scroll_log_to owns clearing this once the scroll lands; until then
        # every poll tick's _apply re-attempts it, so a flush that outlives the
        # retry chain delays the jump instead of losing it.
        self._pending_jump = (task, pos)
        if task == self._displayed_log_task and self._log_index is not None:
            self._scroll_log_to()
            return
        # another session's log (or this one not rendered yet): pin it and
        # finish the jump once a poll has fed and rendered that file
        if task != self._displayed_log_task:
            self._pin_task = task
        self._tick(force_rescan=False)

    def _scroll_log_to(self, attempts: int = 20, finalize: bool = False) -> None:
        jump = self._pending_jump
        if jump is None or jump[0] != self._displayed_log_task:
            # jump cancelled or superseded, or the pane re-rendered another
            # task's log while a retry was armed — a stale fire must not scroll
            return
        if self._log_index is None:
            # not rendered yet (or a log reset mid-chain): the pending jump
            # survives and the next ≤1s poll tick re-attempts it
            return
        line = self._log_index.line_for_offset(jump[1])
        if line is None:
            self._pending_jump = None  # give up for good: a retry would re-notify
            self.notify("log is empty or not loaded yet", severity="warning")
            return
        log = self.query_one("#log", RichLog)
        target = line + 1  # the '— task.log —' header row above the render
        if log.virtual_size.height <= target:
            # A previously hidden RichLog defers writes until the tab switch
            # gives it a size, and that flush applies its own scroll_end.
            # Wait for the flush (content taller than the target proves it)
            # so our scroll lands after it instead of being stomped. The chain
            # only covers sub-second snappiness: when it exhausts, the pending
            # jump stays set and the next ≤1s poll tick re-attempts it. Each
            # fire re-checks the jump identity so a superseded chain dies, and
            # recomputes the line from the live index so a same-task repaint
            # mid-chain (render_base drift) can't scroll to stale coordinates.
            if attempts > 0:
                self.set_timer(
                    0.05,
                    lambda: (
                        self._scroll_log_to(attempts - 1) if self._pending_jump is jump else None
                    ),
                )
            return
        viewport = max(1, log.scrollable_content_region.height)
        log.scroll_to(y=max(0, target - viewport // 2), animate=False)
        if finalize:
            self._pending_jump = None  # landed: release so later renders don't re-jump
            return
        # The open height gate proves the reveal flush *wrote* — not that its
        # scroll drained: RichLog.on_resize replays the deferred writes, whose
        # scroll_end goes through call_after_refresh, so a tail-scroll can
        # still be queued on the log's pump while scroll_to (immediate on a
        # ScrollView) already landed. Releasing here would let that queued
        # scroll stomp the jump with nothing left to re-attempt it. Route the
        # release through the same queue instead: FIFO puts the finalize fire
        # after any queued stomp, and it re-scrolls to the recomputed target
        # before letting go. If its guards bail (log reset mid-window), the
        # jump stays pending for the next ≤1s tick.
        log.call_after_refresh(
            lambda: (self._scroll_log_to(finalize=True) if self._pending_jump is jump else None)
        )

    def action_unpin_log(self) -> None:
        if self._resize_mode:  # Escape leaves resize mode before it unpins the log
            self._exit_resize_mode()
            return
        if self._pin_task is None and self._pending_jump is None and self._log_follow_tail:
            return  # nothing to unpin and already following the tail
        self._pin_task = None
        self._pending_jump = None
        self._log_follow_tail = True
        self._tick(force_rescan=False)

    def action_copy_pane(self) -> None:
        """Copy the whole Log/Attention pane to the clipboard (mouse-free
        alternative to click-drag + ctrl+c). Only the two RichLog tabs hold
        free-form copyable text; the tables/tree are not selectable."""
        active = self.query_one("#tabs", TabbedContent).active
        log_id = {"tab-log": "#log", "tab-attention": "#attention"}.get(active)
        if log_id is None:
            self.notify("switch to the Log or Attention tab to copy", severity="warning")
            return
        pane = self.query_one(log_id, RichLog)
        # No rstrip: keep the joined buffer byte-for-byte identical to what
        # SelectableRichLog.get_selection() returns, so `y` and drag-select+ctrl+c
        # copy the same text.
        text = "\n".join(strip.text for strip in pane.lines)
        if not text:
            self.notify("nothing to copy", severity="warning")
            return
        self.app.copy_to_clipboard(text)
        self.notify(f"copied {log_id.lstrip('#')} pane to clipboard")

    # ------------------------------------------------------------ pane resizing
    #
    # Model: the pane on one side of a boundary carries an explicit cell size
    # (owned by a reactive below); a designated neighbour stays `1fr` and absorbs
    # slack, including terminal resizes. The sidebar (#left) is already fixed and
    # #detail flexes. The left column freezes Runs/Deferred to explicit heights on
    # first resize (the middle Sprint/Stories slot becomes the sole flex); the
    # detail column freezes Tasks (the Tabs pane already flexes). Watchers clamp
    # against live region sizes and write widget.styles; on_resize re-clamps.

    def _seed_geometry(self) -> None:
        """Apply persisted sizes once the first layout has real dimensions. With
        nothing persisted the panes keep their CSS proportions untouched, so a
        fresh project looks exactly as before."""
        if self._seeded:
            return
        self._seeded = True
        pol = self._tui_policy
        if pol.left_width > 0:
            self.left_width = pol.left_width
        if pol.runs_height > 0 or pol.deferred_height > 0:
            self._freeze_left()
            if pol.runs_height > 0:
                self.runs_height = pol.runs_height
            if pol.deferred_height > 0:
                self.deferred_height = pol.deferred_height
        if pol.tasks_height > 0:
            self._freeze_detail()
            self.tasks_height = pol.tasks_height

    def _freeze_left(self) -> None:
        """Switch the left column to explicit Runs/Deferred heights with the
        Sprint/Stories slot as the sole vertical flex. Idempotent."""
        if self._left_frozen:
            return
        self._left_frozen = True
        # Snapshot both current render heights before mutating anything: assigning
        # one reactive fires its watcher, and _apply_left_heights must see both
        # seeds (its 0-guard skips a half-seeded state). outer_size is the
        # border-box height styles.height sets, so freezing is seamless even for
        # #runs' titled border.
        runs_seed = max(_MIN_PANE, self.query_one("#runs").outer_size.height)
        deferred_seed = max(_MIN_PANE, self.query_one("#deferred").outer_size.height)
        for wid in ("#sprint-tree", "#stories-table"):
            self.query_one(wid).styles.height = "1fr"
        if self.runs_height <= 0:
            self.runs_height = runs_seed
        if self.deferred_height <= 0:
            self.deferred_height = deferred_seed
        self._apply_left_heights()

    def _freeze_detail(self) -> None:
        """Switch Tasks to an explicit height (Tabs already flexes). Idempotent."""
        if self._detail_frozen:
            return
        self._detail_frozen = True
        tasks = self.query_one("#tasks", DataTable)
        if self.tasks_height <= 0:
            self.tasks_height = max(_MIN_TASKS, tasks.outer_size.height)
        # The explicit height governs now; _apply_tasks_height also lifts the CSS
        # `max-height: 35%` cap (setting the inline value to None would NOT — the
        # stylesheet rule wins, silently re-clamping a taller Tasks pane).
        self._apply_tasks_height()

    # ---- appliers (clamp against live sizes, then write styles) --------------

    def _apply_left_width(self) -> None:
        if self.left_width <= 0:
            return
        total = self.size.width
        if total <= 0:
            return
        hi = max(_MIN_SIDEBAR, total - _MIN_DETAIL - 1)  # -1 for the splitter column
        val = max(_MIN_SIDEBAR, min(self.left_width, hi))
        self.query_one("#left").styles.width = val
        if val != self.left_width:
            self.left_width = val  # snap the reactive to the clamped value

    def _apply_left_heights(self) -> None:
        if not self._left_frozen:
            return
        if self.runs_height <= 0 or self.deferred_height <= 0:
            return  # mid-freeze: both seeds not in yet (see _freeze_left)
        total = self.query_one("#left").size.height
        if total <= 0:
            return
        # Reserve rows the two fixed panes cannot use: the two splitter bars, the
        # Runs border-top, and the flex middle's minimum.
        room = max(2 * _MIN_PANE, total - (2 + 1 + _MIN_PANE))
        rh = max(_MIN_PANE, self.runs_height)
        dh = max(_MIN_PANE, self.deferred_height)
        if rh + dh > room:  # trim the deferred pane first, then runs
            dh = max(_MIN_PANE, min(dh, room - _MIN_PANE))
            rh = max(_MIN_PANE, min(rh, room - dh))
        self.query_one("#runs").styles.height = rh
        self.query_one("#deferred").styles.height = dh
        if rh != self.runs_height:
            self.runs_height = rh
        if dh != self.deferred_height:
            self.deferred_height = dh

    def _apply_tasks_height(self) -> None:
        if not self._detail_frozen or self.tasks_height <= 0:
            return
        total = self.query_one("#detail").size.height
        if total <= 0:
            return
        header = self.query_one("#runheader").size.height
        hi = max(_MIN_TASKS, total - header - 1 - _MIN_TABS)  # -1 for the splitter
        val = max(_MIN_TASKS, min(self.tasks_height, hi))
        tasks = self.query_one("#tasks")
        tasks.styles.height = val
        # Neutralize the stylesheet's `max-height: 35%` (the unfrozen default): an
        # inline value >= the height is the only way to override it, since None
        # falls back to the CSS rule and would re-clamp Tasks below `val`.
        tasks.styles.max_height = val
        if val != self.tasks_height:
            self.tasks_height = val

    def watch_left_width(self, _old: int, _new: int) -> None:
        self._apply_left_width()

    def watch_runs_height(self, _old: int, _new: int) -> None:
        self._apply_left_heights()

    def watch_deferred_height(self, _old: int, _new: int) -> None:
        self._apply_left_heights()

    def watch_tasks_height(self, _old: int, _new: int) -> None:
        self._apply_tasks_height()

    def on_resize(self, event: events.Resize) -> None:
        # Re-clamp explicit sizes so a shrunk terminal can't push a fixed pane off
        # screen; unfrozen dimensions are no-ops.
        self._apply_left_width()
        self._apply_left_heights()
        self._apply_tasks_height()

    # ---- drag targets (one per boundary; sign lives here) -------------------

    def _resize_left(self, delta: int) -> None:
        base = self.left_width if self.left_width > 0 else self.query_one("#left").size.width
        self.left_width = base + delta  # +delta widens the sidebar

    def _resize_runs(self, delta: int) -> None:
        self._freeze_left()
        self.runs_height = max(_MIN_PANE, self.runs_height + delta)  # down grows Runs

    def _resize_deferred(self, delta: int) -> None:
        self._freeze_left()
        # The bar sits atop Deferred: moving it down grows Sprint / shrinks Deferred.
        self.deferred_height = max(_MIN_PANE, self.deferred_height - delta)

    def _resize_tasks(self, delta: int) -> None:
        self._freeze_detail()
        self.tasks_height = max(_MIN_TASKS, self.tasks_height + delta)  # down grows Tasks

    def _persist_geometry(self) -> None:
        """Write the current sizes to policy.toml (drag end / resize-mode exit).
        Best-effort: a write or parse failure degrades to a toast, never a crash."""
        import tomlkit.exceptions

        from ..settings import PolicyDoc

        path = self.project / policy_mod.POLICY_FILE
        dirty = self.left_width > 0 or self._left_frozen or self._detail_frozen
        if not dirty and not path.is_file():
            return  # nothing customised and no file to update — don't create one
        try:
            # A missing file starts empty, NOT from POLICY_TEMPLATE: a geometry
            # save must not freeze every default setting into a fresh policy.toml.
            doc = PolicyDoc.load(path, template_when_missing=False)
            doc.set("tui", "left_width", self.left_width if self.left_width > 0 else None)
            doc.set("tui", "runs_height", self.runs_height if self._left_frozen else None)
            doc.set("tui", "deferred_height", self.deferred_height if self._left_frozen else None)
            doc.set("tui", "tasks_height", self.tasks_height if self._detail_frozen else None)
            doc.save(path)
        except (OSError, tomlkit.exceptions.TOMLKitError) as e:
            self.notify(f"could not save layout: {e}", severity="warning")

    def on_unmount(self) -> None:
        # Quitting mid-resize-mode would otherwise drop the new geometry: keyboard
        # bumps persist only on mode exit, and `q` stays live inside the mode.
        if self._resize_mode:
            self._persist_geometry()

    # ---- keyboard resize mode -----------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # The arrow/Enter resize bindings are priority so they beat the focused
        # table/list while resizing; disable them otherwise so the key reaches the
        # widget (Tab/Shift+Tab stay live — see action_resize_cycle).
        if action in _RESIZE_ACTIONS and not self._resize_mode:
            return False
        return True

    def action_resize_mode(self) -> None:
        if self._resize_mode:
            self._exit_resize_mode()
        else:
            self._enter_resize_mode()

    def _enter_resize_mode(self) -> None:
        self._resize_mode = True
        self._active_hsplit = 0
        self.query_one("#split-main", Splitter).add_class("-active")
        self._highlight_active_hsplit()
        self._saved_subtitle = self.app.sub_title
        self.app.sub_title = "[RESIZE] ←/→ width · ↑/↓ height · Tab boundary · Esc done"

    def _exit_resize_mode(self) -> None:
        if not self._resize_mode:
            return
        self._resize_mode = False
        for split in self.query(Splitter):
            split.remove_class("-active")
        self.app.sub_title = self._saved_subtitle or str(self.project)
        self._persist_geometry()

    def _highlight_active_hsplit(self) -> None:
        for i, sid in enumerate(_HSPLITS):
            self.query_one(sid, Splitter).set_class(i == self._active_hsplit, "-active")

    def _active_hsplit_widget(self) -> Splitter:
        return self.query_one(_HSPLITS[self._active_hsplit], Splitter)

    def action_resize_cycle(self, direction: int) -> None:
        # Tab stays bound always: cycle the active boundary while resizing, else
        # fall back to the normal focus movement Tab would have done.
        if not self._resize_mode:
            if direction > 0:
                self.app.action_focus_next()
            else:
                self.app.action_focus_previous()
            return
        self._active_hsplit = (self._active_hsplit + direction) % len(_HSPLITS)
        self._highlight_active_hsplit()

    def action_resize_left(self) -> None:
        self.query_one("#split-main", Splitter).bump(-1)

    def action_resize_right(self) -> None:
        self.query_one("#split-main", Splitter).bump(1)

    def action_resize_up(self) -> None:
        self._active_hsplit_widget().bump(-1)

    def action_resize_down(self) -> None:
        self._active_hsplit_widget().bump(1)

    def action_resize_done(self) -> None:
        self._exit_resize_mode()

    # --------------------------------------------------------------- polling

    def _tick(self, force_rescan: bool | None = None) -> None:
        if self._pending_run is not None and time.monotonic() > self._pending_deadline:
            self._pending_run = None
            self.notify(
                "launch may have failed — attach to control session bmad-loop-ctl",
                severity="error",
                timeout=15,
            )
        if force_rescan is None:
            force_rescan = self._tick_count % _RESCAN_EVERY == 0
            self._tick_count += 1
        # the pin is read here on the UI thread; ctx stays worker-owned. If a
        # worker is still mid-flight _poll bails on the lock; _pin_task and
        # _pending_jump persist on the screen, so the next interval tick (≤1s)
        # re-applies them — no extra rescheduling needed.
        self._poll(self._ctx, self._generation, force_rescan, self._pin_task)

    @work(thread=True, exclusive=True, group="poll")
    def _poll(
        self, ctx: _PollContext | None, generation: int, rescan: bool, pin: str | None
    ) -> None:
        # A superseded thread worker keeps running until it returns, so guard
        # the whole body: only one poll may touch ctx (and ctx.log's pyte
        # stream) at a time. Skipped ticks are safe — _pin_task/_pending_jump
        # persist on the screen and the next tick reapplies them.
        if not self._poll_lock.acquire(blocking=False):
            return
        try:
            snap = _Snapshot(generation=generation)
            if rescan:
                snap.runs = data.discover_runs(self.project)
                snap.project_refreshed = True
                snap.sprint = data.sprint_overview(self.project)
                snap.deferred = data.deferred_entries(self.project)
                snap.missed_decisions = len(data.pending_missed_decisions(self.project))
            if ctx is not None:
                snap.has_run = True
                snap.run_id = ctx.run_dir.name
                snap.state = ctx.watcher.state()
                snap.status = ctx.watcher.status()
                # A graceful stop pending is the control file, meaningful while an
                # engine is still around to consume it — RUNNING or UNKNOWN (an
                # unverifiable pid still honors it, matching the CLI's != "dead").
                snap.stopping = (
                    snap.status in (data.RUNNING, data.UNKNOWN) and ctx.watcher.stopping()
                )
                if snap.state is not None and snap.state.source == "stories":
                    # per-run board: re-derived each tick (only while a stories run
                    # is selected) so it tracks the dev sessions writing story specs.
                    snap.stories_mode = True
                    snap.stories = data.stories_overview(self.project, snap.state.spec_folder)
                snap.new_entries = ctx.journal.read_new()
                ctx.entries.extend(snap.new_entries)
                del ctx.entries[:-_MAX_ENTRIES]
                snap.decision = data.pending_decision(ctx.entries)
                if snap.decision is not None and ctx.decision_toasted != snap.decision[0]:
                    snap.toast_decision = True
                ctx.decision_toasted = snap.decision[0] if snap.decision else None
                task = pin or data.active_task_id(ctx.run_dir, ctx.entries)
                # The live agent driving the run, gated like `stopping`: a dead run
                # has no live agent, but UNKNOWN liveness is honored (cf. 3eb5c21).
                if snap.status in (data.RUNNING, data.UNKNOWN):
                    snap.agent = data.active_agent(
                        ctx.entries, snap.state.policy_snapshot if snap.state else None
                    )
                snap.log_pinned = pin is not None
                if task != ctx.log_task:
                    ctx.log_task = task
                    ctx.log = (
                        data.LogView(ctx.run_dir / data.LOGS_DIR / f"{task}.log") if task else None
                    )
                    snap.log_reset = True
                snap.log_task = task
                if ctx.log is not None and (ctx.log.read_new() or snap.log_reset):
                    snap.log_lines = ctx.log.render()
                    snap.log_index = ctx.log.index()
                if ctx.log is not None and ctx.log.altscreen_seen:
                    snap.log_altscreen = True
                    if snap.state is not None and task:
                        snap.log_transcript = _transcript_for_task(snap.state, task)
                attention = ctx.watcher.attention()
                if len(attention) < ctx.attention_seen:
                    snap.attention_reset = True
                    snap.new_attention = attention
                else:
                    snap.new_attention = attention[ctx.attention_seen :]
                ctx.attention_seen = len(attention)
                snap.toast_attention = bool(snap.new_attention.strip()) and not ctx.first_poll
                ctx.first_poll = False
            self.app.call_from_thread(self._apply, snap)
        finally:
            self._poll_lock.release()

    # ------------------------------------------------------------ applying

    def _apply(self, snap: _Snapshot) -> None:
        # A thread-worker poll delivers its snapshot here via call_from_thread; that
        # callback can land after the screen has been torn down (app shutdown, or
        # another screen switched in), when the widgets below are already gone and
        # query_one would raise NoMatches. is_running flips False on teardown but
        # stays True while merely backgrounded under a pushed screen, so a stale
        # apply is dropped while a live-but-background one still refreshes.
        if not self.is_running:
            return
        if snap.runs is not None:
            self._apply_runs(snap.runs)
        if snap.project_refreshed:
            self._apply_sprint_tree(snap.sprint)
            self._apply_deferred(snap.deferred)
            self._apply_missed_decisions(snap.missed_decisions)
        if not snap.has_run or snap.generation != self._generation:
            return  # selection changed mid-poll: per-run parts are stale

        self._decision = snap.decision
        header = self.query_one("#runheader", RunHeader)
        if snap.run_id == self._pending_run and snap.state is None:
            header.show_starting(snap.run_id)  # launched, state.json not yet written
        else:
            if snap.run_id == self._pending_run:
                self._pending_run = None  # the engine is up
            header.show_run(
                snap.run_id,
                snap.status,
                snap.state,
                snap.decision,
                stopping=snap.stopping,
                agent=snap.agent,
            )
        self._apply_board(snap)
        if snap.state is not None:
            self._apply_tasks(snap.state, snap.agent)
        if snap.toast_decision and snap.decision is not None:
            self.notify(
                snap.decision[1] or snap.decision[0],
                title=f"decision needed: {snap.decision[0]} — press a to attach",
                severity="warning",
                timeout=30,
            )

        journal = self.query_one("#journal", OptionList)
        if snap.new_entries:
            at_end = journal.is_vertical_scroll_end
            journal.add_options(JournalEntryOption(e) for e in snap.new_entries)
            for _ in range(max(0, journal.option_count - _MAX_JOURNAL_OPTIONS)):
                journal.remove_option_at_index(0)
            if at_end:
                # follow the tail like the old RichLog did, but leave the
                # highlight alone so a user browsing upward is not yanked down
                journal.scroll_end(animate=False)

        log = self.query_one("#log", RichLog)
        self._displayed_log_task = snap.log_task
        if snap.log_reset:
            self._log_index = None
        if snap.log_index is not None:
            self._log_index = snap.log_index
        if snap.log_reset or snap.log_lines is not None:
            # Cursor-up repaints rewrite earlier content, so the pane is a full
            # re-render, not an append; RichLog keeps scroll_y across the
            # clear+rewrite, so only an explicit scroll_end moves the view.
            # Follow the tail only when the user means to (no jump has pinned
            # them off it) — inferring it from "currently at the bottom" dragged
            # a jump that happened to land at the tail down as the log grew.
            # (Jump targets rely on wrap=False: one render line == one row.)
            following = self._log_follow_tail and log.is_vertical_scroll_end
            at_end = snap.log_reset or following
            log.clear()
            if snap.log_task:
                suffix = " (pinned — esc to follow)" if snap.log_pinned else ""
                log.write(Text(f"— {snap.log_task}.log —{suffix}", style="dim"), scroll_end=False)
            if snap.log_altscreen:
                # A fullscreen (alt-screen) TUI repaints in place, so the emulated
                # pane collapses to the final frame. Flag it so the partial view is
                # not mistaken for the whole session, and point at the full record.
                note = "⚠ fullscreen (alt-screen) session — this pane shows only the final frame"
                if snap.log_transcript:
                    note += f"; full transcript: {snap.log_transcript}"
                log.write(Text(note, style="yellow"), scroll_end=False)
            if snap.log_lines is not None and snap.log_lines.plain:
                log.write(snap.log_lines, scroll_end=at_end)
        if self._pending_jump is not None:
            # _scroll_log_to owns clearing the pending jump when the scroll
            # lands (or the log is provably empty) — until then this block
            # re-attempts on every tick, so a flush that outlives the retry
            # chain delays the jump by ≤1s instead of losing it. Task-match
            # and index guards live inside _scroll_log_to.
            self._scroll_log_to()

        attention = self.query_one("#attention", RichLog)
        if snap.attention_reset:
            attention.clear()
        if snap.new_attention.strip():
            attention.write(Text(snap.new_attention.rstrip("\n")))
            if snap.toast_attention:
                last = snap.new_attention.strip().splitlines()[-1]
                self.notify(last, title="attention", severity="warning", timeout=10)

    def _apply_runs(self, runs: list[data.RunInfo]) -> None:
        table = self.query_one("#runs", DataTable)
        self._apply_attention(table, runs)
        ids = [r.run_id for r in runs]
        if not runs:
            if self._run_rows:
                table.clear()
                self._run_rows.clear()
            if self._ctx is None:
                self.query_one("#runheader", RunHeader).show_empty(self.project)
            return
        if any(known not in ids for known in self._run_rows):
            # a run dir disappeared — rare enough to just rebuild
            table.clear()
            self._run_rows.clear()
        first_populate = not self._run_rows
        added: list[str] = []
        for run in runs:
            note = stopping_tag() if run.stopping else pause_tag(run.paused_stage)
            if run.run_id in self._run_rows:
                table.update_cell(run.run_id, "st", status_cell(run.status))
                table.update_cell(run.run_id, "note", note)
            else:
                table.add_row(
                    status_cell(run.status),
                    run.run_id,
                    run.run_type,
                    note,
                    key=run.run_id,
                )
                self._run_rows.append(run.run_id)
                added.append(run.run_id)
        if first_populate:
            if self.selected_run_id in ids:
                # a pre-selected (just-launched) run beats auto-select-newest
                table.move_cursor(row=ids.index(self.selected_run_id))
            else:
                table.move_cursor(row=len(ids) - 1)  # newest; RowHighlighted selects
                self._select_run(ids[-1])
        elif self.selected_run_id in added:
            # the selected run was launched before its dir existed; its row
            # just appeared — bring the cursor to it
            table.move_cursor(row=self._run_rows.index(self.selected_run_id))

    def _apply_tasks(self, state: RunState, agent: data.ActiveAgent | None = None) -> None:
        table = self.query_one("#tasks", DataTable)
        weight = state.cache_read_weight()
        for key, task in state.tasks.items():
            weighted = task.tokens.weighted_total(weight)
            has_tokens = bool(task.tokens.total)
            # Gate on total (any tokens?), not on `weighted`: with cache_read_weight=0
            # a cache-read-only task has weighted==0 but nonzero raw — show "0", not "-"
            # (which reads as missing data). "-" means the task has no tokens at all.
            tokens = f"{weighted:,}" if has_tokens else "-"
            raw = f"{task.tokens.total:,}" if has_tokens else "-"
            info = task.defer_reason or (task.commit_sha or "")[:12]
            cells = {
                "phase": str(task.phase),
                "agent": self._agent_cell(task, key, agent),
                "dev": f"×{task.attempt}",
                "review": f"×{task.review_cycle}",
                "tokens": tokens,
                "info": info,
                "raw": raw,  # must be last — matches add_column order
            }
            if key in self._task_rows:
                for column, value in cells.items():
                    table.update_cell(key, column, value)
            else:
                table.add_row(key, *cells.values(), key=key)
                self._task_rows.add(key)

    @staticmethod
    def _agent_cell(task: StoryTask, key: str, agent: data.ActiveAgent | None) -> str:
        """The agent column for one task row. Prefer the live agent when it is
        driving THIS task, else the last session that stamped an adapter identity
        (#153 phase 1), else "-" (no live agent and no stamped history)."""
        if agent is not None and agent.story_key == key:
            return agent_label(agent.name, agent.model, agent.binary)
        for record in reversed(task.sessions):
            if record.adapter:
                return agent_label(record.adapter, record.model, record.binary)
        return "-"

    def _apply_attention(self, table: DataTable, runs: list[data.RunInfo]) -> None:
        """Global attention indicator: how many runs are paused awaiting a human.
        Shown on the runs-table border title, consistent with the per-run pause
        badge and the ATTENTION-file notify machinery."""
        waiting = sum(1 for r in runs if r.status == data.PAUSED)
        table.border_title = f"Runs — ⚑ {waiting} need attention" if waiting else "Runs"

    def _apply_board(self, snap: _Snapshot) -> None:
        """Toggle the sprint tree vs the stories board by the selected run's mode
        and refresh whichever is live. The stories board is per-run (keyed by the
        run's spec folder); the sprint tree is project-level and painted
        separately on rescan."""
        sprint_tree = self.query_one("#sprint-tree", SprintTree)
        stories_table = self.query_one("#stories-table", StoriesTable)
        sprint_tree.display = not snap.stories_mode
        stories_table.display = snap.stories_mode
        # The splitter above this slot carries its section title; set_label already
        # no-ops when the label is unchanged (this runs every poll tick).
        self.query_one("#split-runs", Splitter).set_label(
            "Stories" if snap.stories_mode else "Sprint"
        )
        if snap.stories_mode:
            stories_table.update_stories(snap.stories)

    def _apply_sprint_tree(self, ss: sprintstatus.SprintStatus | None) -> None:
        if ss is self._last_sprint:
            return
        self._last_sprint = ss
        self.query_one("#sprint-tree", SprintTree).update_sprint(ss)

    def _apply_deferred(self, items: list[data.DeferredItem] | None) -> None:
        if items is self._last_deferred:
            return
        self._last_deferred = items
        deferred = self.query_one("#deferred", OptionList)
        highlighted_id: str | None = None
        if deferred.highlighted is not None:
            highlighted_id = deferred.get_option_at_index(deferred.highlighted).id
        deferred.clear_options()
        if not items:
            label = "no deferred work" if items is not None else "deferred ledger unavailable"
            deferred.add_option(Option(Text(label, style="dim"), disabled=True))
            return
        seen_ids: set[str] = set()
        for item in items:
            key = item.option_key or item.id
            option_id = key if key not in seen_ids else None
            seen_ids.add(key)
            deferred.add_option(DeferredEntryOption(item, option_id))
        if highlighted_id is not None:
            try:
                deferred.highlighted = deferred.get_option_index(highlighted_id)
            except OptionDoesNotExist:
                pass

    def _apply_missed_decisions(self, count: int) -> None:
        deferred = self.query_one("#deferred", OptionList)
        deferred.border_title = (
            f"Deferred Work — {count} to answer (d)" if count else "Deferred Work"
        )
