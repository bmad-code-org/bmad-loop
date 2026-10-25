"""Read-only filesystem observation layer for the TUI.

Everything the dashboard shows comes from the run-dir artifacts the engine
already writes atomically: state.json (os.replace), journal.jsonl
(append-only), logs/<task-id>.log, ATTENTION. This module never imports
textual — it is plain stdlib + core modules + pyte/rich, fully unit-testable,
and the screens own the poll cadence.

The run inventory itself (``discover_runs`` and its status/liveness closure) now
lives in :mod:`bmad_loop.runs` — the core CLI's ``list`` needs it and must not
drag pyte/rich in with it (#650). It is re-exported below, so ``data.<name>``
stays the TUI-facing spelling.

All readers are stat-gated: parse results are cached while the file's
(mtime_ns, size, inode) is unchanged. Liveness is the exception — a dying engine
changes no file, so the pid is re-checked on every call.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyte
from rich.color import ColorParseError
from rich.style import Style
from rich.text import Text

from .. import (
    bmadconfig,
    deferredwork,
    policy,
    sprintstatus,
    stories,
    ticketmodel,
    tickettree,
    worksource,
)
from ..gates import ATTENTION_FILE
from ..journal import JOURNAL_FILE, LOGS_DIR, STATE_FILE, load_state, unreadable_line_entry
from ..model import RunState
from ..platform_util import (
    UnconfinedWriteError,
    is_link_like,
    recorded_root_identity,
    require_root_pinned,
    resolve_or_lexical,
    stale_device_record,
)

# Run-inventory names relocated to bmad_loop.runs (#650) and re-exported here so
# tui.data stays the TUI-facing surface (`data.discover_runs`, `data.RUNNING`,
# monkeypatched `data.liveness` in tests). Each pin is load-bearing: without it
# ruff F401 autofix deletes the re-export and every `data.<name>` consumer breaks.
from ..runs import CRASHED  # noqa: F401 — re-export
from ..runs import FINISHED  # noqa: F401 — re-export
from ..runs import INTERRUPTED  # noqa: F401 — re-export
from ..runs import PAUSED  # noqa: F401 — re-export
from ..runs import RUNNING  # noqa: F401 — re-export
from ..runs import STOPPED  # noqa: F401 — re-export
from ..runs import RunInfo  # noqa: F401 — re-export
from ..runs import _header_cache  # noqa: F401 — re-export
from ..runs import _session_liveness  # noqa: F401 — re-export
from ..runs import discover_runs  # noqa: F401 — re-export
from ..runs import (
    STOP_REQUEST_FILE,
    UNKNOWN,
    _classify,
    _stat_sig,
    _StatSig,
    list_run_dirs,
    liveness,
    rebase_recorded_project_path,
)

# ------------------------------------------------------------- run watching


def _describe(exc: BaseException) -> str:
    """``<Class>: <message>`` — the operator-facing spelling of a folded fault."""
    return f"{type(exc).__name__}: {exc}"


def _classified_sig(path: Path) -> tuple[_StatSig | None, str | None]:
    """``(sig, fault)``: ``(sig, None)`` for a present file, ``(None, None)`` for
    genuine absence, ``(None, "<Class>: <msg>")`` for a stat that failed for any
    other reason. ``_stat_sig`` folds the last two into one ``None``; the readers
    below must tell "not there" (reset / nothing to show) from "could not look"
    (keep position, flag it)."""
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError as e:
        return None, _describe(e)
    return (st.st_mtime_ns, st.st_size, st.st_ino), None


class RunWatcher:
    """Stat-gated views of one run dir. Never raises on missing or mid-write
    files: state() keeps returning the last good parse.

    Keeping the last good parse is not the same as presenting it as current
    (DW-472): ``state_fault`` names why the state shown is stale — a state.json
    that will not parse, or one that has gone or cannot be stat'd after a good
    read — and clears on the next good parse, or when the last good read's file
    stats back unchanged. It is published only once the SAME
    signature has failed on two consecutive looks: a mid-write file is re-stat'd
    every poll (``_state_sig`` is not advanced on a failure), and a writer still
    working changes the signature, so a torn read that the next write heals never
    flags; one the file is still sitting in a poll later is not torn, it is
    broken. ``attention_fault`` does the same for ATTENTION (DW-475), without the
    two-look rule — that file is appended, never parsed, so there is no torn read
    to forgive.

    Every reload after the first good parse is pinned to that parse
    (``load_state(pin=...)``): a run dir or ``runs/`` swapped for a link to another
    tree raises ``UnconfinedReadError``, an ``OSError``, so the watcher keeps the
    last good state and ``state_fault`` names the refusal rather than showing the
    foreign record. The first read has nothing to pin to and reads by path."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self._state_sig: _StatSig | None = None
        self._state: RunState | None = None
        self.state_fault: str | None = None
        # (signature, why) of the last failed look, awaiting its confirming repeat
        self._suspect: tuple[_StatSig | None, str] | None = None
        self._attention_sig: _StatSig | None = None
        self._attention = ""
        self.attention_fault: str | None = None

    def state(self) -> RunState | None:
        sig, stat_fault = _classified_sig(self.run_dir / STATE_FILE)
        if sig is None:
            # Before the first write there is nothing to be stale; after a good read,
            # a state.json that vanished or cannot be stat'd leaves that read stale.
            if self._state is not None:
                self._note_state_fault(
                    None,
                    (
                        f"state.json cannot be stat'd ({stat_fault})"
                        if stat_fault
                        else "state.json is gone"
                    ),
                )
            return self._state
        if sig == self._state_sig:
            # The last good read's own file is back unchanged, so it is current again.
            # A finished or paused run never rewrites state.json: waiting for a fresh
            # parse to clear the flag would leave a recovered header stale forever.
            self._suspect = None
            self.state_fault = None
            return self._state
        try:
            self._state = load_state(self.run_dir, pin=self._state)
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            self._note_state_fault(sig, f"state.json unreadable ({_describe(e)})")
            return self._state  # keep last good — flagged once the fault persists
        self._state_sig = sig
        self._suspect = None
        self.state_fault = None
        return self._state

    def _note_state_fault(self, sig: _StatSig | None, why: str) -> None:
        if self._suspect is not None and self._suspect[0] == sig:
            self.state_fault = why
        self._suspect = (sig, why)

    def liveness(self) -> str:
        return liveness(self.run_dir)

    def status(self) -> str:
        state = self.state()
        if state is None:
            return UNKNOWN
        return _classify(state.finished, state.paused, state.stopped, state.crashed, self.run_dir)

    def stopping(self) -> bool:
        """True when a stop request of *either* mode is pending for this run (its
        control file is present) — a bare existence read for the run-header pending
        line, mirroring runs.graceful_stop_requested. Deliberately mode-blind: a run
        with a hard request lodged (#319) is stopping too, and the request is on disk
        only for the seconds it takes the engine to honor it. The caller gates on a
        RUNNING status so a file lingering on a stopped run doesn't read as
        still-stopping."""
        return (self.run_dir / STOP_REQUEST_FILE).is_file()

    def attention(self) -> str:
        """ATTENTION's text, last good read kept across a fault — which
        ``attention_fault`` then names until a read succeeds (DW-475). Absence is
        no fault: the engine writes the file only when it has something to say."""
        path = self.run_dir / ATTENTION_FILE
        sig, fault = _classified_sig(path)
        if fault is not None:
            self.attention_fault = f"ATTENTION cannot be stat'd ({fault})"
            return self._attention
        if sig is None:
            self.attention_fault = None
            return self._attention
        if sig != self._attention_sig:
            try:
                self._attention = path.read_text(encoding="utf-8")
                self._attention_sig = sig
            except (OSError, UnicodeDecodeError) as e:
                self.attention_fault = f"ATTENTION unreadable ({_describe(e)})"
                return self._attention
        self.attention_fault = None
        return self._attention


class JournalTail:
    """Incremental journal.jsonl reader.

    The byte offset only ever advances past complete lines, so a partially
    flushed append is withheld until its newline lands — and ``Journal.append``'s
    tail heal is what makes that newline arrive, on the fragment's own line,
    rather than as the head of the next record. Truncation (size < offset) resets
    to the start.

    A COMPLETE line (one its newline has landed for) that will not parse is
    reported as :func:`journal.unreadable_line_entry` in the position it occupied,
    so a lost record is visible in the live pane rather than skipped. An
    UNTERMINATED final line is not a marker here — it is still withheld, because
    this reader cannot yet tell a torn record from one being written. That is the
    one place this reader deliberately diverges from ``Journal.entries``, which
    reads the file whole and so mints a marker for a trailing fragment
    immediately: here the marker appears once the heal terminates the fragment.

    An observer, so it never refuses: unlike a pinned ``Journal.entries`` it reads
    by path and keeps returning entries. Given the run's held state
    (``read_new(pin=state)``), a run dir that no longer ``lstat``s as the directory
    ``state.run_dir_identity`` records (a renumbered ``st_dev`` alone excepted, as
    the writers except it), or — with or without a record — a run dir or
    ``journal.jsonl`` that is a link leaves ``fault`` naming the swap — the rows shown may be another tree's. It writes nothing.
    """

    def __init__(self, run_dir: Path):
        self.path = run_dir / JOURNAL_FILE
        self._offset = 0
        # Why the last look at the journal failed, or None (DW-475). A stat or read
        # FAULT keeps the offset — only genuine absence or a shrink (truncation /
        # rewrite) restarts from zero — so recovering re-reads nothing twice.
        self.fault: str | None = None

    def read_new(self, *, pin: RunState | None = None) -> list[dict[str, Any]]:
        entries = self._read_new()
        if self.fault is None:
            self.fault = self._swap_fault(pin)
        return entries

    def _swap_fault(self, pin: RunState | None) -> str | None:
        """Why the journal read by path may not be this run's, or None. Check only:
        the read already happened, and observation degrades rather than refusing."""
        if pin is None:
            return None
        run_dir = self.path.parent
        if not run_dir.exists() and not run_dir.is_symlink():
            return None  # gone, not swapped: the stat above already reads it as absent
        record = pin.run_dir_identity
        if record is not None and not stale_device_record(run_dir, record):
            try:
                require_root_pinned(run_dir, recorded_root_identity(record))
            except UnconfinedWriteError:
                return (
                    f"run dir is no longer the directory this run recorded ({run_dir}) "
                    "— journal rows may be from another tree"
                )
        if is_link_like(run_dir):
            return f"run dir is a link ({run_dir}) — journal rows may be from another tree"
        if is_link_like(self.path):
            return "journal.jsonl is a link — journal rows may be from another file"
        return None

    def _read_new(self) -> list[dict[str, Any]]:
        sig, fault = _classified_sig(self.path)
        if fault is not None:
            self.fault = f"journal.jsonl cannot be stat'd ({fault})"
            return []
        self.fault = None
        if sig is None:
            self._offset = 0
            return []
        size = sig[1]
        if size < self._offset:
            self._offset = 0
        if size == self._offset:
            return []
        try:
            with self.path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
        except (FileNotFoundError, NotADirectoryError):
            self._offset = 0  # gone between the stat and the open: absence
            return []
        except OSError as e:
            self.fault = f"journal.jsonl unreadable ({_describe(e)})"
            return []
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
                # The shared minter, never a local dict: one shape for this reader and
                # `Journal.entries`. Byte length off the RAW line, before the
                # `errors="replace"` decode above, which can change the count.
                entries.append(unreadable_line_entry(len(raw)))
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
        # Why the cold-open prefix scan could not run, or None (DW-475). A False
        # `altscreen_seen` then means "not looked", not "not fullscreen", and the
        # consumer must say so rather than present the tail as the whole session.
        self.altscreen_scan_fault: str | None = None
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
        self.altscreen_scan_fault = None

    def _scan_prefix_for_altscreen(self, end: int) -> None:
        """One-time scan of bytes [0, end) for an altscreen switch marker, in
        overlapping windows so a marker straddling a window boundary still
        matches. `end` is the caller-bounded scan ceiling (see
        _ALTSCREEN_PREFIX_SCAN_CAP), not necessarily the full prefix. Sets
        altscreen_seen; best-effort, never raises — a read fault is recorded in
        altscreen_scan_fault instead."""
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
        except OSError as e:
            self.altscreen_scan_fault = _describe(e)
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


def _open_session_start(journal_entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The session-start entry whose agent session is currently open: the last
    session-start with no later matching session-end. None when every started
    session has ended (or none started). The task_id is tracked as a string so
    the session-end match is byte-identical to what active_task_id compared."""
    entry, _ = _open_session_start_indexed(journal_entries)
    return entry


def _open_session_start_indexed(
    journal_entries: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, int]:
    """`_open_session_start` plus the entry's index in `journal_entries` (-1 when
    None), so a caller can scan the entries that FOLLOW the open start — the
    #680 idle events belong to a session only from its start onward, and a
    `session-idle` left behind by an earlier session with a reused task id must
    not be read as this one's."""
    open_entry: dict[str, Any] | None = None
    open_index = -1
    active: str | None = None
    for index, entry in enumerate(journal_entries):
        kind = entry.get("kind")
        if kind == "session-start" and entry.get("task_id") is not None:
            active = str(entry["task_id"])
            open_entry = entry
            open_index = index
        elif kind == "session-end" and str(entry.get("task_id")) == active:
            active = None
            open_entry = None
            open_index = -1
    return open_entry, open_index


def _idle_since(journal_entries: list[dict[str, Any]], start: int, task_id: str) -> float | None:
    """Wall timestamp the open session's current idle stretch began (#680), or
    None when it is not idle: the `since_ts` of the last `session-idle` for
    `task_id` after index `start`, unless a later `session-active` for the same
    task closed it. Malformed or non-finite timestamps are ignored without
    erasing an earlier valid stretch. Never raises on a malformed entry."""
    since: float | None = None
    for entry in journal_entries[start + 1 :]:
        if str(entry.get("task_id")) != task_id:
            continue
        kind = entry.get("kind")
        if kind == "session-idle":
            raw = entry.get("since_ts")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                try:
                    stamp = float(raw)
                except OverflowError:
                    continue
                if math.isfinite(stamp):
                    since = stamp
        elif kind == "session-active":
            since = None
    return since


def _idle_tool(journal_entries: list[dict[str, Any]], start: int, task_id: str) -> str | None:
    """The tool call the open session's current idle stretch is waiting on, or
    None: the string `in_tool` of the last `session-idle` for `task_id` after
    index `start` — a closed slug the adapter chose — unless a later
    `session-active` for the same task closed the stretch. None too when that
    `session-idle` carries no `in_tool` (a CLI that cannot tell) or a non-string
    one. `_idle_since`'s walk; never raises on a malformed entry."""
    tool: str | None = None
    for entry in journal_entries[start + 1 :]:
        if str(entry.get("task_id")) != task_id:
            continue
        kind = entry.get("kind")
        if kind == "session-idle":
            raw = entry.get("in_tool")
            tool = raw if isinstance(raw, str) else None
        elif kind == "session-active":
            tool = None
    return tool


def _quota_wait(
    journal_entries: list[dict[str, Any]], start: int, task_id: str
) -> tuple[float | None, str | None]:
    """The open session's usage-limit quota wait (DW-396, #610): ``(since,
    resets_at)`` from the last `quota-wait-started` for `task_id` after index
    `start` — its journal `ts` and its string `resets_at` (None when the wait
    line named no reset) — or ``(None, None)`` when no wait is open: a later
    `quota-wait-ended` for the same task closes it, and a wait can re-open
    after one ends, so the latest wins. A `quota-wait-started` without a finite
    numeric `ts` is skipped without erasing an earlier open wait, as
    `_idle_since` skips a malformed stamp. Never raises on a malformed entry."""
    since: float | None = None
    resets: str | None = None
    for entry in journal_entries[start + 1 :]:
        if str(entry.get("task_id")) != task_id:
            continue
        kind = entry.get("kind")
        if kind == "quota-wait-started":
            raw = entry.get("ts")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                try:
                    stamp = float(raw)
                except OverflowError:
                    continue
                if math.isfinite(stamp):
                    since = stamp
                    raw_resets = entry.get("resets_at")
                    resets = raw_resets if isinstance(raw_resets, str) and raw_resets else None
        elif kind == "quota-wait-ended":
            since = None
            resets = None
    return since, resets


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
        # Safe fold: this is only the fallback GUESS at which log to show; the next poll
        # re-guesses, and a logs/ that cannot be listed holds nothing LogView could show.
        return None
    return logs[-1].stem if logs else None


@dataclass(frozen=True)
class UnreadableAgent:
    """An open session whose agent identity could not be derived (DW-474): a
    malformed journal entry or policy snapshot. The header shows it as such
    rather than dropping the agent line, which reads as "no session open"."""

    error: str


# What the body of `active_agent` can raise on malformed journal data or a
# malformed snapshot: a non-dict entry (AttributeError), a missing or ill-typed
# field (KeyError / TypeError), a bad value (ValueError). Anything else is a
# programming error and propagates.
_MALFORMED_AGENT_FAULTS = (AttributeError, KeyError, TypeError, ValueError)


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
    # Wall timestamp the session's open idle stretch began (#680) — the `since_ts`
    # of the last `session-idle` not closed by a later `session-active` — or None
    # while the transcript is moving. The header renders `· idle <age>` from it
    # only when set. APPENDED, so every positional construction stays valid.
    idle_since: float | None = None
    # The tool call that open idle stretch is waiting on — its `session-idle`'s
    # `in_tool` slug (a Claude built-in name, `mcp`, `other`, `none`, `unknown`)
    # — or None when no stretch is open or the CLI cannot tell. The header
    # appends `· in <tool>` from it unless it is `none`. APPENDED, like
    # `idle_since`.
    idle_tool: str | None = None
    # Wall timestamp the session's open usage-limit quota wait began (DW-396) —
    # the journal `ts` of the last `quota-wait-started` not closed by a later
    # `quota-wait-ended` — or None when no wait is open. The header renders
    # `· quota wait <age>` from it IN PLACE OF the idle notice: the transcript
    # sits still by design while the CLI waits out a usage-window reset, so the
    # session is neither idle nor stuck. APPENDED, like `idle_since`.
    quota_wait_since: float | None = None
    # That open wait's parsed reset (#610): ISO-8601, or the raw clause text
    # when it did not resolve; None when the wait line named none. The header
    # appends `· resets <time>` from it. APPENDED.
    quota_resets_at: str | None = None


def _story_key_from_task_id(task_id: str, role: str) -> str:
    """Recover the story key from a session task_id when the journal entry
    predates story-key stamping (#153 phase 1). The id is
    ``safe_segment(f"{story_key}-{part}-{seq}{gen}")`` where ``part`` is the role, or
    a workflow label for labeled plugin sessions, and ``gen`` is a ``-g<N>`` re-arm
    generation suffix emitted only above zero (#705). Peel one optional terminal
    generation suffix, then peel the trailing ``-{part}-{seq}``: drop the numeric
    seq, then the recorded role when it matches (the common case), else one more
    ``-`` group (best-effort, since a label is not recoverable from the entry).
    Malformed shapes return the original task id unchanged."""
    original = task_id
    task_id = re.sub(r"-g[1-9][0-9]*\Z", "", task_id)
    head, sep, seq = task_id.rpartition("-")
    if not sep or not seq.isdigit():
        return original  # not the expected shape — best we can do
    if role and head.endswith(f"-{role}"):
        return head[: -(len(role) + 1)]
    parent = head.rpartition("-")[0]
    return parent or head


def active_agent(
    journal_entries: list[dict[str, Any]], policy_snapshot: dict[str, Any] | None
) -> ActiveAgent | UnreadableAgent | None:
    """The agent currently driving the run, or None when no session is open.

    Journal-only: unlike :func:`active_task_id` there is no logs/ fallback — a
    tail attached mid-session carries no adapter identity. A session-start
    stamped since #153 phase 1 supplies the adapter name/model (and story_key)
    directly. For an older, unstamped entry the run's persisted
    ``policy_snapshot`` is rebuilt and resolved for the entry's role; when that
    yields nothing trustworthy (no/empty snapshot) the agent is unknown -> None.
    Never raises on a malformed entry: that answers :class:`UnreadableAgent`
    carrying the fault (DW-474), never ``None`` — which means no session is open.
    Only the malformed-data faults are caught; any other exception is a bug and
    propagates."""
    try:
        entry, start_index = _open_session_start_indexed(journal_entries)
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
        quota_since, quota_resets = _quota_wait(journal_entries, start_index, task_id)
        return ActiveAgent(
            task_id=task_id,
            story_key=story_key,
            role=role,
            name=name,
            model=model,
            idle_since=_idle_since(journal_entries, start_index, task_id),
            idle_tool=_idle_tool(journal_entries, start_index, task_id),
            quota_wait_since=quota_since,
            quota_resets_at=quota_resets,
        )
    except _MALFORMED_AGENT_FAULTS as e:
        return UnreadableAgent(error=f"malformed session-start or snapshot: {_describe(e)}")


@dataclass(frozen=True)
class SweepOutcomes:
    """The run's auto-sweep ledger (#501): triggers whose child sweep was
    delivered, and triggers refused with their closed ``SWEEP_REFUSED_*`` slug.
    Disjoint here, though not on disk: a child that started then failed is
    latched into ``sweeps_triggered`` AND recorded ``failed`` (model.py), and
    that trigger was not delivered — so it lands in ``refused`` only."""

    triggered: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()


def sweep_outcomes(state: RunState) -> SweepOutcomes:
    """Project ``sweeps_triggered``/``sweeps_refused`` off the parsed state.json —
    the same RunState ``status``, ``status --json`` and ``diagnose`` read — in
    file order. A pre-#501 state.json carries neither key and reads empty."""
    return SweepOutcomes(
        triggered=tuple(t for t in state.sweeps_triggered if t not in state.sweeps_refused),
        refused=tuple(state.sweeps_refused.items()),
    )


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

# project root -> (sigs of every BMAD config source, ProjectPaths)
_paths_cache: dict[Path, tuple[tuple[object, ...], bmadconfig.ProjectPaths]] = {}
# sprint-status.yaml path -> (sig or None for missing, parse or None)
_sprint_cache: dict[Path, tuple[_StatSig | None, sprintstatus.SprintStatus | None]] = {}
# deferred-work.md path -> (sig or None for missing, items or None)
_deferred_cache: dict[Path, tuple[_StatSig | None, list[DeferredItem] | None]] = {}
# project root -> (signature, pending decisions) — invalidated when the ledger,
# pre-answer store, or the set of run dirs changes. Answers only: a fault is never
# cached, so the next rescan retries it.
_missed_cache: dict[Path, tuple[Any, MissedDecisions]] = {}
# project spelling as asked -> why `_project_paths` last answered None for it
_paths_fault: dict[Path, str] = {}
# the same two, for the tickets-mode resolution (`want_tickets`): a mixed v6/v7
# install answers v6 paths without it and v7 paths with it
_tickets_paths_cache: dict[Path, tuple[tuple[object, ...], bmadconfig.ProjectPaths]] = {}
_tickets_paths_fault: dict[Path, str] = {}


def _config_source_sig(path: Path) -> object:
    """A config source's cache signature, distinguishing what `_stat_sig` folds into
    one `None`: absent, present but failing (an unreadable directory), and a link
    whose target is gone or loops. `load_paths` refuses the last two where it reads
    the first as "no layer", so appearing at an absent path must invalidate."""
    try:
        st = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        return ("error", e.errno)
    return ((st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode), _stat_sig(path))


def _project_paths(project: Path, *, want_tickets: bool = False) -> bmadconfig.ProjectPaths | None:
    """BMAD artifact paths, stat-gated on every config source `load_paths` reads
    (the four central TOML layers, the legacy config.yaml and the v7 marker that
    picks the era), so an edit to any of them is seen on the next call; None when
    the project is not initialized (or the config is unreadable) — why is left in
    ``_paths_fault`` (``_tickets_paths_fault`` under ``want_tickets``) under the
    spelling asked, for a caller that must say so rather than answer empty.
    ``want_tickets`` resolves as a tickets run does, so a mixed install answers v7."""
    asked = project
    project = resolve_or_lexical(project)
    cache, faults = (
        (_tickets_paths_cache, _tickets_paths_fault)
        if want_tickets
        else (_paths_cache, _paths_fault)
    )
    sources = (
        *bmadconfig.CENTRAL_LAYERS_REL,
        bmadconfig.LEGACY_CONFIG_REL,
        bmadconfig.V7_MARKER_REL,
    )
    config_sigs = tuple(_config_source_sig(project / rel) for rel in sources)
    cached_paths = cache.get(project)
    if cached_paths is not None and cached_paths[0] == config_sigs:
        faults.pop(asked, None)
        return cached_paths[1]
    try:
        paths = bmadconfig.load_paths(project, want_tickets=want_tickets)
    except (bmadconfig.BmadConfigError, OSError) as e:
        faults[asked] = _describe(e)
        return None
    faults.pop(asked, None)
    cache[project] = (config_sigs, paths)
    return paths


def sprint_overview(project: Path) -> sprintstatus.SprintStatus | None:
    """Parsed sprint-status.yaml, or None when unavailable (uninitialized
    project, missing file, bad YAML). Stat-gated on both the BMAD config and the
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


def _stories_board(project: Path, state: RunState) -> list[stories.StoryRow] | None:
    return stories_overview(project, state.spec_folder)


@dataclass(frozen=True)
class TicketsBoard:
    """The tickets-mode board for one initiative: ``tickets.py status``'s rows in
    the script's order, or why the tree could not be read. A read that failed
    carries ``fault`` (and no rows), never an empty board that would read as "no
    tickets". ``initiative`` is the one the board was read for (``""`` when there
    was none to read)."""

    rows: tuple[ticketmodel.TicketRow, ...]
    fault: str | None
    initiative: str


# A board read that failed is retried after this long even when no file changed:
# a timeout or a busy store lock clears on its own, so a fault kept until the
# next edit could outlive its cause. An answer is kept until a file changes.
TICKETS_FAULT_RETRY_S = 30.0

# (project, initiative, initiative folder) -> (tree signature, board, monotonic
# time of the read)
_tickets_cache: dict[tuple[Path, str, Path], tuple[object, TicketsBoard, float]] = {}


def _tickets_tree_sig(project: Path, folder: Path) -> object:
    """What a ``tickets.py status`` answer depends on, as one comparable value:
    the ``(path, mtime_ns, size)`` of ``tickets.toml`` and every ``*.md`` in the
    initiative folder and its subfolders (four levels, enough for either tree
    layout: ``tickets/epic-x/tickets/*.md`` in the current one, ``epic-x/*.md`` in
    the flat one), plus the config layers ``active_initiative`` is read
    from, the script itself and the ``config_utils.py`` it execs. Read with
    ``scandir``; a folder that cannot be listed or a file that cannot be stat'ed
    is part of the signature too."""
    files: list[tuple[str, object, object]] = []
    pending: list[tuple[Path, int]] = [(folder, 1)]
    while pending:
        here, depth = pending.pop()
        try:
            with os.scandir(here) as it:
                for entry in it:
                    try:
                        if entry.is_dir():
                            if depth < 4:
                                pending.append((Path(entry.path), depth + 1))
                            continue
                        if entry.name != "tickets.toml" and not entry.name.endswith(".md"):
                            continue
                        st = entry.stat()
                    except OSError as e:
                        files.append((entry.path, "error", e.errno))
                        continue
                    files.append((entry.path, st.st_mtime_ns, st.st_size))
        except OSError as e:
            files.append((str(here), "unlistable", e.errno))
    sources = (
        *bmadconfig.V7_CENTRAL_LAYERS_REL,
        bmadconfig.V7_MARKER_REL,
        bmadconfig.V7_CONFIG_UTILS_REL,
    )
    return (
        tuple(sorted(files, key=lambda f: f[0])),
        tuple(_config_source_sig(project / rel) for rel in sources),
    )


def _board_ticket_tree(project: Path, initiative: str) -> tickettree.TicketTreeLike:
    """The tree the board reads: the project's installed ``tickets.py``, pinned
    to ``initiative`` so a switched ``active_initiative`` reads as a fault, not
    as another initiative's board. A seam: tests swap in a ``FakeTicketTree``."""
    return tickettree.TicketTree(project, pinned_initiative=initiative)


def initiative_board(project: Path, initiative: str, folder: Path) -> TicketsBoard:
    """``initiative``'s board, stat-gated on :func:`_tickets_tree_sig`: the
    script is spawned only when that signature changes (or a cached fault is
    older than :data:`TICKETS_FAULT_RETRY_S`). ``tickets.py`` is project code (it
    imports the project's ``config_utils``) and may run for seconds, so call this
    from a worker thread, never the UI thread."""
    key = (project, initiative, folder)
    sig = _tickets_tree_sig(project, folder)  # before the read: an edit mid-read re-reads
    cached = _tickets_cache.get(key)
    if cached is not None and cached[0] == sig:
        board = cached[1]
        if board.fault is None or time.monotonic() - cached[2] < TICKETS_FAULT_RETRY_S:
            return board
    try:
        view = _board_ticket_tree(project, initiative).status()
    except tickettree.TicketsError as e:
        board = TicketsBoard((), f"tickets.py {e.kind}: {e}", initiative)
    else:
        board = TicketsBoard(view.tickets, None, initiative)
    _tickets_cache[key] = (sig, board, time.monotonic())
    return board


def tickets_overview(project: Path, state: RunState) -> TicketsBoard:
    """The tickets-mode board for a run: the tree of the initiative it pinned
    (``RunState.initiative`` and ``initiative_folder``), read as
    :func:`initiative_board` reads it — off the UI thread. The recorded folder
    moves with a renamed project (:func:`~bmad_loop.runs.rebase_recorded_project_path`),
    so the signature watches the tree the script reads. A run with no pinned
    initiative has no tree to read and says so in ``fault``."""
    if not state.initiative or not state.initiative_folder:
        return TicketsBoard((), "this run pinned no initiative", state.initiative)
    folder = Path(state.initiative_folder)
    if not folder.is_absolute():
        folder = project / folder
    folder = rebase_recorded_project_path(folder, state, project)
    return initiative_board(project, state.initiative, folder)


def project_tickets_overview(project: Path) -> TicketsBoard:
    """The board a new tickets run would start from: the project's live ``[core]
    active_initiative``. A project that is not v7, or names no initiative, says
    so in ``fault``. Resolves as ``bmad-loop run`` does in tickets mode, so a mixed
    v6/v7 install previews its v7 initiative. Spawns like :func:`initiative_board`
    — off the UI thread."""
    paths = _project_paths(project, want_tickets=True)
    if paths is None:
        why = _tickets_paths_fault.get(project, "the project is not initialized")
        return TicketsBoard((), f"BMAD config unreadable: {why}", "")
    if paths.era != "v7" or paths.initiative_folder is None or not paths.active_initiative:
        return TicketsBoard((), "no v7 active initiative ([core] active_initiative)", "")
    return initiative_board(project, paths.active_initiative, paths.initiative_folder)


def _tickets_board(project: Path, state: RunState) -> TicketsBoard:
    return tickets_overview(project, state)


BoardLoader = Callable[[Path, RunState], "list[stories.StoryRow] | TicketsBoard | None"]


BOARD_LOADERS: Mapping[str, BoardLoader | None] = {
    worksource.SOURCE_SPRINT: None,
    worksource.SOURCE_STORIES: _stories_board,
    worksource.SOURCE_TICKETS: _tickets_board,
}
"""The per-run board each work source shows in the dashboard's board slot, keyed
by ``RunState.source``. ``None`` means the source has no per-run board: the slot
shows the project-level sprint tree (:func:`sprint_overview`, painted on rescan).
A source this table does not carry is shown as sprint, as before the registry.
A loader returns stories rows (the stories board) or a :class:`TicketsBoard`
(the tickets board); the dashboard picks the widget by that type.
One row per ``worksource.SOURCES`` (``tests/test_worksource.py`` holds it
exhaustive)."""


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
        # OBSERVATION arm of the ledger-read contract (DW-146): the dashboard
        # writes nothing and already degrades to `items = None` (rendered as
        # unavailable). `UnicodeDecodeError` is a `ValueError`, so undecodable
        # bytes escaped this arm and took the whole TUI refresh down instead.
        try:
            text = ledger_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
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
                            severity=e.severity,
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


@dataclass(frozen=True)
class MissedDecisions:
    """:func:`pending_missed_decisions`' answer. ``fault`` is None for a real
    answer (``items`` may then be empty: nothing is pending); otherwise it names
    why nothing could be read, and ``items`` is empty because nothing was — not
    because nothing is pending (DW-473). ``ledger`` is the deferred-work ledger
    ``items`` were listed from, for :func:`decisions.apply_pre_answer`'s
    ``listed_ledger``."""

    items: list[Any]
    fault: str | None = None
    ledger: Path | None = None


def pending_missed_decisions(project: Path) -> MissedDecisions:
    """Deferred-work decisions earlier sweeps surfaced but no one answered (a
    list of sweep.Decision). Cached on a signature of the ledger, the pre-answer
    store, and the set of run dirs — a new sweep (new run dir) or an answer
    (store/ledger change) invalidates it.

    Never raises. A project whose BMAD config cannot be loaded, a read that
    raises, and a ledger that exists but cannot be read each answer a
    ``fault`` (DW-473) — the last because ``decisions.pending_missed_decisions``
    reads an unreadable ledger as "no open ids" and answers ``[]``, the same
    probe ``cmd_decisions`` makes. So does an incomplete run listing (DW-468):
    the triage caches of a run that cannot be read are missing from the answer.
    A fault is not cached: the next call retries."""
    from .. import decisions  # lazy: pulls sweep; keep this module import-light

    paths = _project_paths(project)
    if paths is None:
        why = _paths_fault.get(project, "BMAD config unavailable")
        return MissedDecisions([], fault=f"BMAD config cannot be loaded ({why})")
    project = paths.project
    run_dirs, listing_fault = list_run_dirs(project)
    if listing_fault is not None:
        return MissedDecisions([], fault=f"run listing incomplete ({listing_fault})")
    sig = (
        paths.deferred_work,
        _stat_sig(paths.deferred_work),
        _stat_sig(decisions.store_path(project, decisions.store_scope(paths))),
        tuple(d.name for d in run_dirs),
    )
    cached = _missed_cache.get(project)
    if cached is not None and cached[0] == sig:
        return cached[1]
    try:
        items = decisions.pending_missed_decisions(project, paths)
    except (bmadconfig.BmadConfigError, OSError) as e:
        return MissedDecisions([], fault=_describe(e))
    _, ledger_fault = deferredwork.read_for_observation(paths.deferred_work)
    if ledger_fault is not None:
        return MissedDecisions([], fault=f"deferred-work ledger unreadable ({ledger_fault})")
    result = MissedDecisions(items, ledger=paths.deferred_work)
    _missed_cache[project] = (sig, result)
    return result
