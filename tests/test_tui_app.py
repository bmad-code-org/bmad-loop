"""Coarse Pilot smoke tests for the dashboard and run control. Fine-grained
data correctness lives in test_tui_data.py, exact launch argv in
test_tui_launch.py; here we only prove the wiring: app mounts, the run table
populates and auto-selects the newest run, selection switches the task table,
the journal pane picks up appended events on a poll, and the r/s/e/a/v
bindings drive modals into tui.launch calls (monkeypatched — no real tmux)."""

from __future__ import annotations

import dataclasses
import json
import os
import tomllib
from pathlib import Path

import pytest
from conftest import git, install_bmad_config, make_validate_document, write_sprint
from rich.console import Console
from rich.text import Text
from textual.events import MouseMove
from textual.geometry import Offset, Size
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

from bmad_loop import bmadconfig, documents
from bmad_loop import policy as policy_mod
from bmad_loop.adapters.multiplexer import MultiplexerError
from bmad_loop.journal import Journal, save_state
from bmad_loop.model import Phase, RunState, SessionRecord, StoryTask, TokenUsage
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
    A pending log jump survives skipped/starved ticks (each tick's _apply
    re-attempts it until it lands), so waiting on its effect is deterministic —
    no rerun markers needed on the journal-jump tests."""
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


# ------------------------------------------------ #210: validate --json renderer
#
# The pure seams: parsing a validate document and rendering it. No app is
# mounted here — these are the pieces the validate modal is built out of.

# The width the modal is laid out for.
_FINDING_WIDTH = 96


def render(renderable, width: int = _FINDING_WIDTH) -> str:
    """Rich renderable -> plain text, as journal_rows does for the journal."""
    console = Console(width=width)
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


async def test_journal_jump_survives_exhausted_scroll_retry_chain(project):
    # Regression for #178: the hidden #log pane defers its writes, and on a
    # starved runner the flush can outlive _scroll_log_to's whole retry chain.
    # The old code gave up silently and lost the jump forever; now the pending
    # jump survives exhaustion and the next poll tick re-attempts it. Exhaust
    # the chain deterministically (attempts=0 against the unflushed pane)
    # instead of relying on a contended runner to starve it for real.
    root = project.project
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


async def test_journal_jump_retry_recomputes_line_after_same_task_repaint(project):
    # A delayed retry must not reuse the line captured when the chain was
    # armed: a poll can repaint the same task's log mid-chain (history
    # eviction advances LogIndex.render_base), shifting the line a byte
    # offset maps to. The old code scrolled the stale line and cleared
    # _pending_jump, silencing the fresher chain. Each fire now recomputes
    # the line from the live index. Fully deterministic: the armed timer
    # callback is captured and invoked by hand — no reveal, no tick race.
    root = project.project
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


async def test_journal_jump_release_survives_flush_scroll_end_stomp(project):
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
    root = project.project
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
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: any("not clean" in m for m in notifications(app)))
        assert not calls


def _split_root_tui_project(project):
    """The #414 pair, written where the guard reads them. Deliberately left
    UNCOMMITTED: the guard is ordered ahead of the clean-tree gate exactly as
    `cmd_run` orders it, and a committed fixture could not tell the two orders
    apart."""
    install_bmad_config(project)
    cfg = project.project / "_bmad" / "bmm" / "config.yaml"
    cfg.write_text(cfg.read_text() + "repo_root: '{project-root}/git-root'\n", encoding="utf-8")
    (project.project / ".bmad-loop").mkdir(parents=True, exist_ok=True)
    (project.project / ".bmad-loop" / "policy.toml").write_text(
        '[adapter]\nname = "claude"\n\n[scm]\nisolation = "worktree"\n', encoding="utf-8"
    )


async def test_worktree_isolation_under_a_repo_root_override_blocks_launch(project, monkeypatch):
    """#414: the TUI launches a detached CLI, and that CLI refuses this combination
    itself — this guard exists so the operator gets a toast instead of a pane that
    dies immediately. Asserted against the sole producer of the text rather than a
    literal, so a reworded message cannot drift this test away from the CLI's."""
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    _split_root_tui_project(project)
    expected = bmadconfig.worktree_isolation_conflict(
        bmadconfig.load_paths(project.project), "worktree"
    )
    assert expected is not None, "the fixture really does carry the conflicting pair"

    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.click(await ready(pilot, "#ok"))
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
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: calls)
        assert not any("isolation" in m for m in notifications(app))


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


async def test_dry_run_shows_captured_output(project, monkeypatch):
    seen = {}
    monkeypatch.setattr(launch, "mux_available", lambda: True)

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


async def test_dry_run_worker_survives_a_raising_subprocess(project, monkeypatch):
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
    app = BmadLoopApp(project.project)
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


async def test_validate_shows_findings_modal(project, monkeypatch):
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
    app = BmadLoopApp(project.project)
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


async def test_validate_detail_toggle_expands_every_finding(project, monkeypatch):
    """`d` re-renders the same document with detail on."""
    doc = make_validate_document(
        [("host.process", "ok", "process host: Posix", {"host": "PosixProcessHost"})]
    )
    stub_validate(monkeypatch, stdout=json.dumps(doc))
    app = BmadLoopApp(project.project)
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


async def test_validate_verdict_comes_from_the_document_not_the_exit_code(project, monkeypatch):
    """rc conflates "checks failed" with "the command broke"; the document's `ok`
    does not. Both legs are rendered here with rc deliberately disagreeing with
    what the old code would have inferred from it."""
    failing = make_validate_document([("adapter.binary", "problem", "codex not found", None)])
    stub_validate(monkeypatch, stdout=json.dumps(failing), rc=1)
    app = BmadLoopApp(project.project)
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
    app = BmadLoopApp(project.project)
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
async def test_validate_degrades_to_the_text_modal(project, monkeypatch, stdout, why):
    """Every undrawable document RE-RUNS validate in text mode. Showing the
    captured JSON instead would hand the reader a wall of `{"schema_version": ...}`
    at the exact moment the structural rendering failed; re-running costs one
    subprocess and makes the degrade byte-for-byte the pre-#210 behavior."""
    seen = stub_validate(monkeypatch, stdout=stdout, rc=1)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, TextOutputModal), timeout=10.0)
        await ready(pilot, "Label")  # body mounts a tick after the screen swaps
        labels = app.screen.query("Label")
        assert any("exit 1" in str(label.content) for label in labels), why
        assert "--json" not in seen["text_tail"], "the text re-run is the plain command"


async def test_validate_worker_survives_a_raising_subprocess(project, monkeypatch):
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
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, TextOutputModal))
        await ready(pilot, "#output Static")
        body = render(app.screen.query_one("#output Static").content)
        assert "no such file" in body, "the modal carries the reason, not a blank panel"
        assert app.is_running, "the app survived a failure BOTH legs hit"


async def test_resume_confirm_launches(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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

    monkeypatch.setattr(launch, "mux_available", lambda: True)
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


async def test_cleanup_sessions_mux_error_notifies(project, monkeypatch):
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
        raise MultiplexerError("ctl window probe unreachable")

    monkeypatch.setattr(launch, "prune_ctl_windows", boom)
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.click(await ready(pilot, "#ok"))
        await until(
            pilot, lambda: any("ctl window probe unreachable" in m for m in notifications(app))
        )
        # the ctl-window failure is surfaced, but the session pruning that already
        # completed is still reported — not swallowed by an early return
        await until(pilot, lambda: any("unverifiable engine pid" in m for m in notifications(app)))
        assert any("removed 1 session(s)" in m for m in notifications(app))
        assert isinstance(app.screen, DashboardScreen)  # worker failed soft, no crash


async def test_resume_finished_run_refused(project, monkeypatch):
    monkeypatch.setattr(launch, "mux_available", lambda: True)
    make_run(project.project, "20260611-100000-aaaa", finished=True)
    app = BmadLoopApp(project.project)
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
    monkeypatch.setattr(launch, "ctl_window", lambda run_id: None)
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
    monkeypatch.setattr(launch, "ctl_window", lambda run_id: None)

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
    monkeypatch.setattr(launch, "ctl_window", lambda run_id: None)

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
async def test_attach_targets_ctl_window_when_decision_pending(project, monkeypatch):
    run_dir = make_run(project.project, "20260611-100000-aaaa", run_type="sweep", alive=True)
    Journal(run_dir).append("decision-pending", dw_id="DW-7", question="q?")
    selected: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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
    assert stamps == [("=bmad-loop-ctl:sweep-20260611-100000-aaaa", "=main:%9")]


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_attach_outside_tmux_stamps_detach(project, monkeypatch):
    # No TMUX: a throwaway client attaches under suspend, so the ctl window is
    # stamped to detach it on exit (returning to the suspended TUI) rather than
    # switch-client back to a pane we do not have.
    run_dir = make_run(project.project, "20260611-100000-aaaa", run_type="sweep", alive=True)
    Journal(run_dir).append("decision-pending", dw_id="DW-7", question="q?")
    monkeypatch.delenv("TMUX", raising=False)
    stamps: list[tuple[str, str]] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_attach_prefers_agent_session_without_decision(project, monkeypatch):
    make_run(project.project, "20260611-100000-aaaa", alive=True)
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_attach_falls_back_to_ctl_window(project, monkeypatch):
    make_run(project.project, "20260611-100000-aaaa", alive=True)
    selected: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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
    assert stamps == [("=bmad-loop-ctl:run-20260611-100000-aaaa", "=main:%9")]


@pytest.mark.usefixtures("force_tmux_backend")  # pin tmux against win32-matching externals
async def test_resolve_escalation_launches_and_attaches(project, monkeypatch):
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
    assert stamps == [("@7", "=main:%9")]


async def test_resolve_unknown_pid_refused(project, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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
        await pilot.click(await ready(pilot, "#act-continue"))
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


async def test_story_checkpoint_stop_marks_stopped(project, monkeypatch):
    from bmad_loop import runs

    stops: list[Path] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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


# ------------------------------------------------------------ graceful stop (S)


async def test_graceful_stop_requests_via_helper(project, monkeypatch):
    # S writes the graceful-stop control file via the runs helper and toasts. No
    # multiplexer is touched (no _mux_missing gate, mux_available left unset), no
    # shell-out — the worker only calls runs.request_graceful_stop.
    from bmad_loop import runs

    calls: list[Path] = []
    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    monkeypatch.setattr(runs, "request_graceful_stop", lambda rd: calls.append(rd) or "requested")
    make_run(project.project, "20260611-100000-aaaa", alive=True)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: len(calls) == 1)
        assert calls[0].name == "20260611-100000-aaaa"
        await until(pilot, lambda: any("graceful stop requested" in m for m in notifications(app)))


@pytest.mark.parametrize(
    "token, needle",
    [
        ("already-pending", "already has a graceful stop pending"),
        ("requested-unverifiable", "could not confirm a live engine"),
    ],
)
async def test_graceful_stop_token_messages(project, monkeypatch, token, needle):
    # The worker translates each status token from the helper into its own toast.
    from bmad_loop import runs

    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    monkeypatch.setattr(runs, "request_graceful_stop", lambda rd: token)
    make_run(project.project, "20260611-100000-aaaa", alive=True)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: any(needle in m for m in notifications(app)))


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
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: len(calls) == 1)
        assert calls[0].name == "20260611-100000-aaaa"
        needle = "could not confirm a live engine"
        await until(pilot, lambda: any(needle in m for m in notifications(app)))


async def test_graceful_stop_error_toasts(project, monkeypatch):
    # A GracefulStopError out of the helper (finished/dead run) surfaces as an
    # error toast rather than escaping the worker (exit_on_error would kill the app).
    from bmad_loop import runs

    def boom(rd):
        raise runs.GracefulStopError("run has already finished — nothing to stop")

    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    monkeypatch.setattr(runs, "request_graceful_stop", boom)
    make_run(project.project, "20260611-100000-aaaa", alive=True)
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: dashboard(app).selected_run_id == "20260611-100000-aaaa")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.click(await ready(pilot, "#ok"))
        await until(pilot, lambda: any("nothing to stop" in m for m in notifications(app)))


async def test_graceful_stop_pending_shows_in_header_and_note(project, monkeypatch):
    # End to end: a RUNNING run with the control file present paints the header
    # pending line and the runs-table stop tag (data -> snapshot -> apply).
    from bmad_loop.runs import STOP_REQUEST_FILE

    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    run_dir = make_run(project.project, "20260611-100000-aaaa", alive=True)
    (run_dir / STOP_REQUEST_FILE).write_text("{}", encoding="utf-8")
    app = BmadLoopApp(project.project)
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


async def test_active_agent_shows_in_header_and_task_cell(project, monkeypatch):
    # End to end: a RUNNING run with an open, adapter-stamped session-start paints
    # the header's live agent line and the task row's agent cell
    # (data.active_agent -> snapshot -> apply). Story key matches STORY_RE.
    monkeypatch.setattr(data, "liveness", lambda run_dir: "alive")
    task = StoryTask(story_key="1-1-alpha", epic=1, phase=Phase.DEV_RUNNING)
    run_dir = make_run(
        project.project,
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
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        header = screen.query_one("#runheader", RunHeader)
        await until(pilot, lambda: "claude · opus · dev" in str(header.content))
        tasks_table = screen.query_one("#tasks", DataTable)
        await until(
            pilot,
            lambda: tasks_table.row_count == 1
            and tasks_table.get_cell("1-1-alpha", "agent") == "claude·opus",
        )


async def test_idle_run_shows_configured_agents_and_cell_falls_back(project, monkeypatch):
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
        project.project,
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
    app = BmadLoopApp(project.project)
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
            lambda: tasks_table.row_count == 1
            and tasks_table.get_cell("1-1-alpha", "agent") == "claude·haiku",
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


async def test_escalation_rearm_resumes_when_resolution_ready(project, monkeypatch):
    from bmad_loop import resolve, runs

    calls: list[str] = []
    rearms: list[str] = []
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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


async def test_escalation_rearm_warns_when_restore_recorded(project, monkeypatch):
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
        runs, "rearm_escalation", lambda rd, sk: rearms.append(sk) or "ready-for-dev"
    )
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
        blocked_result="Blocked: intent gap; saved patch: artifacts/attempt.patch",
    )
    marker = resolve.resolution_path(run_dir, "1")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"restore_patch": "artifacts/attempt.patch"}', encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await _open_review(app, pilot, EscalationModal)
        assert app.screen._restore_recorded is True  # the modal shows the warning hint
        await pilot.click(await ready(pilot, "#act-rearm"))
        await until(pilot, lambda: rearms == ["1"] and calls == ["20260611-100000-aaaa"])
    assert any("NOT honored" in n for n in notes)  # the drop was surfaced, not silent


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
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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


async def test_start_run_modal_stories_preview_validates(project, monkeypatch):
    # action_start_run bails on _mux_missing() before it can push the modal, and
    # the Windows CI matrix has no tmux on PATH — every StartRunModal test stubs
    # this out so the modal opens (its absence here was the all-Windows timeout).
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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
    monkeypatch.setattr(launch, "mux_available", lambda: True)
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


async def test_resize_mode_widens_and_narrows_sidebar(project):
    app = BmadLoopApp(project.project)
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


async def test_resize_mode_grows_left_panes_and_cycles(project):
    app = BmadLoopApp(project.project)
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


async def test_arrows_and_tab_untouched_outside_resize_mode(project):
    root = project.project
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


async def test_mouse_drag_resizes_sidebar_and_left_pane(project):
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        w0 = screen.query_one("#left").size.width
        await _drag(pilot, "#split-main", 6, 0)
        assert screen.query_one("#left").size.width == w0 + 6
        r0 = screen.query_one("#runs").size.height
        await _drag(pilot, "#split-runs", 0, 2)  # drag the bar down: Runs grows
        assert screen._left_frozen
        assert screen.query_one("#runs").size.height == r0 + 2


async def test_mouse_drag_resizes_tasks_and_tabs(project):
    """The detail-column boundary: dragging #split-tasks grows Tasks and shrinks
    the Tabs pane (which flexes). Regressed the whole boundary being unusable."""
    app = BmadLoopApp(project.project)
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


async def test_persisted_tall_tasks_height_survives_max_height_cap(project):
    """Regression: a persisted tasks_height above the CSS `max-height: 35%`
    default must render at full height, not be silently re-clamped to 35% —
    which froze the boundary (story-maker: tasks_height=30, no run selected)."""
    root = project.project
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


async def test_sidebar_width_is_clamped(project):
    app = BmadLoopApp(project.project)
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


async def test_geometry_persists_and_restores(project):
    root = project.project
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


async def test_untouched_layout_writes_nothing_and_keeps_defaults(project):
    """No resize -> no policy file, panes at their CSS defaults, columns still
    flex (unfrozen)."""
    root = project.project
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


async def test_split_runs_label_tracks_sprint_vs_stories(project):
    """The splitter above the middle slot carries its section title, swapping
    Sprint<->Stories with the selected run's board mode."""
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        bar = screen.query_one("#split-runs", Splitter)
        assert bar.label == "Sprint"
        screen._apply_board(_Snapshot(generation=screen._generation, stories_mode=True, stories=[]))
        await pilot.pause()
        assert bar.label == "Stories"


async def test_dashboard_survives_policy_read_oserror(project, monkeypatch):
    """A transient read failure (permissions, race after the is_file check) while
    loading policy at construction degrades to default geometry instead of
    crashing the TUI at startup."""

    def boom(path):
        raise OSError("permission denied")

    monkeypatch.setattr(policy_mod, "load", boom)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _seeded(pilot, app)
        assert screen._tui_policy == policy_mod.TuiPolicy()
        assert screen.query_one("#left").size.width == 34  # CSS default, unseeded
        assert not screen._left_frozen and not screen._detail_frozen


async def test_first_geometry_save_writes_only_tui_keys(project):
    """A geometry save on a project without policy.toml must create a minimal
    [tui]-only file — not materialise POLICY_TEMPLATE, which would freeze every
    default setting (gates, limits, ...) into the fresh file."""
    root = project.project
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


async def test_quit_in_resize_mode_persists_geometry(project):
    """Quitting the app mid-resize-mode still persists the new geometry:
    keyboard bumps only save on mode exit, and quit stays live in the mode."""
    root = project.project
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
