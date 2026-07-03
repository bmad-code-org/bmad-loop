"""`bmad-loop diagnose`: a sanitized diagnostic dump of a run/sweep.

When a run or sweep misbehaves, a maintainer needs to see the *shape* of what
happened — phase transitions, escalations, token usage, which adapter/model ran,
how sessions ended — but must NEVER receive the user's proprietary code, spec or
story content, prompts, transcripts, file paths, or any PII. This command derives
that diagnostic shape from a run dir and routes every content-bearing value
through the audited :mod:`bmad_loop.sanitize` chokepoint before rendering.

It mirrors :mod:`bmad_loop.probe`: typed findings → collectors → ``render_markdown``
/ ``render_json``, with ``--out``/``--json`` on the CLI. The safety model is
fail-closed by construction: structure (counts, enums, ints, durations) is derived
directly; every value that could carry content is dropped, reduced to a boolean,
**pseudonymized** (story keys/branches/SHAs are identifier-shaped and would
otherwise survive verbatim), or scrubbed. Unknown/future fields default to a
``scrub_json`` pass, never raw. As a final backstop the rendered bytes are run
through :func:`sanitize.assert_no_leak`; on any hit the render raises so the
command refuses to write.

The guiding assumption: the dump will be posted publicly.
"""

from __future__ import annotations

import json
import platform
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, sanitize
from .journal import Journal, load_state
from .model import RunState, StoryTask

SCHEMA_VERSION = 1
DEFAULT_JOURNAL_CAP = 200

# Run-dir subdirectories whose mere existence/size is diagnostic but whose
# CONTENTS are off-limits (raw tmux panes = code, prompts, feedback prose,
# patches, full worktree checkouts). We stat them; we never read into output.
_FILE_CATEGORIES = ("logs", "tasks", "feedback", "bundles", "failed", "worktrees", "events")

# Journal fields that name a proprietary identifier — pseudonymized, not dropped,
# so events stay correlatable. Maps field name -> alias namespace.
_JOURNAL_ALIAS_FIELDS = {
    "story_key": "story",
    "paused_story_key": "story",
    "log_task": "story",
    "task_id": "story",
    "bundle": "bundle",
    "branch": "branch",
    "target_branch": "branch",
    "commit": "commit",
    "baseline": "commit",
}
# Journal fields that carry free text (LLM/merge prose, prompts, errors). Never
# emitted — replaced with a boolean presence marker so a maintainer still learns
# the field was set without seeing it.
_JOURNAL_DROP_FIELDS = frozenset(
    {
        "prompt",
        "reason",
        "error",
        "detail",
        "suggestion",
        "message",
        "note",
        "blocker",
        "commit_message",
        "was_paused",
    }
)
# Journal fields whose value is a LIST of story keys (sprint unknown-keys).
_JOURNAL_KEYLIST_FIELDS = frozenset({"keys", "dw_ids"})

# Policy keys whose values can carry secrets/paths/free text. Dropped or reduced
# rather than scrubbed, since a single-token API key or repo name could be
# identifier-shaped and survive a plain scrub.
_POLICY_COUNT_KEYS = frozenset({"extra_args", "env", "worktree_seed"})
_POLICY_BOOL_KEYS = frozenset({"commit_message_template"})
_POLICY_KEYSET_KEYS = frozenset({"settings"})  # plugins.settings -> plugin ids only


# --------------------------------------------------------------- dataclasses


@dataclass
class EnvInfo:
    os: str
    os_release: str
    python_version: str
    package_version: str
    multiplexer: str
    tmux_version: str | None


@dataclass
class FileGroup:
    category: str
    count: int
    total_bytes: int
    total_lines: int | None = None  # logs only


@dataclass
class SessionTally:
    by_status: dict[str, int] = field(default_factory=dict)
    by_role: dict[str, int] = field(default_factory=dict)


@dataclass
class TaskDiag:
    alias: str
    epic: int
    phase: str
    attempt: int
    review_cycle: int
    terminal: bool
    rearmed: bool
    resolved_redrive: bool
    followup_review_recommended: bool
    committed: bool
    deferred_with_reason: bool
    spec_present: bool
    worktree_isolated: bool
    dw_count: int
    n_sessions: int
    sessions: SessionTally
    tokens: dict[str, int]


@dataclass
class JournalDiag:
    total_entries: int
    kind_histogram: dict[str, int]
    first_ts: float | None
    last_ts: float | None
    duration_s: float | None
    escalation_count: int
    defer_count: int
    plugin_error_count: int
    per_alias_event_counts: dict[str, dict[str, int]]
    entries: list[dict] = field(default_factory=list)


@dataclass
class RunDiag:
    run_id: str
    project_alias: str
    run_type: str
    started_date: str | None
    finished: bool
    stopped: bool
    paused: bool
    paused_stage: str | None
    paused_reason_present: bool
    current_epic: int | None
    sweep_cycle: int
    sweeps_triggered: list[str]
    plugin_shared_keys: int
    policy: dict
    n_tasks: int
    phase_histogram: dict[str, int]
    token_totals: dict[str, int]
    session_tally: SessionTally
    tasks: list[TaskDiag]
    journal: JournalDiag
    files: list[FileGroup]
    warnings: list[str] = field(default_factory=list)


@dataclass
class Diagnostics:
    schema_version: int
    generated_at: str
    tool_version: str
    env: EnvInfo
    runs: list[RunDiag]


# ----------------------------------------------------------------- collectors


def collect_env() -> EnvInfo:
    from .adapters.multiplexer import get_multiplexer

    mux = "none"
    tmux_v = None
    try:
        backend = get_multiplexer()
        mux = type(backend).__name__
        raw = backend.version()
        tmux_v = sanitize.scrub_text(raw, max_lines=1) if raw else None
    except Exception:  # noqa: BLE001  # nosec B110 - env probe is best-effort; absent mux is fine
        pass
    return EnvInfo(
        os=platform.system(),
        os_release=sanitize.scrub_text(platform.release()),
        python_version=platform.python_version(),
        package_version=__version__,
        multiplexer=mux,
        tmux_version=tmux_v,
    )


def summarize_files(run_dir: Path) -> list[FileGroup]:
    """Counts/sizes only — file contents are NEVER opened into the output."""
    groups: list[FileGroup] = []
    for category in _FILE_CATEGORIES:
        root = run_dir / category
        if not root.is_dir():
            continue
        count = 0
        total_bytes = 0
        total_lines = 0
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            count += 1
            try:
                total_bytes += p.stat().st_size
            except OSError:
                pass
            if category == "logs":
                try:
                    with p.open("rb") as f:
                        total_lines += sum(1 for _ in f)
                except OSError:
                    pass
        if count:
            groups.append(
                FileGroup(
                    category=category,
                    count=count,
                    total_bytes=total_bytes,
                    total_lines=total_lines if category == "logs" else None,
                )
            )
    return groups


def _session_tally(tasks: list[StoryTask]) -> SessionTally:
    by_status: Counter[str] = Counter()
    by_role: Counter[str] = Counter()
    for task in tasks:
        for s in task.sessions:
            by_status[str(s.status)] += 1
            by_role[str(s.role)] += 1
    return SessionTally(by_status=dict(by_status), by_role=dict(by_role))


def _task_diag(task: StoryTask, pseudo: sanitize.Pseudonymizer) -> TaskDiag:
    tokens = task.tokens.to_dict()
    tokens["total"] = task.tokens.total
    return TaskDiag(
        alias=pseudo.alias(task.story_key, ns="story", epic=task.epic),
        epic=task.epic,
        phase=str(task.phase),
        attempt=task.attempt,
        review_cycle=task.review_cycle,
        terminal=task.terminal,
        rearmed=task.rearmed,
        resolved_redrive=task.resolved_redrive,
        followup_review_recommended=task.followup_review_recommended,
        committed=task.commit_sha is not None,
        deferred_with_reason=bool(task.defer_reason),
        spec_present=bool(task.spec_file),
        worktree_isolated=bool(task.worktree_path),
        dw_count=len(task.dw_ids),
        n_sessions=len(task.sessions),
        sessions=_session_tally([task]),
        tokens=tokens,
    )


def _scrub_policy(obj: Any) -> Any:
    """Deep-scrub a policy snapshot, dropping the keys that can carry secrets,
    paths, or free text (``extra_args``/``env`` -> count, ``settings`` -> the
    plugin ids only, ``commit_message_template`` -> bool); everything else goes
    through the standard ``scrub_json`` value gate."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for raw_key, value in obj.items():
            key = str(raw_key)
            if key in _POLICY_COUNT_KEYS:
                out[f"{key}_count"] = len(value) if isinstance(value, (list, tuple, dict)) else 0
            elif key in _POLICY_BOOL_KEYS:
                out[f"{key}_set"] = bool(value)
            elif key in _POLICY_KEYSET_KEYS and isinstance(value, dict):
                out[key] = sorted(
                    k for k in (str(x) for x in value) if sanitize.looks_like_identifier(k)
                )
            else:
                out[key] = _scrub_policy(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [_scrub_policy(v) for v in obj]
    return sanitize.scrub_json(obj)


def _alias_in_entry(entry: dict, pseudo: sanitize.Pseudonymizer, epic_by_key: dict[str, int]):
    """The alias an entry's story-key/task field maps to, for per-alias counts."""
    for fld in ("log_task", "story_key", "task_id", "paused_story_key"):
        val = entry.get(fld)
        if val:
            return pseudo.alias(val, ns="story", epic=epic_by_key.get(str(val)))
    return None


def _scrub_entry(
    entry: dict,
    pseudo: sanitize.Pseudonymizer,
    epic_by_key: dict[str, int],
    first_ts: float | None,
) -> dict:
    """One journal entry reduced to a shareable form: relative timestamp, kind
    verbatim, identifier fields aliased, free-text fields collapsed to a
    presence boolean, and every remaining/unknown field scrub_json'd."""
    out: dict[str, Any] = {}
    ts = entry.get("ts")
    if isinstance(ts, (int, float)) and first_ts is not None:
        out["ts_offset"] = round(ts - first_ts, 3)
    kind = str(entry.get("kind", "?"))
    out["kind"] = kind if sanitize.looks_like_identifier(kind) else "<redacted:str>"
    for k, v in entry.items():
        if k in ("ts", "kind"):
            continue
        if k in _JOURNAL_DROP_FIELDS:
            out[f"{k}_present"] = v is not None and v != ""
        elif k in _JOURNAL_KEYLIST_FIELDS and isinstance(v, list):
            ns = "story" if k == "keys" else "dw"
            out[k] = [pseudo.alias(x, ns=ns, epic=epic_by_key.get(str(x))) for x in v]
        elif k in _JOURNAL_ALIAS_FIELDS:
            ns = _JOURNAL_ALIAS_FIELDS[k]
            epic = epic_by_key.get(str(v)) if ns == "story" else None
            out[k] = pseudo.alias(v, ns=ns, epic=epic)
        else:
            out[k] = sanitize.scrub_json(v)
    return out


def summarize_journal(
    entries: list[dict],
    pseudo: sanitize.Pseudonymizer,
    epic_by_key: dict[str, int],
    *,
    cap: int,
) -> JournalDiag:
    kinds: Counter[str] = Counter()
    per_alias: dict[str, Counter[str]] = {}
    timestamps: list[float] = []
    for entry in entries:
        kind = str(entry.get("kind", "?"))
        kinds[kind] += 1
        ts = entry.get("ts")
        if isinstance(ts, (int, float)):
            timestamps.append(ts)
        alias = _alias_in_entry(entry, pseudo, epic_by_key)
        if alias is not None:
            per_alias.setdefault(alias, Counter())[kind] += 1
    first_ts = min(timestamps) if timestamps else None
    last_ts = max(timestamps) if timestamps else None
    scrubbed = (
        [_scrub_entry(e, pseudo, epic_by_key, first_ts) for e in entries[:cap]] if cap > 0 else []
    )
    return JournalDiag(
        total_entries=len(entries),
        kind_histogram=dict(kinds),
        first_ts=first_ts,
        last_ts=last_ts,
        duration_s=(round(last_ts - first_ts, 3) if first_ts is not None else None),
        escalation_count=kinds.get("story-escalated", 0) + kinds.get("preference-escalation", 0),
        defer_count=kinds.get("story-deferred", 0),
        plugin_error_count=kinds.get("plugin-error", 0),
        per_alias_event_counts={a: dict(c) for a, c in per_alias.items()},
        entries=scrubbed,
    )


def _coarsen_date(started_at: str | None) -> str | None:
    if not started_at:
        return None
    head = str(started_at)[:10]
    return head if sanitize.looks_like_identifier(head.replace("-", "0")) else None


def collect_run(run_dir: Path, *, pseudo: sanitize.Pseudonymizer, cap: int) -> RunDiag:
    state: RunState = load_state(run_dir)
    tasks = list(state.tasks.values())
    epic_by_key = {t.story_key: t.epic for t in tasks}

    token_totals: Counter[str] = Counter()
    for t in tasks:
        for k, v in t.tokens.to_dict().items():
            token_totals[k] += v
        token_totals["total"] += t.tokens.total

    phase_hist: Counter[str] = Counter(str(t.phase) for t in tasks)

    return RunDiag(
        run_id=state.run_id,
        project_alias=pseudo.alias(Path(state.project).name, ns="project"),
        run_type=state.run_type,
        started_date=_coarsen_date(state.started_at),
        finished=state.finished,
        stopped=state.stopped,
        paused=state.paused,
        paused_stage=state.paused_stage,
        paused_reason_present=state.paused_reason is not None,
        current_epic=state.current_epic,
        sweep_cycle=state.sweep_cycle,
        sweeps_triggered=[
            s if sanitize.looks_like_identifier(str(s)) else "<redacted:str>"
            for s in state.sweeps_triggered
        ],
        plugin_shared_keys=len(state.plugin_shared),
        policy=_scrub_policy(state.policy_snapshot),
        n_tasks=len(tasks),
        phase_histogram=dict(phase_hist),
        token_totals=dict(token_totals),
        session_tally=_session_tally(tasks),
        tasks=[_task_diag(t, pseudo) for t in tasks],
        journal=summarize_journal(Journal(run_dir).entries(), pseudo, epic_by_key, cap=cap),
        files=summarize_files(run_dir),
    )


def collect(
    run_dirs: list[Path],
    *,
    pseudo: sanitize.Pseudonymizer,
    cap: int = DEFAULT_JOURNAL_CAP,
    generated_at: str | None = None,
) -> Diagnostics:
    runs: list[RunDiag] = []
    for run_dir in run_dirs:
        try:
            runs.append(collect_run(run_dir, pseudo=pseudo, cap=cap))
        except Exception as e:  # noqa: BLE001 — one bad run never sinks the dump
            runs.append(_unreadable_run(run_dir, e))
    return Diagnostics(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        tool_version=__version__,
        env=collect_env(),
        runs=runs,
    )


def _unreadable_run(run_dir: Path, err: Exception) -> RunDiag:
    return RunDiag(
        run_id=run_dir.name if sanitize.looks_like_identifier(run_dir.name) else "<redacted:str>",
        project_alias="project-?",
        run_type="?",
        started_date=None,
        finished=False,
        stopped=False,
        paused=False,
        paused_stage=None,
        paused_reason_present=False,
        current_epic=None,
        sweep_cycle=0,
        sweeps_triggered=[],
        plugin_shared_keys=0,
        policy={},
        n_tasks=0,
        phase_histogram={},
        token_totals={},
        session_tally=SessionTally(),
        tasks=[],
        journal=JournalDiag(0, {}, None, None, None, 0, 0, 0, {}),
        files=[],
        warnings=[f"run unreadable: {type(err).__name__}"],
    )


# ------------------------------------------------------------------ rendering


def _fmt_kv(label: str, value: Any) -> str:
    return f"- **{label}:** {value}"


def _to_jsonable(d: Diagnostics) -> dict:
    from dataclasses import asdict

    return asdict(d)


def render_json(d: Diagnostics, *, pseudo: sanitize.Pseudonymizer | None = None) -> str:
    rendered = json.dumps(_to_jsonable(d), indent=2, sort_keys=True)
    _guard(rendered, pseudo)
    return rendered


def render_markdown(d: Diagnostics, *, pseudo: sanitize.Pseudonymizer | None = None) -> str:
    out: list[str] = []
    out.append("# bmad-loop diagnostic dump (sanitized)")
    out.append("")
    out.append(
        "_Identifiers are pseudonymized; code, prompts, paths and free text are "
        "redacted. Safe to share._"
    )
    out.append("")
    out.append("## Environment")
    e = d.env
    out.append(_fmt_kv("bmad-loop version", e.package_version))
    out.append(_fmt_kv("python", e.python_version))
    out.append(_fmt_kv("os", f"{e.os} {e.os_release}"))
    out.append(_fmt_kv("multiplexer", e.multiplexer))
    out.append(_fmt_kv("tmux", e.tmux_version or "—"))
    out.append(_fmt_kv("schema / generated", f"v{d.schema_version} @ {d.generated_at}"))
    out.append("")

    for r in d.runs:
        out.append(f"## Run `{r.run_id}` ({r.run_type})")
        out.append(_fmt_kv("project", f"`{r.project_alias}`"))
        out.append(_fmt_kv("started", r.started_date or "—"))
        out.append(
            _fmt_kv(
                "state",
                f"finished={r.finished} stopped={r.stopped} paused={r.paused}"
                + (
                    f" (stage={r.paused_stage}, reason_present={r.paused_reason_present})"
                    if r.paused
                    else ""
                ),
            )
        )
        out.append(_fmt_kv("epic / sweep_cycle", f"{r.current_epic} / {r.sweep_cycle}"))
        if r.sweeps_triggered:
            out.append(_fmt_kv("sweeps_triggered", ", ".join(f"`{s}`" for s in r.sweeps_triggered)))
        out.append(_fmt_kv("tasks", r.n_tasks))
        out.append(_fmt_kv("phase histogram", _dict_inline(r.phase_histogram)))
        out.append(_fmt_kv("token totals", _dict_inline(r.token_totals)))
        out.append(_fmt_kv("sessions by status", _dict_inline(r.session_tally.by_status)))
        out.append(_fmt_kv("sessions by role", _dict_inline(r.session_tally.by_role)))
        if r.warnings:
            for w in r.warnings:
                out.append(f"- ⚠️ {w}")
        out.append("")

        out.append("### Tasks")
        if r.tasks:
            out.append(
                "| alias | epic | phase | att | rev | committed | spec | dw | sessions | tokens |"
            )
            out.append("|---|---|---|---|---|---|---|---|---|---|")
            for t in r.tasks:
                out.append(
                    f"| `{t.alias}` | {t.epic} | {t.phase} | {t.attempt} | {t.review_cycle} "
                    f"| {t.committed} | {t.spec_present} | {t.dw_count} | {t.n_sessions} "
                    f"| {t.tokens.get('total', 0)} |"
                )
        else:
            out.append("_no tasks._")
        out.append("")

        j = r.journal
        out.append("### Journal")
        out.append(_fmt_kv("entries", j.total_entries))
        out.append(_fmt_kv("duration (s)", j.duration_s if j.duration_s is not None else "—"))
        out.append(
            _fmt_kv(
                "escalations / defers / plugin-errors",
                f"{j.escalation_count} / {j.defer_count} / {j.plugin_error_count}",
            )
        )
        out.append(_fmt_kv("kind histogram", _dict_inline(j.kind_histogram)))
        if j.per_alias_event_counts:
            out.append("\n_Per-task event counts:_")
            for alias, counts in sorted(j.per_alias_event_counts.items()):
                out.append(f"- `{alias}`: {_dict_inline(counts)}")
        out.append("")

        out.append("### Run-dir files (counts only)")
        if r.files:
            for g in r.files:
                lines = f", {g.total_lines} lines" if g.total_lines is not None else ""
                out.append(_fmt_kv(g.category, f"{g.count} files, {g.total_bytes} bytes{lines}"))
        else:
            out.append("_none._")
        out.append("")

    rendered = "\n".join(out)
    _guard(rendered, pseudo)
    return rendered


def _dict_inline(d: dict) -> str:
    if not d:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in sorted(d.items()))


def _guard(rendered: str, pseudo: sanitize.Pseudonymizer | None) -> None:
    """Fail closed: refuse to emit output that trips the leak self-check.

    When the pseudonymizer is supplied, its legend's original values (the real
    story keys/branches/SHAs) are fed in too, so any that slipped through the
    per-field routing are caught here in the final bytes."""
    extra = list(pseudo.legend().values()) if pseudo is not None else []
    fired = sanitize.assert_no_leak(rendered, extra=extra)
    if fired:
        raise LeakDetected(fired)


class LeakDetected(Exception):
    """The rendered dump tripped sanitize.assert_no_leak — emission is refused."""

    def __init__(self, rules: list[str]):
        self.rules = rules
        super().__init__("diagnostic dump tripped leak self-check: " + ", ".join(rules))
