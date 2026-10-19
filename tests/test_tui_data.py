"""TUI data layer — pure filesystem observation, no textual involved."""

from __future__ import annotations

import builtins
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import (
    FakeTicketTree,
    install_bmad_central_config,
    install_bmad_config,
    install_bmad_v7,
    refuse_to_resolve,
    ticket_row,
    write_sprint,
)

from bmad_loop import bmadconfig, deferredwork, platform_util, policy
from bmad_loop.journal import UNREADABLE_LINE_KIND, Journal, save_state
from bmad_loop.model import RunState
from bmad_loop.runs import RUNS_DIR
from bmad_loop.tickettree import TicketsTimeout
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


_DEAD_CHILDREN: list[subprocess.Popen[bytes]] = []


def dead_pid() -> int:
    """Return an exited child's PID, retaining its handle to prevent Windows reuse."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    _DEAD_CHILDREN.append(proc)
    deadline = time.monotonic() + 10.0
    while platform_util.pid_alive(proc.pid):
        if time.monotonic() > deadline:
            raise RuntimeError(f"exited child {proc.pid} still reads alive after 10s")
        time.sleep(0.01)
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
    assert pending.fault is None
    assert [d.id for d in pending.items] == ["DW-1"]
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

    assert [decision.id for decision in pending.items] == ["DW-1"]
    assert data.pending_missed_decisions(original_spelling) is pending
    assert paths.project in data._missed_cache
    assert original_spelling not in data._missed_cache


def test_pending_missed_decisions_survives_an_undecodable_triage(project):
    """DW-145 at the one surface where the fault escaped UNCAUGHT. This reader
    catches `(BmadConfigError, OSError)`, and `UnicodeDecodeError` is a
    `ValueError`: one run's cached triage holding non-UTF-8 bytes raised straight
    out of `decisions.pending_missed_decisions`, past this handler, into the
    dashboard's render. The good run's DW-1 still lists, so the widened except
    tuple degrades per FILE rather than blanking the panel.
    Ablation: revert that tuple to `(json.JSONDecodeError, OSError)` and this
    reddens with `UnicodeDecodeError` rather than returning ["DW-1"]."""
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _write_triage_decision(make_run(project.project, "20260101-000000-aaaa"))
    bad = make_run(project.project, "20260102-000000-bbbb")
    (bad / "triage.json").write_bytes(b'{"workflow": "deferred-sweep-triage", "x": "\xff"}')

    assert [d.id for d in data.pending_missed_decisions(project.project).items] == ["DW-1"]


def test_pending_missed_decisions_survives_a_nested_null_triage(project):
    """DW-155/DW-158 at the same uncaught surface, one fault class over. A cached
    triage can decode and parse cleanly and still hold a `null` where a list
    member belongs; `validate_triage` called `.get` on it unscreened, so an
    `AttributeError` -- not an `OSError`, so this reader's
    `(BmadConfigError, OSError)` catch does not see it either -- escaped
    `decisions.pending_missed_decisions` and reached the dashboard's render, the
    same path DW-145's `UnicodeDecodeError` took. The validator is total over
    shapes now, so the bad cache is refused and skipped and the good run's DW-1
    still lists: degradation is per FILE, not a blanked panel.
    Ablation: drop the `_plan_mapping` call in `validate_triage`'s `bundles` loop
    and this reddens with `AttributeError` rather than returning ["DW-1"]."""
    import json

    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _write_triage_decision(make_run(project.project, "20260101-000000-aaaa"))
    bad = make_run(project.project, "20260102-000000-bbbb")
    (bad / "triage.json").write_text(
        json.dumps(
            {
                "workflow": "deferred-sweep-triage",
                "open_ids": [],
                "already_resolved": [],
                "bundles": [None],
                "blocked": [],
                "skip": [],
                "decisions": [],
                "escalations": [],
            }
        ),
        encoding="utf-8",
    )

    assert [d.id for d in data.pending_missed_decisions(project.project).items] == ["DW-1"]


def test_pending_missed_decisions_faults_for_uninitialized(tmp_path):
    """DW-473: a project whose BMAD config cannot be loaded has no answer to give,
    so the reader says so instead of answering "none pending".

    Ablation: return `MissedDecisions([])` on the `_project_paths is None` arm and
    this reddens on the fault assertion."""
    missed = data.pending_missed_decisions(tmp_path)
    assert missed.items == []
    assert missed.fault is not None and "BMAD config not found" in missed.fault


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


def test_sweep_outcomes_reads_both_fields_off_state_json(tmp_path):
    """DW-366: the TUI reads the auto-sweep ledger off the same parsed state.json
    `status`/`diagnose` use, in file order, refusals kept apart from deliveries.

    Ablation: drop the `not in state.sweeps_refused` filter — the `triggered`
    assert fails on epic-2, the latched-then-failed trigger. Return
    `SweepOutcomes()` instead and both populated-run asserts fail."""
    from bmad_loop.model import SWEEP_REFUSED_DIRTY, SWEEP_REFUSED_FAILED

    # epic-2 is the engine's real `failed` shape: latched into sweeps_triggered
    # when the child started, then recorded refused when it failed.
    run_dir = make_run(
        tmp_path,
        "20260611-100000-aaaa",
        sweeps_triggered=["epic-1", "epic-2"],
        sweeps_refused={"epic-2": SWEEP_REFUSED_FAILED, "run-end": SWEEP_REFUSED_DIRTY},
    )
    outcomes = data.sweep_outcomes(data.RunWatcher(run_dir).state())
    assert outcomes.triggered == ("epic-1",)  # a failed child was not delivered
    assert outcomes.refused == (("epic-2", "failed"), ("run-end", "dirty"))

    # A state.json written before #501 carries neither key: empty, not a crash.
    legacy = make_run(tmp_path, "20260611-100000-bbbb")
    raw = json.loads((legacy / "state.json").read_text(encoding="utf-8"))
    del raw["sweeps_triggered"], raw["sweeps_refused"]
    (legacy / "state.json").write_text(json.dumps(raw), encoding="utf-8")
    assert data.sweep_outcomes(data.RunWatcher(legacy).state()) == data.SweepOutcomes()


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


def test_journal_tail_reports_unparseable_lines_as_a_marker(tmp_path):
    """An unreadable line is REPORTED in the live pane, not skipped: the shared
    `journal.unreadable_line_entry` takes its stream position, so the operator sees
    that a record was lost rather than a gap they cannot detect. (Inverted from
    `test_journal_tail_skips_unparseable_lines` by DW-97.)

    Ablation: restore `except json.JSONDecodeError: continue` in `read_new` and this
    reddens with the marker absent."""
    path = tmp_path / "journal.jsonl"
    path.write_text('not json\n{"ts": 1, "kind": "run-start"}\n')
    tail = data.JournalTail(tmp_path)
    assert tail.read_new() == [
        {"kind": UNREADABLE_LINE_KIND, "bytes": len("not json")},
        {"ts": 1, "kind": "run-start"},
    ]


def test_journal_tail_marker_matches_the_journal_entries_marker(tmp_path):
    """Both readers mint the SAME shape from the same helper — the whole reason the
    minter lives in `journal.py` rather than twice.

    The torn line carries a MULTI-BYTE character, so `bytes` can distinguish the raw
    line (15 bytes) from the decoded string this reader parses (14 characters). With
    an ASCII-only fixture the two are equal and `len(raw)` vs `len(line)` is
    untestable — and they must not diverge, or the two readers would report different
    counts for one line.

    Ablation: count `len(line)` (the decoded string) in `read_new` instead of
    `len(raw)` and this reddens, 14 != 15."""
    path = tmp_path / "journal.jsonl"
    torn = '{"kind": "café'  # 14 characters, 15 UTF-8 bytes
    path.write_text(f'{torn}\n{{"ts": 1, "kind": "run-start"}}\n', encoding="utf-8")
    assert len(torn) == 14 and len(torn.encode("utf-8")) == 15  # the two spellings differ

    tail_entries = data.JournalTail(tmp_path).read_new()
    assert tail_entries == Journal(tmp_path).entries()
    assert tail_entries[0] == {"kind": UNREADABLE_LINE_KIND, "bytes": 15}


def test_journal_tail_withholds_a_fragment_until_its_newline_lands(tmp_path):
    """The byte offset only advances past complete lines, so a partially flushed
    record is not read as a truncated entry — and `Journal.append`'s heal is what
    guarantees that newline eventually arrives on the fragment's OWN line."""
    path = tmp_path / "journal.jsonl"
    path.write_text('{"ts": 1, "kind": "run-start"}\n{"ts": 2, "kind": "unit-merge-star')
    tail = data.JournalTail(tmp_path)
    assert [e["kind"] for e in tail.read_new()] == ["run-start"]
    assert tail.read_new() == []  # nothing new; the fragment is still withheld


def test_journal_tail_sees_both_records_after_a_healed_append(tmp_path):
    """The two-record regression at the TUI's reader: one partial flush costs one
    record, and the SUCCESSOR of the healing append is intact.

    Ablation: drop the `_tail_is_terminated` prepend in `Journal.append` and this
    reddens — `unit-merged` is swallowed with the fragment."""
    path = tmp_path / "journal.jsonl"
    path.write_text('{"ts": 2, "kind": "unit-merge-star')
    tail = data.JournalTail(tmp_path)
    assert tail.read_new() == []

    journal = Journal(tmp_path)
    journal.append("unit-merged", unit="u1")
    journal.append("run-complete")
    assert [e["kind"] for e in tail.read_new()] == [
        UNREADABLE_LINE_KIND,
        "unit-merged",
        "run-complete",
    ]


def test_journal_tail_marker_at_the_tail_clears_a_pending_decision(tmp_path):
    """`data.pending_decision` (and `launch.decision_pending`) read the LAST entry
    only, on the documented ground that any later entry means the prompt moved on. A
    marker is a later entry, so the alert clears — read-only evidence, asserted here
    so the coupling is not rediscovered by an operator staring at a stuck alert."""
    entries = [{"kind": "decision-pending", "dw_id": "DW-1", "question": "?"}]
    assert data.pending_decision(entries) is not None
    entries.append({"kind": UNREADABLE_LINE_KIND, "bytes": 12})
    assert data.pending_decision(entries) is None


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
    record = platform_util.root_identity_record(tmp_path)
    journal.write_verify_stream("verify-1-1-a-dev-1-1-0.stdout.log", "out", run_dir_identity=record)
    journal.write_verify_stream("verify-1-1-a-dev-1-1-0.stderr.log", "err", run_dir_identity=record)

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


def _stamped_start(task_id="1-1-alpha-dev-3"):
    return {
        "kind": "session-start",
        "task_id": task_id,
        "role": "dev",
        "adapter": "claude",
        "model": "opus",
        "story_key": "1-1-alpha",
    }


def test_active_agent_idle_since_from_open_idle_stretch():
    """#680: the last `session-idle` for the open session's task, not closed by a
    later `session-active`, sets `idle_since` to its `since_ts`; the default is
    None, so every existing construction and comparison stands.

    ABLATION E: drop the `_idle_since` derivation and the first assertion reddens."""
    entries = [
        _stamped_start(),
        {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": 5030.0, "idle_s": 60.0},
    ]
    agent = data.active_agent(entries, None)
    assert agent is not None and agent.idle_since == 5030.0
    assert data.active_agent([_stamped_start()], None).idle_since is None


def test_active_agent_idle_since_cleared_by_session_active_and_session_end():
    idle = {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": 5030.0}
    active = {"kind": "session-active", "task_id": "1-1-alpha-dev-3", "idle_s": 120.0}
    agent = data.active_agent([_stamped_start(), idle, active], None)
    assert agent is not None and agent.idle_since is None
    # a later stretch reopens it with its own since_ts
    later = {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": 5150.0}
    agent = data.active_agent([_stamped_start(), idle, active, later], None)
    assert agent is not None and agent.idle_since == 5150.0
    # session-end closes the session: no agent at all
    ended = {"kind": "session-end", "task_id": "1-1-alpha-dev-3"}
    assert data.active_agent([_stamped_start(), idle, ended], None) is None


def test_active_agent_idle_since_ignores_other_tasks_and_earlier_sessions():
    """A `session-idle` for another task, or one left behind by an EARLIER session
    (before this session-start), is not this session's."""
    stale = {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": 4000.0}
    other = {"kind": "session-idle", "task_id": "2-2-beta-dev-1", "since_ts": 5030.0}
    agent = data.active_agent([stale, _stamped_start(), other], None)
    assert agent is not None and agent.idle_since is None


def test_active_agent_idle_since_never_raises_on_malformed_entry():
    """A `session-idle` without a numeric `since_ts` is skipped (the TUI ages the
    text from it); the agent itself is still derived."""
    for bad in (
        {"kind": "session-idle", "task_id": "1-1-alpha-dev-3"},
        {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": "soon"},
        {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": None},
        {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": True},
        {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": float("nan")},
        {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": float("inf")},
        {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": float("-inf")},
        {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": 10**1000},
    ):
        agent = data.active_agent([_stamped_start(), bad], None)
        assert agent is not None and agent.idle_since is None
        agent = data.active_agent(
            [
                _stamped_start(),
                {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": 5030.0},
                bad,
            ],
            None,
        )
        assert agent is not None and agent.idle_since == 5030.0
    # an int since_ts is a number too
    agent = data.active_agent(
        [
            _stamped_start(),
            {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": 5030},
        ],
        None,
    )
    assert agent is not None and agent.idle_since == 5030.0


def test_active_agent_idle_tool_from_open_idle_stretch():
    """The open stretch's `session-idle` `in_tool` slug sets `idle_tool`; a
    `session-idle` without one (a CLI that cannot tell) and no stretch at all
    both leave it None, the default.

    ABLATION: drop the `_idle_tool` derivation and the first assertion reddens."""
    idle = {
        "kind": "session-idle",
        "task_id": "1-1-alpha-dev-3",
        "since_ts": 5030.0,
        "in_tool": "Bash",
    }
    agent = data.active_agent([_stamped_start(), idle], None)
    assert agent is not None and agent.idle_tool == "Bash"
    unlabelled = {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": 5030.0}
    agent = data.active_agent([_stamped_start(), unlabelled], None)
    assert agent is not None and agent.idle_tool is None
    assert data.active_agent([_stamped_start()], None).idle_tool is None


def test_active_agent_idle_tool_cleared_by_session_active():
    """A `session-active` closes the stretch and its label; a later stretch
    carries its own, and a later UNLABELLED one never inherits the old slug.

    ABLATION: drop the `session-active` arm of `_idle_tool` and the first
    assertion reddens."""
    idle = {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "in_tool": "Bash"}
    active = {"kind": "session-active", "task_id": "1-1-alpha-dev-3", "idle_s": 120.0}
    agent = data.active_agent([_stamped_start(), idle, active], None)
    assert agent is not None and agent.idle_tool is None
    later = {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "in_tool": "mcp"}
    agent = data.active_agent([_stamped_start(), idle, active, later], None)
    assert agent is not None and agent.idle_tool == "mcp"
    bare = {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "since_ts": 5150.0}
    agent = data.active_agent([_stamped_start(), idle, active, bare], None)
    assert agent is not None and agent.idle_tool is None
    other = {"kind": "session-idle", "task_id": "2-2-beta-dev-1", "in_tool": "Bash"}
    agent = data.active_agent([_stamped_start(), other], None)
    assert agent is not None and agent.idle_tool is None


def test_active_agent_idle_tool_never_raises_on_malformed_entry():
    """A non-string `in_tool` reads as None — never rendered, never raised —
    and the agent itself is still derived.

    ABLATION: pass the raw value through and the non-string rows leak it."""
    for raw in (None, 7, True, ["Bash"], {"name": "Bash"}):
        bad = {"kind": "session-idle", "task_id": "1-1-alpha-dev-3", "in_tool": raw}
        agent = data.active_agent([_stamped_start(), bad], None)
        assert isinstance(agent, data.ActiveAgent) and agent.idle_tool is None


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


def test_project_paths_degrades_and_recovers_from_root_resolve_refusal(project_tree, monkeypatch):
    """A dead provider yields unavailable readers without poisoning recovery.

    INVERSE ablation: restore bare ``project.resolve()`` in ``_project_paths``
    and this test raises the stubbed WinError 64 on its first observation rather
    than returning empty panes and recovering after the provider is healthy.
    """
    install_bmad_config(project_tree)
    write_sprint(project_tree, {"1-1-a": "ready-for-dev"})
    root = project_tree.project

    with monkeypatch.context() as refusal:
        refuse_to_resolve(refusal, root)
        assert data._project_paths(root) is None
        assert data.sprint_overview(root) is None
        assert data.deferred_entries(root) is None
        missed = data.pending_missed_decisions(root)
        assert missed.items == [] and missed.fault is not None
        assert "cannot canonicalize" in missed.fault
        assert root not in data._paths_cache

    paths = data._project_paths(root)
    assert paths is not None
    assert paths.project == root
    assert data._paths_cache[root][1] is paths
    assert data.sprint_overview(root) is not None


def test_project_paths_uses_one_canonical_cache_key(project_tree):
    """Healthy aliases share one ProjectPaths snapshot under the canonical root.

    INVERSE ablation: key ``_paths_cache`` with the pre-canonical spelling while
    loading from the stable root and this test finds the ``..`` spelling as a
    second cache key instead of reusing the canonical entry.
    """
    install_bmad_config(project_tree)
    root = project_tree.project.resolve()
    alternate_spelling = root / ".." / root.name

    paths = data._project_paths(alternate_spelling)

    assert paths is not None
    assert paths.project == root
    assert data._project_paths(root) is paths
    assert root in data._paths_cache
    assert alternate_spelling not in data._paths_cache


def _override_implementation_artifacts(project, rel: str) -> None:
    (project.project / "_bmad" / "custom" / "config.toml").write_text(
        f'[modules.bmm]\nimplementation_artifacts = "{{project-root}}/{rel}"\n',
        encoding="utf-8",
    )


def test_project_paths_sees_a_toml_layer_edit_in_a_mixed_install(project_tree):
    """#769: the TOML layers outrank the legacy YAML the v6.12 installer still writes
    beside them, so an override-layer edit must invalidate the cached snapshot.

    Ablation: stat-gate on config.yaml alone and the second call serves the stale
    artifact dir from cache.
    """
    install_bmad_config(project_tree)
    install_bmad_central_config(project_tree)
    root = project_tree.project.resolve()
    before = data._project_paths(root)
    assert before is not None
    assert data._project_paths(root) is before

    _override_implementation_artifacts(project_tree, "moved-impl")

    after = data._project_paths(root)
    assert after is not None
    assert after.implementation_artifacts == root / "moved-impl"


def test_project_paths_caches_and_invalidates_a_toml_only_install(project_tree):
    """With no config.yaml there is still a source to stat-gate on: the snapshot is
    cached (it used to be reloaded on every call) and a layer edit invalidates it."""
    install_bmad_central_config(project_tree)
    root = project_tree.project.resolve()
    assert not (root / bmadconfig.LEGACY_CONFIG_REL).exists()

    first = data._project_paths(root)
    assert first is not None
    assert data._paths_cache[root][1] is first
    assert data._project_paths(root) is first

    _override_implementation_artifacts(project_tree, "moved-impl")

    second = data._project_paths(root)
    assert second is not None
    assert second is not first
    assert second.implementation_artifacts == root / "moved-impl"
    assert data._paths_cache[root][1] is second


def test_sprint_overview(project_tree):
    install_bmad_config(project_tree)
    write_sprint(
        project_tree,
        {
            "epic-1": "in-progress",
            "1-1-a": "ready-for-dev",
            "1-2-b": "done",
            "epic-1-retrospective": "optional",
            "epic-2": "backlog",
            "2-1-c": "backlog",
        },
    )
    ss = data.sprint_overview(project_tree.project)
    assert ss.epics == {1: "in-progress", 2: "backlog"}
    assert [(s.key, s.status) for s in ss.stories] == [
        ("1-1-a", "ready-for-dev"),
        ("1-2-b", "done"),
        ("2-1-c", "backlog"),
    ]
    assert ss.retros == {1: "optional"}

    # cached result (same object) until the file changes, then re-parsed
    assert data.sprint_overview(project_tree.project) is ss
    write_sprint(project_tree, {"1-1-a": "done"})
    assert [s.status for s in data.sprint_overview(project_tree.project).stories] == ["done"]


def test_sprint_overview_unavailable(tmp_path, project_tree):
    assert data.sprint_overview(tmp_path) is None  # no _bmad config at all
    install_bmad_config(project_tree)  # config but no sprint file
    assert data.sprint_overview(project_tree.project) is None

    # LLM-maintained file: malformed content must come back None, not raise
    project_tree.sprint_status.write_text("{ not: valid: yaml: [")
    assert data.sprint_overview(project_tree.project) is None
    project_tree.sprint_status.write_text("- just\n- a\n- list\n")
    assert data.sprint_overview(project_tree.project) is None


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


# ------------------------------------------------------------- tickets board
# Every tickets-board test reads a FakeTicketTree through `_board_ticket_tree`:
# real-spawn budget 0 (the Windows leg's budget).


def _tickets_state(root: Path, initiative: str = "init-a") -> RunState:
    return RunState(
        run_id="r1",
        project=str(root),
        started_at="2026-06-11T10:00:00",
        source="tickets",
        initiative=initiative,
        initiative_folder=str(root / "_bmad-output" / initiative) if initiative else "",
    )


def _fake_board_tree(monkeypatch, root: Path) -> FakeTicketTree:
    tree = FakeTicketTree.from_rows(
        root,
        initiative="init-a",
        ready_to_start=(ticket_row("1.2", plan_checkpoint=True),),
        others=(ticket_row("1.1", status="done"),),
        pinned_initiative="init-a",
    )
    seen: list[tuple[Path, str]] = []

    def board_tree(project: Path, initiative: str) -> FakeTicketTree:
        seen.append((project, initiative))
        return tree

    monkeypatch.setattr(data, "_board_ticket_tree", board_tree)
    monkeypatch.setattr(data, "_tickets_cache", {})
    tree.seen = seen  # type: ignore[attr-defined]
    return tree


def _write_plan(path: Path, status: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: {status}\n---\n", encoding="utf-8")


def test_tickets_overview_stat_gated(tmp_path, monkeypatch):
    """The board spawns `tickets.py status` once while the initiative's files
    are unchanged, and again when a plan (two levels down) or a `tickets.toml`
    changes; a file the script does not read changes nothing.

    Ablation (performed): drop the signature check in `initiative_board` and the
    second call spawns again — this FAILS."""
    tree = _fake_board_tree(monkeypatch, tmp_path)
    folder = tmp_path / "_bmad-output" / "init-a"
    plan = folder / "epic-cart" / "story-cart-model-plan.md"
    _write_plan(plan, "ready-for-dev")
    (folder / "tickets.toml").write_text("[[epic]]\n", encoding="utf-8")
    state = _tickets_state(tmp_path)

    first = data.tickets_overview(tmp_path, state)
    assert first.fault is None
    assert first.initiative == "init-a"
    assert [r.ref for r in first.rows] == ["1.2", "1.1"]
    assert data.tickets_overview(tmp_path, state) is first
    assert tree.calls == [("status",)]
    assert tree.seen == [(tmp_path, "init-a")]  # pinned to the run's initiative

    (folder / "notes.txt").write_text("not read by tickets.py\n", encoding="utf-8")
    assert data.tickets_overview(tmp_path, state) is first
    assert len(tree.calls) == 1

    _write_plan(plan, "in-progress")
    later = plan.stat().st_mtime_ns + 1_000_000_000
    os.utime(plan, ns=(later, later))
    data.tickets_overview(tmp_path, state)
    assert len(tree.calls) == 2

    (folder / "tickets.toml").write_text("[[epic]]\n# edited\n", encoding="utf-8")
    data.tickets_overview(tmp_path, state)
    assert len(tree.calls) == 3


def test_tickets_overview_rereads_a_plan_in_the_tickets_folder_layout(tmp_path, monkeypatch):
    """In the current upstream layout a plan sits four levels below the
    initiative (`tickets/epic-x/tickets/*-plan.md`); editing it re-reads the
    board. Ablation (performed): cap the signature walk at two levels and the
    cached board is served — this FAILS."""
    tree = _fake_board_tree(monkeypatch, tmp_path)
    folder = tmp_path / "_bmad-output" / "init-a"
    plan = folder / "tickets" / "epic-cart" / "tickets" / "story-cart-model-plan.md"
    _write_plan(plan, "ready-for-dev")
    state = _tickets_state(tmp_path)

    first = data.tickets_overview(tmp_path, state)
    assert data.tickets_overview(tmp_path, state) is first
    assert len(tree.calls) == 1

    _write_plan(plan, "in-progress")
    later = plan.stat().st_mtime_ns + 1_000_000_000
    os.utime(plan, ns=(later, later))
    data.tickets_overview(tmp_path, state)
    assert len(tree.calls) == 2


def test_tickets_overview_rereads_when_config_utils_changes(tmp_path, monkeypatch):
    """`tickets.py` execs the project's `config_utils.py` to resolve the store and
    initiative, so an upgraded helper re-reads a cached board.

    Ablation (performed): drop `V7_CONFIG_UTILS_REL` from the signature sources
    and the cached board is served — this FAILS."""
    tree = _fake_board_tree(monkeypatch, tmp_path)
    _write_plan(tmp_path / "_bmad-output" / "init-a" / "epic-cart" / "story-a-plan.md", "done")
    helper = tmp_path / bmadconfig.V7_CONFIG_UTILS_REL
    helper.parent.mkdir(parents=True)
    helper.write_text("# v1\n", encoding="utf-8")
    state = _tickets_state(tmp_path)

    data.tickets_overview(tmp_path, state)
    data.tickets_overview(tmp_path, state)
    assert len(tree.calls) == 1

    helper.write_text("# v2, repaired\n", encoding="utf-8")
    data.tickets_overview(tmp_path, state)
    assert len(tree.calls) == 2


def test_tickets_overview_follows_a_moved_project(tmp_path, monkeypatch):
    """A run recorded under a project that has since moved watches the live
    initiative folder: a plan edit there re-reads the board.

    Ablation (performed): drop the rebase in `tickets_overview` and the signature
    stats the vanished folder, so the edit never re-reads — this FAILS."""
    old, live = tmp_path / "old", tmp_path / "live"
    tree = _fake_board_tree(monkeypatch, live)
    plan = live / "_bmad-output" / "init-a" / "epic-cart" / "story-cart-model-plan.md"
    _write_plan(plan, "ready-for-dev")
    state = _tickets_state(old)

    data.tickets_overview(live, state)
    assert data.tickets_overview(live, state).fault is None
    assert len(tree.calls) == 1

    _write_plan(plan, "in-progress")
    later = plan.stat().st_mtime_ns + 1_000_000_000
    os.utime(plan, ns=(later, later))
    data.tickets_overview(live, state)
    assert len(tree.calls) == 2


def test_tickets_overview_carries_fault(tmp_path, monkeypatch):
    """A tree that cannot be read is a board with `fault` set and no rows —
    never None and never an empty board. The fault is cached like an answer,
    but retried once `TICKETS_FAULT_RETRY_S` has passed with no file changed.

    Ablation (performed): return `TicketsBoard(rows=(), fault=None, ...)` on the
    error and the fault assertion FAILS; drop the retry window and the
    recovered read never happens — the last assertion FAILS."""
    tree = _fake_board_tree(monkeypatch, tmp_path)
    (tmp_path / "_bmad-output" / "init-a").mkdir(parents=True)
    tree.faults["status"] = TicketsTimeout("tickets.py status took longer than 60s")
    state = _tickets_state(tmp_path)

    board = data.tickets_overview(tmp_path, state)
    assert board is not None
    assert board.rows == ()
    assert board.fault == "tickets.py timeout: tickets.py status took longer than 60s"
    assert data.tickets_overview(tmp_path, state) is board  # within the window
    assert len(tree.calls) == 1

    del tree.faults["status"]
    monkeypatch.setattr(data, "TICKETS_FAULT_RETRY_S", 0.0)
    recovered = data.tickets_overview(tmp_path, state)
    assert recovered.fault is None
    assert len(recovered.rows) == 2


def test_tickets_overview_unavailable_without_initiative(tmp_path, monkeypatch):
    """A run that pinned no initiative has no tree to read: the board says so in
    `fault` and spawns nothing. A project with no v7 active initiative gives the
    start-run preview a fault the same way.

    Ablation (performed): drop the initiative check in `tickets_overview` and
    the tree is read for initiative "" — this FAILS."""

    def no_tree(project: Path, initiative: str):
        raise AssertionError(f"read the tree for {initiative!r}")

    monkeypatch.setattr(data, "_board_ticket_tree", no_tree)
    monkeypatch.setattr(data, "_tickets_cache", {})
    board = data.tickets_overview(tmp_path, _tickets_state(tmp_path, initiative=""))
    assert board.rows == ()
    assert board.fault == "this run pinned no initiative"

    preview = data.project_tickets_overview(tmp_path)  # no BMAD config at all
    assert preview.rows == ()
    assert preview.fault is not None and preview.fault.startswith("BMAD config unreadable")


def test_project_tickets_overview_resolves_a_mixed_install_as_tickets_mode(tmp_path, monkeypatch):
    """A v7 install with the v6 keys left behind resolves v6 for an auto-detecting
    read but v7 for a tickets run (`load_paths(want_tickets=True)`). The start-run
    preview shows the board that run would start from, so it reads the v7 active
    initiative rather than answering "no v7 active initiative".

    Ablation (performed): resolve the preview with `_project_paths(project)` (no
    `want_tickets`) and this FAILS on the fault."""
    root = tmp_path / "mixed"
    root.mkdir()
    layout = install_bmad_v7(root)
    (root / "_bmad" / "custom" / "config.toml").write_text(
        "[modules.bmm]\n"
        'planning_artifacts = "{project-root}/_bmad-output/planning-artifacts"\n'
        'implementation_artifacts = "{project-root}/_bmad-output/implementation-artifacts"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(data, "_paths_cache", {})
    monkeypatch.setattr(data, "_tickets_paths_cache", {})
    read: list[tuple[str, Path]] = []

    def board(project: Path, initiative: str, folder: Path) -> data.TicketsBoard:
        read.append((initiative, folder))
        return data.TicketsBoard((), None, initiative)

    monkeypatch.setattr(data, "initiative_board", board)
    sprint_paths = data._project_paths(root)
    assert sprint_paths is not None and sprint_paths.era == "v6"  # the auto-detect stays v6

    preview = data.project_tickets_overview(root)
    assert preview.fault is None
    assert read == [(layout.initiative, layout.initiative_folder.resolve())]
    assert data._project_paths(root) is sprint_paths  # its own cache entry, untouched


# ------------------------------------------------------------- deferred work


def test_deferred_entries(project_tree):
    install_bmad_config(project_tree)
    project_tree.deferred_work.write_text(
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
    items = data.deferred_entries(project_tree.project)
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
    assert data.deferred_entries(project_tree.project) is items
    project_tree.deferred_work.write_text("# Deferred Work\n\nfreeform, no entries\n")
    assert data.deferred_entries(project_tree.project) == []


def test_deferred_entries_unavailable(tmp_path, project_tree):
    assert data.deferred_entries(tmp_path) is None  # no _bmad config at all
    install_bmad_config(project_tree)  # config but no ledger file
    assert data.deferred_entries(project_tree.project) is None


def test_deferred_entries_undecodable_ledger_is_unavailable(project_tree):
    """The pane already had an "unavailable" degrade (`items = None`), but reached it
    only for `OSError` — and `UnicodeDecodeError` is a `ValueError` (DW-146), so
    undecodable bytes escaped the whole refresh instead of rendering the pane
    unavailable. Same answer as a missing ledger: the dashboard cannot show entries
    it could not read.
    Ablation: revert the except tuple to `OSError` alone and this reddens with
    `UnicodeDecodeError` escaping rather than `None`."""
    install_bmad_config(project_tree)
    project_tree.deferred_work.write_bytes(b"# Deferred Work\n\n### DW-1: bad \xff byte\n")

    assert data.deferred_entries(project_tree.project) is None


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


def test_deferred_entries_does_not_read_severity_from_a_fenced_example(project_tree):
    install_bmad_config(project_tree)
    project_tree.deferred_work.write_text(
        "# Deferred Work\n\n"
        "### DW-1: quoted severity\n\n"
        "```markdown\nseverity: critical\n```\nstatus: open\n",
        encoding="utf-8",
    )

    items = data.deferred_entries(project_tree.project)

    assert items is not None
    assert items[0].severity is None


def test_deferred_entries_legacy_ledger(project_tree):
    install_bmad_config(project_tree)
    project_tree.deferred_work.write_text(
        "# Deferred Work\n\n"
        "## Deferred from: code review of story 1.2 (2026-04-06)\n\n"
        "- ~~**Old fixed thing** — was broken, then repaired~~ → fixed in 1.3\n"
        "- W9 — open item with a bracket severity. [MAJOR]\n"
        "- **Open bold-titled thing here** — details that run on and on\n",
        encoding="utf-8",
    )
    items = data.deferred_entries(project_tree.project)
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
    assert data.deferred_entries(project_tree.project) is items


def test_deferred_entries_mixed_ledger_in_file_order(project_tree):
    install_bmad_config(project_tree)
    project_tree.deferred_work.write_text(
        "# Deferred Work\n\n"
        "## Deferred from: epic 1 review (2026-04-06)\n\n"
        "- legacy item first in the file\n\n"
        "### DW-1: Canonical entry\n\n"
        "origin: test, 2026-06-01\nseverity: high\nreason: t.\nstatus: open\n",
        encoding="utf-8",
    )
    items = data.deferred_entries(project_tree.project)
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_project_paths_invalidates_when_a_dangling_link_appears_at_an_absent_layer(project_tree):
    """`_stat_sig` follows links and folds every OSError into None, so a dangling
    link created at a layer path that was absent signs exactly like the absence and
    the cache served stale paths. `load_paths` refuses that link, so must the TUI.

    Ablation: sign config sources with `_stat_sig` and the stale paths come back."""
    install_bmad_config(project_tree)
    install_bmad_central_config(project_tree)
    root = project_tree.project.resolve()
    layer = root / bmadconfig.CENTRAL_LAYERS_REL[3]
    layer.unlink()  # an optional layer the operator never wrote
    assert data._project_paths(root) is not None
    assert root in data._paths_cache
    layer.symlink_to("missing.toml")

    assert data._project_paths(root) is None


# ------------------------------------------- folded read faults (DW-472..475)


def _refuse_stat(monkeypatch, target: Path) -> None:
    """`target.stat()` raises EACCES; every other path stats normally."""
    real = Path.stat

    def fake(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "stubbed: permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake)


def test_classified_sig_tells_absence_from_a_stat_fault(tmp_path, monkeypatch):
    present = tmp_path / "f"
    present.write_text("x")
    assert data._classified_sig(present)[0] is not None
    assert data._classified_sig(present)[1] is None
    assert data._classified_sig(tmp_path / "missing") == (None, None)
    assert data._classified_sig(present / "under-a-file") == (None, None)  # ENOTDIR
    _refuse_stat(monkeypatch, present)
    sig, fault = data._classified_sig(present)
    assert sig is None and fault is not None and "PermissionError" in fault


def test_watcher_flags_a_persisting_state_parse_fault(tmp_path):
    """DW-472: the last good state survives a bad state.json, but once the SAME
    signature has failed twice the watcher says the shown state is stale — and the
    next good parse clears it. One failed look is forgiven (a torn read the next
    write heals).

    Ablation: drop `self.state_fault = why` in `_note_state_fault` and the second
    look's assertion reddens."""
    run_dir = make_run(tmp_path, "20260611-100000-aaaa", current_epic=1)
    watcher = data.RunWatcher(run_dir)
    assert watcher.state().current_epic == 1 and watcher.state_fault is None

    (run_dir / "state.json").write_text("{ broken for good")
    assert watcher.state().current_epic == 1
    assert watcher.state_fault is None  # first failed look: maybe torn, not flagged
    assert watcher.state().current_epic == 1  # still the last good...
    assert watcher.state_fault is not None  # ...but now marked stale
    assert "state.json unreadable" in watcher.state_fault
    assert "JSONDecodeError" in watcher.state_fault

    save_state(
        run_dir,
        RunState(run_id=run_dir.name, project=str(tmp_path), started_at="t", current_epic=2),
    )
    assert watcher.state().current_epic == 2
    assert watcher.state_fault is None


def test_watcher_forgives_a_torn_state_read_the_next_write_heals(tmp_path):
    """A failure at one signature followed by a good write at a new one never flags:
    the two-look rule compares signatures, not failure counts."""
    run_dir = make_run(tmp_path, "20260611-100000-aaaa", current_epic=1)
    watcher = data.RunWatcher(run_dir)
    watcher.state()
    (run_dir / "state.json").write_text("{ torn")
    watcher.state()
    save_state(
        run_dir,
        RunState(run_id=run_dir.name, project=str(tmp_path), started_at="t", current_epic=3),
    )
    assert watcher.state().current_epic == 3
    assert watcher.state_fault is None


def test_watcher_flags_state_json_gone_after_a_good_read(tmp_path):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa", current_epic=1)
    watcher = data.RunWatcher(run_dir)
    watcher.state()
    (run_dir / "state.json").unlink()
    watcher.state()
    assert watcher.state().current_epic == 1
    assert watcher.state_fault == "state.json is gone"


def test_watcher_clears_a_stat_fault_when_state_json_recovers_unchanged(tmp_path, monkeypatch):
    """A flagged stat fault clears once the last good read's file stats back at the
    SAME signature: a finished or paused run never rewrites state.json, so no fresh
    parse would ever arrive to clear it.

    Ablation: drop the clears in `state()`'s `sig == self._state_sig` branch and
    the recovered look's assertion reddens."""
    run_dir = make_run(tmp_path, "20260611-100000-aaaa", current_epic=1)
    watcher = data.RunWatcher(run_dir)
    watcher.state()
    with monkeypatch.context() as m:
        _refuse_stat(m, run_dir / "state.json")
        watcher.state()
        watcher.state()
        assert watcher.state_fault is not None and "PermissionError" in watcher.state_fault
    for _ in range(3):
        assert watcher.state().current_epic == 1
        assert watcher.state_fault is None
    # The recovery also retired the suspect: one fresh failed look is forgiven again.
    with monkeypatch.context() as m:
        _refuse_stat(m, run_dir / "state.json")
        watcher.state()
        assert watcher.state_fault is None


def test_watcher_state_never_written_is_not_stale(tmp_path):
    watcher = data.RunWatcher(tmp_path / "nope")
    for _ in range(3):
        assert watcher.state() is None
    assert watcher.state_fault is None


def test_watcher_state_fault_names_a_never_parsed_state(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "state.json").write_text("not json")
    watcher = data.RunWatcher(run_dir)
    watcher.state()
    assert watcher.state() is None
    assert watcher.state_fault is not None and "state.json unreadable" in watcher.state_fault


def test_watcher_attention_flags_an_unreadable_file(tmp_path):
    """DW-475: a read fault keeps the last text but is named in
    `attention_fault`, cleared by the next good read; absence is no fault.

    Ablation: drop the `attention_fault` assignment in the read arm and the
    directory-case assertion reddens."""
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    watcher = data.RunWatcher(run_dir)
    assert watcher.attention() == "" and watcher.attention_fault is None

    (run_dir / "ATTENTION").mkdir()  # stats fine, cannot be read as a file
    assert watcher.attention() == ""
    assert watcher.attention_fault is not None
    assert "ATTENTION unreadable" in watcher.attention_fault

    (run_dir / "ATTENTION").rmdir()
    (run_dir / "ATTENTION").write_text("[ts] gate: epic boundary\n", encoding="utf-8")
    assert watcher.attention() == "[ts] gate: epic boundary\n"
    assert watcher.attention_fault is None


def test_watcher_attention_undecodable_bytes_are_a_fault_not_a_crash(tmp_path):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "ATTENTION").write_bytes(b"\xff\xfe not utf-8\n")
    watcher = data.RunWatcher(run_dir)
    assert watcher.attention() == ""
    assert watcher.attention_fault is not None
    assert "UnicodeDecodeError" in watcher.attention_fault


def test_watcher_attention_stat_fault_keeps_text_and_flags(tmp_path, monkeypatch):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "ATTENTION").write_text("[ts] one\n", encoding="utf-8")
    watcher = data.RunWatcher(run_dir)
    assert watcher.attention() == "[ts] one\n"
    _refuse_stat(monkeypatch, run_dir / "ATTENTION")
    assert watcher.attention() == "[ts] one\n"
    assert watcher.attention_fault is not None and "cannot be stat'd" in watcher.attention_fault


def test_journal_tail_stat_fault_keeps_the_offset(tmp_path, monkeypatch):
    """DW-475: a stat FAULT is not absence — the offset survives it, so recovery
    reads only what is new, and the fault is named until then.

    Ablation: reset `_offset` on the fault arm and the post-recovery read returns
    all three entries (re-delivering two), reddening the last assertion."""
    journal = Journal(tmp_path)
    journal.append("run-start")
    journal.append("story-start", story="1-1-a")
    tail = data.JournalTail(tmp_path)
    assert len(tail.read_new()) == 2

    with monkeypatch.context() as m:
        _refuse_stat(m, tmp_path / "journal.jsonl")
        assert tail.read_new() == []
        assert tail.fault is not None and "journal.jsonl cannot be stat'd" in tail.fault

    journal.append("story-done", story="1-1-a")
    assert [e["kind"] for e in tail.read_new()] == ["story-done"]
    assert tail.fault is None


def test_journal_tail_open_fault_keeps_the_offset(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    journal.append("run-start")
    tail = data.JournalTail(tmp_path)
    assert len(tail.read_new()) == 1
    journal.append("story-start", story="1-1-a")

    real_open = Path.open

    def refuse(self, *args, **kwargs):
        if self == tmp_path / "journal.jsonl":
            raise PermissionError(13, "stubbed: permission denied", str(self))
        return real_open(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "open", refuse)
        assert tail.read_new() == []
        assert tail.fault is not None and "journal.jsonl unreadable" in tail.fault

    assert [e["kind"] for e in tail.read_new()] == ["story-start"]
    assert tail.fault is None


def test_journal_tail_absence_still_resets_without_a_fault(tmp_path):
    journal = Journal(tmp_path)
    journal.append("run-start")
    journal.append("story-start")
    tail = data.JournalTail(tmp_path)
    assert len(tail.read_new()) == 2
    (tmp_path / "journal.jsonl").unlink()
    assert tail.read_new() == []
    assert tail.fault is None
    Journal(tmp_path).append("run-resume")
    assert [e["kind"] for e in tail.read_new()] == ["run-resume"]


def test_log_view_prefix_scan_fault_is_flagged(tmp_path, monkeypatch):
    """DW-475: a cold-open prefix scan that cannot read leaves `altscreen_seen`
    False — which then means "not looked", so the fault is recorded for the pane.

    Ablation: drop the `altscreen_scan_fault` assignment and this reddens."""
    path = tmp_path / "task.log"
    path.write_bytes(b"\x1b[?1049h" + b"filler\r\n" * 2000 + b"THE-END\r\n")
    real_open = Path.open
    calls = {"n": 0}

    def fail_first(self, *args, **kwargs):
        if self == path:
            calls["n"] += 1
            if calls["n"] == 1:  # the prefix scan opens first
                raise PermissionError(13, "stubbed: permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_first)
    view = data.LogView(path, max_bytes=1024)
    assert view.read_new() is True
    assert view.altscreen_seen is False
    assert view.altscreen_scan_fault is not None
    assert "PermissionError" in view.altscreen_scan_fault


def test_log_view_clean_scan_records_no_fault(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(b"filler\r\n" * 2000)
    view = data.LogView(path, max_bytes=1024)
    assert view.read_new() is True
    assert view.altscreen_scan_fault is None


def test_active_agent_malformed_entry_is_unreadable_not_absent():
    """DW-474: a malformed entry answers `UnreadableAgent` with the fault, never
    `None` (which means no session is open).

    Ablation: return None from the narrowed except arm and this reddens."""
    agent = data.active_agent([_stamped_start(), None], None)  # type: ignore[list-item]
    assert isinstance(agent, data.UnreadableAgent)
    assert "AttributeError" in agent.error


def test_active_agent_does_not_swallow_an_unexpected_exception(monkeypatch):
    """DW-474: only the malformed-data faults are caught; a bug propagates.

    Ablation: widen the except back to `Exception` and this reddens."""

    def bug(*_args, **_kwargs):
        raise RuntimeError("programming error")

    monkeypatch.setattr(data, "_idle_since", bug)
    with pytest.raises(RuntimeError, match="programming error"):
        data.active_agent([_stamped_start()], None)


def test_pending_missed_decisions_read_fault_is_named_and_not_cached(project, monkeypatch):
    """DW-473: a raising read answers a fault, not an empty list, and is not
    cached — the next call, with nothing on disk changed, reads for real.

    Ablation: cache the fault result (or return it without `fault`) and one of the
    two assertions reddens."""
    from conftest import write_ledger

    from bmad_loop import decisions

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _write_triage_decision(make_run(project.project, "20260101-000000-aaaa"))

    with monkeypatch.context() as m:

        def boom(_project, _paths=None):
            raise PermissionError(13, "stubbed: permission denied")

        m.setattr(decisions, "pending_missed_decisions", boom)
        missed = data.pending_missed_decisions(project.project)
        assert missed.items == []
        assert missed.fault is not None and "PermissionError" in missed.fault

    missed = data.pending_missed_decisions(project.project)
    assert missed.fault is None
    assert [d.id for d in missed.items] == ["DW-1"]


def test_pending_missed_decisions_unreadable_ledger_is_a_fault(project_tree):
    """DW-473: `decisions.pending_missed_decisions` reads an undecodable ledger as
    "no open ids" and answers []; the TUI reader probes the ledger the way
    `cmd_decisions` does and names the fault instead.

    Ablation: drop the `read_for_observation` probe and `fault` is None."""
    from conftest import UNDECODABLE_LEDGER

    install_bmad_config(project_tree)
    project_tree.deferred_work.write_bytes(UNDECODABLE_LEDGER)
    _write_triage_decision(make_run(project_tree.project, "20260101-000000-aaaa"))
    missed = data.pending_missed_decisions(project_tree.project)
    assert missed.items == []
    assert missed.fault is not None and "deferred-work ledger unreadable" in missed.fault
    assert "UnicodeDecodeError" in missed.fault


def _deny_stat_under(monkeypatch, root: Path) -> None:
    """Every `stat()` of `root` or below raises EACCES — an unreadable dir, as
    3.14's `is_dir`/`is_file` would fold it into False."""
    real = Path.stat

    def stat(self, *args, **kwargs):
        if self == root or root in self.parents:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)


def test_pending_missed_decisions_incomplete_run_listing_is_a_fault(project, monkeypatch):
    """DW-468: a run dir the listing cannot read holds triage this reader never
    saw, so the readable run's DW-1 is not the whole answer — the reader names the
    fault instead, and does not cache it: once the run reads again, so does DW-1.

    Ablation: drop the `listing_fault` return in `pending_missed_decisions` and
    this reddens — `fault` is None over a partial listing."""
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _write_triage_decision(make_run(project.project, "20260101-000000-aaaa"))
    bad = make_run(project.project, "20260102-000000-bbbb")

    with monkeypatch.context() as m:
        _deny_stat_under(m, bad)
        missed = data.pending_missed_decisions(project.project)
        assert missed.items == []
        assert missed.fault is not None and "run listing incomplete" in missed.fault
        assert "20260102-000000-bbbb" in missed.fault

    missed = data.pending_missed_decisions(project.project)
    assert missed.fault is None
    assert [d.id for d in missed.items] == ["DW-1"]


def test_pending_missed_decisions_none_pending_is_an_answer(project):
    from conftest import write_ledger

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "done 2026-06-01"})
    missed = data.pending_missed_decisions(project.project)
    # `ledger` names what the (empty) listing was read from, for the answer's
    # `listed_ledger` guard.
    assert missed == data.MissedDecisions([], ledger=project.deferred_work)


# ----------------------------------------- pinned reads in the observer (DW-564)


def _swap_runs_dir_for_a_foreign_tree(root: Path, run_id: str) -> Path:
    """Swap `runs/` for a link to an outside tree holding a REAL `<run_id>/` with its
    own state.json and journal.jsonl; return that outside run dir."""
    outside = root.parent / f"{root.name}-outside"
    foreign = outside / run_id
    save_state(
        foreign,
        RunState(run_id=run_id, project="elsewhere", started_at="t", current_epic=99),
    )
    Journal(foreign).append("foreign-row")
    runs_dir = root / RUNS_DIR
    runs_dir.rename(runs_dir.with_name("runs-aside"))
    runs_dir.symlink_to(outside, target_is_directory=True)
    return foreign


def _minted_run(root: Path, run_id: str = "20260611-100000-aaaa") -> tuple[Path, RunState]:
    run_dir = root / RUNS_DIR / run_id
    run_dir.mkdir(parents=True)
    state = RunState(
        run_id=run_id,
        project=str(root),
        started_at="t",
        current_epic=1,
        run_dir_identity=platform_util.root_identity_record(run_dir),
    )
    save_state(run_dir, state)
    Journal(run_dir, pin=state).append("run-start")
    return run_dir, state


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_journal_tail_flags_a_swapped_run_dir_and_keeps_reading(tmp_path):
    """An observer never refuses: with the run's held state, a `runs/` swapped for a
    link to a foreign tree still yields rows (read by path), but `fault` names the
    swap so the pane is not presented as this run's. An intact dir sets no fault.

    Ablation (run): drop `self.fault = self._swap_fault(pin)` in `read_new` and the
    swapped look's `fault` assertion fails with None."""
    run_dir, state = _minted_run(tmp_path)
    tail = data.JournalTail(run_dir)
    assert [e["kind"] for e in tail.read_new(pin=state)] == ["run-start"]
    assert tail.fault is None

    _swap_runs_dir_for_a_foreign_tree(tmp_path, run_dir.name)
    tail = data.JournalTail(run_dir)
    assert [e["kind"] for e in tail.read_new(pin=state)] == ["foreign-row"]
    assert tail.fault is not None and "no longer the directory" in tail.fault
    assert [e["kind"] for e in data.JournalTail(run_dir).read_new()] == ["foreign-row"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_journal_tail_flags_a_linked_journal_file(tmp_path):
    """`journal.jsonl` itself replaced by a link: rows still returned, `fault` set."""
    run_dir, state = _minted_run(tmp_path)
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text('{"ts": 1, "kind": "foreign-row"}\n', encoding="utf-8")
    (run_dir / "journal.jsonl").unlink()
    (run_dir / "journal.jsonl").symlink_to(outside)

    tail = data.JournalTail(run_dir)
    assert [e["kind"] for e in tail.read_new(pin=state)] == ["foreign-row"]
    assert tail.fault is not None and "journal.jsonl is a link" in tail.fault


def test_journal_tail_pin_tolerates_a_stale_device_or_absent_record(tmp_path):
    """A renumbered `st_dev` alone (the writers' degrade) and a state with no record
    are not swaps: no fault."""
    run_dir, state = _minted_run(tmp_path)
    dev, ino = state.run_dir_identity
    for record in ((dev + 1, ino), None):
        state.run_dir_identity = record
        tail = data.JournalTail(run_dir)
        assert [e["kind"] for e in tail.read_new(pin=state)] == ["run-start"]
        assert tail.fault is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_watcher_refuses_a_foreign_state_after_a_runs_dir_swap(tmp_path):
    """The watcher's reloads are pinned to its last good parse: `runs/` swapped for a
    link to a tree holding a real `<id>/state.json` keeps the last good state and,
    once the fault persists, names the refusal — never shows the foreign record.

    Ablation (run): drop `pin=self._state` from `RunWatcher.state`'s `load_state`
    and the second look returns `current_epic == 99`."""
    run_dir, _ = _minted_run(tmp_path)
    watcher = data.RunWatcher(run_dir)
    assert watcher.state().current_epic == 1

    _swap_runs_dir_for_a_foreign_tree(tmp_path, run_dir.name)
    assert watcher.state().current_epic == 1
    assert watcher.state().current_epic == 1
    assert watcher.state_fault is not None
    assert "UnconfinedReadError" in watcher.state_fault


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("linked", ["journal", "run-dir"])
def test_journal_tail_flags_a_link_on_a_no_record_run(tmp_path, linked):
    """A pin with no `run_dir_identity` has no identity to compare, but the pinned
    readers still refuse a link at the run dir or at `journal.jsonl` there — so the
    pane flags both, too.

    Ablation (run): return None from `_swap_fault` when the pin has no record and
    both rows fail with `fault` None."""
    run_dir, state = _minted_run(tmp_path)
    state.run_dir_identity = None
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "journal.jsonl").write_text('{"ts": 1, "kind": "foreign-row"}\n', encoding="utf-8")
    if linked == "journal":
        (run_dir / "journal.jsonl").unlink()
        (run_dir / "journal.jsonl").symlink_to(outside / "journal.jsonl")
    else:
        run_dir.rename(run_dir.with_name("aside"))
        run_dir.symlink_to(outside, target_is_directory=True)

    tail = data.JournalTail(run_dir)
    assert [e["kind"] for e in tail.read_new(pin=state)] == ["foreign-row"]
    assert tail.fault is not None and "is a link" in tail.fault
