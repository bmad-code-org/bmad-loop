"""Small presentation widgets for the dashboard.

Rendering builds rich Text objects rather than markup strings: pause reasons,
defer reasons and journal fields are arbitrary engine output and must never be
interpreted as markup.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from rich.table import Table
from rich.text import Text
from textual.selection import Selection
from textual.widgets import RichLog, Static, Tree
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from ..model import Phase, RunState
from ..sprintstatus import SprintStatus, Story
from . import data

STATUS_GLYPHS = {
    data.RUNNING: "▶",
    data.PAUSED: "⏸",
    data.FINISHED: "✔",
    data.STOPPED: "⏹",
    data.CRASHED: "✖",
    data.INTERRUPTED: "✖",
    data.UNKNOWN: "?",
}

STATUS_STYLES = {
    data.RUNNING: "green",
    data.PAUSED: "yellow",
    data.FINISHED: "dim",
    data.STOPPED: "bold yellow",
    data.CRASHED: "bold red",
    data.INTERRUPTED: "bold red",
    data.UNKNOWN: "dim",
}


def status_cell(status: str) -> Text:
    return Text(STATUS_GLYPHS.get(status, "?"), style=STATUS_STYLES.get(status, ""))


class RunHeader(Static):
    """One-glance summary of the selected run, or the empty-state hint."""

    def show_empty(self, project: Path) -> None:
        text = Text()
        text.append("no runs found", style="bold")
        text.append(f"  ({project})\n", style="dim")
        text.append(
            "start one with `bmad-loop run` or `bmad-loop sweep`"
            " — or `bmad-loop init` if this project is not set up yet",
            style="dim",
        )
        self.update(text)

    def show_starting(self, run_id: str) -> None:
        text = Text()
        text.append(run_id, style="bold")
        text.append("  ⧗ starting…", style="yellow")
        text.append(
            "\nwaiting for the engine to write state.json"
            " — if nothing appears, attach to tmux session bmad-loop-ctl",
            style="dim",
        )
        self.update(text)

    def show_run(
        self,
        run_id: str,
        status: str,
        state: RunState | None,
        decision: tuple[str, str] | None = None,
    ) -> None:
        text = Text()
        text.append(run_id, style="bold")
        if state is not None and state.run_type != "story":
            text.append(f" [{state.run_type}]")
        text.append("  ")
        text.append(
            f"{STATUS_GLYPHS.get(status, '?')} {status}",
            style=STATUS_STYLES.get(status, ""),
        )
        if state is None:
            text.append("\nstate unavailable", style="dim")
            self.update(text)
            return
        text.append(f"  started {state.started_at}", style="dim")
        if state.current_epic is not None:
            text.append(f"  epic {state.current_epic}", style="dim")

        counts = {Phase.DONE: 0, Phase.DEFERRED: 0, Phase.ESCALATED: 0}
        weight = state.cache_read_weight()
        weighted = raw = 0
        for task in state.tasks.values():
            if task.phase in counts:
                counts[task.phase] += 1
            weighted += task.tokens.weighted_total(weight)
            raw += task.tokens.total
        text.append("\n")
        text.append(f"tasks {len(state.tasks)}", style="dim")
        text.append(f"  done {counts[Phase.DONE]}", style="green")
        text.append(f"  deferred {counts[Phase.DEFERRED]}", style="yellow")
        style = "red" if counts[Phase.ESCALATED] else "dim"
        text.append(f"  escalated {counts[Phase.ESCALATED]}", style=style)
        text.append(f"  {weighted:,} tokens ({raw:,} raw)", style="dim")

        if status == data.PAUSED:
            text.append("\n⏸ paused", style="bold yellow")
            if state.paused_stage:
                text.append(f" ({state.paused_stage})", style="yellow")
            if state.paused_reason:
                text.append(f" — {state.paused_reason}", style="yellow")
            text.append("  · press e to resume", style="dim")
        elif status == data.CRASHED:
            text.append(
                "\n✖ engine crashed — see crash.txt · press e to resume",
                style="bold red",
            )
            if state.crash_error:
                text.append(f"\n  {state.crash_error}", style="red")
        elif status == data.INTERRUPTED:
            text.append(
                "\n✖ engine gone — run was interrupted · press e to resume",
                style="bold red",
            )
        if decision is not None and status not in (
            data.FINISHED,
            data.INTERRUPTED,
            data.CRASHED,
        ):
            dw_id, question = decision
            text.append(f"\n⚑ decision needed: {dw_id}", style="bold yellow")
            if question:
                text.append(f" — {_short(question, 100)}", style="yellow")
            text.append("\n  press a to attach and answer", style="bold yellow")
        self.update(text)


# ------------------------------------------------------------ journal lines

# kind substrings -> style, first match wins; anything else renders dim
_JOURNAL_STYLES = (
    ("escalation-resolved", "green"),  # positive — must precede the "escalat" -> red rule
    ("escalat", "red"),
    ("failed", "red"),
    ("done", "green"),
    ("complete", "green"),
    ("finished", "green"),
    ("decision", "yellow"),
    ("deferred", "yellow"),
    ("boundary", "yellow"),
    ("truncated", "yellow"),
    ("start", "cyan"),
    ("resume", "cyan"),
)


# metadata fields not worth a column on every line; log_task/log_pos drive
# the journal -> log jump, not the human
_JOURNAL_HIDDEN_FIELDS = ("ts", "kind", "log_task", "log_pos")

# Row-grid geometry. The fields column's left edge sits at
# _JOURNAL_CLOCK_WIDTH + _JOURNAL_COL_PAD + _JOURNAL_KIND_WIDTH + _JOURNAL_COL_PAD;
# the hanging-indent test derives its indent from the same constants so the two
# can't silently drift apart.
_JOURNAL_CLOCK_WIDTH = 8
_JOURNAL_KIND_WIDTH = 24
_JOURNAL_COL_PAD = 1  # per-column right pad in the row grid


def journal_line(entry: dict[str, Any]) -> Table:
    kind = str(entry.get("kind", "?"))
    style = next((s for sub, s in _JOURNAL_STYLES if sub in kind), "dim")
    ts = entry.get("ts")
    clock = ""
    if isinstance(ts, (int, float)):
        clock = time.strftime("%H:%M:%S", time.localtime(ts))
    fields = "  ".join(
        f"{k}={_short(v)}" for k, v in entry.items() if k not in _JOURNAL_HIDDEN_FIELDS
    )
    # A grid per row so the fields cell folds within its own column (hanging
    # indent) instead of wrapping back under the clock/kind columns. A long kind
    # likewise folds within its own column rather than spilling into the fields.
    grid = Table.grid(padding=(0, _JOURNAL_COL_PAD, 0, 0))
    grid.add_column(width=_JOURNAL_CLOCK_WIDTH)
    grid.add_column(width=_JOURNAL_KIND_WIDTH, overflow="fold")
    grid.add_column(overflow="fold")
    grid.add_row(Text(clock, style="dim"), Text(kind, style=style), Text(fields))
    return grid


class JournalEntryOption(Option):
    """One journal entry as an OptionList row; carries the raw entry so
    selecting it can jump to the entry's position in the pane log."""

    def __init__(self, entry: dict[str, Any]) -> None:
        super().__init__(journal_line(entry))
        self.entry = entry


def _short(value: Any, limit: int = 60) -> str:
    s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ------------------------------------------------------------- sprint tree

# Story/retro statuses -> glyph + style. Statuses come from an LLM-maintained
# file, so lookups always .get() with a "?"/dim fallback, never KeyError.
SPRINT_GLYPHS = {
    "done": "✓",
    "in-progress": "▶",
    "review": "◆",
    "ready-for-dev": "○",
    "backlog": "·",
    "optional": "·",
}

SPRINT_STYLES = {
    "done": "green",
    "in-progress": "cyan",
    "review": "magenta",
    "ready-for-dev": "cyan",
    "backlog": "dim",
    "optional": "dim",
}


def sprint_story_label(story: Story) -> Text:
    glyph = SPRINT_GLYPHS.get(story.status, "?")
    style = SPRINT_STYLES.get(story.status, "dim")
    # Original format was "{glyph} {num}-{slug}". When the story came from a
    # sprint / backlog with no numeric segment (e.g. bugfix-*), num is "" and
    # the rendered label would start with "-". Drop the num segment when empty
    # so the visible label is just "{glyph} {slug}".
    if story.num:
        text = f"{glyph} {story.num}-{story.slug}"
    else:
        text = f"{glyph} {story.slug}"
    return Text(text, style=style)


def sprint_retro_label(status: str) -> Text:
    glyph = SPRINT_GLYPHS.get(status, "?")
    style = SPRINT_STYLES.get(status, "dim")
    return Text(f"{glyph} retrospective", style=style)


def _epic_id_sort_key(epic_id: str) -> tuple:
    """Natural-order sort key for epic ids: numeric first ("1", "2", "10"),
    then letter-suffix ("1a", "1b", "18a"), then phase-suffix ("0-p2"). Used by
    :meth:`SprintTree.update_sprint` so the panel lists "Epic 1, 2, 3, 1a, 1b"
    in human order rather than alphabetic order ("1, 10, 11, 1a, 1b, 2").
    """
    # Match "<num>[<letter>][-p<num>]" parts and use (group, num, letter, ...).
    m = re.match(r"^(\d+)([a-z]?)(?:-p(\d+))?$", epic_id)
    if not m:
        return (1, 0, "", epic_id)
    n = int(m.group(1))
    letter = m.group(2) or ""
    p = int(m.group(3)) if m.group(3) is not None else -1
    return (0, n, letter, p, epic_id)


def sprint_epic_label(epic_id: str, status: str, done: int, total: int) -> Text:
    """Render an epic row label. ``epic_id`` is the canonical id (str):

    * "1", "1a", "0-p2" → "Epic 1", "Epic 1a", "Epic 0-p2"
    * "sprint-08" → "Sprint 08"
    * "backlog"   → "Backlog"

    Original signature took ``num: int``; we accept the str id and render
    based on prefix so the SprintTree can group letter-suffix / sprint /
    backlog epics alongside numeric ones.
    """
    complete = status == "done" or (total > 0 and done == total)
    text = Text()
    if epic_id == "backlog":
        label = "Backlog"
    elif epic_id.startswith("sprint-"):
        label = f"Sprint {epic_id.removeprefix('sprint-')}"
    elif epic_id.startswith("epic-"):
        label = f"Epic {epic_id.removeprefix('epic-')}"
    else:
        label = f"Epic {epic_id}"
    text.append(label, style="green" if complete else "bold")
    if total:
        text.append(f" · {done}/{total}", style="green" if complete else "dim")
    if complete:
        text.append(" ✓", style="green")
    return text


class SprintTree(Tree[str]):
    """Sprint status as expandable epics with their stories and retro.

    Refreshed every rescan tick, so updates reconcile in place: existing
    nodes only get set_label(), which keeps expansion state and the cursor.
    Children are rebuilt only when an epic's story set actually changes.
    Node data is the sprint-status key ("epic-2", "2-1-slug", ...)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.show_root = False
        self.guide_depth = 2
        self._epic_nodes: dict[int, TreeNode[str]] = {}
        self._epic_child_keys: dict[int, tuple[str, ...]] = {}
        self._placeholder = True
        self.update_sprint(None)

    def _show_placeholder(self, label: str) -> None:
        self.clear()
        self._epic_nodes.clear()
        self._epic_child_keys.clear()
        self.root.add_leaf(Text(label, style="dim"))
        self._placeholder = True

    def update_sprint(self, ss: SprintStatus | None) -> None:
        if ss is None:
            self._show_placeholder("sprint status unavailable")
            return
        stories_by_epic: dict[str, list[Story]] = {}
        for story in ss.stories:
            stories_by_epic.setdefault(story.epic, []).append(story)
        # Sort: numeric epics first (natural order), then letter-suffix, then
        # sprint-NN, then backlog last. We use a tuple sort key so the visual
        # grouping stays stable as the YAML evolves.
        def _sort_key(epic_id: str) -> tuple:
            if epic_id == "backlog":
                return (3, 0, epic_id)
            if epic_id.startswith("sprint-"):
                try:
                    n = int(epic_id.removeprefix("sprint-"))
                except ValueError:
                    n = 0
                return (2, n, epic_id)
            if epic_id.startswith("epic-"):
                return _epic_id_sort_key(epic_id.removeprefix("epic-"))
            return _epic_id_sort_key(epic_id)

        epic_nums = sorted(
            set(ss.epics) | set(stories_by_epic) | set(ss.retros), key=_sort_key
        )
        if not epic_nums:
            self._show_placeholder("no sprint data")
            return
        if self._placeholder:
            self.clear()
            self._placeholder = False
        for num in [n for n in self._epic_nodes if n not in epic_nums]:
            self._epic_nodes.pop(num).remove()
            self._epic_child_keys.pop(num, None)
        for num in epic_nums:
            stories = stories_by_epic.get(num, [])
            retro = ss.retros.get(num)
            label = sprint_epic_label(
                num,
                ss.epics.get(num, ""),
                sum(s.status == "done" for s in stories),
                len(stories),
            )
            node = self._epic_nodes.get(num)
            if node is None:
                # Node data is the canonical epic id; downstream code only
                # stores it (no read-by-data consumers exist in the TUI today),
                # so the old "epic-{num}" prefix is no longer needed.
                node = self.root.add(label, data=num)
                self._epic_nodes[num] = node
            else:
                node.set_label(label)
            child_keys = tuple(s.key for s in stories)
            child_labels = [sprint_story_label(s) for s in stories]
            if retro is not None:
                # The retrospective child node data follows the same canonical
                # form as the YAML literal key (e.g. "1a-retrospective",
                # "sprint-08-retrospective", "backlog-retrospective" if any).
                child_keys += (f"{num}-retrospective",)
                child_labels.append(sprint_retro_label(retro))
            if self._epic_child_keys.get(num) == child_keys:
                for child, child_label in zip(node.children, child_labels):
                    child.set_label(child_label)
            else:
                node.remove_children()
                for key, child_label in zip(child_keys, child_labels):
                    node.add_leaf(child_label, data=key)
                self._epic_child_keys[num] = child_keys


# ------------------------------------------------------------ deferred work

_SEVERITY_STYLES = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "dim",
}


def deferred_line(item: data.DeferredItem) -> Text:
    # single-line; the pane's text-wrap/text-overflow CSS truncates with "…"
    text = Text()
    if item.done:
        text.append(f"{item.id} ✓ {item.title}", style="green")
    else:
        text.append(f"{item.id} ", style="dim")
        text.append(item.title, style=_SEVERITY_STYLES.get(item.severity or "", ""))
    if item.legacy:
        text.append(" ·legacy", style="dim italic")
    return text


class DeferredEntryOption(Option):
    """One deferred-work entry as an OptionList row; carries the item so
    selecting it can show the full entry body. option_id is the DW id when
    unique in the ledger (used to restore the highlight across refreshes),
    None for forgiveness when an LLM wrote duplicate ids."""

    def __init__(self, item: data.DeferredItem, option_id: str | None = None) -> None:
        super().__init__(deferred_line(item), id=option_id)
        self.item = item


class SelectableRichLog(RichLog):
    """RichLog that supports Textual text selection + ctrl+c copy.

    Base RichLog caches rendered Strips rather than a single renderable, so the
    default Widget.get_selection returns None and ctrl+c copies nothing. Rebuild
    the plain text from the cached strips (as the builtin Log widget does) so
    click-drag selection and ctrl+c work. wrap=False (the default, kept by the
    dashboard) means one strip per logical row, so document line indices line up
    with selection offsets.
    """

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = "\n".join(strip.text for strip in self.lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self.refresh()
