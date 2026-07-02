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
from rich.style import Style
from rich.text import Text

from .. import bmadconfig, deferredwork, sprintstatus
from ..adapters.multiplexer import MultiplexerError, get_multiplexer
from ..gates import ATTENTION_FILE
from ..journal import JOURNAL_FILE, LOGS_DIR, STATE_FILE, load_state
from ..model import RunState
from ..process_host import ProcessHostError
from ..runs import list_run_dirs, probe_liveness, read_pid_identity, session_name

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
        # A misconfigured host (bad BMAD_AUTO_PROCESS_HOST) stays a hard error on
        # CLI decision paths, but the display layer must degrade, not crash: the
        # dashboard poll worker has no except and would take the whole app down.
        return "unknown"


def _session_liveness(run_id: str) -> str:
    # An absent multiplexer / dead query proves nothing about a legacy run, so the
    # only positive signal is a live session; everything else is 'unknown'.
    mux = get_multiplexer()
    if not mux.available():
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


# state.json path -> (stat sig, (run_type, started_at, finished, paused, stopped, crashed))
_header_cache: dict[Path, tuple[_StatSig, tuple[str, str, bool, bool, bool, bool]]] = {}


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
            run_type, started_at, finished, paused, stopped, crashed = cached[1]
        else:
            try:
                doc = json.loads(state_path.read_text(encoding="utf-8"))
                run_type = str(doc.get("run_type", "story"))
                started_at = str(doc.get("started_at", ""))
                finished = bool(doc.get("finished", False))
                paused = doc.get("paused_reason") is not None
                stopped = bool(doc.get("stopped", False))
                crashed = bool(doc.get("crashed", False))
            except (OSError, json.JSONDecodeError):
                out.append(RunInfo(run_dir.name, run_dir, "?", "", UNKNOWN))
                continue
            if sig is not None:
                _header_cache[state_path] = (
                    sig,
                    (run_type, started_at, finished, paused, stopped, crashed),
                )
        status = _classify(finished, paused, stopped, crashed, run_dir)
        out.append(RunInfo(run_dir.name, run_dir, run_type, started_at, status))
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

# pyte names SGR 33 "brown"; aixterm brights carry no underscore.
_PYTE_COLOR_FIX = {"brown": "yellow", "brightbrown": "bright_yellow"}
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
        style = _style_cache[key] = Style(
            color=_rich_color(fg),
            bgcolor=_rich_color(bg),
            bold=bold or None,
            italic=italics or None,
            underline=underscore or None,
            strike=strikethrough or None,
            reverse=reverse or None,
        )
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
            text.append("".join(run), _char_style(prev_key))
            run.clear()
        prev_key = key
        run.append(ch.data)
    if run and prev_key is not None:
        text.append("".join(run), _char_style(prev_key))
    return text


# CSI sequences with a private/secondary marker (< > = ?) are terminal capability
# negotiation, never display SGR. pyte 0.8.2 ignores the marker and misdispatches
# them to SGR anyway: e.g. XTMODKEYS `CSI > 4 ; 2 m` (modifyOtherKeys, emitted at
# session start by Claude Code et al.) is read as SGR 4 = underline-on, leaving the
# whole log underlined until an exit-time disable a live capture never contains.
# Strip them before pyte sees them; a legitimate SGR carries no marker, so this can
# only remove non-display sequences (never printable text or genuine styling).
_PRIVATE_MARKER_SGR = re.compile(rb"\x1b\[[<>=?][0-9;:]*m")
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
        self._stream = pyte.ByteStream(self._screen)
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
            line = top.dropped + len(top) + self._screen.cursor.y
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
        while len(self._checkpoints) > 1 and self._checkpoints[1][1] <= top.dropped:
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
        self._render_base = screen.history.top.dropped + front_drop
        self._render_len = len(rows)
        return Text("\n").join(rows)


def active_task_id(run_dir: Path, journal_entries: list[dict[str, Any]]) -> str | None:
    """Task whose agent session is currently open: the last session-start
    without a later session-end. Falls back to the newest file in logs/ —
    a tail attached mid-session has no start event in view."""
    active: str | None = None
    for entry in journal_entries:
        kind = entry.get("kind")
        if kind == "session-start" and entry.get("task_id") is not None:
            active = str(entry["task_id"])
        elif kind == "session-end" and str(entry.get("task_id")) == active:
            active = None
    if active is not None:
        return active
    try:
        logs = sorted(
            (run_dir / LOGS_DIR).glob("*.log"),
            key=lambda p: p.stat().st_mtime_ns,
        )
    except OSError:
        return None
    return logs[-1].stem if logs else None


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
