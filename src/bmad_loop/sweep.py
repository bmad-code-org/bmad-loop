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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import deferredwork, gates, verify
from .engine import Engine
from .escalation import critical_escalations
from .model import Phase, StoryTask
from .statemachine import advance
from .workspace import discard_worktree


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


TRIAGE_KEY = "sweep-triage"
TRIAGE_WORKFLOW = "deferred-sweep-triage"
MIGRATE_KEY = "sweep-migrate"
MIGRATE_WORKFLOW = "deferred-sweep-migrate"
BUNDLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
DECISION_EFFECTS = ("build", "close", "keep-open")


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


def validate_migration(
    rj: dict[str, Any] | None,
    manifest: list[dict[str, Any]],
    pre_canonical: dict[str, str],
    new_text: str,
) -> list[str]:
    """Deterministic validation of a legacy-ledger migration session: the
    rewritten ledger must contain zero legacy items, preserve every
    pre-existing canonical entry's status, continue DW numbering, and the
    result.json mapping must cover the manifest exactly. Returns errors,
    empty on success."""
    rj = rj or {}
    if rj.get("workflow") != MIGRATE_WORKFLOW:
        return [f"workflow must be {MIGRATE_WORKFLOW!r}: got {rj.get('workflow')!r}"]
    errors: list[str] = []

    leftovers = deferredwork.parse_legacy(new_text)
    if leftovers:
        listed = "; ".join(f"{e.section or 'top level'}: {e.title[:60]}" for e in leftovers[:10])
        errors.append(f"{len(leftovers)} legacy item(s) still parse as legacy: {listed}")

    entries: dict[str, deferredwork.DWEntry] = {}
    dupes: set[str] = set()
    for e in deferredwork.parse_ledger(new_text):
        if e.id in entries:
            dupes.add(e.id)
        entries[e.id] = e
    if dupes:
        errors.append("duplicate DW ids: " + ", ".join(sorted(dupes)))

    def first_word(status: str) -> str:
        return status.split()[0] if status.split() else ""

    pre_max = max((int(i.split("-")[1]) for i in pre_canonical), default=0)
    for dw_id, status in pre_canonical.items():
        e = entries.get(dw_id)
        if e is None:
            errors.append(f"pre-existing {dw_id} disappeared")
        elif first_word(e.status) != first_word(status):
            errors.append(f"pre-existing {dw_id} status changed: {status!r} -> {e.status!r}")
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

    # the date stamped into ledger edits; isolated for tests
    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    # ------------------------------------------------------------ main loop

    def _loop(self) -> None:
        ledger = self.workspace.paths.deferred_work
        cycle = max(1, self.state.sweep_cycle)
        while True:
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

    def _cycle(self, cycle: int, open_now: set[str]) -> bool:
        """One triage -> close -> decide -> bundle pass. Returns whether the
        cycle completed any addressable work — the repeat loop's progress
        predicate. Caveat: on crash-resume of a cycle whose only progress was
        already-resolved closes, the replayed (idempotent) closes report 0 and
        the run stops with no-progress; errs toward stopping, never loops."""
        self._emit("pre_sweep_cycle", phase=str(cycle))
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
        dropped = decisions_store.prune_pre_answers(
            self.workspace.root, deferredwork.open_ids(text)
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
        else:
            # interrupted mid-bundle: same recovery as Engine._finish_inflight
            self.journal.append("resume-restart", story_key=key, phase=str(task.phase))
            isolated = self._isolated and task.worktree_path
            if task.phase == Phase.DEV_VERIFY and task.spec_file:
                self._save()
                if isolated:
                    unit = self._reopen_unit(task)
                    prev = self.workspace
                    self.workspace = unit.workspace
                    try:
                        self._review_and_commit(task)
                    finally:
                        self.workspace = prev
                    self._integrate_unit(task, unit)
                else:
                    self._review_and_commit(task)
                return
            if isolated:
                # drop the half-built worktree; _run_story mounts a fresh one
                discard_worktree(self.paths.repo_root, task.worktree_path, task.branch)
                task.worktree_path = ""
                task.branch = ""
            elif task.baseline_commit:
                self._rollback_or_pause(task)
            task.phase = Phase.PENDING  # deliberate reset, not a normal transition
        dirname = bundle.name if cycle == 1 else f"c{cycle}-{bundle.name}"
        task.bundle_file = str(self._write_intent(bundle, dirname))
        self._save()
        self._emit("pre_bundle", task)
        self._run_story(task)
        self._emit("post_bundle", task)

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
        if not task.baseline_commit:
            task.baseline_commit = verify.rev_parse_head(self.workspace.root)
            task.baseline_untracked = sorted(verify.untracked_files(self.workspace.root))

        legacy = deferredwork.parse_legacy(text)
        pre_canonical = {e.id: e.status for e in deferredwork.parse_ledger(text)}
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
                errors = [f"migration session {result.status}"]
            else:
                errors = validate_migration(result.result_json, manifest, pre_canonical, new_text)
            self.journal.append(
                "migrate-decision",
                attempt=task.attempt,
                session_status=result.status,
                ok=not errors,
                errors=errors,
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
            ledger.write_text(text, encoding="utf-8")
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
            # decisions), so skip the open-set equality re-check
            plan, errors = validate_triage(_read_json(triage_path), None)
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
                plan, errors = None, [f"triage session {result.status}"]
            else:
                plan, errors = validate_triage(result.result_json, open_now)
            self.journal.append(
                "triage-decision",
                attempt=task.attempt,
                session_status=result.status,
                ok=plan is not None,
                errors=errors,
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
        answers: dict[str, dict[str, str]] = (
            _read_json(decisions_path) if decisions_path.is_file() else {}
        )
        closed = 0
        # Adopt out-of-band pre-answers (a human answered decisions an earlier
        # unattended/abandoned sweep left). The ledger edits were already applied
        # when they answered, so here we only take the answer onboard — this run
        # won't re-prompt/re-skip and build answers materialize into bundles.
        pre = decisions_store.load_pre_answers(self.workspace.root)
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
            tmp = decisions_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(answers, indent=2), encoding="utf-8")
            tmp.replace(decisions_path)
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
                tmp = decisions_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(answers, indent=2), encoding="utf-8")
                tmp.replace(decisions_path)
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

        After a successful return we go unattended for the rest of the run: a
        later --repeat cycle's input() would otherwise block forever in a window
        no one is viewing. New decisions then defer via the unattended path,
        recorded for `bmad-loop decisions` or the next attended sweep."""
        from .tui import launch  # import-light: launch.py has no textual imports

        if launch.return_attached_client():
            self.journal.append("sweep-returned-after-decisions")
            self.prompter.print_fn("✓ decisions recorded — sweep continues in the background")
            self.prompting = False

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
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    # ------------------------------------------------------ override seams

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        return self._generic_bundle_prompt(task, feedback)

    def _generic_bundle_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        """Bundle invocation for the generic bmad-dev-auto dev skill: the self-contained
        intent.md (intent + verbatim ledger entries) is handed over as freeform
        intent. The orchestrator owns the deferred-work ledger — the skill is told
        not to edit it — and records resolution itself in `_post_dev_state_sync`.
        On a repair the bundle spec is re-opened first (B6) so step-01 resumes."""
        bundle_ref = task.bundle_file or task.story_key
        if feedback is None:
            return (
                f"/bmad-dev-auto Implement the deferred-work bundle described in "
                f"`{bundle_ref}` — it carries the intent and the verbatim ledger "
                f"entries to resolve. Do NOT edit the deferred-work ledger; the "
                f"orchestrator records resolution."
            )
        self._reset_spec_for_repair(task)
        spec_ref = task.spec_file or bundle_ref
        return (
            f"/bmad-dev-auto Resume the autonomous dev session on the in-progress "
            f"spec at `{spec_ref}` for the deferred-work bundle `{bundle_ref}`. The "
            f"previous session's work failed deterministic verification; repair the "
            f"working tree so verification passes without changing the frozen intent "
            f"contract or editing the deferred-work ledger. Verification evidence is "
            f"in `{feedback}`."
        )

    def _post_dev_state_sync(self, task: StoryTask, result_json: dict | None) -> None:
        """Generic-path ledger single-writer for bundles. The decoupled
        bmad-dev-auto skill does not touch the ledger, so the orchestrator marks
        each dw id the bundle owns ``done`` once the bundle's spec reaches the
        terminal status for the current stage. Mirrors the story sprint sync;
        no-op on the legacy path."""
        if not self._generic_dev():
            return
        spec_file = (result_json or {}).get("spec_file")
        if not spec_file:
            return
        success_status = "in-review" if self._dev_review_enabled() else "done"
        self._close_bundle_ledger_when_spec_status(task, str(spec_file), success_status)

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
        if verify.status_of(verify.read_frontmatter(spec_path)) != success_status:
            return
        ledger = self.workspace.paths.deferred_work
        note = f"resolved by sweep bundle {task.story_key}"
        marked = [i for i in task.dw_ids if deferredwork.mark_done(ledger, i, self._today(), note)]
        if marked:
            self.journal.append(kind, story_key=task.story_key, dw_ids=marked)

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        return verify.verify_dev_bundle(
            task, self.workspace.paths, result_json, review_enabled=self._dev_review_enabled()
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

    def _commit_message(self, task: StoryTask) -> str:
        rendered = self._render_commit_template(task)
        if rendered is not None:
            return rendered
        return f"sweep {task.story_key}: {', '.join(task.dw_ids)} via bmad-loop"
