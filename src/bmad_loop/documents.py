"""The library-level read-model projection layer for the ``--json`` contract.

Domain object in, contract document dict out. Every builder here is a pure
projection — it reads an already-loaded domain object (a ``ValidationReport``, a
``RunState``, a list of ``Decision`` / ``RunInfo``) and returns the plain dict
that :mod:`bmad_loop.machine` serializes. No I/O, no process state, no printing,
no exit codes: the caller loads, this layer projects, ``machine.emit`` writes.

Each command owns its own ``*_SCHEMA_VERSION`` constant and obeys the pure-document
contract documented in :mod:`bmad_loop.machine` — one JSON object, additive-only
evolution, anything breaking bumps that command's version. Those constants live
here, next to the builders they version, because a field and its version bump are
one edit.

This mirrors the split :mod:`bmad_loop.probe` and :mod:`bmad_loop.diagnostics`
already make — one finding, two render targets — generalized to the commands whose
document is a dict rather than a rendered string. The point of the separation is
that the contract is not a CLI feature: a future non-CLI frontend (the planned web
backend) imports these builders directly and serializes them itself, never
shelling out to the CLI to parse its stdout. Keeping them in ``cli.py`` made the
library surface reachable only through ``argparse``, and made every new command's
document another few hundred lines of accretion in the dispatch module.

So: add a new command's builder here, not in ``cli.py``. ``cli.py`` re-imports
these names, which is what keeps ``cli.STATUS_SCHEMA_VERSION`` and friends
resolving for existing callers and tests.

Every name in this module is public, and a new builder must be too: the
projection surface *is* the API, so a leading underscore would say "do not
import this" about the one thing the module exists to be imported for. The
builders carried one until #212 only because they were born private inside
``cli.py``. ``run_token_totals`` is public for the same reason even though it
projects no document of its own — ``cmd_status`` calls it across the module
boundary to render text, and a private name imported by another module is the
contradiction this module is meant not to have. Keep genuinely intra-module
helpers private, as :mod:`bmad_loop.probe` and :mod:`bmad_loop.diagnostics` do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import policy, runs

if TYPE_CHECKING:
    from .checks import ValidationReport
    from .model import RunState
    from .operatoractions import ParkedStory
    from .sweep import Decision
    from .tui.data import RunInfo


VALIDATE_SCHEMA_VERSION = 1


def validate_document(
    report: ValidationReport, stories_on: bool, spec_folder: str
) -> dict[str, object]:
    """The `validate --json` document: the verdict plus every check that produced it.

    Obeys the pure-document contract in machine.py (additive-only evolution;
    anything breaking bumps VALIDATE_SCHEMA_VERSION). ``ok`` is true when no
    finding has severity ``problem`` — warnings do not clear it — so it mirrors
    the exit code exactly. This is the first ``--json`` command that emits a whole
    document at rc 1: the nonzero code is the verdict being reported, not a
    failure to produce one (see machine.py on parsing non-empty stdout whatever
    the exit code).

    Three things a consumer has to know:

    - **``message`` is not contracted.** Several problems are a bare ``str(e)``
      from the config, policy, profile and sprint-status exceptions, so their
      wording moves with those modules. ``check`` is the only matchable identity;
      match on it, and read ``message``/``detail`` for humans.
    - **Absence is not a pass.** The gates are chained: if policy fails to load,
      ``profiles`` is empty and the binary, hook and base-skill gates contribute
      no finding at all. A check id missing from ``findings`` means "did not run",
      never "passed" — check ``ok`` for the verdict, not the absence of an id.
    - **``mux.backends-detected`` is gated on more than one registered backend**,
      so a lone-tmux host carries no backend inventory. Same rule as above.

    ``findings`` stays flat and in emission order rather than grouped by severity:
    grouping would destroy the cross-severity ordering (the order the gates ran)
    and turn "every non-ok finding" into a two-array concatenation.
    """
    return {
        "schema_version": VALIDATE_SCHEMA_VERSION,
        "ok": report.passed,
        "mode": "stories" if stories_on else "sprint",
        # "" rather than null in sprint mode, where a spec folder is inapplicable —
        # the same convention as list_document's paused_stage.
        "spec_folder": spec_folder if stories_on else "",
        "counts": report.counts(),
        "findings": [
            {
                "check": f.check,
                "severity": f.severity,
                "message": f.message,
                "detail": f.detail,
            }
            for f in report.findings
        ],
    }


CONFIRM_SCHEMA_VERSION = 1


def confirm_document(parked: list[ParkedStory]) -> dict[str, object]:
    """The `confirm --list --json` document: every story parked at
    `awaiting-operator`, in story-key order, with what each owes.

    Obeys the pure-document contract in machine.py (additive-only evolution;
    anything breaking bumps CONFIRM_SCHEMA_VERSION). Nothing parked is a valid
    empty document with exit 0, never an error.

    Carries BOTH `confirmable` and the raw `spec_status`/`board_status` it is
    derived from. A consumer scripting confirmations only needs the boolean, but
    one triaging a stuck board needs to see which side disagreed — collapsing
    them would make the document say a story "cannot be confirmed" without ever
    saying what is wrong with it, and `drift` is prose, not something to branch
    on. `actions` is the SPEC's list where the spec is readable, matching what
    the text listing shows and what a human would be acknowledging.

    `resumable` marks a confirmation interrupted between its spec writes and its
    board write — a story `confirm` finishes rather than refuses, even though
    `confirmable` is False and `drift` reads like ordinary staleness. It ships
    with the raw `confirmation_recorded` it is derived from, for the same reason
    `confirmable` ships with the two statuses: a consumer triaging a stuck board
    has to be able to see WHICH reading made the boolean what it is.

    CONFIRM_SCHEMA_VERSION deliberately stays 1. Evolution here is additive-only
    (machine.py), new fields may appear, and no field has changed presence or
    type. One value semantic did move with #356, stated honestly: `commit` is now
    DERIVED from the park record's git history rather than stored at park time
    (the committed record cannot contain its own sha), so it can be `""` for a
    record not yet in any commit — a consumer that pinned "always 40 hex chars"
    must treat empty as "no provenance", which the field always could express."""
    return {
        "schema_version": CONFIRM_SCHEMA_VERSION,
        "parked": [
            {
                "story_key": p.story_key,
                "actions": list(p.actions),
                "spec_file": str(p.spec_path) if p.spec_path is not None else None,
                "spec_status": p.spec_status,
                "board_status": p.board_status,
                "commit": p.commit,
                "run_id": p.run_id,
                "parked_at": p.parked_at,
                "confirmable": p.confirmable,
                "confirmation_recorded": p.confirmation_recorded,
                "resumable": p.resumable,
                "drift": p.drift(),
            }
            for p in parked
        ],
    }


DECISIONS_SCHEMA_VERSION = 1


def decisions_document(pending: list[Decision]) -> dict[str, object]:
    """The `decisions --json` document: every pending decision, in DW order.

    Obeys the pure-document contract in machine.py (additive-only evolution;
    anything breaking bumps DECISIONS_SCHEMA_VERSION). A pure projection of
    pending_missed_decisions(), and a lossless one — unlike the `--list` text,
    which drops `context` outright and shows only key/label/effect of each
    option, hiding the `intent`/`resolution`/`bundle_name` that decide what a
    sweep actually builds or writes. A caller answering by policy needs those,
    so the document carries the whole dataclass.

    `recommended` is the derived form of the decision's `recommendation` key,
    so a consumer never has to cross-reference two fields (the text encodes it
    as a "(recommended)" suffix on a free-text line). Exactly one option
    carries it when the recommendation names a real key. Nothing pending is a
    valid empty document with exit 0, never an error.
    """
    return {
        "schema_version": DECISIONS_SCHEMA_VERSION,
        "decisions": [
            {
                "id": d.id,
                "question": d.question,
                "context": d.context,
                "recommendation": d.recommendation,
                "options": [
                    {
                        "key": opt.key,
                        "label": opt.label,
                        "effect": opt.effect,
                        "intent": opt.intent,
                        "resolution": opt.resolution,
                        "bundle_name": opt.bundle_name,
                        "recommended": opt.key == d.recommendation,
                    }
                    for opt in d.options
                ],
            }
            for d in pending
        ],
    }


STATUS_SCHEMA_VERSION = 1


def run_token_totals(state: RunState) -> tuple[int, int, float]:
    """Run-level token totals as ``(raw, weighted, weight)``.

    The weight is the run's persisted snapshot — never live policy — so the
    figures match what the run actually enforced; the TUI and the run summary
    agree (see Engine.summary). Weighted is the sum of per-task weighted
    totals (sum-of-rounds), never a weighted_total of the summed counters:
    two tasks of 101 cache reads at weight 0.5 weigh 50 + 50 = 100, not
    round(202 * 0.5) = 101.
    """
    weight = state.cache_read_weight()
    raw = sum(t.tokens.total for t in state.tasks.values())
    weighted = sum(t.tokens.weighted_total(weight) for t in state.tasks.values())
    return raw, weighted, weight


def status_document(state: RunState, *, graceful_stop_pending: bool = False) -> dict[str, object]:
    """The `status --json` document: the stable machine-readable contract.

    Obeys the pure-document contract in machine.py (additive-only evolution;
    anything breaking bumps STATUS_SCHEMA_VERSION), unlike the human-readable
    status text, which is best-effort and free to change. Everything here is
    derived from state.json alone — never from live policy or other project
    files — so a consumer can reproduce the document, and the weight matches
    what the run actually enforced (see run_token_totals). The one exception is
    ``graceful_stop_pending``: liveness plus the presence of the control file is
    not in state.json, so the caller supplies it (default False keeps the
    builder a pure projection); ``status`` itself is unaffected.

    Two adapter-identity keys (#153 phase 3), both derived from the snapshot and
    the recorded sessions — never live policy — and deliberately named apart:

    - Run-level ``adapters`` is the *configured-resolved* identity: the
      dev/review/triage adapter the run's ``policy_snapshot`` resolves to, via
      :func:`policy.adapter_policy_from_snapshot` + ``AdapterPolicy.resolved`` so
      the stage-inheritance rules are the canonical ones, not re-derived here.
      ``None`` when the snapshot carries no rebuildable ``[adapter]`` block (a run
      that predates adapter stamping) — distinct from an all-``claude`` default.
    - Per-task ``adapters_used`` is the *actually-recorded* identity: the last
      stamped :class:`~bmad_loop.model.SessionRecord` per role in the task's
      sessions, ``{role: {"name", "model", "binary"}}``. ``{}`` for a task whose
      sessions predate stamping (``adapter == ""``). Configured need not equal used — a
      policy edited mid-run, or a role that never ran, diverge.
    """
    raw_total, weighted_total, weight = run_token_totals(state)
    adapter_policy = policy.adapter_policy_from_snapshot(state.policy_snapshot)
    adapters: dict[str, dict[str, str]] | None = None
    if adapter_policy is not None:
        adapters = {}
        for role in ("dev", "review", "triage"):
            resolved = adapter_policy.resolved(role)
            # `binary` is additive (#395): "" means the profile's own executable,
            # which is also what every run predating the field reports — so a
            # consumer that ignores the key reads exactly what it read before.
            adapters[role] = {
                "name": resolved.name,
                "model": resolved.model,
                "binary": resolved.binary,
            }
    if state.finished:
        status = "finished"
    elif state.paused:
        status = "paused"
    elif state.crashed:
        status = "crashed"
    elif state.stopped:
        status = "stopped"
    else:
        status = "in-progress"
    tasks: list[dict[str, object]] = []
    for key, task in state.tasks.items():
        tokens = task.tokens.to_dict()
        tokens["raw"] = task.tokens.total
        tokens["weighted"] = task.tokens.weighted_total(weight)
        # Last stamped session per role wins (iteration is chronological); a role
        # whose only sessions predate stamping (adapter "") contributes nothing.
        adapters_used: dict[str, dict[str, str]] = {}
        for record in task.sessions:
            if record.adapter:
                adapters_used[record.role] = {
                    "name": record.adapter,
                    "model": record.model,
                    "binary": record.binary,
                }
        tasks.append(
            {
                "story_key": key,
                "epic": task.epic,
                "phase": str(task.phase),
                "attempt": task.attempt,
                "review_cycle": task.review_cycle,
                "tokens": tokens,
                "commit_sha": task.commit_sha,
                "defer_reason": task.defer_reason,
                # the recovery ref a rolled-back attempt's work was parked on, or
                # null. Reported verbatim from state.json — never re-validated
                # against git here (retention may since have pruned it).
                "preserve_ref": task.preserve_ref,
                "adapters_used": adapters_used,
            }
        )
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "run_id": state.run_id,
        "run_type": state.run_type,
        "source": state.source,
        "started_at": state.started_at,
        "status": status,
        "finished": state.finished,
        "stopped": state.stopped,
        "graceful_stop_pending": graceful_stop_pending,
        "crashed": state.crashed,
        "crash_error": state.crash_error,
        "paused_stage": state.paused_stage,
        "paused_reason": state.paused_reason,
        "paused_story_key": state.paused_story_key,
        "cache_read_weight": weight,
        "tokens": {
            "raw": raw_total,
            "weighted": weighted_total,
        },
        "adapters": adapters,
        "tasks": tasks,
    }


LIST_SCHEMA_VERSION = 1


def list_document(infos: list[RunInfo]) -> dict[str, object]:
    """The `list --json` document: one entry per run, oldest first.

    Obeys the pure-document contract in machine.py (additive-only evolution;
    anything breaking bumps LIST_SCHEMA_VERSION). A pure projection of
    discover_runs(): status is its liveness-aware vocabulary
    (running|paused|finished|stopped|crashed|interrupted|unknown), and runs
    whose state.json fails to parse are included (run_type "?", started_at "",
    status "unknown") — enumeration scripts must see them. `ref` is
    runs.short_ref(run_id), derived from the id — stable, not positional.
    paused_stage is "" unless status is "paused". An empty runs dir is a valid
    empty document with exit 0, never an error.
    """
    return {
        "schema_version": LIST_SCHEMA_VERSION,
        "runs": [
            {
                "ref": runs.short_ref(ri.run_id),
                "run_id": ri.run_id,
                "run_type": ri.run_type,
                "started_at": ri.started_at,
                "status": ri.status,
                "paused_stage": ri.paused_stage,
            }
            for ri in infos
        ],
    }


CLEANUP_SCHEMA_VERSION = 1


def cleanup_document(
    *,
    dry_run: bool,
    killed: list[str],
    live: list[str],
    unknown: set[str],
    windows: list[str],
) -> dict[str, object]:
    """The `cleanup --json` document: the multiplexer artifacts this invocation
    removed, or — under ``--dry-run`` — would remove.

    Obeys the pure-document contract in machine.py (additive-only evolution;
    anything breaking bumps CLEANUP_SCHEMA_VERSION). Plan and outcome share one
    shape: the lists mean the same thing either way and `dry_run` alone says
    whether it happened, so a caller can diff a preview against the real run.
    `sessions.removed` holds run ids (the session name is `bmad-loop-<id>`),
    `ctl_windows.removed` holds window names. `unverifiable_pid` is the subset
    of `sessions.removed` whose engine liveness could not be proven — the text
    mode's stderr warning, carried in the document so JSON mode leaves stderr
    empty. It never blocks cleanup: pruning kills the tmux session, never the
    engine pid. Nothing to clean up is a valid document of empty lists at
    exit 0, never an error.
    """
    return {
        "schema_version": CLEANUP_SCHEMA_VERSION,
        "dry_run": dry_run,
        "sessions": {
            "removed": list(killed),
            "live": list(live),
            "unverifiable_pid": sorted(unknown),
        },
        "ctl_windows": {"removed": list(windows)},
    }


CLEAN_SCHEMA_VERSION = 1


def clean_document(
    *,
    dry_run: bool,
    retain: int,
    cleanup_policy: policy.CleanupPolicy,
    freed_bytes: int,
    worktrees: list[str],
    trimmed: list[str],
    archived: list[str],
    deleted: list[str],
    protected: list[str],
    unverifiable_pid: list[str],
) -> dict[str, object]:
    """The `clean --json` document: the disk this invocation reclaimed, or —
    under ``--dry-run`` — would reclaim.

    Obeys the pure-document contract in machine.py (additive-only evolution;
    anything breaking bumps CLEAN_SCHEMA_VERSION). Plan and outcome share one
    shape: the lists mean the same thing either way and `dry_run` alone says
    whether it happened, so a caller can diff a preview against the real run.

    `freed_bytes` is a raw integer — the text mode's `~1.2MB` is a rendering of
    this number, and formatting is the renderer's job. It is the same estimate
    the text prints: measured before mutating (so it holds under --dry-run) and
    approximate by construction, since it sums whole run dirs for archive/delete
    but only the `worktrees/` tree for a trim.

    Every list names items the text enumerates or counts: `worktrees` holds
    absolute worktree paths, the rest hold run ids. `protected` is the runs left
    untouched — `--keep`-listed or non-terminal — which the text reports only as
    a count. `unverifiable_pid` is the subset of touched runs whose engine
    liveness could not be proven; it is the text mode's stderr warning, carried
    in the document so JSON mode leaves stderr empty, and it never blocks
    reclamation.

    `policy.retain` is the *effective* window — `--retain` when given, else
    `[cleanup] run_retention`. The other three are the configured policy as
    loaded. Note `--hard` overrides `archive_old` for this invocation only, so
    it does not change the reported value; the outcome shows in `deleted`.
    """
    return {
        "schema_version": CLEAN_SCHEMA_VERSION,
        "dry_run": dry_run,
        "policy": {
            "retain": retain,
            "retention_days": cleanup_policy.retention_days,
            "archive_old": cleanup_policy.archive_old,
            "trim_artifacts": cleanup_policy.trim_artifacts,
        },
        "freed_bytes": freed_bytes,
        "worktrees": list(worktrees),
        "trimmed": list(trimmed),
        "archived": list(archived),
        "deleted": list(deleted),
        "protected": list(protected),
        "unverifiable_pid": list(unverifiable_pid),
    }
