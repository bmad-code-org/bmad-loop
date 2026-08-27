"""`bmad-loop diagnose`: a sanitized diagnostic dump of a run/sweep.

When a run or sweep misbehaves, a maintainer needs to see the *shape* of what
happened — phase transitions, escalations, token usage, which adapter/model ran,
how sessions ended — but must NEVER receive the user's proprietary code, spec or
story content, prompts, transcripts, file paths, or any PII. This command derives
that diagnostic shape from a run dir and routes every content-bearing value
through the audited :mod:`bmad_loop.sanitize` chokepoint before rendering.

It mirrors :mod:`bmad_loop.probe`: typed findings → collectors → ``render_markdown``
/ ``render_json``. On the CLI the default is the markdown report and ``--json``
selects the pure JSON document instead (the :mod:`bmad_loop.machine` contract —
one object on stdout, nothing else); ``--out FILE`` redirects whichever of the
two was selected to a file. The safety model is fail-closed by construction:
structure (counts, enums, ints, durations) is derived
directly; every value that could carry content is dropped, reduced to a boolean,
**pseudonymized** (story keys/branches/SHAs are identifier-shaped and would
otherwise survive verbatim), or scrubbed. Unknown/future fields default to a
``scrub_json`` pass, never raw. As a final backstop the rendered bytes are run
through :func:`sanitize.guard` — the fail-closed egress self-check shared with
``probe-adapter`` since #199. That backstop re-scans the rendered bytes for known
*shapes* — email, URL credentials, credential-shaped tokens, the absolute-home
spellings in both separator forms (#512 added the backslash WSL-UNC one), and *this
process's* username — and refuses to emit on a hit; it is not a proof of absence, since
a home spelling it does not know, or a username that is not this process's (the Linux
account behind a WSL UNC path), passes it untouched. Per-field routing is the primary
defense and the guard is defense in depth. A stray pseudonymized original (a
per-field routing gap — the value is in the legend, so its safe alias is known)
is **repaired** by substituting the alias, re-verified, and disclosed in the
dump itself — a "Backstop repairs" section in the markdown report, an optional
top-level ``backstop_repairs`` label→count key in the JSON document — so the gap
still surfaces as a reportable bug even when only one of the two was rendered.
A genuine PII/secret/path/username hit, or a repair that does not converge,
raises so the command refuses to write.

The guiding assumption: the dump will be posted publicly.
"""

from __future__ import annotations

import json
import os
import platform
import re
import stat
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, sanitize
from .journal import VERIFY_DIR, Journal, load_state
from .model import RunState, StoryTask
from .platform_util import walk_files_unlinked

# The guard machinery (fail-closed egress self-check + alias-substitution
# repair) moved to sanitize.py so probe-adapter shares the single audited
# implementation (#199). LeakDetected stays importable from here — cli.py and
# external callers resolve `diagnostics.LeakDetected` — so it carries a noqa:
# without it ruff F401 autofixes the re-export away and the except clause in
# cmd_diagnose breaks.
from .sanitize import LeakDetected  # noqa: F401 — re-export

# Deliberately NOT bumped when `--json` stopped being a fenced ```json block
# inside the markdown report and became a pure document (machine.py contract):
# this number versions the *document*, and the payload did not change. Bumping it
# would falsely tell a consumer pinned to v1 that the fields it reads are gone,
# while a consumer actually broken by the repackaging finds out immediately —
# the fence is gone and json.loads fails. Bump only on a payload break.
SCHEMA_VERSION = 1
DEFAULT_JOURNAL_CAP = 200

# Subdirectories whose mere existence/size is diagnostic but whose CONTENTS are
# off-limits (raw tmux panes = code, prompts, feedback prose, patches, full
# worktree checkouts). We stat them; we never read into output.
#
# All are run-dir-relative EXCEPT "events", which since #494 lives out of the
# project tree at the user state root — see `_category_roots`.
#
# VERIFY_DIR belongs here for the reason the category exists: retained verifier
# stdout/stderr is a build's own output — off-limits to read, but its SIZE is
# exactly the diagnostic. `[verify] stream_capture_kb` defaults to 256 KiB per
# stream, so a run retains up to 512 KiB per command per attempt with no GC
# behind it yet, which can make this store one of the larger things in a run
# dir. Omitting it left `diagnose` unable to show a retention or disk-usage
# problem it is the natural place to notice. Imported, not re-spelled, so the
# reporter cannot drift from the writer that creates the directory.
_FILE_CATEGORIES = (
    "logs",
    "tasks",
    "feedback",
    "bundles",
    "failed",
    "worktrees",
    "events",
    VERIFY_DIR,
)
_EVENTS_CATEGORY = "events"

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
    # The baseline a re-arm's re-stamp replaced (`rearm-baseline-restamped`). Its
    # OWN entry rather than the `baseline` one beside it, because both shas ride a
    # single record: alias one and leave the other and a dump pseudonymizes half a
    # comparison, which is worse than either doing both or doing neither.
    "overwritten": "commit",
    # A spec name IS the customer's feature name — `Pseudonymizer`'s own docstring
    # has always listed "spec filenames" among what it exists to alias, so the
    # omission here was a routing gap, not a policy. A producer that journals a
    # bare basename passes a value `looks_like_identifier` waves through verbatim
    # on the scrub_json fallback — and the egress backstop cannot rescue it,
    # because it only repairs values already in the legend, so it happens to
    # catch `1.2-Acme….md` (the story key is in there) and misses
    # `AcmeVaultRotation.md` entirely. Its OWN namespace, not "story": the epic
    # lookup below is keyed on ns == "story", so a filename aliased there would
    # render as an epic-less `story-<hex>` — indistinguishable from a story key
    # whose epic could not be resolved. See `_JOURNAL_BASENAME_NAMESPACES` for why
    # the value is normalized first: the producers do NOT agree on a bare basename.
    "spec": "spec",
    # The same value also arrives under a second field NAME. `runs.rearm_escalation`
    # is the only journal producer of `spec_file` (its `rearm-baseline-restamped` and
    # `rearm-baseline-restamp-skipped` kinds); `engine._park_awaiting_operator` passes
    # `spec_file=` to `operatoractions.record_park`, which is a record file, not the
    # journal. So the divergence is BETWEEN FIELDS, not between two producers of this
    # one: `spec` carries absolute paths from engine's reconcile and marker-repair
    # kinds, while `spec_file` carries whatever `StoryTask` persisted — worktree-relative
    # for a task that ran under isolation. Same value, same hazard, same namespace, and
    # `_JOURNAL_BASENAME_NAMESPACES` keys on the namespace rather than the field, so
    # both spellings still reduce to one alias.
    "spec_file": "spec",
}
# Namespaces whose journalled value arrives in more than one shape and must be
# reduced to its basename before it is aliased. `spec` is one: engine.py's
# reconcile and marker-repair kinds journal `str(spec_path)` (absolute —
# `verify.resolve_spec_path` returns an absolute path), while stories_engine's
# `checkpoint-pause` journals `task.spec_file`, which `StoryTask` persists
# worktree-relative (a bare basename for a spec at the worktree root). Aliasing
# the raw string would give ONE spec TWO aliases in a single dump, defeating the
# correlation these fields are aliased rather than dropped to preserve, and would
# park an absolute home path in the local `--legend` file (before `spec` was
# routed such a value died at `scrub_json` and never entered the map at all).
#
# Split on BOTH separators rather than using `PurePath(...).name`: a journal
# written on Windows is routinely diagnosed on POSIX, where `PurePath` treats a
# backslash as an ordinary character and would keep the whole path. The cost is
# that a POSIX filename containing a literal backslash aliases on its tail —
# a pathological name, and the consequence is a shorter legend entry, never a
# leak, since the alias is still stable and the raw value still never ships.
_JOURNAL_BASENAME_NAMESPACES = frozenset({"spec"})
_PATH_SEP_RE = re.compile(r"[\\/]")
# Journal fields that carry free text (LLM/merge prose, prompts, errors). Never
# emitted — replaced with a boolean presence marker so a maintainer still learns
# the field was set without seeing it.
#
# The `verify-command-result` group at the end is the same convention applied to
# the verifier records: `command` is operator-authored shell (`[verify] commands`),
# `output_tail` is a build's own output, `capture_error` is an OSError string
# carrying a path, and the two pointers embed the story key. Routing them here
# rather than leaving them to `scrub_json` is deliberate — that fallback fails
# closed only by accident of shape, since `_IDENTIFIER_RE` forbids `/` and spaces
# and so collapses paths, argv-ish commands and multi-line tails, while a
# one-word `command` (`make`) or a one-word tail (`FAILED`) is identifier-shaped
# and would ship verbatim. The presence boolean is also strictly more useful for
# the pointers: it separates "a stream was retained" from "the cap is 0 or the
# write failed", which a redacted string cannot.
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
        "command",
        "output_tail",
        "capture_error",
        "stdout_path",
        "stderr_path",
        # An absolute host path naming the run's code tree
        # (`rearm-baseline-advance-failed`). Dropped rather than aliased: unlike a
        # spec filename it correlates nothing across events — one run has one code
        # root — while carrying the customer's directory layout. Dropped rather
        # than left to the `scrub_json` fallback: that fallback happens to redact an
        # absolute path (`_IDENTIFIER_RE` forbids `/`, `\` and `:`, so any real root
        # collapses to `<redacted:str>`), but it fails closed only by accident of
        # shape — a root that did parse as a bare identifier would pass verbatim, and
        # nothing here should depend on a path never looking like one. The `error=`
        # field on the very same record is dropped too, but under this set's
        # free-text rule above (it is a `GitError` string quoting git's own stderr),
        # not this identifier-shape argument — same set, different rationale.
        "repo",
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
    # The host's git, as git names itself — `verify.GIT_FLOOR` is the floor a run is
    # refused below, so "which git ran this" is the first thing a dump has to answer
    # about a refusal. `None` when git could not be asked at all, which is itself the
    # finding: the same probe that fails here is the one that aborts a run.
    git_version: str | None
    # `platform.system()` above says "Windows" for both a native shell and a WSL
    # interop launch. `sys_platform` carries the raw token instead, so a dump can be
    # matched character-for-character against validate's `platform default for {token}`
    # line; `win32_on_wsl_path` is the #332 condition itself. Named for exactly what it
    # observes — a win32 interpreter working a distro path — and NOT "wsl interop":
    # `cd \\wsl.localhost\...` from native PowerShell reaches the same state, so this
    # must not claim where the operator is standing (the matching `host.win32-on-wsl-path`
    # finding is worded to the same limit). Additive fields — no SCHEMA_VERSION bump;
    # `--json` evolution is additive-only, see the contract note in machine.py.
    sys_platform: str
    win32_on_wsl_path: bool


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
    sweeps_refused: dict[str, str]
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


def collect_env(project: Path) -> EnvInfo:
    """Host facts for the dump's Environment block.

    ``project`` feeds only the #332 verdict. It is required — here *and* on
    ``collect`` — so a new caller must consciously supply the project it actually
    diagnosed; the type cannot force that choice to be *right* (any ``Path``
    yields a confident verdict), and the sole production caller, ``cmd_diagnose``,
    passes the resolved project.

    The project *path* itself is never emitted: it is a redaction hazard — the
    redactor leaves the Linux username in a ``\\\\wsl.localhost\\...\\home\\<user>\\...``
    path standing (it compares against the *Windows* account), and ``collect_env``
    has no pseudonymizer to alias it against — so the boolean is what ships, and since
    #512 that shape trips the egress guard, which refuses the whole dump rather than
    passing it. Same
    reason ``sys.executable`` is absent despite naming the exact mismatch: the venv
    path carries the project name past the redactor."""
    from . import verify
    from .adapters.multiplexer import fold_version, get_multiplexer
    from .platform_util import is_wsl_unc_path

    mux = "none"
    tmux_v = None
    try:
        backend = get_multiplexer()
        mux = type(backend).__name__
        # fold_version both flattens and bounds (the seam's inline-render
        # contract), so the max_lines=1 this used to carry is redundant — and it
        # never held anyway: scrub_text's "(N more lines redacted)" marker is
        # itself a new line, re-introducing exactly what the cap removed.
        # Scrub first so the home/email redaction reads the whole probe rather
        # than a tail the fold already cut.
        raw = backend.version()
        tmux_v = fold_version(sanitize.scrub_text(raw)) if raw else None
    except Exception:  # nosec B110 - env probe is best-effort; absent mux is fine
        pass

    git_v = None
    try:
        # Same scrub-then-fold order as the mux probe above, for the same #321
        # reason: folding first can cut a home path mid-way, leaving a fragment
        # `redact_home` no longer matches. `git version` is one short line today,
        # but a vendor build is free to say more and the bound is what makes that
        # safe. Reported RAW rather than as a verdict — a dump is evidence, and the
        # floor it is read against can move after the dump was written.
        # `git_bytes` ANSWERS with a non-zero rc rather than raising, so the rc is
        # checked here for the same reason `git_below_floor` checks it: stdout on a
        # failed probe is not a version, and folding it would put a fabricated
        # answer in the dump. `None` — "could not be asked" — is the honest value.
        # Bounded rather than inheriting the engine's `GIT_TIMEOUT_S` (120s), via
        # the #390 per-call seam the other two best-effort probes already use
        # (`install`'s init hint, the TUI's commit-subject render). `diagnose` is a
        # FOREGROUND recovery aid — the command you reach for when the host is
        # already broken — and a git that hangs is one of the states it exists to
        # be usable in. Inheriting the engine bound made it sit silent for two
        # minutes and then swallow the fault anyway, so the whole wait bought a
        # `None` this returns in five seconds. The dump is worth far more than the
        # one line this probe fills.
        probed = verify.git_bytes(project, "version", timeout_s=5)
        if probed.returncode == 0:
            git_v = fold_version(sanitize.scrub_text(os.fsdecode(probed.stdout))) or None
    except Exception:  # nosec B110 - env probe is best-effort; absent git is fine
        pass

    return EnvInfo(
        os=platform.system(),
        os_release=sanitize.scrub_text(platform.release()),
        python_version=platform.python_version(),
        package_version=__version__,
        multiplexer=mux,
        tmux_version=tmux_v,
        git_version=git_v,
        sys_platform=sys.platform,
        win32_on_wsl_path=sys.platform == "win32" and is_wsl_unc_path(project),
    )


def _category_roots(category: str, run_dir: Path, events_dir: Path | None) -> list[Path]:
    """Where a category's files live. One directory for all but ``events``.

    The events channel has TWO live roots since #494 (Phase 3): the primary is
    out of the project tree under the user state root, and the legacy in-tree
    ``<run-dir>/events`` is still both written and polled. It has to be, because
    the hook relay is COPIED into the project by ``init`` — an upgraded
    orchestrator routinely drives sessions whose relay predates the move and
    still writes in-tree, so the watcher dual-polls. Counting only one root would
    report zero events for precisely the runs whose event routing is what a
    maintainer is reading the dump to understand.

    Both are summed into ONE ``FileGroup`` named ``events``: the payload shape is
    the schema, and splitting the category (or adding a field) would be a break
    for a v1 consumer. Which root the events came from is not what the count is
    for — "did the hooks fire at all" is.
    """
    if category != _EVENTS_CATEGORY:
        return [run_dir / category]
    legacy = run_dir / _EVENTS_CATEGORY
    # Deduped by spelling so a state root deliberately pointed inside the run dir
    # (BMAD_LOOP_STATE_DIR is honoured as spelled) cannot double-count.
    if events_dir is None or events_dir == legacy:
        return [legacy]
    return [events_dir, legacy]


def _count_lines(path: Path) -> int:
    """Lines in a regular file, or 0 — never blocking on a FIFO a session planted.

    ``O_NONBLOCK`` plus an ``S_ISREG`` check **on the descriptor**, the idiom
    ``runs.read_trusted_config_digest`` and ``tui.launch._read_ctl_window``
    already carry for the same hazard: the run directory is exported to the
    driven session as ``BMAD_LOOP_RUN_DIR``, so an lstat taken before the open is
    a check-then-open race, and ``fstat`` describes the object actually opened.
    Opening a FIFO read-only without ``O_NONBLOCK`` blocks until a writer
    arrives — indefinitely, for a diagnostic dump nobody is feeding, and
    ``diagnose`` is a foreground command a human is waiting on. ``O_NOFOLLOW``
    keeps the final component from redirecting the read out of the run, which is
    the one hop :func:`platform_util.walk_files_unlinked` cannot refuse for it.
    The POSIX-only flags degrade to 0 on win32, where the fd check carries alone.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)  # win32: no CRLF translation on the raw fd
    try:
        fd = os.open(path, flags)
    except OSError:
        return 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return 0
        with os.fdopen(fd, "rb", closefd=False) as f:
            return sum(1 for _ in f)
    except OSError:
        return 0
    finally:
        os.close(fd)


def summarize_files(run_dir: Path, *, events_dir: Path | None = None) -> list[FileGroup]:
    """Counts/sizes only — file contents are NEVER opened into the output.

    ``events_dir`` is this run's out-of-tree event channel (#494); ``None`` when
    the caller could not derive one, which degrades to the legacy in-tree
    location alone rather than dropping the category.
    """
    groups: list[FileGroup] = []
    for category in _FILE_CATEGORIES:
        count = 0
        total_bytes = 0
        total_lines = 0
        for root in _category_roots(category, run_dir, events_dir):
            if not root.is_dir():
                continue
            # walk_files_unlinked, not rglob: `is_dir()` above FOLLOWS a link, so a
            # planted redirect at a category root reads as a directory and rglob
            # then counts the target's tree as this run's retained output.
            for p in walk_files_unlinked(root):
                # The regular-file filter `rglob` + `is_file()` used to carry, and
                # which came off with the switch: `os.walk` reports every
                # non-directory entry, so `files` holds FIFOs, device nodes and
                # symlinks too. None of those is retained output of this run, and
                # the `logs` arm below OPENS what it counts. lstat, not
                # `is_file()` — that FOLLOWS, so it answers about the target of a
                # planted link rather than about the entry in this run's tree.
                try:
                    info = p.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                count += 1
                total_bytes += info.st_size
                if category == "logs":
                    total_lines += _count_lines(p)
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


def _task_diag(task: StoryTask, pseudo: sanitize.Pseudonymizer, weight: float) -> TaskDiag:
    tokens = task.tokens.to_dict()
    tokens["total"] = task.tokens.total
    # Derived from the run's snapshot weight, which this bundle also carries
    # under policy.limits — so the figure stays checkable against its inputs.
    tokens["weighted"] = task.tokens.weighted_total(weight)
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
                # Keys pass through VERBATIM here, deliberately — unlike
                # `sanitize._scrub`, which scrubs keys as well as values. The
                # warrant is what this snapshot IS: `Policy.to_dict()` is
                # `asdict()` over frozen dataclasses, so every key is a
                # compile-time field name — developer-authored, non-PII, and the
                # point of the dump (a reader diagnosing a run needs to see
                # `max_review_cycles`, not `<redacted:str>`). The one user-keyed
                # table, `plugins.settings`, never reaches this branch:
                # `_POLICY_KEYSET_KEYS` intercepts it above and reduces it to its
                # identifier-gated plugin-id keyset (#186).
                #
                # That rests on an invariant nothing else stated until #202: NO
                # policy section has a free-keyed table. Add one — say an
                # `adapter.overrides` keyed by binary path — and its keys ship
                # verbatim in a dump meant to be shareable.
                # `test_no_policy_section_has_a_free_keyed_table`
                # (tests/test_diagnostics.py) enforces it over the field TYPES, so
                # it fires when such a table is declared rather than when a user
                # first populates it; route a new one through `_POLICY_KEYSET_KEYS`
                # or `_POLICY_COUNT_KEYS` rather than widening that test.
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


def _alias_input(value: Any, ns: str) -> Any:
    """The string an alias is computed over: the basename for the path-shaped
    namespaces, the value unchanged for every other one (and for any non-string,
    which :meth:`Pseudonymizer.alias` handles itself)."""
    if ns not in _JOURNAL_BASENAME_NAMESPACES or not isinstance(value, str):
        return value
    # `or value`: a value ending in a separator splits to an empty tail, and an
    # empty string is the one input `alias()` passes through unaliased — so it
    # would render as `""` and lose the event's only reference to the spec.
    return _PATH_SEP_RE.split(value)[-1] or value


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
            v = _alias_input(v, ns)
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
        # first_ts/last_ts are set together (both None iff no timestamps), so the
        # first_ts guard also proves last_ts is not None here.
        duration_s=(
            round(last_ts - first_ts, 3)  # pyright: ignore[reportOptionalOperand]
            if first_ts is not None
            else None
        ),
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


def _events_dir(state: RunState) -> Path | None:
    """This run's out-of-tree event channel, or ``None`` if it is not derivable.

    Derived from the run's OWN recorded project and id rather than threaded down
    from ``cmd_diagnose``'s ``--project``, which is the smaller change and the
    truer one: ``--all`` dumps every run under a project, and a run carries the
    project it was started against. ``run_dir`` cannot answer this — the state
    root is keyed by a digest of the resolved project, not by the run dir's
    ancestry.

    Every failure mode is observation, so every one of them degrades. The
    derivation resolves the project (``runs.project_tag``) and consults the host
    for a state root, and a dump is routinely read on a machine that is not the
    one that produced it: an unresolvable project, or a host with no derivable
    state root, must cost the events count and nothing else.
    """
    from . import runs

    try:
        return runs.events_dir_for(Path(state.project), state.run_id)
    except Exception:
        # No `# nosec`: bandit's B110/B112 are about `pass`/`continue` bodies, and
        # a directive naming a rule that never fires here would read as a waived
        # finding. The breadth is deliberate and stated above, not silenced.
        return None


def collect_run(run_dir: Path, *, pseudo: sanitize.Pseudonymizer, cap: int) -> RunDiag:
    state: RunState = load_state(run_dir)
    tasks = list(state.tasks.values())
    epic_by_key = {t.story_key: t.epic for t in tasks}

    weight = state.cache_read_weight()
    token_totals: Counter[str] = Counter()
    for t in tasks:
        for k, v in t.tokens.to_dict().items():
            token_totals[k] += v
        token_totals["total"] += t.tokens.total
        # Per-task, matching Engine.summary and the TUI (weighted_total rounds
        # internally, so summing per task is what keeps the numbers identical).
        token_totals["weighted"] += t.tokens.weighted_total(weight)

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
        # BOTH halves are filtered. The value is a closed SWEEP_REFUSED_* slug by
        # construction, but the key is a trigger string off state.json — the same
        # untrusted footing as sweeps_triggered above — and a hand-edited or
        # foreign state file must not be able to route a home path into a report
        # that `sanitize.guard` would then refuse to emit at all.
        sweeps_refused={
            (k if sanitize.looks_like_identifier(str(k)) else "<redacted:str>"): (
                v if sanitize.looks_like_identifier(str(v)) else "<redacted:str>"
            )
            for k, v in state.sweeps_refused.items()
        },
        plugin_shared_keys=len(state.plugin_shared),
        policy=_scrub_policy(state.policy_snapshot),
        n_tasks=len(tasks),
        phase_histogram=dict(phase_hist),
        token_totals=dict(token_totals),
        session_tally=_session_tally(tasks),
        tasks=[_task_diag(t, pseudo, weight) for t in tasks],
        journal=summarize_journal(Journal(run_dir).entries(), pseudo, epic_by_key, cap=cap),
        files=summarize_files(run_dir, events_dir=_events_dir(state)),
    )


def collect(
    run_dirs: list[Path],
    *,
    pseudo: sanitize.Pseudonymizer,
    cap: int = DEFAULT_JOURNAL_CAP,
    generated_at: str | None = None,
    project: Path,
) -> Diagnostics:
    runs: list[RunDiag] = []
    for run_dir in run_dirs:
        try:
            runs.append(collect_run(run_dir, pseudo=pseudo, cap=cap))
        except Exception as e:  # one bad run never sinks the dump
            runs.append(_unreadable_run(run_dir, e))
    return Diagnostics(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        tool_version=__version__,
        env=collect_env(project),
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
        sweeps_refused={},
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


def render_json(
    d: Diagnostics,
    *,
    pseudo: sanitize.Pseudonymizer | None = None,
    repairs: list[tuple[str, int]] | None = None,
) -> str:
    # ensure_ascii=False is a SAFETY requirement, not cosmetics: the default
    # escapes every non-ASCII char to \uXXXX, so a sensitive value like a
    # non-ASCII username reaches _guard as "café-user" and matches nothing.
    # The guard must see the string as itself. The document is only ever written
    # with encoding="utf-8" (CLI --out) or printed to a UTF-8 stream, so emitting
    # real non-ASCII is safe — and it must be identical in both dumps below, or
    # the bytes we verified would not be the bytes we emit.
    rendered = json.dumps(_to_jsonable(d), indent=2, sort_keys=True, ensure_ascii=False)
    rendered, reps = sanitize.guard(rendered, pseudo)
    if reps:
        # Disclose the repair in the dump itself so the routing gap surfaces as
        # a reportable bug. Substitution preserved JSON validity — a leaked
        # original is identifier-shaped and its alias is [A-Za-z0-9-], neither
        # side carries quotes or backslashes — so reload-and-extend is safe.
        # backstop_repairs is an optional additive key: absent on a clean dump.
        data = json.loads(rendered)
        data["backstop_repairs"] = dict(reps)
        rendered = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
        sanitize.assert_clean(rendered, pseudo)
    if repairs is not None:
        repairs.extend(reps)
    return rendered


def render_markdown(
    d: Diagnostics,
    *,
    pseudo: sanitize.Pseudonymizer | None = None,
    repairs: list[tuple[str, int]] | None = None,
) -> str:
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
    out.append(_fmt_kv("sys.platform", e.sys_platform))
    out.append(_fmt_kv("win32 on WSL distro path", "yes" if e.win32_on_wsl_path else "no"))
    out.append(_fmt_kv("multiplexer", e.multiplexer))
    out.append(_fmt_kv("tmux", e.tmux_version or "—"))
    out.append(_fmt_kv("git", e.git_version or "—"))
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
        if r.sweeps_refused:
            out.append(
                _fmt_kv(
                    "sweeps_refused",
                    ", ".join(f"`{k}`: {v}" for k, v in r.sweeps_refused.items()),
                )
            )
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
                "| alias | epic | phase | att | rev | committed | spec | dw | sessions "
                "| weighted | raw |"
            )
            out.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for t in r.tasks:
                out.append(
                    f"| `{t.alias}` | {t.epic} | {t.phase} | {t.attempt} | {t.review_cycle} "
                    f"| {t.committed} | {t.spec_present} | {t.dw_count} | {t.n_sessions} "
                    f"| {t.tokens.get('weighted', 0)} | {t.tokens.get('total', 0)} |"
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
    rendered, reps = sanitize.guard(rendered, pseudo)
    if reps:
        note = [
            "",
            "### Backstop repairs",
            "",
            "_The leak self-check caught stray occurrences of pseudonymized "
            "identifiers that the per-field routing missed, and substituted "
            "their aliases — a bmad-loop routing gap; please report it._",
            "",
        ]
        for label, count in reps:
            note.append(f"- `{label}`: {count} stray occurrence(s) pseudonymized")
        note.append("")
        rendered += "\n".join(note)
        # The note is appended after the repair loop verified the body, so
        # re-check the whole thing: the note must sit inside the verified bytes.
        sanitize.assert_clean(rendered, pseudo)
    if repairs is not None:
        repairs.extend(reps)
    return rendered


def _dict_inline(d: dict) -> str:
    if not d:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in sorted(d.items()))
