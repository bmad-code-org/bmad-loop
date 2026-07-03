#!/usr/bin/env python3
"""Render the documentation screenshots of the bmad-loop TUI.

The TUI is a pure read-only observer (see ``bmad_loop.tui``): it renders from
the artifacts an engine writes under ``.bmad-loop/runs/<id>/`` plus the BMAD
``sprint-status.yaml`` / ``deferred-work.md`` ledgers. So we can produce a fully
populated dashboard with no live engine and no tmux: build a throwaway project
on disk, drive the app headlessly through Textual's ``run_test`` pilot, and call
``App.save_screenshot`` (SVG) at each view. Each SVG is then rasterised to PNG
with ``resvg`` for hosts that do not render SVG.

Run it from a checkout that has the TUI extra installed::

    uv sync --extra tui              # textual, tomlkit, pyte
    uv run python scripts/gen_screenshots.py

Output lands in ``docs/images/`` as ``<view>.svg`` + ``<view>.png``. The script
is idempotent — re-run it whenever the TUI changes so the docs stay honest.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from textual.widgets import Collapsible

# Import the package the same way the installed CLI does.
from bmad_loop.journal import Journal, save_state
from bmad_loop.model import Phase, RunState, StoryTask, TokenUsage
from bmad_loop.runs import RUNS_DIR
from bmad_loop.tui import launch
from bmad_loop.tui.app import BmadLoopApp
from bmad_loop.tui.screens.dashboard import DashboardScreen
from bmad_loop.tui.screens.modals import DecisionModal, DeferredEntryModal, StartRunModal
from bmad_loop.tui.screens.settings_screen import SettingsScreen

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "docs" / "images"
SIZE = (150, 42)  # wide enough for the two-column layout to breathe

# Run ids sort chronologically (YYYYMMDD-HHMMSS-hex); the TUI auto-selects the
# newest, so the rich running story run is last and becomes the hero shot.
FINISHED_RUN = "20260610-090000-a1b2"
SWEEP_RUN = "20260611-101500-c3d4"
STORY_RUN = "20260612-141000-e5f6"

POLICY_TOML = """\
[gates]
mode = "per-epic"
retrospective = "notify"

[limits]
max_review_cycles = 3
max_dev_attempts = 2
session_timeout_min = 90
max_tokens_per_story = 2_000_000
cache_read_weight = 0.1

[verify]
commands = ["pytest -q", "ruff check ."]

[notify]
desktop = true
file = true

[adapter]
name = "claude"
model = ""

[adapter.review]
name = "codex"
model = "gpt-5-codex"

[sweep]
auto = "per-epic"
max_bundles = 5
repeat = true
max_cycles = 5
"""

SPRINT_STATUS = {
    "epic-1": "done",
    "1-1-auth": "done",
    "1-2-session-tokens": "done",
    "1-3-password-reset": "done",
    "epic-1-retrospective": "done",
    "epic-2": "in-progress",
    "2-1-search-index": "done",
    "2-2-typeahead": "done",
    "2-3-search-ranking": "in-progress",
    "2-4-saved-searches": "review",
    "2-5-search-analytics": "ready-for-dev",
    "epic-3": "backlog",
    "3-1-billing-portal": "backlog",
    "3-2-invoices": "backlog",
}

DEFERRED_WORK = """\
# Deferred Work

### DW-1: Harden the OAuth token refresh race

origin: epic 1 review, 2026-06-02
location: src/auth/tokens.py:184
severity: high
reason: Two concurrent requests can both refresh; needs a single-flight lock.
status: open

### DW-2: Add pagination to the search results API

origin: 2-1-search-index review, 2026-06-07
location: src/search/api.py:96
severity: medium
reason: Result sets over ~500 items should page rather than truncate silently.
status: open

### DW-3: Replace the ad-hoc ranking weights with a config table

origin: 2-3-search-ranking dev, 2026-06-11
location: src/search/ranking.py:42
severity: low
reason: Weights are inline constants; product wants to tune them without a deploy.
status: open

### DW-4: Flaky retry in the indexer integration test

origin: CI, 2026-06-05
location: tests/test_indexer.py:210
severity: critical
reason: Network timeout makes the suite red ~1 in 20 runs; quarantine and fix.
status: open

### DW-5: Polish the empty-state copy on the search page

origin: 2-2-typeahead review, 2026-06-08
location: src/search/views.py:31
severity: low
reason: Copy reads as an error; should be a friendly prompt.
status: done 2026-06-10
"""

# A short, colourful pane log for the active dev session so the Log tab has
# something real to emulate (pyte collapses repaint frames; ANSI is preserved).
SESSION_LOG = (
    b"\x1b[2m$ claude /bmad-dev-auto 2-3-search-ranking\x1b[0m\r\n"
    b"\x1b[36m\xe2\x97\x8f\x1b[0m planning spec \xe2\x80\xa6 \x1b[2m(1.8k tokens)\x1b[0m\r\n"
    b"\x1b[32m\xe2\x9c\x94\x1b[0m spec-2-3-search-ranking.md written \xe2\x80\x94 status in-review\r\n"
    b"\x1b[36m\xe2\x97\x8f\x1b[0m implementing relevance scoring\r\n"
    b"  \x1b[2msrc/search/ranking.py\x1b[0m  \x1b[32m+128\x1b[0m \x1b[31m-14\x1b[0m\r\n"
    b"  \x1b[2mtests/test_ranking.py\x1b[0m  \x1b[32m+86\x1b[0m\r\n"
    b"\x1b[36m\xe2\x97\x8f\x1b[0m running verify commands \xe2\x80\xa6\r\n"
    b"  \x1b[32mpytest -q\x1b[0m  \x1b[32m214 passed\x1b[0m \x1b[2min 9.2s\x1b[0m\r\n"
)


def _tokens(inp: int, out: int, cache_read: int = 0, cache_creation: int = 0) -> TokenUsage:
    return TokenUsage(
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
    )


def build_project(root: Path) -> None:
    """Lay down a BMAD-shaped project with rich, fully-populated artifacts."""
    impl = root / "_bmad-output" / "implementation-artifacts"
    impl.mkdir(parents=True, exist_ok=True)
    (root / "_bmad-output" / "planning-artifacts").mkdir(parents=True, exist_ok=True)

    cfg = root / "_bmad" / "bmm"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text(
        "implementation_artifacts: '{project-root}/_bmad-output/implementation-artifacts'\n"
        "planning_artifacts: '{project-root}/_bmad-output/planning-artifacts'\n",
        encoding="utf-8",
    )

    (impl / "sprint-status.yaml").write_text(
        yaml.safe_dump(
            {
                "generated": "12-06-2026 09:00",
                "last_updated": "12-06-2026 14:10",
                "project": "acme-search",
                "project_key": "ACME",
                "tracking_system": "file-system",
                "development_status": dict(SPRINT_STATUS),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (impl / "deferred-work.md").write_text(DEFERRED_WORK, encoding="utf-8")

    policy = root / ".bmad-loop" / "policy.toml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(POLICY_TOML, encoding="utf-8")

    _build_finished_run(root)
    _build_sweep_run(root)
    _build_story_run(root)


def _build_finished_run(root: Path) -> None:
    tasks = {
        "1-1-auth": StoryTask(
            story_key="1-1-auth",
            epic=1,
            phase=Phase.DONE,
            attempt=1,
            review_cycle=1,
            commit_sha="9f1c2ab7e4d05c18",
            tokens=_tokens(184_000, 22_000, 410_000, 38_000),
        ),
        "1-2-session-tokens": StoryTask(
            story_key="1-2-session-tokens",
            epic=1,
            phase=Phase.DONE,
            attempt=1,
            review_cycle=2,
            commit_sha="c4408de91a2b77f0",
            tokens=_tokens(201_000, 26_500, 522_000, 41_000),
        ),
        "1-3-password-reset": StoryTask(
            story_key="1-3-password-reset",
            epic=1,
            phase=Phase.DONE,
            attempt=2,
            review_cycle=1,
            commit_sha="71e0b9c33d6a4e82",
            tokens=_tokens(168_000, 19_800, 388_000, 35_500),
        ),
    }
    run_dir = root / RUNS_DIR / FINISHED_RUN
    save_state(
        run_dir,
        RunState(
            run_id=FINISHED_RUN,
            project=str(root),
            started_at="2026-06-10T09:00:00",
            run_type="story",
            current_epic=1,
            finished=True,
            tasks=tasks,
        ),
    )


def _build_sweep_run(root: Path) -> None:
    tasks = {
        "dw-search-pagination": StoryTask(
            story_key="dw-search-pagination",
            epic=2,
            phase=Phase.DONE,
            attempt=1,
            review_cycle=1,
            commit_sha="2b7f0a91c4408de9",
            dw_ids=["DW-2"],
            tokens=_tokens(96_000, 12_300, 240_000, 21_000),
        ),
    }
    run_dir = root / RUNS_DIR / SWEEP_RUN
    save_state(
        run_dir,
        RunState(
            run_id=SWEEP_RUN,
            project=str(root),
            started_at="2026-06-11T10:15:00",
            run_type="sweep",
            current_epic=2,
            tasks=tasks,
        ),
    )
    (run_dir / "engine.pid").write_text(str(os.getpid()), encoding="utf-8")
    # The triage output this sweep produced. `bmad-loop decisions` / the TUI's
    # `d` key reconstruct unanswered decisions from these triage*.json files, so
    # writing it makes the missed-decisions feature show: DW-1 and DW-3 are still
    # open and unanswered, so the Deferred Work pane reads "2 to answer (d)".
    triage = {
        "workflow": "deferred-sweep-triage",
        "open_ids": ["DW-1", "DW-2", "DW-3", "DW-4"],
        "already_resolved": [],
        "bundles": [
            {
                "name": "search-pagination",
                "dw_ids": ["DW-2"],
                "intent": "Page the search results API instead of truncating past ~500 items.",
            }
        ],
        "blocked": [],
        "skip": [
            {"id": "DW-4", "reason": "quarantined in CI; tracked under the flaky-tests ticket."}
        ],
        "decisions": [
            {
                "id": "DW-1",
                "question": "Harden the OAuth refresh race now, or hold it for the auth-hardening epic?",
                "context": (
                    "Two concurrent requests can both refresh the token. A single-flight lock "
                    "fixes it now, but the upcoming auth-hardening epic reworks this module anyway."
                ),
                "options": [
                    {
                        "key": "1",
                        "label": "Add a single-flight lock now",
                        "effect": "build",
                        "intent": "Guard the refresh path with a single-flight lock so only one refresh runs.",
                    },
                    {
                        "key": "2",
                        "label": "Hold for the auth-hardening epic",
                        "effect": "keep-open",
                    },
                    {
                        "key": "3",
                        "label": "Won't fix — refresh is idempotent",
                        "effect": "close",
                        "resolution": "A double refresh is harmless; the second result is discarded.",
                    },
                ],
                "recommendation": "1",
            },
            {
                "id": "DW-3",
                "question": "Move the ranking weights to a config table, or keep them inline?",
                "context": "Product wants to tune relevance weights without shipping a deploy.",
                "options": [
                    {
                        "key": "1",
                        "label": "Add a weights config table",
                        "effect": "build",
                        "intent": "Load ranking weights from a config table editable without a deploy.",
                    },
                    {"key": "2", "label": "Keep them inline for now", "effect": "keep-open"},
                ],
                "recommendation": "1",
            },
        ],
        "escalations": [],
    }
    (run_dir / "triage.json").write_text(json.dumps(triage, indent=2), encoding="utf-8")
    journal = Journal(run_dir)
    journal.append("sweep-start", cycle=1)
    journal.append("triage-done", bundles=1, already_resolved=1, decisions=2)
    journal.append("bundle-done", bundle="search-pagination", dw_ids="DW-2")
    journal.append(
        "decision-pending",
        dw_id="DW-1",
        question="Reopen the OAuth refresh race now, or hold it for the auth hardening epic?",
    )


def _build_story_run(root: Path) -> None:
    tasks = {
        "2-1-search-index": StoryTask(
            story_key="2-1-search-index",
            epic=2,
            phase=Phase.DONE,
            attempt=1,
            review_cycle=1,
            commit_sha="a1b2c3d4e5f60718",
            tokens=_tokens(212_000, 28_400, 540_000, 44_000),
        ),
        "2-2-typeahead": StoryTask(
            story_key="2-2-typeahead",
            epic=2,
            phase=Phase.DONE,
            attempt=1,
            review_cycle=2,
            commit_sha="b2c3d4e5f6071829",
            tokens=_tokens(176_000, 21_900, 463_000, 39_000),
        ),
        "2-3-search-ranking": StoryTask(
            story_key="2-3-search-ranking",
            epic=2,
            phase=Phase.REVIEW_RUNNING,
            attempt=1,
            review_cycle=1,
            tokens=_tokens(143_000, 17_200, 311_000, 33_000),
        ),
        "2-4-saved-searches": StoryTask(
            story_key="2-4-saved-searches",
            epic=2,
            phase=Phase.DEV_RUNNING,
            attempt=1,
            tokens=_tokens(54_000, 6_300, 96_000, 12_000),
        ),
        "2-5-search-analytics": StoryTask(
            story_key="2-5-search-analytics",
            epic=2,
            phase=Phase.DEFERRED,
            attempt=2,
            review_cycle=1,
            defer_reason="dev budget exhausted (2 attempts)",
            tokens=_tokens(88_000, 9_100, 162_000, 18_000),
        ),
    }
    run_dir = root / RUNS_DIR / STORY_RUN
    save_state(
        run_dir,
        RunState(
            run_id=STORY_RUN,
            project=str(root),
            started_at="2026-06-12T14:10:00",
            run_type="story",
            current_epic=2,
            tasks=tasks,
        ),
    )
    (run_dir / "engine.pid").write_text(str(os.getpid()), encoding="utf-8")

    logs = run_dir / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "2-3-search-ranking-review.log").write_bytes(SESSION_LOG)

    journal = Journal(run_dir)
    journal.append(
        "session-start", task_id="2-1-search-index", role="dev", story_key="2-1-search-index"
    )
    journal.append("story-done", story_key="2-1-search-index", commit="a1b2c3d4e5f6")
    journal.append("session-start", task_id="2-2-typeahead", role="dev", story_key="2-2-typeahead")
    journal.append("review-cycle", story_key="2-2-typeahead", cycle=2, findings=3)
    journal.append("story-done", story_key="2-2-typeahead", commit="b2c3d4e5f607")
    journal.append("story-start", story_key="2-3-search-ranking", epic=2)
    journal.append("spec-approved", story_key="2-3-search-ranking", tokens=1834)
    journal.append("dev-done", story_key="2-3-search-ranking", tasks_done=3, tasks_total=3)
    journal.append("verify-ok", story_key="2-3-search-ranking", commands="pytest -q, ruff check .")
    journal.set_active_log("2-3-search-ranking-review")
    journal.append("review-start", story_key="2-3-search-ranking", role="review")
    journal.append(
        "escalation-preference",
        story_key="2-3-search-ranking",
        detail="reviewer used codex for the scoring math",
    )
    journal.append(
        "story-deferred", story_key="2-5-search-analytics", reason="dev budget exhausted"
    )


async def _wait(pilot, predicate, timeout: float = 8.0) -> None:
    waited = 0.0
    while not predicate():
        if waited >= timeout:
            raise AssertionError("condition not met before timeout")
        await pilot.pause(0.05)
        waited += 0.05


def _dashboard(app: BmadLoopApp) -> DashboardScreen:
    assert isinstance(app.screen, DashboardScreen)
    return app.screen


async def capture(root: Path) -> list[str]:
    saved: list[str] = []
    # tmux exists on CI/dev boxes, but make the run-control bindings unconditional.
    launch.tmux_available = lambda: True  # type: ignore[assignment]

    app = BmadLoopApp(root)
    async with app.run_test(size=SIZE) as pilot:
        await _wait(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = _dashboard(app)
        app.sub_title = "~/code/acme-search"  # clean title bar; hide the tmpdir

        # The hero: the newest run (the running story run) is auto-selected.
        await _wait(pilot, lambda: screen.selected_run_id == STORY_RUN)
        await _wait(
            pilot,
            lambda: "running" in str(screen.query_one("#runheader").content)
            and screen.query_one("#tasks").row_count == 5,
        )
        # Expand the in-flight epics so the sprint tree shows real stories.
        tree = screen.query_one("#sprint-tree")
        await _wait(pilot, lambda: len(tree.root.children) == 3)
        for node in tree.root.children:
            node.expand()
        await pilot.pause(0.2)
        app.save_screenshot(str(OUT_DIR / "dashboard.svg"))
        saved.append("dashboard.svg")

        # Sweep run blocked on a human decision: select its row, await the banner.
        runs = screen.query_one("#runs")
        runs.move_cursor(row=1)  # oldest first: [finished, sweep, story]
        await _wait(pilot, lambda: screen.selected_run_id == SWEEP_RUN)
        await _wait(pilot, lambda: screen.decision_pending is not None)
        await _wait(pilot, lambda: "decision needed" in str(screen.query_one("#runheader").content))
        await pilot.pause(0.2)
        app.save_screenshot(str(OUT_DIR / "sweep-decision.svg"))
        saved.append("sweep-decision.svg")

        # Deferred-work entry modal (full body of a high-severity open item).
        deferred = screen.query_one("#deferred")
        deferred.focus()
        deferred.highlighted = 0  # DW-1
        await pilot.press("enter")
        await _wait(pilot, lambda: isinstance(app.screen, DeferredEntryModal))
        await pilot.pause(0.2)
        app.save_screenshot(str(OUT_DIR / "deferred-modal.svg"))
        saved.append("deferred-modal.svg")
        await pilot.press("escape")
        await _wait(pilot, lambda: isinstance(app.screen, DashboardScreen))

        # Answering a decision a past sweep left unanswered ("d"): the modal
        # walks the open, unanswered decisions reconstructed from triage output.
        await pilot.press("d")
        await _wait(pilot, lambda: isinstance(app.screen, DecisionModal))
        await pilot.pause(0.2)
        app.save_screenshot(str(OUT_DIR / "decision-answer.svg"))
        saved.append("decision-answer.svg")
        await pilot.press("escape")
        await _wait(pilot, lambda: isinstance(app.screen, DashboardScreen))

        # Start-run modal.
        await pilot.press("r")
        await _wait(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.pause(0.2)
        app.save_screenshot(str(OUT_DIR / "start-run-modal.svg"))
        saved.append("start-run-modal.svg")
        await pilot.press("escape")
        await _wait(pilot, lambda: isinstance(app.screen, DashboardScreen))

        # policy.toml settings editor.
        await pilot.press("g")
        await _wait(pilot, lambda: isinstance(app.screen, SettingsScreen))
        await pilot.pause(0.3)
        app.save_screenshot(str(OUT_DIR / "settings.svg"))
        saved.append("settings.svg")

        # Same screen, [scm] section expanded — the sections start collapsed, so
        # the worktree-isolation + config-seeding knobs are only visible here.
        seed_switch = app.screen.query_one("#scm-seed_adapter_defaults")
        scm_section = next(a for a in seed_switch.ancestors if isinstance(a, Collapsible))
        scm_section.collapsed = False
        await pilot.pause()
        # frame the seed knobs with the worktree knobs above them still in view
        app.screen.query_one("#scm-worktree_seed").scroll_visible(animate=False)
        await pilot.pause(0.3)
        app.save_screenshot(str(OUT_DIR / "settings-scm.svg"))
        saved.append("settings-scm.svg")

    return saved


# Textual's SVG names its terminal font "Fira Code", monospace and the title
# bar font "arial"; both render in browsers but resvg matches neither by name.
# Map them to a real installed monospace for the PNG without touching the
# committed SVG (which browsers render with their own fonts).
MONO_FONT = "FiraCode Nerd Font"


def rasterise(names: list[str]) -> None:
    resvg = shutil.which("resvg")
    if not resvg:
        print("! resvg not found on PATH — skipping PNG export (SVGs are written)", file=sys.stderr)
        return
    for name in names:
        svg = OUT_DIR / name
        png = svg.with_suffix(".png")
        patched = svg.read_text(encoding="utf-8").replace(
            "font-family: arial;", f'font-family: "{MONO_FONT}";'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as tf:
            tf.write(patched)
            tmp_svg = tf.name
        try:
            subprocess.run(
                [
                    resvg,
                    "--zoom",
                    "2",  # crisp on high-DPI displays
                    "--font-family",
                    MONO_FONT,
                    "--monospace-family",
                    MONO_FONT,
                    tmp_svg,
                    str(png),
                ],
                check=True,
                stderr=subprocess.DEVNULL,
            )
        finally:
            os.unlink(tmp_svg)
        print(f"  {png.relative_to(REPO)}")


def scrub_paths(names: list[str], root: Path) -> None:
    """Replace the throwaway project's absolute path (which the settings editor
    prints) with a clean, deterministic one, so the committed SVGs never carry a
    machine-specific tmpdir."""
    for name in names:
        svg = OUT_DIR / name
        text = svg.read_text(encoding="utf-8")
        if str(root) in text:
            svg.write_text(text.replace(str(root), "~/code/acme-search"), encoding="utf-8")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bmad-loop-shots-") as tmp:
        root = Path(tmp) / "acme-search"
        root.mkdir()
        build_project(root)
        saved = asyncio.run(capture(root))
        scrub_paths(saved, root)
    print(f"Wrote {len(saved)} SVG screenshots to {OUT_DIR.relative_to(REPO)}/")
    rasterise(saved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
