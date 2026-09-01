"""TUI data layer — pure filesystem observation, no textual involved."""

from __future__ import annotations

import builtins
import importlib
import os
import subprocess
import sys
from pathlib import Path

from conftest import install_bmad_config, refuse_to_resolve, write_sprint

from bmad_loop import bmadconfig, deferredwork, policy
from bmad_loop.journal import Journal, save_state
from bmad_loop.model import RunState
from bmad_loop.runs import RUNS_DIR
from bmad_loop.tui import data


def make_run(root: Path, run_id: str, **state_kwargs) -> Path:
    run_dir = root / RUNS_DIR / run_id
    state = RunState(
        run_id=run_id,
        project=str(root),
        started_at="2026-06-11T10:00:00",
        **state_kwargs,
    )
    save_state(run_dir, state)
    return run_dir


def dead_pid() -> int:
    """Pid guaranteed (modulo astronomically unlikely reuse) to be dead."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


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
                        "question": "q",
                        "context": "",
                        "options": [
                            {"key": "1", "label": "Build", "effect": "build", "intent": "x"},
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


def test_pending_missed_decisions_reads_and_caches(project, monkeypatch):
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    run_dir = make_run(project.project, "20260101-000000-aaaa")
    _write_triage_decision(run_dir)

    pending = data.pending_missed_decisions(project.project)
    assert [d.id for d in pending] == ["DW-1"]
    # cached: same object back while ledger/store/run-set are unchanged
    assert data.pending_missed_decisions(project.project) is pending


def test_pending_missed_decisions_uses_loaded_project_root(project, monkeypatch):
    """The canonical root from ProjectPaths is both the reader and cache key.

    INVERSE ablation: restore the second ``project.resolve()`` in
    ``pending_missed_decisions`` and this test raises the stubbed WinError 64
    instead of returning the cached decision from the already-loaded root.
    """
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    run_dir = make_run(project.project, "20260101-000000-aaaa")
    _write_triage_decision(run_dir)
    paths = bmadconfig.load_paths(project.project)
    original_spelling = project.project / "unresolved-alias" / ".."
    monkeypatch.setattr(data, "_project_paths", lambda _project: paths)
    refuse_to_resolve(monkeypatch, original_spelling)

    pending = data.pending_missed_decisions(original_spelling)

    assert [decision.id for decision in pending] == ["DW-1"]
    assert data.pending_missed_decisions(original_spelling) is pending
    assert paths.project in data._missed_cache
    assert original_spelling not in data._missed_cache


def test_pending_missed_decisions_empty_for_uninitialized(tmp_path):
    assert data.pending_missed_decisions(tmp_path) == []


# ------------------------------------------------------------ no textual dep


def test_data_imports_without_textual(monkeypatch):
    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        assert not name.startswith("textual"), "data.py must not import textual"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    importlib.reload(data)


# ----------------------------------------------------------------- discovery


def test_stopped_run_watcher_status_is_stopped(tmp_path):
    # Companion to test_runs.py's discover_runs half of this case (#650): the
    # watcher runs the same _classify, so a deliberate stop's dead pid must read
    # STOPPED here too, not INTERRUPTED.
    run_dir = make_run(tmp_path, "20260611-100000-aaaa", stopped=True)
    (run_dir / "engine.pid").write_text(str(dead_pid()))
    assert data.RunWatcher(run_dir).status() == data.STOPPED


def test_watcher_stopping_reads_the_control_file(tmp_path):
    from bmad_loop.runs import STOP_REQUEST_FILE

    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    watcher = data.RunWatcher(run_dir)
    assert watcher.stopping() is False
    (run_dir / STOP_REQUEST_FILE).write_text("{}", encoding="utf-8")
    assert watcher.stopping() is True


# --------------------------------------------------------------- RunWatcher


def test_watcher_state_keeps_last_good_parse(tmp_path):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa", current_epic=1)
    watcher = data.RunWatcher(run_dir)
    assert watcher.state().current_epic == 1

    (run_dir / "state.json").write_text("{ mid-write garbage")
    assert watcher.state().current_epic == 1  # last good survives

    state = RunState(
        run_id=run_dir.name,
        project=str(tmp_path),
        started_at="2026-06-11T10:00:00",
        current_epic=2,
    )
    save_state(run_dir, state)
    assert watcher.state().current_epic == 2


def test_watcher_state_none_before_first_write(tmp_path):
    watcher = data.RunWatcher(tmp_path / "nope")
    assert watcher.state() is None
    assert watcher.status() == data.UNKNOWN


def test_watcher_status_interrupted(tmp_path):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "engine.pid").write_text(str(dead_pid()))
    watcher = data.RunWatcher(run_dir)
    assert watcher.status() == data.INTERRUPTED
    assert watcher.liveness() == "dead"


def test_watcher_status_reused_pid_reads_interrupted(tmp_path):
    # A live pid whose recorded identity no longer matches (pid reuse — immediate on
    # Windows) must read as dead/INTERRUPTED, not a false RUNNING. Uses our own pid
    # with a bogus identity token; identity() re-read never matches 0.5.
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "engine.pid").write_text(f"{os.getpid()} 0.5")
    watcher = data.RunWatcher(run_dir)
    assert watcher.liveness() == "dead"
    assert watcher.status() == data.INTERRUPTED


def test_watcher_status_crashed(tmp_path):
    # Companion to test_runs.py::test_classify_crashed (#650): a state.json
    # carrying crashed=True surfaces through the watcher, ahead of liveness.
    run_dir = make_run(tmp_path, "20260611-100000-aaaa", crashed=True)
    (run_dir / "engine.pid").write_text(str(dead_pid()))
    assert data.RunWatcher(run_dir).status() == data.CRASHED


def test_watcher_status_legacy_crash_stays_interrupted(tmp_path):
    # Companion to test_runs.py's discover_runs half (#650): a pre-feature run has
    # no crashed flag, so the watcher reads a dead pid as INTERRUPTED, not CRASHED.
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    import json

    doc = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    doc.pop("crashed", None)
    (run_dir / "state.json").write_text(json.dumps(doc), encoding="utf-8")
    (run_dir / "engine.pid").write_text(str(dead_pid()))
    assert data.RunWatcher(run_dir).status() == data.INTERRUPTED


def test_watcher_attention(tmp_path):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    watcher = data.RunWatcher(run_dir)
    assert watcher.attention() == ""
    (run_dir / "ATTENTION").write_text("[ts] gate: epic boundary\n")
    assert watcher.attention() == "[ts] gate: epic boundary\n"
    with (run_dir / "ATTENTION").open("a") as f:
        f.write("[ts] escalation: help\n")
    assert watcher.attention().count("\n") == 2


# -------------------------------------------------------------- JournalTail


def test_journal_tail_withholds_partial_line(tmp_path):
    journal = Journal(tmp_path)
    tail = data.JournalTail(tmp_path)
    assert tail.read_new() == []  # no file yet

    journal.append("run-start", run_id="x")
    path = tmp_path / "journal.jsonl"
    with path.open("a") as f:
        f.write('{"ts": 2, "kind": "story-start"')  # flush mid-line, no newline
    assert [e["kind"] for e in tail.read_new()] == ["run-start"]
    assert tail.read_new() == []  # partial still withheld

    with path.open("a") as f:
        f.write(', "story": "1-1-a"}\n')
    entries = tail.read_new()
    assert [e["kind"] for e in entries] == ["story-start"]
    assert entries[0]["story"] == "1-1-a"


def test_journal_tail_resets_on_truncation(tmp_path):
    journal = Journal(tmp_path)
    for i in range(3):
        journal.append("session-start", task_id=f"t{i}")
    tail = data.JournalTail(tmp_path)
    assert len(tail.read_new()) == 3

    (tmp_path / "journal.jsonl").write_text('{"ts": 9, "kind": "run-start"}\n')
    assert [e["kind"] for e in tail.read_new()] == ["run-start"]


def test_journal_tail_skips_unparseable_lines(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text('not json\n{"ts": 1, "kind": "run-start"}\n')
    tail = data.JournalTail(tmp_path)
    assert [e["kind"] for e in tail.read_new()] == ["run-start"]


# ------------------------------------------------------------------ LogView


def ink_stream() -> bytes:
    """Two real lines, a spinner repainted in place, then a final replace —
    the shape an ink-style interactive CLI leaves in a pipe-pane capture."""
    out = b"line one\r\nline two\r\n"
    out += "⠋ thinking\r\n".encode()
    for glyph in "⠙⠹⠸":
        out += b"\x1b[1A\x1b[2K" + f"{glyph} thinking\r\n".encode()
    out += b"\x1b[1A\x1b[2Kdone in 3s\r\n"
    return out


def test_log_view_collapses_repaints(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(ink_stream())
    view = data.LogView(path)
    assert view.read_new() is True
    plain = view.render().plain
    assert plain.count("line one") == 1
    assert plain.count("line two") == 1
    assert "done in 3s" in plain
    assert "thinking" not in plain
    assert "\x1b" not in plain


def test_log_view_first_read_seeks_to_tail(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(b"filler\r\n" * 12_000 + b"THE-END\r\n")
    view = data.LogView(path, max_bytes=1024)
    assert view.read_new() is True
    assert view.render().plain.endswith("THE-END")

    with path.open("ab") as f:
        f.write(b"more output\r\n")
    assert view.read_new() is True
    assert view.render().plain.endswith("more output")
    assert view.read_new() is False


def test_log_view_missing_file(tmp_path):
    view = data.LogView(tmp_path / "task.log")
    assert view.read_new() is False
    assert view.render().plain == ""


def test_log_view_truncation_resets_emulator(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(b"hello\r\n")
    view = data.LogView(path)
    assert view.read_new() is True
    assert "hello" in view.render().plain

    path.write_bytes(b"anew\r\n")  # shrank: rewritten log
    assert view.read_new() is True
    plain = view.render().plain
    assert "anew" in plain
    assert "hello" not in plain


def test_log_view_flags_altscreen(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(b"plain line\r\n")
    view = data.LogView(path)
    assert view.read_new() is True
    assert view.altscreen_seen is False

    # a fullscreen TUI switches to the alternate screen mid-stream
    with path.open("ab") as f:
        f.write(b"\x1b[?1049h" + b"fullscreen frame\r\n")
    assert view.read_new() is True
    assert view.altscreen_seen is True


def test_log_view_altscreen_detected_past_tail_seek(tmp_path):
    # The enter marker sits in the prefix the max_bytes tail seek skips; a cold
    # open must still flag it (the user's case: viewing a finished fullscreen run).
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[?1049h" + b"filler\r\n" * 12_000 + b"THE-END\r\n")
    view = data.LogView(path, max_bytes=1024)
    assert view.read_new() is True
    assert view.altscreen_seen is True


def test_log_view_altscreen_prefix_scan_is_capped(tmp_path, monkeypatch):
    # The cold-open prefix scan is bounded so a huge finished log is not read whole.
    # A marker beyond the cap (but inside the tail-skipped prefix) is missed on cold
    # open; one within the cap is still flagged.
    monkeypatch.setattr(data, "_ALTSCREEN_PREFIX_SCAN_CAP", 100)
    path = tmp_path / "task.log"
    # marker at offset 200 (> cap), then enough filler that it stays out of the tail
    path.write_bytes(b"A" * 200 + b"\x1b[?1049h" + b"filler\r\n" * 4000 + b"END\r\n")
    view = data.LogView(path, max_bytes=1024)
    assert view.read_new() is True
    assert view.altscreen_seen is False  # marker sat past the capped scan window

    # raise the cap above the marker offset: the same cold open now flags it
    monkeypatch.setattr(data, "_ALTSCREEN_PREFIX_SCAN_CAP", 1 << 20)
    view2 = data.LogView(path, max_bytes=1024)
    assert view2.read_new() is True
    assert view2.altscreen_seen is True


def test_log_view_truncation_clears_altscreen(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[?1049hframe\r\n")
    view = data.LogView(path)
    assert view.read_new() is True
    assert view.altscreen_seen is True

    path.write_bytes(b"plain again\r\n")  # shrank: rewritten log, no altscreen
    assert view.read_new() is True
    assert view.altscreen_seen is False


def test_log_view_split_escape_across_reads(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(b"hello\r\n\x1b[1")
    view = data.LogView(path)
    assert view.read_new() is True
    with path.open("ab") as f:
        f.write(b"A\x1b[2Kbye\r\n")
    assert view.read_new() is True
    plain = view.render().plain
    assert "bye" in plain
    assert "hello" not in plain
    assert "\x1b" not in plain


def test_log_view_styles(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[31mred\x1b[0m plain \x1b[38;5;196mX\r\n")
    view = data.LogView(path)
    assert view.read_new() is True
    line = view.render()
    styled = {}
    for start, end, style in line.spans:
        if style.color is not None:
            styled[line.plain[start:end]] = style.color
    assert styled["red"].name == "red"
    assert styled["X"].name == "#ff0000"


def test_log_view_strips_private_marker_sgr(tmp_path):
    # XTMODKEYS `CSI > 4 ; 2 m` (modifyOtherKeys, emitted at session start by
    # Claude Code et al.) is not an SGR. pyte 0.8.2 ignores the `>` marker and
    # misreads the `4` as underline-on with no matching off, underlining the whole
    # log; we strip private-marker sequences before pyte sees them.
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[>4;2mhello world\r\n")
    view = data.LogView(path)
    assert view.read_new() is True
    line = view.render()
    assert "hello world" in line.plain
    assert not any(style.underline for _, _, style in line.spans)


def test_log_view_preserves_legitimate_underline(tmp_path):
    # A real, properly-closed underline still renders — the fix removes only the
    # misparsed private-marker sequences, not genuine SGR styling.
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[4mUP\x1b[24m DOWN\r\n")
    view = data.LogView(path)
    assert view.read_new() is True
    line = view.render()
    underlined = "".join(line.plain[s:e] for s, e, st in line.spans if st.underline)
    assert "UP" in underlined
    assert "DOWN" not in underlined


def test_log_view_strips_private_marker_sgr_split_across_reads(tmp_path):
    # The marker sequence straddles two reads; the held-back trailing CSI lets the
    # filter see it whole on the next read instead of leaking past pyte.
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[>4")
    view = data.LogView(path)
    view.read_new()
    with path.open("ab") as f:
        f.write(b";2mhello\r\n")
    assert view.read_new() is True
    line = view.render()
    assert "hello" in line.plain
    assert not any(style.underline for _, _, style in line.spans)


def test_log_view_strips_private_marker_mid_params(tmp_path):
    # gemini's XTMODKEYS reply `CSI > 4 ; ? m` carries the `?` marker *inside* the
    # params. Unstripped, pyte 0.8.2 dispatches it with private=True and
    # select_graphic_rendition rejects the kwarg — the TypeError that killed the
    # whole TUI in #111.
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[>4;?mhello world\r\n")
    view = data.LogView(path)
    assert view.read_new() is True
    line = view.render()
    assert "hello world" in line.plain
    assert not any(style.underline for _, _, style in line.spans)


def test_log_view_survives_gemini_startup_preamble(tmp_path):
    # The exact byte prefix from the #111 traceback: the gemini CLI's terminal
    # capability negotiation burst, including the crashing `CSI > 4 ; ? m`.
    path = tmp_path / "task.log"
    path.write_bytes(
        b"\x1b[8m\x1b[?u\x1b]11;?\x1b\\\x1b[>q\x1b[>4;?m\x1b[c\x1b[2K\r\x1b[0m" b"ready to work\r\n"
    )
    view = data.LogView(path)
    assert view.read_new() is True
    assert "ready to work" in view.render().plain


def test_log_view_strips_vim9_private_sgr(tmp_path):
    # `CSI ? 4 m` (vim 9+, upstream selectel/pyte#202): marker in first position
    # but final `m` — must be stripped, not read as underline or crash pyte.
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[?4mhello\r\n")
    view = data.LogView(path)
    assert view.read_new() is True
    line = view.render()
    assert "hello" in line.plain
    assert not any(style.underline for _, _, style in line.spans)


def test_log_view_strips_private_marker_mid_params_split_across_reads(tmp_path):
    # The #111 sequence straddles two reads; the held-back trailing CSI (whose
    # char class already admits marker bytes anywhere) lets the filter see it
    # whole on the next read.
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[>4;?")
    view = data.LogView(path)
    view.read_new()
    with path.open("ab") as f:
        f.write(b"mhello\r\n")
    assert view.read_new() is True
    line = view.render()
    assert "hello" in line.plain
    assert not any(style.underline for _, _, style in line.spans)


def test_log_view_survives_unfilterable_private_csi(tmp_path):
    # Belt-and-braces: a private-marked CSI with a non-`m` final passes the strip
    # filter deliberately (only marker-SGR is stripped) and crashes raw pyte 0.8.2
    # (`cursor_position() got an unexpected keyword argument 'private'`). The
    # tolerant stream drops the sequence instead of killing the poll worker, and
    # the emulator keeps rendering everything after it.
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[?1;1Hhello\r\n")
    view = data.LogView(path)
    assert view.read_new() is True
    assert "hello" in view.render().plain

    with path.open("ab") as f:
        f.write(b"\x1b[31mstill alive\x1b[0m\r\n")
    assert view.read_new() is True
    line = view.render()
    assert "still alive" in line.plain
    styled = {line.plain[s:e] for s, e, st in line.spans if st.color is not None}
    assert "still alive" in styled


def test_log_view_history_beyond_screen(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(b"".join(f"row {i:03d}\r\n".encode() for i in range(1, 81)))
    view = data.LogView(path)
    assert view.read_new() is True
    plain = view.render().plain
    assert "row 001" in plain  # scrolled into history, still rendered
    assert "row 080" in plain


# ------------------------------------------------------------------ LogIndex


def numbered_log(path: Path, count: int = 40) -> list[int]:
    """`line NN\\r\\n` rows; returns each line's starting byte offset."""
    offsets = []
    buf = b""
    for i in range(count):
        offsets.append(len(buf))
        buf += f"line {i:02d}\r\n".encode()
    path.write_bytes(buf)
    return offsets


def test_log_index_maps_offsets(tmp_path):
    path = tmp_path / "task.log"
    offs = numbered_log(path)
    view = data.LogView(path, checkpoint_bytes=1)
    assert view.read_new() is True
    plain = view.render().plain.splitlines()
    idx = view.index()
    for k in (0, 7, 23, 39):
        # mid-line offset: the cursor is exactly on row k at that byte
        assert plain[idx.line_for_offset(offs[k] + 3)] == f"line {k:02d}"


def test_log_index_interpolates_between_coarse_checkpoints(tmp_path):
    """A whole small file fits in one checkpoint slice; mid-file offsets must
    interpolate by byte fraction, not collapse to the slice's start line."""
    path = tmp_path / "task.log"
    offs = numbered_log(path, count=100)  # uniform 9-byte lines
    view = data.LogView(path)  # default checkpoint_bytes far above file size
    view.read_new()
    plain = view.render().plain.splitlines()
    line = view.index().line_for_offset(offs[50])
    assert plain[line] == "line 50"


def test_log_index_clamps_to_render_window(tmp_path):
    path = tmp_path / "task.log"
    numbered_log(path)
    view = data.LogView(path, checkpoint_bytes=16)
    view.read_new()
    last = len(view.render().plain.splitlines()) - 1
    idx = view.index()
    assert idx.line_for_offset(0) == 0
    assert idx.line_for_offset(10**9) == last  # beyond EOF


def test_log_index_none_when_nothing_rendered(tmp_path):
    view = data.LogView(tmp_path / "task.log")
    view.read_new()
    view.render()
    assert view.index().line_for_offset(0) is None


def test_log_index_clamps_before_tail_seek(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(b"filler\r\n" * 12_000 + b"THE-END\r\n")
    view = data.LogView(path, max_bytes=1024)
    view.read_new()
    view.render()
    assert view.index().line_for_offset(0) == 0  # long before the seek point


def test_log_index_survives_history_eviction(tmp_path):
    path = tmp_path / "task.log"
    offs = numbered_log(path, count=40)
    view = data.LogView(path, checkpoint_bytes=1, lines=5, history=10)
    view.read_new()
    plain = view.render().plain.splitlines()
    idx = view.index()
    assert len(plain) == 10  # render capped to the newest history rows
    assert idx.line_for_offset(offs[0] + 3) == 0  # evicted line clamps to top
    assert plain[idx.line_for_offset(offs[36] + 3)] == "line 36"
    assert idx.line_for_offset(offs[39] + 3) == len(plain) - 1


def test_log_index_truncation_resets(tmp_path):
    path = tmp_path / "task.log"
    numbered_log(path)
    view = data.LogView(path, checkpoint_bytes=1)
    view.read_new()
    view.render()

    path.write_bytes(b"fresh 0\r\nfresh 1\r\n")  # shrank: rewritten log
    assert view.read_new() is True
    plain = view.render().plain.splitlines()
    idx = view.index()
    assert plain[idx.line_for_offset(9 + 3)] == "fresh 1"  # mid second line
    assert idx.line_for_offset(10**6) == len(plain) - 1


def test_log_index_incremental_reads_match_single_read(tmp_path):
    path = tmp_path / "task.log"
    offs = numbered_log(path, count=20)
    whole = path.read_bytes()
    path.write_bytes(whole[: offs[10]])
    view = data.LogView(path, checkpoint_bytes=1)
    view.read_new()
    with path.open("ab") as f:
        f.write(whole[offs[10] :])
    assert view.read_new() is True
    plain = view.render().plain.splitlines()
    idx = view.index()
    for k in (0, 9, 10, 19):
        assert plain[idx.line_for_offset(offs[k] + 3)] == f"line {k:02d}"


# ------------------------------------------------------------ active task id


def test_active_task_id_from_journal(tmp_path):
    entries = [
        {"kind": "session-start", "task_id": "t1"},
        {"kind": "session-end", "task_id": "t1"},
        {"kind": "session-start", "task_id": "t2"},
    ]
    assert data.active_task_id(tmp_path, entries) == "t2"
    entries.append({"kind": "session-end", "task_id": "t2"})
    assert data.active_task_id(tmp_path, entries) is None  # no logs fallback either


def test_active_task_id_clears_on_aborted_session_end(tmp_path):
    # No logs/ here on purpose: this pins the journal-scan contract (an explicit
    # aborted end clears the active id). With logs present the newest-log
    # fallback would still tail the ended session — deliberate, so the operator
    # keeps the aborted log on screen; covered by the fallback test below.
    entries = [
        {"kind": "session-start", "task_id": "t1"},
        {"kind": "session-end", "task_id": "t1", "status": "aborted", "error": "RuntimeError"},
    ]
    assert data.active_task_id(tmp_path, entries) is None


def test_active_task_id_falls_back_to_newest_log(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "t-old.log").write_text("old")
    (logs / "t-new.log").write_text("new")
    os.utime(logs / "t-old.log", ns=(1, 1))
    assert data.active_task_id(tmp_path, []) == "t-new"


def test_active_task_id_matches_open_session_start(tmp_path):
    # Regression: extracting _open_session_start must leave active_task_id
    # byte-identical, including the logs/ fallback tail.
    entries = [
        {"kind": "session-start", "task_id": "t1"},
        {"kind": "session-end", "task_id": "t1"},
        {"kind": "session-start", "task_id": "t2"},
    ]
    assert data.active_task_id(tmp_path, entries) == "t2"
    assert data._open_session_start(entries) is entries[-1]  # the open entry itself

    closed = entries + [{"kind": "session-end", "task_id": "t2"}]
    assert data.active_task_id(tmp_path, closed) is None
    assert data._open_session_start(closed) is None

    # nothing open in the journal -> newest-log fallback still fires
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "t-old.log").write_text("old")
    (logs / "t-new.log").write_text("new")
    os.utime(logs / "t-old.log", ns=(1, 1))
    assert data.active_task_id(tmp_path, closed) == "t-new"


def test_active_task_id_ignores_verifier_streams(tmp_path):
    """The newest-log fallback sees pane logs only: verifier streams are not tasks.

    Regression. Verifier stdout/stderr used to be retained in ``logs/``, whose
    every other inhabitant is an adapter pane capture named after a session task
    id. That collides in the COMMON case, not a corner: session-end is journalled
    when the session ends, before its result reaches verification, so nothing is
    open exactly when the verifier files are the newest in the directory. The
    fallback then returned a stream's stem as the live task and the dashboard
    reopened it as ``logs/{stem}.log`` — a path that resolves, so the log pane
    rendered verifier stderr in place of the agent session log.

    The streams are written through the real writer, not hand-placed: pointing
    ``Journal.write_verify_stream`` back at ``logs/`` must redden this test.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "1-1-a-dev-1.log").write_text("pane capture")
    os.utime(logs / "1-1-a-dev-1.log", ns=(1, 1))  # older than anything written below

    journal = Journal(tmp_path)
    journal.write_verify_stream("verify-1-1-a-dev-1-1-0.stdout.log", "out")
    journal.write_verify_stream("verify-1-1-a-dev-1-1-0.stderr.log", "err")

    # a dev session that has ended -> no open session -> the fallback fires
    ended = [
        {"kind": "session-start", "task_id": "1-1-a-dev-1"},
        {"kind": "session-end", "task_id": "1-1-a-dev-1"},
    ]
    assert data.active_task_id(tmp_path, ended) == "1-1-a-dev-1"
    assert data.active_task_id(tmp_path, []) == "1-1-a-dev-1"


# ------------------------------------------------------------- active agent


def test_active_agent_from_stamped_session_start():
    entries = [
        {
            "kind": "session-start",
            "task_id": "1-1-alpha-dev-3",
            "role": "dev",
            "adapter": "claude",
            "model": "opus",
            "story_key": "1-1-alpha",
        },
    ]
    assert data.active_agent(entries, None) == data.ActiveAgent(
        task_id="1-1-alpha-dev-3",
        story_key="1-1-alpha",
        role="dev",
        name="claude",
        model="opus",
    )


def test_active_agent_none_after_matching_session_end():
    entries = [
        {
            "kind": "session-start",
            "task_id": "1-1-alpha-dev-3",
            "role": "dev",
            "adapter": "claude",
            "model": "opus",
            "story_key": "1-1-alpha",
        },
        {"kind": "session-end", "task_id": "1-1-alpha-dev-3"},
    ]
    assert data.active_agent(entries, None) is None


def test_active_agent_falls_back_to_policy_snapshot():
    # An entry recorded before adapter stamping (#153 phase 1) has no "adapter"
    # and no "story_key": identity is rebuilt from the run's policy snapshot,
    # resolved for the entry's role, and the story key is peeled off the
    # task_id. A review-stage model override must win over the base model.
    snapshot = {"adapter": {"name": "claude", "model": "opus", "review": {"model": "haiku"}}}
    entries = [{"kind": "session-start", "task_id": "2-3-beta-review-1", "role": "review"}]
    agent = data.active_agent(entries, snapshot)
    resolved = policy.adapter_policy_from_snapshot(snapshot).resolved("review")
    assert agent is not None
    assert (agent.name, agent.model) == (resolved.name, resolved.model) == ("claude", "haiku")
    assert agent.story_key == "2-3-beta"  # peeled from "-review-1"
    assert agent.role == "review"


def test_active_agent_labeled_session_peels_story_key():
    # A labeled plugin session's task_id carries the label, not the role; the
    # recorded role won't match, so the story key peels via best-effort rsplit.
    entries = [{"kind": "session-start", "task_id": "4-2-gamma-tea-7", "role": "dev"}]
    agent = data.active_agent(entries, {"adapter": {"name": "codex", "model": "gpt-5"}})
    assert agent is not None
    assert agent.story_key == "4-2-gamma"  # "-tea-7" stripped despite role != "tea"
    assert (agent.name, agent.model) == ("codex", "gpt-5")


def test_active_agent_none_without_resolvable_snapshot():
    # Unstamped entry + no trustworthy snapshot: an all-"claude" reconstruction
    # would mislabel a run that predates stamping, so the agent is unknown.
    unstamped = [{"kind": "session-start", "task_id": "2-3-beta-review-1", "role": "review"}]
    assert data.active_agent(unstamped, None) is None
    assert data.active_agent(unstamped, {}) is None
    assert data.active_agent(unstamped, {"adapter": {}}) is None  # table without a name
    assert data.active_agent([], None) is None  # no open session at all


# ---------------------------------------------------------- pending decision


def test_pending_decision_last_entry_only():
    assert data.pending_decision([]) is None
    entries = [
        {"kind": "sweep-start"},
        {"kind": "decision-pending", "dw_id": "DW-3", "question": "drop the cache?"},
    ]
    assert data.pending_decision(entries) == ("DW-3", "drop the cache?")
    # any later entry means the blocking prompt was answered
    entries.append({"kind": "decision-answered", "dw_id": "DW-3", "key": "a"})
    assert data.pending_decision(entries) is None


def test_pending_decision_missing_fields():
    assert data.pending_decision([{"kind": "decision-pending"}]) == ("?", "")


# ------------------------------------------------------------ sprint overview


def test_project_paths_degrades_and_recovers_from_root_resolve_refusal(project, monkeypatch):
    """A dead provider yields unavailable readers without poisoning recovery.

    INVERSE ablation: restore bare ``project.resolve()`` in ``_project_paths``
    and this test raises the stubbed WinError 64 on its first observation rather
    than returning empty panes and recovering after the provider is healthy.
    """
    install_bmad_config(project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    root = project.project

    with monkeypatch.context() as refusal:
        refuse_to_resolve(refusal, root)
        assert data._project_paths(root) is None
        assert data.sprint_overview(root) is None
        assert data.deferred_entries(root) is None
        assert data.pending_missed_decisions(root) == []
        assert root not in data._paths_cache

    paths = data._project_paths(root)
    assert paths is not None
    assert paths.project == root
    assert data._paths_cache[root][1] is paths
    assert data.sprint_overview(root) is not None


def test_project_paths_uses_one_canonical_cache_key(project):
    """Healthy aliases share one ProjectPaths snapshot under the canonical root.

    INVERSE ablation: key ``_paths_cache`` with the pre-canonical spelling while
    loading from the stable root and this test finds the ``..`` spelling as a
    second cache key instead of reusing the canonical entry.
    """
    install_bmad_config(project)
    root = project.project.resolve()
    alternate_spelling = root / ".." / root.name

    paths = data._project_paths(alternate_spelling)

    assert paths is not None
    assert paths.project == root
    assert data._project_paths(root) is paths
    assert root in data._paths_cache
    assert alternate_spelling not in data._paths_cache


def test_sprint_overview(project):
    install_bmad_config(project)
    write_sprint(
        project,
        {
            "epic-1": "in-progress",
            "1-1-a": "ready-for-dev",
            "1-2-b": "done",
            "epic-1-retrospective": "optional",
            "epic-2": "backlog",
            "2-1-c": "backlog",
        },
    )
    ss = data.sprint_overview(project.project)
    assert ss.epics == {1: "in-progress", 2: "backlog"}
    assert [(s.key, s.status) for s in ss.stories] == [
        ("1-1-a", "ready-for-dev"),
        ("1-2-b", "done"),
        ("2-1-c", "backlog"),
    ]
    assert ss.retros == {1: "optional"}

    # cached result (same object) until the file changes, then re-parsed
    assert data.sprint_overview(project.project) is ss
    write_sprint(project, {"1-1-a": "done"})
    assert [s.status for s in data.sprint_overview(project.project).stories] == ["done"]


def test_sprint_overview_unavailable(tmp_path, project):
    assert data.sprint_overview(tmp_path) is None  # no _bmad config at all
    install_bmad_config(project)  # config but no sprint file
    assert data.sprint_overview(project.project) is None

    # LLM-maintained file: malformed content must come back None, not raise
    project.sprint_status.write_text("{ not: valid: yaml: [")
    assert data.sprint_overview(project.project) is None
    project.sprint_status.write_text("- just\n- a\n- list\n")
    assert data.sprint_overview(project.project) is None


# ------------------------------------------------- stories mode: pause + board


def _write_stories(folder: Path, entries: list[dict]) -> None:
    import yaml

    (folder / "stories").mkdir(parents=True, exist_ok=True)
    (folder / "stories.yaml").write_text(yaml.safe_dump(entries, sort_keys=False))


def test_stories_overview_reads_board(tmp_path):
    folder = tmp_path / "epic-1"
    _write_stories(
        folder,
        [
            {"id": "1", "title": "First", "description": "d", "spec_checkpoint": True},
            {"id": "2", "title": "Second", "description": "d", "done_checkpoint": True},
        ],
    )
    (folder / "stories" / "1-slug.md").write_text("---\nstatus: done\n---\n", encoding="utf-8")
    rows = data.stories_overview(tmp_path, "epic-1")
    assert rows is not None
    assert [(r.id, r.label) for r in rows] == [("1", "done"), ("2", "pending")]
    assert rows[0].spec_checkpoint and rows[1].done_checkpoint


def test_stories_overview_none_when_unavailable(tmp_path):
    assert data.stories_overview(tmp_path, "") is None  # no folder pinned
    assert data.stories_overview(tmp_path, "missing") is None  # no stories.yaml
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "stories.yaml").write_text("not: a list\n", encoding="utf-8")
    assert data.stories_overview(tmp_path, "bad") is None  # invalid manifest, no raise


# ------------------------------------------------------------- deferred work


def test_deferred_entries(project):
    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "### DW-1: High severity item\n\n"
        "origin: test, 2026-06-01\nlocation: src.txt:1\n"
        "severity: high\nreason: test.\nstatus: open\n\n"
        "### DW-2: Critical via priority alias\n\n"
        "origin: test, 2026-06-01\nlocation: src.txt:2\n"
        "Priority: CRITICAL\nreason: test.\nstatus: open\n\n"
        "### DW-3: No severity at all\n\n"
        "origin: test, 2026-06-01\nlocation: src.txt:3\n"
        "reason: test.\nstatus: open\n\n"
        "### DW-4: Junk severity, already done\n\n"
        "origin: test, 2026-06-01\nlocation: src.txt:4\n"
        "severity: banana\nreason: test.\nstatus: done 2026-06-10\n\n"
        "### DW-5: No status line\n\n"
        "origin: test, 2026-06-01\nlocation: src.txt:5\nreason: test.\n",
        encoding="utf-8",
    )
    items = data.deferred_entries(project.project)
    assert [(i.id, i.severity, i.done) for i in items] == [
        ("DW-1", "high", False),
        ("DW-2", "critical", False),
        ("DW-3", None, False),
        ("DW-4", None, True),
        ("DW-5", None, False),
    ]
    assert items[0].title == "High severity item"
    assert "origin: test" in items[0].body

    # cached result (same object) until the file changes, then re-parsed
    assert data.deferred_entries(project.project) is items
    project.deferred_work.write_text("# Deferred Work\n\nfreeform, no entries\n")
    assert data.deferred_entries(project.project) == []


def test_deferred_entries_unavailable(tmp_path, project):
    assert data.deferred_entries(tmp_path) is None  # no _bmad config at all
    install_bmad_config(project)  # config but no ledger file
    assert data.deferred_entries(project.project) is None


def test_severity_extraction():
    cases = {
        "severity: high\n": "high",
        "Severity: HIGH\n": "high",
        "priority: blocker\n": "critical",
        "severity: med\n": "medium",
        "severity:low\n": "low",
        "severity: banana\n": None,
        "no field here\n": None,
        "the word severity: high inline does not count\n": None,
    }
    for body, expected in cases.items():
        assert deferredwork.field_severity(f"### DW-9: t\n\n{body}status: open\n") == expected, body


def test_deferred_entries_legacy_ledger(project):
    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "## Deferred from: code review of story 1.2 (2026-04-06)\n\n"
        "- ~~**Old fixed thing** — was broken, then repaired~~ → fixed in 1.3\n"
        "- W9 — open item with a bracket severity. [MAJOR]\n"
        "- **Open bold-titled thing here** — details that run on and on\n",
        encoding="utf-8",
    )
    items = data.deferred_entries(project.project)
    assert [(i.id, i.done, i.severity, i.legacy) for i in items] == [
        ("L1", True, None, True),
        ("W9", False, "high", True),
        ("L3", False, None, True),
    ]
    assert items[0].status == "done (legacy)"
    assert items[1].status == "open (legacy)"
    assert items[2].title == "Open bold-titled thing here"
    assert all(i.option_key and i.option_key.startswith("legacy:") for i in items)
    # option keys never collide with DW ids and stay stable across refreshes
    assert data.deferred_entries(project.project) is items


def test_deferred_entries_mixed_ledger_in_file_order(project):
    install_bmad_config(project)
    project.deferred_work.write_text(
        "# Deferred Work\n\n"
        "## Deferred from: epic 1 review (2026-04-06)\n\n"
        "- legacy item first in the file\n\n"
        "### DW-1: Canonical entry\n\n"
        "origin: test, 2026-06-01\nseverity: high\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    items = data.deferred_entries(project.project)
    assert [(i.id, i.legacy) for i in items] == [("L1", True), ("DW-1", False)]
    assert items[1].option_key is None  # canonical rows key on the DW id
    assert items[1].severity == "high"


def test_run_watcher_state_refreshes_on_same_size_rewrite(tmp_path):
    # A same-content atomic rewrite keeps size and (forced) mtime identical but
    # lands a fresh inode. The watcher must re-parse — detected here by object
    # identity: a cache hit would return the very same RunState instance.
    run_dir = make_run(tmp_path, "r1")
    watcher = data.RunWatcher(run_dir)
    first = watcher.state()
    assert first is not None

    state_file = run_dir / "state.json"
    pinned = state_file.stat()
    save_state(
        run_dir, RunState(run_id="r1", project=str(tmp_path), started_at="2026-06-11T10:00:00")
    )
    os.utime(state_file, ns=(pinned.st_atime_ns, pinned.st_mtime_ns))  # pin mtime back
    after = state_file.stat()
    assert after.st_size == pinned.st_size and after.st_mtime_ns == pinned.st_mtime_ns

    assert watcher.state() is not first  # re-parsed because the inode changed


def test_rich_color_maps_pyte_names_to_valid_rich_colors():
    # pyte emits aixterm bright names without an underscore (e.g. "brightbrown"
    # for SGR 93). _rich_color remaps pyte's "brown"/"brightbrown" and then
    # applies the "bright" -> "bright_" transform; the remap target must stay in
    # pyte's underscore-free namespace or the transform doubles the underscore
    # into an invalid "bright__yellow" and every log render raises ColorParseError.
    from rich.color import Color

    cases = {
        "default": None,
        "brown": "yellow",
        "brightbrown": "bright_yellow",  # regression: was "bright__yellow"
        "bfightmagenta": "bright_magenta",  # pyte 0.8.2 BG_AIXTERM[105] typo
        "red": "red",
        "brightred": "bright_red",
        "brightyellow": "bright_yellow",
        "ff00aa": "#ff00aa",
    }
    for pyte_name, expected in cases.items():
        got = data._rich_color(pyte_name)
        assert got == expected, f"{pyte_name!r} -> {got!r}, expected {expected!r}"
        if got is not None:
            Color.parse(got)  # must be a color rich accepts, else the TUI crashes

    # Exhaustive: every name the installed pyte can emit must map to something
    # rich parses — a pyte bump that adds/renames table entries fails here, not
    # in the dashboard's poll worker.
    import pyte.graphics as graphics

    every_pyte_name = (
        set(graphics.FG.values())
        | set(graphics.BG.values())
        | set(graphics.FG_AIXTERM.values())
        | set(graphics.BG_AIXTERM.values())
    )
    for pyte_name in sorted(every_pyte_name):
        got = data._rich_color(pyte_name)
        if got is not None:
            Color.parse(got)


def test_char_style_degrades_unparseable_color_instead_of_raising():
    # Belt to the sweep test's suspenders: if a color name rich can't parse
    # ever slips through _rich_color, the run renders uncolored instead of
    # killing the poll worker (and, via exit_on_error, the whole app).
    key = ("no_such_color", "default", True, False, True, False, False)
    style = data._char_style(key)
    assert style.color is None and style.bgcolor is None
    assert style.bold and style.underline and not style.italic
    assert data._char_style(key) is style  # fallback is cached like any other


def test_story_key_from_task_id_grammar_including_the_generation_suffix():
    """The id grammar this fallback parses, pinned in both directions.

    `_session_task_id` composes `safe_segment(f"{story_key}-{part}-{seq}{gen}")`, and
    #705 added `gen` — a `-g<N>` suffix emitted only above generation zero. That
    changed the grammar this parser documents. Only a final numeric generation
    component is peeled; generation-like malformed or nonterminal components retain
    the existing best-effort fallback behavior.
    """
    # the ordinary unsuffixed shape: the recorded role is peeled with its seq
    assert data._story_key_from_task_id("1-1-a-dev-1", "dev") == "1-1-a"
    assert data._story_key_from_task_id("1-1-a-review-12", "review") == "1-1-a"
    # a labeled plugin session: role does not match, so one more `-` group goes
    assert data._story_key_from_task_id("1-1-a-somelabel-1", "dev") == "1-1-a"
    # not the expected shape at all — returned verbatim
    assert data._story_key_from_task_id("nonsense", "dev") == "nonsense"

    # generation-suffixed (#705): one terminal numeric generation is peeled
    assert data._story_key_from_task_id("1-1-a-dev-1-g1", "dev") == "1-1-a"
    assert data._story_key_from_task_id("1-1-a-dev-1-g12", "dev") == "1-1-a"
    # malformed and nonterminal generation-like components are not suffixes
    assert data._story_key_from_task_id("1-1-a-dev-1-g", "dev") == "1-1-a-dev-1-g"
    assert data._story_key_from_task_id("1-1-a-dev-1-gx", "dev") == "1-1-a-dev-1-gx"
    assert data._story_key_from_task_id("1-1-a-dev-1-g0", "dev") == "1-1-a-dev-1-g0"
    assert data._story_key_from_task_id("1-1-a-dev-1-g01", "dev") == "1-1-a-dev-1-g01"
    assert data._story_key_from_task_id("1-1-a-dev-1-g١", "dev") == "1-1-a-dev-1-g١"
    assert data._story_key_from_task_id("1-1-a-dev-1-g1-extra", "dev") == "1-1-a-dev-1-g1-extra"
