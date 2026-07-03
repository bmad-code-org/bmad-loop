#!/usr/bin/env python3
"""Render the README demo GIF of the bmad-loop TUI — headlessly, no live run.

The TUI is a pure read-only observer, so (exactly like ``gen_screenshots.py``)
we build a throwaway, fully-populated BMAD project on disk, drive the app through
Textual's ``run_test`` pilot, and ``save_screenshot`` after each micro-action so
the result actually animates. The narrative: land on the running story run, walk
the runs cursor, unfold the sprint tree epic by epic, step down the deferred-work
ledger and open an entry, type a story key into the start-run modal character by
character, raise the sweep decision banner, scroll the policy editor, and expand
its `[scm]` section to show the worktree-isolation + config-seeding knobs. Frames
are rasterised to PNG with ``resvg`` and stitched into a GIF with ``ffmpeg``,
each frame held for its own delay (so motion is smooth and rests linger).

No engine, no tmux, no coding-agent sessions, no tokens. Deterministic and
re-runnable whenever the TUI changes.

    uv sync --extra tui              # textual, tomlkit, pyte
    uv run python scripts/gen_demo.py  # needs resvg + ffmpeg on PATH

Output: docs/images/demo.gif
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

# Reuse the screenshot harness's mock-project builder and resvg font mapping.
# (Imports follow the sys.path tweak above, hence the E402 suppressions.)
from gen_screenshots import MONO_FONT, SIZE, STORY_RUN, SWEEP_RUN, build_project  # noqa: E402
from textual.widgets import Checkbox, Collapsible, Input  # noqa: E402

from bmad_loop.tui import launch  # noqa: E402
from bmad_loop.tui.app import BmadLoopApp  # noqa: E402
from bmad_loop.tui.screens.dashboard import DashboardScreen  # noqa: E402
from bmad_loop.tui.screens.modals import (  # noqa: E402
    DecisionModal,
    DeferredEntryModal,
    StartRunModal,
)
from bmad_loop.tui.screens.settings_screen import SettingsScreen  # noqa: E402

OUT_DIR = REPO / "docs" / "images"
GIF = OUT_DIR / "demo.gif"
WIDTH = 760  # output width in px (height auto)
MAX_COLORS = 64  # palette size; the TUI uses a small fixed palette so this is ample
MOTION = 0.09  # per-frame hold during motion (~11 fps); rest beats hold much longer


async def _wait(pilot, predicate, timeout: float = 8.0) -> None:
    waited = 0.0
    while not predicate():
        if waited >= timeout:
            raise AssertionError("condition not met before timeout")
        await pilot.pause(0.05)
        waited += 0.05


async def capture(root: Path, frames_dir: Path) -> list[tuple[str, float]]:
    """Drive the narrative with real motion; return [(svg, hold_seconds), …].

    Most calls capture a frame after a *single* micro-action (one cursor step,
    one keystroke, one node expanded) so the GIF actually animates — the cursor
    walks, the tree unfolds, and text appears character by character — rather
    than cutting between rest states.
    """
    frames: list[tuple[str, float]] = []
    launch.tmux_available = lambda: True  # type: ignore[assignment]
    n = 0

    def shot(hold: float = MOTION) -> None:
        nonlocal n
        svg = frames_dir / f"f{n:03d}.svg"
        app.save_screenshot(str(svg))
        frames.append((str(svg), hold))
        n += 1

    async def hold_last(seconds: float, pilot) -> None:
        # Extend the last captured frame's on-screen time without re-rendering.
        await pilot.pause(0.02)
        frames[-1] = (frames[-1][0], seconds)

    async def typeinto(pilot, widget: Input, text: str) -> None:
        widget.focus()
        await pilot.pause(0.05)
        shot(0.25)  # focused, empty field
        for i in range(1, len(text) + 1):
            widget.value = text[:i]
            await pilot.pause(0.02)
            shot(0.06)  # one character appears
        await hold_last(0.7, pilot)

    app = BmadLoopApp(root)
    async with app.run_test(size=SIZE) as pilot:
        await _wait(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = app.screen
        app.sub_title = "~/code/acme-search"

        # Beat 1 — land on the dashboard; newest (running) story run auto-selected.
        await _wait(pilot, lambda: screen.selected_run_id == STORY_RUN)
        await _wait(
            pilot,
            lambda: "running" in str(screen.query_one("#runheader").content)
            and screen.query_one("#tasks").row_count == 5,
        )
        shot()
        await hold_last(2.0, pilot)

        # Beat 2 — walk the runs cursor up the table and back to the live run.
        runs = screen.query_one("#runs")
        runs.focus()
        for row in (1, 0, 2):  # sweep → finished → back to the running story run
            runs.move_cursor(row=row)
            await _wait(pilot, lambda r=row: runs.cursor_row == r)
            await pilot.pause(0.05)
            shot(0.45)
        await _wait(pilot, lambda: screen.selected_run_id == STORY_RUN)

        # Beat 3 — unfold the sprint tree one epic at a time.
        tree = screen.query_one("#sprint-tree")
        await _wait(pilot, lambda: len(tree.root.children) == 3)
        for node in tree.root.children:
            node.expand()
            await pilot.pause(0.05)
            shot(0.5)
        await hold_last(1.6, pilot)

        # Beat 4 — step down the deferred-work ledger, then open DW-1.
        deferred = screen.query_one("#deferred")
        deferred.focus()
        for i in (3, 2, 1, 0):  # walk up to the high-severity DW-1
            deferred.highlighted = i
            await pilot.pause(0.05)
            shot(0.3)
        await hold_last(0.6, pilot)
        await pilot.press("enter")
        await _wait(pilot, lambda: isinstance(app.screen, DeferredEntryModal))
        await pilot.pause(0.15)
        shot()
        await hold_last(2.6, pilot)
        await pilot.press("escape")
        await _wait(pilot, lambda: isinstance(app.screen, DashboardScreen))

        # Beat 4b — answer a decision a past sweep left unanswered ("d"). The
        # Deferred Work pane shows the count; the modal walks each open one.
        await pilot.press("d")
        await _wait(pilot, lambda: isinstance(app.screen, DecisionModal))
        await pilot.pause(0.15)
        shot()
        await hold_last(2.8, pilot)
        await pilot.press("escape")
        await _wait(pilot, lambda: isinstance(app.screen, DashboardScreen))

        # Beat 5 — open the start-run modal and *type* into its fields.
        await pilot.press("r")
        await _wait(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.pause(0.15)
        modal = app.screen
        shot(0.7)
        await typeinto(pilot, modal.query_one("#story", Input), "2-4-saved-searches")
        await typeinto(pilot, modal.query_one("#max-stories", Input), "1")
        dry = modal.query_one("#dry-run", Checkbox)
        dry.focus()
        dry.value = True
        await pilot.pause(0.05)
        shot(0.6)  # dry-run ticked
        await hold_last(2.2, pilot)
        await pilot.press("escape")
        await _wait(pilot, lambda: isinstance(app.screen, DashboardScreen))

        # Beat 6 — select the sweep run; the decision-needed banner raises.
        runs.focus()
        runs.move_cursor(row=1)  # [finished, sweep, story]
        await _wait(pilot, lambda: screen.selected_run_id == SWEEP_RUN)
        await _wait(pilot, lambda: screen.decision_pending is not None)
        await _wait(pilot, lambda: "decision needed" in str(screen.query_one("#runheader").content))
        await pilot.pause(0.15)
        shot()
        await hold_last(2.8, pilot)

        # Beat 7 — open the policy editor and scroll through it.
        await pilot.press("g")
        await _wait(pilot, lambda: isinstance(app.screen, SettingsScreen))
        await pilot.pause(0.2)
        shot(0.8)
        for _ in range(6):
            await pilot.press("down")
            await pilot.pause(0.05)
            shot(0.32)
        await hold_last(2.0, pilot)

        # Beat 8 — expand [scm] to reveal worktree isolation + the config-seeding
        # knobs (seed adapter configs / extra worktree seed files).
        seed_switch = app.screen.query_one("#scm-seed_adapter_defaults")
        scm_section = next(a for a in seed_switch.ancestors if isinstance(a, Collapsible))
        scm_section.scroll_visible(animate=False)
        await pilot.pause(0.2)
        shot(0.9)
        scm_section.collapsed = False
        await pilot.pause(0.2)
        shot(0.6)
        app.screen.query_one("#scm-worktree_seed").scroll_visible(animate=False)
        await pilot.pause(0.2)
        shot()
        await hold_last(3.0, pilot)

    return frames


def rasterise(frames: list[tuple[str, float]], png_dir: Path) -> list[tuple[Path, float]]:
    resvg = shutil.which("resvg")
    if not resvg:
        sys.exit("! resvg not found on PATH — cannot rasterise frames")
    out: list[tuple[Path, float]] = []
    for i, (svg_path, hold) in enumerate(frames):
        svg = Path(svg_path)
        png = png_dir / f"frame_{i:03d}.png"
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
                    "1.5",
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
        out.append((png, hold))
    return out


def assemble(pngs: list[tuple[Path, float]], work: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("! ffmpeg not found on PATH — cannot assemble the GIF")
    # concat demuxer: each frame held for `hold` seconds. The last entry must be
    # repeated without a trailing duration for ffmpeg to honour the final hold.
    lines: list[str] = []
    for png, hold in pngs:
        lines.append(f"file '{png.as_posix()}'")
        lines.append(f"duration {hold:.3f}")
    lines.append(f"file '{pngs[-1][0].as_posix()}'")
    concat = work / "frames.txt"
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # No fps resampling: the concat `duration`s become the GIF's native per-frame
    # delays, so the output is exactly one frame per beat (tiny) rather than a
    # held frame duplicated FPS times a second.
    vf = (
        f"scale={WIDTH}:-1:flags=lanczos,"
        f"split[s0][s1];[s0]palettegen=max_colors={MAX_COLORS}:stats_mode=full[p];"
        "[s1][p]paletteuse=dither=bayer:bayer_scale=3"
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-vf",
            vf,
            "-loop",
            "0",
            str(GIF),
        ],
        check=True,
        stderr=subprocess.DEVNULL,
    )
    optimise()


def optimise() -> None:
    """Shrink the GIF in place with gifsicle if it's available (optional).

    ``-O3`` does inter-frame transparency compression (only changed pixels per
    frame) — a big win for a TUI demo where most of the screen is static between
    frames — and ``--lossy`` quantises the palette for a further cut.
    """
    gifsicle = shutil.which("gifsicle")
    if not gifsicle:
        print("  (gifsicle not found — skipping the optional optimisation pass)")
        return
    before = GIF.stat().st_size
    subprocess.run(
        [gifsicle, "-O3", "--lossy=80", "--colors", str(MAX_COLORS), "--batch", str(GIF)],
        check=True,
        stderr=subprocess.DEVNULL,
    )
    after = GIF.stat().st_size
    print(
        f"  gifsicle: {before / 1024:.0f} KB -> {after / 1024:.0f} KB "
        f"({100 * (1 - after / before):.0f}% smaller)"
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bmad-loop-demo-") as tmp:
        work = Path(tmp)
        root = work / "acme-search"
        root.mkdir()
        build_project(root)

        frames_dir = work / "frames"
        frames_dir.mkdir()
        frames = asyncio.run(capture(root, frames_dir))
        for svg_path, _ in frames:  # scrub the tmpdir path the settings editor prints
            svg = Path(svg_path)
            text = svg.read_text(encoding="utf-8")
            if str(root) in text:
                svg.write_text(text.replace(str(root), "~/code/acme-search"), encoding="utf-8")

        pngs = rasterise(frames, work)
        assemble(pngs, work)
    size_kb = GIF.stat().st_size / 1024
    print(f"Wrote {GIF.relative_to(REPO)} ({len(frames)} beats, {size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
