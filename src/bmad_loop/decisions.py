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

Concurrency (#286/#469, DW-161): the store is a second orchestrator-written file
with the ledger's exact exposure — a `bmad-loop decisions` answer, the TUI
decision modal and a sweep can all reach it at once, and each writer here is a
read->edit->write of the WHOLE file, so unserialized they trade last-write-wins
and a human's answer vanishes. All three writers (`record_pre_answer`,
`prune_pre_answers`, `drop_pre_answer`) therefore run their whole cycle under
:func:`deferredwork.ledger_lock` keyed on the STORE path.

Reused rather than twinned, and precisely one thing is shared. NOT the OS lock:
`runs.lock_path_for` keys each sidecar on `sha256(resolved path)[:16]`, so the
ledger and the store take DIFFERENT locks and exclude nobody from each other —
correctly, since they are different files with different writers. What is shared
is `ledger_lock`'s thread-local NESTING guard, which is path-agnostic, and its
consequence is the whole point: no caller may hold both at once, in either
order. A second helper would mean two independent guards, and a caller could
then hold the ledger and the store simultaneously with neither noticing — the
lock-ordering hazard this avoids by construction rather than by convention. The
hold covers file I/O only, never a subprocess: `apply_pre_answer`'s commit stays
outside it, and it takes the two locks in sequence, never nested.

Readers stay lock-free on purpose (`load_pre_answers`, `pending_missed_decisions`,
`_decisions_phase`'s seeding read): every write replaces the file atomically, so
a reader already sees one whole version or another. And a conditional writer
handed nothing to do answers from ONE advisory read taken above the lock (#736)
— it publishes no bytes, so it linearizes at that read and there is nothing for a
rival to interleave with. That is what keeps `drop_pre_answer` for an id the
store never held — the common case — from newly failing on an acquisition it did
not need.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import Literal

from . import bmadconfig, deferredwork, runs, verify
from .platform_util import atomic_write_text_confined
from .sweep import Decision, DecisionOption, unusable_answer_reason, validate_triage

STORE_REL = Path(".bmad-loop") / "decisions.json"
_TRIAGE_RE = re.compile(r"^triage(?:-(\d+))?\.json$")
# How much of git's own message a `PublishFailure` carries. Both surfaces render
# the note INLINE on one line — appended to a `bmad-loop decisions` outcome line,
# and inside a single Textual toast — and git's ignored-path refusal is a
# multi-sentence hint block, so uncapped it is a several-hundred-character tail
# on both.
PUBLISH_ERROR_MAX = 200


def store_path(project: Path) -> Path:
    return project / STORE_REL


# --------------------------------------------------------------- store I/O


def load_pre_answers(project: Path) -> dict[str, dict]:
    """The project-level pre-answer store, {DW-id: {effect,label,intent,...}}.
    Tolerant of a missing or malformed file (returns {}): an absent file, an
    unreadable one, JSON that will not parse, bytes that are not UTF-8 at all
    (DW-140) and a non-object top level all degrade to an empty store rather than
    aborting the caller — every caller here is either a sweep or the `decisions`
    command, and neither has anything to gain from dying on one bad byte.

    Only the TOP level is validated. Values stay exactly as stored, however
    shaped: `record_pre_answer` and `prune_pre_answers` both read-modify-write the
    whole store through this function, so filtering here would make an unrelated
    write silently DELETE a human's corrupt entries — the file repair this
    codebase refuses. Readers that consume a value screen it themselves with
    `sweep.unusable_answer_reason`."""
    path = store_path(project)
    try:
        # `stat()` + `S_ISREG` INSIDE the `try`, never a bare `is_file()` outside
        # it (DW-261): on Python 3.11–3.13 `is_file()` re-raises a metadata
        # refusal, which escaped this "total" helper out of `_decisions_phase`
        # and every other caller; on 3.14 it suppresses the refusal instead. The
        # explicit probe folds the refusal into the same `{}` every other fault
        # class already answers, on every interpreter. Absence — ENOENT, ENOTDIR,
        # a directory at the store's name — stays a silent `{}` as before.
        if not S_ISREG(path.stat().st_mode):
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        # `ValueError`: `Path.stat` raises it for a non-encodable path (embedded
        # NUL), which the old bare `is_file()` absorbed and which
        # `deferredwork.probe_absence` now classifies as absence at the ledger's
        # write arm and the publish guard (DW-256/DW-268); this loader never asks
        # the helper — total means `{}` for that class and every other alike.
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(project: Path, data: dict) -> None:
    """#363: via the helper, not a hand-rolled tmp+replace. The store lives at a
    path nothing gitignores, so a failed replace used to strand
    `.bmad-loop/decisions.tmp` as an untracked file and hold `worktree_clean`
    False until a human deleted it; the helper removes its temp on any raise.

    Confined to ``project`` (#593). Refusing to follow a link planted at
    `decisions.json` itself was the behaviour-preserving choice — `os.replace`
    never dereferenced this destination either — and the security one: a driven
    session can write under `.bmad-loop/`, so honouring a link planted here would
    hand it a host-side write to any operator-writable path. But that refusal
    stopped at the final component, and the `mkdir` on the line below accepts a
    symlink-to-a-directory, so a link planted at `.bmad-loop/` survived the setup
    and redirected both the temp and the publish. The confined writer walks the
    components below `project` `O_NOFOLLOW` and writes through the descriptor
    that walk produced, so the same escalation now costs a refusal instead of a
    host-side write. Permission-neutral: no-follow never inherited a mode either,
    so the store still lands at `0600`.

    ``require_writable_target=True`` (#597) restores the `PermissionError` a bare
    `Path.write_text` raised here before #363 made the write atomic. The store is
    operator-curated — an operator who marks it read-only is answered rather than
    quietly overwritten and left with the `0444` still showing."""
    path = store_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text_confined(
        path,
        json.dumps(data, indent=2, sort_keys=True),
        confine_root=project,
        require_writable_target=True,
    )


def record_pre_answer(project: Path, dw_id: str, option: DecisionOption, *, date: str) -> None:
    """Persist a chosen option so a future sweep applies it without asking. The
    option's full semantics are stored (not just its key): a later triage may
    renumber options, so the sweep reads effect/intent from here directly.

    ONE locked read->edit->write (#286/#469, DW-161): unserialized, an answer
    recorded here between a rival writer's load and its write was overwritten
    wholesale — the store is read-modify-written in full, so the loser's entry
    does not survive as a merge, it disappears. No advisory probe: this writer
    always publishes bytes, so there is no read-provable no-op to answer above
    the lock. Called as `deferredwork.ledger_lock(...)` — the module attribute,
    not a `from ... import` binding — which is how this module reaches every
    `deferredwork` entry point it uses: the lock lives next to the ledger it was
    written for, and calling it through its home module keeps that ownership
    legible at the call site instead of aliasing it in here."""
    path = store_path(project)
    with deferredwork.ledger_lock(path):
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


def _prunable_ids(data: dict[str, dict], open_ids: set[str]) -> list[str]:
    """The store ids `prune_pre_answers` drops: those no longer in the open set.

    Pure, and module-level rather than inlined at each arm, for the reason
    `deferredwork`'s Concurrency note requires of every #736 probe — the advisory
    read and the authoritative locked read must run "the same pure decision
    helper ... so the two cannot drift". Two identical comprehensions satisfy
    that only by textual coincidence: edit one and the probe starts answering a
    question the hold does not ask, which is a silent lost update in the one
    direction the probe is allowed to skip the lock."""
    return [k for k in data if k not in open_ids]


def _droppable(data: dict[str, dict], dw_id: str, answer: object) -> bool:
    """Whether `drop_pre_answer` removes `dw_id`: present, and still `answer`.
    Pure and shared by its probe and its hold, for `_prunable_ids`'s reason."""
    return dw_id in data and data[dw_id] == answer


def prune_pre_answers(project: Path, open_ids: set[str]) -> list[str]:
    """Drop store entries whose DW id is no longer open (built or closed). No-op
    write when nothing is dropped. Returns the dropped ids.

    ONE locked read->edit->write (#286/#469, DW-161), like every writer here: the
    read that decides WHICH ids survive and the write that publishes them sit
    inside one hold, so a `bmad-loop decisions` answer recorded in that window is
    read by this prune rather than erased by it.

    The pre-lock read is an ADVISORY probe (#736) running :func:`_prunable_ids`,
    the same pure selection the locked pass runs, and only its "nothing to drop"
    answer is acted on — the overwhelmingly common outcome, since most cycles
    consume no pre-answer. Such a call publishes no bytes and linearizes at the
    probe read. Any other answer falls through to the hold, which re-reads and
    re-decides authoritatively.

    No `try:` around the probe, and that is a DIFFERENT arrangement from
    `record_decision`'s, not a shorter spelling of it. That probe wraps its raw
    `read_text` in `except Exception` so a read fault falls THROUGH to the hold,
    which re-reads and decides. Here `load_pre_answers` is total — a missing,
    unreadable, unparseable, non-UTF-8 or non-dict store all degrade to `{}` — so
    a read fault does not fall through at all: it becomes a decisive "nothing to
    prune" and the hold is skipped. That is exactly what an unreadable store did
    before DW-161, when this function was unlocked and read through the same
    total helper, so it is PRESERVED behavior rather than a new degradation — and
    it is the tolerant reader this module refuses to turn into a repair site."""
    path = store_path(project)
    if not _prunable_ids(load_pre_answers(project), open_ids):
        return []  # ADVISORY probe (#736): nothing to write, so nothing to serialize
    with deferredwork.ledger_lock(path):
        data = load_pre_answers(project)
        dropped = _prunable_ids(data, open_ids)
        if dropped:
            for k in dropped:
                del data[k]
            _write_store(project, data)
        return dropped


def drop_pre_answer(project: Path, dw_id: str, *, answer: object) -> bool:
    """Remove ONE id's entry — but only while it still IS `answer` — returning
    whether an entry was removed. The single-id sibling of `prune_pre_answers`
    above, and public for the same reason that one is: a sweep that has just
    dropped a stored answer as stale (DW-143) must be able to retire the entry that
    fed it without reaching into `_write_store`, which is this module's private
    writer.

    `answer` is the value the caller is retiring, and the entry goes only when the
    store still holds exactly that value. The caller's copy is what a sweep READ:
    `_decisions_phase` seeds a project pre-answer into `<run>/decisions.json` and
    from then on the run-local copy wins, so a human who re-answers the id out of
    band while that run is paused (`pending_missed_decisions` screens against this
    store alone, never against a run's file) leaves a NEWER entry here that the
    resumed run has never evaluated. An unconditional removal keyed on the id
    deleted that replacement — and committed the deletion — on the strength of a
    stale copy it had superseded. Equality is the whole provenance test: a seeded
    copy round-trips through JSON unchanged, so it compares equal to the entry it
    came from and unequal to anything a human wrote afterwards, and an interactive
    in-run answer never equals a store entry at all (different shape), so an id a
    run answered itself never reaches this store through the drop.

    The compare and the delete are ONE step under the hold: without it a
    replacement recorded between the read and the write — by `bmad-loop decisions`
    in another process — is precisely the value the compare exists to spare, and
    the stale snapshot would overwrite it. Under the lock a replacement lands
    either before the read (compares unequal, spared) or after the write
    (untouched). Equality then IS the provenance test, with one deliberate
    consequence: a re-answer that is byte-for-byte the stale entry (same option,
    same day — `answered_at` is day-precise) is the same answer, stale by the same
    evidence, and the next run would seed, drop and prune it anyway; retiring it
    now costs the human one drop-and-notify cycle they were owed nothing by, not
    an answer. A store revision would refuse that removal at the price of a store
    schema change, for an entry whose fate is identical either way.

    Same read-modify-write shape, same no-op-when-nothing-changes discipline: an
    absent id, or one holding a different value, writes nothing at all, so a drop
    whose answer only ever lived in `<run>/decisions.json` leaves the project
    store's bytes (and mtime) untouched. A removal goes through `_write_store`, so
    an operator-locked store still raises `PermissionError` rather than silently
    skipping — deleting a human-authored answer is a store write, never a repair.
    The `PermissionError` is raised under the hold and propagates through it; the
    lock is released on the way out.

    ONE locked read->edit->write (#286/#469, DW-161) with an ADVISORY pre-lock
    probe (#736): an absent or replaced id is answered from the probe read, so the
    no-op keeps taking no lock at all. That is load-bearing rather than an
    optimization — the absent-id case is the ordinary one (a stale answer that
    only ever lived in `<run>/decisions.json` has no store entry),
    `_prune_dropped_pre_answer` swallows nothing, and without the probe those
    calls would newly raise `runs.StateRootError` where no state root is
    derivable, or a Windows acquisition timeout, on a call that used to return
    `False` in silence. Probe and hold both decide through :func:`_droppable`,
    for the reason `_prunable_ids` gives: the probe may only skip the lock on the
    exact question the hold would ask.

    The probe needs no `try:`, and for a different reason than
    `record_decision`'s has one. That probe guards a raw `read_text` so a fault
    falls THROUGH to the hold; `load_pre_answers` is total, so a fault here is
    already an answer — an unreadable store reads as `{}`, the id is absent, and
    the call returns `False` without locking. That is what an unreadable store
    did before DW-161 too, when this read was unlocked and used the same total
    helper: PRESERVED behavior, not a new degradation, and consistent with this
    module's refusal to repair the store on the way past."""
    path = store_path(project)
    if not _droppable(load_pre_answers(project), dw_id, answer):
        return False  # ADVISORY probe (#736): nothing to write, so nothing to serialize
    with deferredwork.ledger_lock(path):
        data = load_pre_answers(project)
        if not _droppable(data, dw_id, answer):
            return False
        del data[dw_id]
        _write_store(project, data)
        return True


# ------------------------------------------------------- discovery + apply


def pending_missed_decisions(project: Path) -> list[Decision]:
    """Decisions earlier sweeps surfaced but no one answered: reconstructed from
    every run's persisted triage*.json, kept only when the DW id is still open
    and not already usably answered in the store — the value has to be one a sweep
    would actually consume, not merely a key that is present (see `answered`
    below). The most recent triage's wording of each id wins. Sorted by DW
    number."""
    paths = bmadconfig.load_paths(project)
    ledger = paths.deferred_work
    # OBSERVATION arm of the ledger-read contract (DW-146). This helper writes
    # nothing: every caller is a read-only surface (`cmd_decisions`, `cmd_status`,
    # the TUI), so an undecodable ledger must not take the whole listing down —
    # `UnicodeDecodeError` is a `ValueError` and escaped every `except OSError`
    # above it, exactly as it did for the triage-cache read below (DW-145).
    # The degradation is SILENT here, unlike the engine's observation sites: no
    # journal is reachable from a module-level function handed only a project
    # path, and the same is true of the triage read below. An empty ledger means
    # no open ids, which returns [] — the honest answer for a file nobody could
    # read, and the one the surfaces above already render.
    text, _fault = deferredwork.read_for_observation(ledger)
    open_now = deferredwork.open_ids(text)
    if not open_now:
        return []
    # By usable VALUE, not by key presence: `load_pre_answers` validates only the
    # top level, and a sweep drops a non-dict value and re-files the decision as
    # unanswered (`sweep-decisions-reload-failed`). Counting the bare key as
    # answered hid exactly those ids from this command, so the id was skipped by
    # every sweep and re-offered by nothing — unanswerable until a human found the
    # file. Re-answering overwrites the unusable value, which is the repair.
    #
    # Usable is `sweep.unusable_answer_reason` — the SAME predicate the sweep read
    # site applies (DW-142), not a local restatement of it. A value the sweep will
    # not consume must be re-offered here, so a shape either reader alone screened
    # out was an id no reader ever surfaced: a missing or unrecognized `effect` and
    # a non-string `key`/`label`/`intent`/`bundle_name` are unusable here for
    # exactly the reason they are unusable there.
    #
    # The STORE, not the reader, selects the predicate's configuration (DW-147):
    # both readers of THIS store — here and `_decisions_phase`'s pre-answer
    # seeding loop — pass the identical `allow_close=False`, which is what keeps
    # DW-142's same-store agreement intact while the run-local store, whose
    # interactive writer legitimately records a `close`, passes True. A `close`
    # here is hand-seeded or corrupt (`apply_pre_answer` sends a close to the
    # ledger and never records one), and it matches no bundling lane, so it must
    # be re-offered rather than counted answered.
    answered = {
        k
        for k, v in load_pre_answers(project).items()
        if unusable_answer_reason(v, allow_close=False) is None
    }

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
        # `UnicodeDecodeError` is a `ValueError`, NOT an `OSError`, so bytes that
        # are not UTF-8 at all escaped this arm (DW-145). Every caller here is a
        # read-only surface with nothing to gain from dying on one bad byte in one
        # run's cache: `cmd_decisions` and `cmd_status` catch `BmadConfigError`
        # alone, so `main`'s broad backstop turned the whole command into exit 1
        # (and `decisions --json` into no document at all), while the TUI's
        # `(BmadConfigError, OSError)` catch let it escape outright. Same widening,
        # same reason, as `load_pre_answers` and the two sweep siblings
        # (`_ensure_triage`, `_decisions_phase`).
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        # Boundary guard (DW-155/DW-158): `json.loads` returns `Any`, and the
        # parameter below is `dict[str, Any] | None`, so this call was handing an
        # unchecked shape across a typed boundary — a non-object document made
        # `validate_triage` raise `AttributeError` past every caller listed in the
        # comment above. `validate_triage` is total over shapes now, so removing
        # this alone reproduces nothing; it stands as the parity these two
        # siblings already have — `load_pre_answers` (`isinstance(data, dict)`)
        # and `_ensure_triage`'s cache-reload branch — keeping this reader's
        # per-file degradation independent of the validator's internals.
        if not isinstance(rj, dict):
            continue
        plan, _errors = validate_triage(rj, None)
        if plan is None:
            continue
        for decision in plan.decisions:
            by_id.setdefault(decision.id, decision)  # first (most recent) wins

    pending = [by_id[i] for i in by_id if i in open_now and i not in answered]
    return sorted(pending, key=lambda d: int(d.id.split("-")[1]))


@dataclass(frozen=True)
class PublishRefusal:
    """One operand `apply_pre_answer` WROTE but could not publish.

    `file` is the LEXICAL basename of the operand — `deferred-work.md`
    (`ProjectPaths.deferred_work`) or `decisions.json` (`STORE_REL`), a code
    constant at both operands and never operator-controlled prose, which is what
    makes it safe for both surfaces to print verbatim. `cause` is
    `verify.unpublishable_target`'s closed four-token enum, spelled here
    IDENTICALLY to its return type — the two are one contract. Pyright rejects
    producer tokens this receiving union does not accept; it does not enforce
    equality of the unions. `error`
    carries the decode fault (`target-undecodable`, the ledger's bytes) or the OS
    fault (`target-unreadable`, a probe that raised) where the refusal has one to
    attribute, and is `None` for the two refusals that have none to name: a plain
    absence, and a target that is present but not a regular file (an empty string
    would read as a fault)."""

    file: str
    cause: Literal["target-absent", "target-unreadable", "target-not-a-file", "target-undecodable"]
    error: str | None = None


@dataclass(frozen=True)
class PublishFailure:
    """One operand `apply_pre_answer` WROTE and then tried and FAILED to publish
    (DW-226): git itself answered `GitError` — a parent in no repository, a
    gitignored path `git add` refuses with rc 1, a locked index, git absent.

    A SEPARATE type from `PublishRefusal`, not a fourth token on its `cause`,
    and that is a contract rather than taste: `PublishRefusal.cause` is spelled
    IDENTICALLY to `verify.unpublishable_target`'s return type and the two are
    ONE contract, so a token describing something that guard can never return
    would silently break the equality the next reader is entitled to assume. The
    two reports also answer different questions — a refusal is a publish
    DECLINED before any git ran, a failure is git having run and been unable —
    and a failure always has an error to name where a refusal may not.

    `file` is the LEXICAL basename under the same rule `PublishRefusal.file` is
    held to: a code constant at both operands (`deferred-work.md`,
    `decisions.json`) and never operator-controlled prose, which is what lets
    both surfaces print it verbatim. `error` is git's own message with
    whitespace COLLAPSED and then CLIPPED to `PUBLISH_ERROR_MAX` — git stderr is
    multi-line and its ignored-path refusal is a multi-sentence hint block, while
    both surfaces render this note inline on one line."""

    file: str
    error: str


@dataclass(frozen=True)
class PreAnswerResult:
    """What `apply_pre_answer` persisted: whether a ledger `decision:` line landed,
    and which written operands went unpublished.

    `recorded` is `record_decision`'s own boolean with DW-198's exact meaning —
    a line landed, False is not an error, it withholds nothing, and it says
    nothing about whether the ledger was readable. `refusals` is a separate axis
    and usually empty: the operand list is already gated on what this call wrote,
    so a refusal means an answer that really WAS written could not be published,
    which is why both surfaces report it rather than treating it as noise.
    `failures` is the same axis for the other lane (DW-226) — an operand that
    reached git and could not be committed — and carries the same weight for the
    same reason: the write landed on disk and is missing from git history."""

    recorded: bool
    refusals: tuple[PublishRefusal, ...] = ()
    failures: tuple[PublishFailure, ...] = ()

    def publish_note(self) -> str | None:
        """One shared wording for both out-of-band surfaces, or `None` when no
        target was refused and none failed. Publication remains best effort. The
        caller supplies its own separator: `cli` appends it to the outcome line it
        already prints, and the TUI either
        appends it to the existing non-write toast or raises one of its own.

        The fault rides WITH the cause where the refusal has one, the way the
        sweep's `error` field rides beside its `refuse_cause`. Without it the four
        causes read alike at both surfaces, and `target-undecodable` (the ledger's
        decode fault) and `target-unreadable` (an `EACCES`, a symlink loop) are the
        two that name something a human can act on. The other two have no
        exception text and take the bare wording:
        `target-absent`, and `target-not-a-file` for a target present but of the
        wrong type. An empty parenthetical would read as a fault.

        A FAILED publish (DW-226) joins the same `not committed to git: ...` list
        under its own token, `commit-unavailable`, after the refusals — the
        refusal wording and ordering are untouched, and neither surface needs a
        branch of its own to report the new lane. The token deliberately echoes
        the sweep's `sweep-ledger-commit-unavailable` journal row, which names
        the same `verify.GitError` about the same files."""
        if not self.refusals and not self.failures:
            return None
        named = ", ".join(
            [
                f"{r.file} ({r.cause})" if r.error is None else f"{r.file} ({r.cause}: {r.error})"
                for r in self.refusals
            ]
            + [f"{f.file} (commit-unavailable: {f.error})" for f in self.failures]
        )
        return f"not committed to git: {named}"


def apply_pre_answer(
    project: Path, decision: Decision, option: DecisionOption, *, date: str, commit: bool = True
) -> PreAnswerResult:
    """Record a human's out-of-band answer durably, answering whether a ledger
    `decision:` audit line actually landed. `close` also flips the entry to done
    (so it leaves the open set now), while `build`/`keep-open` are saved to the
    pre-answer store for the next sweep to consume. When `commit`, the files THIS
    call wrote are committed ONE AT A TIME, each on its own (only that path) and
    each rooted at its OWN resolved parent — best effort, so a non-git or dirty
    tree never blocks the on-disk record.

    `PreAnswerResult.recorded` is `record_decision`'s own boolean, and it is the
    CALLER's non-write signal, not decoration — the same discipline
    `sweep._apply_decision_effect` applies inside the sweep (DW-186), carried to
    the two out-of-band surfaces (DW-198). `record_decision` answers False in
    exactly the two states that mean no line was written — no ledger file at all,
    and no entry carrying this id, a rival writer being free to retire one while
    the prompt blocks on the human — and True only when it wrote one. Discarded,
    those two states were indistinguishable from a write at both call sites, which
    then announced closures the ledger never took: `cli.cmd_decisions` printed
    `closed now` and `tui.app._record_decision` counted the decision into
    `recorded N decision(s)`.

    What False does NOT say is that the ledger was readable: the missing-file arm
    answers before any read. So a caller may report a non-write and nothing more;
    it may not infer a read fault from it.

    False is not an error and withholds nothing. The pre-answer store write still
    runs on it, unchanged by the boolean. So for `build`/`keep-open` the human's
    answer really was saved to the store, and a caller's report must not deny that
    — but it must not promise a later sweep will consume it either: a sweep's
    triage is derived from the ledger's open ids, so an id the ledger no longer
    carries is never surfaced again and `prune_pre_answers` drops the stored
    answer as no longer open.

    THE COMMIT IS GATED TWICE, and the two gates answer different questions
    (DW-209/213).

    "Did this call write it?" is DW-185's rule — a phase that wrote nothing runs
    no git — and it is what decides the operand list: the ledger is an operand
    only when `recorded` is True, and the store only when the effect is not
    `close` (a `close` writes no store entry). An empty list spawns no git at all.
    Unconditional, the block reached `verify.commit_paths` with `[ledger, store]`
    whatever had happened, and against a TRACKED ledger that has gone absent
    `commit_paths` deliberately keeps the missing path as a DELETION to stage — so
    a `close` whose ledger file had vanished published that ledger's own REMOVAL
    under a `chore(decisions): pre-answer <id>` message, taking every `decision:`
    line and open entry out of HEAD. The `recorded` gate is what closes that: the
    absent-ledger state never puts the ledger in front of `git add`.

    "Is the target still publishable?" is DW-199/203/205's guard, shared verbatim
    with the sweep's nine publishers as `verify.unpublishable_target`, and it
    narrows the residual race between the write above and the staging below. The
    FAMILY is declared here, never derived from the path. Because the first gate
    is upstream of the second, this one fires only on a race or a resolve fault,
    which is exactly why a refusal is worth reporting: it means an answer that
    really was written could not be published. A resolve fault (`OSError` on a
    broken chain, `RuntimeError` on a symlink loop under 3.11–3.12) takes the same
    refusal arm with cause `target-unreadable` — a target whose path cannot be
    resolved cannot be read well enough to publish — because this module has no
    journal to route it to and the cause enum is closed by contract. The LEDGER
    operand alone can come back `target-undecodable` — its bytes were replaced by
    ones nobody can decode between the write and the staging — carrying the
    `LedgerReadError` text in `error`, and that refusal reaches the outcome line
    and the toast like any other (DW-237). EITHER
    family's target found present but NOT a regular file takes the token
    `target-not-a-file` (DW-211/228 for the store, DW-238 for the ledger, which
    reached it as `target-absent` until then): publishing a directory's literal
    pathspec would stage its descendants recursively under this call's own
    message, and a FIFO or socket at the name is refused the same way.

    A refusal drops only ITS operand; the survivors still publish, and a refusal
    never raises.

    THE COMMIT IS PER OPERAND, ROOTED AT THAT OPERAND'S OWN RESOLVED PARENT
    (DW-225/226) — the rule `sweep._commit_ledger` already publishes this same
    ledger under (DW-160/DW-175): NAME THE FILE YOU PUBLISHED, and let the
    repository follow from the file rather than from a role. `target` is already
    resolved by GATE TWO, so `target.parent` needs no second resolve and
    `commit_paths` relativizes the operand to a bare basename — the one-file
    scope that bounds the blast radius.

    ONE `commit_paths(project, ..., [ledger, store])` call for BOTH operands
    failed two ways at once. The ledger hangs off `implementation_artifacts`,
    which `bmadconfig._resolve` accepts as any absolute path, so it may sit
    outside the project or inside a disjoint `repo_root`; against a project root
    `commit_paths`' `relative_to` raised `ValueError` and its `except ValueError:
    continue` dropped that operand SILENTLY — nothing committed and nothing
    reported (DW-225). And a single `git add` over both operands exits 1 when
    either one is gitignored (`_literal_specs` forces literal pathspecs, and git
    refuses an explicitly named ignored path), so a gitignored ledger took its
    publishable sibling down with it (DW-226). One commit per operand is the
    decided shape for both: the two cannot sink each other, at the cost of two
    commits carrying the same message where the operands share a repository.

    That cost has two consequences, both decided rather than discovered. The pair
    is no longer ATOMIC: an interrupt between the two, or a `pre-commit` hook that
    is now invoked TWICE and fails the second time, can leave the ledger published
    and the store not — a split a single commit could not produce. It is accepted
    because the alternative is the DW-226 sinking, and because both files are
    already on disk and the next sweep reads them from there, not from HEAD. And
    the split-TREE case is deliberately UNREPORTED: two operands landing in two
    repositories is the CORRECT outcome of per-operand rooting, not a degrade, so
    `refusals`, `failures` and `publish_note()` all stay empty for it. Only a
    publish that was refused or that git could not make is news.

    A `verify.GitError` still never escapes — the files are written and git
    history is best effort — but it is no longer SILENT: it is caught per
    operand into a `PublishFailure` that both surfaces print through
    `publish_note()`, beside the refusals and under its own
    `commit-unavailable` token. So a project that is no git repository at all
    now reports on every answer where it previously said nothing; that report is
    the DW-226 lane, not noise. The commit stays OUTSIDE every lock (#286).

    Precondition: `date` is ISO `YYYY-MM-DD`. The ledger writers raise
    `ValueError` on anything else (it would otherwise land a `status:` line that
    reads as neither open nor done), so a caller building the date itself must
    either guarantee the format or catch it. The option's own free text carries
    no such precondition — it is sanitized, never refused."""
    paths = bmadconfig.load_paths(project)
    ledger = paths.deferred_work
    detail = option.resolution or option.intent
    close_note = None
    if option.effect == "close":
        close_note = "closed by human decision" + (
            f": {option.resolution}" if option.resolution else ""
        )
    # ONE locked read->edit->write (#286/#469). As the `append_decision` +
    # `mark_done` pair it was two acquisitions with a window between them, and a
    # rival writer landing there left the entry carrying a decision that says
    # "close it" over a status that still says open. The bytes are identical to
    # the pair's. The commit below stays OUTSIDE any lock — locks are held only
    # around file I/O, never across a subprocess (#286).
    recorded = deferredwork.record_decision(
        ledger, decision.id, date, option.label, detail, close_note=close_note
    )
    if option.effect != "close":
        record_pre_answer(project, decision.id, option, date=date)
    if not commit:
        return PreAnswerResult(recorded=recorded)
    # GATE ONE — only what THIS call wrote, each paired with the family it
    # declares for the guard. Never derived from the path (see the docstring).
    wrote: list[tuple[Path, Literal["ledger", "store"]]] = []
    if recorded:
        wrote.append((ledger, "ledger"))
    if option.effect != "close":
        wrote.append((store_path(project), "store"))
    # GATE TWO — the shared publishable-target guard, on the RESOLVED operand
    # because that is the file git would publish, and before any git runs because
    # a refused publish must spawn none.
    # `(target, path)` pairs, not bare targets: the commit needs the RESOLVED
    # target (it is the file git publishes and its parent is the repository to run
    # in), while a failure report needs the LEXICAL basename for the same reason a
    # refusal does — see the `path.name` comment below.
    operands: list[tuple[Path, Path]] = []
    refusals: list[PublishRefusal] = []
    failures: list[PublishFailure] = []
    for path, family in wrote:
        try:
            target = path.resolve()
        except (OSError, RuntimeError, ValueError) as e:
            # `ValueError` too (DW-275): an embedded NUL, or a lone surrogate as its
            # `UnicodeEncodeError` subclass, on CPython POSIX. This publisher has no
            # journal, so the refusal is the fault's only route out.
            refusals.append(PublishRefusal(file=path.name, cause="target-unreadable", error=str(e)))
            continue
        refusal = verify.unpublishable_target(target, family)
        if refusal is None:
            operands.append((target, path))
            continue
        cause, error = refusal
        # `path.name`, never `target.name`, and here is where that is a CHOICE: a
        # resolved target is reached through a symlink an OPERATOR named, so its
        # tail is arbitrary operator text, while the lexical tail is a code
        # constant at both operands (`deferred-work.md`, `decisions.json`). That is
        # what lets both surfaces print it verbatim — the same rule
        # `sweep._commit_ledger`'s `file` journal field is held to.
        refusals.append(PublishRefusal(file=path.name, cause=cause, error=error))
    # ONE COMMIT PER OPERAND, in the operand's OWN tree (DW-225/226 — see the
    # docstring). An empty operand list spawns no git at all, which is GATE ONE's
    # rule and is why this needs no `if operands:` of its own.
    message = f"chore(decisions): pre-answer {decision.id}"
    for target, path in operands:
        try:
            verify.commit_paths(target.parent, message, [target])
        except verify.GitError as e:
            # Caught PER OPERAND: a failing publish must not skip its sibling's,
            # which is half of what DW-226 is. Still never raised — the file is
            # written and git history is best effort — but reported now rather
            # than swallowed. `path.name` for the same reason a refusal uses it,
            # and git's stderr is multi-line where both surfaces print this note
            # on ONE line, so the text is whitespace-collapsed.
            collapsed = " ".join(str(e).split())
            if len(collapsed) > PUBLISH_ERROR_MAX:
                # ...and CLIPPED: git's ignored-path refusal is a multi-sentence hint
                # block, which uncapped is a several-hundred-character tail on one CLI
                # outcome line and inside one toast.
                collapsed = collapsed[: PUBLISH_ERROR_MAX - 1] + "…"
            failures.append(PublishFailure(file=path.name, error=collapsed))
    return PreAnswerResult(recorded=recorded, refusals=tuple(refusals), failures=tuple(failures))
