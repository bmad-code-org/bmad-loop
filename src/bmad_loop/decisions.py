"""Cross-run pre-answers for deferred-work decisions.

A sweep's triage can surface decisions only a human can make. An unattended
sweep (`--no-prompt`) skips them; an interactive sweep can be abandoned before
every prompt is answered. Either way the answer is otherwise lost: triage
re-derives the `decisions` partition from the open ledger on every run, and the
only record of an answer — the run-scoped `{run_dir}/decisions.json` — does not
carry across runs, so the next sweep re-surfaces (and re-skips) the same
decision.

This module is the durable carrier. A human answers missed decisions out of band
(`bmad-loop decisions`, or the TUI), the answer is recorded both as a ledger
`decision:` line and — for build/keep-open — in a project-level
`.bmad-loop/decisions.json` keyed by DW id, and the next sweep consumes it
instead of asking again (see SweepEngine._decisions_phase). `close` answers need
no store entry: they are applied to the ledger immediately (status -> done), so
the entry simply leaves the open set.

Layering note: this module sits above sweep.py (it reuses Decision/validate_triage
and the deterministic ledger helpers). sweep.py imports it lazily to avoid a cycle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import bmadconfig, deferredwork, runs, verify
from .sweep import Decision, DecisionOption, validate_triage

STORE_REL = Path(".bmad-loop") / "decisions.json"
_TRIAGE_RE = re.compile(r"^triage(?:-(\d+))?\.json$")


def store_path(project: Path) -> Path:
    return project / STORE_REL


# --------------------------------------------------------------- store I/O


def load_pre_answers(project: Path) -> dict[str, dict]:
    """The project-level pre-answer store, {DW-id: {effect,label,intent,...}}.
    Tolerant of a missing or malformed file (returns {})."""
    path = store_path(project)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(project: Path, data: dict) -> None:
    path = store_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def record_pre_answer(project: Path, dw_id: str, option: DecisionOption, *, date: str) -> None:
    """Persist a chosen option so a future sweep applies it without asking. The
    option's full semantics are stored (not just its key): a later triage may
    renumber options, so the sweep reads effect/intent from here directly."""
    data = load_pre_answers(project)
    data[dw_id] = {
        "key": option.key,
        "label": option.label,
        "effect": option.effect,
        "intent": option.intent,
        "resolution": option.resolution,
        "bundle_name": option.bundle_name,
        "answered_at": date,
    }
    _write_store(project, data)


def prune_pre_answers(project: Path, open_ids: set[str]) -> list[str]:
    """Drop store entries whose DW id is no longer open (built or closed). No-op
    write when nothing is dropped. Returns the dropped ids."""
    data = load_pre_answers(project)
    dropped = [k for k in data if k not in open_ids]
    if dropped:
        for k in dropped:
            del data[k]
        _write_store(project, data)
    return dropped


# ------------------------------------------------------- discovery + apply


def pending_missed_decisions(project: Path) -> list[Decision]:
    """Decisions earlier sweeps surfaced but no one answered: reconstructed from
    every run's persisted triage*.json, kept only when the DW id is still open
    and not already in the pre-answer store. The most recent triage's wording of
    each id wins. Sorted by DW number."""
    paths = bmadconfig.load_paths(project)
    ledger = paths.deferred_work
    text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
    open_now = deferredwork.open_ids(text)
    if not open_now:
        return []
    answered = set(load_pre_answers(project))

    # (run-id, cycle) descending == most recent first; run ids sort chronologically
    triage_files: list[tuple[str, int, Path]] = []
    for run_dir in runs.list_run_dirs(project):
        for tp in run_dir.glob("triage*.json"):
            m = _TRIAGE_RE.match(tp.name)
            if m:
                triage_files.append((run_dir.name, int(m.group(1) or 1), tp))
    triage_files.sort(reverse=True)

    by_id: dict[str, Decision] = {}
    for _run, _cycle, tp in triage_files:
        try:
            rj = json.loads(tp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        plan, _errors = validate_triage(rj, None)
        if plan is None:
            continue
        for decision in plan.decisions:
            by_id.setdefault(decision.id, decision)  # first (most recent) wins

    pending = [by_id[i] for i in by_id if i in open_now and i not in answered]
    return sorted(pending, key=lambda d: int(d.id.split("-")[1]))


def apply_pre_answer(
    project: Path, decision: Decision, option: DecisionOption, *, date: str, commit: bool = True
) -> None:
    """Record a human's out-of-band answer durably. Always writes a ledger
    `decision:` audit line; `close` also flips the entry to done (so it leaves
    the open set now), while `build`/`keep-open` are saved to the pre-answer
    store for the next sweep to consume. When `commit`, the ledger and store are
    committed on their own (only those paths) — best effort, so a non-git or
    dirty tree never blocks the on-disk record."""
    paths = bmadconfig.load_paths(project)
    ledger = paths.deferred_work
    detail = option.resolution or option.intent
    deferredwork.append_decision(ledger, decision.id, date, option.label, detail)
    if option.effect == "close":
        note = "closed by human decision" + (f": {option.resolution}" if option.resolution else "")
        deferredwork.mark_done(ledger, decision.id, date, note)
    else:
        record_pre_answer(project, decision.id, option, date=date)
    if commit:
        try:
            verify.commit_paths(
                project,
                f"chore(decisions): pre-answer {decision.id}",
                [ledger, store_path(project)],
            )
        except verify.GitError:
            pass  # files are written; git history is best effort
