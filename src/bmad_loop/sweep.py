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

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from . import deferredwork, gates, verify
from .engine import Engine, RunPaused
from .escalation import critical_escalations, env_fault_pause_reason, session_failure_reason
from .model import PAUSE_STORY_GATE, Phase, StoryTask
from .platform_util import (
    atomic_write_text,
    atomic_write_text_confined,
    neutralize_surrogates,
    safe_segment,
)
from .runs import _project_of_run_dir
from .statemachine import advance
from .workspace import discard_worktree


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


TRIAGE_KEY = "sweep-triage"
TRIAGE_WORKFLOW = "deferred-sweep-triage"
MIGRATE_KEY = "sweep-migrate"
MIGRATE_WORKFLOW = "deferred-sweep-migrate"
BUNDLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}\Z")
_BUNDLE_NAME_MAX_LENGTH = 40
_BUNDLE_NAME_INITIAL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_BUNDLE_NAME_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
# the inverse of SweepEngine._bundle_key: "dw-<name>" (cycle 1) / "dw<N>-<name>".
# A cycle-1 key always has "-" straight after "dw", so the cycle group matches
# empty and the split stays unambiguous even for a bundle named "2fix".
BUNDLE_KEY_RE = re.compile(r"^dw(\d*)-(.+)\Z")
DECISION_EFFECTS = ("build", "close", "keep-open")


@dataclass(frozen=True)
class _BundleNameRepair:
    field: str
    original: str
    normalized: str


def _normalize_bundle_names(rj: dict[str, Any] | None) -> tuple[_BundleNameRepair, ...]:
    """Truncate overlong bundle-name fields only when their shape is already safe."""
    if rj is None:
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


def validate_triage(
    rj: dict[str, Any] | None, expected_open_ids: set[str] | None
) -> tuple[TriagePlan | None, list[str]]:
    """Deterministic validation of the triage session's result.json. Returns
    (plan, []) or (None, errors). expected_open_ids=None skips the ledger
    equality check (used when reloading a previously validated plan)."""
    errors: list[str] = []
    rj = rj or {}
    _normalize_bundle_names(rj)
    if rj.get("workflow") != TRIAGE_WORKFLOW:
        return None, [f"workflow must be {TRIAGE_WORKFLOW!r}: got {rj.get('workflow')!r}"]

    claimed_open = {str(i) for i in rj.get("open_ids", [])}
    if expected_open_ids is not None and claimed_open != expected_open_ids:
        missed = sorted(expected_open_ids - claimed_open)
        invented = sorted(claimed_open - expected_open_ids)
        return None, [
            "open_ids do not match the ledger's open entries"
            + (f"; missing: {', '.join(missed)}" if missed else "")
            + (f"; not open in the ledger: {', '.join(invented)}" if invented else "")
        ]
    universe = expected_open_ids if expected_open_ids is not None else claimed_open

    seen: dict[str, str] = {}  # id -> category that claimed it

    def claim(dw_id: str, category: str) -> None:
        if dw_id not in universe:
            errors.append(f"{category} references unknown/closed id {dw_id}")
        elif dw_id in seen:
            errors.append(f"{dw_id} appears in both {seen[dw_id]} and {category}")
        else:
            seen[dw_id] = category

    resolved = []
    for item in rj.get("already_resolved", []):
        dw_id = str(item.get("id", ""))
        evidence = str(item.get("evidence", "")).strip()
        claim(dw_id, "already_resolved")
        if not evidence:
            errors.append(f"already_resolved {dw_id} has no evidence")
        resolved.append(ResolvedEntry(dw_id, evidence))

    bundles = []
    names: set[str] = set()
    for item in rj.get("bundles", []):
        name = str(item.get("name", ""))
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
        names.add(name)
        dw_ids = [str(i) for i in item.get("dw_ids", [])]
        if not dw_ids:
            errors.append(f"bundle {name!r} has no dw_ids")
        for dw_id in dw_ids:
            claim(dw_id, f"bundle {name!r}")
        intent = str(item.get("intent", "")).strip()
        if not intent:
            errors.append(f"bundle {name!r} has no intent")
        bundles.append(Bundle(name, tuple(dw_ids), intent))

    blocked = []
    for item in rj.get("blocked", []):
        dw_id = str(item.get("id", ""))
        blocker = str(item.get("blocker", "")).strip()
        claim(dw_id, "blocked")
        if not blocker:
            errors.append(f"blocked {dw_id} names no blocker")
        blocked.append((dw_id, blocker))

    skip = []
    for item in rj.get("skip", []):
        dw_id = str(item.get("id", ""))
        reason = str(item.get("reason", "")).strip()
        claim(dw_id, "skip")
        if not reason:
            errors.append(f"skip {dw_id} gives no reason")
        skip.append((dw_id, reason))

    decisions = []
    for item in rj.get("decisions", []):
        dw_id = str(item.get("id", ""))
        claim(dw_id, "decisions")
        question = str(item.get("question", "")).strip()
        if not question:
            errors.append(f"decision {dw_id} has no question")
        options = []
        keys: set[str] = set()
        decision_bundle_names: set[str] = set()
        for raw in item.get("options", []):
            key = str(raw.get("key", ""))
            effect = str(raw.get("effect", ""))
            intent = str(raw.get("intent", "")).strip()
            if not key or key in keys:
                errors.append(f"decision {dw_id}: missing/duplicate option key {key!r}")
            keys.add(key)
            if effect not in DECISION_EFFECTS:
                errors.append(f"decision {dw_id} option {key}: bad effect {effect!r}")
            if effect == "build" and not intent:
                errors.append(f"decision {dw_id} option {key}: effect 'build' needs intent")
            bundle_name = str(raw.get("bundle_name", ""))
            if bundle_name and not BUNDLE_NAME_RE.match(bundle_name):
                errors.append(f"decision {dw_id} option {key}: bad bundle_name {bundle_name!r}")
            # The second site that mints a bundle directory, gated for the reason
            # stated at the `bundles` loop above. A build-effect option's
            # `bundle_name` becomes `Bundle.name` in `_materialize_bundles`, so it
            # reaches `_write_intent`'s cycle-1 directory by the identical path --
            # `BUNDLE_NAME_RE` is no more able to express the rule here than there.
            # Guarded on the match above so one bad name yields one error, and on
            # nothing else: an absent `bundle_name` fails that match already.
            if BUNDLE_NAME_RE.match(bundle_name) and safe_segment(bundle_name) != bundle_name:
                errors.append(
                    f"decision {dw_id} option {key}: bundle_name {bundle_name!r} "
                    "is not a legal path segment"
                )
            if effect == "build" and bundle_name:
                if bundle_name in names:
                    errors.append(f"duplicate bundle name {bundle_name!r}")
                decision_bundle_names.add(bundle_name)
            options.append(
                DecisionOption(
                    key=key,
                    label=str(raw.get("label", "")).strip() or key,
                    effect=effect,
                    intent=intent,
                    resolution=str(raw.get("resolution", "")).strip(),
                    bundle_name=bundle_name,
                )
            )
        names.update(decision_bundle_names)
        if len(options) < 2:
            errors.append(f"decision {dw_id} needs at least 2 options")
        recommendation = str(item.get("recommendation", ""))
        if recommendation not in keys:
            errors.append(f"decision {dw_id}: recommendation {recommendation!r} not an option")
        decisions.append(
            Decision(
                dw_id,
                question,
                str(item.get("context", "")).strip(),
                tuple(options),
                recommendation,
            )
        )

    unclaimed = sorted(universe - set(seen))
    if unclaimed:
        errors.append(f"open entries not triaged: {', '.join(unclaimed)}")

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


def snapshot_canonical(text: str) -> dict[str, PreCanonical]:
    """The pre-migration state ``validate_migration`` holds the rewrite to.

    A named function rather than a comprehension inlined at its one call site so
    that the tests grade the snapshot production actually builds: a hand-written
    ``{"DW-1": PreCanonical("open", ("3-2",))}`` would pass whatever the parser
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
        snapshot[e.id] = PreCanonical(e.status, g.tokens + g.malformed)
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
    rj = rj or {}
    if rj.get("workflow") != MIGRATE_WORKFLOW:
        return [f"workflow must be {MIGRATE_WORKFLOW!r}: got {rj.get('workflow')!r}"]
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

    pre_max = max((int(i.split("-")[1]) for i in pre_canonical), default=0)
    for dw_id, pre in pre_canonical.items():
        e = entries.get(dw_id)
        if e is None:
            errors.append(f"pre-existing {dw_id} disappeared")
            continue
        if first_word(e.status) != first_word(pre.status):
            errors.append(f"pre-existing {dw_id} status changed: {pre.status!r} -> {e.status!r}")
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
        if int(dw_id.split("-")[1]) <= pre_max:
            errors.append(f"new entry {dw_id} does not continue numbering past DW-{pre_max}")
        if first_word(e.status) not in ("open", "done"):
            errors.append(f"new entry {dw_id} has status {e.status!r}; want open or done")

    manifest_by_key = {str(m["key"]): m for m in manifest}
    mapping = rj.get("mapping", [])
    if not isinstance(mapping, list):
        return errors + ["mapping must be a list of {key, dw_id}"]
    seen_keys: set[str] = set()
    for item in mapping:
        key = str(item.get("key", "")) if isinstance(item, dict) else ""
        dw_id = str(item.get("dw_id", "")) if isinstance(item, dict) else ""
        source = manifest_by_key.get(key)
        if source is None:
            errors.append(f"mapping invents unknown key {key!r}")
            continue
        if key in seen_keys:
            errors.append(f"mapping repeats key {key!r}")
        seen_keys.add(key)
        target = entries.get(dw_id)
        if target is None:
            errors.append(f"mapping {key} -> {dw_id}: no such entry in the ledger")
        elif (first_word(target.status) == "done") != bool(source["done"]):
            want = "done" if source["done"] else "open"
            errors.append(f"mapping {key} -> {dw_id}: manifest says {want}, ledger disagrees")
    missing = sorted(set(manifest_by_key) - seen_keys)
    if missing:
        errors.append("manifest keys not mapped: " + ", ".join(missing))
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
        prompter: DecisionPrompter | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.adapters["triage"] = (
            triage_adapter if triage_adapter is not None else self.adapters["dev"]
        )
        self.prompting = prompting
        self.decisions_only = decisions_only
        self.max_bundles = max_bundles if max_bundles is not None else self.policy.sweep.max_bundles
        self.repeat = repeat if repeat is not None else self.policy.sweep.repeat
        self.max_cycles = max_cycles if max_cycles is not None else self.policy.sweep.max_cycles
        self.prompter = prompter or DecisionPrompter()
        # decisions already journaled as skipped this process; without it a
        # persistent decision item would notify once per repeat cycle
        self._skipped_decisions: set[str] = set()
        self.state.run_type = "sweep"

    def _remaining_estimate(self) -> int | None:
        """Sweep override of the graceful-stop hint: how many deferred-work
        entries are still open in the ledger — the work a resume would pick up.
        Like the base, a hint only: the whole body is guarded so an
        unreadable/invalid ledger returns None rather than derailing the stop."""
        try:
            ledger = self.workspace.paths.deferred_work
            text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
            return len(deferredwork.open_ids(text))
        except Exception:  # a hint must never break the stop
            return None

    # ------------------------------------------------------------ main loop

    def _loop(self) -> None:
        ledger = self.workspace.paths.deferred_work
        cycle = max(1, self.state.sweep_cycle)
        if self._finish_inflight_bundles():
            # a recovered bundle's ledger restore can leave the tree dirty, and
            # triage plus the first bundle baseline need a clean one. Guarded on
            # a non-empty pass so a fresh sweep never commits the user's dirt.
            self._commit_ledger("chore(sweep): commit ledger after recovering in-flight bundles")
        while True:
            # First statement of the loop body: covers the boundary right after
            # _finish_inflight_bundles on resume and between repeat cycles. A
            # request during a cycle is caught before the next _run_bundle (see
            # _cycle); one landing between cycles stops here before cycle N+1
            # re-triages.
            self._check_stop_request()
            self.state.sweep_cycle = cycle
            self._save()
            text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
            if deferredwork.has_legacy(text):
                if cycle > 1:
                    # freeform text appeared mid-run; _ensure_migration assumes
                    # one migration per run, so hand off to a fresh sweep
                    self.journal.append(
                        "sweep-repeat-done", cycles=cycle - 1, reason="legacy-appeared"
                    )
                    gates.notify(
                        self.policy,
                        self.run_dir,
                        "legacy ledger entries appeared mid-sweep",
                        "run a fresh `bmad-loop sweep` to migrate them",
                    )
                    return
                self._ensure_migration(text)
                text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
            open_now = deferredwork.open_ids(text)
            if not open_now:
                if cycle == 1:
                    self.journal.append("sweep-nothing-open", ledger=str(ledger))
                else:
                    self.journal.append("sweep-repeat-done", cycles=cycle - 1, reason="no-open")
                return
            if cycle > 1:
                self.journal.append("sweep-cycle", cycle=cycle, open=len(open_now))
            progressed = self._cycle(cycle, open_now)
            if self.decisions_only or not self.repeat:
                return
            if not progressed:
                self.journal.append("sweep-repeat-done", cycles=cycle, reason="no-progress")
                return
            if cycle >= self.max_cycles:
                self.journal.append("sweep-repeat-done", cycles=cycle, reason="max-cycles")
                return
            # a deferred bundle's ledger restore can leave the tree dirty; the
            # next cycle's triage and bundle baselines need a clean tree
            self._commit_ledger("chore(sweep): commit ledger before next sweep cycle")
            cycle += 1

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
        then drops the fresh plan's overlapping bundle."""
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
            self._ensure_bundle_intent(task)
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
        predicate. Caveat: on crash-resume of a cycle whose only progress was
        already-resolved closes, the replayed (idempotent) closes report 0 and
        the run stops with no-progress; errs toward stopping, never loops."""
        self._emit("pre_sweep_cycle", phase=str(cycle))
        self._warn_stranded_bundles()
        plan = self._ensure_triage(open_now, cycle)
        closed = self._close_resolved(plan)
        answers, decisions_closed = self._decisions_phase(plan)
        bundles = self._materialize_bundles(plan, answers)
        if self.decisions_only:
            self.journal.append("sweep-decisions-only", bundles_not_run=len(bundles))
            self._prune_pre_answers()
            self._emit("post_sweep_cycle", phase=str(cycle))
            return False
        for bundle in bundles:
            # Item boundary: a request during bundle N lets N finish through
            # commit; bundle N+1 never starts. A request landing during triage
            # reaches the first iteration here, so triage completes but zero
            # bundles run. Mid-cycle stop is resume-safe: sweep_cycle is
            # persisted, triage.json is cached, closes are idempotent, and
            # terminal tasks are skipped on re-drive.
            self._check_stop_request()
            self._run_bundle(bundle, cycle)
        bundles_done = sum(
            1
            for b in bundles
            if self.state.tasks[self._bundle_key(b.name, cycle)].phase == Phase.DONE
        )
        self._prune_pre_answers()
        self._emit("post_sweep_cycle", phase=str(cycle))
        return closed > 0 or decisions_closed > 0 or bundles_done > 0

    def _prune_pre_answers(self) -> None:
        """Drop consumed pre-answers — entries built or closed this cycle have
        left the open set. Keeps the store from re-applying a stale answer (and a
        keep-open answer's audit line) on the next sweep."""
        from . import decisions as decisions_store  # lazy: decisions imports sweep

        ledger = self.workspace.paths.deferred_work
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        # The store lives under the project that owns `run_dir`, never
        # `self.workspace.root`: under the `repo_root` override the two diverge
        # and a workspace-rooted prune trimmed a store that does not exist,
        # leaving consumed entries behind (the comment in `_decisions_phase`
        # says why the run dir is the stable anchor). The ledger read above is
        # unaffected — `deferred_work` hangs off `implementation_artifacts`,
        # which stays project-rooted under the override.
        dropped = decisions_store.prune_pre_answers(
            _project_of_run_dir(self.run_dir), deferredwork.open_ids(text)
        )
        if dropped:
            self.journal.append("decision-preanswers-pruned", dw_ids=dropped)
            self._commit_ledger("chore(sweep): drop consumed deferred-work pre-answers")

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

    def _run_bundle(self, bundle: Bundle, cycle: int) -> None:
        key = self._bundle_key(bundle.name, cycle)
        task = self.state.tasks.get(key)
        if task is not None and task.terminal:
            return  # finished (or adjudicated) in a previous resume cycle
        if task is None:
            task = StoryTask(story_key=key, epic=0, dw_ids=list(bundle.dw_ids))
            self.state.tasks[key] = task
            self.journal.append("bundle-start", story_key=key, dw_ids=list(bundle.dw_ids))
        elif self._recover_inflight_bundle(task):
            return
        dirname = bundle.name if cycle == 1 else f"c{cycle}-{bundle.name}"
        task.bundle_file = str(self._write_intent(bundle, dirname))
        self._save()
        self._emit("pre_bundle", task)
        self._run_story(task)
        self._emit("post_bundle", task)

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
        resume-commit arm (#115)."""
        isolated = self._isolated and task.worktree_path
        if task.phase == Phase.COMMITTING:
            # the gate+advance save landed pre-death; finish the commit
            # instead of rolling verified bundle work back (see
            # Engine._finalize_commit_phase for the re-drive contract).
            self.journal.append("resume-commit", story_key=task.story_key)
            if isolated:
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
            if isolated:
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
        if isolated:
            # drop the half-built worktree; _run_story mounts a fresh one
            discard_worktree(
                self.paths.repo_root, task.worktree_path, task.branch, run_dir=self.run_dir
            )
            task.worktree_path = ""
            task.branch = ""
        elif task.baseline_commit:
            # latch resolved_redrive so the corrected spec + restored diff stay
            # protected through every reset of this re-drive, not just this
            # first one; cause="resolved" keeps a human-initiated re-arm
            # pause-free regardless of scm.rollback_on_failure
            task.resolved_redrive = task.resolved_redrive or task.rearmed
            self._rollback_or_pause(task, cause="resolved" if task.rearmed else "stopped")
        task.rearmed = False  # past rollback (only reached when not paused)
        task.phase = Phase.PENDING  # deliberate reset, not a normal transition
        return False

    # ------------------------------------------------------------ migration

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
        elif task.phase != Phase.PENDING:
            # resumed mid-migration or retrying after an escalation: restart
            self.journal.append("resume-restart", story_key=MIGRATE_KEY, phase=str(task.phase))
            if task.phase == Phase.ESCALATED:
                task.attempt = 0  # the human resumed deliberately; fresh budget
            if task.baseline_commit and not verify.worktree_clean(self.workspace.root):
                self._safe_reset(task)  # a session died mid-rewrite; restore our ledger
                text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
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
            task.baseline_commit = verify.rev_parse_head(self.workspace.root)
            task.baseline_untracked = sorted(verify.untracked_files(self.workspace.root))

        legacy = deferredwork.parse_legacy(text)
        pre_canonical = snapshot_canonical(text)
        manifest = [
            {
                "key": e.key,
                "id": e.id,
                "title": e.title,
                "section": e.section,
                "done": e.done,
                "severity": e.severity,
            }
            for e in legacy
        ]
        manifest_path = self.run_dir / "migrate-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        feedback: Path | None = None
        while True:
            task.attempt += 1
            advance(task, Phase.TRIAGE_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="triage",
                prompt=self._migrate_prompt(manifest_path, feedback),
                seq=task.attempt,
                session_stage="pre_migrate_session",
            )
            advance(task, Phase.TRIAGE_VERIFY)
            self._save()
            crits = critical_escalations(result.result_json)
            if crits:
                details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
                self._escalate(task, f"CRITICAL escalation from migration session: {details}")
            new_text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
            if result.status != "completed":
                errors = [session_failure_reason("migration", result)]
            else:
                errors = validate_migration(result.result_json, manifest, pre_canonical, new_text)
            self.journal.append(
                "migrate-decision",
                attempt=task.attempt,
                session_status=result.status,
                ok=not errors,
                errors=errors,
                env_fault=result.env_fault,
            )
            if result.status != "completed" and result.env_fault:
                # The migration session's CLI lost its API connection (#194): it did
                # no rewrite work, so pause (the ESCALATED-resume above resets
                # task.attempt to 0 — fresh budget) instead of charging a migration
                # attempt. Escalate BEFORE the _safe_reset/attempt-cap path; that
                # resume also restores the ledger if the worktree is dirty.
                self._escalate(
                    task,
                    env_fault_pause_reason("migration", result),
                )
            if not errors:
                advance(task, Phase.DONE)
                self._save()
                (self.run_dir / "migrate-result.json").write_text(
                    json.dumps(result.result_json, indent=2), encoding="utf-8"
                )
                self._commit_ledger(
                    "chore(sweep): migrate legacy deferred-work entries to DW format"
                )
                post = deferredwork.parse_ledger(new_text)
                self.journal.append(
                    "sweep-migrated",
                    converted=len(manifest),
                    entries_now=len(post),
                    open_now=sum(1 for e in post if e.open),
                )
                self._emit("post_migrate", task)
                return
            # never re-prompt over a half-broken rewrite; the baseline reset
            # covers tracked files, the explicit write covers an untracked
            # ledger that `git reset` cannot restore
            self._safe_reset(task)
            ledger.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(ledger, text)
            if task.attempt >= self.policy.sweep.max_migration_attempts:
                self._escalate(
                    task, "migration failed deterministic validation: " + "; ".join(errors)
                )
            feedback = self._write_feedback(
                task,
                "The legacy-ledger migration failed deterministic validation:\n- "
                + "\n- ".join(errors),
            )

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
        if triage_path.is_file():
            # already validated this run; the ledger has moved since (closes,
            # decisions), so skip the open-set equality re-check. A cache we
            # cannot read or that is not a JSON object degrades to a fresh
            # triage — a truncated file must not crash the whole run.
            try:
                cached = _read_json(triage_path)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                self.journal.append("sweep-triage-reload-failed", errors=[f"unreadable: {exc}"])
            else:
                if isinstance(cached, dict):
                    plan, errors = validate_triage(cached, None)
                else:
                    plan, errors = None, [f"not a JSON object: {type(cached).__name__}"]
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
            if task.phase == Phase.ESCALATED:
                task.attempt = 0  # the human resumed deliberately; fresh budget
            task.phase = Phase.PENDING  # deliberate reset, not a normal transition

        feedback: Path | None = None
        while True:
            task.attempt += 1
            advance(task, Phase.TRIAGE_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="triage",
                prompt=self._triage_prompt(feedback),
                seq=task.attempt,
            )
            advance(task, Phase.TRIAGE_VERIFY)
            self._save()
            crits = critical_escalations(result.result_json)
            if crits:
                details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
                self._escalate(task, f"CRITICAL escalation from triage session: {details}")
            if result.status != "completed":
                plan, errors = None, [session_failure_reason("triage", result)]
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
            )
            if result.status != "completed" and result.env_fault:
                # transport/API failure (#194): pause rather than charge a triage
                # attempt for a session that never reached the API. The
                # ESCALATED-resume above resets task.attempt to 0 (fresh budget).
                self._escalate(
                    task,
                    env_fault_pause_reason("triage", result),
                )
            if plan is not None:
                advance(task, Phase.DONE)
                self._save()
                triage_path.write_text(json.dumps(result.result_json, indent=2), encoding="utf-8")
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
                self._escalate(task, "triage output failed validation: " + "; ".join(errors))
            feedback = self._write_feedback(
                task,
                "The triage result.json failed deterministic validation:\n- " + "\n- ".join(errors),
            )

    def _triage_prompt(self, feedback: Path | None) -> str:
        prompt = "/bmad-loop-sweep"
        if feedback is not None:
            prompt += f" --feedback {feedback}"
        return prompt

    # ------------------------------------------------------ ledger phases

    def _close_resolved(self, plan: TriagePlan) -> int:
        self._emit("pre_close_resolved")
        ledger = self.workspace.paths.deferred_work
        closed = []
        for entry in plan.already_resolved:
            if deferredwork.mark_done(
                ledger, entry.id, self._today(), f"already resolved: {entry.evidence}"
            ):
                closed.append(entry.id)
        if closed:
            self.journal.append("sweep-resolved-closed", dw_ids=closed)
        self._commit_ledger("chore(sweep): close resolved deferred-work entries")
        self._emit("post_close_resolved")
        return len(closed)

    def _decisions_phase(self, plan: TriagePlan) -> tuple[dict[str, dict[str, str]], int]:
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
        answers: dict[str, dict[str, str]] = (
            _read_json(decisions_path) if decisions_path.is_file() else {}
        )
        closed = 0
        # Adopt out-of-band pre-answers (a human answered decisions an earlier
        # unattended/abandoned sweep left). The ledger edits were already applied
        # when they answered, so here we only take the answer onboard — this run
        # won't re-prompt/re-skip and build answers materialize into bundles.
        pre = decisions_store.load_pre_answers(project_root)
        seeded = False
        for decision in plan.decisions:
            if decision.id in answers or decision.id not in pre:
                continue
            answers[decision.id] = pre[decision.id]
            self.journal.append(
                "decision-preanswered",
                dw_id=decision.id,
                effect=pre[decision.id].get("effect"),
            )
            seeded = True
        if seeded:
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
            atomic_write_text_confined(
                decisions_path,
                json.dumps(answers, indent=2),
                confine_root=project_root,
            )
        pending = [d for d in plan.decisions if d.id not in answers]
        answered_interactively = False
        if not self.prompting:
            pending = [d for d in pending if d.id not in self._skipped_decisions]
            for decision in pending:
                self.journal.append("decision-skipped-unattended", dw_id=decision.id)
                self._skipped_decisions.add(decision.id)
            if pending:
                gates.notify(
                    self.policy,
                    self.run_dir,
                    f"{len(pending)} deferred-work decisions pending",
                    "run `bmad-loop sweep` interactively to answer them",
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
                answers[decision.id] = {
                    "key": option.key,
                    "label": option.label,
                    "effect": option.effect,
                    "answered_at": self._today(),
                }
                atomic_write_text_confined(  # same file, same reasoning as above (#363, #593)
                    decisions_path,
                    json.dumps(answers, indent=2),
                    confine_root=project_root,
                )
                self.journal.append(
                    "decision-answered",
                    dw_id=decision.id,
                    key=option.key,
                    effect=option.effect,
                )
                self._apply_decision_effect(decision, option)
                self._emit("post_decision", story_key=decision.id, decision_action=option.effect)
                answered_interactively = True
                if option.effect == "close":
                    closed += 1
        self._commit_ledger("chore(sweep): record deferred-work decisions")
        if answered_interactively:
            self._return_after_decisions()
        return answers, closed

    def _return_after_decisions(self) -> None:
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
        read it."""
        from .tui import launch  # import-light: launch.py has no textual imports

        outcome = launch.return_attached_client()
        if outcome is launch.ReturnOutcome.ATTENDED:
            return
        self.prompting = False
        if outcome is launch.ReturnOutcome.RETURNED:
            self.journal.append("sweep-returned-after-decisions")
            self.prompter.print_fn("✓ decisions recorded — sweep continues in the background")
        else:
            self.journal.append("sweep-return-no-client")

    def _apply_decision_effect(self, decision: Decision, option: DecisionOption) -> None:
        ledger = self.workspace.paths.deferred_work
        detail = option.resolution or option.intent
        deferredwork.append_decision(ledger, decision.id, self._today(), option.label, detail)
        if option.effect == "close":
            note = "closed by human decision" + (
                f": {option.resolution}" if option.resolution else ""
            )
            deferredwork.mark_done(ledger, decision.id, self._today(), note)

    def _commit_ledger(self, message: str) -> None:
        """Commit pending orchestrator ledger edits; bundles need a clean
        baseline. No-op when the tree is already clean."""
        if verify.worktree_clean(self.workspace.root):
            return
        sha = verify.commit_story(self.workspace.root, message)
        self.journal.append("sweep-ledger-commit", message=message, commit=sha)

    # ---------------------------------------------------------- bundles

    def _materialize_bundles(
        self, plan: TriagePlan, answers: dict[str, dict[str, str]]
    ) -> list[Bundle]:
        self._emit("pre_materialize_bundles")
        bundles = list(plan.bundles)
        for decision in plan.decisions:
            answer = answers.get(decision.id)
            if not answer or answer.get("effect") != "build":
                continue
            # An in-run answer maps cleanly to a current option; a pre-answer
            # (answered out of band against an earlier triage) may not — a fresh
            # triage can renumber options — so fall back to the stored option
            # semantics carried in the answer itself.
            option = decision.option(str(answer.get("key")))
            intent = (option.intent if option else "") or str(answer.get("intent", ""))
            if not intent:
                continue
            label = (option.label if option else "") or str(answer.get("label", "")) or "build"
            bundle_name = (option.bundle_name if option else "") or str(
                answer.get("bundle_name", "")
            )
            # A pre-answer's bundle_name never passed `validate_triage` — it was
            # answered out of band against an earlier triage, and a fresh one can
            # renumber or drop the option it named — so this fallback lane was the
            # one route by which a name failing the two option-site gates (#637)
            # still reached `_write_intent` as a directory. Gate it with the same
            # two rules, but by DISCARD rather than by error: the human's build
            # decision is the payload and `decision-<id>` below is the always-legal
            # name it falls back to anyway, so the discard is journaled the way
            # `_normalize_bundle_names`'s repairs are and the sweep proceeds.
            if bundle_name and (
                not BUNDLE_NAME_RE.match(bundle_name) or safe_segment(bundle_name) != bundle_name
            ):
                self.journal.append(
                    "sweep-bundle-name-discarded",
                    decision=decision.id,
                    original=bundle_name,
                )
                bundle_name = ""
            key = (option.key if option else "") or str(answer.get("key", "")) or "?"
            name = bundle_name or "decision-" + decision.id.lower()
            bundles.append(
                Bundle(
                    name=name,
                    dw_ids=(decision.id,),
                    intent=intent,
                    decision_note=(
                        f"The human chose option {key} ({label}) for the "
                        f"question: {decision.question}"
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
        # override that answer (bundle dev sessions mark their dw_ids done)
        keep_open_ids = {dw_id for dw_id, a in answers.items() if a.get("effect") == "keep-open"}
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
            kept.append(b)
        bundles = kept
        if len(bundles) > self.max_bundles:
            dropped = [b.name for b in bundles[self.max_bundles :]]
            self.journal.append("sweep-bundles-truncated", dropped=dropped)
            bundles = bundles[: self.max_bundles]
        self._emit("post_materialize_bundles")
        return bundles

    def _write_intent(self, bundle: Bundle, dirname: str) -> Path:
        ledger = self.workspace.paths.deferred_work
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        entries = {e.id: e for e in deferredwork.parse_ledger(text)}
        blocks = [entries[i].body.rstrip() for i in bundle.dw_ids if i in entries]
        lines = [
            f"# Deferred-work bundle: {bundle.name}",
            "",
            f"bundle_name: {bundle.name}",
            f"dw_ids: {', '.join(bundle.dw_ids)}",
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
        atomic_write_text(path, neutralize_surrogates("\n".join(lines)))
        return path

    def _ensure_bundle_intent(self, task: StoryTask) -> None:
        """Guarantee a recovered bundle has the intent file its dev prompt points
        at. The rendered intent.md persists in the run dir and the prompt consumes
        nothing else from the plan, so the normal case is to reuse it untouched.

        Only when it is gone do we rebuild a degraded one from the task itself.
        The triage session's authored intent prose is the single unrecoverable
        piece; the verbatim ledger entries _write_intent re-attaches carry the
        actual work, so say plainly that they are now the contract."""
        if task.bundle_file and Path(task.bundle_file).is_file():
            return
        match = BUNDLE_KEY_RE.match(task.story_key)
        if match is None:  # pragma: no cover - callers filter on BUNDLE_KEY_RE
            return
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
        task.bundle_file = str(self._write_intent(bundle, dirname))
        self.journal.append(
            "sweep-intent-regenerated",
            story_key=task.story_key,
            dw_ids=list(task.dw_ids),
            path=task.bundle_file,
        )

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
        if feedback is None:
            if task.restore_patch and task.spec_file:
                return (
                    f"/{self._dev_skill()} Resume review of the in-review spec at "
                    f"`{task.spec_file}` for the deferred-work bundle `{bundle_ref}`. "
                    f"The attempted change was restored onto the working tree after "
                    f"an intent-gap resolution; review it against the amended spec. "
                    f"Do NOT edit the deferred-work ledger; the orchestrator records "
                    f"resolution."
                )
            return (
                f"/{self._dev_skill()} Implement the deferred-work bundle described in "
                f"`{bundle_ref}` — it carries the intent and the verbatim ledger "
                f"entries to resolve. Do NOT edit the deferred-work ledger; the "
                f"orchestrator records resolution."
            )
        self._reset_spec_for_repair(task)
        spec_ref = task.spec_file or bundle_ref
        return (
            f"/{self._dev_skill()} Resume the autonomous dev session on the in-progress "
            f"spec at `{spec_ref}` for the deferred-work bundle `{bundle_ref}`. The "
            f"previous session's work failed deterministic verification; repair the "
            f"working tree so verification passes without changing the frozen intent "
            f"contract or editing the deferred-work ledger. Verification evidence is "
            f"in `{feedback}`."
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
        spec_file = (result_json or {}).get("spec_file")
        if not spec_file:
            return
        success_status = "in-review" if self._dev_review_enabled() else "done"
        self._close_bundle_ledger_when_spec_status(task, str(spec_file), success_status)

    def _bundle_close_operation_id(self, task: StoryTask) -> str:
        """Stable identity for a close and its possible defer-time undo."""
        return f"{self.state.run_id}/{task.story_key}"

    def _bundle_close_note(self, task: StoryTask) -> str:
        """Resolution note shared by a bundle close and its possible undo."""
        return f"resolved by sweep bundle {task.story_key}"

    def _close_declared_deferred(
        self, task: StoryTask, snapshot: list[tuple[Path, str]] | None = None
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
    ) -> None:
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
        marked = deferredwork.mark_done_many_reopenable(
            ledger,
            task.dw_ids,
            self._today(),
            note,
            self._bundle_close_operation_id(task),
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
        reopened = [i for i in task.dw_ids if deferredwork.mark_open(ledger, i, note, operation_id)]
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
        """
        super()._carry_isolated_ledger_writes(task)
        if not task.bundle_closes_intended:
            return
        ledger = self.paths.deferred_work
        carried = deferredwork.mark_done_many_reopenable(
            ledger,
            task.bundle_closes_intended,
            self._today(),
            self._bundle_close_note(task),
            self._bundle_close_operation_id(task),
        )
        if carried:
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

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        return verify.verify_dev_bundle(
            task,
            self.workspace.paths,
            result_json,
            review_enabled=self._dev_review_enabled(),
            engine_written=self._harvest_gate_exclude(task),
        )

    def _verify_review(self, task: StoryTask):
        # Generic bundle dev sessions are told not to edit deferred-work.md; the
        # orchestrator is the ledger writer. A follow-up review can rewrite the
        # ledger from its own snapshot and re-open entries that were already
        # closed after dev. Re-apply that idempotent closure immediately before
        # verify_review_bundle requires those entries. The distinct journal kind
        # makes "a review rewrote the ledger" greppable when diagnosing runs.
        if self._generic_dev() and task.spec_file:
            self._close_bundle_ledger_when_spec_status(
                task, task.spec_file, "done", kind="sweep-bundle-reclosed"
            )
        return verify.verify_review_bundle(task, self.workspace.paths, self.policy)

    def _operator_park_enabled(self) -> bool:
        # A bundle carries no sprint-status entry, so the pair a park is verified
        # against does not exist, and `verify_review_bundle` gates on closed dw
        # ids instead. Whether a deferred-work bundle can owe a human action is a
        # separate question from whether a story can; not answered here.
        return False

    def _commit_message(self, task: StoryTask) -> str:
        rendered = self._render_commit_template(task)
        if rendered is not None:
            return rendered
        return f"sweep {task.story_key}: {', '.join(task.dw_ids)} via bmad-loop"
