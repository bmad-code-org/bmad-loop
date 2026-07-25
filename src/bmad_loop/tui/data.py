"""Read-only filesystem observation layer for the TUI.

Everything the dashboard shows comes from the run-dir artifacts the engine
already writes atomically: state.json (os.replace), journal.jsonl
(append-only), logs/<task-id>.log, ATTENTION. This module never imports
textual — it is plain stdlib + core modules + pyte/rich, fully unit-testable,
and the screens own the poll cadence.

All readers are stat-gated: parse results are cached while the file's
(mtime_ns, size, inode) is unchanged. Liveness is the exception — a dying engine
changes no file, so the pid is re-checked on every call.
"""

from __future__ import annotations

import bisect
import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyte
from pyte import modes as pyte_modes
from rich.color import ColorParseError
from rich.style import Style
from rich.text import Text

from .. import bmadconfig, deferredwork, policy, sprintstatus, stories
from ..adapters.multiplexer import (
    MultiplexerError,
    TerminalMultiplexer,
    get_multiplexer,
    mux_usable,
)
from ..gates import ATTENTION_FILE
from ..journal import JOURNAL_FILE, LOGS_DIR, STATE_FILE, load_state
from ..model import RunState
from ..process_host import ProcessHostError
from ..runs import (
    STOP_REQUEST_FILE,
    list_run_dirs,
    probe_liveness,
    read_pid_identity,
    session_name,
)

# Run statuses shown by the dashboard.
RUNNING = "running"
PAUSED = "paused"
FINISHED = "finished"
STOPPED = "stopped"
CRASHED = "crashed"
INTERRUPTED = "interrupted"
UNKNOWN = "unknown"

_StatSig = tuple[int, int, int]


def _stat_sig(path: Path) -> _StatSig | None:
    try:
        st = path.stat()
    except OSError:
        return None
    # st_ino joins (mtime_ns, size): the engine rewrites state.json atomically
    # (temp + os.replace), so every write lands on a fresh inode. That catches a
    # same-size rewrite within one coarse mtime tick (e.g. WSL2 drvfs, or any fast
    # rewrite on a low-resolution mtime) that (mtime_ns, size) alone would miss and
    # serve stale from cache.
    return (st.st_mtime_ns, st.st_size, st.st_ino)


# ------------------------------------------------------------------ liveness


def liveness(run_dir: Path) -> str:
    """'alive' | 'dead' | 'unknown' for the engine that owns run_dir.

    engine.pid is authoritative (written at run/sweep/resume start, never
    deleted). Legacy runs without one fall back to the per-run agent session —
    but that session only exists while an agent session runs, so its absence
    proves nothing: 'unknown', never falsely dead. Pid checks are local-only;
    runs on other hosts always come back 'unknown'.
    """
    pid, identity = read_pid_identity(run_dir)
    if pid is None:
        return _session_liveness(run_dir.name)
    # Probe the pid we just read (shared body with runs.engine_liveness) rather than
    # re-reading it, so a non-atomic pid rewrite can't split the two reads and flash
    # a false 'dead' between "pid present" here and a re-read seeing an empty file.
    try:
        return probe_liveness(pid, identity)
    except ProcessHostError:
        # A misconfigured host (bad BMAD_LOOP_PROCESS_HOST) stays a hard error on
        # CLI decision paths, but the display layer must degrade, not crash: the
        # dashboard poll worker has no except and would take the whole app down.
        return "unknown"


def _session_liveness(run_id: str) -> str:
    # An absent multiplexer / dead query proves nothing about a legacy run, so the
    # only positive signal is a live session; everything else is 'unknown'.
    mux = get_multiplexer()
    if not mux_usable(mux):  # forced-aware, like every other observer gate
        return "unknown"
    try:
        return "alive" if mux.has_session(session_name(run_id)) else "unknown"
    except (OSError, MultiplexerError):
        # The seam raises MultiplexerError (not OSError) on a backend failure; a
        # dead query proves nothing about a legacy run, so degrade to 'unknown'
        # rather than crashing the TUI poll.
        return "unknown"


def _classify(finished: bool, paused: bool, stopped: bool, crashed: bool, run_dir: Path) -> str:
    if finished:
        return FINISHED
    if paused:
        return PAUSED
    # a deliberate stop leaves a dead pid — check it before liveness so it does
    # not read as INTERRUPTED (a crash).
    if stopped:
        return STOPPED
    # a recorded crash leaves a dead pid too — surface it as a distinct CRASHED
    # before liveness, where it would otherwise read as a generic INTERRUPTED.
    if crashed:
        return CRASHED
    live = liveness(run_dir)
    if live == "alive":
        return RUNNING
    if live == "dead":
        return INTERRUPTED
    return UNKNOWN


# ----------------------------------------------------------- run discovery


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    run_dir: Path
    run_type: str
    started_at: str
    status: str
    paused_stage: str = ""  # RunState.paused_stage when PAUSED, else ""; drives the badge
    stopping: bool = False  # a graceful stop is pending (control file present) while RUNNING


# state.json path -> (stat sig, header fields tuple)
_HeaderFields = tuple[str, str, bool, bool, bool, bool, str]
_header_cache: dict[Path, tuple[_StatSig, _HeaderFields]] = {}


def discover_runs(project: Path) -> list[RunInfo]:
    """One RunInfo per run dir, oldest first; [] when the runs dir is missing.

    Parses only the state.json header fields (cached on stat); a state file
    that fails to parse yields status 'unknown' rather than crashing — it is
    transient, the engine writes atomically.
    """
    out: list[RunInfo] = []
    for run_dir in list_run_dirs(project):
        state_path = run_dir / STATE_FILE
        sig = _stat_sig(state_path)
        cached = _header_cache.get(state_path)
        if sig is not None and cached is not None and cached[0] == sig:
            run_type, started_at, finished, paused, stopped, crashed, paused_stage = cached[1]
        else:
            try:
                doc = json.loads(state_path.read_text(encoding="utf-8"))
                run_type = str(doc.get("run_type", "story"))
                started_at = str(doc.get("started_at", ""))
                finished = bool(doc.get("finished", False))
                paused = doc.get("paused_reason") is not None
                stopped = bool(doc.get("stopped", False))
                crashed = bool(doc.get("crashed", False))
                paused_stage = str(doc.get("paused_stage") or "")
            except (OSError, json.JSONDecodeError):
                out.append(RunInfo(run_dir.name, run_dir, "?", "", UNKNOWN))
                continue
            if sig is not None:
                _header_cache[state_path] = (
                    sig,
                    (run_type, started_at, finished, paused, stopped, crashed, paused_stage),
                )
        status = _classify(finished, paused, stopped, crashed, run_dir)
        # paused_stage is advisory: only meaningful while the run is actually PAUSED
        # (a resumed run keeps the last stage in state until it re-pauses/finishes).
        stage = paused_stage if status == PAUSED else ""
        # A pending graceful stop is the control file's presence, but only while an
        # engine is still around to honor it — RUNNING or UNKNOWN (an unverifiable
        # pid still consumes the file). The engine discards the file at the stop
        # boundary, so a lingering file on an already-concluded run is not "stopping":
        # STOPPED/FINISHED/CRASHED classify before liveness, so they never read UNKNOWN.
        stopping = status in (RUNNING, UNKNOWN) and (run_dir / STOP_REQUEST_FILE).is_file()
        out.append(RunInfo(run_dir.name, run_dir, run_type, started_at, status, stage, stopping))
    return out


# ------------------------------------------------------------- run watching


class RunWatcher:
    """Stat-gated views of one run dir. Never raises on missing or mid-write
    files: state() keeps returning the last good parse."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self._state_sig: _StatSig | None = None
        self._state: RunState | None = None
        self._attention_sig: _StatSig | None = None
        self._attention = ""

    def state(self) -> RunState | None:
        sig = _stat_sig(self.run_dir / STATE_FILE)
        if sig is None or sig == self._state_sig:
            return self._state
        try:
            self._state = load_state(self.run_dir)
            self._state_sig = sig
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass  # keep last good
        return self._state

    def liveness(self) -> str:
        return liveness(self.run_dir)

    def status(self) -> str:
        state = self.state()
        if state is None:
            return UNKNOWN
        return _classify(state.finished, state.paused, state.stopped, state.crashed, self.run_dir)

    def stopping(self) -> bool:
        """True when a graceful-stop request is pending for this run (its control
        file is present) — a bare existence read for the run-header pending line,
        mirroring runs.graceful_stop_requested. The caller gates on a RUNNING
        status so a file lingering on a stopped run doesn't read as still-stopping."""
        return (self.run_dir / STOP_REQUEST_FILE).is_file()

    def attention(self) -> str:
        path = self.run_dir / ATTENTION_FILE
        sig = _stat_sig(path)
        if sig is not None and sig != self._attention_sig:
            try:
                self._attention = path.read_text(encoding="utf-8")
                self._attention_sig = sig
            except OSError:
                pass
        return self._attention


def read_viewer_config(run_dir: Path, adapter_name: str | None = None) -> dict[str, str] | None:
    """Read the most recent viewer config persisted to ``<run_dir>/viewer.json``.

    ``adapter_name`` filters to the entry written by that adapter; ``None``
    (the dashboard's call — it doesn't know which adapter ran) matches any
    entry that actually recorded a viewer window, so the reader can't drift
    from the adapter's self-reported name (e.g. ``opencode-http``).

    Returns a dict with the keys ``viewer_window``, ``viewer_url``,
    ``viewer_session``, ``viewer_cwd``, ``viewer_command`` when an entry
    exists and is parseable; ``None`` when the file is missing or unreadable.
    The ``viewer_window`` string is a mux target such as
    ``=bmad-loop-abc:bmad-loop-abc-viewer``."""
    try:
        path = run_dir / "viewer.json"
        if not path.is_file():
            return None
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matches = (
                entry.get("adapter") == adapter_name
                if adapter_name is not None
                else bool(entry.get("viewer_window"))
            )
            if matches:
                return {
                    "viewer_window": entry.get("viewer_window") or "",
                    "viewer_url": entry.get("viewer_url") or "",
                    "viewer_session": entry.get("viewer_session") or "",
                    "viewer_cwd": entry.get("viewer_cwd") or "",
                    "viewer_command": entry.get("viewer_command") or "",
                }
        return None
    except (json.JSONDecodeError, OSError, KeyError, TypeError, UnicodeDecodeError):
        return None


class JournalTail:
    """Incremental journal.jsonl reader.

    The byte offset only ever advances past complete lines, so a partially
    flushed append is withheld until its newline lands. Truncation
    (size < offset) resets to the start; unparseable lines are skipped.
    """

    def __init__(self, run_dir: Path):
        self.path = run_dir / JOURNAL_FILE
        self._offset = 0

    def read_new(self) -> list[dict[str, Any]]:
        sig = _stat_sig(self.path)
        if sig is None:
            self._offset = 0
            return []
        size = sig[1]
        if size < self._offset:
            self._offset = 0
        if size == self._offset:
            return []
        with self.path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read(size - self._offset)
        complete = chunk.rfind(b"\n") + 1
        if complete == 0:
            return []
        self._offset += complete
        entries: list[dict[str, Any]] = []
        for raw in chunk[:complete].splitlines():
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries


# Pane geometry mirrors adapters.generic PANE_COLUMNS/PANE_LINES (not
# imported: that module drags in Policy/SignalWatcher, and this layer stays
# a pure observer).
_PANE_COLUMNS = 220
_PANE_LINES = 50
_HISTORY_LINES = 2000  # matches the dashboard RichLog max_lines

# pyte names SGR 33 "brown"; aixterm brights carry no underscore. Values here
# must stay in pyte's own (underscore-free) namespace: _rich_color re-applies
# the "bright" -> "bright_" transform after this remap, so a "bright_"-prefixed
# value would double up into "bright__yellow" (an invalid rich color).
# "bfightmagenta" is pyte 0.8.2's own BG_AIXTERM[105] typo (SGR 105).
_PYTE_COLOR_FIX = {
    "brown": "yellow",
    "brightbrown": "brightyellow",
    "bfightmagenta": "brightmagenta",
}
_HEX_DIGITS = set("0123456789abcdef")


def _rich_color(value: str) -> str | None:
    """pyte color -> rich color: 'default' (None), bare rrggbb hex
    (256/truecolor), or a named ANSI color."""
    if value == "default":
        return None
    if len(value) == 6 and set(value) <= _HEX_DIGITS:
        return f"#{value}"
    value = _PYTE_COLOR_FIX.get(value, value)
    if value.startswith("bright"):
        return "bright_" + value[6:]
    return value


_style_cache: dict[tuple, Style] = {}


def _char_style(key: tuple) -> Style:
    style = _style_cache.get(key)
    if style is None:
        fg, bg, bold, italics, underscore, strikethrough, reverse = key
        attrs = dict(
            bold=bold or None,
            italic=italics or None,
            underline=underscore or None,
            strike=strikethrough or None,
            reverse=reverse or None,
        )
        try:
            style = Style(color=_rich_color(fg), bgcolor=_rich_color(bg), **attrs)
        except ColorParseError:
            # A color name rich can't parse (e.g. a new pyte table entry) must
            # degrade to an uncolored run, not kill the poll worker — and with
            # it the app (textual workers default to exit_on_error=True).
            style = Style(**attrs)
        _style_cache[key] = style
    return style


def _render_row(row: dict) -> Text:
    """One pyte buffer row (sparse col -> Char dict) -> styled Text. Trailing
    default-background whitespace is trimmed; wide-char stub cells (data '')
    concatenate away naturally."""
    width = max(row, default=-1) + 1
    while width:
        ch = row[width - 1]
        if ch.data not in (" ", "") or ch.bg != "default" or ch.reverse:
            break
        width -= 1
    text = Text()
    run: list[str] = []
    prev_key: tuple | None = None
    for x in range(width):
        ch = row[x]
        key = (ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.strikethrough, ch.reverse)
        if key != prev_key and run:
            # prev_key is non-None whenever `run` is non-empty (a prior cell was buffered).
            text.append("".join(run), _char_style(prev_key))  # pyright: ignore[reportArgumentType]
            run.clear()
        prev_key = key
        run.append(ch.data)
    if run and prev_key is not None:
        text.append("".join(run), _char_style(prev_key))
    return text


# CSI sequences with a private/secondary marker (< > = ?) are terminal capability
# negotiation, never display SGR. pyte 0.8.2 ignores a leading marker and
# misdispatches them to SGR anyway: e.g. XTMODKEYS `CSI > 4 ; 2 m` (modifyOtherKeys,
# emitted at session start by Claude Code et al.) is read as SGR 4 = underline-on,
# leaving the whole log underlined until an exit-time disable a live capture never
# contains. Worse, a marker byte *inside* the params — gemini's `CSI > 4 ; ? m`,
# vim9's `CSI ? 4 m` — makes pyte dispatch with private=True to a handler that
# rejects the kwarg, a TypeError that would kill the poll worker (#111; upstream
# selectel/pyte#202, fixed in master but never released). Strip them before pyte
# sees them, matching the marker anywhere in the param bytes; a legitimate SGR
# carries no marker, so this can only remove non-display sequences (never printable
# text or genuine styling).
_PRIVATE_MARKER_SGR = re.compile(rb"\x1b\[[0-9;:<>=?]*[<>=?][0-9;:<>=?]*m")
# Alternate-screen switch sequences (DECSET/DECRST 1049/1047/47). A CLI fullscreen
# TUI (Claude Code's fullscreen renderer) switches here and repaints in place; pyte
# has no altscreen buffer, so the capture collapses to the final frame. Detecting
# the switch lets a consumer flag a fullscreen log instead of trusting it whole.
# Both enter (h) and exit (l) markers are matched: a finished run read from the
# max_bytes tail can seek past the start-of-session enter but still carries the
# exit emitted when the CLI quit.
_ALTSCREEN_MARKERS = (
    b"\x1b[?1049h",
    b"\x1b[?1049l",
    b"\x1b[?1047h",
    b"\x1b[?1047l",
    b"\x1b[?47h",
    b"\x1b[?47l",
)
# Cold-open altscreen detection scans the max_bytes-skipped prefix, but caps it so a
# huge log doesn't trigger a near-whole-file read that would defeat max_bytes. 8 MiB
# comfortably covers a multi-MB classic preamble before a TUI switches to the
# alternate screen (observed ~3.9 MB). A marker past the cap is missed only when the
# log already exceeds max_bytes at first open (that first read tail-seeks past the
# prefix); a view attached while the log was still small reads the whole stream from
# offset 0 and catches it regardless.
_ALTSCREEN_PREFIX_SCAN_CAP = 8 << 20
# A trailing, not-yet-terminated CSI at the end of a read: ESC, or ESC[ followed by
# only param/marker bytes with no final letter. Held back so a marker sequence split
# across two reads is filtered whole next time (the filter must see it complete).
_INCOMPLETE_CSI_TAIL = re.compile(rb"\x1b(?:\[[0-9;:<>=?]*)?\Z")


def _strip_private_marker_sgr(chunk: bytes) -> tuple[bytes, int]:
    """(filtered chunk, bytes held back). Drops private-marker SGR sequences and
    returns the length of an unterminated trailing CSI that should not be consumed
    yet, so the caller can re-read it next time and see the sequence whole."""
    m = _INCOMPLETE_CSI_TAIL.search(chunk)
    held = len(m.group()) if m else 0
    body = chunk[: len(chunk) - held] if held else chunk
    return _PRIVATE_MARKER_SGR.sub(b"", body), held


class _TolerantByteStream(pyte.ByteStream):
    """pyte 0.8.2 dispatches a private-marked CSI (marker byte in the params) with
    private=True to handlers that don't accept it, raising TypeError (upstream
    selectel/pyte#202; fixed in master Sep 2025, never released). Any parser
    exception here would kill the poll worker — and with it the app — so drop the
    unparseable sequence instead: the base method re-initializes the parser before
    re-raising, leaving the stream usable for the bytes that follow."""

    def _send_to_parser(self, data: str) -> bool | None:
        try:
            return super()._send_to_parser(data)
        except Exception:
            return None


class _CountingDeque(deque):
    """history.top replacement that counts rows permanently gone above the
    window: maxlen evictions plus the clear() from pyte's reset()/ED-3.
    Lets checkpoints live in absolute line coordinates while the deque
    slides (indices into the deque itself shift on every eviction)."""

    def __init__(self, maxlen: int | None = None):
        super().__init__(maxlen=maxlen)
        self.dropped = 0

    def append(self, item: Any) -> None:
        if self.maxlen is not None and len(self) == self.maxlen:
            self.dropped += 1
        super().append(item)

    def clear(self) -> None:
        self.dropped += len(self)
        super().clear()


@dataclass(frozen=True)
class LogIndex:
    """Maps journal log_pos byte offsets to rendered-line indices.

    Built by the poll worker right after render() and handed across to the
    UI thread, so it is frozen. Line indices match render() output rows
    one-to-one — which equals RichLog scroll lines while the log pane keeps
    wrap=False (the textual default)."""

    checkpoints: tuple[tuple[int, int], ...]  # ascending (file_offset, absolute_line)
    render_base: int  # absolute line index of render() row 0
    render_len: int  # rows in the last render()

    def line_for_offset(self, offset: int) -> int | None:
        """Rendered-line index for a byte offset into the log file; None
        when nothing is rendered. Between checkpoints the line is
        interpolated by byte fraction — exact for plain streaming output,
        bounded by the surrounding checkpoints for repaint-heavy segments.
        Offsets below the tail seek or evicted history clamp to the first
        row, offsets past EOF to the last."""
        if self.render_len <= 0:
            return None
        cps = self.checkpoints
        i = bisect.bisect_right(cps, (offset, float("inf"))) - 1
        if i < 0:
            absolute = 0
        elif i + 1 < len(cps) and cps[i + 1][0] > cps[i][0]:
            o0, l0 = cps[i]
            o1, l1 = cps[i + 1]
            absolute = l0 + round((offset - o0) * (l1 - l0) / (o1 - o0))
        else:
            absolute = cps[i][1]
        return max(0, min(absolute - self.render_base, self.render_len - 1))


class LogView:
    """Terminal-emulated view of a pane log (a raw pipe-pane capture full of
    cursor-addressed repaints). Bytes are fed through pyte so the stream
    collapses to what a real pane-sized terminal shows; scrolled-off lines
    land in history, so a finished run shows more than the final screen.
    Same stat-gated incremental contract as JournalTail: the first read seeks
    to the last max_bytes, truncation resets — the emulator included.

    Bytes are fed in checkpoint_bytes slices, recording (file offset,
    absolute cursor line) pairs so journal log_pos offsets map back to
    rendered lines (see LogIndex). Cursor-addressed repaints make the
    mapping approximate — the cursor sits wherever the CLI left it — but
    it is exact for plain streaming output.

    Known degradations, all strictly better than rendering the raw stream:
    a mid-stream first frame may be partial until the next repaint; altscreen
    (mode 1049) CLIs merge frames into one buffer; a human attaching to the
    tmux session resizes the pane away from our fixed geometry."""

    def __init__(
        self,
        path: Path,
        max_bytes: int = 262144,
        columns: int = _PANE_COLUMNS,
        lines: int = _PANE_LINES,
        history: int = _HISTORY_LINES,
        checkpoint_bytes: int = 4096,
    ):
        self.path = path
        self.max_bytes = max_bytes
        self._columns, self._lines, self._history = columns, lines, history
        self._checkpoint_bytes = checkpoint_bytes
        self._offset: int | None = None  # None until the file first appears
        self._row_cache: dict[int, Text] = {}  # id(history row) -> rendered
        # True once the stream enters the alternate screen (CLI fullscreen TUI):
        # pyte has no altscreen buffer, so those frames repaint the one screen in
        # place and the render collapses to the final frame. A consumer surfaces
        # this so a fullscreen capture isn't mistaken for the whole session.
        self.altscreen_seen = False
        self._reset_screen()

    def _reset_screen(self) -> None:
        self._screen = pyte.HistoryScreen(self._columns, self._lines, history=self._history)
        self._screen.history = self._screen.history._replace(
            top=_CountingDeque(maxlen=self._history)
        )
        self._stream = _TolerantByteStream(self._screen)
        self._row_cache.clear()
        self._checkpoints: list[tuple[int, int]] = []
        self._render_base = 0
        self._render_len = 0
        # A truncation restart re-reads from scratch, so re-detect altscreen too.
        self.altscreen_seen = False

    def _scan_prefix_for_altscreen(self, end: int) -> None:
        """One-time scan of bytes [0, end) for an altscreen switch marker, in
        overlapping windows so a marker straddling a window boundary still
        matches. `end` is the caller-bounded scan ceiling (see
        _ALTSCREEN_PREFIX_SCAN_CAP), not necessarily the full prefix. Sets
        altscreen_seen; best-effort, never raises."""
        overlap = max(len(m) for m in _ALTSCREEN_MARKERS) - 1
        window = 1 << 20
        try:
            with self.path.open("rb") as f:
                pos = 0
                while pos < end:
                    f.seek(pos)
                    # window + overlap so a marker straddling a window boundary is
                    # caught; clamped to `end` so the cap stays a strict ceiling.
                    buf = f.read(min(window + overlap, end - pos))
                    if any(seq in buf for seq in _ALTSCREEN_MARKERS):
                        self.altscreen_seen = True
                        return
                    if not buf:
                        return
                    pos += window
        except OSError:
            return

    def read_new(self) -> bool:
        """Feed any new bytes into the emulator; True when content changed."""
        sig = _stat_sig(self.path)
        if sig is None:
            if self._offset is None:
                return False
            self._offset = None
            self._reset_screen()
            return True
        size = sig[1]
        if self._offset is None:
            self._offset = max(0, size - self.max_bytes)
            # offsets at or before the tail seek clamp to the first line
            self._checkpoints = [(self._offset, 0)]
            # The render only emulates the max_bytes tail, but an altscreen switch
            # is a whole-session property whose markers often sit in the skipped
            # prefix (enter at session start). Scan that prefix once on open so a
            # cold-opened fullscreen log is still flagged; the tail is covered by
            # the per-chunk scan below.
            if self._offset > 0 and not self.altscreen_seen:
                self._scan_prefix_for_altscreen(min(self._offset, _ALTSCREEN_PREFIX_SCAN_CAP))
        elif size < self._offset:
            self._offset = 0
            self._reset_screen()
            self._checkpoints = [(0, 0)]
        if size == self._offset:
            return False
        with self.path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read(size - self._offset)
        if not self.altscreen_seen and any(seq in chunk for seq in _ALTSCREEN_MARKERS):
            self.altscreen_seen = True
        # Drop private-marker SGR (pyte misreads it as underline); hold back an
        # unterminated trailing CSI so a sequence split across reads is filtered
        # whole next time. `consumed` is the original byte span we commit here —
        # the file offset advances over it while pyte sees only the filtered bytes.
        filtered, held = _strip_private_marker_sgr(chunk)
        consumed = len(chunk) - held
        base, total = self._offset, len(filtered)
        top = self._screen.history.top
        for start in range(0, total, self._checkpoint_bytes):
            piece = filtered[start : start + self._checkpoint_bytes]
            # ByteStream buffers escape sequences split across feeds
            self._stream.feed(piece)
            # map back to an original-file offset (filtering only removes a few
            # marker bytes; the log_pos->line mapping is already approximate)
            end = start + len(piece)
            self._offset = base + (consumed if end >= total else round(end / total * consumed))
            # pyte types history.top as base deque; bmad-loop installs a _CountingDeque
            # (see class below) that adds `.dropped`.
            line = (
                top.dropped  # pyright: ignore[reportAttributeAccessIssue]
                + len(top)
                + self._screen.cursor.y
            )
            self._checkpoints.append((self._offset, line))
        self._offset = base + consumed
        # When a chunk is entirely an unterminated trailing CSI, consumed == 0 and
        # _offset holds short of EOF until more bytes complete the sequence — a few
        # held bytes re-read per poll. We deliberately don't advance past them: a
        # log that's still streaming self-heals on the next write, and one that
        # permanently ends mid-escape has nothing renderable there anyway (we'd
        # only risk eating a legitimately split sequence by forcing it through).
        # drop checkpoints whose lines evicted past the history horizon;
        # their offsets would clamp to line 0 anyway
        # `.dropped` is on the _CountingDeque bmad-loop installs as history.top.
        while (
            len(self._checkpoints) > 1
            and self._checkpoints[1][1]
            <= top.dropped  # pyright: ignore[reportAttributeAccessIssue]
        ):
            self._checkpoints.pop(0)
        return consumed > 0

    def index(self) -> LogIndex:
        """Snapshot for log_pos -> rendered-line lookups; reflects the most
        recent render() (render_base/render_len are set there)."""
        return LogIndex(tuple(self._checkpoints), self._render_base, self._render_len)

    def render(self) -> Text:
        """History + current screen as one styled Text, trailing blank rows
        dropped, capped to the newest history rows. History rows are detached
        dicts that never mutate, so their renders are memoized by id() —
        valid exactly while the row sits in the deque (the cache is re-keyed
        every call, keeping only surviving ids)."""
        screen = self._screen
        fresh: dict[int, Text] = {}
        rows: list[Text] = []
        for row in screen.history.top:
            text = self._row_cache.get(id(row))
            if text is None:
                text = _render_row(row)
            fresh[id(row)] = text
            rows.append(text)
        self._row_cache = fresh
        rows += [_render_row(screen.buffer[y]) for y in range(screen.lines)]
        while rows and not rows[-1].plain:
            rows.pop()
        front_drop = max(0, len(rows) - self._history)
        del rows[:front_drop]
        # `.dropped` is on the _CountingDeque bmad-loop installs as history.top.
        self._render_base = (
            screen.history.top.dropped + front_drop  # pyright: ignore[reportAttributeAccessIssue]
        )
        self._render_len = len(rows)
        return Text("\n").join(rows)


class ViewerView:
    """Captures a tmux pane via ``capture-pane`` and renders it through pyte.

    Unlike :class:`LogView` (incremental file stream), a viewer refresh
    captures a full terminal screen snapshot and replaces the previous
    content. The captured text includes raw ANSI escape sequences that the
    pyte emulator collapses into styled rows, just like a real terminal."""

    def __init__(
        self,
        target: str,
        mux: TerminalMultiplexer,
        columns: int = _PANE_COLUMNS,
        lines: int = _PANE_LINES,
        history: int = _HISTORY_LINES,
        start_line: int = -100,
    ):
        self._target = target
        self._mux = mux
        self._columns, self._lines, self._history = columns, lines, history
        self._start_line = start_line
        self._last_dims: tuple[int, int] | None = None

    @property
    def target(self) -> str:
        return self._target

    def resize(self, cols: int, lines: int) -> bool:
        """Adopt a new render geometry (the displayed pane's size). Returns
        True when it differs from the previous request — the caller then
        resizes the underlying mux window to match, so the attached TUI
        redraws at the size actually shown."""
        if (cols, lines) == self._last_dims:
            return False
        self._last_dims = (cols, lines)
        self._columns, self._lines = cols, lines
        return True

    def refresh(self) -> Text | None:
        """Capture the pane and re-render it. Returns a rendered :class:`Text`
        on success, or ``None`` when the pane is dead or the backend is
        unavailable."""
        try:
            raw = self._mux.capture_pane(self._target, start_line=self._start_line)
        except (MultiplexerError, OSError, Exception):
            # Pane may have been killed or mux transport failed.
            return None
        if not raw:
            return None
        # ByteStream expects bytes; encode the string capture to UTF-8 so
        # pyte can parse ANSI escape sequences in the usual way.
        if isinstance(raw, str):
            raw_bytes = raw.encode("utf-8", errors="replace")
        else:
            raw_bytes = raw
        # Full-screen viewer: each capture is a complete snapshot, so
        # reset() the screen before feeding the new data so the output
        # replaces the previous frame rather than appending.
        screen = pyte.HistoryScreen(self._columns, self._lines, history=self._history)
        screen.history = screen.history._replace(top=_CountingDeque(maxlen=self._history))
        # capture-pane separates rows with bare \n (no \r): without
        # linefeed-newline mode pyte keeps the column across the feed and
        # every row starts where the previous one ended — a staircase.
        screen.set_mode(pyte_modes.LNM)
        stream = _TolerantByteStream(screen)
        stream.feed(raw_bytes)
        # Strip trailing blanks, apply history cap.
        rows: list[Text] = []
        for row in screen.history.top:
            rows.append(_render_row(row))
        for y in range(screen.lines):
            rows.append(_render_row(screen.buffer[y]))
        while rows and not rows[-1].plain:
            rows.pop()
        front_drop = max(0, len(rows) - self._history)
        del rows[:front_drop]
        return Text("\n").join(rows)


def _open_session_start(journal_entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The session-start entry whose agent session is currently open: the last
    session-start with no later matching session-end. None when every started
    session has ended (or none started). The task_id is tracked as a string so
    the session-end match is byte-identical to what active_task_id compared."""
    open_entry: dict[str, Any] | None = None
    active: str | None = None
    for entry in journal_entries:
        kind = entry.get("kind")
        if kind == "session-start" and entry.get("task_id") is not None:
            active = str(entry["task_id"])
            open_entry = entry
        elif kind == "session-end" and str(entry.get("task_id")) == active:
            active = None
            open_entry = None
    return open_entry


def active_task_id(run_dir: Path, journal_entries: list[dict[str, Any]]) -> str | None:
    """Task whose agent session is currently open: the last session-start
    without a later session-end. Falls back to the newest file in logs/ —
    a tail attached mid-session has no start event in view."""
    entry = _open_session_start(journal_entries)
    if entry is not None:
        return str(entry["task_id"])
    try:
        logs = sorted(
            (run_dir / LOGS_DIR).glob("*.log"),
            key=lambda p: p.stat().st_mtime_ns,
        )
    except OSError:
        return None
    return logs[-1].stem if logs else None


@dataclass(frozen=True)
class ActiveAgent:
    """Identity of the agent currently driving a run, derived from the open
    session-start journal entry: the task/story it is working, the stage role,
    and the resolved adapter name + model."""

    task_id: str
    story_key: str
    role: str
    name: str
    model: str


def _story_key_from_task_id(task_id: str, role: str) -> str:
    """Recover the story key from a session task_id when the journal entry
    predates story-key stamping (#153 phase 1). The id is
    ``safe_segment(f"{story_key}-{part}-{seq}")`` where ``part`` is the role, or
    a workflow label for labeled plugin sessions — so peel the trailing
    ``-{part}-{seq}``: drop the numeric seq, then the recorded role when it
    matches (the common case), else one more ``-`` group (best-effort, since a
    label is not recoverable from the entry)."""
    head, sep, seq = task_id.rpartition("-")
    if not sep or not seq.isdigit():
        return task_id  # not the expected shape — best we can do
    if role and head.endswith(f"-{role}"):
        return head[: -(len(role) + 1)]
    parent = head.rpartition("-")[0]
    return parent or head


def active_agent(
    journal_entries: list[dict[str, Any]], policy_snapshot: dict[str, Any] | None
) -> ActiveAgent | None:
    """The agent currently driving the run, or None when no session is open.

    Journal-only: unlike :func:`active_task_id` there is no logs/ fallback — a
    tail attached mid-session carries no adapter identity. A session-start
    stamped since #153 phase 1 supplies the adapter name/model (and story_key)
    directly. For an older, unstamped entry the run's persisted
    ``policy_snapshot`` is rebuilt and resolved for the entry's role; when that
    yields nothing trustworthy (no/empty snapshot) the agent is unknown -> None.
    Never raises on a malformed entry."""
    try:
        entry = _open_session_start(journal_entries)
        if entry is None:
            return None
        task_id = str(entry.get("task_id", ""))
        role = str(entry.get("role") or "")
        if "adapter" in entry:
            name = str(entry.get("adapter", ""))
            model = str(entry.get("model", ""))
        else:
            rebuilt = policy.adapter_policy_from_snapshot(policy_snapshot)
            if rebuilt is None:
                return None
            resolved = rebuilt.resolved(role)
            name, model = resolved.name, resolved.model
        story_raw = entry.get("story_key")
        story_key = str(story_raw) if story_raw else _story_key_from_task_id(task_id, role)
        return ActiveAgent(task_id=task_id, story_key=story_key, role=role, name=name, model=model)
    except Exception:
        return None


def pending_decision(journal_entries: list[dict[str, Any]]) -> tuple[str, str] | None:
    """(dw_id, question) when the journal's last entry is a decision-pending
    announcement. The sweep prompter blocks on terminal input right after
    writing it, so any subsequent entry of any kind means the prompt was
    answered (or the sweep moved on) and the alert must clear."""
    if not journal_entries:
        return None
    last = journal_entries[-1]
    if last.get("kind") != "decision-pending":
        return None
    return str(last.get("dw_id", "?")), str(last.get("question", ""))


# --------------------------------------------- project-level artifact readers

# project root -> (config.yaml sig, ProjectPaths)
_paths_cache: dict[Path, tuple[_StatSig, bmadconfig.ProjectPaths]] = {}
# sprint-status.yaml path -> (sig or None for missing, parse or None)
_sprint_cache: dict[Path, tuple[_StatSig | None, sprintstatus.SprintStatus | None]] = {}
# deferred-work.md path -> (sig or None for missing, items or None)
_deferred_cache: dict[Path, tuple[_StatSig | None, list[DeferredItem] | None]] = {}
# project root -> (signature, pending decisions) — invalidated when the ledger,
# pre-answer store, or the set of run dirs changes
_missed_cache: dict[Path, tuple[Any, list]] = {}


def _project_paths(project: Path) -> bmadconfig.ProjectPaths | None:
    """BMAD artifact paths, stat-gated on config.yaml; None when the project
    is not initialized (or the config is unreadable)."""
    project = project.resolve()
    config_sig = _stat_sig(project / "_bmad" / "bmm" / "config.yaml")
    cached_paths = _paths_cache.get(project)
    if config_sig is not None and cached_paths is not None and cached_paths[0] == config_sig:
        return cached_paths[1]
    try:
        paths = bmadconfig.load_paths(project)
    except (bmadconfig.BmadConfigError, OSError):
        return None
    if config_sig is not None:
        _paths_cache[project] = (config_sig, paths)
    return paths


def sprint_overview(project: Path) -> sprintstatus.SprintStatus | None:
    """Parsed sprint-status.yaml, or None when unavailable (uninitialized
    project, missing file, bad YAML). Stat-gated on both config.yaml and the
    sprint file; the same object is returned while the file is unchanged."""
    paths = _project_paths(project)
    if paths is None:
        return None
    sprint_path = paths.sprint_status
    sig = _stat_sig(sprint_path)
    cached = _sprint_cache.get(sprint_path)
    if cached is not None and cached[0] == sig:
        return cached[1]
    overview: sprintstatus.SprintStatus | None = None
    if sig is not None:
        try:
            overview = sprintstatus.load(sprint_path)
        except (sprintstatus.SprintStatusError, OSError):
            overview = None
    _sprint_cache[sprint_path] = (sig, overview)
    return overview


def stories_overview(project: Path, spec_folder: str) -> list[stories.StoryRow] | None:
    """The stories-mode board for a run's pinned spec folder: one StoryRow per
    ``stories.yaml`` entry joined with the live on-disk state of its id-keyed
    story spec. None when unavailable (no spec folder, or a missing/invalid
    ``stories.yaml``).

    Unlike :func:`sprint_overview` this is per-run (keyed by the run's
    ``spec_folder``) and is re-derived on every call rather than stat-gated: the
    per-story spec files under ``stories/`` change independently, so there is no
    single file to gate on. It reads a handful of small files, called on the
    dashboard's rescan cadence."""
    if not spec_folder:
        return None
    folder = stories.resolve_spec_folder(project, spec_folder)
    try:
        return stories.story_rows(folder)
    except stories.StoriesError:
        return None


@dataclass(frozen=True)
class DeferredItem:
    id: str
    title: str
    status: str
    done: bool
    severity: str | None  # normalized: critical/high/medium/low, None unknown
    body: str
    legacy: bool = False  # pre-DW-format item (older BMAD-method ledgers)
    option_key: str | None = None  # highlight-restore identity; None -> id


def deferred_entries(project: Path) -> list[DeferredItem] | None:
    """All entries from deferred-work.md in file order — canonical DW entries
    plus tolerantly-parsed legacy items — or None when unavailable
    (uninitialized project, missing/unreadable file). Stat-gated; the same
    list object is returned while the file is unchanged."""
    paths = _project_paths(project)
    if paths is None:
        return None
    ledger_path = paths.deferred_work
    sig = _stat_sig(ledger_path)
    cached = _deferred_cache.get(ledger_path)
    if cached is not None and cached[0] == sig:
        return cached[1]
    items: list[DeferredItem] | None = None
    if sig is not None:
        try:
            text = ledger_path.read_text(encoding="utf-8")
        except OSError:
            items = None
        else:
            merged: list[tuple[int, DeferredItem]] = []
            for e in deferredwork.parse_ledger(text):
                merged.append(
                    (
                        e.span[0],
                        DeferredItem(
                            id=e.id,
                            title=e.title,
                            status=e.status,
                            done=bool(e.status) and e.status.split()[0] == "done",
                            severity=deferredwork.field_severity(e.body),
                            body=e.body,
                        ),
                    )
                )
            for n, le in enumerate(deferredwork.parse_legacy(text), start=1):
                merged.append(
                    (
                        le.span[0],
                        DeferredItem(
                            id=le.id or f"L{n}",
                            title=le.title,
                            status="done (legacy)" if le.done else "open (legacy)",
                            done=le.done,
                            severity=le.severity,
                            body=le.body,
                            legacy=True,
                            option_key=f"legacy:{le.key}",
                        ),
                    )
                )
            merged.sort(key=lambda pair: pair[0])
            items = [item for _, item in merged]
    _deferred_cache[ledger_path] = (sig, items)
    return items


def pending_missed_decisions(project: Path) -> list:
    """Deferred-work decisions earlier sweeps surfaced but no one answered (a
    list of sweep.Decision). Cached on a signature of the ledger, the pre-answer
    store, and the set of run dirs — a new sweep (new run dir) or an answer
    (store/ledger change) invalidates it. Empty when the project is unavailable."""
    from .. import decisions  # lazy: pulls sweep; keep this module import-light

    paths = _project_paths(project)
    if paths is None:
        return []
    project = project.resolve()
    sig = (
        _stat_sig(paths.deferred_work),
        _stat_sig(decisions.store_path(project)),
        tuple(d.name for d in list_run_dirs(project)),
    )
    cached = _missed_cache.get(project)
    if cached is not None and cached[0] == sig:
        return cached[1]
    try:
        result = decisions.pending_missed_decisions(project)
    except (bmadconfig.BmadConfigError, OSError):
        result = []
    _missed_cache[project] = (sig, result)
    return result
