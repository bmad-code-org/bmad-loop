"""Deferred-work sweep: triage the ledger, decide, execute bundles.

A sweep is its own run type. One LLM triage session classifies every open
deferred-work entry (verified against actual code — ledger statuses are
unreliable); the orchestrator validates the result deterministically, asks
the human about decision items (interactive runs only), then drives each
work bundle through the inherited dev -> review -> verify -> commit pipeline.
The orchestrator performs all ledger edits it can do deterministically and
gates on the ones it delegates (verify.verify_review_bundle).
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, NoReturn, TypeVar, assert_never

from . import deferredwork, gates, verify
from .adapters.generic import load_result_document
from .engine import (
    Engine,
    RunPaused,
    _ArmedClose,
    _ledger_fault_text,
    _LedgerAnchor,
    _publication_refusal,
    _session_task_id,
)
from .escalation import critical_session_reason, env_fault_pause_reason, session_failure_reason
from .model import PAUSE_STORY_GATE, Phase, StoryTask, result_mapping
from .platform_util import (
    DIR_FD_ANCHORED_WRITES,
    atomic_write_text,
    atomic_write_text_confined,
    neutralize_surrogates,
    open_dir_confined,
    path_is_confined,
    safe_segment,
)
from .runs import StateRootError, _project_of_run_dir, events_dir_for
from .signals import session_events
from .statemachine import advance


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class MissingLedgerEntriesError(Exception):
    """`_write_intent` refused to brief a bundle whose ids are not all in the
    ledger (DW-252). `ids` names every bundle id with NO parsed entry at all — a
    present-but-closed entry is still emitted verbatim and is not missing.

    Raised BEFORE any side effect: no `bundles/<dirname>/` directory, no file.
    An intent document briefs a dev session; an empty "Ledger entries (verbatim)"
    section briefed it on nothing, and the session was spent anyway.

    A plain `Exception` on purpose, never `OSError` or `ValueError`: the arms
    around the two writers catch `OSError` for the file's own I/O, and a subclass
    would be swallowed by exactly the handler this refusal must escape."""

    def __init__(self, ids: tuple[str, ...]) -> None:
        self.ids = ids
        super().__init__(f"no ledger entry for {', '.join(ids)}")


class _MigrationRecordInvalid(Exception):
    """Internal signal for a recovery record rejected before escalation I/O."""


TRIAGE_KEY = "sweep-triage"
TRIAGE_WORKFLOW = "deferred-sweep-triage"
MIGRATE_KEY = "sweep-migrate"
MIGRATE_WORKFLOW = "deferred-sweep-migrate"
_MIGRATION_RECOVERY_FORMAT = 1
_MIGRATE_BASELINE_RECORD = "migrate-baseline.md"
_MIGRATE_REWRITE_RECORD = "migrate-rewrite.md"
_MIGRATE_MANIFEST_RECORD = "migrate-manifest.json"
_MIGRATE_RESULT_RECORD = "migrate-result.json"
_LedgerCommitOutcome = Literal["committed", "clean", "refused", "unavailable"]
BUNDLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}\Z")
_BUNDLE_NAME_MAX_LENGTH = 40
_BUNDLE_NAME_INITIAL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_BUNDLE_NAME_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
# the inverse of SweepEngine._bundle_key: "dw-<name>" (cycle 1) / "dw<N>-<name>".
# A cycle-1 key always has "-" straight after "dw", so the cycle group matches
# empty and the split stays unambiguous even for a bundle named "2fix".
BUNDLE_KEY_RE = re.compile(r"^dw(\d*)-(.+)\Z")
# The token `_write_intent` emits and `_bundle_intent_reason` parses back out of
# a persisted intent.md. One definition so the writer and the grader cannot drift.
_INTENT_DW_IDS_PREFIX = "dw_ids: "
DECISION_EFFECTS = ("build", "close", "keep-open")
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
DW_ID_RE = re.compile(r"DW-\d+\Z")


def decimal_digits_key(value: str) -> tuple[int, str]:
    """Order arbitrary-length Unicode decimal text without converting to ``int``."""
    normalized = "".join(str(unicodedata.decimal(char)) for char in value)
    normalized = normalized.lstrip("0") or "0"
    return len(normalized), normalized


def increment_decimal_digits(value: str) -> str:
    """Normalize and increment arbitrary-length Unicode decimal text."""
    ascii_value = "".join(str(unicodedata.decimal(char)) for char in value)
    digits = list(ascii_value.lstrip("0") or "0")
    carry = 1
    for index in range(len(digits) - 1, -1, -1):
        if not carry:
            break
        if digits[index] == "9":
            digits[index] = "0"
        else:
            digits[index] = chr(ord(digits[index]) + 1)
            carry = 0
    if carry:
        digits.insert(0, "1")
    return "".join(digits)


# The three lines `_return_after_decisions` shows a human, as whole named constants
# joined explicitly. Assembling any of them at the call site out of adjacent string
# literals is what this avoids: a reflow can silently re-bind a trailing literal
# to one arm of a conditional, and these are the only strings that phase prints.
# Neither failure line leads with the success glyph — a `✓` in front of a
# miss reads as success at a glance — and both name the journal kind an operator
# greps for, since the answers really are saved and only the ledger is short.
_HANDBACK_TAIL = "sweep continues in the background"
_HANDBACK_RECORDED = " ".join(["✓ decisions recorded —", _HANDBACK_TAIL])
_HANDBACK_LEDGER_MISS = " ".join(
    [
        "! answers saved, but not every decision reached the deferred-work ledger",
        "(see sweep-decision-effect-unavailable in the journal) —",
        _HANDBACK_TAIL,
    ]
)
# A fault on the last decision attempt or in the close phase requires repair.
# A later successful decision clears only the decision phase's doubt.
_HANDBACK_LEDGER_HALTED = " ".join(
    [
        "! answers saved, but the deferred-work ledger is not fit to publish",
        "(see sweep-resolved-close-unavailable, sweep-decision-effect-unavailable,",
        "sweep-ledger-commit-refused or sweep-ledger-commit-unavailable in the journal) —",
        "repair the ledger and re-run",
    ]
)
# The scalars a stored answer's consumers read as strings, split by WHO reads them.
# `_agreeing_option` runs on both `_materialize_bundles` lanes, so `key`/`label` are
# consumed whatever the effect; `intent`/`bundle_name` are read by the BUILD lane
# alone. Order within each tuple is the order a defect is reported in.
_ANSWER_STR_FIELDS = ("key", "label")
_BUILD_ANSWER_STR_FIELDS = ("intent", "bundle_name")


def unusable_answer_reason(value: Any, *, allow_close: bool) -> str | None:
    """Why `value` is not a usable persisted decision answer, or None when it is.

    ONE schema for the two readers of a stored answer — `SweepEngine._decisions_phase`'s
    read loops and `decisions.pending_missed_decisions` — because they have to agree
    (DW-142): a value one accepted and the other rejected was an id that either got
    silently ignored by every sweep while this command counted it answered, or the
    reverse. The returned string is the `malformed` row's reason, so it names ids,
    fields and TYPE names only, never a stored answer's prose.

    The checks are exactly what the consumers assume, no more — because rejecting is
    not free: an answer refused here stops being seeded, and for `keep-open` that
    means it stops SUPPRESSING bundles, so a later cycle can bundle and build work
    the human explicitly asked to leave alone. A field is therefore screened only
    where a reader of THIS effect actually consumes it.

    `effect` must be a recognized `DECISION_EFFECTS` member because
    `_materialize_bundles` routes on it and an unrecognized one matches no lane — the
    answer counts as given while nothing acts on it. `key` and `label`, when present,
    must be strings for every effect: `_agreeing_option` reads both and runs on both
    lanes. `intent` and `bundle_name` are screened for `build` ONLY, the sole lane
    that reads them — a list `intent` used to reach `Bundle.intent` as its truthy
    Python repr and ship into a dev session (DW-141), while the same corrupt field on
    a keep-open answer is inert prose no reader touches. Fields no reader consumes
    (`resolution`, `answered_at`) are not validated for any effect.

    `close` is usable for the RUN store (`allow_close=True`) even though
    `record_pre_answer` never stores it: the interactive writer in
    `_decisions_phase` records `effect: "close"` for a decision answered `close`
    this run, so rejecting it would re-ask a decision the human already answered
    inside the same run. That justification does NOT transfer to the project store
    (DW-147): `apply_pre_answer` applies a `close` to the LEDGER and deliberately
    skips `record_pre_answer`, so no legitimate producer writes one there — a
    `close` in `.bmad-loop/decisions.json` is hand-seeded or corrupt, and it used
    to be counted answered by every reader while matching NO `_materialize_bundles`
    lane (which needs `build` or `keep-open`), leaving the id never built, never
    closed and never re-offered. Hence the store, not the reader, selects the gate:
    `allow_close` is keyword-only and has no default, so a future reader has to
    state which store it is reading rather than inherit the wrong answer silently.

    Rejecting is never a repair — the caller keeps the value and re-publishes it
    unchanged; see `_decisions_phase`'s `unusable` map and `load_pre_answers`."""
    if not isinstance(value, dict):
        return f"not a JSON object: {type(value).__name__}"
    if "effect" not in value:
        return "effect missing"
    effect = value["effect"]
    if not isinstance(effect, str) or effect not in DECISION_EFFECTS:
        return "effect not recognized"
    if effect == "close" and not allow_close:
        return "effect close not accepted from this store"
    fields = _ANSWER_STR_FIELDS
    if effect == "build":
        fields += _BUILD_ANSWER_STR_FIELDS
    for field in fields:
        if field in value and not isinstance(value[field], str):
            return f"{field} not a string: {type(value[field]).__name__}"
    return None


def _answer_str(answer: dict[str, Any], field: str) -> str:
    """A stored answer's scalar read as a string, or "" when it is anything else.

    `str(answer.get(field, ""))` was the old spelling and it never failed: a list
    became "['a', 'b']" and a dict "{...}" — truthy prose no human authored, which
    the build lane then shipped as a `Bundle.intent` (DW-141). "" instead, so each
    site's EXISTING fallback chain handles it: an agreeing option's value, then the
    site's own default or its drop cause. No new branch, no new drop cause.

    Defense-in-depth, not the production path: `_decisions_phase` already rejects
    at the read site (`unusable_answer_reason`) every field a reader of that effect
    consumes, so in production each site here only ever sees strings. It stays
    because the test suite hands `_materialize_bundles` a map directly, past that
    read site — the same reason each lane holds its own `isinstance` guard."""
    value = answer.get(field)
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class _BundleNameRepair:
    field: str
    original: str
    normalized: str


def _normalize_bundle_names(rj: dict[str, Any] | None) -> tuple[_BundleNameRepair, ...]:
    """Truncate overlong bundle-name fields only when their shape is already safe.

    Total on any input (DW-181): a non-mapping document answers `()` with no
    repairs rather than raising out of `.get`. Same totality as the
    `escalation._escalation_list` twin and for the same reason -- callers'
    totality over parseable JSON. DW-181 wrote both guards while
    `Engine._run_session` still dereferenced `result.result_json.get(...)`
    behind an `is not None` check alone, so a truthy non-mapping raised there
    before any sweep lane reached here; DW-206 routed that frame through
    `model.result_mapping`, and the triage lane now runs to this guard. It
    calls this one line ahead of `validate_triage`, which names the wrong shape
    on the existing `errors` channel.

    Kept as its own `isinstance` rather than delegated to `result_mapping`, so
    the ablation still proves this function total on its own.
    """
    if not isinstance(rj, dict):
        return ()

    repairs: list[_BundleNameRepair] = []

    def normalize(container: dict[str, Any], key: str, field: str) -> None:
        raw = container.get(key)
        if not isinstance(raw, str) or len(raw) <= _BUNDLE_NAME_MAX_LENGTH:
            return
        if raw[0] not in _BUNDLE_NAME_INITIAL_CHARS or any(
            char not in _BUNDLE_NAME_SAFE_CHARS for char in raw[1:]
        ):
            return
        normalized = raw[:_BUNDLE_NAME_MAX_LENGTH]
        container[key] = normalized
        repairs.append(_BundleNameRepair(field, raw, normalized))

    bundles = rj.get("bundles", [])
    if isinstance(bundles, list):
        for bundle_index, bundle in enumerate(bundles):
            if isinstance(bundle, dict):
                normalize(bundle, "name", f"bundles[{bundle_index}].name")

    decisions = rj.get("decisions", [])
    if isinstance(decisions, list):
        for decision_index, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                continue
            options = decision.get("options", [])
            if not isinstance(options, list):
                continue
            for option_index, option in enumerate(options):
                if isinstance(option, dict):
                    normalize(
                        option,
                        "bundle_name",
                        f"decisions[{decision_index}].options[{option_index}].bundle_name",
                    )
    return tuple(repairs)


# ------------------------------------------------------------- triage plan


@dataclass(frozen=True)
class ResolvedEntry:
    id: str
    evidence: str


@dataclass(frozen=True)
class Bundle:
    name: str
    dw_ids: tuple[str, ...]
    intent: str
    decision_note: str = ""  # human-decision context appended to the intent file


@dataclass(frozen=True)
class DecisionOption:
    key: str
    label: str
    effect: str  # build | close | keep-open
    intent: str = ""  # required when effect == "build"
    resolution: str = ""  # optional when effect == "close"
    bundle_name: str = ""  # optional name override for the built bundle


@dataclass(frozen=True)
class Decision:
    id: str
    question: str
    context: str
    options: tuple[DecisionOption, ...]
    recommendation: str

    def option(self, key: str) -> DecisionOption | None:
        for opt in self.options:
            if opt.key == key:
                return opt
        return None


@dataclass(frozen=True)
class TriagePlan:
    open_ids: frozenset[str]
    already_resolved: tuple[ResolvedEntry, ...] = ()
    bundles: tuple[Bundle, ...] = ()
    blocked: tuple[tuple[str, str], ...] = ()  # (id, blocker)
    skip: tuple[tuple[str, str], ...] = ()  # (id, reason)
    decisions: tuple[Decision, ...] = ()


@dataclass(frozen=True)
class SweepSelection:
    selected: tuple[deferredwork.DWEntry, ...]
    excluded: tuple[deferredwork.DWEntry, ...]
    missing_severity: tuple[deferredwork.DWEntry, ...] = ()


def select_entries(
    entries: Iterable[deferredwork.DWEntry],
    *,
    only_ids: tuple[str, ...] | None = None,
    min_severity: str | None = None,
    validate_only: bool = False,
) -> SweepSelection:
    """Select from canonical open entries without changing the ledger parser's universe."""
    if only_ids is not None and min_severity is not None:
        raise ValueError("--only cannot combine with --min-severity")
    if only_ids is not None:
        if not only_ids:
            raise ValueError("--only requires at least one DW-<n> id")
        malformed = [dw_id for dw_id in only_ids if not DW_ID_RE.fullmatch(dw_id)]
        if malformed:
            raise ValueError("--only contains malformed ids: " + ", ".join(malformed))
    if min_severity is not None and min_severity not in SEVERITY_ORDER:
        raise ValueError("--min-severity must be one of: " + ", ".join(SEVERITY_ORDER))
    open_entries = tuple(entry for entry in entries if entry.open)
    if only_ids is not None:
        open_ids = {entry.id for entry in open_entries}
        unavailable = [dw_id for dw_id in only_ids if dw_id not in open_ids]
        if validate_only and unavailable:
            raise ValueError("--only ids must exist and be open: " + ", ".join(unavailable))
        requested = set(only_ids)
        return SweepSelection(
            selected=tuple(entry for entry in open_entries if entry.id in requested),
            excluded=tuple(entry for entry in open_entries if entry.id not in requested),
        )
    if min_severity is not None:
        floor = SEVERITY_ORDER[min_severity]
        missing = tuple(entry for entry in open_entries if entry.severity is None)
        selected = tuple(
            entry
            for entry in open_entries
            if entry.severity is not None and SEVERITY_ORDER[entry.severity] >= floor
        )
        selected_ids = {entry.id for entry in selected}
        return SweepSelection(
            selected=selected,
            excluded=tuple(entry for entry in open_entries if entry.id not in selected_ids),
            missing_severity=missing,
        )
    return SweepSelection(selected=open_entries, excluded=())


def _plan_str(container: dict[str, Any], field: str, where: str, errors: list[str]) -> str | None:
    """One LLM-authored free-text scalar off a triage plan, or None when it is
    not a string — the plan-input twin of `unusable_answer_reason` (DW-148).

    `str(value)` was the old spelling and it never failed: a list `intent`
    became the truthy repr "['do', 'x']", which satisfied the
    `effect == "build" and not intent` gate, landed in `DecisionOption.intent`
    and rode into `Bundle.intent`, `intent.md` and a dev session — the DW-141
    harm, on the plan surface instead of the persisted-answer store. A malformed
    plan is REFUSED and re-driven instead, through the `errors` channel that
    already exists; there is no repair path and no new drop cause.

    The message names a POSITION, or the decision id when that id is itself a
    string, plus the field and the type name only — never the offending value's
    prose, the same rule and the same wording as `unusable_answer_reason`. Since
    DW-157 a decision-level message can name the decision's own position too,
    when its `id` is not a string.

    Callers thread the `None` through rather than falling back to "": "" would
    re-enter the field's own empty/invalid-value branch and double-report, and
    one error per fault is the convention here (see
    `test_validate_triage_reports_one_error_when_a_name_fails_both_gates`). The
    dataclasses are constructed with `value or ""` at the end.

    The fields screened here: bundle `name` and `intent`; option `key` (DW-156),
    `effect`, `intent`, `label`, `resolution` and `bundle_name`; decision
    `question` (DW-156), `recommendation` and `context`. Everything else off a
    triage plan — the decision, section and `dw_ids` identifiers,
    `already_resolved.evidence`, `blocked.blocker`, `skip.reason` — keeps its
    `str(...)` treatment deliberately.

    `key` and `question` joined the screened set under DW-156 for their LIVE
    unscreened sinks, including operator-facing and journaled consumers:
    `question` is printed by `DecisionPrompter.ask`, announced by `gates.notify`,
    written to the `decision-pending` journal record and listed
    by `bmad-loop decisions --list`; `key` is printed among the options there,
    persisted into the answer store and written to `decision-answered`. Both also
    reach `Bundle.decision_note` and so `intent.md`. That path already screened
    `key`: `_materialize_bundles` takes the option key only when `_agreeing_option`
    matched it against the answer-store-screened `answer_key`. The agreement
    check does not screen `question`, whose type check here closes that path."""
    value = container.get(field, "")
    if isinstance(value, str):
        return value
    errors.append(f"{where}: {field} not a string: {type(value).__name__}")
    return None


def _plan_list(
    container: dict[str, Any], field: str, where: str, errors: list[str]
) -> list[Any] | None:
    """One list-shaped container off a triage plan, or None when it is not a
    list — the container twin of :func:`_plan_str` (DW-155/DW-158).

    Every one of these sites used to iterate `container.get(field, [])`
    unscreened, so a JSON `null` (the likeliest wrong shape an LLM emits) raised
    `TypeError: 'NoneType' object is not iterable` straight out of
    `validate_triage` — past `_ensure_triage`'s live-session call site, which has
    no guard of its own, and past `decisions.pending_missed_decisions`, whose
    read loop catches only decode faults. A malformed plan is REFUSED through the
    `errors` channel that already exists; there is no repair path, and the caller
    never sees a `[]` fallback it could mistake for an empty section.

    `None` (not `[]`) is threaded for the reason `_plan_str` threads it rather
    than "": `[]` would re-enter the field's own emptiness/arity branch and
    double-report one fault (`bundle ... has no dw_ids`, `decision ... needs at
    least 2 options`). One error per fault is the convention here.

    The message names a POSITION (or the decision id, when that id is itself a
    string — since DW-157 a decision-level message can name the decision's own
    position instead) and the type name only, never the offending value's prose —
    these strings reach a journal. `where` is "" for a top-level section, whose
    field name already locates it.
    """
    value = container.get(field, [])
    if isinstance(value, list):
        return value
    prefix = f"{where}: " if where else ""
    errors.append(f"{prefix}{field} not a list: {type(value).__name__}")
    return None


def _plan_mapping(item: Any, where: str, errors: list[str]) -> dict[str, Any] | None:
    """One object-shaped member of a triage plan's list section — or of the
    result.json `mapping` list `validate_migration` walks, its second calling
    function since DW-190 — or None when it is not an object (DW-155/DW-158).

    Two unlike faults, one screen. In `validate_triage` each member loop called
    `.get` on whatever the list held, so a `null` or a bare string member raised
    `AttributeError` out of that validator and every caller of it; there
    `_normalize_bundle_names` runs first and already carries exactly this guard
    on the same members, so this is the check the validator was missing, not a
    new policy. `validate_migration`'s mapping loop has no such pre-pass and
    never crashed: it substituted `""` for the member's absent key and
    mis-diagnosed the shape fault as `mapping invents unknown key ''` (DW-190).

    Callers `continue` past a `None` while enumerating the RAW list, so a dropped
    member does not renumber the positions its siblings report. As with
    :func:`_plan_str` the message carries the position and the type name only.
    """
    if isinstance(item, dict):
        return item
    errors.append(f"{where} not an object: {type(item).__name__}")
    return None


def _plan_identifier(raw: Any, where: str, label_prefix: str) -> tuple[str, str, str]:
    """The three shapes one plan identifier takes: `(dw_id, shown, label)` —
    the IDENTITY, the bare subject a message interpolates, and the same subject
    behind its section prefix (DW-157/DW-171).

    The identity is `str(raw)` and stays that way: `id` members are deliberately
    NOT type-checked (the DW-145/148 Never clause), so what validates and what is
    refused is unchanged by this helper. What is screened is what gets PRINTED. An
    object-valued `id` would otherwise interpolate its own stringified contents
    into every message its loop emits, and this module promises those carry a
    POSITION and the type name only, never the offending value's prose — these
    strings reach a journal.

    A `str` id (the empty string included, since the fallback is keyed on the TYPE
    and not on truthiness) keeps today's wording byte for byte: `shown` is the id
    itself and `label` is `f"{label_prefix} {raw}"`. A non-string one is named by
    its position in both.

    `shown` and `label` are separate values because the two display shapes are:
    `claim`'s `appears in both` prints the subject BARE, while the loops'
    `has no evidence` / `names no blocker` / `gives no reason` / `has no question`
    messages print it behind a section prefix. For a string id the two collapse to
    the strings each site emitted before.

    The two BARE-only callers — a bundle's `dw_ids` members (DW-178) and
    `validate_migration`'s `mapping[i].dw_id` (DW-180) — have no section-prefixed
    message at all, so they pass `label_prefix=""` and discard `label`. Sites that
    print the subject `repr`-QUOTED are not this helper's: `mapping invents unknown
    key {k!r}` and `mapping repeats key {k!r}` go through `_shown_value`. Those stay
    byte-identical for a STRING key only, which is the whole of the parity this
    module promises: the old spelling was `repr(str(raw))`, so a non-string SCALAR
    key that printed `'5'` or `'None'` now prints `5` or `None` unquoted. That is
    accepted — `_shown_value(str(raw))` would restore the quoting only by putting an
    object key's stringified prose back into the message, which is the leak.
    `validate_migration`'s `manifest says ..., ledger disagrees` needs neither
    helper: it is reachable only once `source` AND `target` are both non-`None`,
    which proves its key is a genuine manifest key and its id a genuine ledger id —
    both strings by construction.
    """
    if isinstance(raw, str):
        return raw, raw, f"{label_prefix} {raw}"
    shown = f"{where} (id not a string: {type(raw).__name__})"
    return str(raw), shown, shown


def _shown_value(value: Any) -> str:
    """One LLM-authored value as a diagnostic prints it (DW-171).

    `repr` is kept for the flat scalars — `got None` and `got 'wrong'` are pinned
    wording. What that buys is NOT a length bound: an LLM-authored string is
    printed verbatim and can be arbitrarily long, which the byte-for-byte rule
    freezes here deliberately. What it buys is that a flat scalar has no NESTED
    structure to expose, and nesting is exactly the harm this screens: an object-
    or list-valued field used to print its whole contents — its keys included —
    into a message that reaches the journal. So anything non-scalar is named by
    its type alone.
    """
    if value is None or isinstance(value, (str, int, float)):
        return repr(value)
    return f"a {type(value).__name__}"


def validate_triage(
    rj: dict[str, Any] | None, expected_open_ids: set[str] | None
) -> tuple[TriagePlan | None, list[str]]:
    """Deterministic validation of the triage session's result.json. Returns
    (plan, []) or (None, errors). expected_open_ids=None skips the ledger
    equality check (used when reloading a previously validated plan)."""
    errors: list[str] = []
    if rj is None:
        rj = {}
    if not isinstance(rj, dict):
        # `rj = rj or {}` substituted only on a FALSY document, so every other
        # wrong-shape top level -- a list, a string, a number -- reached `.get`
        # and raised `AttributeError` out of every caller (DW-155). Refused
        # through the same channel as any other malformed plan, and BEFORE
        # `_normalize_bundle_names`, which also assumes a mapping.
        return None, [f"triage result not a JSON object: {type(rj).__name__}"]
    _normalize_bundle_names(rj)
    if rj.get("workflow") != TRIAGE_WORKFLOW:
        return None, [
            f"workflow must be {TRIAGE_WORKFLOW!r}: got {_shown_value(rj.get('workflow'))}"
        ]

    raw_open_ids = _plan_list(rj, "open_ids", "", errors)
    if raw_open_ids is None:
        # Early return, like the `workflow` and open-set-mismatch refusals around
        # it: the ledger-equality check below has nothing left to compare.
        return None, errors
    # Identity is `str(i)`, unchanged — the comparison below is byte for byte the
    # one it always was. What is derived alongside it is the DISPLAY name for each
    # claimed id, from the RAW member: `invented` is the half of the mismatch that
    # comes from the PLAN (`missed` comes from the ledger and is strings by
    # construction), so an object-valued `open_ids` member used to print its own
    # contents into a journaled message. First occurrence wins, so a duplicate
    # cannot rename the position its first spelling reported (DW-171).
    shown_open: dict[str, str] = {}
    for open_index, raw_open in enumerate(raw_open_ids):
        claimed_id = str(raw_open)
        if claimed_id in shown_open:
            continue
        shown_open[claimed_id] = (
            raw_open
            if isinstance(raw_open, str)
            else f"open_ids[{open_index}] (not a string: {type(raw_open).__name__})"
        )
    claimed_open = set(shown_open)
    if expected_open_ids is not None and claimed_open != expected_open_ids:
        missed = sorted(expected_open_ids - claimed_open)
        invented = sorted(shown_open[i] for i in claimed_open - expected_open_ids)
        return None, [
            "open_ids do not match the ledger's open entries"
            + (f"; missing: {', '.join(missed)}" if missed else "")
            + (f"; not open in the ledger: {', '.join(invented)}" if invented else "")
        ]
    universe = expected_open_ids if expected_open_ids is not None else claimed_open

    seen: dict[str, str] = {}  # id -> category that claimed it

    def claim(dw_id: str, category: str, subject: str) -> None:
        """`dw_id` is the plan's identity and keys `seen`; `subject` is what the
        error PRINTS. All five id-bearing loops pass an explicit subject derived
        by `_plan_identifier` — `decisions` (DW-157), `already_resolved`,
        `blocked`, `skip` (DW-171) and `bundles` (DW-178, which claims each
        member of `dw_ids` and names it `bundles[i] dw_ids[j]`) — so an
        object-valued `id` is named by its position instead of by its own
        stringified contents. Every one of them keeps `str(...)` as the IDENTITY
        that keys `seen` (the DW-145/148 Never clause); only the display is
        screened. `subject` is REQUIRED rather than defaulting to `dw_id`: that
        default is exactly how DW-178 happened — the bundles loop silently took
        it, and neither DW-157 nor DW-171 noticed the omission — so a sixth
        id-bearing loop must now name its subject or fail to typecheck rather
        than fail quietly. Byte-identical
        wording for a STRING id comes from `_plan_identifier`, which returns the
        id itself as the subject, not from any fallback here."""
        if dw_id not in universe:
            errors.append(f"{category} references unknown/closed id {subject}")
        elif dw_id in seen:
            errors.append(f"{subject} appears in both {seen[dw_id]} and {category}")
        else:
            seen[dw_id] = category

    resolved = []
    for resolved_index, raw_resolved in enumerate(
        _plan_list(rj, "already_resolved", "", errors) or []
    ):
        item = _plan_mapping(raw_resolved, f"already_resolved[{resolved_index}]", errors)
        if item is None:
            continue
        dw_id, id_shown, resolved_label = _plan_identifier(
            item.get("id", ""), f"already_resolved[{resolved_index}]", "already_resolved"
        )
        evidence = str(item.get("evidence", "")).strip()
        claim(dw_id, "already_resolved", id_shown)
        if not evidence:
            errors.append(f"{resolved_label} has no evidence")
        resolved.append(ResolvedEntry(dw_id, evidence))

    bundles = []
    names: set[str] = set()
    for bundle_index, raw_bundle in enumerate(_plan_list(rj, "bundles", "", errors) or []):
        # Positional, not by name: the name itself may be the non-string field,
        # so it cannot be the thing that identifies the bundle in an error. Same
        # label shape as `_normalize_bundle_names`, which ran above. Enumerating
        # the RAW list is what keeps a dropped member from renumbering its
        # siblings' positions.
        where = f"bundles[{bundle_index}]"
        item = _plan_mapping(raw_bundle, where, errors)
        if item is None:
            continue
        name = _plan_str(item, "name", where, errors)
        # `repr(name)` for every message that already named the bundle by name;
        # the position stands in when there is no name to print.
        label = repr(name) if name is not None else where
        if name is not None:
            if not BUNDLE_NAME_RE.match(name):
                errors.append(f"bundle name {name!r} invalid (want {BUNDLE_NAME_RE.pattern})")
            # The one rule BUNDLE_NAME_RE cannot express. A cycle-1 bundle's name IS its
            # directory (`_write_intent`), and the reserved Windows device basenames --
            # CON, NUL, AUX, PRN, COM<N>, LPT<N> -- are `[a-z0-9-]`-legal names that no
            # Windows filesystem will accept as one (matched case-insensitively, so
            # lowercase is no reprieve). Testing `safe_segment` identity rather than a
            # hand-written device list keeps this gate in lockstep with the sanitizer
            # that defines the set: the identical idiom, for the identical reason, as
            # `runs.is_valid_run_id`. Guarded on the match above so one bad name yields
            # one error and not two.
            if BUNDLE_NAME_RE.match(name) and safe_segment(name) != name:
                errors.append(f"bundle name {name!r} is not a legal path segment")
            if name in names:
                errors.append(f"duplicate bundle name {name!r}")
            # Only a string name is registered, so a type-failed bundle is invisible
            # to the option loop's `bundle_name in names` duplicate check. That gap
            # is covered by the refusal: its type error is already in `errors`, and a
            # non-empty `errors` returns `(None, errors)` before any duplicate could
            # matter.
            names.add(name)
        raw_dw_ids = _plan_list(item, "dw_ids", where, errors)
        # MEMBERS keep their `str(...)` treatment (DW-148 drew that line); only
        # the container is shape-checked. What `_plan_identifier` adds on top of
        # that identity is the DISPLAY name (DW-178): `dw_ids` below is still the
        # `str(...)` list, and still what feeds `Bundle` and the "has no dw_ids"
        # guard, while `claim` now prints an object-valued member by its POSITION
        # instead of its own stringified contents -- these messages reach the
        # journal. Enumerating the RAW list keeps positions stable, and the
        # `label_prefix` return is unused here because neither message `claim`
        # emits is section-prefixed. Guarded on the check having passed so a
        # `null` list does not also report "has no dw_ids".
        members = [
            _plan_identifier(raw_member, f"{where} dw_ids[{member_index}]", "")
            for member_index, raw_member in enumerate(raw_dw_ids or [])
        ]
        dw_ids = [identity for identity, _shown, _member_label in members]
        if raw_dw_ids is not None and not dw_ids:
            errors.append(f"bundle {label} has no dw_ids")
        for dw_id, id_shown, _member_label in members:
            claim(dw_id, f"bundle {label}", id_shown)
        intent = _plan_str(item, "intent", where, errors)
        if intent is not None:
            intent = intent.strip()
            if not intent:
                errors.append(f"bundle {label} has no intent")
        bundles.append(Bundle(name or "", tuple(dw_ids), intent or ""))

    blocked = []
    for blocked_index, raw_blocked in enumerate(_plan_list(rj, "blocked", "", errors) or []):
        item = _plan_mapping(raw_blocked, f"blocked[{blocked_index}]", errors)
        if item is None:
            continue
        dw_id, id_shown, blocked_label = _plan_identifier(
            item.get("id", ""), f"blocked[{blocked_index}]", "blocked"
        )
        blocker = str(item.get("blocker", "")).strip()
        claim(dw_id, "blocked", id_shown)
        if not blocker:
            errors.append(f"{blocked_label} names no blocker")
        blocked.append((dw_id, blocker))

    skip = []
    for skip_index, raw_skip in enumerate(_plan_list(rj, "skip", "", errors) or []):
        item = _plan_mapping(raw_skip, f"skip[{skip_index}]", errors)
        if item is None:
            continue
        dw_id, id_shown, skip_label = _plan_identifier(
            item.get("id", ""), f"skip[{skip_index}]", "skip"
        )
        reason = str(item.get("reason", "")).strip()
        claim(dw_id, "skip", id_shown)
        if not reason:
            errors.append(f"{skip_label} gives no reason")
        skip.append((dw_id, reason))

    decisions = []
    for decision_index, raw_decision in enumerate(_plan_list(rj, "decisions", "", errors) or []):
        item = _plan_mapping(raw_decision, f"decisions[{decision_index}]", errors)
        if item is None:
            continue
        raw_id = item.get("id", "")
        # Still the plan's identity, and still NOT type-checked: it keys `seen`
        # and becomes `Decision.id` (the DW-145/148 Never clause stands). What is
        # screened is what gets PRINTED. An object-valued `id` would otherwise
        # interpolate its own stringified contents into every message this loop
        # emits, and this module promises they carry type names only — these
        # reach a journal. So the two display shapes are derived ONCE, from the
        # raw value: a string id (the empty string included) keeps today's
        # wording byte for byte, a non-string one is named by its position.
        # `id_shown` is the bare subject `claim` interpolates; `decision_label`
        # is the prefix every other message in the loop carries (DW-157). The
        # derivation itself lives in `_plan_identifier`, shared with the three
        # section loops above since DW-171 — one definition of the idiom, not two.
        dw_id, id_shown, decision_label = _plan_identifier(
            raw_id, f"decisions[{decision_index}]", "decision"
        )
        claim(dw_id, "decisions", id_shown)
        # Type-checked rather than `str(...)`-ed (DW-156) for its live unscreened
        # sinks, all of them operator-facing or journaled: `DecisionPrompter.ask`
        # prints it, `gates.notify` announces it, the `decision-pending` journal
        # record carries it and `bmad-loop decisions --list` lists it. It reaches
        # `Bundle.decision_note` and `intent.md` too: `_agreeing_option` checks
        # option semantics, not the question, so this check closes that path.
        # Guarded on `is not None` so a non-string never also trips the emptiness
        # error — one error per fault.
        question = _plan_str(item, "question", decision_label, errors)
        if question is not None:
            question = question.strip()
            if not question:
                errors.append(f"{decision_label} has no question")
        options = []
        keys: set[str] = set()
        decision_bundle_names: set[str] = set()
        raw_options = _plan_list(item, "options", decision_label, errors)
        # Whether `keys` below is a faithful census of the options this decision
        # OFFERED. Only an object contributes a key, so a `null` container or a
        # dropped member leaves `keys` short and a perfectly good
        # `recommendation` would report `not an option` on top of the shape
        # error it is merely downstream of -- one fault, two errors, and a
        # re-driven triage session told to fix a field that was never wrong.
        # This is the `bundles`/`names` gap documented above, but not its
        # frequency: that one needs a second bundle to collide, while this one
        # fires on every shape-failed option a recommendation names.
        options_well_shaped = raw_options is not None
        for option_index, raw_option in enumerate(raw_options or []):
            raw = _plan_mapping(raw_option, f"{decision_label} options[{option_index}]", errors)
            if raw is None:
                options_well_shaped = False
                continue
            raw_key = raw.get("key", "")
            # Positional unless the key is a string, for the reason the `bundles`
            # loop is positional. `key` IS type-checked now (DW-156 — it is printed
            # among the options by `DecisionPrompter.ask` and `decisions --list`,
            # persisted into the answer store and written to the
            # `decision-answered` journal record), but the label still has to be
            # derived from the RAW value BEFORE that check runs: a failed check
            # leaves nothing to name the option with, and interpolating the raw
            # value would print an object key's own prose into a message this file
            # promises carries type names only — and these reach a journal. A
            # string key keeps today's wording byte for byte, empty ones included.
            where = (
                f"{decision_label} option {raw_key}"
                if isinstance(raw_key, str)
                else f"{decision_label} options[{option_index}]"
            )
            key = _plan_str(raw, "key", where, errors)
            if key is None:
                # `keys` is now short by one, exactly as a dropped member leaves
                # it short: a sound `recommendation` must not report `not an
                # option` on top of the fault it is merely downstream of.
                options_well_shaped = False
            # Every free-text scalar this option contributes downstream, screened
            # before any of them is read. A field that failed the type check is
            # None from here on, and each value check below is guarded on that --
            # one error per fault, never a type error plus the empty-value error
            # a "" fallback would also have tripped.
            effect = _plan_str(raw, "effect", where, errors)
            intent = _plan_str(raw, "intent", where, errors)
            if intent is not None:
                intent = intent.strip()
            option_label = _plan_str(raw, "label", where, errors)
            if option_label is not None:
                option_label = option_label.strip()
            resolution = _plan_str(raw, "resolution", where, errors)
            if resolution is not None:
                resolution = resolution.strip()
            bundle_name = _plan_str(raw, "bundle_name", where, errors)
            if key is not None:
                # The one site in this loop that still interpolates an identifier's
                # VALUE rather than a positional label. It is leak-free only because
                # `key` is `_plan_str`-screened above and so is known to be a string
                # here — not because the label is positional (DW-156 carries DW-157
                # at this site).
                if not key or key in keys:
                    errors.append(f"{decision_label}: missing/duplicate option key {key!r}")
                keys.add(key)
            if effect is not None and effect not in DECISION_EFFECTS:
                errors.append(f"{where}: bad effect {effect!r}")
            if effect == "build" and intent is not None and not intent:
                errors.append(f"{where}: effect 'build' needs intent")
            if bundle_name is not None:
                if bundle_name and not BUNDLE_NAME_RE.match(bundle_name):
                    errors.append(f"{where}: bad bundle_name {bundle_name!r}")
                # The second site that mints a bundle directory, gated for the reason
                # stated at the `bundles` loop above. A build-effect option's
                # `bundle_name` becomes `Bundle.name` in `_materialize_bundles`, so it
                # reaches `_write_intent`'s cycle-1 directory by the identical path --
                # `BUNDLE_NAME_RE` is no more able to express the rule here than there.
                # Guarded on the match above so one bad name yields one error, and on
                # nothing else: an absent `bundle_name` fails that match already.
                if BUNDLE_NAME_RE.match(bundle_name) and safe_segment(bundle_name) != bundle_name:
                    errors.append(
                        f"{where}: bundle_name {bundle_name!r} is not a legal path segment"
                    )
                if effect == "build" and bundle_name:
                    if bundle_name in names:
                        errors.append(f"duplicate bundle name {bundle_name!r}")
                    decision_bundle_names.add(bundle_name)
            options.append(
                DecisionOption(
                    key=key or "",
                    label=option_label or key or "",
                    effect=effect or "",
                    intent=intent or "",
                    resolution=resolution or "",
                    bundle_name=bundle_name or "",
                )
            )
        names.update(decision_bundle_names)
        # The RAW length, not the surviving one: a dropped member already
        # reported its own fault and must not also trip the arity error.
        if raw_options is not None and len(raw_options) < 2:
            errors.append(f"{decision_label} needs at least 2 options")
        recommendation = _plan_str(item, "recommendation", decision_label, errors)
        # Guarded on the option shapes for the reason stated at the loop above:
        # against a short `keys` this check reports a fault the plan does not
        # have. A recommendation that really is bogus is still refused on the
        # next pass, once the options are objects.
        if recommendation is not None and options_well_shaped and recommendation not in keys:
            errors.append(f"{decision_label}: recommendation {recommendation!r} not an option")
        context = _plan_str(item, "context", decision_label, errors)
        decisions.append(
            Decision(
                dw_id,
                question or "",
                (context or "").strip(),
                tuple(options),
                recommendation or "",
            )
        )

    unclaimed = sorted(universe - set(seen))
    if unclaimed:
        # Sorted on the IDENTITY as ever, joined on the DISPLAY name (DW-179).
        # `universe` is a subset of `shown_open`'s keys by construction — with
        # `expected_open_ids is None` universe IS `set(shown_open)`, and otherwise
        # the equality check above already returned on any mismatch — so the
        # direct index is total, the same invariant `invented` relies on.
        errors.append(f"open entries not triaged: {', '.join(shown_open[i] for i in unclaimed)}")

    if errors:
        return None, errors
    return (
        TriagePlan(
            open_ids=frozenset(universe),
            already_resolved=tuple(resolved),
            bundles=tuple(bundles),
            blocked=tuple(blocked),
            skip=tuple(skip),
            decisions=tuple(decisions),
        ),
        [],
    )


# ---------------------------------------------------------- migration plan


@dataclass(frozen=True)
class PreCanonical:
    """What a pre-existing canonical entry is held to across a migration.

    Status alone was the whole snapshot until #519, and that is what let a
    rewrite drop a ``gate:`` line and pass: the gate is the one field whose loss
    is both silent and unsafe. ``deferred-work-format.md`` calls removing it
    "the exact failure this field exists to prevent", and
    ``Engine._refuse_gated_story`` then dispatches the story the entry was
    holding back.

    ``gate_tokens`` unions :attr:`~bmad_loop.deferredwork.EntryGates.tokens`
    with ``malformed`` because the question is "did a token the entry declared
    survive", not "was it enforceable". A malformed ``gate: 3.2`` gates nothing,
    but it reads to anyone scanning the entry as a gate in force and ``validate``
    reports it (``deferred.hard-gate-unstructured``); dropping it retires that
    report silently, which is the same failure one level down.

    The counts ``EntryGates`` also carries — ``lines``, ``empty``, ``near_miss``
    — are deliberately NOT snapshotted. None of them names a story, so losing one
    cannot change which story is gated, and they are exactly what a legitimate
    reflow of a multi-line declaration moves.
    """

    status: str
    gate_tokens: tuple[str, ...]
    severity: str | None


def snapshot_canonical(text: str) -> dict[str, PreCanonical]:
    """The pre-migration state ``validate_migration`` holds the rewrite to.

    A named function rather than a comprehension inlined at its one call site so
    that the tests grade the snapshot production actually builds: a hand-written
    ``{"DW-1": PreCanonical("open", ("3-2",), "high")}`` would pass whatever the parser
    really produces for that entry, and the bug being fixed here lived in the
    snapshot, not in the comparison.

    Keying by id is safe only because ``_ensure_migration`` refuses a ledger
    that carries duplicate canonical ids before any rewrite is attempted. Do
    not soften that refusal into per-id collapse-hardening here: tokens and
    status snapshotted independently describe an entry that never existed, and
    each half patched on its own opens the next cross-product (a token
    harvested from a ``done`` twin paired with an ``open`` twin's status both
    refuses a faithful rewrite and newly gates a story that was not gated).
    """
    snapshot: dict[str, PreCanonical] = {}
    for e in deferredwork.parse_ledger(text):
        g = deferredwork.gates(e)
        snapshot[e.id] = PreCanonical(e.status, g.tokens + g.malformed, e.severity)
    return snapshot


def duplicate_ids(entries: Iterable[deferredwork.DWEntry]) -> list[str]:
    """The DW ids naming more than one entry, sorted.

    One function for both sides of a migration on purpose: the rewrite is
    refused when the ledger it STARTED from carries duplicates and when the
    ledger it produced does, and two detectors that disagreed about what counts
    as a duplicate would leave exactly the gap between them open.
    """
    seen: set[str] = set()
    dupes: set[str] = set()
    for e in entries:
        (dupes if e.id in seen else seen).add(e.id)
    return sorted(dupes)


def validate_migration(
    rj: dict[str, Any] | None,
    manifest: list[dict[str, Any]],
    pre_canonical: dict[str, PreCanonical],
    new_text: str,
) -> list[str]:
    """Deterministic validation of a legacy-ledger migration session: the
    rewritten ledger must contain zero legacy items, preserve every
    pre-existing canonical entry's status and every ``gate:`` token it
    declared, continue DW numbering, and the result.json mapping must cover
    the manifest exactly. Returns errors, empty on success."""
    if rj is None:
        rj = {}
    if not isinstance(rj, dict):
        # The DW-155 guard `validate_triage` carries one function over, which this
        # twin was left without (DW-170): `rj = rj or {}` substituted only on a
        # FALSY document, so every other wrong-shape top level -- a list, a string,
        # a number -- reached `.get` and raised `AttributeError` out of THIS
        # function. When DW-170 wrote this guard it bought totality for this
        # function's own callers only, NOT a live crash fix: `_ensure_migration`,
        # the only production caller, could not deliver a non-dict here, because
        # `Engine._run_session` dereferenced `result.result_json.get(...)` behind
        # an `is not None` check alone -- a truthy non-dict raised THERE, inside
        # `_run_session`, before this guard was reached. DW-206 routed that frame
        # through `model.result_mapping` while leaving the document itself
        # untouched, so the migration lane now runs to this guard and it answers
        # a real shape rather than an unreachable one -- the same reachability
        # the `validate_triage` twin gained. Refused through the existing
        # `errors` channel; never raised, never repaired.
        return [f"migration result not a JSON object: {type(rj).__name__}"]
    if rj.get("workflow") != MIGRATE_WORKFLOW:
        return [f"workflow must be {MIGRATE_WORKFLOW!r}: got {_shown_value(rj.get('workflow'))}"]
    errors: list[str] = []

    leftovers = deferredwork.parse_legacy(new_text)
    if leftovers:
        listed = "; ".join(f"{e.section or 'top level'}: {e.title[:60]}" for e in leftovers[:10])
        errors.append(f"{len(leftovers)} legacy item(s) still parse as legacy: {listed}")

    parsed = deferredwork.parse_ledger(new_text)
    entries: dict[str, deferredwork.DWEntry] = {e.id: e for e in parsed}
    dupes = duplicate_ids(parsed)
    if dupes:
        errors.append("duplicate DW ids: " + ", ".join(dupes))

    def first_word(status: str) -> str:
        return status.split()[0] if status.split() else ""

    pre_max = max(
        (dw_id.removeprefix("DW-") for dw_id in pre_canonical),
        key=decimal_digits_key,
        default="0",
    )
    pre_max = decimal_digits_key(pre_max)[1]
    for dw_id, pre in pre_canonical.items():
        e = entries.get(dw_id)
        if e is None:
            errors.append(f"pre-existing {dw_id} disappeared")
            continue
        if first_word(e.status) != first_word(pre.status):
            errors.append(f"pre-existing {dw_id} status changed: {pre.status!r} -> {e.status!r}")
        if e.severity != pre.severity:
            errors.append(
                f"pre-existing {dw_id} severity changed: {pre.severity!r} -> {e.severity!r}"
            )
        # Drops and edits only; an ADDED token is deliberately accepted. The two
        # directions are not the same failure: a dropped token un-gates a story
        # silently, which is what #519 is about, while an added one over-blocks
        # loudly and in the safe direction — the operator meets a refusal naming
        # the entry. Refusing an addition would spend one of the two migration
        # attempts on the only direction that cannot cause the failure this
        # guard exists to stop. An EDITED token is caught here anyway: an edit
        # is a drop plus an add, and the drop half is what this reads.
        post = deferredwork.gates(e)
        kept = set(post.tokens) | set(post.malformed)
        lost = [t for t in pre.gate_tokens if t not in kept]
        if lost:
            errors.append(f"pre-existing {dw_id} lost gate token(s): {', '.join(lost)}")
    for dw_id, e in entries.items():
        if dw_id in pre_canonical:
            continue
        if decimal_digits_key(dw_id.removeprefix("DW-")) <= decimal_digits_key(pre_max):
            errors.append(f"new entry {dw_id} does not continue numbering past DW-{pre_max}")
        if first_word(e.status) not in ("open", "done"):
            errors.append(f"new entry {dw_id} has status {e.status!r}; want open or done")

    manifest_by_key = {str(m["key"]): m for m in manifest}
    mapping = rj.get("mapping", [])
    if not isinstance(mapping, list):
        return errors + ["mapping must be a list of {key, dw_id}"]
    seen_keys: set[str] = set()
    target_by_key: dict[str, str] = {}
    sources_by_target: dict[str, list[dict[str, Any]]] = {}
    # Enumerated for the POSITION only: `key` and `dw_id` keep their `str(...)`
    # identities, so what maps, what is refused and what `seen_keys` records are
    # unchanged (DW-180). Screened is what gets PRINTED, because these errors
    # reach the migrate-decision journal record. The two display shapes split by
    # the wording each message already had: `invents unknown key` / `repeats key`
    # print the key `repr`-quoted, which `_shown_value` reproduces for a string,
    # while `no such entry` prints the id bare, which is `_plan_identifier`'s
    # `shown`. Both collapse to today's bytes for a STRING value and only for one:
    # the key half was `repr(str(raw))`, so a non-string SCALAR key that printed
    # `'5'` now prints `5` unquoted (see `_plan_identifier` for why that trade is
    # taken). Two messages here need no positional treatment at all, for the same
    # reachability reason: `repeats key` is past the `source is None` `continue`
    # and `manifest_by_key`'s keys are `str()`-forced, so its key is provably a
    # genuine manifest key — it is converted for UNIFORMITY with its sibling, not
    # from need — and `manifest says ..., ledger disagrees` is left alone outright
    # because `source` AND `target` are both non-`None` by the time it is
    # reachable, so its key and its id are both provably genuine.
    # `_plan_mapping` screens each member FIRST, as every `validate_triage` loop
    # does — a port of the DW-155/DW-158 guard, not a new policy. Without it
    # `item.get("key", "") if isinstance(item, dict) else ""` handed a `null` or
    # bare-string member to `manifest_by_key.get("")`, printing a shape fault as
    # `mapping invents unknown key ''` (DW-190). The trailing `manifest keys not
    # mapped:` error a drop provokes is the one the fall-through already
    # produced: only the first string of that two-element list changes.
    for item_index, item in enumerate(mapping):
        entry = _plan_mapping(item, f"mapping[{item_index}]", errors)
        if entry is None:
            continue
        raw_key = entry.get("key", "")
        raw_dw_id = entry.get("dw_id", "")
        key = str(raw_key)
        shown_key = _shown_value(raw_key)
        dw_id, shown_dw_id, _dw_id_label = _plan_identifier(
            raw_dw_id, f"mapping[{item_index}].dw_id", ""
        )
        source = manifest_by_key.get(key)
        if source is None:
            errors.append(f"mapping invents unknown key {shown_key}")
            continue
        if key in seen_keys:
            errors.append(f"mapping repeats key {shown_key}")
        seen_keys.add(key)
        target = entries.get(dw_id)
        if target is None:
            errors.append(f"mapping {key} -> {shown_dw_id}: no such entry in the ledger")
            continue
        # Recorded for the manifest-order pass below only once the id resolved
        # to a ledger entry: past this point `dw_id` is a key of `entries`, so
        # the bare `{target}` that pass prints is provably genuine (the same
        # reasoning `manifest says ..., ledger disagrees` relies on), and an id
        # `no such entry` already refused is not reported a second time as an
        # ordering fault.
        target_by_key.setdefault(key, dw_id)
        if dw_id in pre_canonical:
            errors.append(
                f"mapping {key} -> {dw_id}: legacy items must map to newly created entries"
            )
        else:
            sources_by_target.setdefault(dw_id, []).append(source)
            if (first_word(target.status) == "done") != bool(source["done"]):
                want = "done" if source["done"] else "open"
                errors.append(f"mapping {key} -> {dw_id}: manifest says {want}, ledger disagrees")
    for dw_id, sources in sources_by_target.items():
        target = entries[dw_id]
        source_severities = [source.get("severity") for source in sources]
        present = [severity for severity in source_severities if severity is not None]
        expected = max(present, key=SEVERITY_ORDER.__getitem__) if present else None
        if target.severity != expected:
            if len(sources) == 1:
                key = str(sources[0]["key"])
                errors.append(
                    f"mapping {key} -> {dw_id}: manifest severity "
                    f"{expected!r}, ledger has {target.severity!r}"
                )
            else:
                errors.append(
                    f"merged mapping -> {dw_id}: highest manifest severity "
                    f"{expected!r}, ledger has {target.severity!r}"
                )
    missing = sorted(set(manifest_by_key) - seen_keys)
    if missing:
        errors.append("manifest keys not mapped: " + ", ".join(missing))

    # Dry-run projects legacy ids in manifest/file order.  Hold the rewrite to
    # that same contiguous allocation so a selected provisional id cannot name
    # a different issue after migration.  Equal adjacent targets are the one
    # permitted exception: migration mode may merge duplicate legacy items,
    # including nonadjacent items, onto any target allocated earlier.
    expected_suffix = increment_decimal_digits(pre_max)
    allocated_targets: set[str] = set()
    for manifest_item in manifest:
        key = str(manifest_item["key"])
        target = target_by_key.get(key)
        if target is None:
            continue
        if target in allocated_targets:
            continue
        expected_target = f"DW-{expected_suffix}"
        if target != expected_target:
            errors.append(
                f"mapping {key} -> {target}: migration ids must follow manifest order; "
                f"expected {expected_target}"
            )
        allocated_targets.add(target)
        expected_suffix = increment_decimal_digits(expected_suffix)
    return errors


# --------------------------------------------------------------- prompting


class DecisionPrompter:
    """Walks the human through pending decisions on the terminal. Injection
    points exist so tests can script answers.

    The interactive terminal prompt is the v1 protocol: observers (the TUI
    dashboard, ATTENTION watchers) learn a sweep is blocked from the
    decision-pending journal event written just before ask() and attach to
    the sweep's tmux window to answer. A decisions-file protocol — engine
    writes the pending question to a file and polls for an answer the TUI
    could write in-app — is deliberately deferred to v2; it needs timeout +
    ownership semantics this run-blocking prompt avoids."""

    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        print_fn: Callable[[str], None] = print,
    ):
        self.input_fn = input_fn
        self.print_fn = print_fn

    def ask(self, decision: Decision) -> DecisionOption:
        p = self.print_fn
        p("")
        p(f"── decision needed: {decision.id} " + "─" * 30)
        p(decision.question)
        if decision.context:
            p("")
            p(decision.context)
        p("")
        for opt in decision.options:
            marker = "  (recommended)" if opt.key == decision.recommendation else ""
            p(f"  [{opt.key}] {opt.label} — {opt.effect}{marker}")
            if opt.intent:
                p(f"      {opt.intent}")
        keys = [o.key for o in decision.options]
        while True:
            raw = self.input_fn(
                f"choice [{'/'.join(keys)}] (enter = {decision.recommendation}): "
            ).strip()
            if not raw:
                raw = decision.recommendation
            chosen = decision.option(raw)
            if chosen is not None:
                return chosen
            p(f"  invalid choice {raw!r}")


# ------------------------------------------------------------ sweep engine


def _rearm_generation(task: StoryTask) -> None:
    """Open a new session-id generation for a sweep task restarting from ESCALATED.

    The restart resets ``attempt`` to 0 for a fresh budget, and that reset is exactly
    what makes the next dispatch re-mint ``attempt == 1`` — an id byte-equal to the
    abandoned attempt's, since ``engine._session_task_id`` emits its discriminator only
    above zero. The artifact a shared id corrupts is ``tasks/<id>/escalation.json``: the
    sweep skill writes it, and two records carrying one id both name that one mutable
    file, so the abandoned cycle's escalation is the fresh session's too.
    ``resolve._gather_escalations`` now opens each distinct ``task_id`` once and
    de-duplicates entries by content, so it no longer reports the same aliased file
    twice. Both adapters also unlink cycle outputs in ``start_session``, which stops a
    healthy restart from inheriting stale contents — but cleanup still leaves the two
    historical records naming one mutable directory: a healthy restart erases the
    abandoned cycle's artifact, while a re-escalation replaces it for both records.
    Minting a fresh id is what preserves one artifact namespace per recorded cycle.

    Same pattern as ``runs.rearm_escalation``, DIFFERENT reason: #705's harm is
    ``_resumable_session`` verdict replay, which runs only on the dev/review phases and
    never reaches ``TRIAGE_RUNNING``/``TRIAGE_VERIFY``. ``cmd_resolve`` *can* reach a
    sweep task (``_escalate`` raises with ``PAUSE_ESCALATION`` and a story key, which
    the engine persists), and its own bump there is harmless: the re-arm leaves the task
    PENDING, so this restart arm does not fire on top of it.

    Call ONLY where the task is taking a genuinely fresh attempt budget: the
    ``Phase.ESCALATED`` restart arms, and ``Sweep._reset_superseded_bundle_state``
    (a reset bundle task adopting a DIFFERENT bundle's ids never attempted that
    bundle at all). An ordinary non-escalated restart keeps its attempt counter, so
    ``attempt += 1`` already yields a fresh id; bumping there would move the
    namespace for nothing and break the "every id already on disk stays
    byte-identical" property the suffix rule exists to hold.
    """
    task.generation += 1


# The launch overrides a sweep run resolves against `[sweep]` policy. `sweep.json`
# persists each as nullable (None = "not given"); `SweepEngine.__init__` and
# `bmad-loop status` both resolve through `resolve_sweep_override`.
SWEEP_OVERRIDE_KEYS = ("max_bundles", "repeat", "max_cycles")

_T = TypeVar("_T")


def resolve_sweep_override(override: _T | None, policy_value: _T) -> _T:
    """The value a sweep run enforces for one of `SWEEP_OVERRIDE_KEYS`: its own
    override when one was given, else the policy's. Tested with `is not None`,
    never truthiness — an explicit `repeat=False` or `max_bundles=0` is an
    override, not an absence."""
    return override if override is not None else policy_value


# Bound on any artifact-derived text a session-failure diagnostic carries (#752):
# the journal records an error summary, never the document itself.
_DIAGNOSTIC_TEXT_LIMIT = 400


def _bounded(text: str, limit: int = _DIAGNOSTIC_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _triage_verdict(doc: dict[str, Any], open_now: set[str] | None) -> list[str]:
    """`validate_triage`'s errors for ``doc`` as the completed path would grade it —
    bundle names normalized first — on a private copy, so a diagnostic read never
    repairs the artifact it inspects."""
    graded = copy.deepcopy(doc)
    _normalize_bundle_names(graded)
    return validate_triage(graded, open_now)[1]


def _hook_verdict(kinds: set[str]) -> str:
    if not kinds:
        return "none"
    if "Stop" in kinds:
        return "stop"
    if "SessionStart" in kinds:
        return "session-start-without-stop"
    return "no-session-start-or-stop"


def _diagnostic_suffix(diagnostic: dict[str, Any] | None) -> str:
    """The escalation-text tail for a non-completed session's diagnostic (#752):
    what was on disk and which hook events arrived, so an operator whose event
    channel is miswired debugs the channel, not the agent's output."""
    if diagnostic is None:
        return ""
    artifact = str(diagnostic["artifact"])
    if diagnostic.get("artifact_error"):
        artifact += f" ({diagnostic['artifact_error']})"
    return f" [result.json: {artifact}; hook events: {diagnostic['hook_events']}]"


class SweepEngine(Engine):
    """Engine variant whose loop processes the deferred-work ledger instead
    of sprint-status. Bundles reuse the inherited story pipeline through the
    override seams; the triage session has its own phase pair."""

    def __init__(
        self,
        *args: Any,
        triage_adapter: Any = None,
        prompting: bool = False,
        decisions_only: bool = False,
        max_bundles: int | None = None,
        repeat: bool | None = None,
        max_cycles: int | None = None,
        only_ids: tuple[str, ...] | None = None,
        min_severity: str | None = None,
        prompter: DecisionPrompter | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.adapters["triage"] = (
            triage_adapter if triage_adapter is not None else self.adapters["dev"]
        )
        # `Engine.__init__` attached the journal to the adapters it knew about; a
        # distinct triage adapter arrives after that, so attach it here too or every
        # sweep triage session would emit no `session-idle`/`session-active` (#680).
        self.adapters["triage"].journal = self.journal
        self.prompting = prompting
        self.decisions_only = decisions_only
        self.max_bundles = resolve_sweep_override(max_bundles, self.policy.sweep.max_bundles)
        self.repeat = resolve_sweep_override(repeat, self.policy.sweep.repeat)
        self.max_cycles = resolve_sweep_override(max_cycles, self.policy.sweep.max_cycles)
        self.only_ids = only_ids
        self.min_severity = min_severity
        self._selection_started = self.state.sweep_cycle > 1 or any(
            key == TRIAGE_KEY or key.startswith(f"{TRIAGE_KEY}-") or BUNDLE_KEY_RE.match(key)
            for key in self.state.tasks
        )
        self.prompter = prompter or DecisionPrompter()
        # The two decision quarantines — ids already journaled as skipped, and
        # ids whose recorded answer was already journaled as DROPPED (and
        # notified) — live on `state` (`sweep_skipped_decisions` /
        # `sweep_dropped_decisions`), not here. Since DW-200 a THIRD sweep list
        # rides beside them, `sweep_unlanded_decisions`, and it is not a
        # quarantine: it records a VERDICT (`record_decision` wrote no line for
        # this `build` answer) rather than an announcement. It is persisted for
        # the same reason these two are — the observation and its consumer sit in
        # different phases, so an interruption between them must not lose it —
        # and for no other. Its bound is narrower than a quarantine's, and stated
        # exactly: a drop that ANNOUNCES an id clears that id, and nothing else
        # does. An id whose decision a later cycle's fresh triage no longer
        # raises, or one already in `sweep_dropped_decisions` when the lane is
        # re-entered, therefore stays on the list for the life of the run. That
        # is harmless rather than a leak — the list is only ever read at the
        # build lane, which is never reached for an id this cycle's plan does not
        # raise — so no clearing sweep is worth the second write path it would
        # add.
        #
        # Without the two quarantines a persistent decision item notifies once
        # per repeat cycle, and a cycle whose re-triage happens to mint an
        # AGREEING option revives a decision the operator was already told had
        # been dropped: `_materialize_bundles` leaves the run-level `answers`
        # entry alone (it is the human's recorded answer and stays auditable on
        # disk), so `_decisions_phase` re-reads it every cycle. They are
        # persisted run state (DW-124), deliberately:
        # the disposition is the RUN's — so a pause/resume of the same run must
        # not re-announce it, while a NEW run re-evaluates from scratch — and
        # the answer is the human's.
        #
        # The undecodable-prune carry (DW-182/186) is the exact opposite call, and
        # the contrast is the argument for both. It lives HERE, on the instance,
        # never on `state`: its only consumer is the `_loop` frame that just called
        # `_cycle`, and it has to reach that frame because the repeat boundary
        # below it COMMITS the ledger — a refusal that stays inside the prune
        # publishes bytes nobody could decode and crashes cycle N+1 on them
        # anyway. Persisting it would encode a decision no resume can reach:
        # `_loop`'s own read of the identical bytes ends the run at the top of the
        # cycle body (it degrades through `_read_cycle_ledger` since DW-197, where
        # it used to raise — the conclusion is the same either way), so a resume of
        # this run never gets far enough to read the flag. The quarantines above are
        # persisted because their disposition outlives the frame that made it; this
        # one cannot outlive it at all. That argument is about UNDECODABLE bytes
        # and holds only for them; see `_record_ledger_doubt` for the class it does
        # not cover, where the doubt IS persisted for the resume this one says
        # cannot happen. The two rationales do not contradict each other.
        self._prune_ledger_unreadable = False
        # DW-197's sibling latch, carrying the prune's OS-fault refusal to `_loop`
        # for exactly the reasons the one above carries the undecodable one. A
        # SECOND flag rather than a widened `_prune_ledger_unreadable`, on two
        # counts: the two stops report DIFFERENT closed tokens (`ledger-unreadable`
        # is "bytes nobody could decode", `ledger-inaccessible` is "the OS refused
        # the read", and `reason`/`error` are both dropped from diagnose dumps, so
        # the token is the only thing that survives one), and
        # `test_every_repeat_stop_pairs_its_reason_with_the_same_stop_cause` parses
        # this module and requires a LITERAL `reason=`/`stop_cause=` pair at every
        # `sweep-repeat-done` write — one write carrying a variable token would gut
        # that guard. Instance state, never `state`, for the identical reason.
        self._prune_ledger_inaccessible = False
        # This cycle's decision-effect verdict. Instance state lets `_cycle` and
        # `_loop` share the latch without changing the decision-phase return tuple.
        # Unlike the prune latch, this also covers faults on decodable bytes.
        self._ledger_in_doubt = False
        # DW-216. `_close_resolved`'s half of the same verdict, on a latch of its
        # OWN rather than folded into the one above. The reason is the publish
        # spelling at the bottom of `_decisions_phase`: it assigns
        # `self._ledger_in_doubt = ledger_in_doubt` with `=`, not `|=`, and
        # deliberately — a healthy decision phase must CLEAR a doubt an earlier
        # cycle raised, or one bad cycle would withhold every bundle for the rest
        # of the run. `_close_resolved` runs AHEAD of that assign in the same
        # cycle, so an OR at the arming site would simply be overwritten by a
        # healthy phase and the close-phase fault would vanish. Cycle-scoped like
        # the verdict it joins: reset at the top of `_close_resolved`, which runs
        # once per cycle and first. Read only through `_ledger_unfit_to_publish`.
        self._close_ledger_in_doubt = False
        # DW-218/219. Whether the run-scoped mirror was already True in the
        # `state.json` this engine was built from — i.e. whether some PREVIOUS
        # process formed the verdict. Read once, here, because `_release_ledger_doubt`
        # has to tell an arm THIS walk made (which a later landed effect legitimately
        # disproves) from one it merely inherited (which nothing this process observes
        # can speak to). Sampled before any phase runs, so no arm of this run's own
        # can contaminate it.
        self._ledger_doubt_inherited = self.state.sweep_ledger_in_doubt
        self.state.run_type = "sweep"

    def _ledger_unfit_to_publish(self) -> bool:
        """This cycle's verdict from BOTH phases that can raise it (DW-216/217/220),
        OR the RUN's persisted one (DW-218/219).

        The dispatch gate in `_cycle`, `_loop`'s shared stop arm,
        `_prune_pre_answers`' refusal, `_decisions_phase`'s tail publish, and every
        ledger PUBLISHER a resume can reach ahead of that gate — `_close_resolved`'s
        two commit arms, `_loop`'s in-flight recovery pass and the publish that
        follows it (the debt settle included), `_loop`'s legacy-migration arm,
        `_publish_stranded_close` (DW-250: the whole publisher, above both of its
        probes) — and `_loop`'s no-open repair notice (DW-251) all read the doubt
        through here, so a future arming site cannot be wired into one reader and
        missed by the others — which is precisely how the close phase's fault
        reached `_write_intent`'s bare `read_for_write` while the gate above it saw
        a False latch. The persisted term is added HERE for the same reason: one
        reader, however many consumers, no second spelling to keep in step. The
        list above is not a count to keep exact; the invariant is that nothing
        reads `state.sweep_ledger_in_doubt`, `_ledger_in_doubt` or
        `_close_ledger_in_doubt` except through this method.

        The persisted term is safe beside the two cycle-scoped ones. `_loop` stops
        (repeat) or returns (non-repeat, `--decisions-only`) when doubt remains
        armed at the cycle boundary, so no later cycle inherits that doubt inside
        this process. `_release_ledger_doubt` clears the mirror when a later effect
        lands, but only for an arm THIS process made and while the close phase's
        latch is clear. A cycle that releases its doubt can continue repeating.
        Across a RESUME, the inherited mirror preserves a verdict the instance
        latches lost — the defect it exists for.
        """
        return (
            self.state.sweep_ledger_in_doubt or self._ledger_in_doubt or self._close_ledger_in_doubt
        )

    def _quarantine(self, ids: list[str], dw_id: str) -> None:
        """Add `dw_id` to one of `state`'s decision quarantines if absent, and
        persist immediately — mirroring `Engine._run_auto_sweep`'s
        mutate-then-`_save()` latch, since the whole point of the list is that a
        resume of this run sees it.

        Every ANNOUNCING call site runs this AFTER its journal row and its notify,
        so the residual crash window (announced, not yet persisted) resumes into a
        re-announcement rather than into a silent quarantine — the safe
        direction for a record an operator reads.

        `sweep_unlanded_decisions` (DW-200) is the one list this writes that
        announces nothing: it records a VERDICT the moment `record_decision`
        reports no `decision:` line, ahead of the drop that later announces it, so
        the ordering rule above has no notify to be after. The same
        mutate-then-`_save()` latch is what the verdict needs, which is why it
        goes through this helper rather than an inline append."""
        if dw_id not in ids:
            ids.append(dw_id)
        self._save()

    def _record_ledger_doubt(self) -> None:
        """Mirror a cycle's ledger-publication doubt onto RUN state (DW-218/219).

        The same mutate-then-`_save()` latch `_quarantine` takes, and taken at
        EVERY ARMING SITE rather than at `_cycle`'s dispatch gate or at
        `_decisions_phase`'s tail publish: the window that loses the verdict opens
        the instant a latch is armed and closes only when the gate has reported.
        DW-219's trigger is the `_check_stop_request()` the withheld branch takes
        AHEAD of its `sweep-bundles-withheld` row; DW-218's is any crash in the
        same span. A write at the gate would leave both open, and a write at the
        tail publish alone leaves the LONGEST span of all open: the walk's own
        interactive arm arms and then `continue`s into an iteration that blocks on
        `prompter.ask`, so a stop or crash at a LATER decision's prompt ends the
        process with the verdict still only in memory.

        Arming in-walk does not make the run-scoped flag stickier than the latch it
        mirrors — outside the two states `_release_ledger_doubt` refuses to clear,
        where it is stickier on purpose (see that helper's guard paragraphs).
        `_decisions_phase`'s commit gate and `_cycle`'s dispatch gate both read the
        mirror through `_ledger_unfit_to_publish()`, so a mirror that outlived a
        doubt the walk went on to disprove would withhold a commit and a dispatch
        that the local latch had already released. `_release_ledger_doubt` is the
        counterpart, called from the two sites that clear the local latch — and it
        clears only for an arm THIS process made, refusing while the close phase's
        latch is armed this cycle or while the mirror was inherited from a previous
        process.

        DW-218's own stated repro — `_prune_pre_answers` letting an `OSError`
        propagate out of the cycle — is no longer reachable, and the entry is kept
        for its CLASS rather than that path: DW-197 degrades that read to a latch
        and a journal row, and DW-217's `ledger-in-doubt` refusal returns ahead of
        the store write. A reader chasing the entry's repro would be hunting a path
        that no longer exists; the crash window this closes is the general one.

        Why this verdict is persisted where `_prune_ledger_unreadable` is not, so
        the two rationales cannot be read as contradicting each other. The prune
        latch is unpersisted because a resume of the same run meets the identical
        bytes at `_loop`'s own top-of-cycle read and ends the run there, so no
        resume can get far enough to read a persisted flag. That argument holds
        only for bytes NOBODY CAN DECODE. This verdict also arms on `OSError` /
        `ValueError` / `StateRootError` and on a write that half-landed — none of
        which say the bytes fail to decode. On that class the resume's re-read
        PASSES, `pending` filters the stored answer out (it is already in
        `answers`), the DW-167 re-apply walk drops the id because the half-write
        already flipped it to `done`, and `_decisions_phase` re-assigns `False`. So
        the resumed cycle sails past every in-memory arm and dispatches — the exact
        resume the prune argument says cannot happen.

        Only on the False->True EDGE, so the nine call sites (the close phase's
        degrade, the five arms inside the decision phase, that phase's tail
        publish, `_commit_ledger`'s ledger-family refusal arm since DW-244, and
        its ledger-family resolve-fault arm since DW-260) cost one `_save()`
        per edge rather than one per arm — an edge, not a run: `_release_ledger_doubt`
        writing False re-opens it, so a fault/success/fault sequence inside one walk
        saves here more than once. That helper owns the lifetime from here: it clears
        the mirror for an arm THIS process made, while an arm inherited across a
        resume or raised by the close phase stays until a human edits the ledger and
        starts a fresh `bmad-loop sweep`, which is a new run with fresh state.
        """
        if not self.state.sweep_ledger_in_doubt:
            self.state.sweep_ledger_in_doubt = True
            self._save()

    def _release_ledger_doubt(self) -> None:
        """The counterpart to `_record_ledger_doubt`, and the reason arming at the
        in-walk sites does not make the mirror STICKIER than the latch it mirrors —
        outside the two states the guards below refuse to clear, where it is
        stickier deliberately.

        `_decisions_phase`'s local `ledger_in_doubt` is the LAST attempt's verdict,
        not the walk's: an effect that lands afterwards proves the ledger reads and
        writes again, so the walk clears it and the cycle dispatches. Mirroring at
        the arm without mirroring that clear would make the run-scoped flag sticky
        in a way the latch never was — the withheld dispatch
        `test_an_effect_landing_after_the_reapply_gate_refusal_clears_the_doubt`
        ablates against by name. So this is called from exactly the two sites that
        clear the local latch, on exactly the proof those sites rest on.

        It does not reopen DW-218/219's window: the crash class those entries name
        is a process that dies BETWEEN an arm and the next successful land, and
        across that span the mirror is on disk. What clears is a doubt the walk
        itself went on to disprove.

        Guarded on `_close_ledger_in_doubt` because that latch is a DIFFERENT
        phase's verdict about a DIFFERENT write (DW-216): `_close_resolved` ran
        ahead of this walk, and a decision effect landing here proves the ledger
        reads — it does not make the close phase's half-write publishable. Bare,
        this would drop the close phase's doubt from `state.json` and hand the
        resume the dispatch the DW-218 crash row exists to refuse. The cycle-local
        latch itself is unchanged either way; only the mirror is at stake.

        Guarded on `_ledger_doubt_inherited` for the same reason across PROCESSES,
        and this one is the whole point of the entry rather than a corner of it. A
        walk may release only an arm it made ITSELF. Run 1 faults decision A's
        effect and dies before decision B is answered; the resume answers B, B's
        effect lands, and a bare release would drop A's verdict, open the gate and
        let the bundle commit publish A's half-write — which is verbatim the
        "`_decisions_phase` re-assigns False, so the half-recorded ledger reaches
        HEAD" defect, re-entered through the release instead of through the missing
        mirror. "The ledger reads and writes again" is a true statement about THIS
        process's last attempt and says nothing about what a previous one left on
        disk; only a human's repair plus a fresh `bmad-loop sweep` does.
        """
        if (
            self.state.sweep_ledger_in_doubt
            and not self._ledger_doubt_inherited
            and not self._close_ledger_in_doubt
        ):
            self.state.sweep_ledger_in_doubt = False
            self._save()

    def _owe_ledger_commit(self) -> bool:
        """Latch `state.sweep_ledger_commit_owed` and persist it, BEFORE a ledger
        publish whose commit is gated on that publish's own result.

        The two write-result-gated publishers (`_close_resolved` on `closed`,
        `_decisions_phase` on `any_effect_landed`) grade the write THIS invocation
        made, and a resume is a different invocation: a process that dies after
        `mark_done_many` or `record_decision` published but before `_commit_ledger`
        ran replays as a phase that closed nothing — the ids are already `done`,
        the answer already saved — so the guard that was unconditional before
        DW-183 skips the commit, and the closure the journal already claims stays
        dirty ahead of the cycle's bundles, where `commit_story`'s `add -A` absorbs
        it or a failed bundle's rollback discards it. Git cannot tell that dirt
        from an operator's edit; the sweep can, by persisting the debt. Same
        mutate-then-`_save()` latch as `_quarantine`, and for the same reason: the
        whole point is that a resume of this run sees it. Set before the write so
        every crash window is covered — a debt latched for a publish that then
        never happened is RETRACTED by the same invocation
        (`_retract_ledger_commit`), or, past a crash, settles as a `path_clean`
        no-op. Otherwise cleared only by the ledger-family `_commit_ledger` once
        git says the file is at HEAD; `_loop` settles an outstanding one at the
        top of a resume — unless the run's ledger doubt is on disk beside it
        (`sweep_ledger_in_doubt`, DW-218/219), which outranks it: the doubted
        bytes ARE the debt, and they stay unpublished with the debt left latched.

        Returns whether THIS call latched it. A latch already set at entry belongs
        to a previous invocation whose commit never landed — the settle at the top
        of `_loop` degraded, say — and only the invocation that latched a debt may
        retract it: a replay that closes nothing is exactly the shape the debt
        exists for, and must not read its own empty pass as proof there is
        nothing to publish."""
        if self.state.sweep_ledger_commit_owed:
            return False
        self.state.sweep_ledger_commit_owed = True
        self._save()
        return True

    def _retract_ledger_commit(self, owed_here: bool) -> None:
        """Clear the debt `_owe_ledger_commit` latched, because the publish it was
        latched for definitively landed nothing — a fault ahead of the write (the
        DW-166 degrade arms: a lock never taken, bytes nobody could read), a
        `LedgerWriteError` (the atomic write failed and the original is
        untouched), or a mutator that flipped no ids and so wrote no bytes.

        Left set, the latch would outlive the phase as a FALSE debt, and the next
        resume's settle would commit whatever the ledger file happened to be
        carrying — an operator's hand-edit, a rival writer's harvest — under a
        `chore(sweep):` message for a publish that never happened: the DW-183
        hazard back in a narrower form. `owed_here` is `_owe_ledger_commit`'s
        answer, so a debt inherited from an earlier invocation is never retracted
        here (see there). A `LedgerLockReleaseError` never reaches this: the
        publish LANDED, and that debt is real."""
        if owed_here and self.state.sweep_ledger_commit_owed:
            self.state.sweep_ledger_commit_owed = False
            self._save()

    def _remaining_estimate(self) -> int | None:
        """Sweep override of the graceful-stop hint: how many deferred-work
        entries are still open in the ledger — the work a resume would pick up.
        Like the base, a hint only: the whole body is guarded so an
        unreadable/invalid ledger returns None rather than derailing the stop."""
        try:
            ledger = self.workspace.paths.deferred_work
            # OBSERVATION arm (DW-146): a graceful-stop hint, nothing written from
            # it. The outer guard stays — it also covers `open_ids` — but routing
            # the read through the named arm is what records the classification.
            #
            # The fault is CHECKED rather than discarded, because this helper's
            # `None` and its `0` mean opposite things to the stop: `None` is "no
            # estimate", while `0` is a positive claim that a resume would pick up
            # nothing — and that number is published, in the `run-stop` journal row
            # and the graceful-stop notice. Degrading an unreadable ledger to the
            # empty text would report "0 remaining" for a file nobody could read,
            # the same fabricated answer `cli._sweep_dry_run` refuses to print.
            #
            # And the READ's fault is JOURNALED before the `None`, not merely
            # checked: the observation arm's rule is "degrade, and journal the
            # fault where a journal is in hand" — one is in hand here, and an
            # unreadable ledger is by far the likeliest way this hint goes away.
            # Scoped to the read leg, deliberately. The outer guard still answers
            # `None` silently for anything raised AFTER it (`open_ids`, the append
            # below), so `remaining: null` is not in general self-explaining; what
            # the row buys is that the one fault class the arm hands back as a
            # value gets attributed instead of collapsing into that same silence.
            text, fault = deferredwork.read_for_observation(ledger)
            if fault is not None:
                self.journal.append(
                    "sweep-remaining-estimate-unreadable",
                    ledger=str(ledger),
                    error=fault,
                )
                return None
            selection = select_entries(
                deferredwork.parse_ledger(text),
                only_ids=self.only_ids,
                min_severity=self.min_severity,
            )
            return len(selection.selected)
        except Exception:  # a hint must never break the stop
            return None

    # ------------------------------------------------------------ main loop

    def _read_cycle_ledger(
        self, ledger: Path
    ) -> tuple[str, Literal["ledger-unreadable", "ledger-inaccessible"] | None]:
        """`_loop`'s repair/write ledger read, degraded (DW-182/197). Returns
        `(text, None)` on a good read — absence spelled `or ""`, exact here because
        `open_ids("")` and `open_ids(<absent>)` say the same thing about a ledger
        nobody is writing — or `("", token)` after journaling the refusal.

        Both fault classes degrade to different tokens. Undecodable bytes
        (`LedgerReadError`) require a file repair; an OS refusal requires a
        permissions or storage repair. The `LedgerReadFault` subclass wraps OS
        read faults (DW-279) and must be caught before its decode parent, retaining
        the original OS attribution and `ledger-inaccessible` token.

        Bare, either fault ended a `--repeat` run as CRASHED at the top of cycle
        N+1, throwing away the report for cycles 1..N that had already completed.
        The refusal is announced rather than silent because this read gates the
        whole write-bearing cycle below it: nothing else in the journal would say
        why the run stopped one cycle short."""
        try:
            # REPAIR/WRITE (DW-146): this text drives migration and the whole
            # write-bearing cycle below it.
            return (deferredwork.read_for_write(ledger) or "", None)
        except (OSError, deferredwork.LedgerReadFault) as e:
            if isinstance(e, deferredwork.LedgerReadFault) and isinstance(e.__cause__, OSError):
                e = e.__cause__  # Preserve the original OS attribution.
            # The class NAME is kept beside the message because the message alone
            # ("[Errno 13] Permission denied") does not say what kind of refusal it
            # was, and this whole field is dropped from a scrubbed dump anyway —
            # the raw journal is the only reader that ever sees it.
            self.journal.append(
                "sweep-cycle-ledger-refused",
                ledger=str(ledger),
                reason="ledger-inaccessible",
                error=f"{e.__class__.__name__}: {e}",
            )
            return ("", "ledger-inaccessible")
        except deferredwork.LedgerReadError as e:
            self.journal.append(
                "sweep-cycle-ledger-refused",
                ledger=str(ledger),
                reason="ledger-unreadable",
                error=str(e),
            )
            return ("", "ledger-unreadable")

    def _stop_on_ledger_fault(
        self,
        fault: Literal["ledger-unreadable", "ledger-inaccessible"],
        *,
        cycles: int,
        ledger: Path,
    ) -> None:
        """End a repeating run on a ledger fault: one `sweep-repeat-done` write per
        token, then the shared repair notice.

        Spelled as two arms holding LITERAL `reason=`/`stop_cause=` pairs rather
        than one write passing `fault` through, because
        `test_every_repeat_stop_pairs_its_reason_with_the_same_stop_cause` parses
        this module and grades those keywords as constants — a variable token would
        typecheck, run correctly and leave the guard scanning nothing. Two literal
        arms satisfy it and still share the notice, which is the part that must not
        drift (it names a path and a precondition neither guessable nor cheap to
        get wrong twice).

        Spelled as an EXHAUSTIVE dispatch rather than an `if`/fall-through, for the
        reason `verify.unpublishable_target`'s `family` dispatch is: a THIRD token added to the
        `Literal` would otherwise typecheck at every call site and be reported
        silently as `ledger-unreadable` — a stop naming the wrong operator repair,
        which is the one failure the separate tokens exist to prevent. This reds
        under pyright the moment the union grows, before any run."""
        if fault == "ledger-inaccessible":
            self.journal.append(
                "sweep-repeat-done",
                cycles=cycles,
                reason="ledger-inaccessible",
                stop_cause="ledger-inaccessible",
            )
            self._notify_ledger_repair(
                ledger, "the deferred-work ledger could not be read mid-sweep"
            )
        elif fault == "ledger-unreadable":
            self.journal.append(
                "sweep-repeat-done",
                cycles=cycles,
                reason="ledger-unreadable",
                stop_cause="ledger-unreadable",
            )
            self._notify_ledger_repair(
                ledger, "the deferred-work ledger could not be decoded mid-sweep"
            )
        else:
            assert_never(fault)

    def _notify_ledger_repair(self, ledger: Path, headline: str) -> None:
        """The ledger-repair ATTENTION notice every ledger-fault stop takes.

        The message NAMES the file and the re-run's precondition. Neither is
        guessable: `implementation_artifacts` is configurable to any absolute path
        and the ledger may be symlinked out of the project, so "the ledger" names
        nothing an operator can open; and these stops deliberately leave the file
        DIRTY, which is exactly what `cmd_sweep`'s `worktree_clean` refusal rejects
        in the code repo (an external ledger repo is not checked), so an unqualified
        "re-run `bmad-loop sweep`" sends the human into an exit-1 they were told not
        to expect. One copy, because a second one would drift from it."""
        gates.notify(
            self.policy,
            self.run_dir,
            headline,
            f"repair {ledger} by hand, then commit or stash any changes in "
            f"{self.paths.repo_root} and re-run `bmad-loop sweep` "
            "(which requires that worktree to be clean)",
        )

    def _loop(self) -> None:
        ledger = self.workspace.paths.deferred_work
        cycle = max(1, self.state.sweep_cycle)
        # A regeneration refusal (DW-243/252) PAUSES from `_ensure_bundle_intent`
        # and propagates here as `RunPaused`, exactly like the migrate gate below —
        # and only when the recovery pass runs at all: see the gate just below.
        if self._ledger_unfit_to_publish():
            # DW-218/219, one gate AHEAD of `_cycle`'s dispatch gate. Read through
            # `_ledger_unfit_to_publish()` like every other consumer of the
            # verdict; here the two cycle-scoped latches are still clear, so only
            # the inherited mirror can answer. A doubt is armed only in the two
            # phases that run before dispatch, so no bundle THIS process armed a
            # doubt over can be in flight — but a bundle re-armed out of band can:
            # `bmad-loop resolve` resets an escalated bundle to PENDING, and a run
            # paused on a stop request in the withheld branch (DW-219) carries
            # that re-arm into its resume. Re-driving it here is the dispatch the
            # gate below exists to refuse — the bundle's own `commit_story` /
            # `finalize_commit` opens with a whole-tree `git add -A` that sweeps
            # the doubted ledger into HEAD — so the recovery pass is withheld
            # whole, the COMMITTING-window arm included. The bundles stay
            # nonterminal and `_warn_stranded_bundles` names them at the cycle
            # that follows; this row covers the no-open exit, which has no cycle.
            # The repair is the doubt's: a human edits the ledger and starts a
            # fresh `bmad-loop sweep`, whose triage re-bundles the still-open ids.
            inflight = [
                t.story_key
                for t in self.state.tasks.values()
                if BUNDLE_KEY_RE.match(t.story_key) and not t.terminal
            ]
            recovered = 0
            if inflight:
                self.journal.append(
                    "sweep-bundles-withheld",
                    cycle=cycle,
                    bundles_not_run=len(inflight),
                    reason="ledger-unreadable",
                    story_keys=inflight,
                )
        else:
            recovered = self._finish_inflight_bundles()
        # ...and the same verdict gates the publish that follows either trigger,
        # at the call site inside the arm (the DW-246 withhold below), so a
        # trigger added later cannot reach the publisher around it.
        if recovered or self.state.sweep_ledger_commit_owed:
            # a recovered bundle's ledger restore can leave the LEDGER dirty, and
            # triage plus the first bundle baseline read it, so it is published
            # here. Only it: unrelated dirt in the same repository is left for
            # whoever owns it, so this no longer ends on a clean TREE and nothing
            # downstream may assume one. Guarded on a non-empty recovery pass, so
            # a fresh sweep spawns no git at all (see `_close_resolved` for the
            # guard inventory across all nine sites) — OR on a persisted debt: a
            # publish the already-resolved close or the decision phase landed and
            # then died before committing (`_owe_ledger_commit`). Both sites gate
            # their own commit on this invocation's write, which a replay of an
            # already-landed publish cannot show, so the debt is settled HERE,
            # before triage or a bundle baseline reads the ledger. One call, two
            # messages: the recovery one when a recovery pass ran (it covers the
            # debt too), the debt's own otherwise.
            # The LEDGER FILE (`_commit_ledger`): this publisher wrote the ledger,
            # so it names the file it published and the commit is narrowed to it.
            # Spelled off `self.workspace.paths` rather than a `ledger` local, at
            # every one of the seven publishers: `self.paths.deferred_work` is a
            # DIFFERENT file under worktree isolation, and only the workspace's
            # copy is the one a publisher just wrote.
            # ...and WITHHELD when the run already knows the ledger is unfit
            # (DW-246), journaled `sweep-ledger-commit-withheld` rather than
            # silently skipped. `recovered` is 0 under doubt by construction above,
            # so the DEBT is the only term that can reach this arm: a process that
            # armed `_record_ledger_doubt` and then died between an effect's
            # publish and its commit resumes with BOTH latches set, and the debt
            # describes exactly the bytes the doubt refuses to publish — the settle
            # would walk the half-written ledger into HEAD ahead of every gate that
            # withholds it. The doubt outranks the debt: the settle is withheld,
            # the debt stays latched, and the run ends where the doubt ends it (the
            # withheld dispatch, then `ledger-unreadable`); a fresh sweep is a NEW
            # run with fresh state, so the unsettled debt contaminates nothing.
            # Read through `_ledger_unfit_to_publish()`, the one reader; the gate
            # sits AT the call site because `_commit_ledger` has publishers that
            # deliberately stay ungated (see `_close_resolved`'s inventory).
            publish_message = (
                "chore(sweep): commit ledger after recovering in-flight bundles"
                if recovered
                else "chore(sweep): commit a ledger write an interrupted phase left unpublished"
            )
            if self._ledger_unfit_to_publish():
                self._withhold_ledger_publish(publish_message)
            else:
                self._commit_ledger(
                    publish_message,
                    path=self.workspace.paths.deferred_work,
                    family="ledger",
                )
        while True:
            # First statement of the loop body: covers the boundary right after
            # _finish_inflight_bundles on resume and between repeat cycles. A
            # request during a cycle is caught before the next _run_bundle (see
            # _cycle); one landing between cycles stops here before cycle N+1
            # re-triages.
            self._check_stop_request()
            self.state.sweep_cycle = cycle
            self._save()
            migrate_task = self.state.tasks.get(MIGRATE_KEY)
            if (
                cycle == 1
                and migrate_task is not None
                and (
                    migrate_task.phase == Phase.COMMITTING
                    or (
                        migrate_task.phase == Phase.TRIAGE_VERIFY
                        and migrate_task.migration_recovery_format != 0
                    )
                )
            ):
                # Recovery evidence, not the generic cycle reader, owns faults at
                # these durable boundaries, including unknown marker formats. A
                # cycle-reader return would let the outer engine stamp the run
                # finished and make the public resume command refuse it. Format 0
                # TRIAGE_VERIFY is the one pre-upgrade path that still needs the
                # cycle reader's live text for its legacy restart.
                self._ensure_migration("")
            text, ledger_fault = self._read_cycle_ledger(ledger)
            if ledger_fault is not None:
                # `cycle - 1`: this cycle did no work at all — the read that would
                # have driven it is the thing that failed — so it reports like the
                # `legacy-appeared` arm below, not like the prune carry further
                # down, whose cycle COMPLETED.
                self._stop_on_ledger_fault(ledger_fault, cycles=cycle - 1, ledger=ledger)
                return
            migrate_task = self.state.tasks.get(MIGRATE_KEY)
            migration_resume = (
                cycle == 1
                and migrate_task is not None
                and migrate_task.phase not in (Phase.DONE, Phase.ESCALATED)
            )
            if deferredwork.has_legacy(text) or migration_resume:
                if cycle > 1:
                    # freeform text appeared mid-run; _ensure_migration assumes
                    # one migration per run, so hand off to a fresh sweep
                    # `stop_cause` beside `reason`, on all five stop sites (DW-201):
                    # `diagnostics._JOURNAL_DROP_FIELDS` holds `reason` and renders it
                    # as a presence boolean, so a scrubbed dump could not tell the five
                    # stops apart at all. The same closed-slug convention `regen_cause`
                    # (DW-164) and `drop_cause` use, and for the same reason — the two
                    # carry the SAME token, so `reason` is unchanged for every reader
                    # of the raw journal.
                    self.journal.append(
                        "sweep-repeat-done",
                        cycles=cycle - 1,
                        reason="legacy-appeared",
                        stop_cause="legacy-appeared",
                    )
                    gates.notify(
                        self.policy,
                        self.run_dir,
                        "legacy ledger entries appeared mid-sweep",
                        "run a fresh `bmad-loop sweep` to migrate them",
                    )
                    return
                if self._ledger_unfit_to_publish():
                    # DW-218/219 at the one publisher that sits ABOVE the cycle:
                    # `_ensure_migration` spends a session rewriting the whole
                    # ledger and publishes the result through `_commit_ledger`,
                    # so under an inherited doubt it would normalize and commit
                    # the very bytes every gate below withholds. Reachable as a
                    # hand-repair gone sideways — the human the notice sent to
                    # edit the file pastes legacy prose in and then `resume`s
                    # instead of starting the fresh sweep it named. The doubt's
                    # OWN stop and notice, and `cycle - 1` like the arm above:
                    # this cycle did no work. Literal `reason=`/`stop_cause=`
                    # pair, as the module-parsing guard requires.
                    self.journal.append(
                        "sweep-repeat-done",
                        cycles=cycle - 1,
                        reason="ledger-unreadable",
                        stop_cause="ledger-unreadable",
                    )
                    self._notify_ledger_repair(
                        ledger, "the deferred-work ledger is not fit to publish"
                    )
                    return
                self._ensure_migration(text)
                # Same cycle, re-read after migration — and degraded on the same
                # terms as the read above, since the migration's own write is a way
                # for this read to start failing where the first one did not.
                text, ledger_fault = self._read_cycle_ledger(ledger)
                if ledger_fault is not None:
                    self._stop_on_ledger_fault(ledger_fault, cycles=cycle - 1, ledger=ledger)
                    return
            entries = deferredwork.parse_ledger(text)
            selection = select_entries(
                entries,
                only_ids=self.only_ids,
                min_severity=self.min_severity,
                validate_only=not self._selection_started,
            )
            self._selection_started = True
            open_now = {entry.id for entry in entries if entry.open}
            if not open_now:
                # DW-193's ROUTING half, and the reason a phase-local fix alone
                # cannot keep that entry's promise. When the crash that stranded a
                # close between `mark_done_many`'s write and its commit retired the
                # LAST open entry, this `return` fires before `_cycle` ever calls
                # `_ensure_triage`/`_close_resolved`, so no phase gate — however
                # wide — is ever reached and the durable write stays off HEAD for
                # the rest of a single-cycle run. The publish attempt therefore sits
                # ABOVE the stop rows below: a reader sees the recovered commit and
                # then the stop, in that order. The rows and the `return` beneath
                # are unchanged, and `_publish_stranded_close` degrades rather than
                # raising, so every path still ends here.
                self._publish_stranded_close(cycle)
                if cycle == 1:
                    self.journal.append("sweep-nothing-open", ledger=str(ledger))
                else:
                    self.journal.append(
                        "sweep-repeat-done",
                        cycles=cycle - 1,
                        reason="no-open",
                        stop_cause="no-open",
                    )
                # DW-251: a run that reaches this exit with the doubt armed
                # withheld every publish it was offered — the stranded-close
                # republish above withholds on the same verdict (DW-250) —
                # and then ended on `no-open` with no repair instruction at all.
                # Not because an earlier notice was lost: `gates.notify` APPENDS
                # to `<run>/ATTENTION`, which a resume shares. The gap is that the
                # class that ARMS the doubt never wrote one — the arms do not
                # notify, the withheld branch's stop check exits ahead of its row,
                # and a non-repeat run has no stop notice — so the process that
                # withheld said nothing. That holds for a resume inheriting the
                # DW-218/219 mirror and for a same-process arm alike (a refused
                # `_publish_stranded_close` or migration publish under DW-244
                # lands here in a fresh process too). So the shared notice is
                # written here, with the same headline the repeat boundary's
                # unfit stop uses, guarded on the one reader. AFTER the
                # exit's own row and BEFORE its `return`, in both arms: the rows,
                # their fields and the `return` are unchanged, no stop token is
                # added, and the notice is deliberately NOT conditional on whether
                # `_publish_stranded_close` published anything — the doubt is the
                # run's verdict, and a human repair plus a fresh `bmad-loop sweep`
                # is the only thing that clears an inherited one.
                if self._ledger_unfit_to_publish():
                    self._notify_ledger_repair(
                        ledger, "the deferred-work ledger is not fit to publish"
                    )
                return
            selected_ids = {entry.id for entry in selection.selected}
            selector = "only" if self.only_ids is not None else f"min-severity:{self.min_severity}"
            if selection.excluded:
                self.journal.append(
                    "sweep-selection-excluded",
                    cycle=cycle,
                    reason=selector,
                    dw_ids=[entry.id for entry in selection.excluded],
                )
            if selection.missing_severity:
                self.journal.append(
                    "sweep-selection-missing-severity",
                    cycle=cycle,
                    dw_ids=[entry.id for entry in selection.missing_severity],
                )
            if not selected_ids:
                if cycle == 1:
                    self.journal.append("sweep-selection-empty", reason=selector)
                else:
                    self.journal.append(
                        "sweep-repeat-done",
                        cycles=cycle - 1,
                        reason="no-selected",
                        stop_cause="no-selected",
                    )
                return
            if cycle > 1:
                self.journal.append("sweep-cycle", cycle=cycle, open=len(open_now))
            progressed = self._cycle(cycle, selected_ids)
            if self.decisions_only or not self.repeat:
                return
            if self._prune_ledger_inaccessible:
                # DW-197's half of the carry, read ABOVE the arm below so the
                # prune's own last-observed fault wins the report — the same
                # precedence that arm already applies between
                # `_prune_ledger_unreadable` and `_ledger_in_doubt`. Everything the
                # comment below argues holds verbatim: the boundary
                # `_commit_ledger` publishes the ledger, and cycle N+1 would meet
                # the same refusal at this loop's own read. `cycles=cycle` because
                # the cycle COMPLETED — that is what the prune's degrade bought.
                self._stop_on_ledger_fault("ledger-inaccessible", cycles=cycle, ledger=ledger)
                return
            if self._prune_ledger_unreadable or self._ledger_unfit_to_publish():
                # DW-182/186. `_prune_pre_answers` refused to read the ledger
                # because nothing could decode it, and that refusal has to END a
                # repeating run rather than stay inside the cycle: the boundary
                # `_commit_ledger` below PUBLISHES the ledger, so falling through
                # would commit the undecodable bytes and cycle N+1 would then
                # get no further than this loop's own read of them anyway — which
                # since DW-197 STOPS that cycle rather than crashing it, a
                # different mechanism reaching the same place. Cycle
                # `cycle` COMPLETED — that is what the prune's degrade bought —
                # so `cycles=cycle`, unlike the `legacy-appeared` arm above, which
                # fires before its cycle does any work and reports `cycle - 1`.
                # Placed above `not progressed` and `max_cycles` so it is the
                # reported reason whenever it fires; below the early return so a
                # non-repeating or `--decisions-only` run is untouched. Not a
                # pause and not recovery: the repair is a human editing the file,
                # and re-running `bmad-loop sweep` is the resume. Deliberately NOT
                # extended to the DW-176 absence refusal — an absent ledger ends
                # the next cycle cleanly on `no-open`.
                # DW-194/202/210 shares this stop: same repair and closed stop-token
                # contract. Effect faults can leave decodable bytes, so the gate
                # needs its own latch rather than relying on the prune to refuse.
                # DW-216/217/220 widen that leg to `_ledger_unfit_to_publish()`,
                # which adds the CLOSE phase's own latch — `_close_resolved` runs
                # ahead of the decision phase's `=` publish, so it cannot share the
                # flag — plus the re-apply gate's read refusal and the end-of-phase
                # probe, which both feed `_ledger_in_doubt`. All three land on THIS
                # token and no new stop: the repair is identical (a human editing
                # the file) and `_REPEAT_STOP_TOKENS` is a closed set.
                self.journal.append(
                    "sweep-repeat-done",
                    cycles=cycle,
                    reason="ledger-unreadable",
                    stop_cause="ledger-unreadable",
                )
                # The notice — which names the ledger path and the clean-worktree
                # precondition, and why both are load-bearing — lives in
                # `_notify_ledger_repair`, shared with the DW-197 stops. The
                # HEADLINE still belongs to this arm: `_prune_ledger_unreadable`
                # chooses it over `_ledger_in_doubt` for the same reason the
                # condition is ordered that way — it is the only latch that
                # observed bytes nobody could DECODE, so it is the only one that
                # may say so. The close-phase leg and the probe both reach the
                # generic "not fit to publish" wording for the same reason a
                # decodable effect fault does: what they observed was a write that
                # could not land or a read that was refused, which says nothing
                # about the bytes decoding.
                self._notify_ledger_repair(
                    ledger,
                    (
                        "the deferred-work ledger could not be decoded mid-sweep"
                        if self._prune_ledger_unreadable
                        else "the deferred-work ledger is not fit to publish"
                    ),
                )
                return
            # Publish the workspace ledger at the repeat-cycle boundary, before
            # no-progress, max-cycles, or cycle N+1. This also retries a close or
            # decision publish that degraded earlier in the cycle (DW-223) — the
            # pre-attempt degrade, a tree git could not read at that moment; a
            # commit git was asked to make and refused raised out of that phase
            # instead (S05), so it never reaches here.
            # Keep this single site below the ledger-fault/unfit stops above;
            # non-repeat, decisions-only and no-open exits return earlier.
            # There is no landed-write gate here: even a skip-only terminal cycle
            # reaches `path_clean`, and any dirty ledger is published whole,
            # including out-of-band edits without a recovered close beside them.
            # Unrelated files stay with their owner. The whole-file trade is the
            # same as `_publish_stranded_close`; `_close_resolved` inventories the
            # nine publication sites. A pre-attempt git fault stays a best-effort
            # miss here too; an attempted commit git refuses raises, as at every
            # ledger publisher.
            self._commit_ledger(
                "chore(sweep): commit ledger at the sweep cycle boundary",
                path=self.workspace.paths.deferred_work,
                family="ledger",
            )
            if not progressed:
                self.journal.append(
                    "sweep-repeat-done",
                    cycles=cycle,
                    reason="no-progress",
                    stop_cause="no-progress",
                )
                return
            if cycle >= self.max_cycles:
                self.journal.append(
                    "sweep-repeat-done",
                    cycles=cycle,
                    reason="max-cycles",
                    stop_cause="max-cycles",
                )
                return
            cycle += 1

    def _publish_stranded_close(self, cycle: int) -> None:
        """Publish a close whose ledger write landed but whose commit never ran,
        at `_loop`'s empty-open-set exit (DW-193).

        The RESUME-ONLY term is the CACHE, and it carries the whole
        recovery/fresh-sweep distinction with no extra state flag: `triage{suffix}
        .json` for THIS cycle exists only because a previous pass of this run
        already triaged it, so a fresh `bmad-loop sweep` over an empty-or-absent
        ledger returns here before any read and spawns no git at all — graded at the
        git seam by `test_a_fresh_nothing_open_sweep_spawns_no_git_at_the_no_open_exit`
        for THIS arm, the way `test_a_phase_that_wrote_nothing_spawns_no_git` grades
        it for the phase-local one. The CURRENT-CYCLE suffix is load-bearing for the same
        reason: under `--repeat`, cycle 1's finished `triage.json` must not
        authorize a publish at cycle 2's no-open exit, where the cycle that would
        have replayed it never ran.

        The cache is loaded the way `_ensure_triage`'s cache branch loads it —
        `_read_json` plus `validate_triage(cached, None)`, whose `None` skips the
        open-set equality re-check, which is exactly what a nothing-open resume
        needs since the ledger has moved since the plan was written. It is read
        DIRECTLY rather than through `_ensure_triage`: past that method's cache
        branch it dispatches a triage SESSION, and a nothing-open resume must never
        spend one.

        Second term: the SAME per-id evidence the phase arm uses — one read of the
        ledger through `_done_on_disk`, answering which named ids read `done` — over
        the cached plan's `already_resolved` ids AND, since DW-222, over its
        `decisions` ids. One rule at both sites — publish on positive per-id
        evidence of a landed write, never on the ledger merely being dirty — so
        everything that reader documents about what the probe does and does not
        prove applies verbatim here, including its empty-`ids` short-circuit.

        TWO PROBES combined with `or`, never one merged id list, and they are
        DIFFERENT views of the reader. The decision phase strands a close the same
        way the close phase does: `_apply_decision_effect` calls
        `deferredwork.record_decision(..., close_note=...)`, which flips the entry
        to `status: done <date>` on disk, and `_decisions_phase` publishes only at
        its own tail — so a crash between that write and that publish leaves a
        decision-phase close durable on disk and off HEAD, and when it retired the
        last open entry the resume reaches THIS exit with nothing else to run. The
        already-resolved term is `_resolved_write_pending`, the `all` view: the
        close phase closes its ids as ONE batch (`mark_done_many`), so a stranded
        batch write is every id `done`. The decision term is `_any_write_pending`,
        the `any` view (DW-249): decision closes land one effect at a time, so a
        plan with two decision ids where one is absent from the ledger — retired
        by a rival writer, or never closed at all — still carries a stranded close
        under the other, and an `all` over the pair answered False and never
        published it at this exit. Merging the two id lists into one `all` call
        would AND the terms the same way (a NARROWING of the arm that shipped);
        the `any` view makes an absent or unparseable id veto only itself. Both
        views short-circuit on empty `ids` before reading anything, the decision
        set is read ONCE, and the `or` still skips that read when the
        already-resolved term proves the write.

        THE DOUBT GATE sits above BOTH probes (DW-250), read through
        `_ledger_unfit_to_publish()` and reported through `_withhold_ledger_publish`
        with the union of both id lists in `dw_ids`. A failed decision effect can
        leave a decodable done flip without its audit line, a ledger-family refusal
        arms the same verdict (DW-244), and a crash preserves either across a
        resume; readability alone must not authorize publishing those bytes. Until
        DW-250 only the decision term read the verdict, so a resumed run holding
        persisted doubt still published the WHOLE file — the unaudited flip
        included — whenever its cached plan carried an already-resolved id that
        read `done` (the Codex P1 on #792 met the same hole from the other side:
        the `or` short-circuited past a decision-term guard), and a plan with only
        decision ids under doubt returned with no row at all. The gate is placed AFTER the cache is validated and the two id
        lists are known, and after the empty-plan short-circuit: an empty plan
        would never have published (both probes short-circuit), so there is
        nothing to decline and no row is owed — the same "only when a publish was
        about to happen" shape the three DW-246 sites have — and a fresh sweep
        returns even earlier, at the cache term, so its "no read, no git" property
        is untouched. The gate does not change the shared reader or add a phase
        gate; the phase arm's own gates are `_close_resolved`'s (DW-246).

        WHAT THE PROBE PROVES HERE is weaker than the rule's wording suggests, and
        reading it as a contradiction is the trap. This exit is reached only when
        the open set is EMPTY, so over a well-formed ledger every id still in the
        file already reads `done` — which makes the term close to a PRESENCE check
        at this one call site, for the decision ids and the already-resolved ids
        alike (and, under the `any` view, presence of ANY ONE decision id). That
        is pre-existing, not something the widening introduced: the
        already-resolved term has had exactly this property since DW-193, and it is
        tolerable for the reason `_resolved_write_pending` gives — the answer
        authorizes a COMMIT of the ledger, which is the right outcome for a durable
        close whoever wrote it. What the probe still refuses is the part that
        matters: `DWEntry.done` is deliberately not `not .open`, so an id the ledger
        does not carry and an id whose status the format cannot parse both prove
        nothing and authorize nothing, and an empty plan reaches no git at all.

        Faults DEGRADE and end on `_loop`'s own unchanged `return`. A cache that
        cannot be read or does not validate journals `sweep-triage-reload-failed`,
        the kind `_ensure_triage` already mints for exactly that; a probe fault
        journals `sweep-resolved-close-unavailable` with `dw_ids` + `error`, the row
        `_close_resolved`'s degrade arm already writes for the same read. No new
        journal kind is minted at either — reusing the two readers' own rows keeps
        the kind registry untouched. Neither publishes anything. A WITHHELD publish
        (DW-250) is the third non-publishing outcome and is not a fault: the gate
        returns before any read, on `sweep-ledger-commit-withheld`.

        No `sweep-resolved-closed` row and no phase emissions: this is a PUBLISHER,
        not a replay of `_close_resolved`. `pre_close_resolved`/`post_close_resolved`
        pairing stays inside `_cycle`, which is why the phase is not called from
        here even though it would reach the same commit.

        WHOSE DIRT CAN RIDE ALONG, the caveat the phase arm's comment states and
        that binds HARDER here. The boundary that holds is the PATHSPEC:
        `_commit_ledger` narrows both git calls to the ledger file, so an operator's
        in-flight edits anywhere ELSE in the enclosing repository stay with their
        owner (DW-183/DW-185). Inside the ledger there is no such boundary — the
        file is published WHOLE — so an out-of-band edit to it rides into the
        `chore(sweep):` commit beside whatever this arm was asked to publish. The
        phase arm has that property only while a stranded write is outstanding;
        this one keeps it FOREVER, because the probe is keyed on ids that stay
        `done` once published, so every later resume of the same cycle reaches this
        publisher again (`path_clean` no-ops it while the file matches HEAD, but an
        operator's note written months after the close is exactly what stops it
        matching). Accepted deliberately by the DW-193 decision, on the same terms
        as the phase arm: the alternative is a per-hunk publish this bookkeeping
        does not have, and a durable close left off HEAD is the worse loss.
        """
        suffix = "" if cycle == 1 else f"-{cycle}"
        triage_path = self.run_dir / f"triage{suffix}.json"
        try:
            # Use stat directly: is_file suppresses OS faults on Python 3.14,
            # while older runtimes can raise. Absence stays silent; a metadata
            # fault belongs to the same degrade as a failed cache read.
            try:
                cache_mode = triage_path.stat().st_mode
            except (FileNotFoundError, NotADirectoryError):
                return
            if not stat.S_ISREG(cache_mode):
                return
            cached = _read_json(triage_path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            self.journal.append("sweep-triage-reload-failed", errors=[f"unreadable: {exc}"])
            return
        if isinstance(cached, dict):
            plan, errors = validate_triage(cached, None)
        else:
            plan, errors = None, [f"not a JSON object: {type(cached).__name__}"]
        if plan is None:
            self.journal.append("sweep-triage-reload-failed", errors=errors)
            return
        ledger = self.workspace.paths.deferred_work
        resolved_ids = [entry.id for entry in plan.already_resolved]
        decision_ids = [decision.id for decision in plan.decisions]
        if not resolved_ids and not decision_ids:
            # Nothing would have been published — both probes short-circuit on an
            # empty list — so there is nothing to decline: no read, no row, no git.
            return
        close_message = "chore(sweep): close resolved deferred-work entries"
        if self._ledger_unfit_to_publish():
            # DW-250: the run's verdict, over BOTH terms, before either probe reads.
            # The row names the union — every id this publisher declined to prove.
            self._withhold_ledger_publish(close_message, dw_ids=resolved_ids + decision_ids)
            return
        try:
            # `or`, and two calls of two VIEWS: see the docstring — the
            # already-resolved term is the `all` view, the decision term the `any`
            # view (DW-249), and one merged `all` list would AND them and narrow the
            # arm that shipped. Short-circuit is deliberate too: a plan whose
            # already-resolved ids already prove the write never reads twice.
            pending = self._resolved_write_pending(ledger, resolved_ids) or self._any_write_pending(
                ledger, decision_ids
            )
        except (deferredwork.LedgerReadError, OSError, ValueError, StateRootError) as e:
            # The UNION, because either probe can be the one that faulted and the
            # row is the operator's only account of which ids went unproven.
            self.journal.append(
                "sweep-resolved-close-unavailable",
                dw_ids=resolved_ids + decision_ids,
                error=str(e),
            )
            return
        if not pending:
            return
        # the ledger file: the write this arm publishes is a previous pass's write
        # to the ledger, so it names the file it publishes and the commit is
        # narrowed to it. The WORKSPACE's copy, for the reason the publishers above
        # spell it that way. Same message as `_close_resolved`'s two arms: the diff
        # being published IS the close of resolved entries.
        self._commit_ledger(
            close_message,
            path=self.workspace.paths.deferred_work,
            family="ledger",
        )

    def _finish_inflight_bundles(self) -> int:
        """Re-drive every bundle this run left in flight, keyed on the task's own
        persisted story_key. Returns how many were recovered.

        The base Engine._loop opens with _finish_inflight for exactly this reason;
        the sweep loop used to recover a bundle only from inside _run_bundle, which
        a cycle reaches only after re-deriving the bundle's key from the *current*
        triage plan. A re-armed bundle therefore survived a resume solely because
        the cached triage.json reloaded and re-emitted the same bundle name — lose
        that cache and a fresh triage partitions the ids under new names, silently
        orphaning the human's resolution (#94).

        Runs before the ledger is read, so a bundle it closes leaves the open set
        and no fresh triage can re-bundle those ids (validate_triage rejects a plan
        whose open_ids disagree with the ledger). A recovered bundle that defers or
        escalates keeps its ids open. A dev-leg discard never closes them; an
        in-place post-acceptance defer reopens this run's close, while an isolated
        unit's close dies with its unmerged worktree. The existing failed_ids filter
        then drops the fresh plan's overlapping bundle.

        One refusal sits between the recovery and the dispatch (DW-243/252), in
        `_ensure_bundle_intent`: a regeneration the ledger cannot serve — a read
        that faults, an absent file, or a readable ledger lacking an entry for one
        of the task's ids — PAUSES the run at the story gate on that task, and the
        `RunPaused` propagates straight through this frame to `Engine._run_inner`.
        The pass does not continue: no later in-flight bundle is re-driven and no
        cycle (hence no fresh triage) runs beside the refused task, so nothing can
        overwrite its name or re-adopt its ids while it waits. The task stays
        PENDING with its name, ids and attempt intact, the run stays un-finished,
        and `bmad-loop resume` after the repair re-enters this pass, recovers the
        same task, regenerates and re-drives it. Every refusal raises; a `True`
        return from `_ensure_bundle_intent` is the only way out."""
        recovered = 0
        for task in list(self.state.tasks.values()):
            if task.terminal or not BUNDLE_KEY_RE.match(task.story_key):
                continue
            recovered += 1
            self.journal.append(
                "sweep-inflight-redrive",
                story_key=task.story_key,
                phase=str(task.phase),
                rearmed=task.rearmed,  # read before the recovery clears the latch
            )
            if self._recover_inflight_bundle(task):
                continue
            self._ensure_bundle_intent(task)  # every refusal raises RunPaused
            self._save()
            self._emit("pre_bundle", task)
            self._run_story(task)
            self._emit("post_bundle", task)
        return recovered

    def _warn_stranded_bundles(self) -> None:
        """Invariant: _finish_inflight_bundles has driven every persisted bundle to
        a terminal phase before a cycle picks new work. A survivor means a bundle
        would be silently dropped — say so loudly rather than sweep past it."""
        stranded = [
            t.story_key
            for t in self.state.tasks.values()
            if BUNDLE_KEY_RE.match(t.story_key) and not t.terminal
        ]
        if not stranded:
            return
        self.journal.append("sweep-inflight-stranded", story_keys=stranded)
        gates.notify(
            self.policy,
            self.run_dir,
            f"{len(stranded)} sweep bundle(s) left in flight",
            "not re-driven by this cycle: " + ", ".join(stranded),
        )

    def _cycle(self, cycle: int, open_now: set[str]) -> bool:
        """One triage -> close -> decide -> bundle pass. Returns whether the
        cycle completed any addressable work — the repeat loop's progress
        predicate. Dropping a recorded decision answer counts (DW-123, widened
        from the keep-open lane to the build lanes by DW-135; DW-200's
        `effect-unlanded` lane and DW-214's `entry-not-open` screen are later
        additions that count on the same footing): the drop releases its id from a
        stored answer nothing can act on, so a later cycle's fresh triage can
        address it. The screen's id is the one exception to that second clause and
        buys the first alone — it is by construction not open, and `_loop` derives
        triage's universe from `open_ids`, so no later cycle re-raises it; what its
        drop wins is that the id stops being bound to an answer nothing can act on,
        and becomes addressable again through the out-of-band surfaces
        (`bmad-loop decisions`, the TUI modal) once the entry is re-opened.
        It cannot spin the loop —
        `_materialize_bundles` bounds each id to one drop per run, and since
        DW-124 that bound is persisted on `state`, so it holds across a
        pause/resume too and the signal fires at most once per id. Caveat: on
        crash-resume of a cycle whose only progress was already-resolved closes,
        the replayed (idempotent) closes report 0 and the run stops with
        no-progress; the same now goes for a cycle whose only would-be event is a
        drop the pre-crash run already announced and persisted, which the
        quarantine skips rather than re-signalling. Errs toward stopping, never
        loops."""
        self._emit("pre_sweep_cycle", phase=str(cycle))
        self._warn_stranded_bundles()
        plan = self._ensure_triage(open_now, cycle)
        closed = self._close_resolved(plan)
        answers, decisions_closed, effect_unlanded = self._decisions_phase(plan)
        bundles, answer_dropped = self._materialize_bundles(
            plan, answers, effect_unlanded=effect_unlanded
        )
        if self.decisions_only:
            self.journal.append("sweep-decisions-only", bundles_not_run=len(bundles))
            self._prune_pre_answers()
            self._emit("post_sweep_cycle", phase=str(cycle))
            return False
        graded_keys: list[str] = []
        if self._ledger_unfit_to_publish():
            # Undecodable bytes would crash intent creation; decodable partial
            # writes would reach HEAD through a bundle commit's `git add -A`.
            # Read through the helper (DW-216/217/220) rather than
            # `_ledger_in_doubt` directly: the close phase has a latch of its own,
            # and the helper is the one place that knows about both.
            # Preserve the item-boundary stop check even when dispatch is withheld.
            self._check_stop_request()
            if bundles:
                # Report only bundles actually withheld, with their cycle.
                self.journal.append(
                    "sweep-bundles-withheld",
                    cycle=cycle,
                    bundles_not_run=len(bundles),
                    reason="ledger-unreadable",
                )
        else:
            for bundle in bundles:
                # Item boundary: a request during bundle N lets N finish through
                # commit; bundle N+1 never starts. A request landing during triage
                # reaches the first iteration here, so triage completes but zero
                # bundles run. Mid-cycle stop is resume-safe: sweep_cycle is
                # persisted, triage.json is cached, closes are idempotent, and
                # terminal tasks are skipped on re-drive.
                self._check_stop_request()
                key = self._run_bundle(bundle, cycle)
                if key is not None:
                    graded_keys.append(key)
        # Grade the key each bundle was actually resolved to — the one it ran
        # under, or the terminal one it was skipped as already-finished at,
        # which counts here exactly as it always has. What is never used is a
        # key re-derived from `bundle.name`: since DW-125 a bundle whose own key
        # is held by a terminal task carrying different dw_ids runs under a
        # DEDUPED name, so the re-derived key named the wrong task — the
        # finished one, whose DONE phase counted a bundle this cycle never ran,
        # in both the deduped case and the name-collision drop that runs nothing
        # at all. Reading the reported keys also keeps the lookup total: every
        # key returned here has a task by construction, where a re-derived one
        # need not.
        bundles_done = sum(1 for key in graded_keys if self.state.tasks[key].phase == Phase.DONE)
        self._prune_pre_answers()
        self._emit("post_sweep_cycle", phase=str(cycle))
        return closed > 0 or decisions_closed > 0 or bundles_done > 0 or answer_dropped

    def _prune_pre_answers(self) -> None:
        """Drop consumed pre-answers — entries built or closed this cycle have
        left the open set. Keeps the store from re-applying a stale answer (and a
        keep-open answer's audit line) on the next sweep.

        EVERY ledger-read fault degrades here rather than propagating: absence
        (DW-176), undecodable bytes (DW-182) and an `OSError` from the read itself
        (DW-197). The case for staying loud is that
        this read decides a store WRITE, so refusing to guess is right — but the
        refusal IS the refusal to guess. It keeps every answer and prunes nothing,
        so the choice is not "guess vs. crash", it is "keep the store and say so
        vs. crash the sweep". This is end-of-cycle bookkeeping, including when
        bundles were withheld or the run is decisions-only: a raise here crashes
        the cycle over cleanup, where refusal costs only consumed answers
        re-offered on the next sweep. Bytes nobody could decode are unknown open
        work for exactly the reason absence is, so they take the same journal row under a
        second fixed `reason` token rather than a kind of their own.

        The undecodable refusal is not only journaled, it is CARRIED: it sets
        `_prune_ledger_unreadable`, which `_loop` reads at the repeat boundary and
        which ends a repeating run there. Without the carry the degrade is a
        half-measure — the boundary `_commit_ledger`'s pathspec IS the ledger, so
        the very next thing a repeating run does is COMMIT the bytes this method
        just refused to read, and cycle N+1 gets no further than `_loop`'s own read
        of them regardless (which ENDS the run there since DW-197, where it used to
        crash — the carry is what keeps the corrupt bytes out of HEAD either way).
        Absence (DW-176) sets nothing, deliberately: a ledger that is gone ends the
        next cycle cleanly on `no-open` rather than stopping it at all.

        `OSError` used to propagate here, on the argument that it says nothing
        about what the ledger holds — but that was never a reason to CRASH over it
        (DW-197). Every word of the degrade above applies to it verbatim: this is
        still the last call of the cycle, the refusal still keeps every answer, and
        an EACCES/EIO here reported a fully completed cycle as crashed. It refuses
        under the same kind and a THIRD fixed token, `ledger-inaccessible`, kept
        distinct from `ledger-unreadable` because the operator repair differs
        (permissions or storage, versus editing the file) and `reason` and `error`
        are both dropped from a `bmad-loop diagnose` dump, so the token is the only
        thing that survives one. The triad reads cleanly: `ledger-absent` (not
        there), `ledger-unreadable` (there, undecodable), `ledger-inaccessible`
        (there, the OS refused). It CARRIES too, on its own latch, for the reason
        the undecodable arm carries — with the same asymmetry against absence.

        A FOURTH refusal (DW-217) sits below all three and is the only one taken
        with the ledger perfectly readable: this cycle already declared the ledger
        unfit to publish (`_ledger_unfit_to_publish`), so the open set derived from
        these bytes is not a KEEP list anybody should trust. It covers the decodable
        fault class the three reads above cannot see — a half-landed write that
        flipped a status without recording its line — where an id the aborted write
        retired would otherwise take the human's pre-answer with it, committed. It
        carries nothing: the latch it read is already `_loop`-bound.

        `_close_resolved` and `_decisions_phase` also catch `OSError` and
        `LedgerReadFault` around `mark_done_many` and `record_decision`, covering
        OS read, lock and write failures. The DW-167 re-apply gate catches its
        direct read too:
        a fault prevents repairing a stored close, leaving the entry open for a
        later cycle. Here the read protects the human's stored answers, so its
        refusal also carries to `_loop`; `_read_cycle_ledger` instead stops before
        starting work. `read_for_write` wraps OS metadata and text-read faults as
        `LedgerReadFault` (DW-279); pre-lock probes, lock and write failures
        remain raw `OSError`.
        """
        from . import decisions as decisions_store  # lazy: decisions imports sweep

        ledger = self.workspace.paths.deferred_work
        # REPAIR/WRITE (DW-146): the open set derived here decides a store write,
        # and pruning from bytes nobody could read would drop live answers.
        try:
            text = deferredwork.read_for_write(ledger)
        except (OSError, deferredwork.LedgerReadFault) as e:
            if isinstance(e, deferredwork.LedgerReadFault) and isinstance(e.__cause__, OSError):
                e = e.__cause__  # Preserve the original OS attribution.
            # THE OS REFUSED THE READ (DW-197) — EACCES, EIO, a vanished mount. The
            # ledger may be perfectly well-formed; nobody here can tell, and that is
            # precisely the undecodable case's own argument: the open set is the
            # KEEP list for a store write, so a ledger this process cannot read is
            # unknown open work, not zero of it. Same kind and same `reason`-is-a-
            # fixed-token shape as the two arms around it, under a third token,
            # with the errno text in `error` (a `_JOURNAL_DROP_FIELDS` field). The
            # class name rides beside the message because "[Errno 13] Permission
            # denied" alone does not say which refusal it was.
            self.journal.append(
                "sweep-preanswer-prune-refused",
                ledger=str(ledger),
                reason="ledger-inaccessible",
                error=f"{e.__class__.__name__}: {e}",
            )
            # ...and CARRIED, for the same reason as the decode arm below: the repeat boundary
            # commits the ledger, and cycle N+1's own read meets the same refusal.
            # Its own latch rather than the decode latch, because the two stops report
            # different closed tokens — see the declaration in `__init__`.
            self._prune_ledger_inaccessible = True
            return
        except deferredwork.LedgerReadError as e:
            # UNDECODABLE is refused for the same reason absence is (DW-182), and
            # under the same kind: the open set is the KEEP list for a store write,
            # so a ledger nobody can decode is unknown open work, not zero of it.
            # `reason` stays a FIXED token and the decode fault goes in `error`,
            # already a `diagnostics._JOURNAL_DROP_FIELDS` field. `LedgerReadError`
            # is a plain `Exception` on purpose (DW-146), so it must be named: no
            # `except OSError` upstream would ever see it.
            self.journal.append(
                "sweep-preanswer-prune-refused",
                ledger=str(ledger),
                reason="ledger-unreadable",
                error=str(e),
            )
            # ...and the refusal is CARRIED to `_loop`, beside the row rather than
            # in place of it. The repeat boundary commits the ledger, so a refusal
            # that stayed local would publish bytes nobody could decode; `_loop`
            # reads this flag right after `_cycle` and ends a repeating run on
            # `reason="ledger-unreadable"` without taking that commit. Instance
            # state, not `state` — see the declaration in `__init__`. The absence
            # arm below sets nothing: an absent ledger ends the next cycle cleanly.
            self._prune_ledger_unreadable = True
            return
        # ABSENCE is refused, not collapsed to `""` (DW-176). The `or ""` spelling
        # every observation-shaped caller uses is exact for them because
        # `open_ids("")` and `open_ids(<absent>)` say the same thing about a ledger
        # nobody is writing — but here the open set is the KEEP list for a store
        # write, so an empty one means "nothing is open, drop every answer" and a
        # ledger that vanished mid-cycle would wipe the human's whole pre-answer
        # store and (since DW-160) commit the wipe. An absent ledger is unknown
        # open work, not zero of it. The test is `is None`, never falsiness: an
        # empty-but-PRESENT ledger genuinely has zero open ids and must keep
        # pruning exactly as it does today.
        if text is None:
            self.journal.append(
                "sweep-preanswer-prune-refused", ledger=str(ledger), reason="ledger-absent"
            )
            return
        # DW-217. The read above SUCCEEDED, and that is exactly the case this arm
        # exists for: on the decodable fault class — a `record_decision` or
        # `mark_done_many` that flipped a status and then failed before writing the
        # `decision:` line — the ledger reads back perfectly, so neither the DW-182
        # undecodable refusal nor the DW-197 OS refusal fires, and the open set
        # derived from these bytes is the KEEP list for a store write. An id the
        # aborted write flipped to `done` is no longer open, so the human's
        # pre-answer for it was pruned and the deletion COMMITTED — off a ledger
        # this very cycle already declared unfit to publish.
        #
        # It sits BELOW the three read arms deliberately: they own the classes they
        # observe, each with its own fixed token and its own carry, and displacing
        # them would lose both. This is the FOURTH token and the only one taken with
        # the ledger READABLE. It sets no carry of its own — the latch it just read
        # is already on its way to `_loop` — and, like its siblings, it keeps every
        # stored answer and spawns no git.
        if self._ledger_unfit_to_publish():
            self.journal.append(
                "sweep-preanswer-prune-refused", ledger=str(ledger), reason="ledger-in-doubt"
            )
            return
        # The store lives under the project that owns `run_dir`, never
        # `self.workspace.root`: where `repo_root` names a tree DISJOINT from the
        # project the two diverge and a workspace-rooted prune trimmed a store
        # that does not exist, leaving consumed entries behind (the comment in
        # `_decisions_phase` says why the run dir is the stable anchor). Scoped
        # to the disjoint shape on purpose — in the NESTED/monorepo shape
        # (`conftest.nested_repo_root_paths`) `repo_root` is an ANCESTOR of the
        # project, so the store sits inside it and a workspace-rooted spelling
        # found the same file. The ledger read above is unaffected either way —
        # `deferred_work` hangs off `implementation_artifacts`, which stays
        # project-rooted under the override.
        project = _project_of_run_dir(self.run_dir)
        dropped = decisions_store.prune_pre_answers(project, deferredwork.open_ids(text))
        if dropped:
            self.journal.append("decision-preanswers-pruned", dw_ids=dropped)
            # The STORE FILE, not the workspace root: the same divergence that
            # made the prune miss its file made the commit miss its tree (DW-160).
            # Name the file you published, the rule `_commit_ledger` states — this
            # prune writes the pre-answer store and nothing else, and the store is
            # a bare join off the project root that no config knob can move. So
            # the commit carries that one file, and the ledger this cycle's
            # decision phase may have withheld is not published by it (DW-187).
            # The ledger PUBLISHERS name the ledger for the same rule and a
            # different answer. Guarded on `dropped` above: a prune that consumed
            # nothing wrote nothing and spawns no git.
            self._commit_ledger(
                "chore(sweep): drop consumed deferred-work pre-answers",
                path=decisions_store.store_path(project),
                family="store",
            )

    def _prune_dropped_pre_answer(
        self, dw_id: str, drop_cause: str, answer: dict[str, Any]
    ) -> None:
        """Retire the PROJECT-level pre-answer a just-dropped stale answer came
        from (DW-143). Part of the drop itself, not a later cleanup. `answer` is
        the value this run just dropped, and the store entry goes ONLY while it
        still equals it — see the provenance guard below.

        Why it exists: DW-124's quarantine is RUN-scoped by design, so it bounds
        the drop to one announcement per run and a NEW run re-evaluates from
        scratch. But the answer that feeds a stale drop lives in the project store,
        `pending_missed_decisions` filters out any id already usably answered
        there, and `_prune_pre_answers` retires an entry only once a later cycle
        bundles the id and closes it. While triage keeps re-asking the id as a
        DECISION instead, that never happens: every new run re-read the same stale
        answer, re-dropped it and re-notified, and no surface re-offered the id.
        Removing the entry at the drop breaks that loop from both ends — the next
        run reads no stale answer, and `bmad-loop decisions` offers the id again.

        Keep-open-only, deliberately. A dropped `build` answer (`no-intent`,
        `name-collision`) leaves its entry open to be re-asked with the stored
        answer still meaningful, where a dropped keep-open answer has no payload
        left beyond the option it named — there is nothing to preserve.

        Called AFTER `_quarantine`, which is the announce-then-persist order that
        method's docstring promises: the residual crash window (announced,
        quarantined, store not yet pruned) resumes into the DW-124 skip and the
        entry is pruned by the next run that re-drops it. The reverse order would
        leave a window in which a human's answer is already gone while the run has
        no record of having dropped it.

        Reaches the project store ONLY. `<run>/decisions.json` keeps the answer
        (the run-local audit trail is untouched by design) and so does whatever
        `decision:` line `_apply_decision_effect` landed — since DW-186 that call
        can report it wrote none, and this drop is unchanged either way. The journal
        row carries the id and the drop cause alone — no answer prose, no store
        path.

        Retires the entry ONLY while it still holds the value that was dropped.
        The dropped `answer` is this run's RUN-LOCAL copy, and `_decisions_phase`
        lets that copy win over the project store for the rest of the run — so a
        human who re-answers the id out of band while the run is paused
        (`pending_missed_decisions` screens against the store alone, never against
        a run's `decisions.json`) leaves a NEWER store entry this run has never
        evaluated. Keyed on the id alone, the removal deleted that replacement, and
        committed the deletion, on the strength of a stale copy the human had
        already superseded. `drop_pre_answer` compares before it deletes: a seeded
        copy round-trips through JSON unchanged and so equals the entry it came
        from, while a re-answer differs in at least `answered_at`, and an
        interactive in-run answer never equals a store entry at all. The surviving
        replacement is left for the NEXT run to evaluate from scratch, exactly as
        a fresh answer would be; this run stays on its own record."""
        from . import decisions as decisions_store  # lazy: decisions imports sweep

        # `_project_of_run_dir`, never `self.workspace.root`: where `repo_root`
        # names a tree DISJOINT from the project the two diverge and only the run
        # dir stays anchored to the project that owns the store (see
        # `_decisions_phase` and `_prune_pre_answers`, which resolve it the same
        # way). The nested/monorepo shape is unaffected — `repo_root` is an
        # ancestor there, so the store sits inside it.
        project = _project_of_run_dir(self.run_dir)
        if not decisions_store.drop_pre_answer(project, dw_id, answer=answer):
            # Either no store entry (a run-local-only answer) or an entry that is
            # no longer the value dropped (a human's later replacement): no write,
            # no row — the store's bytes are untouched either way.
            return
        self.journal.append(
            "sweep-decision-preanswer-pruned", decision=dw_id, drop_cause=drop_cause
        )
        # Committed like `_prune_pre_answers`': `_materialize_bundles` runs ahead of
        # this cycle's bundles, and bundles need a clean baseline. The STORE FILE for
        # the same reason the removal used it — name the file you published
        # (`_commit_ledger`), and the store is a bare join off the project root.
        # Where `repo_root` names a DISJOINT tree, `workspace.root` is a separate
        # repo and a clean check there says nothing about the tree this write
        # dirtied (DW-160). Narrowed to the store, this site is also the one DW-187
        # is about: it runs LATER in the same cycle as the decision phase, so a wide
        # commit here republished the very ledger bytes that phase withheld.
        # Guarded on `drop_pre_answer` above: no store entry, no write, no git.
        self._commit_ledger(
            "chore(sweep): drop stale deferred-work pre-answer",
            path=decisions_store.store_path(project),
            family="store",
        )

    def _drive_story(self, task: StoryTask) -> None:
        # no spec-approval gate for bundles: the bundle intent came from the
        # validated triage plan (and, for decision bundles, from the human).
        # The base _run_story wraps this in a worktree when isolation=worktree.
        if self._dev_phase(task):
            self._review_and_commit(task)

    # cycle 1 keeps the legacy key so pre-repeat paused runs resume unchanged;
    # "dw{N}-" (not "dw-c{N}-") so a cycle-1 bundle named "c2-foo" can never
    # collide with a cycle-2 bundle named "foo"
    def _bundle_key(self, name: str, cycle: int) -> str:
        return f"dw-{name}" if cycle == 1 else f"dw{cycle}-{name}"

    def _bundle_name_for(self, bundle: Bundle, cycle: int) -> tuple[str, int] | None:
        """The name this bundle runs under and the attempt that found it, or
        None when no key is available. Pure apart from the exhaustion record:
        the dedupe record belongs to `_run_bundle`, which is the only caller
        that knows whether the bundle went on to USE the deduped name.

        The terminal-task early return below is what makes a resume cheap: a
        bundle already finished this run is skipped rather than re-driven. It
        used to compare the KEY alone (DW-125), and the key is a pure function of
        `(name, cycle)` — so when a resume loses `<run>/triage.json`,
        `_ensure_triage` regenerates a plan whose names are re-authored freely,
        and a fresh bundle that happens to reuse a finished bundle's name was
        silently swallowed with its ids never run. `_materialize_bundles`'
        uniqueness pass cannot see this: it compares names against THIS cycle's
        list, never against persisted state.

        The bundle's identity is its `dw_ids`, so agreement is tested on those,
        as SET equality — a regenerated triage may emit the same ids in a
        different order, and treating that as a new bundle would re-run finished
        work on every cache-loss resume, a worse regression than the bug. A
        persisted EMPTY list agrees with anything: it is the pre-`dw_ids`
        `state.json` shape (`model.py` loads a missing key as `[]`), and reading
        it as divergence would re-run every bundle of every legacy paused run.

        On divergence the name gains the same bounded `-2` … `-9` suffix
        `_materialize_bundles` applies to a colliding stored name — deduping the
        NAME rather than the key alone is what keeps `_bundle_key`, the intent
        dirname and `_ensure_bundle_intent`'s key→name round-trip consistent.
        This is the third collision remedy in this file and must not be confused
        with the other two: `_materialize_bundles` DISCARDS a colliding stored
        `bundle_name` (it has `decision-<id>` beneath it) and SUFFIXES that
        fallback (which has nothing beneath it). Here a validated plan name
        collides with PERSISTED state, and suffixing is the only repair — there
        is no fallback name to reach for.

        Scoped to TERMINAL tasks deliberately: an in-flight task at the key still
        goes through `_recover_inflight_bundle` exactly as before
        (`_finish_inflight_bundles` drives persisted bundles terminal before a
        cycle picks new work, and `_warn_stranded_bundles` says so loudly when
        one survives)."""
        wanted = set(bundle.dw_ids)
        for attempt in range(1, 10):
            name = bundle.name if attempt == 1 else f"{bundle.name}-{attempt}"
            task = self.state.tasks.get(self._bundle_key(name, cycle))
            if task is not None and task.terminal and task.dw_ids and set(task.dw_ids) != wanted:
                continue
            return name, attempt
        # Bounded, so the search is provably finite — and loud on both surfaces,
        # because the alternative is the swallowed bundle this guard exists to
        # prevent. The ids stay open for the next sweep.
        self.journal.append(
            "sweep-bundle-key-collision", name=bundle.name, dw_ids=list(bundle.dw_ids)
        )
        # Spell the KEYS, not the bare name: from cycle 2 they are `dw<N>-...`,
        # so a name-only message names nothing the operator can grep state.json
        # for.
        first = self._bundle_key(bundle.name, cycle)
        gates.notify(
            self.policy,
            self.run_dir,
            f"sweep bundle {bundle.name!r} could not be named",
            f"every key from {first} through {first}-9 (cycle {cycle}) is held by a "
            "finished bundle carrying different deferred-work ids; not run: "
            + ", ".join(bundle.dw_ids),
        )
        return None

    def _reset_superseded_bundle_state(self, task: StoryTask) -> None:
        """Drop the per-bundle state a reset task still carries from the bundle it
        was minted for, once `_run_bundle` adopts a DIFFERENT bundle's ids onto it
        (DW-162, DW-163, DW-165). The four ids are one behavior: on divergent
        adoption the task stops owning the superseded bundle's state, so the clears
        live together and this docstring is the record of what is deliberately
        absent from them.

        Every field cleared here NAMES or BUDGETS the superseded bundle:

        - ``bundle_closes_intended`` -- the previous bundle's intended ledger
          closes. ``_carry_isolated_ledger_writes`` and the engine's post-rollback
          replay predicate both key on it, so a stale list closes ids this task
          never ran.
        - ``spec_file`` -- the superseded bundle's amended contract.
          ``_generic_bundle_prompt`` selects the restore-review prompt naming it
          (paired with ``restore_patch``), and ``Engine._record_dev_spec`` is a
          no-op once set, so a survivor would also refuse the replacement bundle's
          own spec on escalation.
        - ``restore_patch`` -- the diff of the superseded bundle's attempt.
        - ``preserve_from_attempt`` -- the claim that ``preserve_ref`` holds THIS
          task's attempt. The ref itself stays (below); the flag goes so the retry
          dev prompt does not offer the superseded bundle's work as this one's
          (#777). Git cannot tell the two apart: same run, key and baseline.
        - ``attempt`` + ``review_cycle`` + ``followup_reviews_spent`` -- reset the
          retry and review counters; clear the associated ``defer_reason`` and
          advance ``generation`` for fresh session ids. These operations follow
          ``runs._rearm_escalation_locked``. A sweep bundle runs the base engine's
          review loop, so a
          replacement inheriting an exhausted review budget would force-converge or
          defer on its first round. ``attempt`` and ``generation`` in particular are
          inseparable: zeroing ``attempt`` alone re-mints a byte-equal session id
          (see ``_rearm_generation``).

        ``resolved_redrive`` is deliberately NOT cleared (DW-165's recorded
        2026-09-07 decision): it records that a HUMAN resolved this task, which is
        a fact about the task, not a statement about which spec the task owns.
        Nor is any of the following, each for its own reason:

        - ``sessions`` -- an append-only audit trail; ``_rearm_generation``'s fresh
          id namespace is what keeps the replacement's records distinct.
        - ``bundle_file`` -- the single most bundle-naming field here, and the one
          exception: the caller OVERWRITES it two lines later with this bundle's
          freshly written ``intent.md``, so clearing it would be dead code. Nothing
          reads it in between.
        - ``isolated_ledger_carried`` / ``harvest_carry_commit_pending`` -- the
          ledger-carry replay latches (``Engine._replay_unlatched_ledger_carries``
          skips a task already latched, which WOULD strand the replacement
          bundle's own close). Safe because both are set only on legs that have
          already reached a TERMINAL phase -- the ``_defer`` leg (DEFERRED) and
          past a unit merge (DONE / AWAITING_OPERATOR) -- and ``_run_bundle``
          returns on a terminal task before ever reaching this branch.
        - ``commit_sha`` -- names a commit that really happened; a later commit
          overwrites it.
        - ``rearmed`` -- ``_recover_inflight_bundle`` already cleared it on the
          reset that got us here.
        - ``preserve_ref`` / ``preserve_partial`` -- a ref to a rolled-back
          worktree that still exists on disk; clearing the name would orphan it
          rather than release it. Its provenance flag is cleared instead (above).
        - the ``baseline_*`` pair and ``worktree_path`` / ``branch`` -- mount and
          rollback anchors owned by the reset, not by either bundle.
        - ``dispatched_spec_file`` / ``dispatched_spec_snapshot`` -- ``Sweep``
          overrides ``_dispatched_spec_for_attempt`` to ``None`` and
          ``_requires_dispatched_spec_snapshot`` to ``False``, so a sweep task never
          binds them and a clear would be unablatable dead code.

        These clears are DEFENSIVE. No reachable sequence was demonstrated that
        strands a task with a superseded ``spec_file`` / ``restore_patch``
        (DW-162, DW-165) -- but ``_warn_stranded_bundles`` concedes an in-flight
        survivor is possible at all, so the guard makes the hazard structurally
        impossible instead of argued unreachable.

        An EMPTY persisted ``task.dw_ids`` reaches here and takes the FULL reset:
        it satisfies the divergence gate, which is exactly the reading
        ``_run_bundle`` already takes of an empty list for id adoption (that task
        genuinely has no ids and must take the bundle's). Nothing it holds is
        exempt on account of having no ids.

        Why the rearm is gated on DIVERGENCE here rather than added to
        ``_recover_inflight_bundle``'s reset tail: that tail mirrors
        ``Engine._finish_inflight``'s restart arm, which deliberately KEEPS a plain
        crash-restart's budget -- zeroing it there would let a crash-looping run
        never exhaust ``limits.max_attempts``. The two siblings that do zero
        (``_ensure_migration``'s reset, the triage reset) each gate on
        ``Phase.ESCALATED``, i.e. "the human resumed deliberately"; ESCALATED is
        terminal and so never reaches ``_recover_inflight_bundle`` at all.
        Divergent adoption is this seam's equivalent gate. Do not widen the scope
        to the agreeing (or merely reordered) re-dispatch: that is the same bundle
        the task already attempted, and its budget, spec ownership and intended
        closes are rightfully its own.
        """
        task.bundle_closes_intended = []
        task.artifact_baseline = None
        task.artifact_destination = None
        task.artifact_source_digests = None
        task.artifact_tracked_source_oids = None
        task.artifact_acceptance_identity = None
        task.artifact_payload = None
        task.artifact_publication_complete = False
        task.spec_file = None
        task.restore_patch = None
        task.preserve_from_attempt = False
        task.attempt = 0
        task.review_cycle = 0
        task.followup_reviews_spent = 0
        task.defer_reason = None
        _rearm_generation(task)

    def _run_bundle(self, bundle: Bundle, cycle: int) -> str | None:
        """Run one bundle; returns the task key it ran under, or None when no key
        was available (see `_bundle_name_for`). `_cycle` grades progress on the
        returned keys, so it must never be re-derived from `bundle.name`."""
        resolved = self._bundle_name_for(bundle, cycle)
        if resolved is None:
            return None
        name, attempt = resolved
        key = self._bundle_key(name, cycle)
        task = self.state.tasks.get(key)
        if task is not None and task.terminal:
            return key  # finished (or adjudicated) in a previous resume cycle
        if attempt > 1:
            # Below the skip deliberately: the ordinary second-resume shape has
            # the deduped key ALREADY terminal and agreeing, and journaling the
            # rename up in the resolver re-announced it once per resume for a
            # bundle nothing then renamed. `original=` + `name=` so the record
            # stands on its own, the way its sibling `sweep-bundle-name-deduped`
            # does; `dw_ids` say which work the new key carries.
            self.journal.append(
                "sweep-bundle-key-deduped",
                original=bundle.name,
                name=name,
                attempt=attempt,
                dw_ids=list(bundle.dw_ids),
            )
        if task is None:
            task = StoryTask(story_key=key, epic=0, dw_ids=list(bundle.dw_ids))
            self.state.tasks[key] = task
            self.journal.append("bundle-start", story_key=key, dw_ids=list(bundle.dw_ids))
        elif self._recover_inflight_bundle(task):
            return key
        else:
            # DW-144 (+DW-162, DW-163, DW-165). Recovery reset the task to
            # PENDING and handed the dispatch back to us — and the intent written
            # below is THIS bundle's, not the one the persisted task was minted
            # for. `_bundle_name_for`'s dedupe is scoped to TERMINAL tasks, so a
            # non-terminal task at the key keeps the key whatever its ids are.
            # Stale task ids can reject a dev result for this bundle or make
            # `_close_bundle_ledger_when_spec_status` derive
            # `bundle_closes_intended` from the previous bundle's ids.
            #
            # Journal only on divergence but assign unconditionally: a bundle's
            # identity is its ids under SET equality (a regenerated triage may
            # reorder them, per `_bundle_name_for`), so a pure reorder is not
            # worth announcing once per resume. A persisted EMPTY list is the
            # pre-`dw_ids` `state.json` shape and reads as divergence here, which
            # is right — that task genuinely has no ids and must take these.
            if set(task.dw_ids) != set(bundle.dw_ids):
                self.journal.append(
                    "sweep-bundle-dwids-adopted",
                    story_key=key,
                    previous_dw_ids=list(task.dw_ids),
                    dw_ids=list(bundle.dw_ids),
                )
                # DW-162/163/165. Ids are not the only per-bundle field the reset
                # task carries: everything else naming or budgeting the superseded
                # bundle goes with them. It rides THIS gate — the same divergence
                # test the append above rides — and nothing else about its
                # placement is load-bearing: it never touches `task.dw_ids`.
                self._reset_superseded_bundle_state(task)
            task.dw_ids = list(bundle.dw_ids)
        dirname = name if cycle == 1 else f"c{cycle}-{name}"
        # The document has to agree with the directory it lands in and with the
        # name `_ensure_bundle_intent` recovers back out of the story key.
        written = bundle if name == bundle.name else replace(bundle, name=name)
        task.bundle_file = str(self._write_intent(written, dirname))
        self._save()
        self._emit("pre_bundle", task)
        self._run_story(task)
        self._emit("post_bundle", task)
        return key

    def _recover_inflight_bundle(self, task: StoryTask) -> bool:
        """Recover a bundle task interrupted mid-flight (or re-armed after a
        human resolved its escalation via `bmad-loop resolve`): the same recovery
        as Engine._finish_inflight, including the re-drive latch so a
        human-resolved escalation is protected through every reset (mirrors
        engine.py:1163-1169).

        Returns True when the persisted PROCEED receipt carried the bundle all
        the way through accepted sync, review, and commit — the caller is done.
        Returns False once the task has been reset to PENDING, leaving the caller
        to dispatch it. A bare DEV_VERIFY + spec_file shape is insufficient: the
        pre-action decision save has the same shape for rejected decisions.

        Deliberately narrower than the base _finish_inflight: no
        `_resumable_session` arm, so a bundle whose host died in the
        post-session window still restarts rather than replaying its recorded
        result. Lifting that is a resume-fidelity change of its own. The
        COMMITTING window IS recovered, though — same as the base engine's
        resume-commit arm (#115). The base's `_pending_salvage_session` replay
        (DW-278) is not mirrored either: a bundle whose review-timeout salvage
        latched `salvage_refile_pending` — at its handoff save, or at the
        refile's repair pause — restarts here like every other post-session
        window, so the restart arm below CLEARS the latch as the base restart
        arm does (#794 review). Left set, the abandoned product's latch would
        ride onto the replacement attempt and force `_review_and_commit` down
        the review path it exists to bypass for a latched replay. Mirroring the
        replay is the same resume-fidelity change as the `_resumable_session`
        arm and is deferred with it.

        The reset tail below deliberately does NOT zero `attempt` or re-arm the
        session-id generation: like the base restart arm it mirrors, a plain
        crash-restart keeps its budget, so zeroing here would let a crash-looping
        run never exhaust `limits.max_attempts`. The fresh budget belongs to the
        ADOPTION site instead — `_run_bundle`'s divergence branch, via
        `_reset_superseded_bundle_state`, which is reached only when the caller
        hands this task a different bundle's ids. Those superseded-state clears
        are defensive: no reachable sequence was demonstrated for DW-162 or
        DW-165; the adoption-site guard makes that hazard structurally impossible.
        """
        if task.worktree_path:
            # Sweep replaces Engine._loop, so it performs Engine._finish_inflight's
            # mount-relative re-anchor itself. Accepted receipts reopen this mount
            # regardless of live policy; restart is the only path allowed to release
            # or discard its ownership before future work begins.
            task.rebase_spec_paths_on(Path(task.worktree_path))
        mounted = bool(task.worktree_path)
        restart_isolated = self._isolated and mounted
        if task.phase == Phase.COMMITTING:
            # the gate+advance save landed pre-death; finish the commit
            # instead of rolling verified bundle work back (see
            # Engine._finalize_commit_phase for the re-drive contract).
            self.journal.append("resume-commit", story_key=task.story_key)
            if mounted:
                unit = self._reopen_unit(task)
                prev = self.workspace
                self.workspace = unit.workspace
                try:
                    self._finalize_commit_phase(task)
                finally:
                    self.workspace = prev
                self._integrate_unit(task, unit)
            else:
                self._finalize_commit_phase(task)
            return True
        self.journal.append("resume-restart", story_key=task.story_key, phase=str(task.phase))
        if (
            task.phase == Phase.DEV_VERIFY
            and task.spec_file
            and self._accepted_dev_session_matches(task)
        ):
            self._save()
            if mounted:
                unit = self._reopen_unit(task)
                prev = self.workspace
                self.workspace = unit.workspace
                try:
                    self._resume_after_dev_verify(task)
                finally:
                    self.workspace = prev
                self._integrate_unit(task, unit)
            else:
                self._resume_after_dev_verify(task)
            return True
        if restart_isolated:
            # drop the half-built worktree; _run_story mounts a fresh one
            self._discard_unit_for_restart(task)
        elif mounted:
            # Live in-place policy applies to the replacement attempt, not to an
            # incomplete attempt's mount-owned baselines, paths, and claims.
            self._release_orphaned_mount(task)
        # Abandoning this product's salvage retry: the replacement attempt owes
        # its own review decision, not the latched replay's. Cleared BEFORE the
        # rollback below, as the base restart arm does, so a rollback pause
        # persists the task unlatched.
        task.salvage_refile_pending = False
        if not restart_isolated and task.baseline_commit:
            # latch resolved_redrive so the corrected spec + restored diff stay
            # protected through every reset of this re-drive, not just this
            # first one; cause="resolved" keeps a human-initiated re-arm clear of
            # the policy pause regardless of scm.rollback_on_failure. Unsafe
            # attempt-owned authority may still require manual recovery.
            task.resolved_redrive = task.resolved_redrive or task.rearmed
            self._rollback_or_pause(task, cause="resolved" if task.rearmed else "stopped")
        task.rearmed = False  # past rollback (only reached when not paused)
        task.phase = Phase.PENDING  # deliberate reset, not a normal transition
        return False

    # ------------------------------------------------------------ migration

    @staticmethod
    def _migration_manifest(text: str) -> list[dict[str, Any]]:
        return [
            {
                "key": entry.key,
                "id": entry.id,
                "title": entry.title,
                "section": entry.section,
                "done": entry.done,
                "severity": entry.severity,
            }
            for entry in deferredwork.parse_legacy(text)
        ]

    def _migration_evidence_failure(self, task: StoryTask, detail: str) -> NoReturn:
        """Refuse current-format recovery evidence without mutating the ledger."""
        self.journal.append(
            "sweep-migration-recovery-invalid",
            story_key=MIGRATE_KEY,
            detail=detail,
        )
        self._escalate(task, f"migration recovery evidence is invalid: {detail}")
        raise AssertionError("migration escalation returned")

    def _migration_record_text(
        self, task: StoryTask, path: Path, label: str, *, optional: bool = False
    ) -> str | None:
        root = _project_of_run_dir(self.run_dir)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_BINARY", 0)
        parent_fd: int | None = None
        fd: int | None = None
        try:
            if DIR_FD_ANCHORED_WRITES:
                parent_fd = open_dir_confined(root, path.parent)
                if parent_fd is None:
                    raise _MigrationRecordInvalid(f"unconfined {label} record")
                fd = os.open(path.name, flags, dir_fd=parent_fd)
            else:
                try:
                    path.lstat()
                except (FileNotFoundError, NotADirectoryError):
                    if optional:
                        return None
                    raise _MigrationRecordInvalid(f"missing {label} record")
                if not path_is_confined(root, path):
                    raise _MigrationRecordInvalid(f"unconfined {label} record")
                fd = os.open(path, flags)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _MigrationRecordInvalid(f"nonregular {label} record")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = None
                return stream.read()
        except _MigrationRecordInvalid as e:
            # Keep escalation outside the OSError catch below: if its journal,
            # notification, or state save refuses, that boundary fault must
            # propagate once rather than be mistaken for another record fault.
            self._migration_evidence_failure(task, str(e))
        except (FileNotFoundError, NotADirectoryError):
            if optional:
                return None
            self._migration_evidence_failure(task, f"missing {label} record")
        except (OSError, UnicodeDecodeError):
            self._migration_evidence_failure(task, f"unreadable {label} record")
        finally:
            if fd is not None:
                os.close(fd)
            if parent_fd is not None:
                os.close(parent_fd)

    def _remove_migration_record(self, path: Path) -> None:
        """Remove a stale run record without following redirected ancestors."""
        root = _project_of_run_dir(self.run_dir)
        if DIR_FD_ANCHORED_WRITES:
            parent_fd = open_dir_confined(root, path.parent)
            if parent_fd is None:
                raise OSError(f"cannot confine stale migration record {path.name}")
            try:
                try:
                    os.unlink(path.name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            finally:
                os.close(parent_fd)
            return
        try:
            path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return
        if not path_is_confined(root, path):
            raise OSError(f"cannot confine stale migration record {path.name}")
        path.unlink()

    def _migration_baseline_and_manifest(self, task: StoryTask) -> tuple[str, list[dict[str, Any]]]:
        baseline = self._migration_record_text(
            task, self.run_dir / _MIGRATE_BASELINE_RECORD, "baseline"
        )
        assert baseline is not None
        raw_manifest = self._migration_record_text(
            task, self.run_dir / _MIGRATE_MANIFEST_RECORD, "manifest"
        )
        assert raw_manifest is not None
        try:
            manifest = json.loads(raw_manifest)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
            self._migration_evidence_failure(task, "malformed manifest record")
        if not isinstance(manifest, list) or not all(isinstance(item, dict) for item in manifest):
            self._migration_evidence_failure(task, "manifest record is not an object list")
        expected = self._migration_manifest(baseline)
        if manifest != expected:
            self._migration_evidence_failure(task, "manifest disagrees with accepted baseline")
        return baseline, manifest

    def _migration_result_evidence(
        self,
        task: StoryTask,
        baseline: str,
        manifest: list[dict[str, Any]],
        rewrite: str,
    ) -> dict[str, Any]:
        raw = self._migration_record_text(task, self.run_dir / _MIGRATE_RESULT_RECORD, "result")
        assert raw is not None
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
            self._migration_evidence_failure(task, "malformed result record")
        if not isinstance(result, dict):
            self._migration_evidence_failure(task, "result record is not a JSON object")
        errors = validate_migration(result, manifest, snapshot_canonical(baseline), rewrite)
        if errors:
            self._migration_evidence_failure(
                task, "result record is inconsistent with accepted migration"
            )
        return result

    def _restore_accepted_migration(self, task: StoryTask, baseline: str, rewrite: str) -> None:
        """Restore a validated rewrite by compare-and-set, never over a rival."""
        try:
            current_head = verify.rev_parse_head(self.workspace.root)
        except verify.GitError:
            self._migration_evidence_failure(task, "migration baseline cannot be verified")
        if not task.baseline_commit or current_head != task.baseline_commit:
            self._migration_evidence_failure(task, "repository advanced beyond migration baseline")
        try:
            current = deferredwork.read_for_write(self.workspace.paths.deferred_work)
        except (deferredwork.LedgerReadError, OSError):
            self._migration_evidence_failure(
                task, "live ledger cannot be compared with recovery records"
            )
        if current not in (baseline, rewrite):
            self._migration_evidence_failure(
                task, "live ledger diverged from migration recovery records"
            )
        self._safe_reset(task)
        diverged = False
        ledger = self.workspace.paths.deferred_work
        with deferredwork.ledger_lock(ledger):
            try:
                current = deferredwork.read_for_write(ledger)
            except (deferredwork.LedgerReadError, OSError):
                current = None
                diverged = True
            if not diverged and current == rewrite:
                ledger.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(ledger, baseline)
            elif not diverged and current == baseline:
                pass
            else:
                diverged = True
        if diverged:
            self.journal.append(
                "sweep-migration-restore-diverged",
                story_key=MIGRATE_KEY,
                ledger=str(ledger),
            )
            self._escalate(
                task,
                "the ledger changed underneath the failed migration attempt — re-run the sweep",
            )

    def _finish_migration_commit(
        self,
        task: StoryTask,
        baseline: str,
        manifest: list[dict[str, Any]],
        rewrite: str,
    ) -> bool:
        """Run the idempotent publication tail; return whether DONE was earned."""
        ledger = self.workspace.paths.deferred_work
        try:
            live = deferredwork.read_for_write(ledger)
        except (deferredwork.LedgerReadError, OSError):
            self._migration_evidence_failure(task, "live ledger cannot be verified for publication")
        if live != rewrite:
            self._migration_evidence_failure(task, "live ledger differs from accepted rewrite")
        had_doubt = self.state.sweep_ledger_in_doubt
        if not had_doubt:
            # Claim the next doubt before entering the helper: its refusal arm
            # persists the run flag internally, so a host death immediately
            # after that save must not strand an apparently inherited doubt.
            task.migration_ledger_doubt_owned = True
            self._save()
        outcome = self._commit_ledger(
            "chore(sweep): migrate legacy deferred-work entries to DW format",
            path=self.workspace.paths.deferred_work,
            family="ledger",
            accepted_text=rewrite,
            accepted_baseline_text=baseline,
            accepted_baseline_commit=task.baseline_commit,
        )
        if task.migration_ledger_doubt_owned and not self.state.sweep_ledger_in_doubt:
            # A Git-only unavailable outcome arms no ledger doubt. Retire the
            # provisional claim so it can never clear a later phase's evidence.
            task.migration_ledger_doubt_owned = False
            self._save()
        if outcome in ("refused", "unavailable"):
            # Returning would let Engine._run_inner stamp the whole run finished,
            # which makes the public resume command refuse the durable COMMITTING
            # task. Use the existing crash/resume channel; the helper's journal
            # row retains the specific refusal/unavailable diagnosis.
            raise RuntimeError(f"migration ledger publication {outcome}")
        if outcome == "clean":
            try:
                confirmed = deferredwork.read_for_write(ledger)
            except (deferredwork.LedgerReadError, OSError):
                self._migration_evidence_failure(
                    task, "clean publication cannot confirm the accepted rewrite"
                )
            if confirmed != rewrite:
                self._migration_evidence_failure(
                    task, "clean publication no longer contains the accepted rewrite"
                )
        if task.migration_ledger_doubt_owned:
            task.migration_ledger_doubt_owned = False
            self.state.sweep_ledger_in_doubt = False
            self._ledger_doubt_inherited = False
        advance(task, Phase.DONE)
        self._save()
        post = deferredwork.parse_ledger(rewrite)
        self.journal.append(
            "sweep-migrated",
            converted=len(manifest),
            entries_now=len(post),
            open_now=sum(1 for entry in post if entry.open),
        )
        self._emit("post_migrate", task)
        return True

    def _migration_input_is_current(self, expected: str) -> bool:
        """Whether the authoritative ledger still equals this cycle's input."""
        try:
            return deferredwork.read_for_write(self.workspace.paths.deferred_work) == expected
        except (deferredwork.LedgerReadError, OSError):
            return False

    def _retire_migration_dispatch_authority(
        self,
        task: StoryTask,
        *,
        refund_attempt: bool,
    ) -> NoReturn:
        """Persist no-launch authority before best-effort record retirement.

        A cleanup fault is intentionally allowed to propagate only after the
        durable state says PENDING with no baseline or current-format marker.
        Thus leftover files are inert evidence, never recovery authority.
        """
        task.phase = Phase.PENDING
        task.baseline_commit = None
        task.baseline_untracked = None
        task.migration_recovery_format = 0
        if refund_attempt and task.attempt > 0:
            task.attempt -= 1
        self._save()
        for name in (
            _MIGRATE_BASELINE_RECORD,
            _MIGRATE_MANIFEST_RECORD,
            _MIGRATE_REWRITE_RECORD,
            _MIGRATE_RESULT_RECORD,
        ):
            self._remove_migration_record(self.run_dir / name)
        raise RuntimeError("migration ledger changed before adapter launch")

    def _ensure_migration(self, text: str) -> None:
        """Pre-DW-format ledger content (older BMAD-method projects) blocks a
        sweep: open_ids() cannot see it and mark_done() cannot flip it. One
        LLM session rewrites the legacy items into canonical DW entries; the
        orchestrator pins exactly what to convert (a manifest from
        parse_legacy), validates the rewrite deterministically, and restores
        the original ledger before any retry."""
        ledger = self.workspace.paths.deferred_work
        task = self.state.tasks.get(MIGRATE_KEY)
        if task is None:
            task = StoryTask(story_key=MIGRATE_KEY, epic=0)
            self.state.tasks[MIGRATE_KEY] = task
        elif (
            task.phase == Phase.PENDING
            and task.migration_recovery_format == _MIGRATION_RECOVERY_FORMAT
            and task.baseline_commit is not None
        ):
            # A host can die after the recovery marker and baseline are durable
            # but before the manifest is published or a session is dispatched.
            # PENDING proves no attempt-owned rewrite exists, so retire that
            # interrupted attempt's rollback authority before stamping the
            # current (possibly operator-repaired) HEAD below.
            task.baseline_commit = None
            task.baseline_untracked = None
            self._save()
        elif task.phase == Phase.COMMITTING:
            if task.migration_recovery_format != _MIGRATION_RECOVERY_FORMAT:
                self._migration_evidence_failure(task, "commit tail has no recovery marker")
            baseline, manifest = self._migration_baseline_and_manifest(task)
            rewrite = self._migration_record_text(
                task, self.run_dir / _MIGRATE_REWRITE_RECORD, "rewrite"
            )
            assert rewrite is not None
            self._migration_result_evidence(task, baseline, manifest, rewrite)
            self._finish_migration_commit(task, baseline, manifest, rewrite)
            return
        elif task.phase == Phase.TRIAGE_VERIFY and task.migration_recovery_format not in (
            0,
            _MIGRATION_RECOVERY_FORMAT,
        ):
            self._migration_evidence_failure(task, "unknown migration recovery marker")
        elif (
            task.phase == Phase.TRIAGE_VERIFY
            and task.migration_recovery_format == _MIGRATION_RECOVERY_FORMAT
        ):
            baseline, manifest = self._migration_baseline_and_manifest(task)
            rewrite = self._migration_record_text(
                task,
                self.run_dir / _MIGRATE_REWRITE_RECORD,
                "rewrite",
                optional=True,
            )
            if rewrite is not None:
                raw_result = self._migration_record_text(
                    task,
                    self.run_dir / _MIGRATE_RESULT_RECORD,
                    "result",
                    optional=True,
                )
                if raw_result is not None:
                    self._migration_result_evidence(task, baseline, manifest, rewrite)
                    advance(task, Phase.COMMITTING)
                    self._save()
                    self._finish_migration_commit(task, baseline, manifest, rewrite)
                    return
                # Validation completed and the accepted rewrite became durable,
                # but result publication did not. Put the accepted legacy input
                # back before redispatching; no stale rewrite is ever consumed by
                # the replacement TRIAGE_RUNNING session.
                self._restore_accepted_migration(task, baseline, rewrite)
                text = baseline
                task.phase = Phase.PENDING
                task.baseline_commit = None
                task.baseline_untracked = None
                self._save()
            else:
                # Once a current-format task reaches TRIAGE_VERIFY the accepted
                # rewrite is required evidence. Treat a publication failure or
                # crash before that record as corruption; only an unmarked
                # pre-upgrade task may use reset-and-reread recovery.
                self._migration_evidence_failure(task, "missing accepted rewrite record")
        elif task.phase != Phase.PENDING:
            # resumed mid-migration or retrying after an escalation: restart
            self.journal.append("resume-restart", story_key=MIGRATE_KEY, phase=str(task.phase))
            if task.phase == Phase.ESCALATED:
                task.attempt = 0  # the human resumed deliberately; fresh budget
                _rearm_generation(task)  # ...and into a fresh session-id namespace
            if task.baseline_commit and not verify.worktree_clean(self.workspace.root):
                self._safe_reset(task)  # a session died mid-rewrite; restore our ledger
                # REPAIR/WRITE (DW-146): the restored text this migration grades.
                text = deferredwork.read_for_write(ledger) or ""
            task.phase = Phase.PENDING  # deliberate reset, not a normal transition
        # **The invariant: a refusal that dispatches nothing leaves this task
        # owning NO baseline.** It takes both halves below. Sitting above the
        # stamp keeps a fresh entry from taking one; clearing handles the entry
        # that arrives already holding one, which the resume-after-escalation
        # branch above does. Either way the next resume re-stamps the repaired
        # HEAD. Leave a baseline behind and it names the PRE-repair tree: the
        # operator renumbers and resumes, and when that migration session
        # env-faults, the next resume's `_safe_reset` rewinds to that stale
        # baseline — destroying the repair and any commits beside it, and
        # landing back on this same pause. Safe to clear because nothing of
        # ours is outstanding here: no session has run, and the branch above
        # has already restored the tree if a previous one died mid-rewrite.
        # This still sits BELOW that branch, because the ledger it reads must
        # be the restored one.
        #
        # Refused BEFORE a rewrite is dispatched, not after one is graded (#519).
        # A ledger where one id names two entries is corrupt in a way the format
        # cannot express, and there is no automatic outcome that is right: this
        # mode is required to keep pre-existing entries byte-identical, so the
        # only rewrite that preserves the pair trips `duplicate DW ids` in
        # validate_migration, and the only rewrite that passes is a collapse
        # that drops one twin's `gate:` silently. Grading the collapse instead
        # cannot be made safe — a snapshot keyed by id describes a merged entry
        # that never existed, and each half hardened on its own opens the next
        # cross-product. So a human renumbers, which is the call
        # `_apply_deferred_closes` already makes on a duplicate id (#286).
        # Paused rather than ESCALATED, and the task stays PENDING on purpose:
        # that is what makes the refusal re-askable the way `_refuse_gated_story`
        # is. The operator renumbers the ids and resumes, and this re-reads the
        # ledger and lets the migration run — an ESCALATED task would also spend
        # the migration attempt budget on a rewrite that never happened.
        #
        # PAUSE_STORY_GATE, NOT PAUSE_ESCALATION, and the pairing is the point:
        # the stage picks the recovery UI, and every escalation action
        # (`runs.rearm_story`, the TUI's Resolve) requires the task to be
        # Phase.ESCALATED, which this one deliberately is not — so an escalation
        # stage here would offer the operator only actions that must fail. The
        # gate stage routes to a viewer whose single action is "resume", which
        # is the whole remedy once the ids are renumbered. `_refuse_gated_story`
        # already pauses this way with a task that is not escalated: same
        # contract — the deferred-work ledger blocks the run, a human edits it,
        # the resume re-reads it.
        dupes = duplicate_ids(deferredwork.parse_ledger(text))
        if dupes:
            reason = (
                f"{ledger.name} carries duplicate DW ids: {', '.join(dupes)} — one id "
                "names more than one entry, so no migration of it can both preserve "
                "the entries and produce a valid ledger; renumber or merge them by "
                "hand and COMMIT the fix, then resume"
            )
            self.journal.append("migrate-duplicate-ids", story_key=MIGRATE_KEY, dw_ids=list(dupes))
            gates.notify(
                self.policy,
                self.run_dir,
                f"migration refused: {ledger.name}",
                f"{reason} — then `bmad-loop resume {self.state.run_id}`",
            )
            task.baseline_commit = None
            task.baseline_untracked = None
            self._save()
            raise RunPaused(reason, PAUSE_STORY_GATE, MIGRATE_KEY)

        if not task.baseline_commit:
            # `self.workspace.root`, which is `paths.repo_root` — the same anchor the
            # dev writer (`Engine._dev_phase`), the re-arm writer
            # (`runs.rearm_escalation`) and every proof-of-work probe in
            # `verify._verify_shared_gates` use. Under the `repo_root` override it is
            # NOT `paths.project`, and a baseline stamped in one tree and measured in
            # the other names a commit the measuring repo has never heard of (#716).
            task.baseline_commit = verify.rev_parse_head(self.workspace.root)
            task.baseline_untracked = sorted(verify.untracked_files(self.workspace.root))

        pre_canonical = snapshot_canonical(text)
        manifest = self._migration_manifest(text)
        manifest_path = self.run_dir / _MIGRATE_MANIFEST_RECORD
        confine_root = _project_of_run_dir(self.run_dir)
        # Bind the recovery records to the same authoritative input `_loop`
        # supplied.  This reread is deliberately immediately before the first
        # publication and outside the ledger lock: records and Git work must
        # never occur while that short file-I/O lock is held.
        if not self._migration_input_is_current(text):
            self._retire_migration_dispatch_authority(task, refund_attempt=False)
        try:
            atomic_write_text_confined(
                self.run_dir / _MIGRATE_BASELINE_RECORD,
                text,
                confine_root=confine_root,
            )
            task.migration_recovery_format = _MIGRATION_RECOVERY_FORMAT
            self._save()
            # A replacement attempt must not inherit validation/result authority
            # from the attempt it is replacing. Phase alone then makes a stale
            # rewrite impossible to consume after a TRIAGE_RUNNING crash.
            self._remove_migration_record(self.run_dir / _MIGRATE_REWRITE_RECORD)
            self._remove_migration_record(self.run_dir / _MIGRATE_RESULT_RECORD)
            atomic_write_text_confined(
                manifest_path,
                json.dumps(manifest, indent=2),
                confine_root=confine_root,
            )
        except OSError:
            # No child was dispatched, therefore no attempt-owned work exists.
            # The next resume must stamp the then-current (possibly repaired)
            # HEAD instead of retaining authority over the pre-repair tree.
            task.baseline_commit = None
            task.baseline_untracked = None
            self._save()
            raise

        # A writer may have landed after the first comparison or either durable
        # record write.  Refuse before claiming TRIAGE_RUNNING ownership.
        if not self._migration_input_is_current(text):
            self._retire_migration_dispatch_authority(task, refund_attempt=False)

        feedback: Path | None = None
        while True:
            task.attempt += 1
            advance(task, Phase.TRIAGE_RUNNING)
            self._save()
            launch_floor_ns = time.time_ns()
            result = self._run_session(
                task,
                role="triage",
                prompt=self._migrate_prompt(manifest_path, feedback),
                seq=task.attempt,
                session_stage="pre_migrate_session",
                prelaunch_validator=lambda: (
                    None
                    if self._migration_input_is_current(text)
                    else self._retire_migration_dispatch_authority(task, refund_attempt=True)
                ),
            )
            advance(task, Phase.TRIAGE_VERIFY)
            self._save()
            critical_reason = critical_session_reason("migration", result.result_json)
            if critical_reason is not None:
                self._escalate(task, critical_reason)
            # Split so ABSENCE survives: `new_text` stays `str` for
            # `validate_migration`, while `rewrite` keeps the difference between
            # "the session emptied the ledger" and "the session deleted it". On
            # an untracked ledger the restore below has no blob to anchor on and
            # this rejected rewrite — the exact text this attempt graded — is the
            # anchor instead, so flattening `None` to `""` here would make the
            # deleted-ledger case indistinguishable from a rival's empty write.
            # REPAIR/WRITE (DW-146), absence preserved: `None` is the restore
            # anchor's "the session deleted it", distinct from an empty write.
            rewrite = deferredwork.read_for_write(ledger)
            new_text = rewrite if rewrite is not None else ""
            diagnostic: dict[str, Any] | None = None
            if result.status != "completed":
                errors = [session_failure_reason("migration", result)]
                diagnostic = self._session_failure_diagnostic(
                    task,
                    launch_floor_ns,
                    lambda doc: validate_migration(doc, manifest, pre_canonical, new_text),
                )
            else:
                errors = validate_migration(result.result_json, manifest, pre_canonical, new_text)
            self.journal.append(
                "migrate-decision",
                attempt=task.attempt,
                session_status=result.status,
                ok=not errors,
                errors=errors,
                env_fault=result.env_fault,
                diagnostic=diagnostic,
            )
            if result.status != "completed" and result.env_fault:
                # The migration session's CLI lost its API connection (#194): it did
                # no rewrite work, so pause (the ESCALATED-resume above resets
                # task.attempt to 0 — fresh budget) instead of charging a migration
                # attempt. Escalate BEFORE the _safe_reset/attempt-cap path; that
                # resume also restores the ledger if the worktree is dirty.
                self._escalate(
                    task,
                    env_fault_pause_reason("migration", result) + _diagnostic_suffix(diagnostic),
                )
            if not errors:
                # This record is the durable proof that validation accepted the
                # exact ledger text. It precedes result publication so a refused
                # result write resumes by restoring this known rewrite and
                # redispatching, never by treating the task as complete.
                atomic_write_text_confined(
                    self.run_dir / _MIGRATE_REWRITE_RECORD,
                    new_text,
                    confine_root=confine_root,
                )
                atomic_write_text_confined(
                    self.run_dir / _MIGRATE_RESULT_RECORD,
                    json.dumps(result.result_json, indent=2),
                    confine_root=confine_root,
                )
                # Re-open the complete durable evidence set before granting the
                # COMMITTING boundary. The local values above were validated
                # before publication; only these run-owned records survive a
                # crash and therefore only they may authorize commit replay.
                durable_baseline, durable_manifest = self._migration_baseline_and_manifest(task)
                durable_rewrite = self._migration_record_text(
                    task, self.run_dir / _MIGRATE_REWRITE_RECORD, "rewrite"
                )
                assert durable_rewrite is not None
                self._migration_result_evidence(
                    task,
                    durable_baseline,
                    durable_manifest,
                    durable_rewrite,
                )
                advance(task, Phase.COMMITTING)
                self._save()
                self._finish_migration_commit(
                    task,
                    durable_baseline,
                    durable_manifest,
                    durable_rewrite,
                )
                return
            # never re-prompt over a half-broken rewrite; the baseline reset
            # covers tracked files, the explicit write covers an untracked
            # ledger that `git reset` cannot restore
            self._safe_reset(task)
            # The WRITE anchor derives from the committed blob, never from an
            # observation of the tree taken after the very reset it would attest
            # to: a rival writing a tracked ledger inside that window would BE
            # the observation, and this restore would overwrite it (#735). Probed
            # BEFORE the lock — it spawns git, and `ledger_lock` may cover file
            # I/O only, which no reset window can (#286). A ledger git does not
            # own has no blob to anchor on, and `reset --hard` cannot have
            # touched it either, so there the anchor is the rejected rewrite this
            # attempt actually graded — down to `None == None` when the session
            # deleted the ledger outright. No anchor at all withholds the write.
            anchor, committed = self._ledger_baseline_text(task)
            expected = committed if committed is not None else rewrite
            diverged = False
            with deferredwork.ledger_lock(ledger):
                # PURE TEXT ONLY under the hold — `ledger_lock` is not reentrant
                # and every mutator takes it.
                # REPAIR/WRITE (DW-146), absence preserved: this compare-and-set
                # authorizes the restore write below, and `None` is a real answer.
                current = deferredwork.read_for_write(ledger)
                if anchor is _LedgerAnchor.NO_RESET_CONTENT and current == text:
                    # ALREADY the text this restore exists to write, so it is
                    # done and there is nothing to escalate. Reachable without
                    # any rival: a session that atomic-SAVES the ledger replaces
                    # a tracked symlink with a regular file, `reset --hard` puts
                    # the link back, and the external target it cannot reach was
                    # never rewritten — so the ledger is correct while `rewrite`,
                    # read off the regular file, is not what is on disk. Demanding
                    # the anchor here would escalate a finished restore and spend
                    # the attempt budget on it.
                    #
                    # Scoped to NO_RESET_CONTENT deliberately. On a BASELINE
                    # anchor the reset republishes the committed text, so
                    # `current == text` is the ORDINARY post-reset state and
                    # accepting it there would retire the divergence check and
                    # the probe-fault escalation along with it. Only where the
                    # reset restored no text of its own is "already correct"
                    # information the anchor cannot supply.
                    pass
                # Either anchor will do below, unlike the engine's two restores:
                # this site supplies its own text for the no-reset-content case
                # (`rewrite`, which it graded), so `expected` is never the bare
                # `None` that would read a rival's deletion as the reset's work.
                elif anchor is not _LedgerAnchor.NONE and current == expected:
                    ledger.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(ledger, text)
                else:
                    diverged = True
            if diverged:
                # No merge and no silent skip. The comment above is the reason:
                # leaving the rejected rewrite standing IS re-prompting over a
                # half-broken ledger, and the migration input a human must fix is
                # no longer the one this attempt was graded against — the same
                # call `migrate-duplicate-ids` makes about a corrupt ledger.
                # A baseline probe that could not answer lands here too, and
                # deliberately: without an anchor there is no proof the text on
                # disk is the reset's own work rather than somebody's live write,
                # and an unprovable restore is exactly the overwrite this arm
                # exists to refuse. The escalation is the right recovery for both
                # — the resume above resets the attempt budget and re-reads the
                # ledger, which is what a rival-corrupted migration input needs.
                # Journaled outside the hold; `_escalate` raises.
                self.journal.append(
                    "sweep-migration-restore-diverged",
                    story_key=MIGRATE_KEY,
                    ledger=str(ledger),
                )
                self._escalate(
                    task,
                    "the ledger changed underneath the failed migration attempt — "
                    "re-run the sweep",
                )
            if task.attempt >= self.policy.sweep.max_migration_attempts:
                self._escalate(
                    task,
                    "migration failed deterministic validation: "
                    + "; ".join(errors)
                    + _diagnostic_suffix(diagnostic),
                )
            feedback = self._write_feedback(
                task,
                "The legacy-ledger migration failed deterministic validation:\n- "
                + "\n- ".join(errors),
            )

    def _session_failure_diagnostic(
        self,
        task: StoryTask,
        launch_floor_ns: int,
        validate: Callable[[dict[str, Any]], list[str]],
    ) -> dict[str, Any]:
        """Observe what a NON-completed triage/migration session left behind (#752):
        its ``result.json`` and the hook events of THIS attempt. Diagnosis only —
        a valid artifact here is never a completion (sessions complete on Stop or
        window death alone), and nothing in it changes the attempt's routing.

        ``artifact``: ``missing`` | ``malformed`` (``artifact_error`` names the parse
        or validation failure, bounded) | ``valid`` | ``unreadable: <reason>``.
        ``validate`` is the leg's own completed-path validator, handed the parsed
        document read-only.

        ``hook_events``: ``none`` | ``session-start-without-stop`` | ``stop`` |
        ``no-session-start-or-stop`` | ``unreadable: <reason>`` |
        ``n/a (<observation> observation)`` for a triage adapter that does not
        observe through hooks and so writes no events to count. Counted over both
        the out-of-tree channel and the legacy in-tree ``<run_dir>/events`` through
        ``signals.is_session_event`` — this attempt's session id and launch floor,
        so an earlier healthy attempt's events cannot mask this failure.

        Observation degrades, never raises: this runs on a failure path that must
        still reach its retry/escalation decision."""
        # The id `_run_session` just minted for this attempt: the sweep dispatches
        # both legs as role "triage" with no label.
        task_id = _session_task_id(task.story_key, "triage", task.attempt, task.generation)
        diagnostic: dict[str, Any] = {"task_id": task_id}
        # The adapter's own read-back location, which it clears at launch, so a
        # document there was written during this attempt.
        try:
            doc = load_result_document(self.run_dir / "tasks", task_id)
        except OSError as exc:
            diagnostic["artifact"] = f"unreadable: {_bounded(str(exc))}"
        except (ValueError, RecursionError) as exc:
            diagnostic["artifact"] = "malformed"
            diagnostic["artifact_error"] = _bounded(f"{type(exc).__name__}: {exc}")
        else:
            if doc is None:
                diagnostic["artifact"] = "missing"
            else:
                try:
                    errors = validate(doc)
                except Exception as exc:  # observation degrades; see docstring
                    diagnostic["artifact"] = (
                        f"unreadable: validator raised {_bounded(f'{type(exc).__name__}: {exc}')}"
                    )
                else:
                    if errors:
                        diagnostic["artifact"] = "malformed"
                        diagnostic["artifact_error"] = _bounded("; ".join(errors))
                    else:
                        diagnostic["artifact"] = "valid"
        # An adapter that does not observe through hooks (opencode-http's SSE)
        # never writes either event channel, so an empty scan is no evidence about
        # it; `none` would send its operator debugging a relay it does not use.
        observation = self.adapters["triage"].observation
        if observation != "hook-signal":
            diagnostic["hook_events"] = f"n/a ({observation or 'unknown'} observation)"
            return diagnostic
        try:
            events = session_events(
                events_dir_for(self.paths.project, self.run_dir.name),
                self.run_dir / "events",
                task_id,
                launch_floor_ns,
            )
        except Exception as exc:  # observation degrades; see docstring
            diagnostic["hook_events"] = f"unreadable: {_bounded(f'{type(exc).__name__}: {exc}')}"
        else:
            kinds = {event.event for event in events}
            diagnostic["hook_events"] = _hook_verdict(kinds)
            diagnostic["hook_event_kinds"] = sorted(kinds)
            diagnostic["hook_event_count"] = len(events)
        return diagnostic

    def _migrate_prompt(self, manifest: Path, feedback: Path | None) -> str:
        prompt = f"/bmad-loop-sweep --migrate {manifest}"
        if feedback is not None:
            prompt += f" --feedback {feedback}"
        return prompt

    # --------------------------------------------------------------- triage

    def _ensure_triage(self, open_now: set[str], cycle: int = 1) -> TriagePlan:
        suffix = "" if cycle == 1 else f"-{cycle}"
        triage_path = self.run_dir / f"triage{suffix}.json"
        triage_key = TRIAGE_KEY + suffix
        selector_cache_mismatch = False
        # `stat()` rather than `is_file()` (DW-224). The convenience method splits
        # by RUNTIME on an OS fault: Python 3.11-3.13 re-raise anything that is not
        # a "this cannot be a file" errno — a `PermissionError` on a path component
        # included — out of a bookkeeping read that runs before a single bundle,
        # while 3.14 swallows it and answers False. The floor is 3.11 and CI runs
        # every version in between, so the explicit call is what makes this degrade
        # UNIFORM instead of version-dependent, and it is the shape
        # `_publish_stranded_close` already proved. Absence — and a path component
        # that is not a directory — stays SILENT and falls through to a fresh triage
        # session, exactly as the old guard's False did; only a real metadata fault
        # earns the row, and it earns the SAME row a failed cache read does.
        try:
            cache_mode = triage_path.stat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            cache_mode = None
        except OSError as exc:
            self.journal.append("sweep-triage-reload-failed", errors=[f"unreadable: {exc}"])
            cache_mode = None
        if cache_mode is not None and stat.S_ISREG(cache_mode):
            # already validated this run; the ledger has moved since (closes,
            # decisions), so skip the open-set equality re-check. A cache we
            # cannot read or that is not a JSON object degrades to a fresh
            # triage — a truncated file must not crash the whole run.
            try:
                cached = _read_json(triage_path)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                self.journal.append("sweep-triage-reload-failed", errors=[f"unreadable: {exc}"])
                # DW-263: INVALIDATE before re-triaging. This is the one arm where
                # a VALID plan can be sitting behind a transient fault: the fresh
                # triage's write-back below is best-effort, so a refused overwrite
                # would leave these older bytes in place for the next resume to
                # replay as if they were this cycle's plan — off a ledger that has
                # moved since. Not the metadata-fault arm (it cannot reliably unlink
                # what it cannot stat) and not the validation-failed arm below
                # (those bytes cannot be replayed as a plan at all). A refused
                # unlink degrades the same way the read did: row, then fresh triage.
                try:
                    triage_path.unlink(missing_ok=True)
                except OSError as unlink_exc:
                    self.journal.append(
                        "sweep-triage-cache-unlink-failed",
                        errors=[f"unremovable: {unlink_exc}"],
                    )
                else:
                    self.journal.append("sweep-triage-cache-invalidated")
            else:
                if isinstance(cached, dict):
                    plan, errors = validate_triage(cached, None)
                else:
                    plan, errors = None, [f"not a JSON object: {type(cached).__name__}"]
                if (
                    plan is not None
                    and (self.only_ids is not None or self.min_severity is not None)
                    and plan.open_ids != frozenset(open_now)
                ):
                    selector_cache_mismatch = True
                    plan, errors = None, [
                        "cached selected open_ids no longer match the current selector universe"
                    ]
                if plan is not None:
                    return plan
                self.journal.append("sweep-triage-reload-failed", errors=errors)

        task = self.state.tasks.get(triage_key)
        if task is None:
            task = StoryTask(story_key=triage_key, epic=0)
            self.state.tasks[triage_key] = task
        elif task.phase != Phase.PENDING:
            # resumed mid-triage or retrying after an escalation: restart
            self.journal.append("resume-restart", story_key=triage_key, phase=str(task.phase))
            if selector_cache_mismatch:
                task.attempt = 0
                _rearm_generation(task)
            elif task.phase == Phase.ESCALATED:
                task.attempt = 0  # the human resumed deliberately; fresh budget
                _rearm_generation(task)  # ...and into a fresh session-id namespace
            task.phase = Phase.PENDING  # deliberate reset, not a normal transition

        feedback: Path | None = None
        while True:
            task.attempt += 1
            advance(task, Phase.TRIAGE_RUNNING)
            self._save()
            launch_floor_ns = time.time_ns()
            result = self._run_session(
                task,
                role="triage",
                prompt=self._triage_prompt(feedback, open_now),
                seq=task.attempt,
            )
            advance(task, Phase.TRIAGE_VERIFY)
            self._save()
            critical_reason = critical_session_reason("triage", result.result_json)
            if critical_reason is not None:
                self._escalate(task, critical_reason)
            diagnostic: dict[str, Any] | None = None
            if result.status != "completed":
                plan, errors = None, [session_failure_reason("triage", result)]
                diagnostic = self._session_failure_diagnostic(
                    task, launch_floor_ns, lambda doc: _triage_verdict(doc, open_now)
                )
            else:
                repairs = _normalize_bundle_names(result.result_json)
                plan, errors = validate_triage(result.result_json, open_now)
                for repair in repairs:
                    self.journal.append(
                        "sweep-bundle-name-normalized",
                        field=repair.field,
                        original=repair.original,
                        normalized=repair.normalized,
                    )
            self.journal.append(
                "triage-decision",
                attempt=task.attempt,
                session_status=result.status,
                ok=plan is not None,
                errors=errors,
                env_fault=result.env_fault,
                diagnostic=diagnostic,
            )
            if result.status != "completed" and result.env_fault:
                # transport/API failure (#194): pause rather than charge a triage
                # attempt for a session that never reached the API. The
                # ESCALATED-resume above resets task.attempt to 0 (fresh budget).
                self._escalate(
                    task,
                    env_fault_pause_reason("triage", result) + _diagnostic_suffix(diagnostic),
                )
            if plan is not None:
                advance(task, Phase.DONE)
                self._save()
                # Cache write failure must not discard this validated plan (DW-247).
                # Use a WRITE kind: `sweep-triage-reload-failed` means an existing
                # cache could not be read, not that a healthy triage could not save.
                # With no usable cache, resume re-triages this cycle;
                # `_publish_stranded_close` cannot recover its close at the no-open
                # exit, and `bmad-loop decisions` cannot reconstruct its decisions.
                # Written ATOMICALLY (DW-263): a short or refused write leaves either
                # no cache or a complete one, never torn JSON — and the older bytes
                # a transient read fault left behind were already unlinked in the
                # reload arm above, so a refused write-back cannot leave a stale
                # plan for the next resume to replay, except where that unlink was
                # itself refused (`sweep-triage-cache-unlink-failed`) or the cache
                # faulted at the metadata probe, whose arm does not unlink: there the
                # older plan survives a refused write-back. A directory also reaches this
                # catch: `os.replace` onto it raises `IsADirectoryError` on POSIX or
                # `PermissionError` on Windows. Confined to the project root (#593,
                # DW-269): the plain writer resolved `.bmad-loop/`, `runs/` and the
                # run dir by name, so a link planted at any of them aimed the temp
                # and the published cache out of the project. The root is the
                # PROJECT that owns the run dir (`_decisions_phase` says why not
                # `self.workspace.root`), not `self.run_dir` — a file confined
                # against its own parent walks no components and refuses nothing.
                # A refused parent reaches this catch as `UnconfinedWriteError`,
                # itself an `OSError`, so it costs the cycle its cache and nothing
                # else. `OSError` alone: unlike `atomic_write_text`, the confined
                # helper never `resolve()`s, so DW-247's `RuntimeError` arm has
                # nothing to catch here.
                try:
                    atomic_write_text_confined(
                        triage_path,
                        json.dumps(result.result_json, indent=2),
                        confine_root=_project_of_run_dir(self.run_dir),
                    )
                except OSError as exc:
                    self.journal.append(
                        "sweep-triage-cache-write-failed", errors=[f"unwritable: {exc}"]
                    )
                self.journal.append(
                    "sweep-triage-result",
                    bundles=len(plan.bundles),
                    decisions=len(plan.decisions),
                    already_resolved=len(plan.already_resolved),
                    blocked=len(plan.blocked),
                    skip=len(plan.skip),
                )
                self._emit("post_triage", task)
                return plan
            if task.attempt >= self.policy.sweep.max_triage_attempts:
                self._escalate(
                    task,
                    "triage output failed validation: "
                    + "; ".join(errors)
                    + _diagnostic_suffix(diagnostic),
                )
            feedback = self._write_feedback(
                task,
                "The triage result.json failed deterministic validation:\n- " + "\n- ".join(errors),
            )

    def _extra_session_env(
        self, task: StoryTask, role: str, label: str | None = None
    ) -> dict[str, str]:
        # Triage and migration sessions read the ledger the orchestrator resolved,
        # not one they re-derive: `load_paths` layers the central TOML over the
        # legacy YAML, and a skill reading the YAML alone would triage a stale
        # ledger — or find none on a TOML-only install (#769). Bundle sessions
        # never read the ledger, so they stay byte-identical.
        if role != "triage" or label is not None:
            return {}
        return {"BMAD_LOOP_LEDGER": str(self.workspace.paths.deferred_work)}

    def _triage_prompt(self, feedback: Path | None, open_now: set[str] | None = None) -> str:
        prompt = "/bmad-loop-sweep"
        if open_now is not None and (self.only_ids is not None or self.min_severity is not None):
            ordered = (
                [dw_id for dw_id in self.only_ids if dw_id in open_now]
                if self.only_ids is not None
                else sorted(
                    open_now,
                    key=lambda value: (
                        len(value.removeprefix("DW-").lstrip("0") or "0"),
                        value.removeprefix("DW-").lstrip("0") or "0",
                        value,
                    ),
                )
            )
            prompt += " --only " + ",".join(ordered)
        if feedback is not None:
            prompt += f" --feedback {feedback}"
        return prompt

    # ------------------------------------------------------ ledger phases

    def _close_resolved(self, plan: TriagePlan) -> int:
        self._emit("pre_close_resolved")
        ledger = self.workspace.paths.deferred_work
        # DW-216: cycle-scoped, so a cycle whose close phase reads and writes fine
        # cannot inherit the previous cycle's fault. This method runs once per
        # cycle and FIRST, which is what makes the top of it the reset point — the
        # same discipline `_decisions_phase` gets from publishing with `=`.
        self._close_ledger_in_doubt = False
        # ONE locked read->edit->write for the whole batch (#286/#469). The
        # per-entry `mark_done` loop this replaces took the cross-process ledger
        # lock once per id, leaving a rival writer — a live run's harvest, the TUI
        # decision modal, `sweep --archive` — a window between every pair of
        # closures, so half this phase's closures could be lost while the other
        # half landed and the journal claimed all of them. `notes=` carries the
        # per-entry evidence the loop passed positionally, so the resulting ledger
        # text and the returned ids (order preserved, skips dropped) are unchanged.
        ids = [entry.id for entry in plan.already_resolved]
        # The write DEGRADES rather than propagating (DW-166). `mark_done_many`'s
        # locked `read_for_write` raises `LedgerReadError` on undecodable bytes,
        # and the lock itself can fail on `OSError` or `StateRootError`; bare, any
        # of them ended the whole sweep as crashed out of a bookkeeping phase that
        # runs before a single bundle. Entries staying `open` is the conservative
        # outcome — the next cycle re-triages them and closes them then — where a
        # crash loses the cycle. `ValueError` is the writers' `date` precondition,
        # unreachable from `_today()` and named for the same reason the DW-146
        # sibling handlers name it. `LedgerReadError` is a plain `Exception` and
        # `StateRootError` is not an `OSError`, so both must be spelled out.
        #
        # What does NOT degrade is the publish itself. `mark_done_many` ends in an
        # atomic write, and its `ENOSPC`/`EROFS`/failed-rename `OSError` reached
        # this tuple looking exactly like the lock's — the fault DW-166 never
        # named, swallowed along with the ones it did. `deferredwork._publish`
        # retypes it as `LedgerWriteError` (an `OSError` subclass, so the CLI and
        # TUI degrade arms are untouched) and this site re-raises it first: a
        # repair write that failed is not a phase that closed nothing, it is a
        # sweep that cannot keep its books, and the rule is AGENTS.md's —
        # observation may degrade, repair writes must raise.
        #
        # The commit below is gated on `closed`, THIS invocation's write — and a
        # process that dies between the publish and that commit replays with the
        # ids already `done`, so `closed` comes back empty and the guard skips the
        # commit of bytes the journal already claims. The debt is persisted ahead
        # of the write instead (`_owe_ledger_commit`), and `_loop` settles it at
        # the top of the resume. Only when there is something to write: an empty
        # plan spawns no git and owes nothing (DW-183/DW-185). And every outcome
        # below that definitively published nothing RETRACTS it, so a false debt
        # never outlives this phase to be settled against an operator's edit.
        owed_here = bool(ids) and self._owe_ledger_commit()
        try:
            closed = deferredwork.mark_done_many(
                ledger,
                ids,
                self._today(),
                "already resolved",
                notes=[f"already resolved: {entry.evidence}" for entry in plan.already_resolved],
            )
            # Computed INSIDE this `try` (DW-193) so the probe's read faults take
            # the degrade arm below rather than minting a second row of their own.
            # It runs only when this pass flipped nothing, so a fault here is a
            # pass that wrote no bytes and the degrade arm's retract is right.
            pending = not closed and self._resolved_write_pending(ledger, ids)
        except deferredwork.LedgerWriteError:
            # the atomic write failed and the original is untouched: nothing to
            # settle, so the debt is retracted on the way out
            self._retract_ledger_commit(owed_here)
            raise
        except deferredwork.LedgerLockReleaseError:
            # ...and its sibling: the publish LANDED and the lock's release then
            # faulted. Degrading that reads a close that happened as one that did
            # not, and skips the commit of bytes already on disk. The debt STAYS:
            # the bytes are on disk and uncommitted, which is what it is for.
            raise
        except (deferredwork.LedgerReadError, OSError, ValueError, StateRootError) as e:
            self._retract_ledger_commit(owed_here)  # every arm here is pre-write
            self.journal.append("sweep-resolved-close-unavailable", dw_ids=ids, error=str(e))
            # ...and the doubt is LATCHED (DW-216). This batch either could not read
            # the ledger or could not write it, so this cycle cannot claim the bytes
            # on disk are fit to publish — and the bundles `_cycle` dispatches after
            # the decision phase commit through `verify.commit_story` /
            # `verify.finalize_commit`, both of which open with a whole-tree
            # `git add -A`. Bare, a `mark_done_many` that half-wrote a decodable
            # ledger reached HEAD under a bundle's commit message, and an
            # undecodable one crashed `_write_intent`'s bare `read_for_write`, while
            # `_cycle`'s gate read a latch nothing here had armed. Its OWN latch and
            # not `_ledger_in_doubt`: `_decisions_phase` runs after this and
            # publishes its verdict with `=`, so writing the shared flag here would
            # be erased by a healthy decision phase — see the declaration in
            # `__init__`. `_prune_pre_answers` reads it too and refuses its
            # KEEP-list derivation, so a decodable half-write cannot prune a live
            # pre-answer either.
            self._close_ledger_in_doubt = True
            # ...and MIRRORED onto run state (DW-218/219), because the latch above
            # is instance-only: a crash between here and `_cycle`'s gate resumes
            # with it cleared. EVERY arming site persists, not just the decision
            # phase's tail publish — a close-phase-only fault leaves that phase's
            # local `ledger_in_doubt` False, so the publish would persist nothing.
            self._record_ledger_doubt()
            # `post_close_resolved` still fires and 0 is still returned: the phase
            # RAN, it just closed nothing, and a plugin watching the phase boundary
            # must not silently lose its pairing with `pre_close_resolved`.
            self._emit("post_close_resolved")
            return 0
        if closed:
            self.journal.append("sweep-resolved-closed", dw_ids=closed)
            # ...and the commit rides a guard on a LANDED WRITE — this pass's here,
            # or one already on disk on the `pending` arm below (DW-193). What it is
            # NOT is a guard on the ledger being dirty. The boundary that holds is
            # the PATHSPEC, so it is about dirt OUTSIDE the published file: an
            # operator's in-flight edits elsewhere in the enclosing repository stay
            # with their owner (DW-183/DW-185). Dirt INSIDE the ledger is a different
            # story and the pending arm widens it — `_commit_ledger` publishes the
            # whole file, so an out-of-band edit to the ledger (an operator's note, a
            # rival writer's half-landed change) rides into the `chore(sweep):`
            # commit beside the close it was asked to publish. The `closed` arm has
            # always had that property; what is new is that a pass which wrote
            # NOTHING can now trigger it. Accepted deliberately: the alternative is
            # a per-hunk publish this bookkeeping does not have, and the entry the
            # arm exists for — a durable close stranded off HEAD — is the worse loss.
            # The emptiness short-circuit inside `_resolved_write_pending` is what
            # keeps a phase that resolved nothing away from git ENTIRELY: with no
            # ids named there is no write to publish under either arm.
            #
            # The guard inventory across all nine `_commit_ledger` sites, since
            # it is not uniform and reading it as uniform is the trap:
            #   * SIX gate on a write result or a normally returned effect:
            #     both prunes (`dropped`, and `drop_pre_answer` answering True),
            #     this site's TWO arms (`closed`, and `pending` — the same landed
            #     write, read back off disk after a crash lost only its commit),
            #     `_decisions_phase` (`any_effect_landed`), and
            #     `_publish_stranded_close` — `_loop`'s empty-open-set recovery
            #     arm, which reads the SAME per-id reader as `pending`
            #     (`_done_on_disk`) over the CACHED triage plan and so gates on
            #     the same landed write. Two VIEWS of that one reader, combined
            #     with `or` (DW-222/DW-249): `all` over the plan's
            #     `already_resolved` ids, as `pending` does, and `any` over its
            #     `decisions` ids — the decision phase strands a close the same
            #     way this one does, since `record_decision` flips the entry to
            #     `done` on disk and `_decisions_phase` publishes only at its
            #     tail, one effect at a time. Two calls and not one merged `all`
            #     list: a union would AND the terms and narrow the arm. Its
            #     extra term is the CACHE: `triage{suffix}.json` for the
            #     current cycle must already be on disk, which is true only on a
            #     resume, so a fresh sweep over a ledger with nothing open still
            #     reaches no git at all.
            #   * THREE gate on something that does NOT prove a write: `_loop`'s
            #     post-recovery publisher counts recovered tasks (which may defer
            #     without editing the ledger), `_loop`'s cycle-boundary publisher
            #     gates on reaching the end of a cycle at all — since DW-223 it
            #     sits ABOVE the `no-progress` and `max-cycles` returns, so all
            #     THREE cycle exits share the one call and it no longer reads
            #     `progressed` (which a dropped answer could set without touching
            #     the ledger anyway) — and `_ensure_migration` gates on
            #     `if not errors:`, a verdict on the rewrite session rather than on
            #     bytes changing. The boundary publisher stays BELOW `_loop`'s
            #     ledger-fault / unfit-to-publish stops, which still return without
            #     publishing.
            # The RUN'S DOUBT (`_ledger_unfit_to_publish()`, the one reader of the
            # two cycle latches and the persisted DW-218/219 mirror) is a SECOND,
            # non-uniform gate laid over the nine, and which sites read it is the
            # other trap to read as uniform:
            #   * FOUR read it at the call site and journal
            #     `sweep-ledger-commit-withheld` instead of publishing: this site's
            #     TWO arms and `_loop`'s post-recovery publisher, the debt settle
            #     included (DW-246 — all three
            #     run on a resume AHEAD of `_cycle`'s dispatch gate, so a run that
            #     KNEW its ledger was unfit published the whole file, half-write
            #     included, before the gate was ever consulted), and
            #     `_publish_stranded_close` (DW-250 — the gate sits above BOTH of
            #     its probes, and its row alone carries `dw_ids`, the union of the
            #     cached plan's already-resolved and decision ids it declined to
            #     prove; before DW-250 only its decision term read the verdict, so
            #     a doubted resume still published on an already-resolved id).
            #   * `_decisions_phase`'s tail publish already gated on the same
            #     reader, silently: its withhold is part of the walk's own verdict
            #     and is reported by `sweep-bundles-withheld` and the repeat stop,
            #     so it writes no row of its own.
            #   * `_loop`'s boundary publisher needs no gate of its own: it sits
            #     BELOW the unfit-to-publish stop, which returns first.
            #   * `_loop` reads it TWICE more, ahead of publishers rather than at
            #     them: the in-flight recovery pass is withheld whole (a re-armed
            #     bundle's own `commit_story` is a whole-tree `git add -A`;
            #     `sweep-bundles-withheld`), and `_ensure_migration` is refused on
            #     the doubt's own `ledger-unreadable` stop before any rewrite
            #     session is spent — so its publisher never reads it itself.
            #   * Both store prunes are about a different file and never read it.
            # Reading is one direction; ARMING is the other, and since DW-244 it
            # IS uniform across the ledger family: every LEDGER-family site — the
            # migration publisher and `_publish_stranded_close` included, whether
            # or not it reads the doubt — arms the mirror on a refusal inside
            # `_commit_ledger`, while the store prunes neither read nor arm it.
            # A refusal at the boundary publisher, which sits BELOW the unfit
            # stop, is therefore honoured one cycle LATER: cycle N+1 withholds its
            # bundles and ends on the unfit stop, unless one of its own effects
            # lands and releases the arm.
            # A withheld publish is NOT a refusal: no target was probed and no git
            # was spawned, so it carries `reason="ledger-in-doubt"` (DW-217's token
            # for a ledger that READS but is unfit) rather than a fifth
            # `refuse_cause`.
            # Beneath all nine are TWO uniform floors, in this order:
            #   * the TARGET VALIDATION (DW-199/203/205), which asks whether the
            #     declared `family`'s file is still there and still readable before
            #     any git runs. It is what the per-site guards above cannot cover:
            #     each of them grades the phase's own WRITE, and the ledger can
            #     vanish or go undecodable AFTER that write — at which point
            #     `commit_paths` would have staged the absence as a DELETION.
            #     Every site declares its family; none derives one.
            #   * `_commit_ledger`'s `path_clean`, which makes any of them a no-op
            #     when the published file already matches HEAD.
            # The per-site guards are the early-outs that keep a phase which wrote
            # nothing from reaching git at all. What the two write-result guards
            # cannot see is a publish a PREVIOUS invocation landed and never
            # committed — a replay closes nothing — so those two sites persist
            # the debt ahead of the write (`_owe_ledger_commit`) and `_loop`
            # settles it at the top of a resume.
            #
            # ...and BOTH arms sit behind the run's ledger-doubt verdict, the
            # inherited mirror in particular (DW-218/219): this phase runs at the
            # top of every cycle, ahead of the dispatch gate that reads the same
            # verdict, and its commit is of the FILE — so a resume that inherited
            # a doubt over a half-landed decision effect would walk that flip into
            # HEAD here, under this message, before `_cycle` withholds a single
            # bundle over the same bytes. A write that lands THIS pass is not a
            # release either: `_release_ledger_doubt` refuses an inherited arm on
            # exactly that proof. Withheld, the debt latched above stays for the
            # bytes on disk, the `sweep-resolved-closed` row still says what was
            # written, and the run ends where the doubt ends it. Same rule the
            # decision phase's tail publish and `_publish_stranded_close` apply.
            #
            # the ledger file: `mark_done_many` above wrote the ledger
            # ...withheld on the run's doubt (DW-246): on a resume this arm runs
            # ahead of `_cycle`'s dispatch gate, so it must read the same verdict
            # that gate reads. The close itself LANDED and its row above stands;
            # only the publish is withheld. It reaches HEAD through the human's
            # own commit (the notice's clean-worktree precondition) or through a
            # publisher in the fresh `bmad-loop sweep` that follows the repair —
            # never through this run: an inherited mirror is not released
            # in-process, and a repeating run with the doubt armed returns at the
            # unfit stop BEFORE the boundary publisher. Gate AT the call site, not inside
            # `_commit_ledger`: see the inventory above for the sites that stay
            # ungated on purpose.
            if self._ledger_unfit_to_publish():
                self._withhold_ledger_publish("chore(sweep): close resolved deferred-work entries")
            else:
                self._commit_ledger(
                    "chore(sweep): close resolved deferred-work entries",
                    path=self.workspace.paths.deferred_work,
                    family="ledger",
                )
        elif pending:
            # These ids read `done` on disk while this pass flipped none of them —
            # the shape a crash between a previous pass's write and its commit
            # leaves behind, and the one this arm exists for (DW-193). It is not the
            # only writer that can produce it; see `_resolved_write_pending` for what
            # the probe actually proves. No `sweep-resolved-closed` row: this pass flipped nothing
            # and must not claim otherwise — and `len(closed)` stays 0, so the
            # cycle's progress predicate is unchanged. Only the commit is missing,
            # and the diff being published IS the close of resolved entries, so the
            # message above describes it exactly; a distinct one would fork the two
            # publishers for no reader's benefit. `_commit_ledger`'s `path_clean`
            # makes this a no-op when the write already reached HEAD. The debt
            # this pass latched (`owed_here`) is NOT retracted here: the probe just
            # proved a durable close on disk, so a commit that degrades leaves a
            # real debt for the next resume's settle, exactly as the `closed` arm's
            # would.
            # ...and withheld on the run's doubt (DW-246), for the same reason as
            # the `closed` arm and with more at stake: this arm is REACHED ONLY on
            # a resume, which is exactly when the persisted mirror is the verdict
            # in force, and the stranded `done` it would publish sits in the same
            # file as whatever half-write armed that doubt. Kept as its own
            # `if`/`else` rather than folded with the arm above so each arm's gate
            # can be ablated on its own.
            if self._ledger_unfit_to_publish():
                self._withhold_ledger_publish("chore(sweep): close resolved deferred-work entries")
            else:
                self._commit_ledger(
                    "chore(sweep): close resolved deferred-work entries",
                    path=self.workspace.paths.deferred_work,
                    family="ledger",
                )
        else:
            # zero ids flipped is zero bytes written: nothing to settle
            self._retract_ledger_commit(owed_here)
        self._emit("post_close_resolved")
        return len(closed)

    def _done_on_disk(self, ledger: Path, ids: list[str]) -> frozenset[str] | None:
        """Which of `ids` read `done` in the ledger ON DISK, in ONE read (DW-193,
        split out under DW-249) — the shared reader behind `_resolved_write_pending`
        (the `all` view) and `_any_write_pending` (the `any` view). `None` means the
        ledger is ABSENT (`read_for_write`'s own answer); an empty `ids` answers an
        empty set WITHOUT reading, so a caller naming nothing takes no read at all.

        WHAT IT PROVES, exactly: that these ids read `done` NOW. Not that a previous
        pass of the caller's phase is what wrote them, and not that the bytes are
        still those of that write. This read takes NO cross-process ledger lock,
        where `mark_done_many` above took one for its read-edit-write, so the same
        rival writers that lock names — a live run's harvest, the TUI decision
        modal, `sweep --archive` — can have closed these entries instead, or can
        edit the file between this read and the publish. That is tolerable because
        of what the answer is USED for: a commit of the ledger file, which is the
        right outcome for a durable close whoever wrote it, and which
        `_commit_ledger` re-validates and no-ops on its own. It would NOT be
        tolerable for a claim about this run's work, which is exactly why
        `_close_resolved` writes no `sweep-resolved-closed` row on its `pending`
        arm and still returns `len(closed)`.

        POSITIVE PROOF, per id. `[]` back from a non-empty `ids` is ambiguous:
        `mark_done_many` skips both already-done ids AND ids the ledger holds no
        entry for. So an id counts here only when it parses to :attr:`DWEntry.done`
        — an id the ledger does not carry, or one carrying a status the format does
        not understand, is simply not in the answer, proves nothing and must not
        authorize a commit under either view. `.done`, never `not .open`, for the
        reason that property documents.

        `read_for_write`, not `read_for_observation`: this gates a PUBLISH, so its
        `None`/`LedgerReadError`/`OSError` answers belong in the caller's existing
        degrade arm rather than collapsing to an empty text that would read as "no
        entry is done" — the same answer a healthy ledger of open entries gives.
        Whether the file is actually dirty stays `_commit_ledger`'s `path_clean`
        question; this method spawns no git and asks no second one.
        """
        if not ids:
            return frozenset()
        text = deferredwork.read_for_write(ledger)
        if text is None:
            return None
        # LAST-wins on a duplicate id, where the writer's `_apply_done` locates the
        # FIRST. The two can disagree only on a ledger carrying duplicate ids, which
        # is a corrupt shape `duplicate_ids` refuses elsewhere; not handled here.
        entries = {entry.id: entry for entry in deferredwork.parse_ledger(text)}
        return frozenset(dw_id for dw_id in ids if dw_id in entries and entries[dw_id].done)

    def _resolved_write_pending(self, ledger: Path, ids: list[str]) -> bool:
        """Whether EVERY id in `ids` reads `done` in the ledger on disk (DW-193) —
        the state a crash between `mark_done_many`'s write and its commit leaves
        behind, which the resume's cached triage plan replays as an empty
        `mark_done_many` return. The `all` view over `_done_on_disk`, whose
        docstring holds everything about what one read does and does not prove;
        `_close_resolved`'s `pending` arm and `_publish_stranded_close`'s
        already-resolved term read through here.

        The empty-`ids` arm is CORRECTNESS, not merely an ordering nicety: `all()`
        over an empty sequence is vacuously True, so deleting it makes a phase that
        resolved nothing publish — the exact regression DW-185 closed. It also
        keeps that phase from taking this read at all, which is the property
        `test_a_phase_that_wrote_nothing_spawns_no_git` grades at the git seam. An
        absent ledger (`None` from the reader) proves nothing either.
        """
        if not ids:
            return False
        done = self._done_on_disk(ledger, ids)
        return done is not None and all(dw_id in done for dw_id in ids)

    def _any_write_pending(self, ledger: Path, ids: list[str]) -> bool:
        """Whether ANY id in `ids` reads `done` in the ledger on disk (DW-249) —
        the `any` view over the same one read, for `_publish_stranded_close`'s
        decision term. A decision-phase close is stranded per DECISION: each
        `_apply_decision_effect` flips its own entry, so one landed flip is a write
        the exit must publish whether or not the plan's other decision ids are
        still in the ledger, where the `all` view let one absent id veto its
        sibling's publish. Same DW-185 properties as the `all` view: `[]` answers
        False without reading, an absent ledger answers False, and an id the
        ledger does not carry or cannot parse contributes nothing — it vetoes
        ITSELF, no longer the others.
        """
        return bool(self._done_on_disk(ledger, ids))

    # `dict[str, Any]` per answer, not `dict[str, str]`: `unusable_answer_reason`
    # deliberately screens only the fields a reader consumes, so `resolution` and
    # `answered_at` can legitimately hold non-strings and a keep-open answer may
    # carry a corrupt `intent`. The narrower annotation read as a guarantee that
    # would justify deleting `_answer_str`; it never was one.
    #
    # THIRD return element (DW-200): the ids whose `build` answer was recorded while
    # `record_decision` reported writing no `decision:` line. RETURNED rather than
    # latched on `self`, so `_cycle` cannot hand `_materialize_bundles` a stale or
    # forgotten set — the same reason that argument is required there.
    def _decisions_phase(
        self, plan: TriagePlan
    ) -> tuple[dict[str, dict[str, Any]], int, frozenset[str]]:
        from . import decisions as decisions_store  # lazy: decisions imports sweep

        decisions_path = self.run_dir / "decisions.json"
        # The project that OWNS `run_dir`, not `self.workspace.root`: under the
        # supported `repo_root` override (isolation = "none") the workspace root
        # is the separate code repo while the run dir — and the project-level
        # pre-answer store — stay under the PROJECT, so a workspace-rooted
        # confinement refused every write here and a workspace-rooted read
        # silently ignored the store. Derived from the run dir's own shape, which
        # no workspace swap moves.
        project_root = _project_of_run_dir(self.run_dir)
        # The orchestrator writes this store itself, but a crash mid-write, a hand
        # edit or an out-of-band writer can still leave it unreadable or wrongly
        # shaped — and every consumer below calls `.get(...)` on its values, so the
        # bare read let one malformed byte abort the whole sweep. Degrade exactly
        # the way `_ensure_triage`'s cache reload does (journal it, carry on with
        # what is usable): a decision left with no usable answer simply goes back
        # down the pending/skip path, which is where it was before anyone answered
        # it. Per-VALUE, not all-or-nothing, so one bad entry does not cost the
        # well-shaped rest their answers.
        #
        # `unusable` keeps the PER-VALUE drops so the two write-backs below
        # re-publish their parsed values unchanged: on that arm the degrade really is
        # in-memory and this method neither repairs nor trims the file. The
        # WHOLE-FILE arms cannot offer that — nothing per-value is left to carry —
        # and they split two ways by what the fault says about the bytes on disk:
        #
        # - A DECODE fault (`JSONDecodeError`, `UnicodeDecodeError`) or a non-object
        #   top level means the bytes themselves are corrupt, so `unusable` stays
        #   empty and the next write this phase makes for its own reasons (a
        #   seeded pre-answer, an in-run answer) replaces the corrupt file
        #   wholesale — that replacement IS the repair.
        # - An `OSError` at the metadata probe or the content read says nothing
        #   about the bytes: a store full of valid answers merely could not be read
        #   THIS cycle. Replacing it from an `answers` that started empty would
        #   turn a transient refusal into permanent loss of every answer it held
        #   (DW-264), so `store_unreadable` is set on those two arms alone and
        #   the SEEDED write-back below is withheld while it is set — the adopted
        #   answers stay in memory for this cycle's bundling and the row
        #   `sweep-decisions-store-write-withheld` names the ids that did not
        #   persist (a resume re-adopts them from the project store). The
        #   INTERACTIVE arm has no second copy, so there the PROMPT is withheld
        #   instead (`sweep-decisions-prompt-withheld`, below): an answer taken
        #   at a prompt this cycle would live in memory alone, and a crash before
        #   the bundle it authorizes is materialized would lose it — nothing reads
        #   a `build` back off the ledger's `decision:` line (#794 review). The
        #   withheld check precedes the write at the seeded site, so a withheld
        #   write is never also reported as a failed one.
        answers: dict[str, dict[str, Any]] = {}
        unusable: dict[str, Any] = {}
        malformed: list[str] = []
        store_unreadable = False
        store_fault = ""  # the refusal's text, for the prompt-withheld notice below
        # `stat()` + `S_ISREG`, not `is_file()` (DW-248, the DW-224 shape). The
        # convenience probe splits by RUNTIME on a metadata fault: 3.11-3.13
        # re-raise a `PermissionError` out of this bookkeeping read — aborting the
        # sweep before its saved answers can be consumed — while 3.14
        # swallows it and answers False. The explicit call makes the degrade the
        # comment above promises UNIFORM: absence and a non-directory path
        # component stay SILENT, as `is_file()`'s False was, and only a real
        # metadata fault earns the row — the same `sweep-decisions-reload-failed`
        # the content faults below write, since either way the store on disk
        # could not be used.
        try:
            store_mode = decisions_path.stat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            store_mode = None
        except OSError as exc:
            self.journal.append("sweep-decisions-reload-failed", errors=[f"unreadable: {exc}"])
            store_mode = None
            store_unreadable = True  # DW-264: the bytes may be fine; withhold the writes
            store_fault = str(exc)
        if store_mode is not None and stat.S_ISREG(store_mode):
            try:
                stored = _read_json(decisions_path)
            # Two arms, one row: the classes are disjoint (`JSONDecodeError` and
            # `UnicodeDecodeError` are both `ValueError`s), and the split exists
            # because only the I/O refusal earns the withhold flag — the decode
            # arm keeps wholesale replacement as its repair (comment block above).
            except OSError as exc:
                self.journal.append("sweep-decisions-reload-failed", errors=[f"unreadable: {exc}"])
                store_unreadable = True
                store_fault = str(exc)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self.journal.append("sweep-decisions-reload-failed", errors=[f"unreadable: {exc}"])
            else:
                if isinstance(stored, dict):
                    for stored_id, value in stored.items():
                        key = str(stored_id)
                        # `unusable_answer_reason`, not a local `isinstance`: the
                        # SAME schema `decisions.pending_missed_decisions` screens
                        # by (DW-142), so an id this loop refuses to answer is one
                        # that command re-offers instead of counting answered.
                        # The predicate is shared but store-PARAMETERIZED (DW-147):
                        # this is the run-local store, whose interactive writer
                        # legitimately records `effect: "close"`, so it alone
                        # passes `allow_close=True`. The pre-answer loop below
                        # reads a different file and passes False.
                        reason = unusable_answer_reason(value, allow_close=True)
                        if reason is None:
                            answers[key] = value
                        else:
                            unusable[key] = value
                            malformed.append(f"{key}: {reason} (<run>/decisions.json)")
                else:
                    self.journal.append(
                        "sweep-decisions-reload-failed",
                        errors=[f"not a JSON object: {type(stored).__name__}"],
                    )
        closed = 0
        # Adopt out-of-band pre-answers (a human answered decisions an earlier
        # unattended/abandoned sweep left). The ledger edits were already applied
        # when they answered, so here we only take the answer onboard — this run
        # won't re-prompt/re-skip and build answers materialize into bundles.
        pre = decisions_store.load_pre_answers(project_root)
        # The ids adopted from the project store THIS cycle, not every id in
        # `answers`: the write-back rows below name exactly what this phase tried
        # to persist, not the answers the run store already held.
        seeded_ids: list[str] = []
        for decision in plan.decisions:
            if decision.id in answers or decision.id not in pre:
                continue
            pre_answer = pre[decision.id]
            pre_reason = unusable_answer_reason(pre_answer, allow_close=False)
            if pre_reason is not None:
                # `load_pre_answers` validates only the TOP level (decisions.py),
                # so a value here can be any JSON at all. Same degrade as the
                # run-local store above — same predicate, too, but configured for
                # THIS store: `allow_close=False`, because `apply_pre_answer`
                # applies a close to the ledger and never records one here, so a
                # `close` in this file is hand-seeded or corrupt and matches no
                # bundling lane (DW-147). Journaled in the same record, which is
                # why each entry names the store it came from: the two files are
                # different, only one of them is the one to hand-fix, and the
                # reason names the effect alone so the suffix is not duplicated.
                # Nothing is written back to the project store:
                # this phase never repairs either file, and re-answering the
                # decision out of band is what overwrites the unusable value.
                malformed.append(f"{decision.id}: {pre_reason} (project .bmad-loop/decisions.json)")
                continue
            answers[decision.id] = pre_answer
            self.journal.append(
                "decision-preanswered",
                dw_id=decision.id,
                effect=pre_answer.get("effect"),
            )
            seeded_ids.append(decision.id)
        if malformed:
            # The PER-VALUE record: one, however many values it covers, naming the
            # ids that lost their answer and the store each came from. It is not
            # the only one a read can write — a whole-file fault above journals its
            # own, so a run-local store that will not parse AND a malformed
            # pre-answer behind it produce two records, one per fault class. Ids,
            # store names and type names only: an answer's prose stays out of the
            # journal, the way `sweep-decision-option-mismatch` keeps it out.
            self.journal.append("sweep-decisions-reload-failed", errors=malformed)
        if seeded_ids and store_unreadable:
            # DW-264, withheld FIRST: while the store could not be read, no write
            # is attempted at all — so the failed row below is unreachable here and
            # a withheld write is never also reported as failed. Ids and the
            # store's basename only, never answer prose; both fields are already
            # routed (`dw_ids` keylist, `file` benign), so no new field is minted.
            self.journal.append(
                "sweep-decisions-store-write-withheld",
                file=decisions_path.name,
                dw_ids=list(seeded_ids),
            )
        elif seeded_ids:
            # Same helper as `decisions._write_store` (#363), but NOT for #363's
            # reason: `decisions_path` here is the PER-RUN file under
            # `.bmad-loop/runs/<id>/`, which init gitignores, so a stranded temp
            # was never untracked and never held `worktree_clean` False. The
            # project-level `.bmad-loop/decisions.json` is a different file with a
            # near-identical temp name — that is the exposed one. Taken anyway for
            # the fsync and the unique temp name, which two writers of one key
            # would otherwise collide on. Confined to the project root (#593):
            # replacing the NAME, as the bare replace did, left `.bmad-loop/` and
            # `runs/` above it resolved by name, and a link planted at either
            # aimed this write out of the project. The root has to be the PROJECT
            # (`project_root` above; its comment says why not
            # `self.workspace.root`) — and not `self.run_dir` either: a file
            # confined against its own parent walks no components at all, which
            # would refuse nothing.
            #
            # Guarded (DW-262), the DW-247 shape: a directory planted at the store
            # — which the `S_ISREG` probe above deliberately answers SILENTLY —
            # reaches `os.replace` here as `IsADirectoryError` (POSIX) or
            # `PermissionError` (win32), and a refused parent reaches it as
            # `UnconfinedWriteError`, itself an `OSError`. Bare, either aborted an
            # otherwise healthy sweep at a bookkeeping write of answers the human
            # ALREADY gave out of band. The answers stay in `answers` for this
            # cycle's bundling; the row says the store and the ids did not persist,
            # so a resume re-adopts them from the project store instead. Its own
            # kind, not `sweep-decisions-reload-failed`, which is a READER's row.
            # `OSError` alone: unlike `atomic_write_text`, the confined helper
            # never `resolve()`s, so DW-247's `RuntimeError` arm has nothing to
            # catch here.
            try:
                atomic_write_text_confined(
                    decisions_path,
                    # `unusable` first so a well-shaped answer always wins the key:
                    # the entries it holds are the ones the read above could not use,
                    # re-published unchanged rather than dropped by a write this
                    # method makes for an unrelated reason.
                    json.dumps({**unusable, **answers}, indent=2),
                    confine_root=project_root,
                )
            except OSError as exc:
                self.journal.append(
                    "sweep-decisions-store-write-failed",
                    file=decisions_path.name,
                    dw_ids=list(seeded_ids),
                    error=str(exc),
                )
        pending = [d for d in plan.decisions if d.id not in answers]
        answered_interactively = False
        # THREE flags, because the commit and the hand-back ask different
        # questions. `ledger_in_doubt` is the LAST attempt's verdict — set by the
        # degrade below, cleared by the next effect that succeeds — so it means
        # "the bytes now on disk are the ones an effect could not read".
        # `any_effect_faulted` is sticky and only shapes what the human is told. A
        # sticky flag on the commit would be wrong: a walk where DW-1 faults and
        # DW-2 then lands a human-authorized `decision:` line would leave that line
        # uncommitted immediately ahead of this cycle's bundles.
        # `any_effect_landed` is the NON-EMPTY PASS the commit needs beside the
        # withhold: `_apply_decision_effect` is this walk's only ledger write, so a
        # walk that ran none of them — every decision already answered, skipped
        # unattended, or dropped — published nothing and must spawn no git at all
        # (DW-183/DW-185). It is sticky in the other direction, and deliberately:
        # once an effect has written the ledger, a LATER fault is the withhold's
        # business, not this flag's.
        ledger_in_doubt = False
        any_effect_faulted = False
        any_effect_landed = False
        # whether THIS walk latched the commit debt (`_owe_ledger_commit`); a walk
        # that lands no effect retracts it at the end, and a `LedgerWriteError`
        # ahead of any landed effect retracts it on the way out. Declared ahead of
        # the DW-167 re-apply walk below because that walk is a ledger writer too,
        # and it latches the same debt for the same reason the interactive arm does.
        owed_here = False
        # DW-200's signal, collected in the interactive arm below and handed to
        # `_materialize_bundles` through `_cycle`: the ids whose `build` answer was
        # persisted while `record_decision` reported writing no `decision:` line.
        # It covers the False RETURN only.
        #
        # In-memory HALF of the verdict. The interactive arm persists the same id to
        # `state.sweep_unlanded_decisions` as it adds it here, and
        # `_materialize_bundles` checks the union, so an interruption between the
        # non-write and the drop resumes into the same refusal rather than into a
        # materialized bundle. This set is what carries the verdict for the run that
        # observed it; the state list is what carries it across a resume — and
        # `sweep_dropped_decisions`, a separate question again, is what stops the
        # ANNOUNCED drop from being repeated or revived.
        effect_unlanded: set[str] = set()
        # DW-167. The stored answer is written BEFORE the effect is applied (see the
        # interactive arm's comment below, which explains why that order stays), so
        # a crash in that window leaves `<run>/decisions.json` holding an
        # `effect: "close"` over an entry the ledger still lists as open. On resume
        # the read above accepts that answer — `allow_close=True` is correct for
        # this store (DW-147), its in-run writer is the legitimate producer — and
        # `pending` filters the id out, while no `_materialize_bundles` lane matches
        # `close`. The decision was therefore never re-asked and never applied.
        #
        # Closed on the READ side rather than by reordering the write: a stored
        # `close` for an id the LEDGER STILL LISTS AS OPEN is an effect that has not
        # landed, so re-apply it here instead of counting it consumed. "Still open
        # in the ledger" is the only discriminator — the mirror-image crash (effect
        # landed, answer write lost or not) leaves the entry `done`, which this walk
        # skips, so no second `decision:` line is ever added.
        #
        # Scoped to ids in THIS cycle's `plan.decisions` on purpose: `pending`'s
        # filter over that tuple IS the suppression DW-167 names, so an id the fresh
        # triage no longer asks about has no suppressed decision to repair and its
        # own routing already stands.
        reapply = [
            d
            for d in plan.decisions
            if isinstance(answers.get(d.id), dict) and answers[d.id].get("effect") == "close"
        ]
        if reapply:
            ledger = self.workspace.paths.deferred_work
            # REPAIR/WRITE (DW-146), the shape `_prune_pre_answers` reads in: this
            # open set GATES a ledger write, so absence and undecodable bytes are
            # both refused as unknown open work rather than collapsed to "nothing is
            # open". `is None`, never falsiness — an empty-but-PRESENT ledger
            # genuinely has zero open ids and correctly re-applies nothing.
            fault: str | None = None
            # DW-220 splits the two fault legs apart: `fault` still drives the
            # per-candidate rows for BOTH, while only a RAISED read arms the doubt
            # below — see the comment at the arm for why absence is excluded.
            ledger_read_faulted = False
            try:
                ledger_text = deferredwork.read_for_write(ledger)
            except (deferredwork.LedgerReadError, OSError) as e:
                ledger_text, fault = None, f"re-apply gate could not read the ledger: {e}"
                ledger_read_faulted = True
            else:
                if ledger_text is None:
                    fault = "re-apply gate could not read the ledger: the ledger file is gone"
            if fault is not None:
                # One row per CANDIDATE, not one per file: the news is per-decision
                # ("this answer may still be unapplied"), and a single file-shaped
                # row would name no id to chase. The kind is the one
                # `_HANDBACK_LEDGER_MISS` names, so an operator greps one spelling
                # for every non-write — but on the pure resume this walk exists for
                # nobody answered anything, `answered_interactively` is False and the
                # hand-back never prints, and unlike DW-200's lane below this arm
                # raises no notify. So the row is the journal record, not an
                # announcement. What DOES surface an unreadable ledger on that path
                # is `_prune_pre_answers` later in the same cycle:
                # `sweep-preanswer-prune-refused` with `reason="ledger-unreadable"`,
                # plus the `_prune_ledger_unreadable` carry that ends a repeating run
                # rather than committing bytes nobody could decode.
                for decision in reapply:
                    self.journal.append(
                        "sweep-decision-effect-unavailable",
                        dw_id=decision.id,
                        effect="close",
                        error=fault,
                    )
                # DW-220: the EXCEPT clause above arms the doubt, the absence leg
                # does not. This read attempted no effect, but it did OBSERVE the
                # ledger, and what it observed on that leg is bytes this process
                # could not decode or an OS that refused the read — the two classes
                # that make `_write_intent`'s bare `read_for_write` RAISE, which is
                # what a bundle dispatched later this cycle runs into. ABSENCE stays
                # out, on DW-176's discipline: `_loop`'s cycle-top read exits on
                # `no-open` before any dispatch, so a bundle can meet an absent
                # ledger at `_write_intent` only if the file vanishes MID-cycle —
                # a `MissingLedgerEntriesError` there since DW-252, not a thin
                # intent — and an absent ledger ends the NEXT cycle cleanly on
                # `no-open` rather than stopping this one.
                #
                # This sets the LOCAL `ledger_in_doubt`, which a later landed effect
                # clears, so the walk's own final verdict is unchanged by arming here
                # and neither is this phase's commit gate below. The RUN's mirror
                # (DW-218/219) is armed alongside it and TRACKS it within this
                # process — `_release_ledger_doubt` clears the mirror at the same two
                # sites that clear the local latch — but is not clearable ACROSS one:
                # a mirror inherited from `state.json` is a previous process's verdict
                # about bytes still on disk, and no effect landing here speaks to it.
                # That asymmetry is why the mirror is written at the ARM rather than
                # at the tail publish: a process that dies before the tail never
                # computes a final verdict at all.
                if ledger_read_faulted:
                    ledger_in_doubt = True
                    self._record_ledger_doubt()
                any_effect_faulted = True
                reapply = []
            else:
                still_open = deferredwork.open_ids(ledger_text or "")
                reapply = [d for d in reapply if d.id in still_open]
        for decision in reapply:
            answer = answers[decision.id]
            answer_key = _answer_str(answer, "key")
            # `key` and `label` come from the stored answer, which is the only place
            # they survive a resume; `resolution` and `intent` can come ONLY from an
            # agreeing option (DW-123's one agreement discipline — never
            # re-implemented here). The interactive arm above persists exactly
            # key/label/effect/answered_at, and the project store cannot hold a
            # `close` at all (DW-147), so no stored `close` answer ever carries a
            # `resolution` to prefer: reading one off the answer would be dead code
            # dressed as a fallback. `_apply_decision_effect` derives its note as
            # `option.resolution or option.intent`, so when NO option agrees — the
            # key is gone, or the label was re-authored — the walk degrades to a bare
            # `closed by human decision` with no detail, and the `decision:` line
            # carries the answer's label alone. That is the honest floor: the human's
            # rationale lived in the option they picked, and this cycle no longer has
            # it. The close still lands, which is the decision that was authorized.
            option = self._agreeing_option(decision, answer, answer_key)
            effect_option = DecisionOption(
                key=answer_key or (option.key if option else ""),
                label=_answer_str(answer, "label") or (option.label if option else ""),
                effect="close",
                intent=option.intent if option else "",
                resolution=option.resolution if option else "",
            )
            # Routed through `_apply_decision_effect` — the walk's ONLY ledger write
            # — so its landed boolean feeds the same three flags and the same commit
            # gate the interactive arm feeds, and the two dispositions below are the
            # interactive arm's, reused rather than re-decided.
            #
            # `require_open=True` is this walk's alone. The open-set gate above is a
            # FILTER taken outside `record_decision`'s lock, and a snapshot cannot
            # hold across the write it authorizes: a rival writer closing the entry
            # in that window left the ledger with a second `decision:` line over a
            # close already recorded, and this walk reporting it as a repair. The
            # predicate now runs inside the same locked read->edit->write as the
            # mutation, so the promise is enforced where it can actually be kept and
            # a refusal takes the `if not recorded:` arm below.
            # The debt is persisted ahead of the write (`_owe_ledger_commit`), as
            # the interactive arm does: the commit below is gated on
            # `any_effect_landed`, and a process that dies between this re-apply and
            # that commit replays with the entry already `done` — nothing to
            # re-apply, the `decision:` line dirty and unpublished. Idempotent.
            owed_here = self._owe_ledger_commit() or owed_here
            try:
                recorded = self._apply_decision_effect(decision, effect_option, require_open=True)
            except deferredwork.LedgerWriteError:
                # the atomic write failed and the original is untouched; only a
                # walk that has landed nothing retracts (an earlier effect's debt
                # is real). Both typed publish faults are `OSError` subclasses, so
                # they must be caught AHEAD of the degrade arm below.
                if not any_effect_landed:
                    self._retract_ledger_commit(owed_here)
                raise
            except deferredwork.LedgerLockReleaseError:
                raise  # the publish LANDED: the debt stays
            except (
                deferredwork.LedgerReadError,
                OSError,
                ValueError,
                StateRootError,
            ) as e:
                self.journal.append(
                    "sweep-decision-effect-unavailable",
                    dw_id=decision.id,
                    effect="close",
                    error=str(e),
                )
                ledger_in_doubt = True
                self._record_ledger_doubt()  # DW-218/219: at the ARM, not the tail
                any_effect_faulted = True
                continue
            if not recorded:
                # A race, not a contradiction: the entry was open when the gate
                # read and was retired or removed before this write. `ledger_in_doubt`
                # is left alone for the interactive arm's reason — a False return says
                # nothing about whether the bytes on disk are readable.
                #
                # THREE states here where the interactive arm has two, because this
                # walk passes `require_open=True`: the ledger file is gone, no entry
                # carries the id, or the entry is present and no longer open — a
                # rival writer closed it between the gate and the write, which means
                # the close this walk exists to repair is already recorded. Naming
                # the third as "no entry for this id" would send an operator hunting
                # a vanished entry that is sitting in the ledger, done. The probe is
                # a fresh best-effort read taken only to write the sentence: it is a
                # journal row, not a control decision, so a race between the refusal
                # and the probe can at worst mislabel a row nothing acts on.
                self.journal.append(
                    "sweep-decision-effect-unavailable",
                    dw_id=decision.id,
                    effect="close",
                    error=f"record_decision wrote no line: {self._non_write_state(decision.id)}",
                )
                any_effect_faulted = True
                continue
            self.journal.append(
                "sweep-decision-effect-reapplied", dw_id=decision.id, effect="close"
            )
            # NO `post_decision` emit, and that is not an omission. `pre_decision` and
            # `post_decision` BRACKET a human-decision item — the interactive arm emits
            # the first before `prompter.ask` blocks and the second once the effect
            # lands — so a plugin watching the pair sees one open and one close per
            # question actually put to a human. This walk puts no question to anyone:
            # it repairs an item whose `pre_decision` was emitted by the run that
            # crashed. Emitting only the close half here would hand that plugin an
            # unpaired `post_decision` for a prompt this process never opened.
            any_effect_landed = True
            ledger_in_doubt = False
            self._release_ledger_doubt()  # DW-218/219: mirror the clear, not just the arm
            closed += 1
        if not self.prompting:
            pending = [d for d in pending if d.id not in self.state.sweep_skipped_decisions]
            for decision in pending:
                self.journal.append("decision-skipped-unattended", dw_id=decision.id)
            if pending:
                gates.notify(
                    self.policy,
                    self.run_dir,
                    f"{len(pending)} deferred-work decisions pending",
                    "run `bmad-loop sweep` interactively to answer them",
                )
            # Quarantine LAST — after the journal rows AND the notify above, the
            # order `_quarantine`'s docstring promises. Persisting inside the loop
            # instead would leave a crash window between the last `_save()` and
            # the notify in which a resume finds every id already quarantined,
            # filters `pending` empty and never writes the ATTENTION line at all:
            # silently swallowing the announcement rather than repeating it.
            for decision in pending:
                self._quarantine(self.state.sweep_skipped_decisions, decision.id)
        elif store_unreadable and pending:
            # DW-264's interactive half, re-drawn (#794 review): while the run
            # store could not be read this cycle, an answer taken at the prompt
            # could not be persisted — the write is withheld for the reason above,
            # and unlike the seeded arm's adopted answers it has no second copy.
            # Taking it anyway left the authorization in memory alone: the effect
            # walk landed the ledger's `decision:` line, but nothing reads a `build`
            # back off that line, so a process that died between the answer and
            # `_materialize_bundles`' save resumed with the old store, re-asked the
            # question (or, unattended, quarantined it) and the human's `build` was
            # lost. So the question is NOT put: one row names the ids not asked,
            # the notice tells the human what to repair, and the decisions stay
            # pending and UNQUARANTINED — the unattended arm's quarantine is what
            # stops a repeated announcement, and these were never announced as
            # skipped — so the next interactive sweep over a readable store asks
            # them. `store_fault` is the refusal's own text; `dw_ids` and `file`
            # are the withheld row's fields, `error` is diagnostics-dropped.
            self.journal.append(
                "sweep-decisions-prompt-withheld",
                file=decisions_path.name,
                dw_ids=[d.id for d in pending],
                error=store_fault,
            )
            gates.notify(
                self.policy,
                self.run_dir,
                f"{len(pending)} deferred-work decisions not asked",
                f"{decisions_path} could not be read ({store_fault}), so an answer could "
                "not be persisted and none was taken — fix the file, then run "
                "`bmad-loop sweep` interactively again",
            )
        else:
            for decision in pending:
                # announce before blocking on input so observers (TUI, ATTENTION
                # watchers) can tell a sweep is waiting on a human
                self.journal.append(
                    "decision-pending", dw_id=decision.id, question=decision.question
                )
                gates.notify(
                    self.policy,
                    self.run_dir,
                    f"decision needed: {decision.id}",
                    decision.question,
                )
                self._emit("pre_decision", story_key=decision.id)
                option = self.prompter.ask(decision)
                # True from the moment the human answers, which is what the flag
                # means: `_return_after_decisions` owes them a hand-back whether or
                # not the effect below lands.
                answered_interactively = True
                answers[decision.id] = {
                    "key": option.key,
                    "label": option.label,
                    "effect": option.effect,
                    "answered_at": self._today(),
                }
                # Deliberately BARE, unlike the seeded write-back above (DW-262):
                # a human just answered at a prompt, and a write that FAILS must
                # stop the sweep loudly rather than be spent on a bundle a resume
                # cannot reconstruct — the seeded arm can re-adopt from the
                # project store; this one has no second copy. Not reached while
                # the store could not be READ this cycle (DW-264): that arm
                # withholds the prompt itself above, since a write here would
                # replace valid answers the refusal merely hid and an answer held
                # only in memory does not survive a crash.
                atomic_write_text_confined(  # same file, same reasoning as above (#363, #593)
                    decisions_path,
                    json.dumps({**unusable, **answers}, indent=2),  # as above
                    confine_root=project_root,
                )
                self.journal.append(
                    "decision-answered",
                    dw_id=decision.id,
                    key=option.key,
                    effect=option.effect,
                )
                # The effect DEGRADES per decision and the walk carries on
                # (DW-166), the same shape `cli.cmd_decisions` and
                # `tui.app._record_decision` took in the DW-146 pass — and the
                # handler sits here rather than inside `_apply_decision_effect`
                # so the row can name the decision it lost, exactly as those two
                # wrap `apply_pre_answer` at the loop.
                #
                # This is the reachable shape, not a theoretical one: `prompter.ask`
                # above blocks on the human, so a ledger that goes undecodable
                # while the prompt is open raises out of `record_decision`'s locked
                # `read_for_write`. And by then the answer is already persisted to
                # `<run>/decisions.json` and journalled as `decision-answered` — so
                # bare, the run recorded an answer whose ledger line never landed
                # and then crashed. (That ordering is deliberate and stays: the
                # human's answer must survive a crash. It is the reason this
                # degrade matters, not a thing to fix by reordering.)
                #
                # The publish is the exception to the degrade, as in
                # `_close_resolved`: a `LedgerWriteError` out of `record_decision`
                # means the human's answer is in `<run>/decisions.json` and the
                # ledger's atomic write FAILED — not a lock we never got, not bytes
                # we could not read — and that raises. The ordering above is what
                # makes the raise safe: the answer already survives the crash, and
                # a `build` bundle must not be dispatched off an authorization the
                # ledger could not record.
                #
                # And the debt is persisted ahead of it (`_owe_ledger_commit`):
                # the commit below is gated on `any_effect_landed`, this walk's
                # own write, and a process that dies between this effect and that
                # commit replays with the answer already saved — nothing pending,
                # no effect applied, the `decision:` line dirty and unpublished.
                # Idempotent, so one `_save()` per walk however many decisions.
                owed_here = self._owe_ledger_commit() or owed_here
                try:
                    recorded = self._apply_decision_effect(decision, option)
                except deferredwork.LedgerWriteError:
                    # the atomic write failed and the original is untouched. An
                    # EARLIER effect in this walk may have landed, and that debt
                    # is real; only a walk that landed nothing retracts.
                    if not any_effect_landed:
                        self._retract_ledger_commit(owed_here)
                    raise
                except deferredwork.LedgerLockReleaseError:
                    raise  # the publish LANDED: the debt stays
                except (
                    deferredwork.LedgerReadError,
                    OSError,
                    ValueError,
                    StateRootError,
                ) as e:
                    self.journal.append(
                        "sweep-decision-effect-unavailable",
                        dw_id=decision.id,
                        effect=option.effect,
                        error=str(e),
                    )
                    # What is skipped is everything that would claim the effect
                    # landed: no `post_decision` emit, and no `closed` increment,
                    # so the cycle's progress signal does not count a closure the
                    # ledger never received.
                    #
                    # What the human is left with differs by effect. The answer is
                    # already in `<run>/decisions.json`, so the next cycle reloads
                    # it into `answers` and `pending` filters the id out — nothing
                    # is RE-ASKED this run either way. For `close`, the entry stays
                    # open, and the DW-167 re-apply walk above is what picks it up:
                    # a later cycle (or a resume) finds the stored `close` over a
                    # still-open entry and applies it, so a ledger that reads again
                    # repairs itself without a new run. For `build`, the bundle
                    # still materializes from the stored answer. Dispatch requires
                    # the phase's final doubt to clear, for example when a later
                    # effect succeeds. The saved build is retained because a read
                    # fault does not prove that its entry is absent.
                    # DW-200's fourth drop lane is scoped to the
                    # False RETURN below, which is positive proof there is no entry
                    # to build for. Both outcomes are recoverable; crashing the
                    # sweep mid-walk is not, which is the trade this arm makes.
                    #
                    # `ledger_in_doubt` withholds this phase's commit below. The
                    # commit's pathspec IS the ledger, so a commit taken while the
                    # ledger on disk is the text an effect could not read publishes
                    # exactly those bytes — narrowing the scope bounds what else
                    # rides along, it does not make the ledger itself safe to
                    # publish, so the withhold is unchanged. It is the LAST
                    # attempt's verdict, not the walk's: a later effect that
                    # succeeds proves the ledger reads again and clears it, and
                    # that commit then carries the earlier decisions' lines too.
                    # `_close_resolved` refuses its commit on the same fault by
                    # returning early, but its question is simpler — one
                    # all-or-nothing `mark_done_many` batch, so nothing there can
                    # have landed, where this walk writes one decision at a time.
                    ledger_in_doubt = True
                    # DW-218/219, and THIS is the site the entry's stop repro runs
                    # through: the `continue` goes back into a loop whose next
                    # iteration blocks on `prompter.ask`, so a stop or crash at a
                    # LATER decision's prompt ends the process before the tail
                    # publish below ever runs. Mirrored here, at the arm.
                    self._record_ledger_doubt()
                    any_effect_faulted = True
                    continue
                # A False RETURN is the same silent non-write, so it takes the same
                # arm (DW-186). `record_decision` answers False in exactly two
                # states — no ledger file, and no entry carrying this id — and both
                # mean no `decision:` line landed, which is the very claim the
                # `except` above refuses to let the phase make. Bare, the discarded
                # boolean let `closed` count a closure and `post_decision` announce
                # one for an entry the ledger never received. The SAME journal kind
                # on purpose: `_HANDBACK_LEDGER_MISS` prints exactly one kind for an
                # operator to grep, and a second kind here would make that pointer
                # incomplete.
                #
                # `ledger_in_doubt` is deliberately left ALONE — neither set nor
                # cleared. The latch means "the bytes on disk are ones nobody could
                # read", and a False return says nothing either way about that:
                # `record_decision` answers False from its presence guard
                # (`_ledger_present`, DW-255) for a vanished ledger BEFORE it reads
                # anything — a REFUSED ledger raises there instead — so a vanished
                # ledger reaches here having read nothing at all, while a missing
                # entry reaches here off a perfectly good read. So the latch keeps
                # meaning what it meant —
                # the verdict of the last attempt that actually READ — and this arm
                # neither withholds a commit that may carry an earlier decision's
                # authorized line nor clears a doubt it cannot speak to.
                #
                # WHICH of the two states it was is named in `error`, because they
                # are not the same news: a missing entry is one retired id, where a
                # ledger that is gone means every earlier `decision:` line this walk
                # wrote went with it. The sentence comes from `_non_write_state` —
                # the same call the DW-167 re-apply walk makes — whose absence answer
                # is the observation reader's own `("", None)` (DW-265), not an
                # `is_file()` probe re-taken here (DW-281). The window is narrow but
                # real: a ledger refused at the recorder itself raises out of
                # `_ledger_present` and takes the `except` arm above, so this probe
                # sees a fault only when the ledger goes unreadable BETWEEN the
                # recorder's False answer and the probe. In that window `is_file()`
                # suppresses every OS error on Python 3.14 and answers False, so a
                # ledger sitting in place read as GONE, and on 3.11–3.13 it raised
                # the `PermissionError` straight out of this bare arm and ended
                # `run()`. Through the reader a refused ledger reads "holds no entry"
                # and the probe cannot raise. This is a journal sentence, not a
                # control decision: the helper's third sentence ("present but no
                # longer open") is reachable here only as a race — a rival writer
                # adding and closing the entry between `record_decision`'s refusal
                # and the probe — and a race between the two only ever mislabels a
                # row nothing acts on.
                if not recorded:
                    self.journal.append(
                        "sweep-decision-effect-unavailable",
                        dw_id=decision.id,
                        effect=option.effect,
                        error=f"record_decision wrote no line: {self._non_write_state(decision.id)}",
                    )
                    if option.effect == "build":
                        # DW-200. The build lane routed purely on the stored
                        # `effect`, so a `build` answer whose `decision:` line never
                        # landed still materialized a bundle and spent a dev session
                        # on an id the ledger holds no entry for. The close lane got
                        # this discipline from DW-186 — `closed` is not incremented
                        # and no `post_decision` is announced — and this set is what
                        # gives the build lane the same one, at the only place the
                        # non-write is known. Scoped to the False RETURN and to
                        # `build`: the `except` arm above is the unreadable-ledger
                        # case, whose accepted trade is unchanged, and a `keep-open`
                        # answer mints no bundle to withhold.
                        effect_unlanded.add(decision.id)
                        # ...and PERSISTED here, where the verdict is observed,
                        # rather than only returned. The answer is already on disk
                        # saying `build`, so between this line and the drop
                        # `_materialize_bundles` announces there is an interval in
                        # which a stop-and-resume would rebuild the engine, reload
                        # that answer, and — with the returned set rebuilt empty by
                        # the resumed phase — materialize the very bundle this
                        # verdict refuses. The non-write interval is never proof of
                        # success: a resume that finds the id here treats the line as
                        # unlanded, exactly as this frame did.
                        #
                        # A SECOND list beside `sweep_dropped_decisions`, answering a
                        # different question and replacing neither. This one means
                        # "this `build` answer's `decision:` line never landed";
                        # that one keeps its existing and only job, "this drop was
                        # already announced", which is what stops a resume from
                        # re-announcing or reviving the drop. A drop that ANNOUNCES
                        # this id clears it from here as it quarantines it there, so
                        # the two never both hold it and this list never becomes a
                        # second announcement gate. That is the only thing that
                        # clears it: an id a later cycle's triage stops raising keeps
                        # its entry for the life of the run, mints no bundle (the
                        # lane reading it is never reached for an id the plan does
                        # not raise) and is discarded with the run's state.
                        # No notify: the announcement is the drop's, and the
                        # announcement order (journal, notify, quarantine) is
                        # unchanged.
                        self._quarantine(self.state.sweep_unlanded_decisions, decision.id)
                    any_effect_faulted = True
                    continue
                # the ledger read and wrote, so the doubt the last fault raised is
                # settled — whatever it left on disk is now committable, and this
                # walk now HAS something to publish
                ledger_in_doubt = False
                self._release_ledger_doubt()  # DW-218/219: mirror the clear, not just the arm
                any_effect_landed = True
                self._emit("post_decision", story_key=decision.id, decision_action=option.effect)
                if option.effect == "close":
                    closed += 1
        # DW-216. The two arms above publish an OBSERVATION; a phase that attempted
        # nothing has none to publish, and that is the shape the unattended sweep
        # takes every time every decision is skipped. Bare, such a cycle handed
        # `_cycle`'s gate a False latch over a ledger that had gone bad mid-cycle
        # and the first bundle's `_write_intent` died on it. So when — and only
        # when — the phase both attempted no effect and observed no fault, take the
        # ONE read it does not otherwise take. It sits ABOVE the commit gate and
        # the hand-back so the verdict it computes is published before anything
        # acts on it, rather than after.
        #
        # The condition is what keeps this probe from masking the arms that already
        # spoke — every one of them, which is why `_close_ledger_in_doubt` is in it
        # beside the two phase locals: a close phase that faulted has ALREADY made
        # this cycle's observation, and a probe that re-read on top of it would arm
        # `_ledger_in_doubt` on the close phase's behalf, making that arm untestable
        # on any persistent fault class. Scoped, each arm owns a class no other
        # reaches.
        #
        # Arms on the two classes that make `_write_intent`'s bare `read_for_write`
        # RAISE and on nothing else: ABSENCE keeps DW-176's discipline — `_loop`'s
        # cycle-top read exits on `no-open` before any dispatch, so a bundle meets
        # an absent ledger at `_write_intent` only if the file vanishes mid-cycle
        # (a `MissingLedgerEntriesError` there since DW-252), and an absent ledger
        # ends the next cycle on `no-open`. Fixed `reason` token, fault text in
        # `error` — both already
        # `diagnostics._JOURNAL_DROP_FIELDS`, so neither needs new routing. The two
        # tokens land on ONE stop: `_loop`'s shared arm reports `ledger-unreadable`,
        # and a persistent `OSError` also trips `_prune_pre_answers`' own read,
        # which sets `_prune_ledger_inaccessible` — read FIRST by `_loop`, so that
        # precedence reports `ledger-inaccessible` and is unchanged by this probe.
        if (
            not ledger_in_doubt
            and not any_effect_landed
            and not any_effect_faulted
            and not self._close_ledger_in_doubt
        ):
            probe_ledger = self.workspace.paths.deferred_work
            try:
                deferredwork.read_for_write(probe_ledger)
            except (OSError, deferredwork.LedgerReadFault) as e:
                if isinstance(e, deferredwork.LedgerReadFault) and isinstance(e.__cause__, OSError):
                    e = e.__cause__  # Preserve the original OS attribution.
                self.journal.append(
                    "sweep-decision-ledger-refused",
                    ledger=str(probe_ledger),
                    reason="ledger-inaccessible",
                    error=f"{e.__class__.__name__}: {e}",
                )
                ledger_in_doubt = True
                self._record_ledger_doubt()  # DW-218/219: at the ARM, not the tail
            except deferredwork.LedgerReadError as e:
                self.journal.append(
                    "sweep-decision-ledger-refused",
                    ledger=str(probe_ledger),
                    reason="ledger-unreadable",
                    error=str(e),
                )
                ledger_in_doubt = True
                self._record_ledger_doubt()  # DW-218/219: at the ARM, not the tail
        if any_effect_landed and not ledger_in_doubt and not self._ledger_unfit_to_publish():
            # THREE conditions, and they are different questions. `ledger_in_doubt`
            # is the withhold: the last effect faulted, so whatever is on disk is
            # bytes nobody could read. `_ledger_unfit_to_publish()` is the CYCLE's
            # verdict rather than this walk's (DW-216): `_close_resolved` runs ahead
            # of this phase and can have half-written the ledger decodably, and this
            # commit's pathspec IS that file — a landed decision effect here proves
            # the ledger reads, it does not make the close phase's half-write
            # publishable. Without it the withhold, the dispatch gate and the repeat
            # stop all held while this one publisher walked the bogus flip into HEAD.
            # `self._ledger_in_doubt` inside the helper is still the PREVIOUS cycle's
            # value until the assign below, which is harmless because `_loop` never
            # starts another cycle after one that armed. `any_effect_landed` is the
            # non-empty pass:
            # `_apply_decision_effect` is this walk's only ledger write, so a walk
            # that answered nothing (every decision pre-answered, skipped
            # unattended, or dropped) wrote nothing, and no git is spawned
            # (DW-183/DW-185). This is one of the SIX sites gating on a write
            # result or returned effect; three gate on something weaker, and `path_clean`
            # is the uniform floor beneath all nine — the inventory is spelled out
            # at `_close_resolved`.
            #
            # The LEDGER FILE, not the project and not `self.workspace.root`: this
            # phase's write went to the ledger, and `implementation_artifacts` is
            # configurable to any absolute path (see `_commit_ledger`).
            self._commit_ledger(
                "chore(sweep): record deferred-work decisions",
                path=self.workspace.paths.deferred_work,
                family="ledger",
            )
        elif not any_effect_landed:
            # every effect faulted ahead of its write or wrote no line: nothing
            # this walk published, so nothing to settle. The withheld case —
            # an effect landed, then the LAST one faulted on undecodable bytes —
            # keeps the debt: those landed lines are on disk and uncommitted.
            self._retract_ledger_commit(owed_here)
        if answered_interactively:
            # ...on the CYCLE's verdict too (DW-216), for the same reason the commit
            # gate reads it: a hand-back that promised "sweep continues in the
            # background" while `_cycle` withheld every bundle over a close-phase
            # fault told the human the opposite of what the run was about to do.
            self._return_after_decisions(
                every_effect_landed=not any_effect_faulted,
                ledger_in_doubt=ledger_in_doubt or self._ledger_unfit_to_publish(),
            )
        # Assign the final verdict so a healthy phase clears any earlier doubt.
        self._ledger_in_doubt = ledger_in_doubt
        # ...and mirror it onto run state when it is a DOUBT (DW-218/219). In every
        # REACHABLE state this is a defensive no-op, never the first writer: each of
        # the five sites that sets `ledger_in_doubt = True` above records at its own
        # arm, and both sites that set it False go through `_release_ledger_doubt`,
        # so a True local here implies the mirror is already True. It cannot be the
        # persister precisely because the span between an arm and this line includes
        # a `prompter.ask` a stop or crash can end the process in — which is why the
        # arms own the write and this line only refuses to disagree with them.
        # Guarded on True on purpose: the
        # `=` above is a CYCLE-local clear, and letting it clear the persisted flag
        # too would hand the resume the same dispatch this entry exists to refuse.
        # This line is not a release path at all: `_release_ledger_doubt` is the
        # only one, it runs on the POSITIVE proof of a later effect landing, and it
        # clears the mirror only for an arm THIS process made while the close
        # phase's latch is clear — never off a `=` that merely recomputed a phase
        # verdict.
        if ledger_in_doubt:
            self._record_ledger_doubt()
        return answers, closed, frozenset(effect_unlanded)

    def _return_after_decisions(self, *, every_effect_landed: bool, ledger_in_doubt: bool) -> None:
        """Once the human has answered this cycle's decisions over an attached
        terminal, hand it back so the sweep runs its bundles in the background —
        detach a plain-shell client, switch a tmux client back to its origin. A
        plain foreground sweep (nobody attached, no return target) is untouched.

        We then go unattended for the rest of the run: a later --repeat cycle's
        input() would otherwise block forever in a window no one is viewing. New
        decisions defer via the unattended path instead, recorded for
        `bmad-loop decisions` or the next attended sweep.

        The trigger for that is "nobody can be relied on to answer here any
        more", NOT "the hand-back succeeded" — the two come apart on a failed
        return, in opposite directions. A *refused* switch is evidence the
        client is still in this window with a human in front of it (ATTENDED:
        keep prompting, which is the whole point of #227). Everything else
        reports only that no hand-back was verified — a detach that found
        nothing attached, an effect the backend cannot observe, no detach verb
        at all, or a switch the backend cannot vouch for (a timed-out verb, an
        unreadable client count, nothing attached to move) — and under that
        uncertainty going unattended is the outcome that does not strand a
        --repeat cycle on input(); the decisions it defers stay reachable via
        `bmad-loop decisions`. The `sweep-return-no-client` record keeps its
        name across that widening: it has always meant "no hand-back verified",
        which is what an unvouched switch reports too. Only a real return is
        announced: UNREACHABLE prints nothing, since there may be no one to
        read it.

        `every_effect_landed` says only what the printed line may CLAIM, never
        whether to hand back: the trigger above is unchanged, so a walk in which
        every effect faulted still detaches and still goes unattended. It is
        REQUIRED and keyword-only — there is exactly one caller, and a default
        would make the optimistic claim the thing a new caller inherits by
        forgetting. Sticky over the whole walk, unlike the flag that gates the
        phase's commit: a PARTIAL miss is still a miss to the human, and the line
        says "not every decision" rather than claiming a total one either way. The
        answers themselves are on disk in `<run>/decisions.json`, which the next
        cycle reloads, so what is short is the ledger alone. `bmad-loop decisions`
        reconstructs unanswered questions from triage files and the project-level
        pre-answer store; it does not read these run-local answers.
        `sweep-returned-after-decisions` and every other branch are byte-identical
        either way: the journal records the hand-back, and the
        misses are already attributed by `sweep-decision-effect-unavailable`."""
        from .tui import launch  # import-light: launch.py has no textual imports

        outcome = launch.return_attached_client()
        if outcome is launch.ReturnOutcome.ATTENDED:
            return
        self.prompting = False
        if outcome is launch.ReturnOutcome.RETURNED:
            self.journal.append("sweep-returned-after-decisions")
            # Report the final ledger verdict before the sticky partial-miss flag.
            if ledger_in_doubt:
                line = _HANDBACK_LEDGER_HALTED
            elif every_effect_landed:
                line = _HANDBACK_RECORDED
            else:
                line = _HANDBACK_LEDGER_MISS
            self.prompter.print_fn(line)
        else:
            self.journal.append("sweep-return-no-client")

    def _non_write_state(self, dw_id: str) -> str:
        """Which of `record_decision`'s non-write states an id is in, as the tail
        of a `sweep-decision-effect-unavailable` sentence.

        THREE states. Both arms of `_decisions_phase` call this (the interactive
        arm since DW-281), but only the DW-167 replay walk can be refused into the
        third by design: it passes `require_open=True`, so beside "no ledger file"
        and "no entry carries this id" it can also be refused for an entry that is
        present and no longer open — a rival writer closed it between the walk's
        gate and its write, which means the close being repaired is already
        recorded. That is not a missing entry and must not be reported as one:
        the operator would go hunting a vanished entry that is sitting in the
        ledger, done. The interactive arm passes no `require_open`, so it reaches
        the third sentence only as a race — an entry added and closed between
        `record_decision`'s refusal and this probe — which is prose on a row
        nothing acts on, not a state the refusal saw.

        A fresh best-effort read, taken only to write the sentence. The OBSERVATION
        arm (DW-146) is the right one precisely because nothing is written from it
        and it never raises — a ledger that has gone unreadable since the refusal
        answers the empty text, which falls through to "no entry carries this id",
        the same sentence the pre-DW-167 code gave. Absence is the reader's own
        `(None, None)` answer, not an `is_file()` pre-gate (DW-265): that gate
        suppresses every OS error on Python 3.14 and answers False, so a refused
        ledger was reported as GONE there — a sentence that tells the operator
        every `decision:` line already written went with it, when the file is
        sitting in place, unreadable. The reader is the presence-aware
        `observe_ledger`, not `read_for_observation`, for the mirror-image reason
        (PR #794 review): the text-only reader answers the same `""` for a
        present 0-byte ledger as for a missing one, so testing the text's
        truthiness called a ledger that EXISTS and holds no entry gone. `None`
        is absence; `""` is a present, empty ledger, and falls through to the
        missing-entry sentence like any other text without this id.

        `.done`, never `not .open` — the derivation `DWEntry.done`'s docstring
        exists to refuse. Two states of the entry fall through to the
        missing-entry sentence rather than claiming a close that did not happen.
        An entry whose status the format cannot read (`status: opne`, or no
        status line) is NEITHER open nor done: `require_open` refuses it, and
        reporting it as "no longer open" would tell an operator the entry was
        closed when what actually happened is that its status line is broken. An
        entry this probe finds OPEN was reopened or replaced between the refusal
        and the read, which is a race, not a state the refusal saw. Both take the
        sentence this row carried before the third state existed — the
        conservative direction. Nothing acts on the row either way; it is prose
        for an operator, not a control decision.

        FIRST match wins, agreeing with `_find_entry` — and so with the entry
        `record_decision` actually refused on — because a duplicated id would
        otherwise let this sentence describe a different entry than the write
        did."""
        ledger = self.workspace.paths.deferred_work
        text, fault = deferredwork.observe_ledger(ledger)
        if fault is None and text is None:
            return "the ledger file is gone"
        entry = next((e for e in deferredwork.parse_ledger(text or "") if e.id == dw_id), None)
        if entry is not None and entry.done:
            return "the ledger entry is present but no longer open"
        return "the ledger holds no entry for this id"

    def _apply_decision_effect(
        self, decision: Decision, option: DecisionOption, *, require_open: bool = False
    ) -> bool:
        """Record the human's decision on its ledger entry, answering whether a
        `decision:` line actually landed.

        The boolean is the CALLER's non-write signal, not decoration (DW-186).
        `record_decision` answers False in exactly the two states that mean no line
        was written — no ledger file at all, and no entry carrying this `dw_id` —
        and True only when it wrote one. Discarded, those two states are
        indistinguishable from a successful write at the call site, which then
        counts a closure and announces a `post_decision` for an entry the ledger
        never received.

        What False does NOT say is that the ledger was readable: the missing-file
        arm answers before any read. So the caller treats it as a non-write and
        nothing more — see `_decisions_phase`, which leaves `ledger_in_doubt`
        untouched on it for exactly that reason.

        `require_open` adds a THIRD non-write state, and only for the one caller
        that passes it: `_decisions_phase`'s DW-167 replay walk, whose whole
        premise is that the entry is still open (a done entry means the effect
        already landed, so re-applying would double-record it). Defaulted False,
        so the interactive arm — where a decision recorded on an entry someone
        else already closed is still what the human chose — is unchanged. The
        walk's own open-set gate is the FILTER; this is the ENFORCEMENT, taken
        inside `record_decision`'s lock because a snapshot read before the lock
        cannot hold across the write it authorizes.
        """
        ledger = self.workspace.paths.deferred_work
        detail = option.resolution or option.intent
        close_note = None
        if option.effect == "close":
            close_note = "closed by human decision" + (
                f": {option.resolution}" if option.resolution else ""
            )
        # ONE locked read->edit->write (#286/#469). As the `append_decision` +
        # `mark_done` pair it was two acquisitions with a window between them, and
        # a rival writer landing there saw an entry whose decision line says "close
        # it" and whose status still says open — a human answer half-recorded. The
        # bytes are identical to the pair's: `record_decision` inserts the decision
        # line before it applies the close, which is the order the pair produced.
        return deferredwork.record_decision(
            ledger,
            decision.id,
            self._today(),
            option.label,
            detail,
            close_note=close_note,
            require_open=require_open,
        )

    def _commit_ledger(
        self,
        message: str,
        *,
        path: Path,
        family: Literal["ledger", "store"],
        accepted_text: str | None = None,
        accepted_baseline_text: str | None = None,
        accepted_baseline_commit: str | None = None,
    ) -> _LedgerCommitOutcome:
        """Publish the orchestrator bookkeeping FILE a phase just wrote: that one
        file reaches HEAD, and everything else the enclosing repository is
        carrying is left dirty for whoever owns it. No-op when the file already
        matches HEAD.

        The rule is NAME THE FILE YOU PUBLISHED. `path` is the file the caller
        just wrote, and everything else follows from it: it is resolved, both git
        calls run in the resolved parent, and both are pathspec'd to the resolved
        basename. Nothing is derived from a role ("the project owns sweep
        bookkeeping") — the two families name different files because they write
        different files:

        * the seven ledger PUBLISHERS pass `self.workspace.paths.deferred_work`.
          The ledger hangs off `implementation_artifacts`, which
          `bmadconfig._resolve` accepts as any absolute path and
          `ProjectPaths.rebased` leaves unmoved when it sits outside the project
          — so it may be under the project, inside a disjoint `repo_root`, or in
          no repository at all, and git resolves the enclosing repository in all
          three. The WORKSPACE's copy, never `self.paths.deferred_work`, which is
          a different file in a different tree under worktree isolation.
        * the two pre-answer PRUNES pass `decisions.store_path(project)`. The
          pre-answer store is a bare join off the project root
          (`decisions.STORE_REL`) that no config knob can move, and the run dir is
          the anchor no workspace swap relocates.

        `self.workspace.root` is refused at every site. Where `repo_root` names a
        tree DISJOINT from the project it is the separate CODE repo, so a
        workspace-rooted commit interrogated a tree the write never touched
        (DW-160 for the prunes, DW-175 for the publishers): the clean check
        passed, nothing was committed, and the worktree carrying the edit stayed
        dirty ahead of this cycle's bundles. A hardcoded project root fails the
        mirror-image way for the publishers, which is why they do not use one.

        RESOLVED, following symlinks — and that rationale belongs to the LEDGER
        publishers alone (DW-188). Their writer is `platform_util
        .atomic_write_text`, whose default `follow_symlinks=True` resolves the
        target, so a ledger symlinked into the project has its TARGET rewritten.
        Against the lexical parent the two disagreed: the clean check interrogated
        the link's own directory while the bytes landed in the target's
        repository, which then received no commit at all. Resolving here is what
        makes the check, the commit and that write name one file, so
        `follow_symlinks=False` semantics would be exactly wrong for them —
        agreement with the writer is the property, not link-hardening.

        The two PRUNES are a different case and the resolve is not doing that job
        for them. Their writer is `decisions._write_store` via
        `atomic_write_text_confined`, which takes the OPPOSITE symlink policy:
        `follow_symlinks=False` refuses to write through a link planted at the
        store's own name, and a lexical parent walk refuses a redirected directory
        above it. So a store reached through a link is a shape that writer REFUSES
        rather than one it follows, and the resolve here can only ever agree with
        the plain path it did write. It stays uniform because a per-family
        spelling would claim a distinction the callers cannot act on — not because
        the two writers agree about links. Nothing here should be read as a
        promise that a symlinked pre-answer store works; its own writer says it
        does not.

        Both git calls see ONE scope: `verify.path_clean` checks the resolved
        basename and `verify.commit_paths` commits that same single path. That is
        what bounds the blast radius to the published file (DW-183/DW-185) — an
        operator's unrelated in-flight edits in the enclosing repository stay
        dirty rather than riding into a `chore(sweep):` commit — and what
        quarantines a withheld ledger from a LATER commit in the same cycle
        (DW-187): a prune commits the pre-answer store alone, so the undecodable
        bytes the decision phase refused to publish are still unpublished.
        `worktree_clean`'s `:(exclude)policy.toml` wart is gone with it: a single
        pathspec naming one published file cannot reach `policy.toml`.

        `commit_paths` rather than a narrow twin of `commit_story`: it already
        commits an exact path list, and it is stronger than a hand-rolled pair —
        it forces `:(literal)` pathspecs (an `implementation_artifacts` carrying a
        `[`, `*` or `?` reaches here verbatim from the operator's config, where a
        bare operand is a wildmatch glob), keeps a missing-but-TRACKED path as a
        deletion to stage, wraps its own root resolve in `GitError`, and answers
        `None` for "these paths held no change". `_commit_ledger` already knows
        that from `path_clean`, so a `None` here has TWO causes and not one: the
        pathspec went clean between the two calls, or the single operand survived
        neither the working tree nor the index (`commit_paths`' `if not rels:`
        arm, which drops a path git has never seen so one optional operand cannot
        hard-fail a whole commit) — a ledger deleted and left untracked after the
        check reported it dirty. Both mean nothing was published, which is what
        the row below announces.

        `path_clean` still runs FIRST, and is load-bearing rather than an
        optimization: `commit_paths` opens with `git add`, so without the check an
        already-clean publish would stage and re-interrogate a file it had nothing
        to say about — reaching git, and the index, for a non-event. Idempotent
        replays make that the ordinary case, not the rare one: a resumed cycle
        re-closing ids already `done` reproduces the committed bytes exactly.

        A `verify.GitError` from a tree git cannot INTERROGATE degrades to a
        journal row naming the resolved directory and the error, and under this
        rule that degrade is REQUIRED rather than a kindness. `cli`'s sweep
        precondition only requires `paths.repo_root` to be a git repository, so
        neither the project nor a freestanding artifacts directory need be one,
        and `git status` there answers `fatal: not a git repository`. Letting
        that raise through would abort the whole sweep over a destination that
        will answer the same way on every cycle — strictly worse than the missed
        commit it replaces. `decisions.apply_pre_answer` degrades on `GitError`
        for the store ("best effort, so a non-git or dirty tree never blocks the
        on-disk record") and this keeps them agreeing.

        A `GitError` from a commit that was ATTEMPTED is a different fault, and
        for the LEDGER family it propagates. `path_clean` runs first, so by the
        time `commit_paths` raises, git has already answered for this tree: the
        destination is a repository, the ledger is dirty in it, and the commit
        itself failed — a hook refused it, the index could not be written, the
        disk filled. That is not a destination that cannot be published to; it is
        a publication that failed, and the five ledger publishers raised on it
        before they were re-rooted (DW-175) — "their raise is the pre-existing
        contract, and nothing here should quiet a commit failure nobody asked to
        re-root", which the re-rooting then quieted by accident of sharing one
        handler with the store. Restored, because the degrade had a hazard behind
        it and not just a doctrine: the cycle's bundles run next, against a
        baseline this commit was meant to clean, and a dirty ledger there is
        swept into a story commit by `commit_story`'s `add -A` or discarded by a
        failed bundle's rollback — closures and `decision:` lines already
        journalled as landed. The STORE family keeps degrading on an attempted
        commit too, as it did before this PR (DW-160): its two prunes are the
        cycle's last call and a materialize-time drop, the on-disk record is what
        matters for a pre-answer, and re-dropping later is cheap. Observation may
        degrade, repair writes must raise (AGENTS.md); the ledger commit is the
        publication step of a repair, and the store commit is bookkeeping.

        The RESOLVE degrades to the same row for the not-a-repository reason, but
        from an arm of its OWN (DW-260): `path.resolve()` can raise `OSError` (a
        broken link chain, a permission-denied component) or `RuntimeError` (a
        symlink loop), and that pair is caught around the resolve alone, while
        the arm around `unpublishable_target`/`path_clean`/`commit_paths` catches
        `verify.GitError` alone. The split is by SITE, not by class, because the
        two faults say different things: a resolve that fails is the ledger's own
        path refusing to be walked — the readability fact `_cycle`'s dispatch
        gate withholds on — so the resolve arm ARMS the run's doubt for the
        ledger family exactly as the refusal arm below does, where a git fault
        after a successful resolve says nothing about readability and arms
        nothing. Narrowing the git arm loses no fault: `_run_git` translates
        spawn, timeout and decode faults into the `GitError` taxonomy,
        `verify.commit_paths` wraps its own resolves and `lstat` probes the same
        way, and `unpublishable_target` returns a token rather than raising.
        `verify.last_commit_for` guards its own resolve against the same pair.
        Best effort applies to Git interrogation and resolution only: journal
        I/O failures propagate, as they do for other journal writes, and so does
        either arming arm's `state.json` write (`_record_ledger_doubt()` →
        `_save()`, DW-244/DW-260) — a repair write must raise, the doctrine every
        other arming site follows.

        `path` is a REQUIRED keyword argument with no default, replacing the old
        `root=None` arm and its runtime raise. That is strictly louder, not
        laxer: a caller that forgets to name the file it dirtied now fails at call
        time and under pyright, before any run, instead of on whichever branch
        first reached the raise.

        `family` is REQUIRED and keyword-only for exactly that reason, and it is
        DECLARED rather than derived (DW-199/203/205). Before it, this method
        published whatever `path` named without ever asking whether that file was
        still there or still readable, and `verify.commit_paths` deliberately keeps
        a missing-but-TRACKED path as a DELETION to stage — so a ledger removed
        after the phase wrote it was committed as a deletion under a
        `chore(sweep):` message, and a resume whose ledger held undecodable bytes
        published them and only then raised on them. `verify.unpublishable_target` is the
        guard, and it runs between the resolve and `path_clean`: after, because git
        is asked about the RESOLVED target (DW-188) and those are the bytes that
        would be published; before, because a refused publish must spawn no git at
        all — the same property the per-site guards buy. The two families need
        different validation (the seven ledger publishers read through
        `deferredwork.read_for_write`, the two prunes probe the type directly —
        though BOTH families require a REGULAR FILE and both name
        `target-not-a-file` when they do not get one, since DW-238), and
        the family is a caller's declaration because deriving it from the path
        would be precisely the "chosen by role" test the rule above refuses; a
        required keyword-only argument also makes a NEW call site fail under
        pyright rather than silently inherit a validation it does not want.

        A refusal journals `sweep-ledger-commit-refused` and returns, exactly as
        the other two no-op arms do — never a raise, because the read this guard
        takes is bookkeeping and not the sweep's own read. `refuse_cause` is one of
        FOUR fixed tokens (`target-absent`; `target-undecodable`, the ledger's own
        decode fault; `target-unreadable`, a probe that raised;
        `target-not-a-file` — EITHER family's target replaced by a directory (or,
        for the store, a link to one), whose literal pathspec `git add` would
        stage recursively; a FIFO or socket at the name takes the same token) and is minted for
        the reason `stop_cause` (DW-201), `drop_cause` and `regen_cause` were: the
        natural spelling is `reason`, which sits in
        `diagnostics._JOURNAL_DROP_FIELDS` and renders as a presence boolean, so a
        scrubbed dump could not tell the causes apart. The decode or OS fault
        rides in `error` beside it, which is dropped, and `file` carries the same
        lexical basename the sibling rows do. What this guard does NOT do is
        rescue the run: `_loop`'s own ledger read still refuses the same
        undecodable bytes right after the refusal and ends the run there (it raised
        until DW-197 degraded it; either way the cycle does not proceed), which is
        pre-existing behavior. What is gone is the publish that used to precede
        it. A LEDGER-family refusal also ARMS the run's doubt through
        `_record_ledger_doubt()` after its row (DW-244): the refusal says the
        ledger the run wrote cannot be published, which is the very fact
        `_cycle`'s dispatch gate withholds on — without the arm a refusal after a
        landed decision effect left the gate clear and the cycle's bundles reached
        `_write_intent`'s bare `read_for_write` on the same ledger. A store
        refusal says nothing about the ledger and arms nothing. The resolve arm
        above ARMS on the same terms and for the same fact (DW-260): a ledger
        whose `path.resolve()` raises is one whose component `read_for_write`'s
        `stat` walks too, so a bundle dispatched behind the degrade crashed in
        `_write_intent` exactly as it did behind a refusal. The git arm — a
        `GitError` after a successful resolve — does NOT arm: git declining to
        publish a file it could reach is bookkeeping, not evidence about the
        file. Both arming arms are ledger-family only.

        The guard NARROWS a window it does not close, and the residual is worth
        naming the way `_prune_dropped_pre_answer` names its own: a TRACKED target
        removed between `unpublishable_target`'s probe and `commit_paths`' `git add` is
        still staged as a deletion. Closing it would mean changing
        `verify.commit_paths`, whose missing-but-tracked deletion contract other
        callers rely on, so it stays out of bounds here — and the residual is a
        genuine race (a file removed inside a few milliseconds by something that is
        not this sweep), where the shapes this guard exists for are steady states
        the publisher walked into deliberately.

        Both no-op outcomes journal `sweep-ledger-commit-clean` (DW-191).
        `path_clean` also answers True for an ignored path, so a ledger under a
        gitignored `implementation_artifacts` was previously skipped with no row
        at all. The shared row states only that nothing was published; no extra
        git call distinguishes ignored, unchanged or disappeared operands.
        Appends stay outside the guarded Git operations so a journal write fault
        cannot be misreported as a publication failure.

        `file` is the LEXICAL basename (`path.name`), never `target.name`, and
        that distinction is what makes it declarable (DW-192). The degrade row's
        other identifying field is `repo`, which — like `message` and `error`
        beside it — sits in `diagnostics._JOURNAL_DROP_FIELDS`, so a scrubbed dump
        retained nothing saying WHICH of the two published files went
        uncommitted. `file` is declared benign in
        `tests/test_portability_guard.py` and survives the scrub verbatim, and it
        can only be declared benign because every caller passes a code constant —
        `deferred-work.md` (`ProjectPaths.deferred_work`) or `decisions.json`
        (`decisions.STORE_REL`) — so the lexical tail is invariant by
        construction. The RESOLVED tail is not: the DW-188 resolve above follows
        a symlink to a target the OPERATOR named, so `target.name` can be
        arbitrary operator text of exactly the identifier shape `scrub_json`
        ships verbatim, and a benign row for it would be pre-approving that text.
        Bound beside `root` and before either `try` for the same reason `root` is,
        so the degrade row still names the file when the resolve is what failed.
        `repo` still carries the resolved directory for anyone reading the raw
        journal."""
        # `root` is re-bound off the resolved target below because the resolve
        # that derives it can itself fail; until it succeeds the only directory
        # known is the LEXICAL parent, which is what the resolve arm's degrade
        # row then names.
        root = path.parent
        # The LEXICAL tail, bound here rather than off `target` below: see the
        # docstring — it is a code constant at every caller, which is what lets it
        # be a benign (undropped) journal field, and the resolved tail is not.
        name = path.name
        # Bound ahead of both `try`s so neither is possibly-unbound below them: the
        # refusal short-circuits past the two git calls, and `sha`'s `None` is the
        # same "nothing was published" the clean arm reads.
        sha: str | None = None
        clean = False
        refusal: tuple[str, str | None] | None = None
        # TWO arms rather than one (DW-260), discriminated by SITE and not by class:
        # the resolve's fault says the ledger's own path cannot be walked, which is
        # the readability fact the dispatch gate withholds on; a git fault after a
        # successful resolve says nothing about readability. The one-tuple handler
        # that stood here could not tell them apart, so a refused resolve left the
        # run holding the ledger publishable. `ValueError` sits in the resolve arm's
        # tuple (DW-275): `Path.resolve()` raises it for an embedded NUL, and its
        # `UnicodeEncodeError` subclass for a lone surrogate, on CPython POSIX — the
        # same fold `engine._publication_refusal` makes.
        # Flipped the moment `commit_paths` is entered: a `GitError` after that
        # point comes from a commit git was asked to make, not from a tree it could
        # not read (see the docstring — `path_clean` has already answered).
        attempted = False
        try:
            target = path.resolve()
        except (OSError, RuntimeError, ValueError) as e:
            # `repo` (not `root`): an absolute host path, already routed out of
            # diagnostics dumps, exactly as `rearm-baseline-advance-failed` spells
            # the same value. The LEXICAL parent here — the resolve that would have
            # replaced it is what failed. `file` is what SURVIVES a dump: `repo`,
            # `message` and `error` are all dropped.
            self.journal.append(
                "sweep-ledger-commit-unavailable",
                message=message,
                repo=str(root),
                error=str(e),
                file=name,
            )
            # DW-260: the same arm the refusal branch below takes, for the same
            # fact — a ledger whose path cannot be resolved cannot be read by
            # `_write_intent`'s bare `read_for_write` either (its `stat` walks the
            # same component). LEDGER family only, AFTER the row, through the
            # mutate-then-`_save()` helper; see the refusal arm's comment for the
            # release contract and why neither cycle latch is touched.
            if family == "ledger":
                self._record_ledger_doubt()
            return "unavailable"
        try:
            root = target.parent
            # THE TARGET VALIDATION (DW-199/203/205), between the resolve and
            # `path_clean` for two reasons the docstring states: git is asked about
            # the RESOLVED target, and a refused publish must spawn no git at all.
            refusal = verify.unpublishable_target(target, family)
            if refusal is None:
                if accepted_text is None:
                    if accepted_baseline_text is not None:
                        raise RuntimeError(
                            "accepted migration baseline supplied without accepted rewrite"
                        )
                    # Preserve the generic clean short-circuit without catching
                    # journal write faults.
                    clean = verify.path_clean(root, target.name)
                    if not clean:
                        attempted = True
                        sha = verify.commit_paths(root, message, [target])
                else:
                    if accepted_baseline_text is None:
                        raise RuntimeError(
                            "accepted migration rewrite supplied without its baseline"
                        )
                    # Migration alone carries durable byte authority.  Keep the
                    # lexical path as the live identity so a redirected configured
                    # symlink cannot be hidden by the resolved Git operand. Not
                    # `attempted`: the re-raise arm below exists so a refused
                    # ledger commit cannot degrade into the next cycle's dirty
                    # baseline, and `_finish_migration_commit` already ends the
                    # run on `unavailable` — through this arm's journal row, which
                    # keeps the sanitized diagnosis a bare raise would drop.
                    # The baseline commit is the HEAD the accepted baseline was
                    # read beside: it is what lets the publisher tell a ledger
                    # that was never tracked from one a rival commit deleted
                    # after the baseline was taken.
                    sha = verify.commit_path_bound(
                        root,
                        message,
                        target,
                        accepted_text=accepted_text,
                        baseline_text=accepted_baseline_text,
                        baseline_commit=accepted_baseline_commit,
                        live_path=path,
                    )
        except verify.GitError as e:
            if attempted and family == "ledger":
                raise  # a ledger commit git was asked to make failed: publication failed
            # `verify.GitError` ALONE: `_run_git` translates spawn/timeout/decode
            # faults and `commit_paths` its own resolves and `lstat` probes into
            # this taxonomy, and `unpublishable_target` returns rather than raises,
            # so nothing an `OSError`/`RuntimeError` arm could catch here escapes
            # the helpers untranslated. The RESOLVED directory in `repo`, which is
            # the one git was actually asked about. No doubt is armed: see the
            # docstring.
            self.journal.append(
                "sweep-ledger-commit-unavailable",
                message=message,
                repo=str(root),
                error=str(e),
                file=name,
            )
            return "unavailable"
        if refusal is not None:
            cause, error = refusal
            # Outside the guarded git block, like every other row here, so a journal
            # write fault is never misreported as a publication failure. `error`
            # only where the refusal HAS a fault to attribute — an absent target has
            # no exception text, and an empty string would read as one.
            extra = {} if error is None else {"error": error}
            self.journal.append(
                "sweep-ledger-commit-refused",
                message=message,
                file=name,
                refuse_cause=cause,
                **extra,
            )
            # DW-244: the refusal is the run's evidence that the ledger it wrote
            # cannot be published, so it ARMS the doubt `_cycle`'s dispatch gate
            # reads. Bare, a ledger refusal after a landed decision effect
            # (`_decisions_phase`'s tail publish) left every latch clear — the
            # effect landed, so the walk's own verdict was False — and the cycle's
            # bundles went on to `_write_intent`, whose bare `read_for_write`
            # crashed on the same ledger the refusal had just declined. Every one
            # of the four `refuse_cause` tokens arms: each is the same fact for the
            # gate's purposes — including `target-absent`, on which that crash
            # is not the hazard (`_loop`'s cycle-top read exits on `no-open`
            # before any dispatch, so `_write_intent` meets an absent ledger only
            # if it vanishes mid-cycle, a `MissingLedgerEntriesError` since
            # DW-252): a bundle dispatched over an absent TRACKED
            # ledger commits with a whole-tree `git add -A`, which would stage the
            # ledger's DELETION under the bundle's message — the DW-199 hazard by
            # another route — so absence is withheld on too, at the cost that the
            # no-open notice then names a file to RESTORE rather than repair.
            # Where the refusal fires matters for WHEN it is honoured: at the
            # boundary publisher, which sits BELOW `_loop`'s unfit stop, the arm
            # lands after that stop already passed, so cycle N+1 withholds its
            # bundles and ends on the unfit stop — unless one of its own effects
            # lands and releases the arm. LEDGER family only — a store refusal is about
            # `decisions.json` and says nothing about the ledger. AFTER the row,
            # the announce-then-persist order `_quarantine` documents, and through
            # `_record_ledger_doubt()` (mutate-then-`_save()`, so it survives a
            # crash) rather than a bare assignment. Neither cycle latch is touched:
            # `_ledger_in_doubt` is `_decisions_phase`'s `=`-published verdict and
            # `_close_ledger_in_doubt` the close phase's, and the mirror is what the
            # one reader consults. A same-process arm, so it is released on the
            # resolved contract — a LATER landed effect while the close latch is
            # clear (`_release_ledger_doubt`) — which is intended: that is positive
            # proof the ledger reads and writes again, the same proof the walk's
            # own arms rest on.
            if family == "ledger":
                self._record_ledger_doubt()
            return "refused"
        # Git has now answered for the LEDGER: it is at HEAD, either because this
        # commit put it there or because it already was. That settles any debt a
        # publisher latched (`_owe_ledger_commit`) — and only that answer does:
        # the degrade and refusal arms above return with the latch untouched,
        # since a tree git cannot interrogate, or a target that is absent or
        # undecodable, says nothing about whether the write reached HEAD, and
        # neither does the `commit_paths` race below (dirty, then gone untracked
        # before `git add`), which is why `clean or sha` and not `not refusal`.
        # A debt that survives to run end costs the next resume one `path_clean`.
        # The STORE family never latches and never clears.
        if (
            family == "ledger"
            and (clean or sha is not None)
            and self.state.sweep_ledger_commit_owed
        ):
            self.state.sweep_ledger_commit_owed = False
            self._save()
        if sha is None:
            # Already clean/ignored, or raced clean between the two calls. Absence
            # reaches here only as that RACE — a target removed after the guard
            # above read it and left untracked, which is `commit_paths`' `if not
            # rels:` arm. A plainly-absent target never gets this far; it took the
            # refusal arm before `path_clean` ran.
            self.journal.append("sweep-ledger-commit-clean", message=message, file=name)
            return "clean"
        self.journal.append("sweep-ledger-commit", message=message, commit=sha, file=name)
        return "committed"

    def _withhold_ledger_publish(self, message: str, *, dw_ids: list[str] | None = None) -> None:
        """Journal a ledger publish the RUN declined to attempt because it already
        holds the ledger unfit to publish (DW-246) — `sweep-ledger-commit-withheld`
        with the `message` the publish would have carried, the LEXICAL basename in
        `file` (`deferred-work.md`, as every sibling `_commit_ledger` row spells
        it — DW-192) and `reason="ledger-in-doubt"`. `dw_ids` (DW-250) is the
        list of ids the declining publisher would otherwise have set out to prove,
        emitted ONLY when given: `_publish_stranded_close` names its cached plan's
        already-resolved and decision ids, while the three DW-246 callers
        deliberately pass none — `_close_resolved`'s arms have `closed` and `ids`
        in scope, but their DW-246 row shape stays unchanged. `diagnostics`
        already routes the field by name (`_JOURNAL_KEYLIST_FIELDS`).

        A row of its OWN rather than a fifth `refuse_cause` on
        `sweep-ledger-commit-refused`: a refusal is `verify.unpublishable_target`'s
        verdict about the target's BYTES, and here no target was probed and no git
        was spawned — the verdict is the run's, formed earlier by whichever arm
        raised the doubt, and `_ledger_unfit_to_publish()` is the only thing this
        row reports on. Widening `refuse_cause` would also widen a `Literal` that
        `Engine._carry_harvested_deferrals` dispatches on exhaustively.
        `ledger-in-doubt` rather than the dispatch gate's `ledger-unreadable`:
        that gate chose the repeat stop's token because it ends on that stop; a
        withheld publish happens over a ledger that READS, which is exactly the
        state DW-217 minted `ledger-in-doubt` for at the pre-answer prune.

        This helper writes the row and nothing else. It never wraps the
        `_commit_ledger` call it stands in for: each gate sits AT its call site so
        `test_every_sweep_ledger_commit_names_its_own_tree` still counts every
        publisher, and so the publishers that deliberately stay ungated (see
        `_close_resolved`'s inventory) can."""
        fields: dict[str, Any] = {
            "message": message,
            "file": self.workspace.paths.deferred_work.name,
            "reason": "ledger-in-doubt",
        }
        if dw_ids is not None:
            fields["dw_ids"] = dw_ids
        self.journal.append("sweep-ledger-commit-withheld", **fields)

    # ---------------------------------------------------------- bundles

    def _agreeing_option(
        self, decision: Decision, answer: dict[str, Any], answer_key: str
    ) -> DecisionOption | None:
        """The stored answer's key resolved against THIS cycle's decision, but only
        when the option it lands on is still the one the human answered.

        `Decision.option` matches on KEY ALONE, and a key is a position in a list a
        later triage re-authors freely: `_ensure_triage` mints a fresh
        `triage-<n>.json` per repeat cycle while `answers` persists for the whole run
        in `<run>/decisions.json`, and a pre-answer is resolved against a triage
        minted after it was recorded. Either provenance can hand a caller ONE
        question's answer beside a DIFFERENT question's option (DW-118: a stored
        `build` answer keyed "1" met a fresh option "1" spelled "Close as decayed",
        and the bundle shipped the stale intent under the close label). `label` +
        `effect` is the whole agreement test — the only two fields BOTH provenances
        always carry (`record_pre_answer` stores the chosen option's full semantics;
        an in-run answer is written with key/label/effect/answered_at) — and a
        disagreeing option is discarded outright, its mismatch journaled the way
        `sweep-bundle-name-discarded` is.

        ONE agreement discipline for every site that resolves a stored answer against
        a live option (DW-123): the build lane had this test inline while the
        keep-open lane trusted the stored `effect` with no resolution at all, so a
        renumbered option let a stale keep-open answer suppress a bundle under a
        `human-chose-keep-open` skip that reads as the human's decision. Both lanes of
        `_materialize_bundles` run it, and since DW-167 so does `_decisions_phase`'s
        re-apply walk — a THIRD caller, and the only one outside this file's bundling
        half. What the callers differ on is the DISPOSITION of a `None`: the build
        lane falls back to the answer's own intent, the keep-open lane drops the
        answer, and the re-apply walk lands the close anyway with an empty note. See
        each call site.
        """
        option = decision.option(answer_key)
        if option is None:
            return None  # nothing resolved, so there is nothing to describe
        label_matched = option.label == _answer_str(answer, "label")
        if label_matched and option.effect == _answer_str(answer, "effect"):
            return option
        # No triage prose in the record (labels, questions): the fields are closed
        # effect enums and a bare boolean. `answer_effect` says which CALLER wrote the
        # record — it is the stored answer's own effect, invariant per caller but no
        # longer invariant across the three that reach here, and it is what separates
        # a discarded build option from a discarded keep-open one, and both from a
        # re-apply walk's `close` (DW-167), in a journal all three write with the same
        # kind.
        self.journal.append(
            "sweep-decision-option-mismatch",
            decision=decision.id,
            key=answer_key,
            option_effect=option.effect,
            label_matched=label_matched,
            answer_effect=_answer_str(answer, "effect"),
        )
        return None

    def _live_open_ids(self) -> set[str] | None:
        """The ledger's CURRENT open ids, or `None` when the read refused (DW-214).

        One place owns the read-and-degrade, so the screen in
        `_materialize_bundles` below reads as a screen rather than as I/O
        handling.

        REPAIR/WRITE (DW-146), never `read_for_observation`, and this is the
        decision that can be got backwards: the set does not DESCRIBE the ledger
        for a reader, it AUTHORIZES spending a dev session on a stored `build`
        answer. The observation arm answers `("", fault)` on both of its fault
        classes, and `open_ids("")` is empty — which under that screen reads as
        "no id is open" and would drop every adopted build answer in the cycle at
        once. The repair arm keeps absence (`None`) and undecodable bytes
        (`LedgerReadError`) distinguishable from a genuinely empty ledger, which
        is what lets the degrade keep every answer instead.

        `None` on all three fault classes, each under its own FIXED `reason`
        token — the triad `_prune_pre_answers` established: `ledger-absent` (not
        there), `ledger-unreadable` (there, undecodable), `ledger-inaccessible`
        (there, the OS refused). The raw journal distinguishes the repairs;
        `bmad-loop diagnose` reduces both `reason` and `error` to presence flags,
        so these tokens do not survive that export. Absence carries no `error`:
        there is no fault text, only the fact.

        A FOURTH refusal, `ledger-in-doubt`, mirrors the one DW-217 gave
        `_prune_pre_answers` and is the only one of the four taken with the ledger
        PERFECTLY READABLE. It covers the decodable fault class the three reads
        cannot see — a `record_decision` or `mark_done_many` that flipped a
        `status:` and then failed before writing its line — where the bytes read
        back fine and an id the aborted write retired reads as not-open. Here that
        would discard a human's recorded `build` answer, notify, and quarantine the
        id in PERSISTED run state for the rest of the run, so a resume taken after
        the operator repairs the ledger still skips it: strictly worse than the
        prune's consequence, off the same unfit bytes. Like its sibling it sits
        BELOW the read arms — they own the classes they observe, each with its own
        token — and it carries nothing, because the latch it reads is already
        `_loop`-bound.

        None of the four sets a repeat-boundary carry of its own, unlike
        `_prune_pre_answers`' two raising arms. If the cycle reaches that method,
        its fresh read owns the carry. This helper only guarantees degradation
        during materialization: a later `_write_intent` read can still raise
        before pruning runs.

        Arms NO ledger doubt, unlike the DW-167 re-apply gate whose read shape
        this otherwise copies verbatim. Doubt withholds every bundle in the
        cycle, far beyond the one screen this refusal is entitled to degrade —
        and the degrade here is already the conservative direction: a refused
        read screens NOTHING, so every answer keeps the disposition it had.
        Writes nothing — not the ledger, not `<run>/decisions.json`, not the
        project store — and spawns no git.
        """
        ledger = self.workspace.paths.deferred_work
        try:
            text = deferredwork.read_for_write(ledger)
        except (OSError, deferredwork.LedgerReadFault) as e:
            if isinstance(e, deferredwork.LedgerReadFault) and isinstance(e.__cause__, OSError):
                e = e.__cause__  # Preserve the original OS attribution.
            # The class name rides beside the message because "[Errno 13]
            # Permission denied" alone does not say which refusal it was.
            self.journal.append(
                "sweep-decision-open-set-refused",
                ledger=str(ledger),
                reason="ledger-inaccessible",
                error=f"{e.__class__.__name__}: {e}",
            )
            return None
        except deferredwork.LedgerReadError as e:
            # `LedgerReadError` is a plain `Exception` on purpose (DW-146), so it
            # must be named: no `except OSError` would ever see it.
            self.journal.append(
                "sweep-decision-open-set-refused",
                ledger=str(ledger),
                reason="ledger-unreadable",
                error=str(e),
            )
            return None
        # `is None`, never falsiness: an empty-but-PRESENT ledger genuinely holds
        # zero open ids, and every adopted build answer SHOULD be screened out by
        # it. Only the three refusals above mean "unknown open work".
        if text is None:
            self.journal.append(
                "sweep-decision-open-set-refused", ledger=str(ledger), reason="ledger-absent"
            )
            return None
        # DW-217's refusal, applied to this screen. The read above SUCCEEDED, which
        # is exactly the case this arm exists for: on the decodable fault class the
        # ledger reads back perfectly, so none of the three arms above fires, and an
        # id an aborted write flipped out of the open set would take a human's
        # recorded `build` answer with it — dropped, announced, and quarantined on
        # disk for the rest of the run. No `error`: nothing faulted here.
        if self._ledger_unfit_to_publish():
            self.journal.append(
                "sweep-decision-open-set-refused", ledger=str(ledger), reason="ledger-in-doubt"
            )
            return None
        return deferredwork.open_ids(text)

    def _materialize_bundles(
        self,
        plan: TriagePlan,
        answers: dict[str, dict[str, Any]],
        *,
        effect_unlanded: frozenset[str],
    ) -> tuple[list[Bundle], bool]:
        """This cycle's bundles, and whether ANY recorded answer was dropped by one
        of the five drop lanes below — `_cycle`'s progress signal.

        Every drop is progress for the same reason (DW-123, widened to the build
        lanes by DW-135): it quarantines the id in `state.sweep_dropped_decisions`,
        so the id stops being bound to a stored answer nothing can act on and a
        later cycle's fresh triage is free to address it. The signal stays finite
        because that same list bounds each id to ONE drop per run — persisted, so
        the bound holds across a pause/resume too (DW-124) — and a given id can
        raise it at most once however many repeat cycles run.

        `effect_unlanded` is `_decisions_phase`'s per-id verdict (DW-200): the ids
        whose `build` answer this run recorded while `record_decision` reported
        writing no `decision:` line, so the ledger holds no entry to build for. It
        is REQUIRED and keyword-only for the reason
        `_return_after_decisions.every_effect_landed` is required — an empty default
        is the optimistic claim, and a new caller would inherit it by forgetting.

        The lane checks the UNION of that argument and
        `state.sweep_unlanded_decisions`, which the phase persists at the moment it
        observes the non-write. The argument alone covers only the frame that
        answered the decision; the persisted half covers the interval between the
        non-write and this drop, in which a stop-and-resume reloads a stored answer
        still saying `build` and re-enters here with an empty argument. Announcing
        the drop clears the id from that list in the same `_save()` that quarantines
        it in `sweep_dropped_decisions`, so an id never sits in both and the verdict
        never becomes a second announcement gate. It is cleared by the drop it
        PRODUCES and by nothing else: an id this lane is not reached for — a later
        cycle's triage stopped raising the decision, or the quarantine above already
        skipped it — keeps its verdict until the run ends, which costs a list entry
        and mints nothing.

        The FIFTH lane (DW-214) answers what `effect_unlanded` structurally cannot.
        That verdict is populated at exactly one site — the interactive
        `if not recorded:` arm — so it speaks only for an answer THIS run recorded
        at a prompt, and says nothing about one adopted from the project store or
        reloaded on a resume. `_ensure_triage`'s cache branch revalidates with
        `expected_open_ids=None`, so a resumed cycle can legally raise a decision
        for an id the ledger no longer holds open, adopt its stored `build` answer
        and spend a whole dev session briefed off a `_write_intent` read that finds
        no entry. So before a stored answer mints a bundle, the ledger's LIVE open
        set is read (`_live_open_ids`) and an id it no longer holds open is dropped
        down this same DW-200 lane — one row, one notify, the same quarantine.

        Lane ORDER is load-bearing in one direction. `effect_unlanded` names a
        NON-WRITE this run observed; the screen names a ledger FACT read just now.
        Where both hold, the older and more specific verdict is what the operator
        should be told, so the screen sits BELOW it and above the `no-intent` lane.

        The read is taken LAZILY and at most ONCE per call — the two locals below
        `bundles` are that cache and its latch — so a cycle with no adopted `build`
        answer reaching the screen takes no read at all, and two candidates share
        one read (and one refusal row, since the fault is cached with it).

        The same screen covers the PLAN's bundles too (DW-252), in the final keep
        loop. The cache branch's `expected_open_ids=None` revalidation admits a
        cached bundle naming an id a rival writer has since retired just as it
        admits a stale decision, and the keep loop screened those bundles only
        against `failed_ids | keep_open_ids` — so a resumed cycle spent a dev
        session on a bundle whose intent document could not find one of its
        entries. A bundle whose ids are not ALL open is dropped WHOLE (the triage
        intent prose was authored for the whole id set, and the adjacent
        `failed-or-escalated-earlier` lane already drops whole bundles on partial
        overlap) under `sweep-bundle-skipped` `reason="entry-not-open"`, naming the
        ids that are not open. It sits AFTER that overlap check so the older rows
        keep precedence, shares the one lazy read and latch above (a refused read
        screens nothing here either), and is a SKIP rather than a decision drop:
        no `drop_cause`, no quarantine, and `answer_dropped` untouched — there is
        no stored answer to release. A cycle whose only bundles were skipped this
        way therefore reports NO progress, so a `--repeat` run stops on
        `no-progress` there rather than re-triaging, and the skipped bundle's
        still-open ids wait for the next `bmad-loop sweep`: the skip deliberately
        errs toward stopping rather than being counted as progress.
        """
        self._emit("pre_materialize_bundles")
        bundles = list(plan.bundles)
        answer_dropped = False
        # The DW-214 screen's lazy read, cached across the loop: `open_screen` is
        # the live open set (or `None`, meaning the read refused and the screen is
        # OFF), and the latch is what distinguishes "not read yet" from "read, and
        # it refused" — a bare `is None` check would re-read on every candidate and
        # write a refusal row per candidate for a fault that is one file's.
        open_screen: set[str] | None = None
        open_screen_read = False
        for decision in plan.decisions:
            answer = answers.get(decision.id)
            # `isinstance` rather than truthiness: `answers`' annotation is a
            # contract this method cannot enforce, and the test suite is the caller
            # that hands it a map directly rather than through `_cycle`. Inside
            # `src/` the only caller IS `_cycle` (a resume re-enters there too), so
            # `_decisions_phase`'s read-site guard covers the production path — but
            # a lane that trusts the annotation aborts materialization on a
            # `.get(...)` the moment anything else supplies the map. A silent skip
            # either way: an unusable answer is journaled where it is read, not
            # once per lane that declines to use it.
            if not isinstance(answer, dict) or answer.get("effect") != "build":
                continue
            if decision.id in self.state.sweep_dropped_decisions:
                continue  # announced dropped earlier this run (see __init__)
            if decision.id in effect_unlanded or decision.id in self.state.sweep_unlanded_decisions:
                # DW-200. The human answered `build`, the answer was persisted and
                # journaled `decision-answered` — and then `record_decision`
                # reported writing no `decision:` line, which it does in exactly the
                # two states that mean the ledger holds no entry for this id (no
                # ledger file, no such entry). Routing on the stored `effect` alone,
                # this lane still built a bundle and spent a dev session briefing it
                # from `_write_intent`'s ledger read — on an entry that is not there.
                # The close lane has refused to claim that since DW-186; this is the
                # build lane's half of the same discipline.
                #
                # A DROP rather than a re-ask, matching the two build-lane drops
                # below: there is no `decision:` line to double-apply, the entry (if
                # a rival writer merely retired it) is left exactly as found, and the
                # quarantine is what makes a later cycle's fresh triage free to
                # address the id. The signal covers the False RETURN only — the
                # `except` arm's run-anyway trade is deliberately untouched, since an
                # unreadable ledger is no evidence the entry is gone.
                #
                # THREE lists, three questions, checked in this order.
                # `sweep_dropped_decisions` one line above is "this drop was already
                # announced", so a replayed cycle neither re-announces it nor revives
                # the bundle. `effect_unlanded` is this run's in-memory verdict, and
                # `state.sweep_unlanded_decisions` is the same verdict on disk — the
                # union above — because the argument is rebuilt empty by every
                # `_decisions_phase` and a resume taken between the non-write and
                # this drop would otherwise re-enter with nothing to refuse on and
                # rebuild the bundle off a stored answer that still says `build`.
                self.journal.append(
                    "sweep-decision-answer-dropped",
                    decision=decision.id,
                    drop_cause="effect-unlanded",
                )
                gates.notify(
                    self.policy,
                    self.run_dir,
                    f"decision {decision.id}: recorded build decision discarded",
                    "its ledger entry was gone when the decision was recorded, so "
                    "there is nothing to build against — see "
                    "`sweep-decision-effect-unavailable` for which state it was",
                )
                # The verdict is CONSUMED by the drop it produced: cleared here, in
                # the same `_save()` the quarantine takes, so the two lists never
                # both hold this id and the verdict list cannot accumulate ids whose
                # drop `sweep_dropped_decisions` already covers. Cleared after the
                # journal row and the notify, never before: the announcement order is
                # the one `_quarantine` documents, and a crash between them must
                # resume into a re-announcement rather than into an id that is in
                # neither list and builds again.
                if decision.id in self.state.sweep_unlanded_decisions:
                    self.state.sweep_unlanded_decisions.remove(decision.id)
                self._quarantine(self.state.sweep_dropped_decisions, decision.id)
                answer_dropped = True  # progress: see this method's docstring
                continue
            # DW-214. The lane above claims one class of unlanded build answer: the
            # ones a `_decisions_phase` in THIS run watched `record_decision` refuse
            # to write a `decision:` line for. Nothing populates that verdict for an
            # answer adopted from the project store, or reloaded from
            # `<run>/decisions.json` on a resume — and the resumed cycle is exactly
            # where the hazard lives, because `_ensure_triage`'s cache branch
            # revalidates with `expected_open_ids=None` and will happily re-raise a
            # decision for an id a rival writer retired since the plan was cached.
            # Routed on the stored `effect` alone, such an answer built a bundle and
            # spent a dev session on an entry `_write_intent`'s ledger read cannot
            # find. So screen the id against the ledger as it stands NOW.
            #
            # Read LAZILY and once: the first candidate to reach here pays for it,
            # a cycle whose answers were all shape-guarded, quarantined or unlanded
            # above pays nothing, and a second candidate reuses the same answer —
            # including a refusal, so one bad ledger writes one row and not one per
            # id. The latch, not `open_screen is None`, is what makes that true.
            if not open_screen_read:
                open_screen, open_screen_read = self._live_open_ids(), True
            # `is not None` GATES the screen: a refused read screens NOTHING and
            # every answer keeps the disposition it had (see `_live_open_ids` for
            # why absence and undecodable bytes are unknown open work rather than
            # zero of it). A resolved-but-EMPTY set is a real answer and screens
            # everything out.
            if open_screen is not None and decision.id not in open_screen:
                # A DROP for the reason the lane above drops: there is no entry to
                # build against, no `decision:` line here to double-apply, and the
                # entry (if a rival writer merely retired it) is left exactly as
                # found. The quarantine is what frees the id — a later cycle's fresh
                # triage is free to address it, and a replayed cycle neither
                # re-announces this nor revives the bundle.
                #
                # `_prune_dropped_pre_answer` is deliberately NOT called: it is
                # keep-open-only by design, and `_prune_pre_answers` retires the
                # store entry at the end of this same cycle anyway, since the id is
                # no longer open and is therefore consumed. Nothing here arms ledger
                # doubt either — that would withhold every bundle in the cycle, far
                # beyond what this refusal is entitled to do.
                self.journal.append(
                    "sweep-decision-answer-dropped",
                    decision=decision.id,
                    drop_cause="entry-not-open",
                )
                gates.notify(
                    self.policy,
                    self.run_dir,
                    f"decision {decision.id}: recorded build decision discarded",
                    "the ledger does not currently hold that entry open, so "
                    "there is nothing to build against — it holds either no such "
                    "entry at all or one that is no longer open",
                )
                self._quarantine(self.state.sweep_dropped_decisions, decision.id)
                answer_dropped = True  # progress: see this method's docstring
                continue
            # ONE spelling of the key for the whole loop body: the lookup, the
            # mismatch record and the note below must name the same string, and
            # `str(answer.get("key"))` stringified a missing key to the literal
            # "None" while the record spelled it "" — and a non-string key to its
            # repr, which `_answer_str` reads as "" instead (DW-141).
            answer_key = _answer_str(answer, "key")
            # `matched` is exactly "an agreeing option was resolved": the helper
            # collapses the two ways that can fail (no such key / a re-authored one)
            # because this lane treats them alike. It tolerates BOTH — a build answer
            # carries its own `intent` payload and can still build from it — and
            # drops only when that payload is missing, a few lines below. The
            # keep-open lane has no payload to fall back on, so it cannot.
            option = self._agreeing_option(decision, answer, answer_key)
            matched = option is not None
            # The stored answer is the PAYLOAD; an agreeing option fills only what
            # the answer omits (`answer or option`, not the reverse). That single
            # expression routes both provenances without a provenance flag:
            # `record_pre_answer` stores the chosen option's full semantics and
            # `validate_triage` requires `intent` on every build option, so a build
            # PRE-answer always carries its own intent and never picks up prose
            # freshly re-authored by a triage the human never read; an IN-RUN
            # answer is written with only key/label/effect/answered_at, so it draws
            # intent and bundle_name from the option — but only an agreeing one.
            intent = _answer_str(answer, "intent") or (option.intent if option else "")
            if not intent:
                # A stale in-run answer: nothing to build from. Dropping it is the
                # only safe action here — where `_apply_decision_effect` landed this
                # decision's ledger line in the cycle that answered it, re-asking or
                # re-applying would double-apply. Since DW-200 the `effect-unlanded`
                # drop above claims one class of unlanded answer before this lane
                # sees it — the ids a `_decisions_phase` reported as False RETURNS,
                # this run or a run this one resumed. That is a narrow claim, not a
                # guarantee that everything arriving here has a ledger line: the
                # unreadable-ledger `except` arm deliberately materializes from the
                # stored answer with no line written, and such an answer reaches this
                # lane whenever its option later stops agreeing. So what arrives is an
                # answer whose triage option has lost its intent, whose `decision:`
                # line is usually on the entry and may not be — and either way a
                # recorded human `build` decision must not vanish on a journal line
                # alone.
                # The ledger entry is untouched, so the next sweep re-triages and
                # re-asks it through `_decisions_phase`.
                # `drop_cause` is a closed FIVE-value enum (`effect-unlanded` and
                # `entry-not-open` above, `no-intent` here, `name-collision` below,
                # `stale-option` in the keep-open lane) so the drop lanes are
                # discriminated by an enum
                # rather than by free text or by a second journal kind (`reason` is
                # deliberately not a benign journal field).
                self.journal.append(
                    "sweep-decision-answer-dropped",
                    decision=decision.id,
                    drop_cause="no-intent",
                )
                gates.notify(
                    self.policy,
                    self.run_dir,
                    f"decision {decision.id}: recorded build decision discarded",
                    "its triage option changed and the stored answer carries no "
                    "intent of its own — the entry stays open for the next sweep",
                )
                self._quarantine(self.state.sweep_dropped_decisions, decision.id)
                answer_dropped = True  # progress: see this method's docstring
                continue
            label = _answer_str(answer, "label") or (option.label if option else "") or "build"
            bundle_name = _answer_str(answer, "bundle_name") or (
                option.bundle_name if option else ""
            )
            # A stored answer's bundle_name never passed `validate_triage` — it was
            # answered out of band against an earlier triage, and a fresh one can
            # renumber or drop the option it named — so this lane was the one route
            # by which a name failing the two option-site gates (#637) still reached
            # `_write_intent` as a directory. Gate it with the same two rules, plus
            # the THIRD rule that site enforces as `duplicate bundle name`: a stored
            # name equal to one already on this list makes both bundles hash to one
            # `_bundle_key` and share one intent directory, so one of them is
            # silently lost. All three by DISCARD rather than by error: the human's
            # build decision is the payload and `decision-<id>` below is the
            # always-legal name it falls back to anyway, so the discard is journaled
            # the way `_normalize_bundle_names`'s repairs are and the sweep proceeds.
            # Why a colliding STORED name is discarded here while the fallback below
            # is SUFFIXED, two remedies for one collision condition: a stored name
            # has somewhere to fall back TO, and falling back is the better repair —
            # it is unvalidated prose carried by an answer whose option may be gone,
            # so a `widen-x-2` variant of it claims a name nothing authored. The
            # fallback has nothing below it, so suffixing is the only repair left.
            if bundle_name and (
                not BUNDLE_NAME_RE.match(bundle_name)
                or safe_segment(bundle_name) != bundle_name
                or any(b.name == bundle_name for b in bundles)
            ):
                self.journal.append(
                    "sweep-bundle-name-discarded",
                    decision=decision.id,
                    original=bundle_name,
                )
                bundle_name = ""
            key = (option.key if option else "") or answer_key or "?"
            name = bundle_name or "decision-" + decision.id.lower()
            # `decision-<id>` READS like a reserved namespace and is not one:
            # `validate_triage` builds its duplicate-name set from plan bundle
            # names and build-option `bundle_name`s only, so a triage plan may
            # legally author a bundle literally named `decision-dw-118` and
            # nothing ever compares this fallback against it. Downstream,
            # `_bundle_key` is a pure function of the name, so two same-named
            # `Bundle`s become ONE task: `_run_bundle` returns early on a
            # terminal task, or writes the second's `intent.md` over the first's
            # under the same dirname, and the human's decision bundle disappears
            # without a record. Reserving the prefix upstream was rejected (it
            # changes the triage-plan contract, escalates one unlucky
            # LLM-authored name into a whole-plan rejection, and still misses a
            # STORED name shaped `decision-<other-id>`, which never passes
            # `validate_triage` at all), so uniqueness is re-established here —
            # the one site where validated plan names, validated option names,
            # unvalidated stored-answer names and the fallback all meet. The
            # taken set is recomputed per decision, never snapshotted before the
            # loop: it must cover the decision bundles appended by earlier
            # iterations, which collide with each other the same way.
            taken = {b.name for b in bundles}
            if name in taken:
                for attempt in range(2, 10):
                    candidate = f"{name}-{attempt}"
                    if candidate not in taken:
                        # `name=` so the record stands on its own, the way its
                        # sibling `sweep-bundle-name-discarded` carries `original=`:
                        # without it the resulting name has to be re-derived by hand
                        # from the id and the suffix.
                        self.journal.append(
                            "sweep-bundle-name-deduped",
                            decision=decision.id,
                            attempt=attempt,
                            name=candidate,
                        )
                        name = candidate
                        break
                else:
                    # The one point in NAME ASSIGNMENT at which a buildable stored
                    # answer yields no bundle — a naming impossibility, not a
                    # mismatch disposition, and bounded so the loop is provably
                    # finite. (Scoped to this step deliberately: an already-named
                    # decision bundle can still be removed further down by the
                    # failed/keep-open skip or by the max_bundles truncation.) Loud
                    # on both surfaces, like the no-intent drop it shares a kind
                    # with.
                    self.journal.append(
                        "sweep-decision-answer-dropped",
                        decision=decision.id,
                        drop_cause="name-collision",
                    )
                    gates.notify(
                        self.policy,
                        self.run_dir,
                        f"decision {decision.id}: recorded build decision discarded",
                        f"its bundle could not be given a name unique among this "
                        f"cycle's bundles ({name} and every -2..-9 suffix are "
                        "taken) — the entry stays open for the next sweep",
                    )
                    self._quarantine(self.state.sweep_dropped_decisions, decision.id)
                    answer_dropped = True  # progress: see this method's docstring
                    continue
            bundles.append(
                Bundle(
                    name=name,
                    dw_ids=(decision.id,),
                    intent=intent,
                    decision_note=(
                        f"The human chose option {key} ({label}) for the "
                        f"question: {decision.question}"
                        if matched
                        # Never quote `decision.question` here: the option this
                        # answer names has since been re-authored, so the question
                        # now on file is not the one the human answered.
                        else f"The human chose option {key} ({label}) against an "
                        f"earlier triage of {decision.id}, whose options have "
                        "since changed. The stored answer's own intent above is "
                        "the contract; the question now on file is not the one "
                        "it answered."
                    ),
                )
            )
        # ids a prior bundle already failed on: re-triaging them would rebuild
        # the same hopeless bundle every repeat cycle (and a cached build-effect
        # decision answer would re-materialize its bundle each cycle)
        failed_ids = {
            i
            for t in self.state.tasks.values()
            if t.story_key.startswith("dw") and t.phase in (Phase.DEFERRED, Phase.ESCALATED)
            for i in t.dw_ids
        }
        # ids a human explicitly chose to keep open: a later triage must not
        # override that answer (bundle dev sessions mark their dw_ids done). Held to
        # the SAME agreement test the build lane above runs (DW-123): this set is
        # read straight off `answers`, whose entries outlive the triage they were
        # answered against, so an unresolved `effect == "keep-open"` let a stale
        # answer suppress an overlapping bundle — journaled only as a
        # `human-chose-keep-open` skip, which reads as the human's decision on a
        # question this cycle never asked.
        #
        # DW-133 proposed gating the stale-option drop below on overlap with THIS
        # cycle's bundles. REFUTED (2026-09-06, human-resolved) — do not
        # re-propose. Two placements are possible and both are wrong:
        #
        # At the drop itself the gate is UNREACHABLE, so it buys nothing.
        # `validate_triage`'s `claim()` (see :178) records every id in one `seen`
        # map and errors on "appears in both", so `plan.bundles` and
        # `plan.decisions` are disjoint by validation; the drop is reached only
        # when `by_id.get(dw_id)` is not None — the id IS in `decisions`, hence in
        # no plan bundle — and a keep-open answer mints no decision bundle of its
        # own (that needs `effect == "build"`, which this lane's own guard
        # excludes). Measured: with the condition replaced by a `raise`, the whole
        # of tests/test_sweep.py passes — it never once fires.
        #
        # Hoisted ABOVE the `decision is None` arm it stops being a no-op and
        # starts doing harm, since that arm is exactly where a kept answer DOES
        # overlap a bundle: it suppresses that bundle, which is what keep-open
        # means. Measured: three tests red, `test_repeat_keep_open_answer_blocks_rebundle`
        # among them — the gate breaks legitimate suppression rather than the drop.
        #
        # Underneath both: the drop's forward-looking timing is load-bearing BY
        # DESIGN. It must fire in the cycle that PROVES the answer stale — where
        # the id is in `decisions` and so in no bundle — so that a LATER cycle's
        # bundle is not silently suppressed. `tests/test_sweep.py`'s
        # `test_keep_open_answer_whose_option_was_re_authored_stops_suppressing_bundles`
        # is the shape to keep in view: its cycle 2 holds DW-1 in `decisions` with
        # no bundles (where the drop must fire) and only cycle 3 bundles DW-1, so
        # any rule keyed on this cycle's bundles can never see them together.
        by_id = {d.id: d for d in plan.decisions}
        keep_open_ids: set[str] = set()
        for dw_id, answer in answers.items():
            # Shape-guarded for the same reason the build lane above is.
            if not isinstance(answer, dict) or answer.get("effect") != "keep-open":
                continue
            if dw_id in self.state.sweep_dropped_decisions:
                continue  # announced dropped earlier this run (see __init__)
            decision = by_id.get(dw_id)
            if decision is None:
                # No decision for this id THIS cycle — the fresh triage bundled or
                # closed it directly instead of re-asking. There is no option to
                # disagree with, so the answer is the only record of the human's
                # choice and it stands: suppressing the bundle is exactly what
                # keep-open means.
                keep_open_ids.add(dw_id)
                continue
            answer_key = _answer_str(answer, "key")
            if self._agreeing_option(decision, answer, answer_key) is not None:
                keep_open_ids.add(dw_id)
                continue
            # Unlike the build lane, a keep-open answer has no payload beyond
            # "keep-open" itself, so without a currently-resolvable, agreeing option
            # there is nothing left to trust and the answer is dropped. Hence a
            # `drop_cause` OF ITS OWN covering both failures — a renumbered option (which wrote
            # a mismatch record just now) and a vanished one (which could not) —
            # rather than one named for the mismatch alone. Dropping is deliberately
            # the loud direction: honouring a stale keep-open answer silently skips
            # work the human never protected, while dropping it is journaled and
            # notified. What this drop does that the build lanes' do not is UNBLOCK
            # the id: a keep-open answer actively suppresses bundles, so removing it
            # makes the id eligible for a bundle a later valid triage cycle can run
            # and close the entry with, where a dropped build answer simply leaves
            # the entry open to be re-asked. Both count as repeat progress (DW-135;
            # this method's docstring says why). The
            # RUN-LOCAL record is what survives — the answer stays auditable in
            # `<run>/decisions.json` and the ledger line `_apply_decision_effect`
            # wrote is unchanged — while an out-of-band pre-answer in the PROJECT
            # store is pruned by the drop itself, below (DW-143): waiting for
            # `_prune_pre_answers` to retire it once a later bundle closed the entry
            # never came due while triage kept re-asking the id as a decision, so
            # every new run re-read the same stale answer and re-dropped it.
            self.journal.append(
                "sweep-decision-answer-dropped",
                decision=dw_id,
                drop_cause="stale-option",
            )
            # Which of the two failures fired, named rather than left to the
            # journal: the notify is the surface an operator actually reads, and
            # "changed" is wrong for a key this triage simply does not offer.
            fate = (
                "is gone from this cycle's triage"
                if decision.option(answer_key) is None
                else "has been re-authored since"
            )
            gates.notify(
                self.policy,
                self.run_dir,
                f"decision {dw_id}: recorded keep-open decision discarded",
                f"the option it answered ({answer_key}) {fate}, so the keep-open "
                f"protection is discarded and {dw_id} is eligible for bundling again",
            )
            self._quarantine(self.state.sweep_dropped_decisions, dw_id)
            # After the row, the notify and the quarantine — announce-then-persist,
            # so a crash mid-drop resumes into the DW-124 skip rather than into a
            # silent removal (the helper's docstring has the full argument). Keep-
            # open only: the build lanes' `no-intent`/`name-collision` drops leave
            # their stored answer alone, since it still carries a payload to re-ask
            # against. `<run>/decisions.json` and the ledger are untouched either
            # way — only the PROJECT store entry goes, and only while it is still
            # THIS answer: a replacement a human recorded out of band since this
            # run last read the store is not the value being dropped, and survives.
            self._prune_dropped_pre_answer(dw_id, "stale-option", answer)
            answer_dropped = True
        kept = []
        for b in bundles:
            overlap = sorted(set(b.dw_ids) & (failed_ids | keep_open_ids))
            if overlap:
                self.journal.append(
                    "sweep-bundle-skipped",
                    name=b.name,
                    dw_ids=overlap,
                    reason=(
                        "failed-or-escalated-earlier"
                        if set(b.dw_ids) & failed_ids
                        else "human-chose-keep-open"
                    ),
                )
                continue
            # DW-252: every bundle that reaches here — plan-authored or minted
            # from a decision — must name only ids the ledger holds open NOW. The
            # same lazy read and latch as the DW-214 lane, so a cycle whose
            # decisions already paid for the read pays nothing more, and a refused
            # read (`None`) screens NOTHING. Dropped whole, never trimmed, and a
            # skip rather than a drop: see the docstring.
            if not open_screen_read:
                open_screen, open_screen_read = self._live_open_ids(), True
            if open_screen is not None:
                not_open = sorted(set(b.dw_ids) - open_screen)
                if not_open:
                    self.journal.append(
                        "sweep-bundle-skipped",
                        name=b.name,
                        dw_ids=not_open,
                        reason="entry-not-open",
                    )
                    continue
            kept.append(b)
        bundles = kept
        if len(bundles) > self.max_bundles:
            dropped = [b.name for b in bundles[self.max_bundles :]]
            self.journal.append("sweep-bundles-truncated", dropped=dropped)
            bundles = bundles[: self.max_bundles]
        self._emit("post_materialize_bundles")
        return bundles, answer_dropped

    def _read_intent_ledger(self) -> str | None:
        """The bare ledger read behind a bundle intent document; `None` is
        absence, kept distinct so `_ensure_bundle_intent` can report it as
        `ledger-absent` rather than as every entry missing.

        REPAIR/WRITE (DW-146): these bytes become the bundle intent file a
        session is dispatched on — an empty one would brief the session on
        nothing at all. `LedgerReadError`, including the OS-read subclass
        `LedgerReadFault` (DW-279), PROPAGATES; failure handling is unchanged for
        `_run_bundle` (DW-197's accepted residual); `_ensure_bundle_intent` is the
        one caller that catches them, and it takes the read through here so the
        catch covers the ledger alone and not the intent file's own I/O. A second
        accepted residual sits beside that one since DW-252: `_run_bundle` calls
        `_write_intent` bare, so a rival write that retires an entry between the
        keep-loop screen's read and this one raises `MissingLedgerEntriesError`
        out of the run — crashed, with no journal row — where the old code briefed
        a thin document."""
        return deferredwork.read_for_write(self.workspace.paths.deferred_work)

    def _write_intent(self, bundle: Bundle, dirname: str, *, text: str | None = None) -> Path:
        """Render `bundle`'s intent document under `bundles/<dirname>/` and return
        its path. `text` is the ledger text to reproduce entries from; `None`
        reads it here through `_read_intent_ledger`.

        Refuses with `MissingLedgerEntriesError` BEFORE any side effect when a
        bundle id has no ledger entry at all (DW-252): the document would carry an
        empty "Ledger entries (verbatim)" section for that id and a dev session
        would be spent on it anyway. Missing means no entry PARSED for the id; a
        present-but-closed entry is still emitted verbatim, since the caller — a
        `_run_bundle` on a fresh plan, or a regeneration of a persisted task —
        may legitimately be briefing on work the ledger has since retired.

        The write is confined to the project root (#593, DW-269): a link planted
        at any directory component below the project root (`.bmad-loop/`,
        `runs/`, the run dir, `bundles/` or `bundles/<dirname>/`) refuses with
        `UnconfinedWriteError` rather than landing the document outside the
        project. That refusal is an `OSError` and PROPAGATES like any other write
        fault here — no degrade arm, by design (DW-243): the document's own write
        faults must never be misreported as a ledger fault."""
        if text is None:
            text = self._read_intent_ledger() or ""
        entries = {e.id: e for e in deferredwork.parse_ledger(text)}
        missing = tuple(i for i in bundle.dw_ids if i not in entries)
        if missing:
            raise MissingLedgerEntriesError(missing)
        blocks = [entries[i].body.rstrip() for i in bundle.dw_ids]
        lines = [
            f"# Deferred-work bundle: {bundle.name}",
            "",
            f"bundle_name: {bundle.name}",
            _INTENT_DW_IDS_PREFIX + ", ".join(bundle.dw_ids),
            "",
            "## Intent",
            "",
            bundle.intent,
        ]
        if bundle.decision_note:
            lines += ["", "## Human decision", "", bundle.decision_note]
        lines += ["", "## Ledger entries (verbatim)", "", "\n\n".join(blocks), ""]
        path = self.run_dir / "bundles" / dirname / "intent.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Surrogates are neutralized over the whole document, not per field: the
        # triage-authored `intent`/`decision_note` are the ones that can revive one
        # (#329), but a document-wide pass covers whatever prose is added here
        # later. Line breaks are deliberately *kept* — this file is markdown, so
        # `_one_line`'s collapse would be damage, and the ledger blocks are read
        # back from a strict-UTF-8 file and so pass through byte-unchanged.
        # Confined against the PROJECT that owns the run dir, never
        # `self.workspace.root` (`_decisions_phase` says why): the `mkdir` above
        # accepts a symlink-to-a-directory at any component, so it is the
        # anchored walk here that refuses a planted parent.
        atomic_write_text_confined(
            path,
            neutralize_surrogates("\n".join(lines)),
            confine_root=_project_of_run_dir(self.run_dir),
        )
        return path

    def _bundle_intent_reason(self, task: StoryTask) -> str | None:
        """Grade the persisted intent document against the task that owns it.
        Returns ``None`` to reuse it untouched, or the reason
        `_ensure_bundle_intent` must regenerate: ``"missing"`` (no `bundle_file`,
        or it is not a file), ``"dw-ids-mismatch"`` (the document's ``dw_ids:``
        line names a different SET than `task.dw_ids`, or carries no such line at
        all), ``"unreadable"`` (the read faulted or the bytes would not decode).

        DW-164: `_run_bundle` writes `task.bundle_file` and only then `_save()`s
        the adopted ids, so a crash between them leaves the persisted ids OLD and
        the document NEW. Re-ordering the two writes does not close that hole, it
        only inverts it — persisted ids NEW, document OLD — and both shapes pair a
        task with a document naming other ids. Grading the document against
        `task.dw_ids` (the field the ledger close, the key dedupe and the dev
        prompt all key on) is TOTAL over both, and the degraded rebuild it triggers
        is exactly the recovery `_ensure_bundle_intent` already exists to perform.

        An EMPTY `task.dw_ids` is deliberately NOT an authority: that is the
        pre-`dw_ids` `state.json` shape, and grading a real document against it
        would trade the bundle's actual brief for a degraded one naming nothing.
        Such a task keeps whatever document it has.

        Bundle identity is SET equality here, as `_bundle_name_for` and
        `_run_bundle` already define it, so a `_write_intent` line whose ids are
        merely reordered still agrees."""
        if not task.bundle_file:
            return "missing"
        path = Path(task.bundle_file)
        if not path.is_file():
            return "missing"
        if not task.dw_ids:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return "unreadable"
        for line in text.splitlines():
            if not line.startswith(_INTENT_DW_IDS_PREFIX):
                continue
            rest = line[len(_INTENT_DW_IDS_PREFIX) :]
            found = {part.strip() for part in rest.split(",") if part.strip()}
            return None if found == set(task.dw_ids) else "dw-ids-mismatch"
        return "dw-ids-mismatch"

    def _pause_on_intent_refusal(
        self, task: StoryTask, reason: str, headline: str, detail: str
    ) -> NoReturn:
        """The tail every regeneration-site refusal in `_ensure_bundle_intent`
        ends in (DW-243/252): notify with the RESUME route, clear the task's
        baseline pair, save, and raise `RunPaused` at the story gate on the task.
        The arm's journal row is written by the arm, before this. The
        `_ensure_migration` duplicate-ids pause is the template.

        `gates.notify` directly, never `_notify_ledger_repair`: that helper steers
        to a fresh `bmad-loop sweep`, which is the wrong route for a run that is
        paused and resumable — following it abandons this run's in-flight task.

        Clearing the baseline pair is safe and necessary: the recovery pass has
        already rolled the attempt back before regeneration ran (or the task never
        held one), so nothing of ours is outstanding, and a stale baseline left
        behind would name the PRE-repair tree — the next env-fault's `_safe_reset`
        would rewind to it and destroy the operator's repair commit (see the
        `_ensure_migration` invariant comment). The next dispatch re-stamps HEAD.

        PAUSE_STORY_GATE, not PAUSE_ESCALATION: the task stays PENDING, and every
        escalation action requires Phase.ESCALATED, so an escalation stage would
        offer only actions that must fail. The gate stage's single action is
        "resume", which is the whole remedy once the ledger is repaired."""
        gates.notify(
            self.policy,
            self.run_dir,
            headline,
            f"{detail} — then `bmad-loop resume {self.state.run_id}`",
        )
        task.baseline_commit = None
        task.baseline_untracked = None
        task.baseline_artifacts = None
        self._save()
        raise RunPaused(reason, PAUSE_STORY_GATE, task.story_key)

    def _pause_for_bundle_close_repair(
        self,
        task: StoryTask,
        ledger: Path,
        fault: deferredwork.LedgerReadError | OSError,
        *,
        site: str,
        dw_ids: list[str],
        operation: Literal["bundle-close", "harvested-deferral-append"] = "bundle-close",
    ) -> NoReturn:
        """Pause a sweep bundle over a ledger its terminal write could not read.

        Bundle-close mutators (DW-280) and the terminal post-merge harvested
        append (DW-286) share ``sweep-bundle-close-refused``, the RESUME route,
        and a story-gate pause. The operation argument keeps both the transient
        notice and durable pause reason truthful while ``site`` remains the
        machine-readable discriminator.

        `fault` is the mutator's own exception, not its text, so the row keeps
        the classification `LedgerReadFault` (DW-279) exists to carry: an OS
        metadata/text-read refusal (`EACCES`, `EIO`, a vanished mount) journals
        `reason="ledger-inaccessible"` and steers the operator at the path's
        permissions or storage, where undecodable bytes journal
        `reason="ledger-unreadable"` and steer at the UTF-8 — the same two tokens
        and the same repairs `sweep-cycle-ledger-refused` and
        `sweep-preanswer-prune-refused` split. A `LedgerReadFault` is tested
        AHEAD of the parent class, as the read contract requires, and its
        `OSError` cause is what the token is read from: the wrapper alone, with
        no chained `OSError`, is treated as the decode arm rather than guessed
        at. The terminal harvest carry's pre-read may hand over a RAW `OSError`
        (its `read_for_write` is not wrapped), which is the OS arm outright.
        `error` is the fault's attributed text either way
        (`engine._ledger_fault_text`), which already names the ledger path and,
        for the OS arm, the `OSError` class and errno.

        The close sites are `_close_bundle_ledger_when_spec_status` (the
        accepted-dev close and review-leg reclose) and the sweep half of
        `_carry_isolated_ledger_writes`. Each is a bare
        `deferredwork.mark_done_many_reopenable`, and every mutator takes its own
        locked `read_for_write` ahead of every write, so a `LedgerReadError` from
        the call itself — including `LedgerReadFault` for OS metadata/text-read
        faults since DW-279 — proves nothing flipped: the pause costs no work.
        The terminal harvest carry also arrives here from the engine dispatch at
        either its pre-read or locked-append site. The direct pre-terminal defer
        carry deliberately stays on the engine escalation route.

        NOT `_pause_on_intent_refusal`: that tail clears the task's baseline pair,
        which is right for a task whose attempt was already rolled back and wrong
        here — an accepted dev attempt still owns its baseline, and the resume
        re-drives from it. NOT the engine's `_pause_for_ledger_repair` either: its
        `ledger-read-refused` row is the engine's inventory, and the sweep carries
        its own refusal vocabulary (`sweep-bundle-close-carry-refused` beside it).
        `gates.notify` directly, never `_notify_ledger_repair`: that helper steers
        to a fresh `bmad-loop sweep`, which abandons this run's in-flight task.

        PAUSE_STORY_GATE, not PAUSE_ESCALATION, for the reason `_pause_on_intent_refusal`
        gives: every escalation action requires Phase.ESCALATED, which none of the
        affected tasks are (DEV_VERIFY, REVIEW_VERIFY, DONE), and the gate
        stage's single action is "resume", which is the whole remedy once the
        ledger reads again. `runs.unreadable_sweep_ledger` fronts that resume for
        the MAIN checkout's ledger — the in-place sites and the carry; under
        `scm.isolation = "worktree"` the two close sites write the unit worktree's
        copy (`self.workspace.paths.deferred_work`), which the notice names via
        `error` and the gate does not probe, so an unrepaired copy simply
        re-pauses here on resume. With the phase untouched the existing resume arms redo the
        close: at DEV_VERIFY, `_recover_inflight_bundle`'s accepted-session arm
        re-enters `_resume_after_dev_verify` → `_post_dev_accepted_sync` and the
        close re-drives with no session spent; at DONE with the latch left False,
        `Engine._replay_unlatched_ledger_carries` re-runs the whole carry hook
        ahead of `_loop`; at REVIEW_VERIFY the sweep has no `_resumable_session`
        arm, so the pause takes its restart arm — `_rollback_or_pause` resets the
        attempt to baseline (rollback policy governing) and the bundle is
        re-driven from dev, whose accepted close then lands. That last is the
        pre-existing sweep resume shape, not widened here. A terminal harvested
        append pauses at DONE and replays through the same unlatched-carry
        pre-pass, which appends before attempting the close again."""
        inaccessible = isinstance(fault, OSError) or (
            isinstance(fault, deferredwork.LedgerReadFault) and isinstance(fault.__cause__, OSError)
        )
        error = _ledger_fault_text(ledger, fault)
        self.journal.append(
            "sweep-bundle-close-refused",
            story_key=task.story_key,
            dw_ids=list(dw_ids),
            site=site,
            ledger=str(ledger),
            reason="ledger-inaccessible" if inaccessible else "ledger-unreadable",
            error=error,
        )
        ids = ", ".join(dw_ids)
        # `error` already begins with the ledger's path, so neither string names
        # the path a second time (the engine's `_pause_for_ledger_repair` does the
        # same). One wording per operation, no per-site branch; the only other
        # fork is the fault class, so the remediation matches the refusal — a
        # permissions or storage repair is not a UTF-8 one. No "COMMIT the fix"
        # steer, unlike
        # `_pause_on_intent_refusal`: at the accepted-dev site the session's
        # uncommitted work sits beside the ledger, and a whole-tree commit by hand
        # would swallow it under the repair. The bundle's own commit carries a
        # tracked ledger's repair once it lands.
        if inaccessible:
            headline = "deferred-work ledger inaccessible"
            verb = "read"
            diagnosis = "the ledger could not be read"
            repair = (
                "Repair the ledger's path, permissions or storage by hand (the "
                "orchestrator must be able to read it)"
            )
        else:
            headline = "deferred-work ledger unreadable"
            verb = "decode"
            diagnosis = "the ledger could not be decoded"
            repair = "Repair the ledger by hand (it must be valid UTF-8)"
        if operation == "harvested-deferral-append":
            attempted = "publish a harvested-deferral append"
            resume_detail = (
                "the harvested-deferral append and then the isolated bundle close "
                "with no session spent"
            )
            paused_operation = "harvested-deferral append"
        else:
            attempted = (
                f"publish a bundle close for {ids} "
                "(a close, or a re-assertion of one after review)"
            )
            resume_detail = (
                "the recorded dev result at the accepted-dev close and the isolated "
                "carry with no session spent, and restarting the bundle from dev at "
                "the review-leg reclose, rollback policy governing"
            )
            paused_operation = f"bundle close for {ids}"
        notice = (
            f"**ACTION REQUIRED — {headline}**\n"
            f"Bundle **{task.story_key}** was about to {attempted}, but the "
            f"orchestrator could not {verb} the deferred-work ledger to publish it: "
            f"{error}.\n"
            f"This write did not land and no work was discarded. {repair}"
        )
        gates.notify(
            self.policy,
            self.run_dir,
            f"ACTION REQUIRED: repair the deferred-work ledger for {task.story_key}",
            f"{notice} — then `bmad-loop resume {self.state.run_id}`, which re-drives "
            f"{resume_detail}",
        )
        self._save()
        # The persisted reason (`state.paused_reason`, `run-paused`, the status
        # summary) carries the same diagnosis as the notice: an operator reading
        # only these surfaces must not be told to repair encoding that is fine.
        raise RunPaused(
            f"bundle {task.story_key}: its {paused_operation} could not be "
            f"published because {diagnosis} ({error}); repair the ledger by hand, "
            "then resume",
            PAUSE_STORY_GATE,
            task.story_key,
        )

    def _pause_for_harvest_carry_repair(
        self,
        task: StoryTask,
        ledger: Path,
        fault: deferredwork.LedgerReadError | OSError,
        *,
        site: str,
        terminal_composite: bool,
    ) -> NoReturn:
        """Use the sweep repair gate only for the terminal composite carry."""
        if not terminal_composite:
            super()._pause_for_harvest_carry_repair(
                task,
                ledger,
                fault,
                site=site,
                terminal_composite=terminal_composite,
            )
        self._pause_for_bundle_close_repair(
            task,
            ledger,
            fault,
            site=site,
            dw_ids=[],
            operation="harvested-deferral-append",
        )

    def _ensure_bundle_intent(self, task: StoryTask) -> bool:
        """Guarantee a recovered bundle has the intent file its dev prompt points
        at, and that the file it points at is the one for THIS task's ids. The
        rendered intent.md persists in the run dir and the prompt consumes nothing
        else from the plan, so the normal case is to reuse it untouched. Returns
        `True` when a usable intent document is on disk for the task.

        Only when `_bundle_intent_reason` rejects it — gone, unreadable, or naming
        other ids — do we rebuild a degraded one from the task itself. The triage
        session's authored intent prose is the single unrecoverable piece; the
        verbatim ledger entries _write_intent re-attaches carry the actual work, so
        say plainly that they are now the contract.

        The regeneration's ledger read is CAUGHT here (DW-243), and only the
        ledger read: `_read_intent_ledger` is split out of `_write_intent` so an
        `OSError` from the intent file's own `mkdir`/`atomic_write_text_confined`
        still propagates as before. Undecodable bytes (`LedgerReadError`) and an
        `OSError` from the read journal `sweep-intent-ledger-refused` under the
        existing pair of tokens (`ledger-unreadable`, `ledger-inaccessible`) and
        then PAUSE the run through `_pause_on_intent_refusal`. Bare, the read
        crashed the resume at this exact site, ahead of any cycle gate. A pause,
        deliberately not a stop and not a doubt latch: `_finish_inflight_bundles`
        stays ungated, and a stop can never keep the "re-driven on the next
        resume" promise — `Engine._run_inner` persists `_loop`'s return as
        `finished`, which `bmad-loop resume` refuses outright. `RunPaused` leaves
        the run un-finished with `paused_*` set, which is exactly what
        `cmd_resume` accepts once `runs.unreadable_sweep_ledger` reads the
        repaired ledger.

        A READABLE ledger that lacks an entry for one of the task's ids is the
        other refusal (DW-252): `_write_intent` raises `MissingLedgerEntriesError`
        before creating anything, and this method journals
        `sweep-intent-regen-refused` (`reason="entry-missing"`; an ABSENT ledger
        takes the same kind under `reason="ledger-absent"`, naming the task's
        ids, so the operator is told to restore the file rather than entries in
        a file that is not there) and pauses the same way, with `task.bundle_file`
        and the phase exactly as they were. Pause rather than announce-and-strand:
        a stranded task beside a recovery pass that went on into fresh triage
        could have its name overwritten by a same-name bundle and its ids
        re-adopted; pausing ends the pass, so no name-reservation or ownership
        rule is needed. Every refusal raises; a `True` return is the only way
        out."""
        reason = self._bundle_intent_reason(task)
        if reason is None:
            return True
        match = BUNDLE_KEY_RE.match(task.story_key)
        if match is None:  # pragma: no cover - callers filter on BUNDLE_KEY_RE
            return True
        cycle = int(match.group(1)) if match.group(1) else 1
        name = match.group(2)
        bundle = Bundle(
            name=name,
            dw_ids=tuple(task.dw_ids),
            intent=(
                "Resolve the deferred-work entries reproduced below. This bundle's "
                "original triage intent did not survive the run it was written in, "
                "so the verbatim ledger entries are the authoritative statement of "
                "the work."
            ),
        )
        dirname = name if cycle == 1 else f"c{cycle}-{name}"
        ledger = self.workspace.paths.deferred_work
        try:
            text = self._read_intent_ledger()
        except (OSError, deferredwork.LedgerReadFault) as e:
            if isinstance(e, deferredwork.LedgerReadFault) and isinstance(e.__cause__, OSError):
                e = e.__cause__  # Preserve the original OS attribution.
            # The class NAME rides beside the message: "[Errno 13] Permission
            # denied" alone does not say which refusal it was.
            self.journal.append(
                "sweep-intent-ledger-refused",
                story_key=task.story_key,
                ledger=str(ledger),
                reason="ledger-inaccessible",
                error=f"{e.__class__.__name__}: {e}",
            )
            self._pause_on_intent_refusal(
                task,
                f"bundle {task.story_key}: its intent document must be regenerated "
                f"off the deferred-work ledger, and {ledger} could not be read "
                f"({e.__class__.__name__}: {e}); repair the ledger by hand and COMMIT "
                "the fix, then resume",
                f"bundle {task.story_key}: intent document not regenerated",
                f"the deferred-work ledger {ledger} could not be read "
                f"({e.__class__.__name__}: {e}); repair it by hand and COMMIT the fix",
            )
        except deferredwork.LedgerReadError as e:
            # A plain `Exception` on purpose (DW-146), so it must be named: no
            # `except OSError` would ever see it. Same row shape as
            # `_read_cycle_ledger`: the decode detail is the message itself.
            self.journal.append(
                "sweep-intent-ledger-refused",
                story_key=task.story_key,
                ledger=str(ledger),
                reason="ledger-unreadable",
                error=str(e),
            )
            self._pause_on_intent_refusal(
                task,
                f"bundle {task.story_key}: its intent document must be regenerated "
                f"off the deferred-work ledger, and {ledger} could not be decoded "
                f"({e}); repair the ledger by hand and COMMIT the fix, then resume",
                f"bundle {task.story_key}: intent document not regenerated",
                f"the deferred-work ledger {ledger} could not be decoded ({e}); "
                "repair it by hand and COMMIT the fix",
            )
        if text is None:
            # ABSENT, kept apart from "every entry missing": `or ""` here would
            # name each of the task's ids as missing and tell the operator to
            # restore entries in a file that does not exist. Same kind as the
            # missing-entries arm below, under its own `reason` token.
            self.journal.append(
                "sweep-intent-regen-refused",
                story_key=task.story_key,
                dw_ids=list(task.dw_ids),
                reason="ledger-absent",
            )
            self._pause_on_intent_refusal(
                task,
                f"bundle {task.story_key}: its intent document must be regenerated "
                f"off the deferred-work ledger, and the ledger is absent at {ledger}; "
                "restore the file and COMMIT the fix, then resume",
                f"bundle {task.story_key}: intent document not regenerated",
                f"the deferred-work ledger is absent at {ledger}, so the bundle is "
                "left in flight and not dispatched; restore the file and COMMIT the "
                "fix to re-drive it",
            )
        try:
            task.bundle_file = str(self._write_intent(bundle, dirname, text=text))
        except MissingLedgerEntriesError as e:
            self.journal.append(
                "sweep-intent-regen-refused",
                story_key=task.story_key,
                dw_ids=list(e.ids),
                reason="entry-missing",
            )
            missing = ", ".join(e.ids)
            self._pause_on_intent_refusal(
                task,
                f"bundle {task.story_key}: its intent document must be regenerated "
                f"off the deferred-work ledger, which holds no entry for {missing}; "
                "restore the entries and COMMIT the fix, then resume",
                f"bundle {task.story_key}: intent document not regenerated",
                f"the deferred-work ledger holds no entry for {missing}, so the "
                "bundle is left in flight and not dispatched; restore the entries "
                "and COMMIT the fix to re-drive it",
            )
        self.journal.append(
            "sweep-intent-regenerated",
            story_key=task.story_key,
            dw_ids=list(task.dw_ids),
            path=task.bundle_file,
            # `regen_cause`, not `reason`: `diagnostics._JOURNAL_DROP_FIELDS` holds
            # `reason` as free text and renders it as a presence boolean, which
            # would defeat this field's whole purpose. Closed-slug siblings in
            # this file (`drop_cause`) use the same convention for the same reason.
            regen_cause=reason,
        )
        return True

    # ------------------------------------------------------ override seams

    def _dispatched_spec_for_attempt(self, task: StoryTask) -> str | None:
        """Sweep dispatch owns intent.md, never an accepted bundle spec."""
        return None

    def _requires_dispatched_spec_snapshot(self, task: StoryTask, prompt: str) -> bool:
        """Keep explicit bundle-spec routing separate from recovery ownership.

        Repair and patch-restore prompts name the accepted spec so deterministic
        read-back follows it, but Sweep still owns ``intent.md`` as its dispatched
        input and must never promote that result artifact into attempt ownership.
        """
        return False

    def _retains_dispatched_spec_snapshot_on_repair(self) -> bool:
        """Sweep repairs remain owned by intent.md, not the accepted spec."""
        return False

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        return self._generic_bundle_prompt(task, feedback)

    def _generic_bundle_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        """Bundle invocation for the generic dev primitive (disk-resolved, see
        ``Engine._dev_skill``): the self-contained
        intent.md (intent + verbatim ledger entries) is handed over as freeform
        intent. The orchestrator owns the deferred-work ledger — the skill is told
        not to edit it — and records resolution itself in `_post_dev_accepted_sync`.
        On a repair the bundle spec is re-opened first (B6) so step-01 resumes.

        A patch-restore re-drive (#2564, #75) must point at the bundle spec
        explicitly: only step-01's spec-pointer intent check EARLY EXITs on the
        `in-review` status the re-arm set — before step-01's version-control
        sanity check, which would otherwise HALT `blocked` on the diff
        `_restore_patch` just laid onto the tree. The freeform intent.md pointer
        takes the path where that dirty-tree check runs first."""
        bundle_ref = task.bundle_file or task.story_key
        artifact_only_guidance = (
            "\n\nArtifact-only receipt: only if this session's actual deliverables are "
            "confined to ignored content in the configured `implementation_artifacts` "
            "directory strictly inside the code repository, you may write "
            "`Artifact only: true` on its own line beside `Status:` in this session's "
            "last genuine `## Auto Run Result` section. Author that marker in the "
            "current session, outside fenced blocks and without an orchestrator "
            "repair note; frontmatter does not assert the receipt. The value must "
            "be the strict boolean `true`. Do not assert it for ordinary changes "
            "or other nonqualifying deliverables, or based on old artifacts alone. "
            "The ordinary proof-of-work probe must first positively find no changes; "
            "the receipt gate then requires a positive ignored-file listing scoped "
            "to that directory. The listing cannot prove which files you wrote. "
            "All other verification and ledger-close checks still apply. In an "
            "isolated worktree, successful integration publishes the accepted ignored "
            "bundle spec before teardown. To publish additional ignored regular files, "
            "list their exact paths relative to `implementation_artifacts` in the "
            "accepted spec's `artifact_deliverables` frontmatter list. Directories, "
            "globs, absolute paths, traversal, symlinks, and the orchestrator-owned "
            "ledger and sprint board are forbidden. Undeclared files are not copied. "
            "Publication checks destination baselines captured before execution; "
            "conflicting main-checkout changes pause publication and retain source "
            "artifacts for recovery. Accepting the receipt alone does not publish files."
        )
        if feedback is None:
            if task.restore_patch and task.spec_file:
                return (
                    f"/{self._dev_skill()} Resume review of the in-review spec at "
                    f"`{task.spec_file}` for the deferred-work bundle `{bundle_ref}`. "
                    f"The attempted change was restored onto the working tree after "
                    f"an intent-gap resolution; review it against the amended spec. "
                    f"Do NOT edit the deferred-work ledger; the orchestrator records "
                    f"resolution.{artifact_only_guidance}"
                )
            # A retry after a rolled-back attempt names its verified parked work
            # (#777); a superseded bundle's ref is suppressed by the shared builder.
            preserved = self._retry_preserve_notice(task)
            return (
                f"/{self._dev_skill()} Implement the deferred-work bundle described in "
                f"`{bundle_ref}` — it carries the intent and the verbatim ledger "
                f"entries to resolve. Do NOT edit the deferred-work ledger; the "
                f"orchestrator records resolution.{artifact_only_guidance}"
            ) + (f"\n\n{preserved}" if preserved else "")
        self._reset_spec_for_repair(task)
        spec_ref = task.spec_file or bundle_ref
        return (
            f"/{self._dev_skill()} Resume the autonomous dev session on the in-progress "
            f"spec at `{spec_ref}` for the deferred-work bundle `{bundle_ref}`. The "
            f"previous session's work failed deterministic verification; repair the "
            f"working tree so verification passes without changing the frozen intent "
            f"contract or editing the deferred-work ledger. Verification evidence is "
            f"in `{feedback}`.{artifact_only_guidance}"
        )

    def _post_dev_state_sync(self, task: StoryTask, result_json: dict | None) -> None:
        """No-op: bundles have no sprint-status row for the pre-gate sync.

        This override and the accepted-only override below are one behavior
        change. Leaving the former close here as well would run bundle closure
        twice at two different gate positions.
        """
        return

    def _post_dev_accepted_sync(self, task: StoryTask, result_json: dict | None) -> None:
        """Generic-path ledger single-writer for bundles. The decoupled
        bmad-build-auto skill does not touch the ledger, so the orchestrator marks
        each dw id the bundle owns ``done`` once the bundle's spec reaches the
        terminal status for the current stage. No-op on the legacy path.

        This runs only after the artifact gate, verify commands, and ``decide_dev``
        have accepted the attempt. In particular, ``outcome.ok`` is insufficient:
        a CRITICAL escalation in the session result preempts that outcome. The
        review gate later requires these entries closed; ``_verify_review`` retains
        its separate reclose because a review session can rewrite the ledger.
        """
        if not self._generic_dev():
            return
        spec_file = result_mapping(result_json).get("spec_file")
        if not spec_file:
            return
        success_status = "in-review" if self._dev_review_enabled() else "done"
        self._close_bundle_ledger_when_spec_status(
            task, str(spec_file), success_status, site="bundle-close-locked"
        )

    def _bundle_close_operation_id(self, task: StoryTask) -> str:
        """Stable identity for a close and its possible defer-time undo."""
        return f"{self.state.run_id}/{task.story_key}"

    def _bundle_close_note(self, task: StoryTask) -> str:
        """Resolution note shared by a bundle close and its possible undo."""
        return f"resolved by sweep bundle {task.story_key}"

    def _close_declared_deferred(
        self, task: StoryTask, snapshot: list[_ArmedClose] | None = None
    ) -> None:
        """No-op: a bundle's ledger closure is owned by
        ``_close_bundle_ledger_when_spec_status``, which runs after accepted dev
        because ``verify_review_bundle`` *requires* those entries closed before the
        later commit boundary. Letting the base class's commit-boundary hook (#234)
        also fire here would re-derive closure for a task whose ids come from
        ``task.dw_ids``, not from a ``closes_deferred:`` declaration."""

    def _close_bundle_ledger_when_spec_status(
        self,
        task: StoryTask,
        spec_file: str,
        success_status: str,
        kind: str = "sweep-bundle-closed",
        *,
        site: str,
    ) -> None:
        """Mark the bundle's ids ``done`` once its spec reaches ``success_status``.

        Called once after accepted dev (``_post_dev_accepted_sync``,
        ``site="bundle-close-locked"``) and again by the review-leg reclose
        (``_verify_review``, ``site="bundle-reclose-locked"``). The catch sits here
        rather than at those callers so there is one arm and one row shape; the
        ``site`` kwarg is what tells the two apart in the journal — keyword-only
        with no default, so a further caller must name its own token rather than
        inherit the accepted-dev one.

        The mutator's own locked re-read (DW-280): ``mark_done_many_reopenable``
        takes ``read_for_write`` under the ledger lock ahead of every write, so a
        ledger that turns undecodable or suffers an OS read fault (DW-279) raises
        ``LedgerReadError`` from the call itself with nothing flipped. Bare, that crashed the run at
        the accepted-dev close with the session's work on disk. It now routes to
        ``_pause_for_bundle_close_repair`` — the sweep's own route, not the
        engine's ``ledger-read-refused`` — with ``bundle_closes_intended`` already
        assigned, so the resume arms re-drive the close. ``LedgerReadError`` ALONE:
        an ``OSError`` here is a write fault as often as a read one and stays on
        the DW-182/186 contract.
        """
        spec_path = verify.resolve_spec_path(spec_file, self.workspace.paths)
        if not spec_path.is_file():
            return
        fm = self._observed_frontmatter(spec_path, task.story_key, "bundle-ledger-close")
        if fm is None:
            return
        if verify.status_of(fm) != success_status:
            return
        ledger = self.workspace.paths.deferred_work
        note = self._bundle_close_note(task)
        # Record the intended ids, never only `marked`. This method is called once
        # after accepted dev and again by the review-leg reclose. The second call
        # normally finds every entry already done, so `marked` is empty; deriving
        # the record from it would erase exactly the state a landing bundle needs.
        task.bundle_closes_intended = list(task.dw_ids)
        try:
            marked = deferredwork.mark_done_many_reopenable(
                ledger,
                task.dw_ids,
                self._today(),
                note,
                self._bundle_close_operation_id(task),
            )
        except deferredwork.LedgerReadError as e:
            # A plain `Exception` on purpose (DW-146), so it must be named: no
            # `except OSError` would ever see it. The record above stays as
            # assigned — it is what the resume's re-drive closes.
            self._pause_for_bundle_close_repair(
                task, ledger, e, site=site, dw_ids=list(task.dw_ids)
            )
        if marked:
            self.journal.append(kind, story_key=task.story_key, dw_ids=marked)

    def _reopen_ledger_after_defer(self, task: StoryTask) -> None:
        """Reopen only this run's bundle closes after its code was discarded.

        ``Engine._defer`` deliberately restores the whole post-review ledger after
        rollback so harvested findings survive. That restore can also replay a
        bundle close whose code no longer exists. The operation-specific undo marker
        makes this "undo my close", not "open these ids": human, legacy, and earlier
        run closures remain untouched. Replaying this method is idempotent.
        """
        ledger = self.workspace.paths.deferred_work
        note = self._bundle_close_note(task)
        operation_id = self._bundle_close_operation_id(task)
        # ONE locked read->edit->write (#286/#469): the per-id `mark_open`
        # comprehension this replaces took the lock once per id, and a rollback
        # that leaves some closes undone and others standing is the one shape this
        # method exists to prevent. Order and skip semantics are `mark_open_many`'s
        # own, which are the comprehension's.
        reopened = deferredwork.mark_open_many(ledger, list(task.dw_ids), note, operation_id)
        if reopened:
            self.journal.append("sweep-bundle-reopened", story_key=task.story_key, dw_ids=reopened)

    def _carry_isolated_ledger_writes(self, task: StoryTask) -> None:
        """The base hook's sweep half: re-apply this bundle's ledger CLOSES to the
        MAIN checkout after an isolated unit lands, the base having first carried
        the harvest.

        ``super()`` runs FIRST, and that is a contract rather than a style choice.
        ``deferredwork.append_entry``'s idempotence scan is OPEN-ONLY, so a close
        applied ahead of the harvest hides an already-filed row from it and mints a
        duplicate under a fresh id. The base hook now ends with a CLOSE of its own
        (``_carry_story_deferred_closes``, #458) and still satisfies the rule, since
        both of its appends precede it; the two closes never coexist on one task,
        because ``_close_declared_deferred`` is a no-op here and a story run has no
        bundle. ``_carry_harvested_deferrals`` defends the same
        hazard a second time with its own status-agnostic pre-scan, which is exactly
        why the order is pinned by a test: with that second line of defence in place
        a reversal is silent today and would only surface if the pre-scan were ever
        narrowed back to the writer's semantics.

        ``_close_bundle_ledger_when_spec_status`` writes
        ``self.workspace.paths.deferred_work`` — under ``scm.isolation = "worktree"``
        that is the unit worktree's copy. The shape this rescues is a GITIGNORED
        ledger named in ``scm.worktree_seed``: the flip lands in the worktree, then
        ``finalize_commit``'s ``git add -A`` skips the ignored path in silence, so it
        never rides the unit branch and the merge brings nothing over. The bundle's
        entries stay ``open``, ``deferredwork.open_ids`` re-bundles them, and every
        later sweep re-triages work that is already done — an unbounded loop rather
        than a one-time drop, which is what makes this worth a carry.

        An UNSEEDED gitignored ledger is a different, still-open hole this cannot
        reach: a worktree checks out tracked files only, so the ledger is absent
        there entirely, ``verify_review_bundle`` (which reads the WORKTREE's copy)
        never sees the ids ``done``, and the unit DEFERS on a fixable retry instead
        of landing. That shape is loud where this one is silent, and no DONE-leg
        carry helps a unit that never reaches DONE (#426).

        DONE leg only, deliberately: ``Engine._defer``'s isolated arm calls
        ``_carry_harvested_deferrals`` directly and never this hook, and
        ``_replay_unlatched_ledger_carries``'s DEFERRED leg matches it for the same
        reason. A defer discarded the code the close claims to have RESOLVED, and a
        close is the most expensive engine-side write to leave behind — ``open_ids``
        re-bundles only ``open`` entries, so a wrong ``done`` is invisible to every
        future sweep.

        Keyed on ``task.bundle_closes_intended``, never on the ids the in-worktree
        close managed to flip: those are exactly empty in the broken case above.
        The close is written reopenable, with the same note and operation id as the
        in-worktree close, so a carried row is indistinguishable from one that
        arrived through the merge instead of a second row shape for one event.

        No ``_generic_dev()`` guard, unlike the two ledger WRITERS: the record is
        the guard. ``bundle_closes_intended`` is assigned only by
        ``_close_bundle_ledger_when_spec_status``, which ``_post_dev_accepted_sync``
        reaches on the generic path alone, so on the legacy path — where the session
        owns the ledger — it is empty and this returns before touching anything. A
        second predicate saying the same thing would be a branch no test can redden.

        The commit is best effort, where ``_carry_harvested_deferrals`` re-raises on
        a ledger git can own. The asymmetry is deliberate: that method's raise is
        backed by ``harvest_carry_commit_pending``, so a replay still owes the commit
        after dedup empties its carried list. This carry has no such latch and its
        flips are idempotent, so a replay finds nothing left to commit — raising here
        would cost the run its ``integrate_unit`` over bookkeeping whose real work is
        already done. The flips themselves are unguarded: losing them is the hazard
        this exists to prevent.

        One fault here RAISES, as a pause rather than a crash — the mutator's own
        locked read (DW-280). ``mark_done_many_reopenable`` takes ``read_for_write``
        under the ledger lock ahead of every write, so a MAIN ledger that is
        undecodable when this half runs raises ``LedgerReadError`` from the call
        itself, before the flips the carry exists to make. That is the one fault
        the best-effort argument does not cover: nothing is on disk yet, so
        degrading would lose exactly the closes this hook is for. Bare, it crashed
        the run after the merge with ``isolated_ledger_carried`` False (neither
        ``worktree_flow.integrate_unit`` nor ``_replay_unlatched_ledger_carries``
        catches it). It now routes to ``_pause_for_bundle_close_repair`` under
        ``bundle-close-carry-locked``, the latch left False by the call site — so
        ``bmad-loop resume`` replays the whole hook through
        ``_replay_unlatched_ledger_carries`` once the ledger reads. Since DW-286
        the base half's harvest carry routes both its pre-read and locked append
        through the same sweep-owned story-gate repair route. The direct
        pre-terminal carry from ``Engine._defer`` retains the engine escalation
        route.

        A publication REFUSAL (DW-237) is best effort on strictly stronger terms.
        ``verify.unpublishable_target`` answers a different question from a
        ``GitError`` — not "can git own this path" but "is this operand a publishable
        file at all". For the three DURABLE causes — ``target-absent``,
        ``target-not-a-file`` and ``target-undecodable`` (bytes nobody can decode,
        split from the OS fault by the guard itself; DW-237) — that
        is answered off an on-disk shape a replay re-reads and refuses identically,
        so there is nothing for a raise to buy even in principle.
        ``target-unreadable`` is the one TRANSIENT cause, a probe fault the next pass
        may not see, so that argument does not cover it — but nothing here needs it
        to: this carry holds no commit latch, its flips are idempotent, and its
        commit was already best effort, so a refusal costs it exactly what a
        ``GitError`` already did. It is ``Engine._carry_harvested_deferrals``, the
        one publisher with a durable latch, that keeps ``target-unreadable`` on its
        retry path instead of refusing it — and refuses the durable three, the
        undecodable one included, because git accepts any bytes and a fall-through
        there commits the corrupt ledger. The row is journalled beside the
        ``-uncommitted`` one rather than folded into it: the two name different
        operator repairs.
        """
        super()._carry_isolated_ledger_writes(task)
        if not task.bundle_closes_intended:
            return
        ledger = self.paths.deferred_work
        try:
            carried = deferredwork.mark_done_many_reopenable(
                ledger,
                task.bundle_closes_intended,
                self._today(),
                self._bundle_close_note(task),
                self._bundle_close_operation_id(task),
            )
        except deferredwork.LedgerReadError as e:
            # A plain `Exception` on purpose (DW-146), so it must be named: no
            # `except OSError` would ever see it. `LedgerReadError` includes the
            # OS-read subclass `LedgerReadFault` (DW-279); pre-lock probes and
            # lock/write failures retain raw `OSError` and their existing route.
            self._pause_for_bundle_close_repair(
                task,
                ledger,
                e,
                site="bundle-close-carry-locked",
                dw_ids=list(task.bundle_closes_intended),
            )
        if carried:
            # The DW-237 publishable-target guard, before any git runs: `commit_paths`
            # forces every operand LITERAL, so a ledger replaced by a DIRECTORY is
            # handed to `git add` as a pathspec and staged RECURSIVELY under this
            # `chore(deferred-work):` message. Refused, never raised, on every cause
            # — see the docstring's best-effort argument, which covers this arm too.
            refusal = _publication_refusal(ledger, "ledger")
            if refusal is not None:
                cause, error = refusal
                extra = {} if error is None else {"error": error}
                self.journal.append(
                    "sweep-bundle-close-carry-refused",
                    story_key=task.story_key,
                    dw_ids=carried,
                    refuse_cause=cause,
                    **extra,
                )
            else:
                try:
                    verify.commit_paths(
                        self.paths.repo_root,
                        f"chore(deferred-work): close {task.story_key}'s bundle ids",
                        [ledger],
                    )
                except verify.GitError as e:
                    self.journal.append(
                        "sweep-bundle-close-carry-uncommitted",
                        story_key=task.story_key,
                        dw_ids=carried,
                        error=str(e),
                    )
        self.journal.append("sweep-bundle-close-carried", story_key=task.story_key, dw_ids=carried)

    def _artifact_baseline(self, task: StoryTask) -> dict[str, list[int] | None] | None:
        """Fingerprint the ignored entries under `implementation_artifacts` at the
        attempt's start, so `verify_dev_bundle`'s artifact-only receipt (DW-273)
        credits only what THIS attempt creates or changes. Stamped from
        `self.workspace` — the unit under isolation — like the baseline pair the
        receipt is measured beside. Observation may degrade: a git fault journals
        `bundle-artifact-baseline-unavailable` and stamps `None`, on which the
        receipt refuses (the attempt is still driven; only the relaxation is
        withheld), rather than ending the run over a probe a bundle with a real
        change never needs."""
        paths = self.workspace.paths
        try:
            return verify.artifact_dir_snapshot(self.workspace.root, paths.implementation_artifacts)
        except verify.GitError as e:
            self.journal.append(
                "bundle-artifact-baseline-unavailable",
                story_key=task.story_key,
                attempt=task.attempt,
                error=str(e),
            )
            return None

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        outcome = verify.verify_dev_bundle(
            task,
            self.workspace.paths,
            result_json,
            review_enabled=self._dev_review_enabled(),
            engine_written=self._harvest_gate_exclude(task),
        )
        # The accepted artifact-only receipt (DW-273) is never silent: one row per
        # accepted attempt, mirroring `Engine._verify_dev_artifacts`'s
        # `park-proof-of-work-skipped`. `count` is the number of IGNORED files
        # under `implementation_artifacts` this ATTEMPT created or changed —
        # measured against the `_artifact_baseline` snapshot, since ignored paths
        # carry no git baseline to attribute against — never the directory's
        # whole listing. The bound is the park record's: the flag rides the
        # `passed()` return, so a receipt refused by the dw_ids cross-check inside
        # `verify_dev_bundle` records nothing, while the `[verify]` commands, the
        # review gate (`verify_review_bundle` still requires every id `done`) and
        # the commit all run AFTER this append and may still reject the attempt.
        if outcome.artifact_only_accepted:
            self.journal.append(
                "bundle-artifact-only-accepted",
                story_key=task.story_key,
                attempt=task.attempt,
                dw_ids=list(task.dw_ids),
                count=outcome.artifact_only_residue,
            )
        return outcome

    def _verify_review(self, task: StoryTask):
        # Generic bundle dev sessions are told not to edit deferred-work.md; the
        # orchestrator is the ledger writer. A follow-up review can rewrite the
        # ledger from its own snapshot and re-open entries that were already
        # closed after dev. Re-apply that idempotent closure immediately before
        # verify_review_bundle requires those entries. The distinct journal kind
        # makes "a review rewrote the ledger" greppable when diagnosing runs.
        if self._generic_dev() and task.spec_file:
            self._close_bundle_ledger_when_spec_status(
                task,
                task.spec_file,
                "done",
                kind="sweep-bundle-reclosed",
                site="bundle-reclose-locked",
            )
        outcome = verify.verify_review_bundle(
            task,
            self.workspace.paths,
            self.policy,
            on_results=self._review_command_sink(task),
        )
        if outcome.ok:
            self._accept_review_artifact_source(task)
        return outcome

    def _operator_park_enabled(self) -> bool:
        # A bundle carries no sprint-status entry, so the pair a park is verified
        # against does not exist, and `verify_review_bundle` gates on closed dw
        # ids instead. Whether a deferred-work bundle can owe a human action is a
        # separate question from whether a story can; not answered here. The one
        # relaxation a bundle DOES get is the artifact-only receipt in
        # `verify.verify_dev_bundle` (DW-273), which is a gate-side receipt over
        # the artifacts dir, not a park: no status change, no waiver.
        return False

    def _commit_message(self, task: StoryTask) -> str:
        rendered = self._render_commit_template(task)
        if rendered is not None:
            return rendered
        return f"sweep {task.story_key}: {', '.join(task.dw_ids)} via bmad-loop"
