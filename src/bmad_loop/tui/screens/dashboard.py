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
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
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

from ... import sprintstatus, stories
from ...model import RunState
from ...runs import RUNS_DIR
from .. import data
from ..widgets import (
    DeferredEntryOption,
    JournalEntryOption,
    RunHeader,
    SelectableRichLog,
    SprintTree,
    StoriesTable,
    pause_tag,
    status_cell,
)
from .modals import DeferredEntryModal

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
    ]

    def __init__(self, project: Path):
        super().__init__()
        self.project = project
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
                runs.border_title = "Runs"
                yield runs
                tree = SprintTree("sprint", id="sprint-tree")
                tree.border_title = "Sprint"
                yield tree
                stories = StoriesTable(id="stories-table")
                stories.border_title = "Stories"
                yield stories
                deferred = OptionList(id="deferred")
                deferred.border_title = "Deferred Work"
                yield deferred
            with Vertical(id="detail"):
                yield RunHeader(id="runheader")
                yield DataTable(id="tasks", cursor_type="row")
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
        tasks.add_column("dev", key="dev", width=5)
        tasks.add_column("review", key="review", width=6)
        tasks.add_column("tokens", key="tokens", width=12)
        tasks.add_column("info", key="info")
        tasks.add_column("raw", key="raw", width=13)
        self.query_one("#runheader", RunHeader).show_empty(self.project)
        self.set_interval(1.0, self._tick)
        self._tick()

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
        if task == self._displayed_log_task and self._log_index is not None:
            self._scroll_log_to(self._log_index.line_for_offset(pos))
            return
        # another session's log (or this one not rendered yet): pin it and
        # finish the jump once a poll has fed and rendered that file
        self._pending_jump = (task, pos)
        if task != self._displayed_log_task:
            self._pin_task = task
        self._tick(force_rescan=False)

    def _scroll_log_to(self, line: int | None, attempts: int = 60) -> None:
        if line is None:
            self.notify("log is empty or not loaded yet", severity="warning")
            return
        log = self.query_one("#log", RichLog)
        target = line + 1  # the '— task.log —' header row above the render
        if log.virtual_size.height <= target:
            # A previously hidden RichLog defers writes until the tab switch
            # gives it a size, and that flush applies its own scroll_end.
            # Wait for the flush (content taller than the target proves it)
            # so our scroll lands after it instead of being stomped.
            if attempts > 0:
                self.set_timer(0.05, lambda: self._scroll_log_to(line, attempts - 1))
            return
        viewport = max(1, log.scrollable_content_region.height)
        log.scroll_to(y=max(0, target - viewport // 2), animate=False)

    def action_unpin_log(self) -> None:
        if self._pin_task is None and self._pending_jump is None:
            return
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

    # --------------------------------------------------------------- polling

    def _tick(self, force_rescan: bool | None = None) -> None:
        if self._pending_run is not None and time.monotonic() > self._pending_deadline:
            self._pending_run = None
            self.notify(
                "launch may have failed — attach to tmux session bmad-loop-ctl",
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
            header.show_run(snap.run_id, snap.status, snap.state, snap.decision)
        self._apply_board(snap)
        if snap.state is not None:
            self._apply_tasks(snap.state)
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
        if (
            self._pending_jump is not None
            and snap.log_task == self._pending_jump[0]
            and self._log_index is not None
        ):
            _, pos = self._pending_jump
            self._pending_jump = None
            self._scroll_log_to(self._log_index.line_for_offset(pos))

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
            if run.run_id in self._run_rows:
                table.update_cell(run.run_id, "st", status_cell(run.status))
                table.update_cell(run.run_id, "note", pause_tag(run.paused_stage))
            else:
                table.add_row(
                    status_cell(run.status),
                    run.run_id,
                    run.run_type,
                    pause_tag(run.paused_stage),
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

    def _apply_tasks(self, state: RunState) -> None:
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
