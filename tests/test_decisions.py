"""Pre-answer store, discovery of missed decisions, and out-of-band apply."""

import contextlib
import json
import shutil
import sys
from pathlib import Path

import pytest
from conftest import (
    NUL_PATH_RESOLVE_FAULTS,
    fault_metadata_probe,
    fault_read_text,
    git,
    ignore_before_commit,
    install_bmad_config,
    refuse_to_resolve,
    write_ledger,
)

from bmad_loop import decisions, deferredwork, platform_util, runs
from bmad_loop.sweep import DecisionOption


def _decision(dw_id, *, question="q", options=None, recommendation="1"):
    options = options or [
        {"key": "1", "label": "Build it", "effect": "build", "intent": "do it"},
        {"key": "2", "label": "Keep as is", "effect": "keep-open"},
    ]
    return {
        "id": dw_id,
        "question": question,
        "context": "ctx",
        "options": options,
        "recommendation": recommendation,
    }


def _triage(open_ids, decisions_):
    return {
        "workflow": "deferred-sweep-triage",
        "open_ids": list(open_ids),
        "already_resolved": [],
        "bundles": [],
        "blocked": [],
        "skip": [],
        "decisions": decisions_,
        "escalations": [],
    }


def _make_run(project, run_id, triage_rj, cycle=1):
    run_dir = project.project / ".bmad-loop" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text("{}", encoding="utf-8")  # so list_run_dirs sees it
    name = "triage.json" if cycle == 1 else f"triage-{cycle}.json"
    (run_dir / name).write_text(json.dumps(triage_rj), encoding="utf-8")
    return run_dir


# ------------------------------------------------------------- store I/O


def test_store_round_trip_and_prune(project):
    opt = DecisionOption(key="1", label="Build it", effect="build", intent="do it")
    decisions.record_pre_answer(project.project, "DW-7", opt, date="2026-06-13")
    loaded = decisions.load_pre_answers(project.project)
    assert loaded["DW-7"]["effect"] == "build"
    assert loaded["DW-7"]["intent"] == "do it"
    assert loaded["DW-7"]["answered_at"] == "2026-06-13"

    # only entries whose id is still open survive a prune
    dropped = decisions.prune_pre_answers(project.project, {"DW-9"})
    assert dropped == ["DW-7"]
    assert decisions.load_pre_answers(project.project) == {}


def test_a_store_writer_that_cannot_lock_writes_nothing(project, monkeypatch):
    """A failed acquisition propagates without touching the store: a write that
    could not be serialized fails loudly rather than proceeding unlocked. The
    error shape is the one `msvcrt.locking` raises when its ~10 s retry runs out
    — a routine outcome on the Windows legs, not a contrived one. Patched at
    `deferredwork.file_lock` because the store's writers take the ledger's lock
    helper keyed on the store path (DW-161); the `StateRootError` sibling of this
    row is `test_a_store_noop_succeeds_when_no_state_root_is_derivable`."""
    _opt = DecisionOption(key="1", label="Build it", effect="build", intent="do it")
    decisions.record_pre_answer(project.project, "DW-1", _opt, date="2026-06-13")
    store = decisions.store_path(project.project)
    before = store.read_bytes()

    @contextlib.contextmanager
    def unavailable(path, **kwargs):
        raise OSError(11, "Resource deadlock avoided")
        yield  # pragma: no cover — unreachable

    monkeypatch.setattr(deferredwork, "file_lock", unavailable)

    with pytest.raises(OSError):
        decisions.record_pre_answer(project.project, "DW-2", _opt, date="2026-06-13")
    with pytest.raises(OSError):
        decisions.drop_pre_answer(project.project, "DW-1", answer=json.loads(before)["DW-1"])
    assert store.read_bytes() == before


def test_drop_pre_answer_removes_one_entry_and_leaves_the_rest(project):
    """DW-143's store primitive at its own layer. `prune_pre_answers` above is
    covered directly; its single-id sibling was reachable only through
    `SweepEngine._materialize_bundles`, which cannot see the returned bool at all
    and pins the no-op branch only indirectly.

    Four claims: the bool reports whether an entry was actually removed (all
    branches), an absent id writes NOTHING — the file's bytes are untouched, so the
    keep-open drop of an answer that only ever lived in `<run>/decisions.json`
    cannot re-serialize a store it has no business rewriting — an id whose entry is
    no longer the `answer` being retired is left alone the same way (the caller's
    copy is a stale read; what the store holds now is a human's LATER answer, and
    deleting it on the strength of the stale copy is exactly the loss the keyword
    exists to refuse), and a real removal carries every sibling through, the
    unusable one included, since `load_pre_answers` validates only the top level
    and an unrelated write must not delete a human's corrupt entry.

    Ablation: drop the `dw_id not in data` half of the early return and the bool
    and the byte-equality both redden; drop the `data[dw_id] != answer` half and
    the replaced-entry branch reddens; write `_write_store(project, {})` and the
    siblings redden."""
    opt = DecisionOption(key="1", label="Build it", effect="build", intent="do it")
    decisions.record_pre_answer(project.project, "DW-7", opt, date="2026-06-13")
    decisions.record_pre_answer(project.project, "DW-8", opt, date="2026-06-13")
    store = decisions.store_path(project.project)
    # planted past the writer, the way a hand edit would: unusable, and not ours to repair
    data = json.loads(store.read_text(encoding="utf-8"))
    data["DW-9"] = ["not a decision answer at all"]
    store.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    before = store.read_bytes()
    dw7 = decisions.load_pre_answers(project.project)["DW-7"]

    # an id the store never held: False, and not one byte written
    assert decisions.drop_pre_answer(project.project, "DW-404", answer=dw7) is False
    assert store.read_bytes() == before

    # an id the store holds under a DIFFERENT value than the one being retired — a
    # human re-answered it since the caller read its copy: False, not one byte written
    superseded = {**dw7, "answered_at": "2026-06-01"}
    assert decisions.drop_pre_answer(project.project, "DW-7", answer=superseded) is False
    assert store.read_bytes() == before

    assert decisions.drop_pre_answer(project.project, "DW-7", answer=dw7) is True
    remaining = decisions.load_pre_answers(project.project)
    assert set(remaining) == {"DW-8", "DW-9"}
    assert remaining["DW-8"]["intent"] == "do it"
    assert remaining["DW-9"] == ["not a decision answer at all"]
    # and removing the same id twice is False the second time
    assert decisions.drop_pre_answer(project.project, "DW-7", answer=dw7) is False


def test_load_pre_answers_tolerates_garbage(project):
    decisions.store_path(project.project).parent.mkdir(parents=True, exist_ok=True)
    decisions.store_path(project.project).write_text("not json", encoding="utf-8")
    assert decisions.load_pre_answers(project.project) == {}


def test_load_pre_answers_tolerates_undecodable_bytes(project):
    """DW-140. The sibling fault `json.JSONDecodeError` never covered: bytes that
    are not UTF-8 at all raise `UnicodeDecodeError` out of `read_text` BEFORE any
    JSON parsing, so one bad byte in the project store aborted the whole sweep
    that read it (and `bmad-loop decisions` with it).
    Ablation: drop `UnicodeDecodeError` from the except tuple in
    `load_pre_answers` and this reddens with that exception rather than {}."""
    store = decisions.store_path(project.project)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b'{"DW-1": {"effect": "\xff"}}')

    assert decisions.load_pre_answers(project.project) == {}


@pytest.mark.parametrize("is_file_answers", ["raises-like-3.13", "false-like-3.14"])
def test_load_pre_answers_tolerates_a_refused_metadata_probe(project, monkeypatch, is_file_answers):
    """DW-261. The docstring promises tolerance of "an unreadable one", and the
    bare `if not path.is_file(): return {}` OUTSIDE the `try` made that false on
    Python 3.11–3.13, where `is_file()` calls `stat()` and re-raises a metadata
    refusal: a `PermissionError` escaped this "total" helper out of
    `_decisions_phase` and every other caller. (On 3.14 the same probe
    suppressed the refusal and answered False — silent `{}`, which is at least
    the contract.) The probe is `stat` + `S_ISREG` INSIDE the `try` now, so the
    refusal folds into the same `{}` every other fault class answers, on every
    interpreter.

    Two rows because the bug had a different face per interpreter, and the
    ablation can only red through the 3.13 face. `raises-like-3.13` leaves
    `Path.is_file` alone: on the 3.11–3.13 dev interpreter it reaches the
    refused `stat` and raises, which is the escape DW-261 closes, and it is the
    row the ablation reds. `false-like-3.14` pins `Path.is_file` False for the
    store, the simulated-3.14 shape: it pins that the new probe answers `{}`
    without consulting `is_file` at all, but a restored bare `is_file()` answers
    `{}` for it too on 3.11–3.13; on a real 3.14 interpreter the ablated bare
    `is_file()` ignores the pin's target-less `stat`, answers True for the
    readable store and returns its content, so that row DOES red there.
    Ablation: restore the bare `if not path.is_file(): return {}` above the `try`
    and the `raises-like-3.13` row reds with `PermissionError` escaping."""
    store = decisions.store_path(project.project)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text('{"DW-1": {"effect": "build"}}', encoding="utf-8")
    if is_file_answers == "false-like-3.14":
        real_is_file = Path.is_file
        monkeypatch.setattr(
            Path,
            "is_file",
            lambda self, *a, **kw: False if self == store else real_is_file(self, *a, **kw),
        )
    fault_metadata_probe(monkeypatch, store, "stat")

    assert decisions.load_pre_answers(project.project) == {}


def test_load_pre_answers_tolerates_a_non_encodable_store_path(project):
    """`Path.stat` raises `ValueError` for a path the OS cannot encode (an
    embedded NUL), which the old bare `is_file()` absorbed; the helper stays
    total for it.
    Ablation: drop `ValueError` from the except tuple and this reds with it
    escaping."""
    assert decisions.load_pre_answers(Path(str(project.project) + "\0")) == {}


def test_load_pre_answers_treats_a_directory_at_the_store_as_empty(project):
    """The absence half of DW-261's classification: a DIRECTORY at the store's
    name is a present target of the wrong TYPE, which `is_file()` answered False
    for and `S_ISREG` answers False for too — `{}`, and nothing raised. Without
    the `S_ISREG` test the directory would reach `read_text` as an
    `IsADirectoryError`, still caught, but by the content arm rather than the
    silent absence one this row pins.
    Ablation: drop the `S_ISREG` test and this stays green only through the
    `OSError` arm; drop both and it reds with `IsADirectoryError`."""
    store = decisions.store_path(project.project)
    store.mkdir(parents=True)

    assert decisions.load_pre_answers(project.project) == {}


def test_record_pre_answer_write_failure_raises_and_keeps_the_store(project, monkeypatch):
    """#363. `_write_store` is a read-modify-rewrite of a file nothing gitignores,
    so its temp must not outlive a failed write: a stranded
    `.bmad-loop/decisions.tmp` is an untracked file that holds `worktree_clean`
    False until a human deletes it. Routing through the helper is what closes that
    — it unlinks its own temp on any raise — and the raise still reaches the caller.

    Patched at decisions' OWN binding, never `Path.write_text`: the helper writes
    through an `mkstemp` fd via `os.fdopen`, so a `Path` patch never fires and the
    test would pass having exercised nothing.

    Ablation A3: revert `_write_store` to the hand-rolled `tmp.write_text(...)` +
    `atomic_replace` and this reddens alone — loudly, as an AttributeError from
    `monkeypatch.setattr`, because the module binding disappears with the revert."""
    path = decisions.store_path(project.project)
    decisions.record_pre_answer(
        project.project,
        "DW-7",
        DecisionOption(key="1", label="Build it", effect="build", intent="do it"),
        date="2026-06-13",
    )
    before = path.read_bytes()

    def boom(path, text, *, confine_root, require_writable_target=False):
        raise OSError("disk full")

    monkeypatch.setattr(decisions, "atomic_write_text_confined", boom)
    with pytest.raises(OSError, match="disk full"):
        decisions.record_pre_answer(
            project.project,
            "DW-9",
            DecisionOption(key="2", label="Keep as is", effect="keep-open"),
            date="2026-06-14",
        )

    assert path.read_bytes() == before
    assert b"DW-9" not in path.read_bytes()  # the specific mutation that must not land


# ------------------------------------------------------- discovery


def test_pending_missed_decisions_most_recent_wins_and_filters(project):
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open", "DW-2": "open", "DW-3": "done 2026-06-01"})
    # older run: DW-1 with stale wording; newer run: DW-1 (fresh wording) + DW-2;
    # DW-3 surfaces too but is closed in the ledger
    _make_run(
        project, "20260101-000000-aaaa", _triage(["DW-1"], [_decision("DW-1", question="old")])
    )
    _make_run(
        project,
        "20260102-000000-bbbb",
        _triage(
            ["DW-1", "DW-2", "DW-3"],
            [
                _decision("DW-1", question="new"),
                _decision("DW-2"),
                _decision("DW-3"),
            ],
        ),
    )
    # DW-2 already pre-answered out of band -> excluded
    decisions.record_pre_answer(
        project.project,
        "DW-2",
        DecisionOption(key="2", label="x", effect="keep-open"),
        date="2026-06-13",
    )

    pending = decisions.pending_missed_decisions(project.project)
    ids = [d.id for d in pending]
    assert ids == ["DW-1"]  # DW-2 answered, DW-3 closed
    assert pending[0].question == "new"  # newest run's wording


def test_pending_missed_decisions_re_offers_an_id_whose_stored_value_is_unusable(project):
    """DW-134's loose end. `load_pre_answers` validates only the top level, so a
    store VALUE can be any JSON; a sweep drops such a value and re-files the id as
    unanswered. Keying `answered` off presence alone therefore hid the id from
    this command while every sweep skipped it — unanswerable until a human opened
    the file. DW-2 (well-shaped) still counts as answered, so the exclusion is not
    simply gone. Ablation: restore `answered = set(load_pre_answers(project))` and
    this reddens — DW-1 drops out of the list."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    _make_run(
        project,
        "20260101-000000-aaaa",
        _triage(["DW-1", "DW-2"], [_decision("DW-1"), _decision("DW-2")]),
    )
    store = decisions.store_path(project.project)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "DW-1": "keep-open",  # a bare effect string, not the answer object
                "DW-2": {"key": "2", "label": "Keep as is", "effect": "keep-open"},
            }
        ),
        encoding="utf-8",
    )

    assert [d.id for d in decisions.pending_missed_decisions(project.project)] == ["DW-1"]


@pytest.mark.parametrize(
    "unusable",
    [
        pytest.param({}, id="empty-object"),
        pytest.param({"effect": "frobnicate"}, id="unknown-effect"),
        pytest.param({"effect": ["build"]}, id="non-string-effect"),
        pytest.param(
            {"key": "1", "label": "Widen", "effect": "build", "intent": ["a", "b"]},
            id="non-string-intent",
        ),
        pytest.param({"key": 1, "effect": "keep-open"}, id="non-string-key"),
        pytest.param(
            {"key": "1", "label": "Widen", "effect": "build", "bundle_name": 7},
            id="non-string-bundle-name",
        ),
    ],
)
def test_pending_missed_decisions_re_offers_ids_whose_stored_shape_is_unusable(project, unusable):
    """DW-142. DW-134 widened `answered` from key presence to "the value is a
    dict", which still counted every shape a sweep now refuses: no `effect`, an
    `effect` this store's reader will not consume — one outside `DECISION_EFFECTS`,
    or `close`, which is inside that set but refused HERE since DW-147 — or a
    scalar the bundle lanes read as a string that is not one. Each is an id
    `_materialize_bundles` silently ignores while this command called it answered —
    so no reader ever surfaced it. Both readers of THIS store share
    `sweep.unusable_answer_reason` at one identical configuration
    (`allow_close=False`), so the two sets agree by construction per store; the
    run-local store's reader legitimately differs on `close` alone.
    DW-2 (well-shaped) still counts as answered, so the exclusion is
    per-value rather than "the store has a bad entry, offer everything".
    Ablation: restore `if isinstance(v, dict)` in `pending_missed_decisions` and
    every parametrization but the non-dict cases reddens — DW-1 drops out."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    _make_run(
        project,
        "20260101-000000-aaaa",
        _triage(["DW-1", "DW-2"], [_decision("DW-1"), _decision("DW-2")]),
    )
    store = decisions.store_path(project.project)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "DW-1": unusable,
                "DW-2": {"key": "2", "label": "Keep as is", "effect": "keep-open"},
            }
        ),
        encoding="utf-8",
    )

    assert [d.id for d in decisions.pending_missed_decisions(project.project)] == ["DW-1"]


def test_pending_missed_decisions_re_offers_a_close_answer_in_the_project_store(project):
    """DW-147, the `bmad-loop decisions` half of the defect. `close` never reaches
    the PROJECT store legitimately — `apply_pre_answer` applies it to the ledger
    and skips `record_pre_answer` — so a `close` here is hand-seeded or corrupt.
    The predicate accepted it anyway (it is a `DECISION_EFFECTS` member), which
    counted the id answered while no `_materialize_bundles` lane acts on a `close`:
    never built, never closed, never re-offered. Store-aware now, so the id comes
    back down this path, which is the repair — re-answering overwrites the value.
    DW-2 (well-shaped) stays answered, so the exclusion is per-value.
    Ablation: pass `allow_close=True` here (or delete the gate) and this reddens
    with DW-1 absent."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open", "DW-2": "open"})
    _make_run(
        project,
        "20260101-000000-aaaa",
        _triage(["DW-1", "DW-2"], [_decision("DW-1"), _decision("DW-2")]),
    )
    store = decisions.store_path(project.project)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "DW-1": {"key": "3", "effect": "close"},
                "DW-2": {"key": "2", "label": "Keep as is", "effect": "keep-open"},
            }
        ),
        encoding="utf-8",
    )

    assert [d.id for d in decisions.pending_missed_decisions(project.project)] == ["DW-1"]


def test_pending_missed_decisions_skips_an_undecodable_triage_cache(project):
    """DW-145. The read loop caught `(json.JSONDecodeError, OSError)`, but
    `UnicodeDecodeError` is a `ValueError`: bytes that are not UTF-8 at all raise
    out of `read_text` BEFORE any JSON parsing, so one bad byte in one run's
    cached triage escaped out of this helper, past every caller: `cmd_decisions`
    and `cmd_status` catch `BmadConfigError` alone (so `main`'s backstop turned
    the command into exit 1) and the TUI catches `(BmadConfigError, OSError)` (so
    it escaped outright). Degradation is per FILE: the newest run is skipped and the
    older run's DW-1 still surfaces, so the widening is not "return nothing".
    Ablation: revert the except tuple to `(json.JSONDecodeError, OSError)` and
    this reddens with `UnicodeDecodeError` rather than ["DW-1"]."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run(project, "20260101-000000-aaaa", _triage(["DW-1"], [_decision("DW-1")]))
    bad = _make_run(project, "20260102-000000-bbbb", _triage([], []))
    (bad / "triage.json").write_bytes(b'{"workflow": "deferred-sweep-triage", "x": "\xff"}')

    assert [d.id for d in decisions.pending_missed_decisions(project.project)] == ["DW-1"]


def test_pending_missed_decisions_survives_an_undecodable_ledger(project):
    """DW-146's OBSERVATION row. This helper's ledger read sat outside any guard,
    one function over from the triage-cache read DW-145 hardened — and it faults
    the same way, because `UnicodeDecodeError` is a `ValueError` that no
    `except OSError` above it catches. Every caller is a read-only surface
    (`cmd_decisions`, `cmd_status`, the TUI), so one undecodable byte must not take
    the whole listing down: an unreadable ledger yields no open ids, which is [].
    The degradation is silent here because no journal is reachable from a
    module-level function handed only a project path — the same reason the triage
    read below it degrades silently.
    Ablation: revert the read to
    `ledger.read_text(encoding="utf-8") if ledger.is_file() else ""` and this
    reddens with `UnicodeDecodeError` escaping rather than []."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run(project, "20260101-000000-aaaa", _triage(["DW-1"], [_decision("DW-1")]))
    project.deferred_work.write_bytes(b"# Deferred Work\n\n### DW-1: bad \xff byte\n")

    assert decisions.pending_missed_decisions(project.project) == []


def test_pending_missed_decisions_skips_an_unparseable_triage_cache(project):
    """The sibling arm the DW-145 widening sits beside: truncated JSON is skipped
    the same way, and the other run still contributes. Guards against a widening
    that accidentally narrows — both faults share one `continue`."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run(project, "20260101-000000-aaaa", _triage(["DW-1"], [_decision("DW-1")]))
    bad = _make_run(project, "20260102-000000-bbbb", _triage([], []))
    (bad / "triage.json").write_text('{"workflow": "deferred-sw', encoding="utf-8")

    assert [d.id for d in decisions.pending_missed_decisions(project.project)] == ["DW-1"]


def test_pending_missed_decisions_skips_an_unreadable_triage_cache(project, monkeypatch):
    """The third arm sharing that `continue`: a filesystem refusal on one run's
    cache. Distinct from JSON and UTF-8 decoding — it raises before either — and
    the same degrade applies, so the older run's DW-1 still surfaces. Ablation:
    drop `OSError` from the except tuple and this raises `PermissionError`."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run(project, "20260101-000000-aaaa", _triage(["DW-1"], [_decision("DW-1")]))
    bad = _make_run(project, "20260102-000000-bbbb", _triage([], []))
    fault_read_text(monkeypatch, bad / "triage.json")

    assert [d.id for d in decisions.pending_missed_decisions(project.project)] == ["DW-1"]


def test_pending_missed_decisions_skips_a_triage_with_a_nested_null_container(project):
    """DW-155/DW-158 at this reader. A cached triage that decodes and parses
    cleanly could still take the whole listing down: `validate_triage` iterated
    `bundles`/`decisions` and their members unscreened, so a `null` member raised
    `AttributeError` -- a fault no arm of the `except` above catches, past
    `cmd_decisions` and `cmd_status` (which catch `BmadConfigError` alone) and
    past the TUI (which catches `(BmadConfigError, OSError)`). Degradation is per
    FILE, like DW-145's: the newest run is skipped and the older run's DW-1 still
    surfaces.
    ABLATION TARGET IS THE VALIDATOR: drop the `_plan_mapping` call in
    `validate_triage`'s `bundles` loop and this raises `AttributeError` rather
    than returning ["DW-1"]. The `isinstance(rj, dict)` boundary guard added here
    does not cover this row -- a nested fault is inside an object document."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run(project, "20260101-000000-aaaa", _triage(["DW-1"], [_decision("DW-1")]))
    bad = _triage([], [])
    bad["bundles"] = [None]
    _make_run(project, "20260102-000000-bbbb", bad)

    assert [d.id for d in decisions.pending_missed_decisions(project.project)] == ["DW-1"]


def test_pending_missed_decisions_skips_a_non_object_triage_document(project):
    """The document-level twin: a cached `triage*.json` whose top level is not an
    object at all. `json.loads` returns `Any`, and `validate_triage`'s parameter
    is `dict[str, Any] | None`, so this call site was handing an unchecked shape
    across a typed boundary -- and `rj = rj or {}` substituted only on a FALSY
    document, so a non-empty list reached `.get` and raised `AttributeError`.
    Two independent checks now stand between that and this reader, and BOTH have
    to be removed to reproduce the traceback: the validator's `isinstance(rj,
    dict)` refusal and this loop's boundary `continue`, which matches the parity
    `load_pre_answers` and `_ensure_triage`'s cache-reload branch already have."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    _make_run(project, "20260101-000000-aaaa", _triage(["DW-1"], [_decision("DW-1")]))
    bad = _make_run(project, "20260102-000000-bbbb", _triage([], []))
    (bad / "triage.json").write_text(json.dumps(["nope"]), encoding="utf-8")

    assert [d.id for d in decisions.pending_missed_decisions(project.project)] == ["DW-1"]


def test_pending_missed_decisions_skips_a_triage_the_stricter_validation_refuses(project):
    """The DW-148 blast radius on THIS reader, pinned rather than discovered. A
    cached triage that decodes and parses cleanly can still fail the new
    type check, and a plan of `None` hits the same `continue` as a truncated file:
    the run contributes nothing, `_errors` is discarded unread at this call site
    (there is no journal here — this is a read-only command surface), and the id
    drops out of `decisions --list`, `status` and the TUI. It is not lost: the
    ledger entry is still open, so the NEXT sweep re-triages it and the fresh
    triage restores it. A pre-existing cache written before this release can hold
    such a value — `resolution: 5` was accepted as `str(5)` until now — so this is
    a real transition, not a hypothetical.

    The string control is what makes the refusal attributable: the two runs differ
    in that one byte, so `[]` cannot be passing for an unrelated reason."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    refused = _decision("DW-1")
    refused["options"][0]["resolution"] = 5  # not a string: refused from now on
    _make_run(project, "20260101-000000-aaaa", _triage(["DW-1"], [refused]))

    assert decisions.pending_missed_decisions(project.project) == []

    # Control: the identical triage with a string resolution still surfaces DW-1.
    accepted = _decision("DW-1")
    accepted["options"][0]["resolution"] = "5"
    _make_run(project, "20260102-000000-bbbb", _triage(["DW-1"], [accepted]))

    assert [d.id for d in decisions.pending_missed_decisions(project.project)] == ["DW-1"]


@pytest.mark.parametrize("field", ["question", "key"])
def test_pending_missed_decisions_skips_a_triage_with_a_scalar_predating_dw_156(project, field):
    """DW-156 refuses old caches with a non-string question or option key.
    Degradation is per file: even valid DW-3 in that cache disappears, while
    DW-2 in an older valid file still lists. All ledger entries stay open.

    Ablation: restore str(...) coercion for the selected field in validate_triage;
    this fails with DW-1 and DW-3 also present. Restoring the string value in the
    same cache is a positive control for the whole-file refusal.
    """
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open", "DW-2": "open", "DW-3": "open"})
    _make_run(project, "20260101-000000-aaaa", _triage(["DW-2"], [_decision("DW-2")]))
    stale = _decision("DW-1")
    target = stale if field == "question" else stale["options"][0]
    value = ["a", "b"] if field == "question" else 1
    target[field] = value
    cached = _triage(["DW-1", "DW-3"], [stale, _decision("DW-3")])
    run = _make_run(project, "20260102-000000-bbbb", cached)

    assert [d.id for d in decisions.pending_missed_decisions(project.project)] == ["DW-2"]

    target[field] = str(value)
    (run / "triage.json").write_text(json.dumps(cached), encoding="utf-8")
    assert {d.id for d in decisions.pending_missed_decisions(project.project)} == {
        "DW-1",
        "DW-2",
        "DW-3",
    }


def test_pending_missed_decisions_empty_when_nothing_open(project):
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "done 2026-06-01"})
    _make_run(project, "20260101-000000-aaaa", _triage([], []))
    assert decisions.pending_missed_decisions(project.project) == []


# ------------------------------------------------------- apply


@pytest.mark.parametrize("effect", ["build", "keep-open"])
def test_apply_pre_answer_build_records_store_and_ledger(project, effect):
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Answer", effect=effect, intent="widen field")
    d = Decision(id="DW-1", question="build it?", context="", options=(opt,), recommendation="1")
    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    # The happy path is the row that says the two REPORT axes stay empty when both
    # operands publish: a per-operand commit loop that reported its own successes
    # as faults would still satisfy every git assertion below.
    assert result.refusals == ()
    assert result.failures == ()
    entries = {
        e.id: e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    }
    assert "decision: 2026-06-13 Answer — widen field" in entries["DW-1"].body
    assert entries["DW-1"].open
    assert decisions.load_pre_answers(project.project)["DW-1"]["effect"] == effect
    # Ablation: include the store only for build; keep-open then has no HEAD answer.
    assert (
        json.loads(_git(project, "show", "HEAD:.bmad-loop/decisions.json"))["DW-1"]["effect"]
        == effect
    )
    assert "chore(decisions): pre-answer DW-1" in _git_log(project)
    # Both written operands are published, and since DW-225/226 each rides its OWN
    # commit: one commit per operand is what keeps a gitignored or out-of-tree
    # operand from sinking its sibling, so a `build` under one project produces TWO
    # commits carrying the SAME message and exactly one file each. Asserting the
    # pathspecs, not merely that a commit exists: gate one builds the operand list
    # from what this call wrote, and a recorded `build` wrote both.
    # Ablation: delete `if recorded:` from that gate (never publish the ledger) and
    # this reds here while every assertion above still passes. Ablation for the
    # per-operand shape: restore the single `commit_paths(project, ..., operands)`
    # call and the two-commit assertion reds on a single commit carrying both.
    heads = [
        _git(project, "show", "--name-only", "--format=%s", h).split("\n", 1)
        for h in ("HEAD", "HEAD~1")
    ]
    assert [h[0] for h in heads] == ["chore(decisions): pre-answer DW-1"] * 2
    published = [h[1].split() for h in heads]
    assert all(len(names) == 1 for names in published)
    assert sorted(n for names in published for n in names) == sorted(
        [
            project.deferred_work.relative_to(project.project).as_posix(),
            ".bmad-loop/decisions.json",
        ]
    )


def test_apply_pre_answer_sanitizes_a_multiline_detail(project):
    """The human-decision writer path (`decisions.py:146-150`), called bare: it
    must sanitize rather than raise. Pins `detail = option.resolution or
    option.intent` on its fallback branch — the resolution is empty here, so an
    option `intent` is what reaches the ledger."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    from bmad_loop.sweep import Decision

    opt = DecisionOption(
        key="1", label="Build\ncap", effect="build", intent="widen the field.\nThen backfill."
    )
    d = Decision(id="DW-1", question="build it?", context="", options=(opt,), recommendation="1")

    decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    text = project.deferred_work.read_text(encoding="utf-8")
    entries = {e.id: e for e in deferredwork.parse_ledger(text)}
    assert set(entries) == {"DW-1"}  # no phantom entry minted
    assert (
        "decision: 2026-06-13 Build cap — widen the field. Then backfill." in entries["DW-1"].body
    )
    assert len([line for line in text.splitlines() if line.startswith("decision:")]) == 1
    assert entries["DW-1"].open  # build stays open until a sweep builds it


def test_apply_pre_answer_raises_on_a_bad_date_leaving_nothing_written(project):
    """The documented precondition, and that it fires before *any* of the four
    side effects `apply_pre_answer` chains: `append_decision`, `mark_done`,
    `record_pre_answer` and `commit_paths`. A raise partway through would leave a
    ledger annotation with no store entry, or either with no commit.

    Both callers catch it — the TUI degrades to a per-decision notification and
    `bmad-loop decisions` to an error line — so the failure a human sees must
    correspond to nothing having happened."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Build", effect="build", intent="do it")
    d = Decision(id="DW-1", question="?", context="", options=(opt,), recommendation="1")
    ledger_before = project.deferred_work.read_text(encoding="utf-8")
    store_before = decisions.load_pre_answers(project.project)

    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        decisions.apply_pre_answer(project.project, d, opt, date="13/06/2026")

    assert project.deferred_work.read_text(encoding="utf-8") == ledger_before
    assert decisions.load_pre_answers(project.project) == store_before
    assert not decisions.store_path(project.project).exists()


def test_apply_pre_answer_close_marks_done_no_store(project):
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Close", effect="close", resolution="superseded")
    d = Decision(id="DW-1", question="close?", context="", options=(opt,), recommendation="1")
    decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    entries = {
        e.id: e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    }
    assert entries["DW-1"].status.startswith("done")
    assert "closed by human decision: superseded" in entries["DW-1"].body
    assert decisions.load_pre_answers(project.project) == {}  # close needs no carry-forward


def test_apply_pre_answer_returns_true_when_the_entry_is_there(project):
    """The positive half of the boolean contract (DW-198), pinned on its own so
    the two False rows below are not the only thing holding the return value —
    a function that returned False unconditionally would satisfy them both."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Close", effect="close", resolution="superseded")
    d = Decision(id="DW-1", question="close?", context="", options=(opt,), recommendation="1")

    assert decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13").recorded is True


def test_apply_pre_answer_returns_false_when_the_entry_was_retired(project):
    """`record_decision`'s first non-write state, propagated (DW-198): the ledger
    reads fine and simply carries no entry for this id — a rival writer retired it
    while the prompt blocked on the human. No raise, so both callers used to read
    the non-exception as a successful close.

    Ablation: hardcode `recorded=True` on the `PreAnswerResult` `apply_pre_answer`
    returns and this reddens."""
    install_bmad_config(project)
    write_ledger(project, {"DW-2": "open"})  # a ledger, but not this id
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Close", effect="close", resolution="superseded")
    d = Decision(id="DW-1", question="close?", context="", options=(opt,), recommendation="1")

    assert decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13").recorded is False
    # ...and nothing was minted for the id the ledger does not carry
    text = project.deferred_work.read_text(encoding="utf-8")
    assert {e.id for e in deferredwork.parse_ledger(text)} == {"DW-2"}
    assert "decision:" not in text


def test_apply_pre_answer_returns_false_when_the_ledger_file_is_gone(project):
    """The second non-write state, which `record_decision` answers BEFORE any read
    (`if not path.is_file()`). That is why False may not be read as a read fault:
    this call never opened the file at all.

    Ablation: hardcode `recorded=True` on the returned `PreAnswerResult` and this
    reddens on the return value. The negative assertion below is the weaker half and needs
    its own — have `record_decision` create the ledger it cannot find (write the
    entry into a fresh file rather than returning False) and it reddens there, where
    the return-value assertion alone would still pass."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    project.deferred_work.unlink()
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Close", effect="close", resolution="superseded")
    d = Decision(id="DW-1", question="close?", context="", options=(opt,), recommendation="1")

    assert decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13").recorded is False
    assert not project.deferred_work.exists()  # no ledger conjured to write into


def test_apply_pre_answer_build_non_write_still_saves_the_store_answer(project):
    """False withholds nothing (DW-198). The ledger line is what did not land; the
    pre-answer store write runs regardless, so the saved `build` answer remains
    structurally usable. This asserts persistence, not future sweep execution."""
    install_bmad_config(project)
    write_ledger(project, {"DW-2": "open"})  # no DW-1 entry to record against
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Widen", effect="build", intent="widen field")
    d = Decision(id="DW-1", question="build it?", context="", options=(opt,), recommendation="1")

    assert decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13").recorded is False
    stored = decisions.load_pre_answers(project.project)["DW-1"]
    assert stored["effect"] == "build"
    assert decisions.unusable_answer_reason(stored, allow_close=False) is None


def test_apply_pre_answer_commit_leaves_unrelated_changes(project):
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    (project.project / "src.txt").write_text("user edit, uncommitted\n")  # unrelated work
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Close", effect="close", resolution="x")
    d = Decision(id="DW-1", question="?", context="", options=(opt,), recommendation="1")
    decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")
    # the unrelated change is still uncommitted (commit_paths staged only the ledger)
    assert "src.txt" in _git_status(project)


# ------------------------------- the commit publishes only what THIS call wrote (DW-209/213)


def _close_decision():
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Close", effect="close", resolution="superseded")
    return (
        Decision(id="DW-1", question="close?", context="", options=(opt,), recommendation="1"),
        opt,
    )


def test_apply_pre_answer_never_commits_away_a_ledger_that_vanished(project):
    """THE reproduced hazard (DW-213). The ledger is TRACKED at HEAD and is unlinked
    while the prompter blocks on the human. `record_decision` answers False without
    raising, and the commit block used to run anyway over `[ledger, store]` —
    `verify.commit_paths` deliberately keeps a missing-but-TRACKED path as a
    DELETION to stage, so the call published the ledger's own REMOVAL under a
    `chore(decisions): pre-answer DW-1` message, taking every `decision:` line and
    open entry out of HEAD.

    The claim is what does NOT happen: no new commit, and HEAD still carries the
    pre-call bytes verbatim.

    Ablation: restore the unconditional `[ledger, store_path(project)]` operand
    list and this reds on all three assertions — a new commit exists, its message
    is the pre-answer one, and `git show HEAD:<ledger>` raises because the path is
    gone at HEAD."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    rel = project.deferred_work.relative_to(project.project).as_posix()
    head_before = _git(project, "rev-parse", "HEAD").strip()
    blob_before = _git(project, "show", f"HEAD:{rel}")
    project.deferred_work.unlink()
    d, opt = _close_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is False
    # Nothing was WRITTEN, so nothing was refused — and nothing reached git to fail
    # either: an empty operand list spawns no commit at all.
    assert result.refusals == ()
    assert result.failures == ()
    assert _git(project, "rev-parse", "HEAD").strip() == head_before
    assert _git(project, "show", f"HEAD:{rel}") == blob_before
    assert "chore(decisions): pre-answer" not in _git_log(project)


@pytest.mark.parametrize("ledger_missing", [False, True], ids=["retired-entry", "absent-ledger"])
def test_apply_pre_answer_close_non_write_spawns_no_git_at_all(
    project, monkeypatch, ledger_missing
):
    """DW-185's rule at this caller: a phase that wrote nothing runs no git. A
    `close` whose ledger is gone writes neither operand (a `close` records no store
    entry), so the operand list is empty and the commit is skipped outright rather
    than reaching git and finding nothing to do.

    Graded by making both git helpers RAISE rather than recording their calls: a
    recording stub would let a regression pass whenever the call happened to be
    harmless, where a raise cannot be ignored by any arm.

    Ablation: always include the ledger operand. The present dirty retired-entry
    case reaches git and raises, independently of the target guard."""
    install_bmad_config(project)
    write_ledger(project, {"DW-2": "open"})
    if ledger_missing:
        project.deferred_work.unlink()
    else:
        with project.deferred_work.open("a", encoding="utf-8") as fh:
            fh.write("\nan unrelated in-flight edit\n")

    def never(*_a, **_k):
        raise AssertionError("a non-write reached git")

    monkeypatch.setattr(decisions.verify, "path_clean", never)
    monkeypatch.setattr(decisions.verify, "commit_paths", never)
    d, opt = _close_decision()

    assert decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13").recorded is False


@pytest.mark.parametrize("effect", ["build", "keep-open"])
@pytest.mark.parametrize("ledger_missing", [False, True], ids=["retired-entry", "absent-ledger"])
def test_apply_pre_answer_build_non_write_publishes_the_store_alone(
    project, effect, ledger_missing
):
    """The operand list is per-OPERAND, not all-or-nothing: a non-close whose ledger
    entry is gone still WROTE the pre-answer store, so the store publishes and the
    ledger — which this call did not write — is absent from the commit's pathspec.

    The tracked ledger is dirty or absent, so an over-broad operand list would
    publish an unrelated edit or deletion instead of silently seeing clean bytes.

    Ablation: restore the unconditional `[ledger, store_path(project)]` list and
    this reds — the retired ledger's unrelated edit appears in the commit. Gate
    two independently protects absence; the empty refusal assertion catches an
    unwritten ledger reaching that gate. Restricting the store to build also reds
    the keep-open cases because their HEAD answer never lands."""
    install_bmad_config(project)
    write_ledger(project, {"DW-2": "open"})  # no DW-1 entry to record against
    ledger_rel = project.deferred_work.relative_to(project.project).as_posix()
    ledger_head = _git(project, "show", f"HEAD:{ledger_rel}")
    if ledger_missing:
        project.deferred_work.unlink()
    else:
        with project.deferred_work.open("a", encoding="utf-8") as fh:
            fh.write("\nan unrelated in-flight edit\n")  # dirty, and not ours to publish
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Answer", effect=effect, intent="widen field")
    d = Decision(id="DW-1", question="build it?", context="", options=(opt,), recommendation="1")

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is False
    assert result.refusals == ()
    assert "chore(decisions): pre-answer DW-1" in _git_log(project)
    published = _git(project, "show", "--name-only", "--format=", "HEAD").split()
    assert published == [".bmad-loop/decisions.json"]
    assert (
        json.loads(_git(project, "show", "HEAD:.bmad-loop/decisions.json"))["DW-1"]["effect"]
        == effect
    )
    assert _git(project, "show", f"HEAD:{ledger_rel}") == ledger_head
    assert ledger_rel in _git_status(project)  # the ledger's own edit stays with its owner


def test_apply_pre_answer_recorded_close_still_publishes_the_ledger(project):
    """The over-gating guard for the row above: gating on "did this call write it"
    must not stop the ordinary `close` from publishing. The ledger IS what this
    call wrote, so it is the commit's sole operand — and the store, which a `close`
    never writes, is not.

    Ablation: always include the store operand; its unrelated dirty answer reaches
    HEAD and the pathspec and preserved-store assertions fail."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    ledger_rel = project.deferred_work.relative_to(project.project).as_posix()
    decisions.record_pre_answer(project.project, "DW-9", _OPT, date="2026-06-13")
    _git(project, "add", ".bmad-loop/decisions.json")
    _git(project, "commit", "-m", "seed pre-answer store")
    store_head = _git(project, "show", "HEAD:.bmad-loop/decisions.json")
    decisions.record_pre_answer(project.project, "DW-8", _OPT, date="2026-06-13")
    store_dirty = decisions.store_path(project.project).read_bytes()
    d, opt = _close_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    assert result.publish_note() is None
    assert "chore(decisions): pre-answer DW-1" in _git_log(project)
    published = _git(project, "show", "--name-only", "--format=", "HEAD").split()
    assert published == [ledger_rel]
    assert _git(project, "show", "HEAD:.bmad-loop/decisions.json") == store_head
    assert decisions.store_path(project.project).read_bytes() == store_dirty
    assert ".bmad-loop/decisions.json" in _git_status(project)


def test_apply_pre_answer_refusal_drops_the_operand_and_rides_back_on_the_result(
    project, monkeypatch
):
    """The SECOND gate (DW-199/203/205's guard, shared with the sweep's nine
    publishers). It fires only on a race between the write above and the staging
    below — the first gate has already removed every operand this call did not
    write — which is why a refusal is worth reporting rather than noise: it means
    an answer that really WAS written could not be published.

    `decisions.py` has no journal, so the refusal rides back on the return value.
    It never raises, and a refused operand reaches no git at all.

    Ablation: delete the `verify.unpublishable_target` call from `apply_pre_answer`
    and this reds through the `AssertionError` the `commit_paths` stub raises."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})

    monkeypatch.setattr(
        decisions.verify, "unpublishable_target", lambda _t, _f: ("target-absent", None)
    )

    def never(*_a, **_k):
        raise AssertionError("a refused publication reached git")

    monkeypatch.setattr(decisions.verify, "path_clean", never)
    monkeypatch.setattr(decisions.verify, "commit_paths", never)
    d, opt = _close_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True  # the LINE landed; only the publish did not
    assert result.refusals == (
        decisions.PublishRefusal(file="deferred-work.md", cause="target-absent", error=None),
    )
    assert result.publish_note() == "not committed to git: deferred-work.md (target-absent)"
    # ...and the ledger really does still carry the line nothing published
    text = project.deferred_work.read_text(encoding="utf-8")
    assert "decision: 2026-06-13 Close — superseded" in text


@pytest.mark.parametrize("refused_family", ["ledger", "store"])
def test_apply_pre_answer_publishes_the_survivor_when_one_operand_is_refused(
    project, monkeypatch, refused_family
):
    """A refusal drops ITS operand, not the commit. A build writes two operands:
    refuse either one and the other must reach HEAD alone, while the refused
    write stays dirty and the result names it.

    Ablation: clear `operands` in the refusal arm. The store-refused case loses
    the ledger already accepted, and fails its HEAD assertion."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})

    ledger_rel = project.deferred_work.relative_to(project.project).as_posix()
    ledger_head = _git(project, "show", f"HEAD:{ledger_rel}")
    monkeypatch.setattr(
        decisions.verify,
        "unpublishable_target",
        lambda _t, family: ("target-absent", None) if family == refused_family else None,
    )
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Widen", effect="build", intent="widen field")
    d = Decision(id="DW-1", question="build it?", context="", options=(opt,), recommendation="1")

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    assert result.refusals == (
        decisions.PublishRefusal(
            file="deferred-work.md" if refused_family == "ledger" else "decisions.json",
            cause="target-absent",
            error=None,
        ),
    )
    assert "chore(decisions): pre-answer DW-1" in _git_log(project)
    published = _git(project, "show", "--name-only", "--format=", "HEAD").split()
    if refused_family == "ledger":
        assert published == [".bmad-loop/decisions.json"]
        assert (
            json.loads(_git(project, "show", "HEAD:.bmad-loop/decisions.json"))["DW-1"]["effect"]
            == "build"
        )
        assert _git(project, "show", f"HEAD:{ledger_rel}") == ledger_head
        assert ledger_rel in _git_status(project)
    else:
        assert published == [ledger_rel]
        assert "decision: 2026-06-13 Widen — widen field" in _git(
            project, "show", f"HEAD:{ledger_rel}"
        )
        assert decisions.load_pre_answers(project.project)["DW-1"]["effect"] == "build"
        assert "?? .bmad-loop/decisions.json" in _git(
            project, "status", "--porcelain", "--untracked-files=all"
        )


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("fault", ["directory", "metadata"])
def test_apply_pre_answer_refuses_a_store_a_directory_replaced_while_publishing_the_ledger(
    project, monkeypatch, tracked, fault
):
    """DW-211/228 at the OUT-OF-BAND publisher, through the real guard rather than a
    stubbed one. The store is replaced by a DIRECTORY between the write and the
    staging — exactly the race this second gate exists for, since the wrote-it gate
    upstream already proved the write happened — and the refusal must drop only its
    own operand: the ledger still commits, the directory's descendants never reach
    HEAD, and nothing raises out of a call whose on-disk record is already made.

    The token matters as much as the refusal. `target-absent` would send an operator
    looking for a vanished file, `target-unreadable` for a permission fault and
    `target-undecodable` (the ledger family only) for a decode fault; the repair
    here is "something is sitting at the store's name", which is another thing.

    Ablation: revert the store leg to its existence-only probe and this reds two
    ways — no refusal is reported, and `git add` stages `swept-in.txt` into the
    `chore(decisions): pre-answer` commit. Remove the store probe exception
    handler and metadata rows raise instead of returning a refusal. Tracked rows
    also pin preservation of the original store blob in HEAD."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    ledger_rel = project.deferred_work.relative_to(project.project).as_posix()
    store = decisions.store_path(project.project)
    if tracked:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("{}\n", encoding="utf-8")
        _git(project, "add", "--", ".bmad-loop/decisions.json")
        _git(project, "commit", "-m", "seed tracked store")
        store_head = _git(project, "show", "HEAD:.bmad-loop/decisions.json")
    real_record = decisions.record_pre_answer

    def record_then_replace(*a, **kw):
        # The write really lands, and only THEN is the target replaced — the guard
        # is the second gate, and the first one is already satisfied.
        real_record(*a, **kw)
        store = decisions.store_path(project.project)
        if fault == "metadata":
            # `lstat`, the one probe the store leg takes since DW-257 — on
            # `is_file` the injection would never fire and the row would silently
            # stop faulting.
            fault_metadata_probe(monkeypatch, store.resolve(), "lstat")
        else:
            store.unlink()
            store.mkdir()
            (store / "swept-in.txt").write_text("an unrelated tree\n", encoding="utf-8")

    monkeypatch.setattr(decisions, "record_pre_answer", record_then_replace)
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Widen", effect="build", intent="widen field")
    d = Decision(id="DW-1", question="build it?", context="", options=(opt,), recommendation="1")

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    assert len(result.refusals) == 1
    refusal = result.refusals[0]
    assert refusal.file == "decisions.json"
    if fault == "metadata":
        assert refusal.cause == "target-unreadable"
        assert "Permission denied" in refusal.error
    else:
        assert refusal.cause == "target-not-a-file"
        assert refusal.error is None
        assert result.publish_note() == "not committed to git: decisions.json (target-not-a-file)"
    # the survivor still publishes, alone
    assert "chore(decisions): pre-answer DW-1" in _git_log(project)
    assert _git(project, "show", "--name-only", "--format=", "HEAD").split() == [ledger_rel]
    assert "decision: 2026-06-13 Widen — widen field" in _git(project, "show", f"HEAD:{ledger_rel}")
    assert "swept-in.txt" not in _git(project, "ls-files")
    if tracked:
        assert _git(project, "show", "HEAD:.bmad-loop/decisions.json") == store_head


@pytest.mark.parametrize(
    "symlinked",
    [
        False,
        pytest.param(
            True, marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
        ),
    ],
)
def test_apply_pre_answer_reports_a_git_fault_without_refusing_or_raising(
    project, monkeypatch, symlinked
):
    """The publication degrade, unchanged by the two gates above it in everything
    but its SILENCE: git publication is best effort, so a `GitError` — a non-git
    tree, a locked index, git absent — leaves the on-disk record standing and
    never reaches the caller as a raise. It is still NOT a refusal (nothing
    declined to publish; the publish itself failed), which is why it takes its own
    `PublishFailure` type rather than a fourth token on the closed `cause` union
    `verify.unpublishable_target` shares.

    What DW-226 changes is that both surfaces now hear about it: this used to
    answer `publish_note() is None`, so an answer written to disk and absent from
    git history was reported by neither the CLI nor the TUI.

    The multi-line error is deliberate — git's stderr is — and the note is
    rendered on ONE line at both surfaces, so the text is whitespace-collapsed.

    Ablation: remove `except verify.GitError` from `apply_pre_answer` and this
    reds where the call raises out of the fixture; keep the catch but drop the
    `failures.append` and it reds on the note while the no-raise assertions pass;
    drop the `" ".join(str(e).split())` collapse and it reds on the newline."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})

    if symlinked:
        target = project.deferred_work.with_name("operator-ledger.md")
        project.deferred_work.replace(target)
        try:
            project.deferred_work.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable on this host: {exc}")
        assert project.deferred_work.name != project.deferred_work.resolve().name
        # Ablation: use target.name for PublishFailure.file; this case fails.

    def boom(*_a, **_k):
        raise decisions.verify.GitError("git is unusable here:\n  fatal: nope\n")

    monkeypatch.setattr(decisions.verify, "commit_paths", boom)
    d, opt = _close_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    assert result.refusals == ()  # a failed publish is not a refused one
    [failure] = result.failures
    assert failure.file == "deferred-work.md"
    assert failure.error == "git is unusable here: fatal: nope"
    assert result.publish_note() == (
        "not committed to git: deferred-work.md "
        "(commit-unavailable: git is unusable here: fatal: nope)"
    )
    # ...and the write the commit could not publish is still on disk
    entries = {
        e.id: e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    }
    assert entries["DW-1"].status.startswith("done")
    assert "chore(decisions): pre-answer" not in _git_log(project)


def test_apply_pre_answer_folds_a_resolve_fault_into_target_unreadable(project, monkeypatch):
    """`Path.resolve` can raise `OSError` (a broken chain, a permission-denied
    component) or `RuntimeError` (a symlink loop on 3.11-3.12). `decisions.py` has
    no journal to route that to and the cause enum is closed by contract, so it
    takes the refusal arm as `target-unreadable`: a target whose path cannot be
    resolved cannot be read well enough to publish. It never escapes.

    Injected AFTER the ledger write rather than before it, which is both the shape
    the guard is about (a fault arising in the window between the write and the
    staging) and the only way to reach the publisher at all: the write's own
    cross-process lock resolves the same path to derive its sidecar, so a fault
    standing before the call aborts it long before any operand is built.

    Ablation: drop the `try` around the resolve and this reds with the injected
    error propagating out of `apply_pre_answer`."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    real_record = decisions.deferredwork.record_decision

    def record_then_break(*a, **kw):
        out = real_record(*a, **kw)
        refuse_to_resolve(monkeypatch, project.deferred_work)
        return out

    monkeypatch.setattr(decisions.deferredwork, "record_decision", record_then_break)

    def never(*_a, **_k):
        raise AssertionError("an unresolvable operand reached git")

    monkeypatch.setattr(decisions.verify, "commit_paths", never)
    d, opt = _close_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    [refusal] = result.refusals
    assert refusal.file == "deferred-work.md"
    assert refusal.cause == "target-unreadable"
    assert refusal.error
    # ...and the fault reaches the surfaces, where a bare cause would read exactly
    # like a plain absence. Ablation: render `f"{r.file} ({r.cause})"` for every
    # refusal in `publish_note` and this reds while the assertions above pass.
    note = result.publish_note()
    assert note is not None and note.startswith("not committed to git: deferred-work.md (")
    assert f"target-unreadable: {refusal.error}" in note


@pytest.mark.parametrize("fault", NUL_PATH_RESOLVE_FAULTS)
def test_apply_pre_answer_folds_a_value_error_from_the_resolve_into_target_unreadable(
    project, monkeypatch, fault
):
    """The `ValueError` CLASS of GATE TWO's resolve `except` tuple, driven on its own
    (DW-275), in the shape of the row above.

    `Path.resolve()` raises `ValueError` for an embedded NUL and `UnicodeEncodeError`
    (a `ValueError` subclass) for a lone surrogate on CPython 3.11-3.14 POSIX. This
    publisher has no journal to route that to, so the refusal is the fault's only
    route out: `target-unreadable` with the fault's text, `recorded` True, git never
    entered, and the text on the note where a bare cause would read like a plain
    absence. Injected AFTER the ledger write through `refuse_to_resolve(...,
    error=)` for the reasons the row above states, and from
    `NUL_PATH_RESOLVE_FAULTS` so the row holds on every interpreter and platform.

    Ablation, per class: delete `ValueError` ALONE from GATE TWO's
    `except (OSError, RuntimeError, ValueError)` in `apply_pre_answer` and both rows
    red with the injected fault propagating out; the `OSError` row above stays
    green."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    real_record = decisions.deferredwork.record_decision

    def record_then_break(*a, **kw):
        out = real_record(*a, **kw)
        refuse_to_resolve(monkeypatch, project.deferred_work, error=fault)
        return out

    monkeypatch.setattr(decisions.deferredwork, "record_decision", record_then_break)

    def never(*_a, **_k):
        raise AssertionError("an unresolvable operand reached git")

    monkeypatch.setattr(decisions.verify, "commit_paths", never)
    d, opt = _close_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    assert result.refusals == (
        decisions.PublishRefusal(
            file="deferred-work.md", cause="target-unreadable", error=str(fault)
        ),
    )
    note = result.publish_note()
    assert note is not None and note.startswith("not committed to git: deferred-work.md (")
    assert f"target-unreadable: {fault}" in note


def test_apply_pre_answer_commit_false_writes_on_disk_and_refuses_nothing(project):
    """`commit=False` short-circuits ahead of both gates: no git, no refusals, and
    the on-disk writes are unchanged.

    Ablation: move the `if not commit:` return below the operand build and the
    refusal assertion still passes, so this row's weight is the git one — delete
    the early return entirely and the `chore(decisions):` assertion reds."""
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    d, opt = _close_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13", commit=False)

    assert result.recorded is True
    assert result.refusals == ()
    assert result.publish_note() is None
    assert "chore(decisions): pre-answer" not in _git_log(project)
    entries = {
        e.id: e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    }
    assert entries["DW-1"].status.startswith("done")


# ---------- per-operand rooting and the failed-publish report (DW-225/226)


def _seed_repo(project, root):
    """Copy this test's sandbox for an independent operand repository.

    Reuse the project fixture's initialized repository and git configuration;
    never initialize a second repository or mutate the session template.
    """
    shutil.copytree(project.project, root)
    return root


def _config_artifacts_at(project, artifacts):
    """`install_bmad_config` with `implementation_artifacts` spelled ABSOLUTE — the
    shape `bmadconfig._resolve` accepts verbatim and `ProjectPaths.rebased` leaves
    unmoved, and the one that puts the ledger outside the project tree."""
    cfg = project.project / "_bmad" / "bmm" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f"implementation_artifacts: '{artifacts.as_posix()}'\n"
        "planning_artifacts: '{project-root}/_bmad-output/planning-artifacts'\n",
        encoding="utf-8",
    )


def _open_ledger(path, *dw_ids):
    """`conftest.write_ledger`'s bytes for a ledger that is not under the project,
    so its own tree (or no tree at all) owns the commit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            ["# Deferred Work\n"]
            + [
                f"### {i}: item {i}\n\norigin: test, 2026-06-01\n"
                f"location: src.txt:1\nreason: test entry.\nstatus: open\n"
                for i in dw_ids
            ]
        ),
        encoding="utf-8",
    )


def _subject_and_files(out):
    """Split `git show --name-only --format=%s` into (subject, [files]); git puts a
    blank line between the two. Both are asserted: since DW-225/226 each operand
    rides its OWN commit, so the file COUNT per commit is part of the claim."""
    subject, _, names = out.partition("\n")
    return subject, names.split()


def _build_decision(dw_id="DW-1"):
    from bmad_loop.sweep import Decision

    opt = DecisionOption(key="1", label="Widen", effect="build", intent="widen field")
    return (
        Decision(id=dw_id, question="build it?", context="", options=(opt,), recommendation="1"),
        opt,
    )


def test_apply_pre_answer_publishes_a_disjoint_ledger_in_its_own_repository(project, tmp_path):
    """DW-225, the reproduced silent drop. `implementation_artifacts` is an absolute
    path inside a git repository DISJOINT from the project — legal, since
    `bmadconfig._resolve` accepts any absolute path — so the project root is a tree
    the write never touched. `verify.commit_paths(project, ...)` relativized the
    ledger with `relative_to(repo_root)`, whose `ValueError` its `except ValueError:
    continue` swallows, leaving no operand: it returned `None` for "these paths held
    no change" and the `decision:` line reached no history at all, reported by
    neither surface.

    Premise before outcome: the two roots are compared RESOLVED and asserted
    disjoint, because "committed in the ledger's own tree" is only a claim while
    that tree is a different answer from the project.

    Ablation: restore the single `verify.commit_paths(project, message, operands)`
    call and this reds on the artifacts repo's HEAD — no `chore(decisions):` commit
    exists there — while `recorded`, `refusals` and `failures` all still pass,
    which is exactly the silence DW-225 names."""
    artifacts_repo = _seed_repo(project, tmp_path / "artifacts-repo")
    artifacts = artifacts_repo / "implementation-artifacts"
    ledger_rel = "implementation-artifacts/deferred-work.md"
    _config_artifacts_at(project, artifacts)
    _open_ledger(artifacts / "deferred-work.md", "DW-1")
    git(artifacts_repo, "add", "-A")
    git(artifacts_repo, "commit", "-q", "-m", "ledger")
    # premise: the ledger's enclosing repository really is DISJOINT from the project
    assert artifacts_repo.resolve() != project.project.resolve()
    assert artifacts_repo.resolve() in artifacts.resolve().parents
    assert project.project.resolve() not in artifacts.resolve().parents
    project_head = _git(project, "rev-parse", "HEAD").strip()
    d, opt = _close_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    assert result.refusals == ()
    assert result.failures == ()
    assert result.publish_note() is None  # nothing to tell either surface about
    # the `decision:` line is in the LEDGER repository's HEAD blob...
    assert "decision: 2026-06-13 Close — superseded" in git(
        artifacts_repo, "show", f"HEAD:{ledger_rel}"
    )
    assert "chore(decisions): pre-answer DW-1" in git(artifacts_repo, "log", "--oneline")
    assert git(artifacts_repo, "show", "--name-only", "--format=", "HEAD").split() == [ledger_rel]
    # ...and the project tree, which holds no part of this write, receives nothing
    assert _git(project, "rev-parse", "HEAD").strip() == project_head
    assert list(project.project.rglob("deferred-work.md")) == []


def test_apply_pre_answer_publishes_each_operand_in_its_own_tree(project, tmp_path):
    """DW-225 for a `build`, whose two operands live in two DIFFERENT trees: the
    ledger under a disjoint `implementation_artifacts`, the pre-answer store at
    `<project>/.bmad-loop/decisions.json` (a bare join off the project root that no
    config knob can move). No single repository can commit both, which is what makes
    per-operand rooting the only shape that publishes them.

    Ablation: restore the single `commit_paths(project, ...)` call and the project
    commits the store alone while the ledger is relativized away — this reds on the
    artifacts repo's commit count, silently, with `failures == ()` still true."""
    artifacts_repo = _seed_repo(project, tmp_path / "artifacts-repo")
    artifacts = artifacts_repo / "implementation-artifacts"
    ledger_rel = "implementation-artifacts/deferred-work.md"
    _config_artifacts_at(project, artifacts)
    _open_ledger(artifacts / "deferred-work.md", "DW-1")
    git(artifacts_repo, "add", "-A")
    git(artifacts_repo, "commit", "-q", "-m", "ledger")
    # premise: two trees, neither inside the other, each holding exactly one operand
    assert project.project.resolve() not in artifacts.resolve().parents
    assert artifacts_repo.resolve() not in project.project.resolve().parents
    before = {
        "project": int(_git(project, "rev-list", "--count", "HEAD")),
        "artifacts": int(git(artifacts_repo, "rev-list", "--count", "HEAD")),
    }
    d, opt = _build_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    assert result.refusals == ()
    assert result.failures == ()
    # exactly one new commit in each tree, each naming only its own file
    assert int(_git(project, "rev-list", "--count", "HEAD")) == before["project"] + 1
    assert int(git(artifacts_repo, "rev-list", "--count", "HEAD")) == before["artifacts"] + 1
    assert _subject_and_files(_git(project, "show", "--name-only", "--format=%s", "HEAD")) == (
        "chore(decisions): pre-answer DW-1",
        [".bmad-loop/decisions.json"],
    )
    assert _subject_and_files(
        git(artifacts_repo, "show", "--name-only", "--format=%s", "HEAD")
    ) == ("chore(decisions): pre-answer DW-1", [ledger_rel])
    # ...and neither tree received the other's file
    assert ledger_rel not in _git(project, "ls-files")
    assert "decisions.json" not in git(artifacts_repo, "ls-files")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_apply_pre_answer_commits_a_symlinked_ledger_in_its_target_repository(project, tmp_path):
    """The parent is RESOLVED, not lexical — the same claim
    `sweep._commit_ledger` carries for this file (DW-188), transplanted with the
    rooting itself.

    `platform_util.atomic_write_text` — the writer behind `record_decision` —
    follows symlinks, so a ledger symlinked out of the project has its TARGET
    rewritten. Rooting the commit at the LINK's own directory makes the write and
    the commit name two different files: `commit_paths` relativizes the resolved
    target against that lexical parent, hits `ValueError`, drops the only operand
    on its `except ValueError: continue`, and returns `None` for "nothing to
    commit" — the exact DW-225 silence, with `failures` empty because git never
    failed. Resolving is what makes the write and the commit name one file.

    Three claims, and the second and third are what stop a "fix" that merely
    widened the scope: the target repo's HEAD carries the `decision:` line, the
    link is still a LINK aimed at the same target (a publisher that replaced the
    name would satisfy the first claim while destroying the operator's
    indirection), and the project repo — which holds only the link — receives
    nothing.

    Premise before outcome (docs/testing.md): the indirection is real, the write
    went through it, the two repositories are compared RESOLVED and asserted
    DISJOINT rather than merely unequal, and both trees start clean.

    Ablation: change `target.parent` to `path.parent` in `apply_pre_answer` and
    this reds on the target repo's HEAD while `recorded`, `refusals`, `failures`
    and `publish_note()` all still pass — which is why the row is needed at all."""
    ledger_repo = _seed_repo(project, tmp_path / "ledger-repo")
    target = ledger_repo / "deferred-work.md"
    install_bmad_config(project)
    link = project.deferred_work
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    try:
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - host without symlink support
        pytest.skip(f"symlinks unavailable on this host: {exc}")
    write_ledger(project, {"DW-1": "open"}, commit=False)  # writes THROUGH the link
    for repo in (project.project, ledger_repo):
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "settle")

    # premise before outcome
    assert link.is_symlink() and link.readlink() == target  # the indirection is real
    assert target.is_file() and not target.is_symlink()  # ...and the write went through it
    project_resolved, ledger_resolved = project.project.resolve(), ledger_repo.resolve()
    assert project_resolved != ledger_resolved
    assert project_resolved not in ledger_resolved.parents  # DISJOINT, not merely unequal
    assert ledger_resolved not in project_resolved.parents
    assert git(project.project, "status", "--porcelain") == ""
    assert git(ledger_repo, "status", "--porcelain") == ""
    project_head = git(project.project, "rev-parse", "HEAD")
    d, opt = _close_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    assert result.refusals == ()
    assert result.failures == ()
    assert result.publish_note() is None
    # the commit is in the TARGET's repository, naming only that file
    assert _subject_and_files(git(ledger_repo, "show", "--name-only", "--format=%s", "HEAD")) == (
        "chore(decisions): pre-answer DW-1",
        ["deferred-work.md"],
    )
    assert "decision: 2026-06-13 Close — superseded" in git(
        ledger_repo, "show", "HEAD:deferred-work.md"
    )
    # ...the operator's indirection survives...
    assert link.is_symlink() and link.readlink() == target
    # ...and the project repo, which holds only the link, receives nothing
    assert git(project.project, "rev-parse", "HEAD") == project_head
    assert git(project.project, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "ignored_rel,ignored_name",
    [
        ("_bmad-output/implementation-artifacts/deferred-work.md", "deferred-work.md"),
        (".bmad-loop/decisions.json", "decisions.json"),
    ],
    ids=["ignored-ledger", "ignored-store"],
)
def test_apply_pre_answer_reports_a_gitignored_operand_without_sinking_its_sibling(
    project, ignored_rel, ignored_name
):
    """DW-226, the reproduced mutual sinking, in BOTH directions. The two operands
    are in ONE repository and one of them is GITIGNORED, so `commit_paths`'
    `_literal_specs` — which forces `:(literal)` pathspecs precisely so an ignored
    operand cannot be silently skipped — makes `git add` exit 1 for the pair. As a
    single call that `GitError` took the publishable operand down with the ignored
    one, and `except verify.GitError: pass` reported it at neither surface.

    Both directions, because they are not symmetric in the code. The operand list
    is built ledger-then-store, so `ignored-ledger` fails BEFORE its sibling
    commits while `ignored-store` fails AFTER one already has — and the second is
    DW-226's own trigger ("a project that gitignores `.bmad-loop/`"), where the
    ledger is the casualty. Only a per-operand loop with a per-operand `except`
    survives both: one commit that stops at the first fault passes the first row
    and reds the second.

    Ablation: restore the single `commit_paths(project, message, operands)` call and
    this reds twice per row — the sibling is absent from HEAD, and `failures` is
    empty; keep the loop but restore `except verify.GitError: pass` and only the
    `failures` and note assertions red; hoist the `try` outside the loop and
    `ignored-ledger` reds on the sibling's commit while `ignored-store` still
    passes."""
    install_bmad_config(project)
    ignore_before_commit(project, ignored_rel)
    write_ledger(project, {"DW-1": "open"})
    ledger_rel = project.deferred_work.relative_to(project.project).as_posix()
    published_rel = ledger_rel if ignored_name == "decisions.json" else ".bmad-loop/decisions.json"
    # premise: git really does consider THIS operand ignored, so `add` will refuse
    # it — and the sibling is not ignored, so the row grades a survivor and not a
    # second failure
    # (one call over BOTH paths: `check-ignore` exits 1 when nothing matched, and it
    # echoes only the paths that DID, so this pins the sibling as not-ignored too)
    assert _git(project, "check-ignore", ignored_rel, published_rel).split() == [ignored_rel]
    d, opt = _build_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    assert result.refusals == ()  # nothing declined; the publish itself failed
    [failure] = result.failures
    assert failure.file == ignored_name  # the LEXICAL basename, as a refusal uses
    assert "ignored" in failure.error
    assert "\n" not in failure.error  # collapsed: both surfaces print one line
    # ...and CLIPPED: git's ignored-path refusal is a multi-sentence hint block, so
    # this is the row where the cap is actually exercised.
    # Ablation: drop the clip in `apply_pre_answer` and this reds on real git output.
    assert len(failure.error) <= decisions.PUBLISH_ERROR_MAX
    note = result.publish_note()
    assert note is not None
    assert note.startswith(f"not committed to git: {ignored_name} (commit-unavailable: ")
    # the publishable sibling still reaches HEAD, alone
    assert _git(project, "show", "--name-only", "--format=", "HEAD").split() == [published_rel]
    assert "chore(decisions): pre-answer DW-1" in _git_log(project)
    # ...and the write git could not publish is intact on disk
    entries = {
        e.id: e
        for e in deferredwork.parse_ledger(project.deferred_work.read_text(encoding="utf-8"))
    }
    assert "decision: 2026-06-13 Widen — widen field" in entries["DW-1"].body
    assert decisions.load_pre_answers(project.project)["DW-1"]["effect"] == "build"


@pytest.mark.parametrize("refuse_ledger", [False, True], ids=["two-failures", "mixed-reports"])
def test_apply_pre_answer_reports_every_unpublished_operand(project, monkeypatch, refuse_ledger):
    """Exercise report accumulation through the publisher with real git failures.

    Ablation: retain only the first failure and the two-failures row fails.
    Dropping either report list fails the mixed-reports row.
    """
    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"}, commit=False)
    ledger_rel = project.deferred_work.relative_to(project.project).as_posix()
    store_rel = ".bmad-loop/decisions.json"
    ignore_before_commit(project, ledger_rel, store_rel)
    assert set(_git(project, "check-ignore", ledger_rel, store_rel).splitlines()) == {
        ledger_rel,
        store_rel,
    }
    real_guard = decisions.verify.unpublishable_target
    real_commit = decisions.verify.commit_paths
    attempts = []

    def guard(target, family):
        if refuse_ledger and target == project.deferred_work.resolve():
            return ("target-unreadable", "ledger probe unavailable")
        return real_guard(target, family)

    def commit(repo, message, paths):
        attempts.extend(paths)
        return real_commit(repo, message, paths)

    monkeypatch.setattr(decisions.verify, "unpublishable_target", guard)
    monkeypatch.setattr(decisions.verify, "commit_paths", commit)
    d, opt = _build_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    store = decisions.store_path(project.project).resolve()
    if refuse_ledger:
        assert attempts == [store]
        assert result.refusals == (
            decisions.PublishRefusal(
                "deferred-work.md", "target-unreadable", "ledger probe unavailable"
            ),
        )
        assert [failure.file for failure in result.failures] == ["decisions.json"]
    else:
        assert attempts == [project.deferred_work.resolve(), store]
        assert result.refusals == ()
        assert [failure.file for failure in result.failures] == [
            "deferred-work.md",
            "decisions.json",
        ]
    note = result.publish_note()
    assert note is not None
    assert note.index("deferred-work.md (") < note.index("decisions.json (commit-unavailable:")
    assert all("ignored" in failure.error for failure in result.failures)
    assert "decision: 2026-06-13 Widen — widen field" in project.deferred_work.read_text(
        encoding="utf-8"
    )
    assert decisions.load_pre_answers(project.project)["DW-1"]["effect"] == "build"


def test_publish_note_renders_refusals_and_failures_together_in_order():
    """Render both report types and preserve their order in one note.

    A single call can refuse one operand and fail to commit another. This row
    isolates formatting; producer-level rows cover how those reports accumulate.

    Ablation: drop either list from the join and its assertion fails; swap the
    lists and the ordering assertion fails.
    """
    note = decisions.PreAnswerResult(
        recorded=True,
        refusals=(decisions.PublishRefusal(file="deferred-work.md", cause="target-absent"),),
        failures=(decisions.PublishFailure(file="decisions.json", error="git is unusable"),),
    ).publish_note()
    assert note == (
        "not committed to git: deferred-work.md (target-absent), "
        "decisions.json (commit-unavailable: git is unusable)"
    )

    # Render both failures when the producer supplies two.
    both = decisions.PreAnswerResult(
        recorded=True,
        failures=(
            decisions.PublishFailure(file="deferred-work.md", error="no repo"),
            decisions.PublishFailure(file="decisions.json", error="ignored"),
        ),
    ).publish_note()
    assert both == (
        "not committed to git: deferred-work.md (commit-unavailable: no repo), "
        "decisions.json (commit-unavailable: ignored)"
    )
    # the empty result is still silent — the note is a report, not a status line
    assert decisions.PreAnswerResult(recorded=True).publish_note() is None


def test_apply_pre_answer_reports_an_operand_that_is_in_no_repository(project, tmp_path):
    """The third topology `sweep._commit_ledger` already carries (`_artifacts_in_no
    _repository`): `implementation_artifacts` outside every git tree, which
    `bmadconfig._resolve` also accepts. Nothing may raise, the on-disk write must
    survive, and — this is DW-226's half — the miss is announced rather than
    swallowed, carrying git's own message.

    Premise before outcome: asserted over EVERY ancestor, since one `.git` anywhere
    above would make this row grade a commit instead of the degrade.

    Ablation: remove the `except verify.GitError` and this reds with the raise
    escaping; keep it as a bare `pass` and it reds on `failures`."""
    artifacts = tmp_path / "loose-artifacts"
    artifacts.mkdir()
    _config_artifacts_at(project, artifacts)
    _open_ledger(artifacts / "deferred-work.md", "DW-1")
    # premise: no git repository encloses the ledger at any depth
    assert not any((p / ".git").exists() for p in (artifacts, *artifacts.parents))
    project_head = _git(project, "rev-parse", "HEAD").strip()
    d, opt = _close_decision()

    result = decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13")

    assert result.recorded is True
    assert result.refusals == ()
    [failure] = result.failures
    assert failure.file == "deferred-work.md"
    assert "not a git repository" in failure.error  # git's own message, not ours
    assert result.publish_note() == (
        f"not committed to git: deferred-work.md (commit-unavailable: {failure.error})"
    )
    # the on-disk write SURVIVES — the degrade is about the commit, never the file
    entries = {
        e.id: e
        for e in deferredwork.parse_ledger(
            (artifacts / "deferred-work.md").read_text(encoding="utf-8")
        )
    }
    assert entries["DW-1"].status.startswith("done")
    # ...and the project, which holds no part of this write, is untouched
    assert _git(project, "rev-parse", "HEAD").strip() == project_head


def _git(project, *args):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(project.project), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _git_log(project):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(project.project), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _git_status(project):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(project.project), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# ------------------------------------------ the store's confined write (#593, #597)


def _answer(project, dw_id="DW-7", date="2026-06-13"):
    decisions.record_pre_answer(
        project.project,
        dw_id,
        DecisionOption(key="1", label="Build it", effect="build", intent="do it"),
        date=date,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_store_write_refuses_a_symlinked_bmad_loop(project, tmp_path):
    """The escape #593 names, at this site. `follow_symlinks=False` refused a link
    planted at `decisions.json`; it never refused one at `.bmad-loop/`, and
    `_write_store`'s own `mkdir(parents=True, exist_ok=True)` ACCEPTS a
    symlink-to-a-directory, so the planted parent survives the setup step and both
    the temp and the published store land wherever the link points.

    A driven session can write under `.bmad-loop/`, which is what makes this a
    real writer rather than a hypothetical one: the escalation the no-follow was
    added to close costs a directory swap instead of a file swap.

    The second assertion is the one that pins the fix — refusing loudly is worth
    nothing if the write already landed outside the project.

    Ablation: revert `_write_store` to
    `atomic_write_text(path, ..., follow_symlinks=False)` and this fails
    `DID NOT RAISE`, with `decisions.json` sitting in `outside/`."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (project.project / ".bmad-loop").symlink_to(outside, target_is_directory=True)

    with pytest.raises(platform_util.UnconfinedWriteError):
        _answer(project)

    assert list(outside.iterdir()) == []  # nothing escaped the project


def test_the_store_write_lands_on_a_clean_tree(project):
    """The positive control for the refusal above. Without it that test passes for
    a `_write_store` wired to refuse everything, which is every reason a file
    could be absent from `outside/`."""
    _answer(project)

    assert decisions.load_pre_answers(project.project)["DW-7"]["effect"] == "build"
    assert decisions.store_path(project.project).is_file()


def test_the_store_write_refuses_a_readonly_store(project):
    """#597 at this site: the store is operator-curated — a human answers these
    decisions out of band — so a read-only one is answered with the
    `PermissionError` a bare `Path.write_text` raised, not routed around by a
    replace that only needs the DIRECTORY writable.

    chmod is on the per-test copytree copy the `project` fixture makes, never the
    session template (a read-only template would be inherited by every later
    copy), and it is restored in a `finally` because Windows rmtree refuses a
    READONLY file at cleanup.

    Ablation: drop `require_writable_target=True` from `_write_store` and this
    fails `DID NOT RAISE`, with the store rewritten and still reading 0444."""
    _answer(project, "DW-7")
    store = decisions.store_path(project.project)
    before = store.read_bytes()
    store.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            _answer(project, "DW-9", date="2026-06-14")
    finally:
        store.chmod(0o644)

    assert store.read_bytes() == before  # the second answer never landed


def test_a_removal_refuses_a_readonly_store_rather_than_skipping(project):
    """The REMOVAL half of the row above. Deleting a human-authored answer is a
    store write, never a repair, so an operator-locked store is answered with the
    `PermissionError` `_write_store` raises rather than treated as "already gone"
    — a silent skip would report the entry retired while it stayed on disk to be
    re-seeded and re-dropped by every later run.

    Load-bearing since DW-161 put the removal behind an advisory pre-lock probe:
    the probe decides only "is there anything to write", never whether the write
    can succeed, so the refusal has to come from under the hold and propagate out
    through it. A probe widened to swallow the write's fault, or one that answered
    `False` on an unwritable store, would turn this raise into exactly the silent
    skip the drop exists to prevent.

    chmod is on the per-test copy and restored in a `finally`, for the reasons the
    row above spells out.

    Ablation: make `drop_pre_answer` return `False` instead of writing when the
    store is not writable (or drop `require_writable_target=True` from
    `_write_store`) and this fails `DID NOT RAISE`, with the entry gone from a
    file still reading 0444."""
    _answer(project, "DW-7")
    _answer(project, "DW-9", date="2026-06-14")
    store = decisions.store_path(project.project)
    before = store.read_bytes()
    store.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            decisions.drop_pre_answer(project.project, "DW-7", answer=json.loads(before)["DW-7"])
    finally:
        store.chmod(0o644)

    assert store.read_bytes() == before  # the removal never landed
    assert set(decisions.load_pre_answers(project.project)) == {"DW-7", "DW-9"}


@pytest.mark.parametrize(
    ("effect", "label", "extra", "close_note"),
    [
        ("build", "Build", {"intent": "widen field"}, None),
        ("close", "Close", {"resolution": "superseded"}, "closed by human decision: superseded"),
    ],
)
def test_apply_pre_answer_is_one_ledger_transaction(
    project, tmp_path, monkeypatch, effect, label, extra, close_note
):
    """The decision record and the closure it asks for land in ONE locked
    read->edit->write, byte-identical to the released `append_decision` +
    `mark_done` pair (#286/#469).

    Two claims, and both are needed. The golden text says the collapse moved no
    bytes — these ledgers are committed and read by humans, so `record_decision`
    inserting the decision line before it applies the close is a contract, not a
    detail (`_MARK_DONE_TAIL_RE` anchors an undo marker on the status/resolution
    adjacency the other order would break). The acquisition count is what says
    the pair actually collapsed: as two calls it was two acquisitions with a
    window between them, and a rival writer landing there left the entry
    carrying a decision that says "close it" over a status that still says open.
    Byte equality alone passes just as well for the released pair.

    The BUILD variant acquires twice, and the second acquisition is a different
    file: `build`/`keep-open` also lands in the pre-answer store, and since
    DW-161 `record_pre_answer` runs its own read->edit->write under this same
    lock keyed on the STORE path. Two sequential holds on two files, never one
    nested inside the other — `ledger_lock`'s reentrancy guard is path-agnostic,
    so a nesting here would raise rather than pass. The CLOSE variant writes no
    store entry (the flip to done takes the id out of the open set now), so its
    count stays at one; the ORDER is asserted too, because the ledger's audit
    line is what the store entry is only a scheduling hint for, and recording the
    hint first would leave a window where a store answer points at a ledger entry
    carrying no decision.

    Ablation: restore the pair in `decisions.apply_pre_answer`. The golden assert
    still passes — that is the point — and the CLOSE variant's `acquisitions`
    goes to 2 on the LEDGER path, which is the one that grades the collapse. The
    build variant stays green on that count under that ablation and is known to:
    with `close_note=None` there is no second call to make, and `append_decision`
    is itself a one-acquisition delegate to `record_decision`, so the two
    spellings are the same transaction. It is kept for the claims it does decide
    — that the no-close path still writes the pair's bytes, leaves the entry
    open, and serializes its store write behind the ledger's.
    """

    from bmad_loop.sweep import Decision

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"}, commit=False)
    pristine = project.deferred_work.read_text(encoding="utf-8")
    opt = DecisionOption(key="1", label=label, effect=effect, **extra)
    d = Decision(id="DW-1", question="?", context="", options=(opt,), recommendation="1")

    # The released serial pair, run against a twin of the same pristine ledger in
    # its own directory so it contends on its own lock and is never counted.
    golden = tmp_path / f"golden-{effect}" / "deferred-work.md"
    golden.parent.mkdir(parents=True)
    golden.write_text(pristine, encoding="utf-8")
    deferredwork.append_decision(golden, "DW-1", "2026-06-13", label, opt.resolution or opt.intent)
    if close_note is not None:
        deferredwork.mark_done(golden, "DW-1", "2026-06-13", close_note)

    acquisitions = []
    real_lock = deferredwork.ledger_lock

    @contextlib.contextmanager
    def spy_lock(p):
        acquisitions.append(p)
        with real_lock(p):
            yield

    monkeypatch.setattr(deferredwork, "ledger_lock", spy_lock)
    decisions.apply_pre_answer(project.project, d, opt, date="2026-06-13", commit=False)

    assert project.deferred_work.read_text(encoding="utf-8") == golden.read_text(encoding="utf-8")
    # ONE hold per file written, ledger first, and never the same file twice
    expected = [project.deferred_work]
    if effect != "close":
        expected.append(decisions.store_path(project.project))
    assert acquisitions == expected


# ------------------------------------ the store's write discipline (DW-161)
#
# The store is the ledger's twin exposure: `bmad-loop decisions`, the TUI
# decision modal and a sweep all reach it, and every writer here read-modify-
# writes the WHOLE file. The rows below are the store-side siblings of
# tests/test_deferredwork.py's `test_scripted_interleave_loses_no_update`,
# `test_every_mutator_holds_the_ledger_lock` and
# `test_a_read_dependent_noop_takes_no_lock`, and they grade the same three
# claims: the hold spans read AND write, every writer takes it, and a call a
# read proves will write nothing takes nothing.

_OPT = DecisionOption(key="1", label="Build it", effect="build", intent="do it")


def _seed_store(project, ids):
    """Plant `ids` in the store through the real writer, before any spy exists."""
    for dw_id in ids:
        decisions.record_pre_answer(project.project, dw_id, _OPT, date="2026-06-13")
    return decisions.store_path(project.project)


# Each row is seeded to WRITE. A no-op row would grade nothing here: the advisory
# pre-lock probe (#736) answers a read-provable no-op above the acquisition these
# rows spy on — that inverse is `test_a_read_dependent_store_noop_takes_no_lock`.
_LOCKED_WRITERS = {
    "record_pre_answer": (
        lambda p: None,
        lambda p: decisions.record_pre_answer(p.project, "DW-7", _OPT, date="2026-06-13"),
    ),
    "prune_pre_answers": (
        lambda p: _seed_store(p, ["DW-7"]),
        lambda p: decisions.prune_pre_answers(p.project, {"DW-9"}),
    ),
    "drop_pre_answer": (
        lambda p: _seed_store(p, ["DW-7"]),
        lambda p: decisions.drop_pre_answer(
            p.project, "DW-7", answer=decisions.load_pre_answers(p.project)["DW-7"]
        ),
    ),
}

# The read-provable no-ops. `record_pre_answer` has none — it always publishes
# bytes — so it is absent by construction rather than by omission. `drop_pre_answer`
# has two: an absent id, and a present one the store no longer holds as the value
# being retired (a human's later replacement — the DW-143 provenance guard).
_STALE = {"key": "9", "label": "Old", "effect": "keep-open", "answered_at": "2026-01-01"}
_NOOP_WRITERS = {
    "prune_pre_answers": (
        lambda p: decisions.prune_pre_answers(p.project, {"DW-7"}),  # nothing to drop
        [],
    ),
    "drop_pre_answer:absent": (
        lambda p: decisions.drop_pre_answer(p.project, "DW-404", answer=_STALE),
        False,
    ),
    "drop_pre_answer:replaced": (
        lambda p: decisions.drop_pre_answer(p.project, "DW-7", answer=_STALE),
        False,
    ),
}


@pytest.mark.parametrize("name", sorted(_LOCKED_WRITERS))
def test_every_store_writer_holds_the_lock_on_the_store_path(project, monkeypatch, name):
    """Each of the three writers takes the lock exactly once, on the STORE's own
    path, and the hold really excludes.

    Three claims and each is needed. That the spy fired at all says the writer
    routes through `deferredwork.ledger_lock` rather than writing unserialized —
    before DW-161 none of them did. That it fired ONCE says the whole
    read->edit->write sits in a single acquisition rather than a per-step hold a
    rival can slip between. The path says it locked the file it is writing: the
    ledger's sidecar is keyed on the ledger, so locking that would serialize the
    store's writers against the wrong file and against nobody who matters. The
    probe from inside the critical section says the acquisition is a real OS lock
    and not a yielding stub, which would satisfy the count and exclude no one.

    Ablation: delete this writer's `with deferredwork.ledger_lock(path):` and
    dedent its body — `acquisitions` is empty and `held` stays empty, and the row
    reds."""
    import contextlib

    seed, call = _LOCKED_WRITERS[name]
    seed(project)
    store = decisions.store_path(project.project)
    real_lock = deferredwork.ledger_lock
    acquisitions, held = [], []

    @contextlib.contextmanager
    def spy_lock(p):
        acquisitions.append(p)
        with real_lock(p):
            try:
                with platform_util.file_lock(runs.lock_path_for(p), blocking=False):
                    held.append(False)
            except OSError:
                held.append(True)  # the sidecar cannot be taken: a real exclusion
            yield

    monkeypatch.setattr(deferredwork, "ledger_lock", spy_lock)

    call(project)

    assert acquisitions == [store]  # ONE, and on the store — never the ledger
    assert held == [True]


@pytest.mark.parametrize("name", sorted(_NOOP_WRITERS))
def test_a_read_dependent_store_noop_takes_no_lock(project, monkeypatch, name):
    """The exact inverse of the row above, over the writers that can no-op: a call
    ONE read proves will write nothing acquires nothing and leaves the file alone.

    Both readings of "the lock is load-bearing" have to hold or the fix has traded
    one failure for another. This is the direction that actually regressed users:
    `_prune_dropped_pre_answer` calls `drop_pre_answer` for ids that usually have
    no store entry and swallows nothing, so an unconditional hold would turn a
    silent `False` into a `StateRootError` (or a Windows acquisition timeout) on
    the ordinary path.

    Bytes AND mtime, not just the count. The probe reaches its answer through the
    same read the locked pass would fold, so a probe that reported "no write"
    where the authority WOULD have written shows up as changed bytes rather than
    as a count; and mtime catches the rewrite that re-serializes identical
    content, which is the store repair this codebase refuses.

    Ablation: delete this writer's advisory pre-lock probe — `acquisitions`
    counts one and the row reds."""
    import contextlib

    call, expected = _NOOP_WRITERS[name]
    store = _seed_store(project, ["DW-7"])
    before, before_mtime = store.read_bytes(), store.stat().st_mtime_ns
    real_lock = deferredwork.ledger_lock
    acquisitions = []

    @contextlib.contextmanager
    def spy_lock(p):
        acquisitions.append(p)
        with real_lock(p):
            yield

    monkeypatch.setattr(deferredwork, "ledger_lock", spy_lock)

    assert call(project) == expected

    assert acquisitions == []
    assert store.read_bytes() == before
    assert store.stat().st_mtime_ns == before_mtime


def test_a_scripted_interleave_loses_no_pre_answer(project, monkeypatch):
    """DW-161's lost-update scenario, made deterministic: a rival writer records a
    whole answer between writer A's call and A's acquisition, and A must still see
    it.

    Writer A records DW-1; writer B records DW-2. B is run to completion —
    acquire, read, write, release — immediately BEFORE A delegates to the real
    lock, which is the worst legal interleaving the lock permits. A therefore has
    to read the store B just wrote, not one it snapshotted earlier, or A's write
    reverts B's answer wholesale: the store is read-modify-written in full, so a
    lost update here is a human's answer silently gone, not a stale field.

    `record_pre_answer` is the writer under test because it is the one with no
    advisory probe — it always publishes, so its read is unconditionally inside
    the hold and the hoist below is a real ablation rather than a probe artifact.

    Ablation: hoist `record_pre_answer`'s `load_pre_answers` above its
    `with deferredwork.ledger_lock(path):` and write from it — A's read then
    happens before the spy fires, A publishes its stale snapshot, and DW-2 is gone
    from the final store."""
    import contextlib

    real_lock = deferredwork.ledger_lock
    rival_ran = []

    @contextlib.contextmanager
    def rival_first(p):
        if not rival_ran:
            rival_ran.append(True)  # once: B's own write re-enters this spy
            decisions.record_pre_answer(
                project.project,
                "DW-2",
                DecisionOption(key="2", label="Keep", effect="keep-open"),
                date="2026-06-14",
            )
        with real_lock(p):
            yield

    monkeypatch.setattr(deferredwork, "ledger_lock", rival_first)

    decisions.record_pre_answer(project.project, "DW-1", _OPT, date="2026-06-13")

    stored = decisions.load_pre_answers(project.project)
    assert set(stored) == {"DW-1", "DW-2"}  # B's answer survived A's write
    assert stored["DW-2"]["effect"] == "keep-open"  # ...whole, not a merged husk
    assert stored["DW-2"]["answered_at"] == "2026-06-14"
    assert stored["DW-1"]["effect"] == "build"  # ...and A's own answer landed


@pytest.mark.parametrize("name", sorted(_NOOP_WRITERS))
def test_a_store_noop_succeeds_when_no_state_root_is_derivable(project, monkeypatch, name):
    """Where no state root can be derived there is no sidecar to lock, and the
    read-provable no-ops still have to succeed — while a real write still fails
    loudly rather than proceeding unserialized.

    `runs.StateRootError` is raised while DERIVING the sidecar path, before any OS
    lock is attempted, so it reaches every caller of `ledger_lock`; it is not an
    `OSError`, so no caller's net catches it. Answering the no-op above the
    acquisition is what stops such an environment from failing calls that were
    never going to write.

    The write-shaped control is not decoration: it is what says the patch is live.
    Without it a `lock_path_for` stub that silently never fired would make the
    no-op rows above vacuously green.

    Ablation: delete either probe — that row raises `StateRootError` instead of
    returning, and reds."""
    call, expected = _NOOP_WRITERS[name]
    store = _seed_store(project, ["DW-7"])
    before = store.read_bytes()

    def no_state_root(_path, **_kwargs):
        raise runs.StateRootError("no state root in this environment")

    monkeypatch.setattr(runs, "lock_path_for", no_state_root)

    assert call(project) == expected
    assert store.read_bytes() == before

    with pytest.raises(runs.StateRootError):
        decisions.record_pre_answer(project.project, "DW-9", _OPT, date="2026-06-13")

    assert store.read_bytes() == before  # the real write raised rather than writing unlocked


@pytest.mark.parametrize("operation", ["stat", "read_text"])
def test_pre_answer_locked_read_fault_preserves_ledger_store_and_head(
    project, monkeypatch, operation
):
    """DW-279: a refused decision read reaches the grouped parent handlers.

    Neither the answer store nor git may publish after the authoritative read
    refuses. Removing the corresponding read wrap fails the named-class assert.
    """
    from conftest import fault_locked_ledger_read

    from bmad_loop.sweep import Decision

    install_bmad_config(project)
    write_ledger(project, {"DW-1": "open"})
    opt = DecisionOption(key="1", label="Build", effect="build", intent="fix it")
    decision = Decision(
        id="DW-1", question="build?", context="", options=(opt,), recommendation="1"
    )
    before = fault_locked_ledger_read(monkeypatch, project.deferred_work, operation)
    head = _git(project, "rev-parse", "HEAD")
    store = decisions.store_path(project.project)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"{}\n")  # bytes: write_text would CRLF on win32

    with pytest.raises(deferredwork.LedgerReadFault) as raised:
        decisions.apply_pre_answer(project.project, decision, opt, date="2026-09-12")

    assert isinstance(raised.value.__cause__, PermissionError)
    assert project.deferred_work.read_bytes() == before
    assert store.read_bytes() == b"{}\n"
    assert _git(project, "rev-parse", "HEAD") == head
