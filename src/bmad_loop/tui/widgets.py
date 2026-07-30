"""Small presentation widgets for the dashboard.

Rendering builds rich Text objects rather than markup strings: pause reasons,
defer reasons and journal fields are arbitrary engine output and must never be
interpreted as markup.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import DataTable, RichLog, Static, Tree
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from .. import policy
from ..model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_PLAN_CHECKPOINT,
    PAUSE_SPEC_APPROVAL,
    PAUSE_STORY_CHECKPOINT,
    PAUSE_STORY_GATE,
    Phase,
    RunState,
)
from ..sprintstatus import SprintStatus, Story
from ..stories import StoryRow
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


# ------------------------------------------------------------- pause badges
#
# Every mid-run pause that awaits a human maps to a visually distinct badge,
# shown as a short tag in the runs table and a full label in the run header.
# (short tag, full label, rich style)
_PAUSE_BADGES: dict[str, tuple[str, str, str]] = {
    PAUSE_PLAN_CHECKPOINT: ("plan", "plan checkpoint", "magenta"),
    PAUSE_STORY_CHECKPOINT: ("story", "story checkpoint", "cyan"),
    PAUSE_SPEC_APPROVAL: ("spec", "spec-approval gate", "yellow"),
    PAUSE_EPIC_BOUNDARY: ("epic", "epic gate", "yellow"),
    PAUSE_STORY_GATE: ("gate", "story gate", "yellow"),
    PAUSE_ESCALATION: ("esc", "escalation", "bold red"),
}


def pause_tag(stage: str) -> Text:
    """Compact colored tag for a paused run in the runs table ('' when not
    paused / unknown stage renders the raw stage)."""
    if not stage:
        return Text("")
    tag, _label, style = _PAUSE_BADGES.get(stage, (stage, stage, "yellow"))
    return Text(tag, style=style)


def pause_label(stage: str) -> tuple[str, str]:
    """(full label, rich style) for a pause stage, for the run-header badge."""
    _tag, label, style = _PAUSE_BADGES.get(stage, (stage, stage, "yellow"))
    return label, style


def stopping_tag() -> Text:
    """Compact tag for a run with a graceful stop pending, shown in the runs-table
    note cell in place of a pause badge. The glyph + style match
    STATUS_GLYPHS/STATUS_STYLES[STOPPED] — the end state a graceful stop lands in."""
    return Text("⏹ stop", style=STATUS_STYLES[data.STOPPED])


def agent_label(name: str, model: str, binary: str = "") -> str:
    """Compact ``name·model`` label for a resolved adapter (e.g. "claude·opus"),
    or just the name when no explicit model is recorded (``model == ""`` means the
    session ran the CLI profile's default). Shared by the header's agent line and
    the tasks table's agent cell so the two never drift.

    A binary override renders as ``name(binary)·model`` — "claude(cc-work)·opus".
    Both facts are load-bearing and neither substitutes for the other: the profile
    explains the session's behavior (hook dialect, prompt template, bypass flags),
    the binary explains which install/account it billed. ``binary == ""`` means
    the profile's own executable, so an un-overridden run renders exactly as it
    did before the field existed."""
    head = f"{name}({binary})" if binary else name
    return f"{head}·{model}" if model else head


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
            " — if nothing appears, attach to control session bmad-loop-ctl",
            style="dim",
        )
        self.update(text)

    def show_run(
        self,
        run_id: str,
        status: str,
        state: RunState | None,
        decision: tuple[str, str] | None = None,
        stopping: bool = False,
        agent: data.ActiveAgent | None = None,
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

        counts = {Phase.DONE: 0, Phase.DEFERRED: 0, Phase.ESCALATED: 0, Phase.AWAITING_OPERATOR: 0}
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
        # Only when non-zero, unlike the three above: the header line is already
        # at the width the narrowest supported pane can hold, and a standing
        # "awaiting 0" would cost that room on every run that never parks a story.
        if counts[Phase.AWAITING_OPERATOR]:
            text.append(f"  awaiting {counts[Phase.AWAITING_OPERATOR]}", style="yellow")
        text.append(f"  {weighted:,} tokens ({raw:,} raw)", style="dim")

        # The agent line: who is driving (or, when idle, who is configured to).
        if agent is not None:
            # A session is open: show the live agent, its model and stage role.
            text.append("\nagent ", style="dim")
            # name(binary) stays one bold token: the executable is part of the
            # agent's identity, not a separate attribute like model or role.
            text.append(
                f"{agent.name}({agent.binary})" if agent.binary else agent.name, style="bold cyan"
            )
            if agent.model:
                text.append(f" · {agent.model}", style="cyan")
            if agent.role:
                text.append(f" · {agent.role}", style="dim")
        else:
            # No session open: show the configured adapters from the run's policy
            # snapshot. Skip the line entirely when the snapshot can't be rebuilt
            # (a pre-#153 run) rather than fabricate an all-defaults "claude".
            rebuilt = policy.adapter_policy_from_snapshot(state.policy_snapshot)
            if rebuilt is not None:
                dev = rebuilt.resolved("dev")
                review = rebuilt.resolved("review")
                dev_label = agent_label(dev.name, dev.model, dev.binary)
                review_label = agent_label(review.name, review.model, review.binary)
                if dev_label == review_label:
                    line = f"\nagents {dev_label}"  # dev and review resolve alike
                else:
                    line = f"\nagents dev {dev_label} review {review_label}"
                if state.run_type == "sweep":
                    triage = rebuilt.resolved("triage")
                    line += f" triage {agent_label(triage.name, triage.model, triage.binary)}"
                text.append(line, style="dim")

        if stopping:
            # A RUNNING or UNKNOWN run with a pending graceful-stop request: it never
            # enters the PAUSED/CRASHED/INTERRUPTED branches below, so this stands on
            # its own.
            text.append(
                "\n⏹ graceful stop pending — will stop after the current item",
                style="bold yellow",
            )

        if status == data.PAUSED:
            text.append("\n⏸ paused", style="bold yellow")
            if state.paused_stage:
                label, badge_style = pause_label(state.paused_stage)
                text.append("  ")
                text.append(f"[{label}]", style=f"bold {badge_style}")
            if state.paused_reason:
                text.append(f" — {state.paused_reason}", style="yellow")
            # p opens the stage-appropriate review viewer; e resumes; R resolves
            # an escalation (the header only hints the common paths).
            text.append("\n  press p to review · e to resume", style="dim")
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


# --------------------------------------------------------- validate findings
#
# A structural rendering of the `validate --json` document (documents.py), for
# the TUI's validate modal. The text mode's severities are string prefixes and
# its verdict is an exit code; here both are fields, and the `detail` each check
# carried before it flattened itself into a sentence is renderable.

# The document schema version this renderer was written against — a HAND-WRITTEN
# literal, deliberately *not* an import of documents.VALIDATE_SCHEMA_VERSION.
#
# This is load-bearing. An import would auto-follow a CLI schema bump, and the
# fields read below would very likely still resolve against a v2 document, so
# the result would not be a refusal — it would be a quietly wrong modal in a
# user's terminal, with the suite green. Pinning the literal makes a bump a
# deliberate edit *here*, after re-reading this renderer against the new
# document, and a tripwire test fails the moment the two diverge.
_RENDERS_VALIDATE_SCHEMA = 1

# Finding severities (checks.py: ok/warning/problem). A DIFFERENT vocabulary
# from _SEVERITY_STYLES further down, which styles deferred-work items
# (critical/high/medium/low) — separate maps on purpose, no reuse. Both are
# read through .get() with a fallback: severity arrives from a subprocess
# document, so an unrecognised string must render neutrally, never KeyError.
_FINDING_STYLES = {
    "ok": "green",
    "warning": "yellow",
    "problem": "bold red",
}

_FINDING_GLYPHS = {
    "ok": "✓",
    "warning": "!",
    "problem": "✖",
}

# Row-grid geometry, mirroring the journal's. The check column is wide enough
# for the longest id in checks.VALIDATE_CHECKS ("queue.sprint-status-unknown-keys")
# so ids do not fold in practice; it folds rather than truncates if one grows.
# The message column's left edge is the sum of everything before it, and the
# alignment test derives its indent from these same constants so the two cannot
# silently drift apart.
_FINDING_GLYPH_WIDTH = 1
_FINDING_CHECK_WIDTH = 32
_FINDING_COL_PAD = 1  # per-column right pad in the row grid


def validate_document(stdout: str) -> dict | None:
    """Parse `validate --json` stdout into a document this renderer can draw.

    Returns ``None`` — **never raises** — for anything undrawable: unparseable
    stdout, a schema version this renderer was not written against, or a
    document whose shape is not the one the renderer walks. The caller's
    degrade is then a value check rather than an exception, which matters
    because the caller runs on a worker thread where an escaping exception
    takes the app down.

    The version check is an equality, not a ``>=``: a *newer* document is
    exactly the case that must degrade rather than be half-rendered.
    """
    try:
        doc = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("schema_version") != _RENDERS_VALIDATE_SCHEMA:
        return None
    if not isinstance(doc.get("findings"), list):
        return None
    if not isinstance(doc.get("counts"), dict):
        return None
    return doc


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _detail_scalar(value: Any) -> str:
    """One leaf as text, in JSON's spelling — ``true``/``null``, not Python's
    ``True``/``None`` — so a rendered detail reads as the document it came from."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _detail_pairs(mapping: dict) -> str:
    return ", ".join(f"{k}={_detail_scalar(v)}" for k, v in mapping.items())


def _json_leaf(value: Any) -> str:
    """A value too deep or too odd for the depth-2 walk, as JSON.

    Never ``str()``: that prints a Python repr (``{'dev': 'claude'}``) at the
    user, which is the exact tell that a renderer met a shape it did not model.
    """
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # unreachable for json.loads output; this renders, never raises
        return str(value)


def _detail_lines(detail: Any) -> list[str]:
    """A finding's ``detail`` as flat text rows: **depth-2, not recursive.**

    The real shapes, enumerated from every check site (cli.py's validate gates
    and platform preflight, install.py's skill probes): ``detail`` is one dict
    whose values are a scalar (``str``/``int``/``bool``/``None`` — ``mux.backend``'s
    ``version`` is ``str | None``), a list of scalars (``missing_markers``,
    ``unknown_keys``, ``trees``), a dict of scalars (``policy``'s ``adapters`` —
    a nested dict, on the *passing* path, in every successful validate), or a
    list of dicts of scalars (``mux.backends-detected``, six keys per backend).
    Two levels covers all of them.

    Anything deeper or otherwise unmodelled falls back to :func:`_json_leaf`
    rather than recursing: recursion is more machinery than this data justifies
    and is unbounded for a payload nobody has written yet, whereas the fallback
    is correct for any depth and still legible.
    """
    if not isinstance(detail, dict):
        # detail is `dict | None` by contract; anything else still renders.
        return [] if detail is None else [_json_leaf(detail)]
    lines: list[str] = []
    for key, value in detail.items():
        if _is_scalar(value):
            lines.append(f"{key}: {_detail_scalar(value)}")
        elif isinstance(value, list) and all(_is_scalar(v) for v in value):
            lines.append(f"{key}: {', '.join(_detail_scalar(v) for v in value) or '—'}")
        elif isinstance(value, dict) and all(_is_scalar(v) for v in value.values()):
            lines.append(f"{key}: {_detail_pairs(value) or '—'}")
        elif isinstance(value, list) and all(
            isinstance(v, dict) and all(_is_scalar(x) for x in v.values()) for v in value
        ):
            # list[dict] gets a line per entry, so six-key backend rows stay
            # readable instead of becoming one unreadable joined string.
            lines.append(f"{key}:")
            lines.extend(f"  {_detail_pairs(v) or '—'}" for v in value)
        else:
            lines.append(f"{key}: {_json_leaf(value)}")
    return lines


def _finding_rows(finding: Any, *, details: bool) -> list[tuple[Text, Text, Text]]:
    """One finding as its grid rows: the finding itself, then its detail lines.

    Detail shows inline for ``warning`` and ``problem`` — the findings someone
    opened the modal to act on — and for everything when ``details``. One
    severity rule, and zero matching on ``check`` ids: ids are the contracted
    identity, but keying layout off them would make every new check a renderer
    edit.
    """
    if not isinstance(finding, dict):
        raise TypeError("finding is not a dict")
    severity = finding.get("severity")
    if not isinstance(severity, str):
        severity = ""
    style = _FINDING_STYLES.get(severity, "")
    check = finding.get("check")
    rows = [
        (
            Text(_FINDING_GLYPHS.get(severity, "?"), style=style),
            Text("?" if check is None else str(check), style=style),
            Text(str(finding.get("message", ""))),
        )
    ]
    if details or severity in ("warning", "problem"):
        rows += [
            (Text(""), Text(""), Text(line, style="dim"))
            for line in _detail_lines(finding.get("detail"))
        ]
    return rows


def validate_findings(doc: dict, *, details: bool) -> Table:
    """Every finding as **one** grid: glyph, ``check`` id, message + detail rows.

    One grid for all findings rather than one per finding, which is what buys
    column alignment *across* findings — the check ids line up into a readable
    column instead of each row sizing itself. (journal_line builds a grid per
    row for the mirror-image reason: there each row is alone and only its own
    columns need to align.)

    Two separate mechanisms keep it legible, both load-bearing:

    - **The grid itself** handles multi-line messages. ``message`` may carry
      newlines — the config, sprint-status and stories loaders put a PyYAML
      ``MarkedYAMLError`` straight into ``str(e)`` — and in a flat ``Text`` an
      embedded newline returns to column 0, destroying the alignment of every
      row below it. In a cell it stays inside its column.
    - **``overflow="fold"``** handles the long *unbroken* runs that wrapping
      cannot break: absolute paths, joined marker lists, a ``check`` id longer
      than its column. Folding wraps them; the default would truncate with an
      ellipsis, silently dropping the end of a path someone needs to read.

    Defensive **per finding**: a finding that is not a dict, or whose fields
    are not what this walks, costs its own placeholder row and nothing more.
    One malformed entry must not blank the modal a reader is using to find out
    what is wrong.
    """
    grid = Table.grid(padding=(0, _FINDING_COL_PAD, 0, 0))
    grid.add_column(width=_FINDING_GLYPH_WIDTH)
    grid.add_column(width=_FINDING_CHECK_WIDTH, overflow="fold")
    grid.add_column(overflow="fold")
    findings = doc.get("findings")
    if not isinstance(findings, list):
        findings = []
    for finding in findings:
        try:
            rows = _finding_rows(finding, details=details)
        except Exception:  # a bad finding costs its row, never the modal
            rows = [
                (
                    Text("?", style="dim"),
                    Text("?", style="dim"),
                    Text("(unreadable finding)", style="dim"),
                )
            ]
        for row in rows:
            grid.add_row(*row)
    return grid


def validate_header(doc: dict) -> Text:
    """The verdict line, built from the document's ``ok`` — never from the
    subprocess exit code, which conflates "checks failed" with "the command
    broke". The document is the thing that actually knows which happened.

    When any problem is present, a dim footer says the gates are chained. That
    puts documents.py's "absence is not a pass" on screen: a policy failure
    leaves the binary, hook and skill gates emitting *nothing at all*, so a
    short findings list after a failure is not a short list of problems. It is
    the one thing this rendering can teach that the text mode cannot.
    """
    ok = doc.get("ok")
    text = Text()
    if ok is True:
        text.append("✓ validate passed", style="green")
    elif ok is False:
        text.append("✖ validate failed", style="bold red")
    else:
        text.append("? validate verdict unknown", style="dim")

    counts = doc.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    tallies = [
        f"{counts[severity]} {severity}"
        for severity in ("ok", "warning", "problem")
        if isinstance(counts.get(severity), int) and not isinstance(counts.get(severity), bool)
    ]
    if tallies:
        text.append("  " + " · ".join(tallies), style="dim")

    meta = []
    mode = doc.get("mode")
    if isinstance(mode, str) and mode:
        meta.append(f"mode: {mode}")
    # spec_folder is user-controlled; it goes into a Text, never into markup.
    spec_folder = doc.get("spec_folder")
    if isinstance(spec_folder, str) and spec_folder:
        meta.append(f"spec: {spec_folder}")
    if meta:
        text.append("\n" + " · ".join(meta), style="dim")

    problems = counts.get("problem")
    if isinstance(problems, int) and not isinstance(problems, bool) and problems > 0:
        text.append(
            "\ngates are chained — checks after a failure may not have run",
            style="dim italic",
        )
    return text


# ------------------------------------------------------------- sprint tree

# Story/retro statuses -> glyph + style. Statuses come from an LLM-maintained
# file, so lookups always .get() with a "?"/dim fallback, never KeyError.
SPRINT_GLYPHS = {
    "done": "✓",
    "in-progress": "▶",
    "review": "◆",
    "ready-for-dev": "○",
    "awaiting-operator": "⏸",
    "backlog": "·",
    "optional": "·",
}

SPRINT_STYLES = {
    "done": "green",
    "in-progress": "cyan",
    "review": "magenta",
    "ready-for-dev": "cyan",
    # yellow, matching the run header's deferred count: work is finished but the
    # story is not, and something outside the loop has to happen next.
    "awaiting-operator": "yellow",
    "backlog": "dim",
    "optional": "dim",
}


def sprint_story_label(story: Story) -> Text:
    glyph = SPRINT_GLYPHS.get(story.status, "?")
    style = SPRINT_STYLES.get(story.status, "dim")
    return Text(f"{glyph} {story.num}{story.suffix}-{story.slug}", style=style)


def sprint_retro_label(status: str) -> Text:
    glyph = SPRINT_GLYPHS.get(status, "?")
    style = SPRINT_STYLES.get(status, "dim")
    return Text(f"{glyph} retrospective", style=style)


def sprint_epic_label(num: int, status: str, done: int, total: int) -> Text:
    complete = status == "done" or (total > 0 and done == total)
    text = Text()
    text.append(f"Epic {num}", style="green" if complete else "bold")
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
        stories_by_epic: dict[int, list[Story]] = {}
        for story in ss.stories:
            stories_by_epic.setdefault(story.epic, []).append(story)
        epic_nums = sorted(set(ss.epics) | set(stories_by_epic) | set(ss.retros))
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
                node = self.root.add(label, data=f"epic-{num}")
                self._epic_nodes[num] = node
            else:
                node.set_label(label)
            child_keys = tuple(s.key for s in stories)
            child_labels = [sprint_story_label(s) for s in stories]
            if retro is not None:
                child_keys += (f"epic-{num}-retrospective",)
                child_labels.append(sprint_retro_label(retro))
            if self._epic_child_keys.get(num) == child_keys:
                for child, child_label in zip(node.children, child_labels):
                    child.set_label(child_label)
            else:
                node.remove_children()
                for key, child_label in zip(child_keys, child_labels):
                    node.add_leaf(child_label, data=key)
                self._epic_child_keys[num] = child_keys


# ------------------------------------------------------------- stories table

# Story on-disk state (stories.state_label) -> glyph + style. The label may be a
# `sentinel:<kind>` composite, so lookups key on the token before ':'.
STORY_GLYPHS = {
    "pending": "·",
    "draft": "◦",
    "ready-for-dev": "○",
    "in-progress": "▶",
    "in-review": "◆",
    "done": "✓",
    "awaiting-operator": "⏸",
    "blocked": "✖",
    "ambiguous": "⚠",
    "sentinel": "⚠",
}

STORY_STYLES = {
    "pending": "dim",
    "draft": "dim",
    "ready-for-dev": "cyan",
    "in-progress": "cyan",
    "in-review": "magenta",
    "done": "green",
    "awaiting-operator": "yellow",
    "blocked": "bold red",
    "ambiguous": "bold red",
    "sentinel": "bold red",
}


def story_state_cell(label: str) -> Text:
    key = label.split(":", 1)[0]
    return Text(f"{STORY_GLYPHS.get(key, '?')} {label}", style=STORY_STYLES.get(key, "dim"))


def story_checkpoint_cell(spec_checkpoint: bool, done_checkpoint: bool) -> Text:
    """Independent spec/done checkpoint markers as one compact cell: `S` (plan
    review before code, magenta), `D` (review after commit, cyan), dim `·` for an
    unset slot so the two stay positionally readable."""
    text = Text()
    text.append("S" if spec_checkpoint else "·", style="magenta" if spec_checkpoint else "dim")
    text.append("D" if done_checkpoint else "·", style="cyan" if done_checkpoint else "dim")
    return text


class StoriesTable(DataTable):
    """The stories-mode board — one row per stories.yaml entry with its live
    on-disk state, replacing the sprint tree when a stories-mode run is selected.

    Reconciles in place each rescan tick (stable row key = story id): the id set
    is stable within a run, so existing rows only get cell updates, keeping the
    cursor. Rebuilds only when the id set/order actually changes (a between-runs
    Story Breakdown re-derive)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cursor_type = "row"
        self.zebra_stripes = True
        self._row_ids: list[str] | None = None  # None: placeholder/empty shown

    def on_mount(self) -> None:
        self.add_column("state", key="state", width=15)
        self.add_column("id", key="id", width=8)
        self.add_column("✓", key="chk", width=2)
        self.add_column("title", key="title")

    def _placeholder(self, label: str) -> None:
        self.clear()
        self.add_row(Text(label, style="dim"), "", "", "")
        self._row_ids = None

    def update_stories(self, rows: list[StoryRow] | None) -> None:
        if rows is None:
            self._placeholder("stories board unavailable")
            return
        if not rows:
            self._placeholder("no stories")
            return
        ids = [r.id for r in rows]
        if self._row_ids != ids:
            self.clear()
            for r in rows:
                self.add_row(
                    story_state_cell(r.label),
                    r.id,
                    story_checkpoint_cell(r.spec_checkpoint, r.done_checkpoint),
                    _short(r.title, 48),
                    key=r.id,
                )
            self._row_ids = ids
            return
        for r in rows:
            self.update_cell(r.id, "state", story_state_cell(r.label))
            self.update_cell(
                r.id, "chk", story_checkpoint_cell(r.spec_checkpoint, r.done_checkpoint)
            )
            self.update_cell(r.id, "title", _short(r.title, 48))


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


class Splitter(Widget):
    """A draggable divider between two panes, drawn as a thin line so it reads
    like the static border it replaces (not a filled bar). A ``horizontal``
    splitter is a 1-row rule between vertically stacked panes (drag up/down); a
    vertical one is a 1-column ``│`` line between side-by-side panes (drag
    left/right). A horizontal splitter's ``label`` rides the rule as ``─ Sprint
    ──`` so the section title that used to sit on the pane's ``border-top``
    survives its removal.

    The widget only measures a drag as a signed cell ``delta`` along its axis and
    hands it to ``apply`` — the screen bakes in the sign and which reactive the
    boundary moves. ``bump`` is the keyboard entry point (resize mode), so mouse
    and keyboard drive the exact same code path. ``on_release`` fires once a drag
    ends, for the screen to persist the new geometry.
    """

    DEFAULT_CSS = """
    Splitter {
        color: $primary-darken-2;  /* the line color; matches the old borders */
        background: transparent;
    }
    Splitter.-vertical {
        width: 1;
        height: 1fr;
    }
    Splitter.-horizontal {
        height: 1;
        width: 1fr;
    }
    Splitter:hover, Splitter.-active, Splitter.-dragging {
        color: $accent;  /* brighten the line while hovered / grabbed */
    }
    """

    def __init__(
        self,
        *,
        horizontal: bool,
        apply: Callable[[int], None],
        on_release: Callable[[], None],
        label: str = "",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._horizontal = horizontal
        self._apply = apply
        self._on_release = on_release
        self._label = label
        self._last: int | None = None  # last drag coordinate; None = not dragging
        self.add_class("-horizontal" if horizontal else "-vertical")
        self.tooltip = "drag to resize (or Ctrl+W)"

    @property
    def label(self) -> str:
        return self._label

    def set_label(self, label: str) -> None:
        if label != self._label:
            self._label = label
            self.refresh()

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if width <= 0:
            return Strip([])
        base = self.rich_style
        if not self._horizontal:
            return Strip([Segment("│", base)], width)
        if self._label:
            # `─ Label ───…` — a left-set title on a box-drawing rule, echoing the
            # old border-title. The label is bold so it reads as a heading.
            head = f"─ {self._label} "[:width]
            tail = "─" * max(0, width - len(head))
            return Strip([Segment(head, base + Style(bold=True)), Segment(tail, base)], width)
        return Strip([Segment("─" * width, base)], width)

    def _coord(self, event: events.MouseEvent) -> int:
        return event.screen_y if self._horizontal else event.screen_x

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._last = self._coord(event)
        self.capture_mouse()
        self.add_class("-dragging")
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._last is None:
            return  # hover, not a drag
        pos = self._coord(event)
        delta = pos - self._last
        if delta:
            self._last = pos
            self._apply(delta)
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._last is None:
            return
        self._last = None
        self.release_mouse()
        self.remove_class("-dragging")
        self._on_release()
        event.stop()

    def bump(self, delta: int) -> None:
        """Keyboard-driven nudge (resize mode). Persistence is handled by the
        screen when the mode exits, so this does not call ``on_release``."""
        self._apply(delta)
