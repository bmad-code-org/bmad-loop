"""Regression guard against POSIX-only patterns creeping back into the core.

The POSIX-decoupling pass (multiplexer seam + portability fixes) quarantined
every Unix assumption behind a single tmux backend and a handful of
platform-guarded helpers. This guard byte/AST-scans ``src/bmad_loop`` so a new
hard POSIX dependency can't sneak in unnoticed. Each sanctioned exception lives
in an allowlisted file and — outside the wholesale tmux quarantine — carries a
``# portability:`` ack on its line, so exceptions stay deliberate.

The same single-pass scan also carries the non-POSIX quarantines that have the
identical shape: AGENTS.md's "New core env vars register in ``envvars.py``;
plugin-owned env-var families stay with their plugin" — see
``test_bmad_loop_env_reads_only_in_the_registry`` — and its "all git subprocess
calls go through the ``_run_git`` chokepoint in ``verify.py``" — see
``test_no_git_invocation_outside_verify``.

Later invariants ride the same machinery, each one previously held by docstring
prose alone (or by nothing):

* the task-directory artifact names are ``journal.TASK_CYCLE_ARTIFACTS`` and not a
  literal repeated per reader/writer — ``test_task_cycle_artifacts_named_only_through_the_constant``
* a session task id is composed only in ``engine._session_task_id`` —
  ``test_session_task_id_composed_only_at_the_chokepoint``
* every journal field name a call spells is either routed by ``diagnostics``'
  redaction tables — by name, or by name-and-kind — or declared benign:
  ``test_journal_fields_are_routed_or_declared_benign``, with
  ``test_journal_kinds_are_literal_or_the_position_is_declared`` holding the kind
  half readable and ``test_journal_append_writes_only_accounted_fields`` covering
  the two names ``Journal.append`` mints itself, which no call site spells.
* ``runs.rearm_escalation`` is called from exactly two places, each of which consults
  liveness first — ``test_rearm_escalation_called_only_behind_a_liveness_gate``.
* every literal journal KIND is a declared ``JOURNAL_KINDS`` row —
  ``test_journal_kind_inventory_is_complete``.
* every ``_refuse_*``/``_reject_*`` helper definition and every #414-family
  isolation-refusal call site is enumerated —
  ``test_refusal_helper_inventory_is_complete`` and
  ``test_isolation_conflict_refusal_sites_are_enumerated``.

If this test flags something unexpected, fix the source (route it through the
seam / a platform helper) rather than widening an allowlist.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from itertools import product
from pathlib import Path

import pytest

import bmad_loop
from bmad_loop import diagnostics, envvars
from bmad_loop.journal import (
    JOURNAL_FILE,
    SELF_MINTED_FIELDS,
    TASK_CYCLE_ARTIFACTS,
    UNREADABLE_LINE_KIND,
    Journal,
)

SRC = Path(bmad_loop.__file__).resolve().parent
# Marker an allowlisted exception line must carry. Written as ``# portability: …``;
# matched as the bare keyword so it also rides along on a ``# nosec B108 portability: …``.
ACK = "portability:"

# ----------------------------------------------------------------- allowlists

# The files allowed to shell out to ``tmux`` — the whole-file quarantine for
# tmux/POSIX-shell knowledge, split across the shared base (where the spawn
# primitive + argv live) and its POSIX leaf. No per-line ack needed: these files
# *are* the sanctioned spot (their module docstrings say so).
TMUX_BACKENDS = {"adapters/tmux_base.py", "adapters/tmux_backend.py"}

# The one file allowed to build a ``["git", ...]`` argv — and within it, only as
# the argv argument of a ``_run_git(...)`` call, the position where the
# chokepoint (engine-configured timeout, ``LC_ALL=C``, the GitError taxonomy) is
# being FED rather than bypassed. Unlike the tmux quarantine the sanction is not
# the whole file: verify.py is mostly non-chokepoint helpers, and a bare
# ``subprocess.run(["git", ...])`` added to one of them skips all three
# guarantees exactly like a bypass in any other module, so the scanner tags each
# git finding with the call-position bit and ``_git_offenders`` requires both
# halves. Every other module calls a verify helper (``git_bytes``,
# ``worktree_clean``, …) instead of spawning git itself. Both bypasses open when
# this guard landed carried real defects (#390): a strict decode crashing the
# TUI checkpoint modal, and a probe ignoring `limits.git_timeout_s`.
GIT_CHOKEPOINT = {"verify.py"}

# The deferred-work ledger-read contract (DW-146), as a chokepoint. Every read of
# the ledger in `src/bmad_loop` must name its arm — `deferredwork.read_for_write`
# (repair/write) or `deferredwork.read_for_observation` (observation) — so the
# classification lives in the CALL rather than in a comment a future reader may not
# write. Comments were the only thing holding the contract when it landed, which is
# how the pre-DW-146 tree ended up with a dozen sites each guarded, unguarded or
# broadly swallowed on its own reasoning.
#
# The exemptions below are the sites that implement an arm inline because they
# carry behavior the helper cannot, keyed by `(rel, enclosing function)` rather
# than by line so the allowlist survives edits above them:
#
#   * `cli._validate_deferred_ledger` — grades the fault as a validation problem
#     (a warning would exit 0 having evaluated no hard gate at all).
#   * `verify.verify_review_bundle` — turns it into a retryable VerifyOutcome.
#   * `Engine._refuse_gated_story` — journals, notifies, and REFUSES the story.
#   * `Engine._close_declared_deferred` — must split absence from a dangling
#     symlink, which the helper deliberately collapses. (Named for the function that
#     OWNS the read: the DW-146 audit filed this site under its caller,
#     `_finalize_commit_phase`, and this guard is what caught the mislabel.)
#   * `tui.data.deferred_entries` — degrades the pane to "unavailable".
#
# Being on this list buys the FUNCTION its inline read and nothing else: a bare
# read anywhere else in those same files is still an offender.
LEDGER_READ_INLINE = {
    ("cli.py", "_validate_deferred_ledger"),
    ("verify.py", "verify_review_bundle"),
    ("engine.py", "_refuse_gated_story"),
    ("engine.py", "_close_declared_deferred"),
    ("tui/data.py", "deferred_entries"),
}
# Receivers whose `.read_text(...)` is a deferred-work ledger read. `path` and
# `archive_path` are ledger spellings only inside `deferredwork.py`, which is the
# module that owns the file and names its own parameter `path`; elsewhere `path` is
# far too generic to flag, and the ledger travels under the names below.
LEDGER_RECEIVER_NAMES = {"ledger", "ledger_path", "deferred_work"}
LEDGER_OWNER_RECEIVER_NAMES = {"path", "archive_path"}
LEDGER_OWNER = "deferredwork.py"
# The two arms' own bodies: the one place a bare `read_text` of the ledger is the
# point rather than a bypass. The observation arm's body is `observe_ledger`, the
# presence-aware reader `read_for_observation` projects to text (PR #794 review);
# the projection holds no `read_text` of its own.
LEDGER_READER_BODIES = {"read_for_write", "observe_ledger"}

# The one file allowed to CALL ``verify_commands_outcome`` — and within it, only
# from inside ``_verify_review_commands``, the helper that resolves the review
# gates' command cwd to ``paths.repo_root``. Three gates used to call the
# composition directly with ``paths.project``, which is #695; the helper exists so
# they cannot drift apart on that root again, and a fourth gate calling past it
# would silently reintroduce the bug in exactly the same shape. Like the git
# exemption the sanction is a call POSITION, not the whole file: verify.py could
# perfectly well grow another helper that calls the composition with some other
# cwd, and that is the thing being refused.
#
# Deliberately NOT widened to ``run_verify_commands``: that has three legitimate
# callers on two roots (the dev side in ``Workspace.root``, this helper in
# ``repo_root``, and ``cli._reverify``, handed ``repo_root`` by its callers), so it
# is not a chokepoint of this shape and a guard over it would be an allowlist that
# grows with every caller until it means nothing. Said here rather than left
# implied, because "why is only one of the two functions guarded" is the first
# question the next reader will have.
VERIFY_COMMANDS_CHOKEPOINT = {"verify.py"}
VERIFY_COMMANDS_SANCTIONED_CALLER = "_verify_review_commands"

# The other half of the same invariant. Fencing the WRAPPER alone leaves the bug
# fully reachable: a fourth gate can spell the composition by hand —
# `verify_command_results_outcome(run_verify_commands(policy, paths.project),
# paths.project)` — and reintroduce #695 with the wrapper guard silent. That is
# also the likely way one gets written, because `Engine._verify_commands_with_results`
# already spells exactly that composition inline, so it is the shape a new gate
# would be copied from.
#
# Two sanctioned positions, keyed file -> the ONE enclosing function, because the
# two are different functions in different modules: `verify_commands_outcome` is
# the review/CLI composition point, `_verify_commands_with_results` the dev side's
# (which must keep its own spelling — it retains the results for the hook payload
# between the two calls, which is the whole reason it does not use the wrapper).
#
# Deliberately NOT extended to `run_verify_commands`: the spec forbids it, and its
# three callers legitimately run on two different roots, so a guard there would be
# an allowlist that grows with every caller until it means nothing.
VERIFY_CLASSIFY_CHOKEPOINT = {
    "verify.py": "verify_commands_outcome",
    "engine.py": "_verify_commands_with_results",
}

# Files where resolving a raw `task.spec_file` / `task.dispatched_spec_file` with a
# bare `Path(...)` is CORRECT, because the reader runs inside the tree the value was
# recorded against. `runs.py` is the chokepoint itself; `engine.py`, `verify.py` and
# `recovery_flow.py` are in-process consumers driving a live run, where the field is
# still the absolute path the engine stamped and no reload has round-tripped it
# through `StoryTask.to_dict`.
#
# Everywhere else the field arrives from `load_state`, and
# `_serialized_worktree_path` persists an isolated unit's spec RELATIVE to the mount.
# A bare `Path(...)` there resolves against the READER's cwd — the main checkout,
# which carries the same implementation-artifacts-relative path and answers with the wrong
# tree's copy. That defect shipped in `tui/app.py::_paused_spec`, where it reached a
# destructive write, and was then re-found one surface at a time in `resolve.py`,
# `sweep.py`, `stories_engine.py` and `worktree_flow.py` across four review rounds.
# Nothing enforced the rule, which is why each round only ever found the next one.
#
# Adding a file here is a claim that its cwd IS the run's tree. If it is not, route
# the read through `runs.task_spec_path` (or `StoryTask.rebase_spec_paths_on` when
# re-anchoring persisted state) instead.
SPEC_ANCHOR_CHOKEPOINT = {"runs.py", "engine.py", "verify.py", "recovery_flow.py"}
SPEC_PATH_FIELDS = {"spec_file", "dispatched_spec_file"}

# ``(file, name)`` of the ONE assignment that may spell the task-directory artifact
# names as literals: ``journal.TASK_CYCLE_ARTIFACTS`` itself. Constants inside that
# assignment's value are the definition, not a copy, so the scan skips them — the
# position idiom the git and verify exemptions use, rather than an allowlist entry
# that would also wave through a bare literal anywhere else in journal.py.
#
# Paired with the FILE on purpose: the same tuple re-declared in another module is a
# second copy, which is exactly what the guard exists to refuse.
TASK_ARTIFACT_DEFINITION = ("journal.py", "TASK_CYCLE_ARTIFACTS")

# ``rel -> enclosing function -> the artifact names it may still spell as a bare
# literal``. Keyed by FUNCTION as well as by file — ``VERIFY_CLASSIFY_CHOKEPOINT``'s
# idiom — because the sanction is a POSITION: a second bare `"result.json"` grown
# anywhere else in `adapters/generic.py` would inherit a file-keyed exemption on its
# path alone, which is both the drift the guard exists to catch and the thing this
# comment used to claim was already impossible.
#
# Scoped by NAME inside that, for `ENV_READ_ALLOW`'s reason: being the sanctioned
# position buys `_result_path` the one name it declares and nothing wider.
#
# `adapters/generic.py::_result_path` is the one sanctioned single-name read: it
# answers "where does THIS task's result.json live", a genuinely single-artifact
# question that folding into the loop would not express. It carries no claim about
# `escalation.json`, so that name stays refused inside it.
TASK_ARTIFACT_LITERAL_ALLOW = {
    "adapters/generic.py": {"_result_path": frozenset({"result.json"})},
}

# The one file allowed to COMPOSE a session task id, and within it only inside
# ``_session_task_id`` — keyed file -> the ONE enclosing function, like
# ``VERIFY_CLASSIFY_CHOKEPOINT``. Every mint site (`engine.py` ×3, `resolve.py`)
# calls it; none spells the format itself.
#
# The sanction is a POSITION, not the file: engine.py is where a fifth mint would
# most naturally be written (it already binds `task_id` three times), so a file-wide
# exemption would leave the invariant unguarded exactly where it matters. The
# function's own docstring states why every caller must be byte-identical —
# ``_resumable_session``'s resume match, and the ``-g<N>`` re-arm discriminator that
# a hand-rolled fourth mint would omit (#705).
SESSION_TASK_ID_CHOKEPOINT = {"engine.py": "_session_task_id"}

# The complete set of ``runs.rearm_escalation`` call sites, as
# ``(file, enclosing function)``. Serialization now comes from the shared run-state
# lock, not this liveness inventory. The gates remain independently load-bearing: a
# serialized control command still must not take its turn after an engine known to be
# live, and a new operator surface must make that policy explicit.
REARM_ESCALATION_CALLERS = {
    ("cli.py", "cmd_resolve"),
    ("tui/app.py", "_do_rearm"),
}

# Every production state publication and every explicit multi-step state transaction.
# ``save_state`` itself serializes the leaf write, so a new direct publisher is safe
# from the fixed-temp collision but still appears here for review: if it reads state
# before deciding what to publish, it also belongs in RUN_STATE_TRANSACTIONS with an
# outer hold. Exact inventories make a newly added writer fail loudly instead of
# relying on a reviewer to find it by grep.
SAVE_STATE_CALLERS = {
    ("cli.py", "_prepare_resume_locked"),
    ("engine.py", "_save"),
    ("runs.py", "_rearm_escalation_locked"),
    ("runs.py", "restamp_code_root"),
    ("runs.py", "_stop_run_once"),
    ("runsetup.py", "compose_run"),
    ("runsetup.py", "compose_sweep"),
}
RUN_STATE_TRANSACTIONS = {
    ("cli.py", "_resume_paused_run"),
    ("cli.py", "cmd_resolve"),
    ("journal.py", "save_state"),
    ("runs.py", "rearm_escalation"),
    ("runs.py", "restamp_code_root"),
    ("runs.py", "_stop_run_once"),
    ("runs.py", "archive_run"),
    ("runs.py", "delete_run"),
    ("runsetup.py", "compose_run"),
    ("runsetup.py", "compose_sweep"),
    ("tui/app.py", "_do_rearm"),
}

# The two refusal surfaces review iteration 6 kept re-finding by hand, enumerated so
# a NEW one reddens CI until its row lands — the row being the PR-time decision
# whose failure message demands the covering test land beside it (the journal-kind
# inventory below is the third surface of that shape).
#
# Every `_refuse_*` / `_reject_*` helper DEFINITION in the tree, as
# `(file, def name)`. The prefix pair is the tree's whole refusal-helper naming
# convention today; a helper named outside it is invisible here — a stated bound,
# not coverage. The guard forces the decision only on names that claim to be
# refusals, and deliberately adds no runtime abstraction (no RefusalError, no
# registry): the inventory is the test file's, not the product's.
REFUSAL_HELPER_DEFS = {
    ("cli.py", "_reject_bad_run_id"),
    ("cli.py", "_reject_isolation_conflict"),
    ("cli.py", "_reject_under_floor_git"),
    ("engine.py", "_refuse_gated_story"),
    ("platform_util.py", "_refuse_unwritable_target"),
    ("platform_util.py", "_refuse_unwritable_target_at"),
    ("resolve.py", "_reject_json_constant"),
    ("runs.py", "_refuse_live_session"),
    ("runs.py", "_refuse_uncontained_run_dir"),
    ("win32_at.py", "_refuse_link"),  # ELOOP for a symlink/junction under O_NOFOLLOW
    ("workspace.py", "_refuse_foreign_checkout"),
    ("worktree_flow.py", "_refuse_integrated_artifacts"),
    ("worktree_flow.py", "_refuse_refused_residue"),
}

# Every #414-family call site — `bmadconfig.worktree_isolation_conflict`, sole
# producer of the isolation-under-repo-root refusal text, plus its rc-returning CLI
# wrapper `_reject_isolation_conflict` — as `(file, enclosing function) -> count`.
# The `REARM_ESCALATION_CALLERS` idiom WITH multiplicity, because `cmd_resolve`
# legitimately calls the wrapper twice: post-confirm is the authority, and the
# pre-session arm spares the operator a full interactive session on a pair knowable
# from config — the `96aa09a9` fix, which landed with no structural gate naming it.
# Accepted cost (human decision 2026-09-02): every future caller of the predicate
# touches a row here in the same PR.
ISOLATION_CONFLICT_CALLERS = {
    ("cli.py", "_reject_isolation_conflict"): 1,  # the wrapper's own predicate call
    ("cli.py", "cmd_run"): 1,
    ("cli.py", "cmd_sweep"): 1,
    ("cli.py", "cmd_resolve"): 2,  # pre-session + post-confirm re-read
    ("cli.py", "cmd_validate"): 1,  # reports a Finding rather than aborting
    ("cli.py", "_prepare_resume_locked"): 1,  # behind both `resume` and the re-arm
    ("cli.py", "_warn_preflight_would_abort"): 1,  # the dry-run honesty banner
    ("cli.py", "factory"): 1,  # `_sweep_factory`'s closure: raises — no rc channel
    ("tui/app.py", "_guarded"): 1,  # the pre-launch toast guard
    ("tui/app.py", "_do_rearm"): 1,
}

# What counts as consulting liveness, matched as a substring of the callee's name
# because the two sites legitimately spell it differently and neither spelling is more
# correct: the CLI calls ``runs.engine_liveness`` directly, the TUI goes through
# ``self._resolve_blocked_by_liveness`` (which reaches ``runs.liveness``, the pid-file
# sibling sharing ``probe_liveness``). Pinning either exact name would redden on a
# rename that changes nothing, while the substring still reddens on the deletion this
# guard exists for.
#
# What the gate establishes is that the engine is not PROVABLY alive, not that it is
# proven dead — ``"unknown"`` proceeds under ``--force`` in ``cmd_resolve``, and the
# TUI counts it as blocking only for a pid-backed run. This guard therefore grades
# that the result controls a terminating branch before the call; the caller-level
# tests pin the exact alive/unknown policy on the two real surfaces.
LIVENESS_GATE_MARK = "liveness"

# The journal field names ``diagnostics`` routes BY NAME, read off the live module
# rather than copied, so the guard cannot drift from the tables it grades: add a row
# there and the corresponding producer stops being an offender with no edit here.
# Three tables, because these three are the by-name routing decisions — an alias, a
# drop, or a key-list reduction. Anything else falls through to
# ``sanitize.scrub_json``, which fails closed only by accident of a value's shape.
#
# ``_JOURNAL_KIND_ALIAS_FIELDS`` is deliberately NOT flattened in here. It routes by
# ``(kind, name)``, and folding it into a by-name union says ``target`` is routed
# everywhere — including on the ``board-advance-*`` family, where that module's own
# comment says by-name routing would be WRONG. Flattened, the guard read
# ``journal.append("unit-merge-failed", target=branch)`` — a NEW kind reusing the
# name — as routed, while ``_scrub_entry`` handed it to ``scrub_json`` and shipped
# the branch verbatim. See ``JOURNAL_KIND_ROUTED_FIELDS`` for the scoped form.
JOURNAL_ROUTED_FIELDS = (
    frozenset(diagnostics._JOURNAL_ALIAS_FIELDS)
    | diagnostics._JOURNAL_DROP_FIELDS
    | diagnostics._JOURNAL_KEYLIST_FIELDS
)

# Every journal KIND that carries a ``patch`` field. Declared because
# ``_JOURNAL_DROP_FIELDS`` routes BY NAME, so the drop reaches every kind spelling the
# name — while that entry's comment described one pair of records and nothing pinned
# the true reach. A by-name rule whose comment names a subset is how a reader concludes
# an unlisted kind is unrouted and starts journalling a path there expecting the
# fallback to redact it.
#
# Inventory, not derivation: read off the producers by hand and asserted equal to the
# scan, so a FURTHER kind picking up the field is a decision someone makes here rather
# than a silent widening of a routing rule that already covers it.
#
# Two other surfaces enumerate these same kinds and neither reddens on its own when
# this one changes: ``diagnostics._JOURNAL_DROP_FIELDS``' ``patch`` comment, and
# ``tests/test_diagnostics.py::_PATCH_PATH_ROUTING_ROWS``, which asserts the drop per
# kind at the routing seam. Adding or removing a kind here means updating both.
JOURNAL_PATCH_KINDS = frozenset(
    {
        # recovery_flow.py — the intent-gap restore pair.
        "attempt-restore-failed",
        "attempt-restored",
        # runs.py — the operator-selected stale-restore pair.
        "stale-restore-unparseable",
        "stale-restore-excluded",
        # worktree_flow.py — the retained forensic patch of a closed unit.
        "unit-closed",
    }
)

# ``kind -> the field names routed on THAT kind only``, read off the same module so
# the guard still cannot drift from it. Alias, identifier-list, and count-list rules
# share this inventory because all three claim the same `(kind, field)` boundary.
JOURNAL_KIND_ROUTING_TABLES = (
    diagnostics._JOURNAL_KIND_ALIAS_FIELDS,
    diagnostics._JOURNAL_KIND_KEYLIST_FIELDS,
    diagnostics._JOURNAL_KIND_COUNTLIST_FIELDS,
)
JOURNAL_KIND_ROUTED_FIELDS: dict[str, frozenset[str]] = {}
for _routing_table in JOURNAL_KIND_ROUTING_TABLES:
    for _kind, _row in _routing_table.items():
        JOURNAL_KIND_ROUTED_FIELDS[_kind] = JOURNAL_KIND_ROUTED_FIELDS.get(
            _kind, frozenset()
        ) | frozenset(_row)

# ``kind -> field names declared benign on that kind alone`` — the kind-scoped twin of
# ``JOURNAL_BENIGN_FIELDS`` for overloaded names whose other shapes are routed.
# ``engine``'s board-advance carry paths journal ``target`` carrying a sprint STATUS
# ("done"), not a branch; ``diagnostics``' ``_JOURNAL_KIND_ALIAS_FIELDS`` comment is
# explicit that aliasing those would destroy the field a maintainer reads the record
# for. Declared per kind rather than by adding ``target`` to the by-name benign set,
# which would also wave through a branch-carrying ``target`` on a kind nobody has
# looked at — exactly the hole the flattening left.
JOURNAL_KIND_BENIGN_FIELDS = {
    "board-advance-carried": frozenset({"target"}),
    "board-advance-carry-failed": frozenset({"target"}),
    "board-advance-carry-foreign-dirt": frozenset({"target"}),
    "board-advance-carry-refused": frozenset({"target"}),
    "board-advance-carry-uncommitted": frozenset({"target"}),
    # The stale-restore record carries SHA strings under this name and is routed;
    # this recovery notice carries only the already-derived integer count.
    "rollback-manual-required": frozenset({"commits"}),
}

# Every OTHER field name journalled today: a declared inventory, not a per-name
# audit. Nobody has argued each of these is safe unrouted; what the list records is
# that they are the set that existed when the guard landed. That is the whole claim,
# and it is worth making — field name #132 cannot appear without someone deciding
# whether it needs routing, which is the decision DW-82 measured nothing forcing.
#
# ⚠️ Adding a name here is that decision, made in the "no routing needed" direction.
# Make it deliberately: a name carrying a story key, a branch, a sha, a spec
# filename, a path, or free text belongs in a `diagnostics` table instead. Adding a
# routing row there for a field that does not need one is equally wrong — it would
# pseudonymize a value a maintainer reads the record for (see
# `_JOURNAL_KIND_ALIAS_FIELDS`' `target` for that failure in the other direction).
#
# ⚠️ STATED BOUND, so nobody reads more into this than it says: the guard catches a
# rename OUT of the tables into unclaimed space — the measured `patch` → `patch_path`
# ablation. It does NOT catch a rename INTO a name one of these sets already holds.
# Respell `recovery_flow.py`'s `patch=` as `path=`, `ref=` or `name=` and every
# assertion here stays green while the value stops being dropped, because the guard
# grades the NAME against a set and all three of those names are in it. Only
# `tests/test_diagnostics.py` can see that, and only if it has a row for the record.
JOURNAL_BENIGN_FIELDS = frozenset(
    {
        "action",
        "actions",
        "adapter",
        "adapter_dev",
        "adapter_review",
        "already_resolved",
        # `sweep-decision-option-mismatch`'s caller discriminator: the STORED
        # answer's own effect, a closed `DECISION_EFFECTS` value (build/close/
        # keep-open) taken from the answer, never authored text. Both lanes of
        # `_materialize_bundles` run one agreement helper and write this kind
        # through it (DW-123), so this is what separates a discarded `build`
        # option from a discarded `keep-open` one. THREE producers since DW-167,
        # and so three reachable values: `_decisions_phase`'s re-apply walk
        # resolves a stored `close` through the same helper, which is the only
        # way `close` reaches this field.
        "answer_effect",
        "attempt",
        "blocked",
        "blocking",
        "budget",
        "budget_mode",
        "budget_weighted",
        "bundles",
        "bundles_not_run",
        "cache_read_weight",
        "cache_read_weight_was",
        "cap",
        "checkout_dirty",
        "checkpoint",
        "code_root_changed",
        "command_index",
        # `accepted-spec-write-unreachable`'s discriminator: whether the byte
        # comparison COMPLETED, not what it found. A bare boolean deliberately —
        # `reason` and `error`, the natural spellings for "the read failed", are in
        # `diagnostics._JOURNAL_DROP_FIELDS` and would ship as a presence marker.
        "compared",
        "condition",
        "contradiction",
        "converted",
        "count",
        "cycle",
        "cycles",
        "decision",
        "decisions",
        "deduped",
        # Written by `runs.restamp_code_root`'s TRAILING append alone — one of the
        # three producers of `rearm-code-root-restamped`, not a predicate over the
        # kind (the two discharge rows omit it, asserting `code_root_changed=true`
        # instead). There it says whether that row settles a record owed by an EARLIER
        # call's move. A bare boolean about the ROW's own role — it names no root, no
        # run and no path; the tree is `repo`, which
        # `diagnostics._JOURNAL_DROP_FIELDS` already reduces to a presence flag.
        "discharged_owed_move",
        # `sweep-decision-answer-dropped`'s discriminator: WHICH drop lane fired, as
        # a closed five-value enum (`effect-unlanded` | `entry-not-open` |
        # `no-intent` | `name-collision` | `stale-option`). `stale-option` is the
        # keep-open lane's
        # (DW-123) and covers both of its failures — a renumbered option and a
        # vanished one — because only the first can also write a
        # `sweep-decision-option-mismatch`, so the cause cannot be named for the
        # mismatch alone. `effect-unlanded` is DW-200's: a `build` answer this run
        # recorded while `record_decision` reported writing no `decision:` line, so
        # the ledger holds no entry to build against. It is the build lane's half of
        # the discipline DW-186 gave the close lane, and it names the NON-WRITE
        # rather than the answer — the answer itself is intact and re-askable, which
        # is why the entry is left alone the way `no-intent`'s is.
        # `entry-not-open` is DW-214's: the build lane read the ledger's LIVE open
        # set before minting a bundle and the id is not in it. It names the ledger
        # FACT — no entry at all, or an entry no longer open — rather than either
        # cause of it, because the screen cannot tell the two apart and neither
        # changes what the lane does. Distinct from `effect-unlanded` because that
        # one names a non-write this run OBSERVED and is populated at the interactive
        # prompt arm alone, where this one is a fresh read that also covers an answer
        # adopted from the project store or reloaded on a resume; an id in both is
        # reported as `effect-unlanded`, the older and more specific verdict.
        # Second producer: `sweep-decision-preanswer-pruned` (DW-143), which carries
        # the cause of the drop it belongs to — the same enum, though only the
        # keep-open lane prunes, so in practice only `stale-option` reaches it.
        # A closed enum deliberately — `reason` and `error`, the natural spellings
        # for "why was it dropped", are in `diagnostics._JOURNAL_DROP_FIELDS` and
        # would ship as a presence marker instead of the distinction the record
        # exists to draw, and a free-text spelling would be the one place triage
        # prose could enter this record.
        "drop_cause",
        "dropped",
        "dw_id",
        "effect",
        "entries",
        "entries_now",
        "env_fault",
        "env_fault_evidence",
        "epic",
        "errors",
        "expired_clock",
        "failed",
        "fallback",
        "field",
        # `_commit_ledger`'s three rows (DW-192): WHICH of the two published files
        # the row is about, as a LEXICAL basename (`path.name`). Benign because it
        # is a code constant at both publisher families — `deferred-work.md` from
        # `ProjectPaths.deferred_work`, `decisions.json` from `decisions.STORE_REL`
        # — so no operator text can reach it. Explicitly NOT the RESOLVED tail:
        # DW-188 follows a ledger symlink to a target the operator named, so
        # `target.name` is arbitrary text of exactly the identifier shape
        # `sanitize.scrub_json` ships verbatim, and blessing it here would
        # pre-approve that text. Minted because the row's other identifiers are
        # gone from a dump: `repo`, `message` and `error` are all in
        # `diagnostics._JOURNAL_DROP_FIELDS`, so a scrubbed dump named no file at
        # all. Not a path — the directory is `repo`, which stays dropped.
        "file",
        "finished",
        "fired_at",
        "flat_remainder",
        "followup_damped",
        "followup_review_recommended",
        "frm",
        "generation",
        "graceful",
        "harvest_attempt",
        "head",
        "id_collisions",
        # `session-idle` / `session-active` (#680): seconds the live transcript has
        # sat still — a float the adapter measured from two stats, no identifier.
        "idle_s",
        "items",
        "kept",
        "key",
        # `sweep-decision-option-mismatch`'s label clause, as a BARE BOOLEAN. The
        # record exists to say a stored decision answer no longer describes the
        # option its key resolves to, and the natural spellings of that — the two
        # labels, the decision's question — are triage prose an LLM authored about
        # the customer's own backlog, so shipping them here would push that prose
        # into every diagnostics dump. The boolean says which clause fired
        # (`False` label, `True` effect-only) and names nothing.
        "label_matched",
        "ledger",
        # `accepted-spec-delivery-unreachable`'s discriminator: whether the locator
        # RESOLVED a project-local rel, or only reported a swallowed filesystem
        # fault. A bare boolean deliberately, exactly like `compared` above —
        # `reason` and `error`, the natural spellings for "which refusal was it",
        # are in `diagnostics._JOURNAL_DROP_FIELDS` and would ship as a presence
        # marker instead of the distinction the record exists to draw. Benign
        # rather than routed: a boolean names no customer artifact, and the paths
        # it discriminates ride `spec_file`, which IS routed.
        "located",
        "log_pos",
        "malformed",
        # `artifact-publication-refused` size-admission diagnostics. The two
        # counts are raw byte totals derived by the bounded publication reader,
        # and `measurement_is_lower_bound` is the bool saying the count stopped
        # at the limit (the reader is bounded, so a growing file is measured "at
        # least"); none is authored text or an identifier, and the refused path
        # remains inside dropped `error`.
        "limit_bytes",
        "measured_bytes",
        "measurement_is_lower_bound",
        "mode",
        "model",
        "name",
        "next",
        "normalized",
        "ok",
        # `old_baseline` is NOT here any more: it moved to `_JOURNAL_ALIAS_FIELDS`
        # (the `commit` namespace) once a second producer —
        # `rearm-commits-probe-failed` — forced the decision this set's own warning
        # describes, and on the same footing as the `question` note above: it was a
        # live leak, just an intermittent one. Unrouted, a real 40-hex sha usually
        # collapses to `<redacted:secret>` at `_scrub_str`'s secret check — but only
        # usually. Real shas straddle that bar, and about one in twenty-five sampled
        # from this repo's own history ships VERBATIM. Routing also restores the
        # correlation the alias table exists to preserve: even on the shas the
        # fallback does catch, `<redacted:secret>` left the two records naming one
        # baseline unable to be seen as naming the same one. Left as a note rather
        # than a silent deletion, because a name leaving this set is the guard working
        # — a benign declaration that turned out to be wrong.
        "open",
        "open_now",
        # `sweep-decision-option-mismatch`'s other discriminator: the CURRENT
        # option's effect, a closed enum (`DECISION_EFFECTS`: build/close/
        # keep-open), so it carries no authored text. Read beside `answer_effect`
        # above, which is the STORED answer's: the record used to be written from
        # one lane, where the answer's own effect was invariably "build" and
        # discriminated nothing; since DW-123 two lanes share the site, and since
        # DW-167 the re-apply walk is a third.
        "option_effect",
        "original",
        "owed_after_implement",
        "phase",
        "pid",
        "platform",
        "plugin",
        "plugins",
        "policy_changed",
        "preserve_ref",
        "problem",
        # `dev-decision` and `session-end` (#727): whether the session changed its
        # pane after the first frame or ended a turn. A bare boolean about the
        # verdict — it names no story, no path and no text, and `False` is what
        # routed the result to the no-work PAUSE.
        "produced_work",
        # A closed two-value enum (`file-limit` | `payload-limit`) emitted only
        # for measured artifact publication admission refusals. The arbitrary
        # path and exception prose ride `error`, which diagnostics drops.
        "publication_cause",
        # `question` is NOT here any more: it moved to `_JOURNAL_DROP_FIELDS`
        # (schema v3) once a one-token `decision-pending` question was shown to
        # ship verbatim. Left as a note rather than a silent deletion, because a
        # name leaving this set is the guard working — a benign declaration that
        # turned out to be wrong.
        "rc",
        "re_review_capped",
        "reaches_redrive",
        "rearmed",
        "record",
        "redrive",
        "ref",
        "refiled",
        "refs",
        # `sweep-ledger-commit-refused` (DW-199/203/205): WHY `_commit_ledger`
        # declined to publish its target, as a closed FOUR-value enum
        # (`target-absent` | `target-unreadable` | `target-not-a-file` |
        # `target-undecodable` — the third added by DW-211/228 for a store replaced
        # by a directory, the fourth split out of `target-unreadable` by the DW-237
        # resolution so a ledger's DURABLE decode fault is told apart from a
        # TRANSIENT OS fault a probe raised), all literals in
        # `verify.unpublishable_target` — lifted out of `sweep.py` by DW-209/213, which
        # gave `decisions.apply_pre_answer`'s out-of-band commit the same guard; that
        # caller has no journal and carries its refusal on its return value instead.
        # Minted for the reason `stop_cause`, `drop_cause` and `regen_cause` below
        # were — the natural spelling is `reason`, which sits in
        # `diagnostics._JOURNAL_DROP_FIELDS` and ships as a presence boolean, which
        # would collapse the two causes into one indistinguishable row. Names no
        # path, identifier or prose; the decode/OS fault rides in `error` beside it,
        # which is dropped.
        "refuse_cause",
        "refused",
        # `sweep-intent-regenerated` (DW-164): which of `missing` /
        # `dw-ids-mismatch` / `unreadable` made `_ensure_bundle_intent` rebuild a
        # bundle intent document. A closed enum for the same reason `drop_cause`
        # above is one — `reason` sits in `diagnostics._JOURNAL_DROP_FIELDS` and
        # would ship as a presence boolean, erasing the distinction the field
        # exists to draw. Names no path, identifier or prose.
        "regen_cause",
        "remaining",
        "reset_from",
        "restore",
        "returncode",
        "role",
        # The outcome of an aborted re-arm's spec rollback (`rearm-aborted`), one of
        # FOUR literal enum strings the producer chooses (`restored`, `unchanged`,
        # `unknown`, `failed`). Benign rather than routed:
        # it names no customer artifact and IS the field both operator surfaces read
        # the record for, so an alias would destroy it (the failure
        # `_JOURNAL_KIND_ALIAS_FIELDS`' `target` row documents in the other direction).
        "rollback",
        "run_id",
        "run_type",
        "security_config_changed",
        "seen_again",
        "sentinel_kind",
        "session_status",
        "session_vanished",
        "signum",
        # `session-idle` (#680): the wall timestamp the idle stretch began — the
        # `ts`-shaped float the TUI ages the `· idle <age>` text from.
        "since_ts",
        "site",
        "skip",
        "source",
        "spec_folder",
        "stage",
        "state_kind",
        "status",
        "stderr_bytes",
        "stderr_captured_bytes",
        "stderr_truncated",
        "stdout_bytes",
        "stdout_captured_bytes",
        "stdout_truncated",
        # `sweep-repeat-done`'s stop discriminator (DW-201): WHICH of the repeat
        # loop's seven stops fired, as a closed seven-value enum (`no-open` |
        # `no-progress` | `max-cycles` | `legacy-appeared` | `ledger-unreadable` |
        # `ledger-inaccessible` | `no-selected`), every one of them a literal in
        # `sweep.py`. Minted for the reason `drop_cause` and `regen_cause` above
        # were: the natural spelling is `reason`, which sits in
        # `diagnostics._JOURNAL_DROP_FIELDS` and ships as a presence boolean,
        # collapsing all seven stops into one indistinguishable
        # row. `reason` is still written beside it, unchanged, carrying the same
        # token — this field adds a surviving copy, it does not replace one.
        "stop_cause",
        "strategy",
        "teardown_s",
        # `session-idle` (#680): the grace the crossing was judged against —
        # `limits.dev_stall_grace_s` as a float, so the record is comparable to a
        # stall without the policy snapshot in hand.
        "threshold_s",
        "to",
        "tokens",
        "tokens_weighted",
        "total",
        "trigger",
        "verification_sequence",
        "verification_stage",
        "via",
        "weighted",
        "workflow",
        "worktree",
        "zero_diff",
    }
)

# Field names NO call site spells as a keyword, because ``Journal.append`` mints them
# itself: ``entry.setdefault("log_task", …)`` and ``entry.setdefault("log_pos", size)``
# on every entry written while a pane log is active. ``log_task`` is routed (a story
# alias); ``log_pos`` is a byte offset and is declared benign above.
#
# The static scan reads CALL SITES, so it cannot see either of them — which means the
# sibling guard's "every field name a journal producer writes" claim is true only of
# the fields a call spells. ``test_journal_append_writes_only_accounted_fields``
# closes that from the other side by RUNNING an append and reading the entry back;
# this set is what stops the staleness check below from calling ``log_pos`` dead.
#
# READ FROM ``journal``, not restated: ``diagnostics._scrub_entry`` exempts the same
# pair from the fail-closed arm it applies to a declared-schema kind, and a literal
# copy here would let this guard and that exemption drift apart silently — which is
# the failure mode DW-82 exists to remove, applied to the guard itself.
JOURNAL_SELF_MINTED_FIELDS = SELF_MINTED_FIELDS

# ``(file, enclosing function)`` of every ``journal.append(**name)`` whose keys are
# NOT statically resolvable, mapped to HOW MANY UNRESOLVED ``**`` KEYWORD ARGUMENTS
# that position holds. An unresolved splat is a HOLE in the inventory above — the
# guard cannot tell whether a new field arrived through it — so it fails loud and each
# hole is declared here, with the field names it lets through and the reasoning for it
# on the sibling ``JOURNAL_SPLAT_FIELDS`` below. A new splat site anywhere else reddens
# the guard until someone either makes its keys resolvable or adds a line here.
#
# All four positions are unresolvable for the same structural reason: the dict is
# not built from literals in the calling function.
#
# ⚠️ THE UNIT IS ONE UNRESOLVED `**` KEYWORD ARGUMENT — never one journal write call,
# and never one call site. The number is the count of `field is None` findings at the
# position, which the scan emits once per `**` keyword whose keys the resolver could
# not read: `journal.append(kind, **a, **b)` counts 2 on its own, and a position
# holding two one-splat calls counts 2 as well. It is not the FIELDS that flow through
# the hole either — that is `JOURNAL_SPLAT_FIELDS`' axis.
#
# The finer of the two readings ON PURPOSE. The waiver is granted per POSITION, so
# before the count existed a SECOND splat dropped inside an already-declared position
# was waived on arrival and its field names escaped the inventory with the suite green
# — the exact shape DW-138 retired on `JOURNAL_DYNAMIC_KIND_ALLOW`, on a table that
# already held two writes at one position. That second splat must redden whether it
# arrives as a new CALL or as a second `**` inside an existing one, and a call-shaped
# unit cannot see the latter: `append(kind, **a, **b)` would stay at 1 while a second
# dict's worth of unreadable names started flowing through the same hole.
# `_journal_measured_splats` is the one definition of the unit,
# `_journal_field_offenders` grades it at the offending LINES, and
# `test_journal_field_guard_actually_saw_the_producers` grades it as NUMBERS in both
# staleness directions; that test's docstring says why the answer appears twice.
#
# SEPARATE from `JOURNAL_SPLAT_FIELDS` rather than a richer value, for the reason
# `JOURNAL_DYNAMIC_KIND_SPELLINGS` is separate from the count it accompanies: the int
# values feed derived count-drift probe rows, the consumers outside the field guard
# read the FIELD sets by subscript, and the two axes answer different questions. A
# further unresolved `**` argument inside the position moves the count, a new key
# inside the splatted dict moves the fields.
#
# ⚠️ STATED BOUNDS. The key holds a BARE function name, not a qualified
# `class.method`, so two same-named journal-writing functions in ONE module aggregate
# into a single row: their unresolved `**` arguments sum into one count and their
# fields into one set, and a splat moving between them reddens nothing. No such pair
# exists today, and the assumption is enforced rather than trusted — the scan emits
# each journal write's enclosing DEF identity, `_journal_bare_name_collisions` reads
# it, and `test_journal_writers_do_not_share_a_bare_name` reddens on the day the pair
# arrives. Qualifying the key is deliberately DEFERRED (DW-152), not overlooked: the
# bound is stated so the next reader inherits the decision, not the surprise.
JOURNAL_SPLAT_ALLOW = {
    # One unresolved `**streams` argument, on the write that closes the
    # verify-command entry.
    ("engine.py", "_journal_verify_command_results"): 1,
    # One unresolved `**pref` argument, on the write that records an escalation.
    ("engine.py", "_review_and_commit"): 1,
    # TWO unresolved `**self._session_end_extras(result)` arguments, one on each of
    # two writes — the normal session-end path and the `finally` fallback that runs
    # when the normal one did not reach. Both splat the SAME method's dict, which is
    # why one field inventory covers the pair; the count is the only thing that can
    # see a third arrive, whether as a third write or as a second `**` on one of
    # these two.
    ("engine.py", "_run_session"): 2,
    # One unresolved `**fields` forward: the forwarder's own hole.
    ("plugins/bus.py", "_log"): 1,
}

# The FIELD names that actually flow through each declared hole above, keyed by the
# same ``(file, enclosing function)`` position. The VALUES are an inventory read off
# the producer, not an assertion the scan can check — they are what keeps the
# staleness check on ``JOURNAL_BENIGN_FIELDS`` from calling a splat-borne name dead,
# and they are the honest answer to "which names does this hole let through".
#
# Co-extensive with `JOURNAL_SPLAT_ALLOW` by ASSERTION, not by convention: every
# declared hole has a field inventory (possibly empty) and every inventory has an
# unresolved-`**`-argument count.
# `test_journal_splat_tables_declare_the_same_positions` holds the two key sets
# equal, because splitting one table into two introduces a way to drift that no
# other row here would catch.
#
# Same position-key bound as the count table — a BARE function name — with the same
# collision guard behind it; see `JOURNAL_SPLAT_ALLOW`'s ⚠️ STATED BOUNDS block.
JOURNAL_SPLAT_FIELDS = {
    # `streams` keys are computed — `f"{kind}_path"` and its three siblings over a
    # fixed (stdout, stderr) loop — so the resolver cannot read them and the argument
    # for the hole is the POSITION. Said plainly because the previous comment argued
    # by VALUE TYPE ("numbers and booleans") while the invariant it exempts is
    # NAME-based: the two `*_path` names are routed (`_JOURNAL_DROP_FIELDS`); the
    # other six are declared benign BY NAME, below. ⚠️ A NEW key added inside this
    # `streams` dict is still invisible to the guard — that is what the hole IS, and
    # no property of its value changes it.
    ("engine.py", "_journal_verify_command_results"): frozenset(
        {
            "stdout_path",
            "stderr_path",
            "stdout_bytes",
            "stderr_bytes",
            "stdout_captured_bytes",
            "stderr_captured_bytes",
            "stdout_truncated",
            "stderr_truncated",
        }
    ),
    # `pref` comes from `preference_escalations(result_json)` — LLM-authored keys out
    # of a session's own result.json. Not statically knowable in principle, not just
    # in this scan, so the OFF-SCHEMA half of this hole can never be inventoried.
    #
    # The three names below are the half that can: they are the record's declared
    # schema, and they are the names whose VALUES still reach the dump (everything
    # else on this kind collapses to a presence marker). Asserted against
    # `diagnostics._JOURNAL_KIND_SCHEMAS` by
    # `test_journal_routing_tables_are_read_from_diagnostics`, so this inventory and
    # that table cannot disagree.
    #
    # What covers it is `diagnostics._JOURNAL_KIND_SCHEMAS`, which declares
    # `preference-escalation`'s record to be `{type, severity, detail}` and collapses
    # every other key on that kind to `<name>_present`. This comment used to say the
    # REDACTION FALLBACK covered it, which was verified false: `scrub_json` is the
    # IDENTITY on an identifier-shaped scalar, so `customer="AcmeVault"` came back
    # byte-identical while this allowlist entry read as accounted for. A comment that
    # names the wrong mechanism is how the next reader concludes a hole is closed
    # when it is not.
    #
    # The hole this entry declares is therefore narrower than it looks, and it is
    # still a hole: the key NAMES remain LLM-authored and still reach the dump as
    # `<name>_present` markers. That residual was weighed against a name-free
    # `unrouted_field_count` collapse and DELIBERATELY ACCEPTED on 2026-08-30 — see
    # `_JOURNAL_KIND_SCHEMAS`. It is decided, not outstanding.
    ("engine.py", "_review_and_commit"): frozenset({"type", "severity", "detail"}),
    # `self._session_end_extras(result)` is a method call, and that method builds its
    # dict with `extras.update(...)` — unresolvable at the call site and at the
    # definition. The names below are read off `engine._session_end_extras`, and five
    # of them (`fired_at`, `teardown_s`, `expired_clock`, `budget_weighted`,
    # `budget_mode`) have NO other producer anywhere: the previous comment's claim
    # that these keys "are in the benign inventory because other sites journal them
    # explicitly" was simply false. They are in it because THIS declaration puts them
    # there. ⚠️ A new key added inside `_session_end_extras` is still invisible.
    ("engine.py", "_run_session"): frozenset(
        {
            "fired_at",
            "teardown_s",
            "expired_clock",
            "budget_weighted",
            "budget",
            "budget_mode",
            "env_fault",
            "env_fault_evidence",
            "session_vanished",
        }
    ),
    # The plugin bus's `_log` forwards its OWN `**fields` parameter, so the keys
    # belong to each CALLER and there is no store in this function to resolve. The
    # callers' keywords are read at their own sites — but ONLY because
    # `JOURNAL_FORWARDERS` declares `_log` a journal write. Before that they were
    # unreachable: `_is_journal_write` matched `.append(...)` alone, the four
    # `self._log(...)` sites were never read, and `rc` and `blocking` sat in neither
    # routing set with this guard green. That is what the old comment's "the scan
    # reads them directly at their own sites" asserted and did not do.
    ("plugins/bus.py", "_log"): frozenset(),
}

# ``(file, function name)`` of every helper that FORWARDS to ``journal.append`` with a
# ``**kwargs`` of its own. A call to that NAME inside that FILE counts as a journal
# write, so the forwarder's callers put their explicit keywords into the inventory
# instead of stopping at a wall.
#
# The forwarder's own `self._journal.append(kind, **fields)` stays an unresolvable
# splat — its parameter has no store to resolve — so both this entry and the
# `JOURNAL_SPLAT_ALLOW` one are needed, and they say different things: this one makes
# the CALLERS visible, that one declares the forwarder's own hole.
JOURNAL_FORWARDERS = {("plugins/bus.py", "_log")}

# ``(file, enclosing function)`` of every journal write whose positional KIND is not
# a string literal, mapped to HOW MANY such writes that position holds. This is the
# scan's `journalkind` population, including an empty positional slot even when a
# literal kind arrives by keyword. Kind-scoped routing
# (`JOURNAL_KIND_ROUTED_FIELDS` / `JOURNAL_KIND_BENIGN_FIELDS`) cannot be evaluated
# at such a call, so — exactly like an unresolvable splat — the site fails loud
# rather than being graded against a kind the scan had to guess.
#
# Declaring a position waives the KIND resolution and NOTHING else: a kind-scoped
# name at one of these sites is still refused, because nothing here can prove which
# kind it lands on. The waiver is per-POSITION, so it covers writes the declarer
# never read; the count is what makes a write added inside an already-declared
# position visible — `test_journal_dynamic_kind_positions_write_what_they_declare`
# holds each key's value against the tree.
#
# The value counts WRITE SITES inside the position — not kinds, and not call sites.
# The `recovery_flow` row's writes happen to mint one kind spelling each, and the
# single-write rows are reached from several callers, but neither is what the number
# means: a further `journal.append` inside the position moves it even when the kind it
# spells already exists.
#
# ⚠️ STATED BOUNDS. The key holds a BARE function name, not a qualified
# `class.method`, so two same-named journal-writing functions in ONE module aggregate
# into a single row: their writes sum into one count and a dynamic kind moving
# between them reddens nothing. Same bound on `JOURNAL_DYNAMIC_KIND_SPELLINGS` and on
# `JOURNAL_SPLAT_ALLOW` / `JOURNAL_SPLAT_FIELDS` — one key shape, one hole. No such
# pair exists today, and the assumption is enforced rather than trusted: the scan
# emits each journal write's enclosing DEF identity,
# `_journal_bare_name_collisions` reads it, and
# `test_journal_writers_do_not_share_a_bare_name` reddens on the day the pair
# arrives. Qualifying the key is deliberately DEFERRED (DW-152), not overlooked.
JOURNAL_DYNAMIC_KIND_ALLOW = {
    # `kind` is a keyword parameter defaulting to `review-skipped`, flipped to
    # `review-skipped-awaiting-operator` by the park path. Journals `story_key` only.
    ("engine.py", "_skip_review_and_commit"): 1,
    # `kind` is chosen by the two ledger-close call sites. Journals `story_key` and
    # `dw_ids` only.
    ("sweep.py", "_close_bundle_ledger_when_spec_status"): 1,
    # F-string writes over the `family` loop variable. WHICH kinds they spell is
    # not claimed here — `JOURNAL_DYNAMIC_KIND_SPELLINGS` holds that axis, read off
    # the same AST, so the spelling claim is made in exactly one place.
    ("recovery_flow.py", "prune_preserve_refs"): 4,
    # The forwarder passes its caller's `kind` straight through; every CALLER spells
    # a literal, and `JOURNAL_FORWARDERS` is what lets the scan read them there.
    ("plugins/bus.py", "_log"): 1,
}

# The kind SPELLINGS a dynamic-kind position mints itself, keyed by the same
# ``(file, function)`` position as the count above. One row today: the f-string
# family in `recovery_flow.prune_preserve_refs`, whose kinds exist nowhere as a
# literal — the scan expands `f"{family}-pruned"` by resolving `family` through the
# for-loop over literal tuples that binds it, and
# `test_journal_dynamic_kind_positions_mint_what_they_declare` grades the two sets
# against each other in both directions.
#
# SEPARATE from `JOURNAL_DYNAMIC_KIND_ALLOW` rather than a richer value on it: that
# dict's int values are consumed by the derived count-drift probe rows (`max`/`min`
# over `.items()` by value) and by a position-key shape those rows read, so the two
# axes stay two tables. The count answers "how many writes live here", this answers
# "what do they spell"; a write added inside the position moves the count, a rename
# moves this, and a new f-string kind moves both.
#
# Same position-key bound as the count table: the key holds a BARE function name, so
# two same-named journal-writing functions in one module aggregate their spellings
# into one row and a spelling moving between them reddens nothing.
#
# ⚠️ This does NOT feed `JOURNAL_KINDS`. These kinds stay out of the literal-kind
# inventory by that set's own stated bound — they are minted, not written, and the
# routing tables the inventory serves cannot be evaluated at a site whose kind is not
# a literal. This table is the identity pin for the minted spellings, nothing more.
JOURNAL_DYNAMIC_KIND_SPELLINGS = {
    ("recovery_flow.py", "prune_preserve_refs"): frozenset(
        {
            "attempt-preserve-pruned",
            "attempt-preserve-prune-failed",
            "attempt-preserve-dirty-pruned",
            "attempt-preserve-dirty-prune-failed",
        }
    ),
}

# Every literal journal KIND written today: a declared inventory, not a per-kind
# audit — `JOURNAL_BENIGN_FIELDS`' claim, made for the kind axis. A kind this set
# does not already hold cannot appear without someone deciding, in the same PR, what
# covers the record it introduces: a routing row in `diagnostics` if any field carries
# an identifier, a path or free text, and a test row asserting the record at the layer
# that reads it — the decision review iteration 6 kept discovering had been skipped.
#
# Generated from the scan, hand-reviewed, grouped by producer module; a kind two
# modules write sits under a shared heading. A deleted or renamed kind reddens the
# staleness direction too — PROVIDED no other producer still writes it: the
# staleness arm sees the union of producers, so removing ONE writer of a shared
# kind (the shared headings below, `run-stop` included) reddens nothing by itself.
#
# A declared dynamic-kind position (`JOURNAL_DYNAMIC_KIND_ALLOW`) writes a
# parameter, not a literal, so its kinds enter here through the literals that reach
# it from outside: the `kind="..."` a caller hands `engine._skip_review_and_commit`
# or `sweep._close_bundle_ledger_when_spec_status`, and each one's parameter
# default (`review-skipped`, `sweep-bundle-closed`) — a second `journalkindliteral`
# arm reads both, keyed by the same `(file, name)` as the position.
#
# ⚠️ STATED BOUNDS. Truly dynamic kinds — the f-string family in
# `recovery_flow.prune_preserve_refs` — are NOT rows here: the position is declared
# and governed by the literalness test, and the kinds it mints (e.g.
# `attempt-preserve-pruned`) never enter this inventory. And every receiver shape
# the journal scan cannot see is a hole in this emit too (`JOURNAL_RECEIVERS`'
# bound): a locally aliased handle, a `Journal` SUBCLASS constructed inline
# (`_RearmJournal(run_dir).append(...)`), the `super().append(...)` inside such a
# subclass's override, and a constructor reached through an import alias. On
# today's tree the only subclass instance is bound to the `journal` name and its
# override forwards its parameter kind, so no literal is missed — but the bound is
# the scan's, not the tree's.
JOURNAL_KINDS = frozenset(
    {
        # adapters/generic.py — the only adapter-side writer. The engine hands its
        # `Journal` to every adapter it owns (`CodingCLIAdapter.journal`, #680) so
        # the wait loop can record what only it sees: the live transcript's idle
        # stretches, one `session-idle` at the crossing of `dev_stall_grace_s` and
        # one `session-active` when the transcript moves again. Both carry only
        # `task_id` (routed) and measured floats (`idle_s`, `since_ts`,
        # `threshold_s`, declared benign).
        "session-active",
        "session-idle",
        # cli.py
        "run-resume",
        # cli.py + runs.py
        "rearm-code-root-restamped",
        # engine.py
        "board-advance-carried",
        "board-advance-carry-failed",
        "board-advance-carry-foreign-dirt",
        # DW-237. The REFUSAL arm of the board carry: `_carry_board_advance` asked
        # `verify.unpublishable_target` whether the board was still a publishable
        # regular file (family `"store"`) and it was not, so the commit was skipped
        # before `verify.commit_paths` could hand the literal pathspec to `git add` —
        # which stages a DIRECTORY's descendants RECURSIVELY, publishing an unrelated
        # tree under a `chore(sprint-status):` message. Reachable through the window
        # the method's own `is_file()` pre-check leaves open (#686). `refuse_cause`
        # and the optional `error` are already declared (see `sweep-ledger-commit-
        # refused`); `target` is this producer's usual sprint STATUS, declared beside
        # its four siblings in `JOURNAL_KIND_BENIGN_FIELDS` rather than by name.
        "board-advance-carry-refused",
        "board-advance-carry-uncommitted",
        "console-ctrl-ignored",
        "defer-ledger-restore-diverged",
        "deferred-artifacts-stashed",
        "deferred-close-duplicate-id",
        "deferred-close-external-ledger",
        "deferred-close-ledger-unavailable",
        "deferred-close-malformed",
        "deferred-close-reopen-unmatched",
        "deferred-close-rollback-failed",
        "deferred-close-rolled-back",
        "deferred-close-skipped-out-of-tree",
        "deferred-close-unmatched",
        "dev-decision",
        "epic-boundary",
        "fix-decision",
        "fix-harvest-failed",
        "harvest-carried",
        # DW-237. The REFUSAL arm of the harvested-deferral carry, minted for the
        # same hazard as its board sibling above: a ledger replaced by a DIRECTORY
        # between the append and the commit would be staged recursively under a
        # `chore(deferred-work):` message. Family `"ledger"`, declared at the site.
        # It never raises, where this method's `GitError` can — a refusal answers
        # "not a publishable file", which for the three DURABLE causes a replay
        # re-reads and refuses identically, so there is nothing left to retry; the
        # one TRANSIENT cause (`target-unreadable`) is not refused at this site but
        # handed back to `commit_paths`, so the durable commit latch survives.
        # `story_key`, `dw_ids`, `refuse_cause` and the optional `error` are all
        # already routed or declared.
        "harvest-carry-refused",
        "harvest-carry-uncommitted",
        "isolation-flip-orphaned-worktree",
        "ledger-baseline-probe-failed",
        # DW-231 (decode faults) and DW-258 (reads the OS refuses). The two
        # routes a ledger read fault takes inside the base `Engine`.
        # `ledger-read-degraded` is the OBSERVATION arm — `_ledger_text` (behind
        # `_ledger_digest`, the pre-harvest snapshot and the two restores) and
        # `_defer`'s in-place snapshot answered a typed `_UndecodableLedger` or
        # `_UnreadableLedger`, or `None`, that nothing can write back, and the run
        # went on. `ledger-read-refused` is the PUBLISH arm — the spec-deferral
        # harvest or the isolated carry was about to write from the text and
        # paused the run for repair instead (`_pause_for_ledger_repair`, no phase
        # change). Both carry only already-declared fields: `story_key` (routed),
        # `site` (benign), `ledger` (benign) and `error` (dropped).
        "ledger-read-degraded",
        "ledger-read-refused",
        "ledger-restore-failed",
        "ledger-restore-skipped-diverged",
        "ledger-scope-probe-failed",
        "ledger-snapshot-missing",
        "ledger-tracked-probe-failed",
        "legacy-ledger-attribution-failed",
        "max-stories-reached",
        "notify-desktop-unavailable",
        "operator-index-failed",
        "park-proof-of-work-skipped",
        "park-record-rollback-failed",
        "plugin-veto",
        "plugins-active",
        "preference-escalation",
        "resume-defer",
        "resume-ledger-carry",
        "resume-review",
        "resume-unit-merge",
        "resume-verify",
        "review-budget-committed",
        "review-budget-ledger-unreadable",
        "review-followup-damped",
        "review-not-recommended",
        "review-result",
        "review-retry",
        "review-skipped",
        "review-skipped-awaiting-operator",
        "review-timeout-salvage",
        "review-timeout-salvage-failed",
        "review-verify-failed",
        "run-complete",
        "run-crash",
        "run-paused",
        "run-stop-finalize-error",
        "session-end",
        "session-rescued-post-kill",
        "session-start",
        "session-synthesized-from-frontmatter",
        "spec-deferral-sighting-stale",
        "spec-deferrals-harvested",
        "spec-deferrals-malformed",
        "spec-deferrals-skipped-out-of-tree",
        "spec-marker-repair-failed",
        "spec-marker-repair-skipped",
        "spec-marker-repaired",
        "spec-read-failed",
        "spec-reconcile-skipped-out-of-tree",
        "spec-status-reconciled",
        "sprint-status-unknown-keys",
        "stop-request-discarded",
        "story-awaiting-operator",
        "story-deferred",
        "story-deferred-close-carried",
        # DW-237. The REFUSAL arm of the declared-close carry (#458), the same guard
        # and the same `"ledger"` family as `harvest-carry-refused` above, on the
        # publisher whose commit was already best effort. Journalled beside the
        # `-uncommitted` row rather than folded into it: "git could not own this
        # path" and "this operand is not a publishable file" name different repairs.
        "story-deferred-close-carry-refused",
        "story-deferred-close-carry-uncommitted",
        "story-deferred-closed",
        "story-done",
        "story-gate-unreadable",
        "story-gated",
        "story-skipped",
        "story-start",
        "sweep-auto-failed",
        "sweep-auto-finished",
        "sweep-auto-not-started",
        "sweep-auto-skipped-dirty",
        "sweep-auto-suppressed",
        "sweep-auto-trigger",
        "token-budget-exceeded",
        "verify-command-result",
        "workflow-end",
        "workflow-start",
        # engine.py + runs.py (runs.py's writer is the constructor-inline spelling)
        "run-stop",
        # engine.py + sweep.py
        "resume-commit",
        "resume-restart",
        # engine.py + worktree_flow.py
        "story-escalated",
        # plugins/bus.py
        "plugin-hook",
        "plugin-hook-error",
        # plugins/bus.py + plugins/registry.py
        "plugin-error",
        # plugins/loader.py
        "plugin-skipped",
        # plugins/registry.py
        "plugin-loaded",
        "plugin-untrusted",
        # recovery_flow.py
        "attempt-commits-preserved",
        "attempt-preserve-enumerate-failed",
        "attempt-preserve-failed",
        "attempt-restore-failed",
        "attempt-restored",
        "attempt-worktree-preserve-failed",
        "attempt-worktree-preserved",
        "rollback-auto",
        "rollback-dirty-check-failed",
        "rollback-manual-required",
        "rollback-owned-spec-baseline-read-failed",
        "rollback-owned-spec-baseline-status-failed",
        "rollback-owned-spec-manual-required",
        "rollback-owned-spec-normalized",
        "rollback-owned-spec-restored",
        "rollback-owned-spec-snapshot-missing",
        "rollback-owned-spec-unavailable",
        "rollback-owned-spec-unpreservable",
        "rollback-owned-spec-unreadable",
        "rollback-reset-failed",
        "rollback-skipped-clean",
        # runs.py
        "rearm-aborted",
        "rearm-baseline-advance-failed",
        "rearm-baseline-restamp-skipped",
        "rearm-baseline-restamped",
        "rearm-commits-probe-failed",
        "rearm-spec-flip-skipped",
        "rearm-spec-write-unreachable",
        "rearm-upstream-write-unreachable",
        "run-stop-undelivered",
        "sentinel-cleared",
        "stale-restore-commits",
        "stale-restore-excluded",
        "stale-restore-unparseable",
        "story-escalation-resolved",
        # runsetup.py
        "composition-unwind-failed",
        "run-start",
        # stories_engine.py
        "checkpoint-pause",
        "checkpoint-resume",
        "checkpoint-skip-last",
        "deferred-close-declaration-unreadable",
        "plan-halt",
        "plan-halt-proof-of-work-skipped",
        "sentinel-detected",
        "stories-escalation-unresolved",
        "stories-manifest-unreadable",
        "stories-selector-unknown",
        "stories-validated",
        "stories-wedged",
        # sweep.py
        # DW-273. The bundle path's artifact-only receipt was ACCEPTED at the dev
        # proof-of-work gate: the ordinary probe positively found nothing, the
        # session's synthesized result asserted the strict `artifact_only: true`
        # boolean, and the directory-scoped `git status --ignored` listing of the
        # configured `implementation_artifacts` held ignored (`!!`) entries. Mirrors
        # the sprint leg's `park-proof-of-work-skipped`. `story_key` and `dw_ids`
        # are routed, `attempt` and `count` (the number of ignored files under the
        # artifacts dir THIS attempt created or changed, measured against the
        # attempt-start snapshot below) are benign.
        "bundle-artifact-only-accepted",
        # The receipt's attempt-start snapshot (`verify.artifact_dir_snapshot`)
        # could not be taken — a `GitError` on the listing — so the task carries
        # no ownership baseline and the receipt refuses for this attempt; the
        # attempt is still driven. `story_key` is an alias, `attempt` benign,
        # `error` (the git detail) in `diagnostics._JOURNAL_DROP_FIELDS`.
        "bundle-artifact-baseline-unavailable",
        "bundle-start",
        "decision-answered",
        "decision-pending",
        "decision-preanswered",
        "decision-preanswers-pruned",
        "decision-skipped-unattended",
        "migrate-decision",
        "migrate-duplicate-ids",
        "sweep-bundle-close-carried",
        # DW-237. The REFUSAL arm of the bundle-close carry — the sweep's own copy of
        # `story-deferred-close-carry-refused`, on `SweepEngine`'s override. Last of
        # the five `verify.commit_paths` callers that reached git with no
        # publishable-target guard; every exact-commit publisher now proves its
        # target before spawning any git for it.
        "sweep-bundle-close-carry-refused",
        "sweep-bundle-close-carry-uncommitted",
        # DW-280/DW-286. A bundle-close mutator's own locked read refused at one
        # of the sweep's three close sites (`bundle-close-locked`,
        # `bundle-reclose-locked`, `bundle-close-carry-locked`), or the terminal
        # post-merge harvested append refused at `harvest-carry` or
        # `harvest-carry-append-locked`. Bare, these raises crashed or selected
        # the engine escalation route; now the run PAUSES at the story gate on
        # the task with its phase and carry intent untouched, so
        # `bmad-loop resume` re-drives the composite carry. Direct pre-terminal
        # sweep defer carries retain the engine route. The sweep's own row beside
        # `sweep-bundle-close-carry-refused`, not the engine's
        # `ledger-read-refused`. No new diagnostics routing: `story_key` is an
        # alias, `dw_ids` (empty for the append, otherwise the ids the close was
        # about to publish) is a keylist, `site` and `ledger` are benign, and
        # `reason` (one of the fixed tokens `ledger-unreadable` /
        # `ledger-inaccessible`, by fault class) and `error` (the decode or OS
        # detail) are both in `diagnostics._JOURNAL_DROP_FIELDS`.
        "sweep-bundle-close-refused",
        "sweep-bundle-closed",
        # DW-144. A reset in-flight bundle task adopting the ids of the bundle now
        # being run. Both id lists are routed (`dw_ids` by name, `previous_dw_ids`
        # by kind in `_JOURNAL_KIND_KEYLIST_FIELDS`) so the divergence stays
        # auditable in a dump without the ledger ids shipping verbatim.
        "sweep-bundle-dwids-adopted",
        "sweep-bundle-key-collision",
        "sweep-bundle-key-deduped",
        "sweep-bundle-name-deduped",
        "sweep-bundle-name-discarded",
        "sweep-bundle-name-normalized",
        "sweep-bundle-reclosed",
        "sweep-bundle-reopened",
        "sweep-bundle-skipped",
        "sweep-bundles-truncated",
        # DW-194/202/210: decision-effect doubt withheld this cycle's bundles.
        # No new diagnostics routing: cycle/bundles_not_run are already benign;
        # reason is already a drop field and carries fixed token ledger-unreadable.
        "sweep-bundles-withheld",
        "sweep-cycle",
        # DW-197. `_loop`'s own repair/write ledger read refused at the top of a
        # cycle body — undecodable bytes, or an `OSError` from the read itself.
        # Bare, either ended a `--repeat` run as crashed and threw away the report
        # for the cycles that had already completed; the row is what says why the
        # run stopped one cycle short, since the read gates the whole cycle below
        # it. No new diagnostics routing: `ledger` is already benign, and `reason`
        # and `error` are both already in `diagnostics._JOURNAL_DROP_FIELDS` —
        # `reason` is one of the same two fixed tokens the stop carries
        # (`ledger-unreadable`, `ledger-inaccessible`), never free text, and the
        # decode or errno detail rides in `error`.
        "sweep-cycle-ledger-refused",
        "sweep-decision-answer-dropped",
        # DW-167. A stored `close` answer whose ledger effect never landed, applied
        # on resume. The answer is persisted BEFORE `record_decision` runs — the
        # human's answer must survive a crash — so a crash in that window left
        # `<run>/decisions.json` claiming `effect: "close"` over an entry the ledger
        # still lists as open, and the read side then counted it consumed: `pending`
        # filtered the id out and no materialization lane matches `close`, so the
        # decision was never re-asked and never applied. This row is what says the
        # `decision:` line landed LATER than the `decision-answered` above it, off
        # the stored answer rather than a fresh prompt. `dw_id` and `effect` only,
        # both already benign (`effect` is a closed `DECISION_EFFECTS` value, and in
        # practice always `close` — the only effect this walk re-applies).
        "sweep-decision-effect-reapplied",
        # DW-166/DW-186. A decision whose ledger effect did not land, from EITHER of
        # the two ways that happens.
        # `prompter.ask` blocks, so a ledger that goes undecodable (or a ledger
        # lock that fails) while the prompt is open RAISES out of
        # `record_decision`; and `record_decision` RETURNS False — no raise
        # involved — when there is no ledger file at all, or no entry carrying the
        # id a rival writer retired while the prompt was open. Both mean no
        # `decision:` line was written, both reach the same degrade, and both take
        # this one kind on purpose: `sweep._HANDBACK_LEDGER_MISS` prints exactly
        # one kind for an operator to grep. Either way the answer was already
        # persisted and journalled first, and this row is what says the
        # `decision-answered` above it has no ledger line behind it. `dw_id` and
        # `effect` are already benign (`effect` is a closed `DECISION_EFFECTS`
        # value, not authored text) and `error` is already in
        # `diagnostics._JOURNAL_DROP_FIELDS` — it carries either the exception text
        # or, for the False return, one of a FIXED SET of sentences, so no new field
        # routing is needed. The interactive arm chooses between two by whether the
        # ledger FILE is still there, since a missing ledger loses every line the
        # walk already wrote where a missing entry loses only this one.
        # THIRD producer since DW-167: the resume re-apply walk, whose ledger-read
        # GATE takes this same kind — one row per candidate id when the ledger is
        # absent or undecodable, and the `except`/False-return rows again for the
        # re-applying write itself. Per CANDIDATE and not per file, so the row names
        # an id an operator can chase; the fixed sentence names the gate, since the
        # news there is "this stored answer may still be unapplied" rather than a
        # write that was attempted and lost. That walk has a THIRD sentence the
        # interactive arm cannot reach: it passes `require_open=True`, so
        # `record_decision` also refuses an entry that is present and no longer open
        # — a rival writer closed it between the walk's gate and its write, which is
        # not the missing-entry state and must not be reported as one.
        "sweep-decision-effect-unavailable",
        # DW-216/220. The decision phase's END-OF-PHASE ledger probe refused. It is
        # taken only by a phase that attempted no effect at all and therefore has no
        # observation of its own to publish — the unattended all-skipped shape —
        # where the cycle used to hand `_cycle`'s dispatch gate a False latch over a
        # ledger that had gone bad mid-cycle, and the first bundle's `_write_intent`
        # died on its bare `read_for_write`. The row is what says the withhold came
        # from a probe rather than from a fault anybody observed. No new diagnostics
        # routing: `ledger` is already benign, and `reason` and `error` are both
        # already in `diagnostics._JOURNAL_DROP_FIELDS` — `reason` is one of the two
        # fixed tokens naming the classes that make that read RAISE
        # (`ledger-unreadable`, `ledger-inaccessible`), never free text, with the
        # decode or errno detail in `error`. Absence arms nothing and writes no row,
        # keeping DW-176's discipline.
        "sweep-decision-ledger-refused",
        # DW-214. `_materialize_bundles`' open-set screen could not read the ledger,
        # so it screened NOTHING this cycle and every adopted `build` answer kept
        # the disposition it already had. The row is what says a bundle that ran was
        # never checked against the ledger's live open set — the alternative,
        # collapsing a fault to an empty open set, would drop every build answer in
        # the cycle at once. No new diagnostics routing: `ledger` is already benign,
        # and `reason` and `error` are both already in
        # `diagnostics._JOURNAL_DROP_FIELDS` — `reason` is one of the same four
        # fixed tokens `sweep-preanswer-prune-refused` carries (`ledger-absent`,
        # `ledger-unreadable`, `ledger-inaccessible`, and DW-217's `ledger-in-doubt`
        # for a ledger that reads perfectly but this cycle already declared unfit to
        # publish), never free text, with the decode or errno detail in `error` and
        # no `error` at all on the two that observed no fault. Unlike the two
        # `*-ledger-refused` kinds beside it this arms no ledger doubt: the refusal
        # degrades ONE screen, not the cycle's dispatch gate.
        "sweep-decision-open-set-refused",
        "sweep-decision-option-mismatch",
        # DW-143. The keep-open lane's `stale-option` drop retired the PROJECT-level
        # pre-answer that fed it, so the next run reads no stale answer to re-drop
        # and `bmad-loop decisions` re-offers the id. `decision` + `drop_cause`
        # only — both already benign — since deleting a human-authored answer has
        # to stay auditable without the answer's prose entering the journal.
        "sweep-decision-preanswer-pruned",
        "sweep-decisions-only",
        # `<run>/decisions.json` (or a project pre-answer inside it) would not
        # read or is not shaped `{id: {...}}`: the answer map degrades instead of
        # aborting the sweep. `errors` carries exception text, type names, the
        # DW ids whose answers were dropped and — since DW-147, which refuses a
        # `close` read from the PROJECT store — the offending effect, a closed
        # `DECISION_EFFECTS` value (build/close/keep-open) taken from the answer
        # rather than authored text, the same bound `answer_effect` and
        # `option_effect` above are blessed under. Already a benign field
        # (`JOURNAL_BENIGN_FIELDS`), and no answer prose goes near it, so the
        # record needs no `diagnostics` routing row.
        "sweep-decisions-reload-failed",
        # DW-262. `_decisions_phase`'s SEEDED write-back of `<run>/decisions.json`
        # refused by the OS (a directory planted at the store, which the `S_ISREG`
        # probe answers silently; a refused parent): the pre-answers adopted from
        # the project store stay in memory for this cycle's bundling but did not
        # persist. Its own kind, not `sweep-decisions-reload-failed` (a READER's
        # row). The interactive write-back is deliberately NOT guarded — a human's
        # answer that cannot be persisted stops the sweep loudly. `file` is the
        # store's basename (a code constant, benign above), `dw_ids` the routed
        # keylist, `error` the dropped exception text — no new field minted.
        "sweep-decisions-store-write-failed",
        # DW-264. Both write-backs of `<run>/decisions.json` WITHHELD because the
        # store's metadata probe or content read was refused with an `OSError`
        # this cycle: the bytes on disk may hold valid answers that merely could
        # not be read, so replacing them from an `answers` that started empty
        # would turn a transient refusal into permanent loss. Decode faults and a
        # non-object top level do NOT withhold — there the replacement is the
        # repair. Same fields as the failed row minus `error`; the withheld check
        # precedes the write, so one write never lands on both rows. The seeded
        # site is the only writer since #794's review: the interactive arm
        # withholds the PROMPT instead (next row).
        "sweep-decisions-store-write-withheld",
        # DW-264's interactive half (#794 review). While `<run>/decisions.json`
        # could not be READ this cycle, the human is not asked: an answer taken at
        # the prompt could not be persisted (the write is withheld above), it has
        # no second copy, and nothing reads a `build` back off the ledger's
        # `decision:` line, so a crash before the bundle was materialized lost the
        # authorization. `file` is the store's basename, `dw_ids` the pending ids
        # not asked, `error` the read refusal's text (diagnostics-dropped); the
        # decisions stay pending and unquarantined for the next interactive run.
        "sweep-decisions-prompt-withheld",
        "sweep-inflight-redrive",
        "sweep-inflight-stranded",
        # DW-243. `_ensure_bundle_intent`'s regeneration read of the ledger
        # refused on a resume — undecodable bytes, or an `OSError` from the read
        # itself. Bare, it crashed the resume at that site, ahead of any cycle
        # gate; now this row names the in-flight bundle the refusal caught and
        # the run PAUSES at the story gate on that task (`run-paused`, no
        # `sweep-repeat-done`), un-finished and PENDING, so `bmad-loop resume`
        # after the repair re-enters the recovery pass and re-drives it. No new
        # diagnostics routing: `story_key` is an alias, `ledger` is benign, and
        # `reason`/`error` are both already in `diagnostics._JOURNAL_DROP_FIELDS`
        # — `reason` is one of the same two fixed tokens (`ledger-unreadable`,
        # `ledger-inaccessible`), never free text, and the decode or errno detail
        # rides in `error`.
        "sweep-intent-ledger-refused",
        # DW-252. An in-flight bundle's intent document was NOT regenerated, for
        # one of two reasons under a closed two-token `reason`: `entry-missing`
        # (the readable ledger holds no entry for one of the task's ids — the
        # document would have briefed a dev session on an empty "Ledger entries
        # (verbatim)" section; `dw_ids` names the MISSING ids) or `ledger-absent`
        # (no ledger file at all; `dw_ids` names the task's ids). The run then
        # pauses at the story gate on the task, the same way the DW-243 row
        # above does, so no later bundle or fresh triage runs beside it; a resume
        # after the ledger is restored regenerates and re-drives it.
        # `dw_ids` is routed by name in `diagnostics._JOURNAL_KEYLIST_FIELDS`,
        # `story_key` is an alias, and `reason` is already a drop field.
        "sweep-intent-regen-refused",
        "sweep-intent-regenerated",
        "sweep-ledger-commit",
        # DW-191. The NO-OP arm of the same producer, covering BOTH of
        # `_commit_ledger`'s silent returns: the `verify.path_clean` early return,
        # and the `sha is None` return where `verify.commit_paths` found the
        # pathspec clean between the check and the commit. One kind for both
        # because the operator-facing fact is identical — nothing was published
        # because the pathspec held no change. Minted because `path_clean` reports
        # an IGNORED path as clean, so the default ledger under a gitignored
        # `implementation_artifacts` was skipped with no row of any kind, which a
        # dump could not tell apart from a publisher that never ran. `message` is
        # already dropped; `file` is the new benign field that names which of the
        # two published files this is about.
        "sweep-ledger-commit-clean",
        # DW-199/203/205. The REFUSAL arm of the same producer: `_commit_ledger`
        # asked whether its declared family's target was still publishable BEFORE
        # reaching git, and it was not. Minted because `verify.commit_paths` keeps
        # a missing-but-TRACKED path as a deletion to stage, so a ledger removed
        # after the phase wrote it was published as a DELETION under a
        # `chore(sweep):` message, and a resume whose ledger held undecodable bytes
        # published them and only then raised on them. `refuse_cause` is the new
        # benign field naming which of FOUR fixed tokens fired (`target-absent` |
        # `target-unreadable` | `target-not-a-file` | `target-undecodable`, all
        # minted in `verify.unpublishable_target`, which
        # DW-209/213 lifted out of `sweep.py` so the out-of-band `bmad-loop decisions`
        # publisher shares one guard with these nine); `file` is the same
        # already-benign lexical basename
        # the sibling rows carry, and `message` plus the optional `error` are
        # already in `diagnostics._JOURNAL_DROP_FIELDS`.
        "sweep-ledger-commit-refused",
        # The degrade arm of the same producer: an EXPLICITLY-rooted
        # `_commit_ledger` whose `verify.GitError` is journalled instead of
        # propagating, leaving the write on disk rather than aborting the sweep.
        # TWO producers reach it. The pre-answer prunes (DW-160) name the project,
        # so `repo` is a project root that is not a git repo. The seven ledger
        # publishers (DW-175) name the ledger's own directory, so `repo` can be a
        # freestanding `implementation_artifacts` enclosed by no repository at all,
        # which is a plain host directory and not a git tree in any sense
        # (`tests/test_sweep.py` asserts that spelling). Both fields are already
        # routed out of diagnostics dumps — `repo` as an absolute host path, `error`
        # as free text quoting git's own stderr — so it needs no new field row.
        "sweep-ledger-commit-unavailable",
        # DW-246. A ledger publish the RUN declined to attempt because it already
        # held the ledger unfit to publish (`_ledger_unfit_to_publish()`, the
        # persisted DW-218/219 doubt included) — written by
        # `sweep._withhold_ledger_publish` at the four resume-time publishers that
        # run AHEAD of `_cycle`'s dispatch gate (`_close_resolved`'s two arms,
        # `_loop`'s post-recovery publisher, and since DW-250 the no-open exit's
        # `_publish_stranded_close`, whose row alone adds `dw_ids` — the cached
        # plan's already-resolved and decision ids it declined to prove). Its own
        # kind rather than a fifth `refuse_cause`: no target was probed and no git
        # was spawned, so it is not a `verify.unpublishable_target` verdict. No new
        # diagnostics routing: `message` and `reason` are already in
        # `_JOURNAL_DROP_FIELDS` (`reason` carries the fixed DW-217 token
        # `ledger-in-doubt` and renders as a presence boolean), `file` is the same
        # already-benign lexical basename the sibling rows carry, and `dw_ids` is
        # already a `_JOURNAL_KEYLIST_FIELDS` name.
        "sweep-ledger-commit-withheld",
        "sweep-migrated",
        # DW-296/DW-297. Current-format migration recovery evidence was absent,
        # nonregular, unreadable, malformed, or mutually inconsistent. `detail`
        # is already routed through diagnostics._JOURNAL_DROP_FIELDS.
        "sweep-migration-recovery-invalid",
        "sweep-migration-restore-diverged",
        "sweep-nothing-open",
        # DW-176/DW-182/DW-197. `_prune_pre_answers` refusing to prune because the
        # deferred-work ledger could not be read for a write — ABSENT (DW-176),
        # holding bytes nobody could decode (DW-182), or refused by the OS
        # (DW-197). The open set is the keep list
        # for a store write, so collapsing any of them to an empty ledger would read as
        # "nothing is open" and drop every pre-answer the human recorded — and
        # since DW-160 commit the wipe. Refusal is announced rather than silent so
        # an operator can see why consumed answers are still in the store. `ledger`
        # is already benign and both `reason` and `error` are already in
        # `diagnostics._JOURNAL_DROP_FIELDS`; the reason is one of THREE fixed
        # tokens (`ledger-absent`, `ledger-unreadable`, `ledger-inaccessible`),
        # never free text, and the decode or errno fault rides in `error` instead.
        # Two of the three do NOT stop at this row: `ledger-unreadable` (DW-182/186)
        # and `ledger-inaccessible` (DW-197) are each CARRIED to the repeat
        # boundary, where they end a `--repeat` run with `sweep-repeat-done` on the
        # matching token and WITHOUT the boundary ledger commit — otherwise the very
        # next act of a repeating run is to publish the bytes the prune just refused
        # to read. They stay distinct because the operator repair differs (edit the
        # file, versus fix permissions or storage) and the token is all a scrubbed
        # dump keeps. `ledger-absent` stays cycle-local: an absent ledger ends the
        # next cycle cleanly on `no-open`.
        # A FOURTH token since DW-217: `ledger-in-doubt`, and the only one of the
        # four taken with the ledger READABLE. The read succeeded; what refuses is
        # that this cycle already declared the ledger unfit to publish
        # (`sweep._ledger_unfit_to_publish`), so the open set derived from these
        # bytes is not a KEEP list to trust — the decodable half-write class, where
        # an id an aborted write flipped to `done` would otherwise take the human's
        # pre-answer with it. It carries nothing of its own (the latch it read
        # already reaches `_loop`) and writes no `error`, since there is no fault
        # text to quote.
        "sweep-preanswer-prune-refused",
        "sweep-remaining-estimate-unreadable",
        "sweep-repeat-done",
        "sweep-resolved-closed",
        # DW-166. The degrade arm of the row above, with THREE producers instead of
        # the bare call ending the whole sweep as crashed. The first two share
        # `_close_resolved`'s one `try`. FIRST: the batched `mark_done_many` could
        # not write — undecodable ledger bytes, or the cross-process ledger lock
        # failing — so nothing was closed and the entries stay `open` for the next
        # cycle to re-triage. SECOND (DW-193): the same read taken by
        # `_resolved_write_pending`, the probe deciding whether an already-landed
        # close still needs publishing, whose ids may already be `done` with
        # nothing about to close at all. THIRD (DW-193's routing half): that SAME
        # probe run from `_publish_stranded_close`, at `_loop`'s empty-open-set
        # exit, over the ids a CACHED triage plan named — the resume where the
        # stranded close retired the last open entry, so `_close_resolved` never
        # runs and its two producers are unreachable. All three deliberately share
        # one row rather than minting further kinds; what the row means across them
        # is that a usable ledger could not be read and nothing was published.
        # `dw_ids` carries the ids the plan named and is routed by name in
        # `diagnostics._JOURNAL_KEYLIST_FIELDS`; `error` is already a drop field.
        "sweep-resolved-close-unavailable",
        "sweep-return-no-client",
        "sweep-returned-after-decisions",
        "sweep-selection-empty",
        "sweep-selection-excluded",
        "sweep-selection-missing-severity",
        # DW-263. `_ensure_triage`'s cache READ faulted and the cache was unlinked
        # before the fresh triage, so a refused write-back afterwards cannot leave
        # the older bytes for the next resume to replay as this cycle's plan. No
        # fields at all.
        "sweep-triage-cache-invalidated",
        # DW-263's degrade: the invalidating unlink itself refused. Fresh triage
        # proceeds either way; `errors` carries the exception text only, already
        # a benign field (`JOURNAL_BENIGN_FIELDS`).
        "sweep-triage-cache-unlink-failed",
        # DW-247. `_ensure_triage`'s cache WRITE-BACK refused by the OS: the fresh
        # triage validated and its plan is acted on, but `triage{suffix}.json`
        # never landed, so a resume re-triages, `_publish_stranded_close` finds no
        # cache and `bmad-loop decisions` cannot see this cycle's decisions. Its own
        # kind rather than `sweep-triage-reload-failed`, which is a READER's row —
        # overloading it would report a healthy triage as a corrupt cache. `errors`
        # carries the exception text only, already a benign field
        # (`JOURNAL_BENIGN_FIELDS`), so no `diagnostics` routing row is needed.
        "sweep-triage-cache-write-failed",
        "sweep-triage-reload-failed",
        "sweep-triage-result",
        "triage-decision",
        # worktree_flow.py
        "accepted-spec-delivery-unreachable",
        "accepted-spec-write-unreachable",
        "isolation-flip-orphan-preserved",
        "merge-preflight-refused",
        "merge-target-cleaned",
        "merge-target-tolerated",
        "scm-failed-diff-unlimited",
        "target-branch",
        "target-branch-checkout",
        "target-branch-created",
        "unit-closed",
        "unit-merge-started",
        "unit-merged",
        "worktree-exclude-degraded",
        "worktree-kept",
        "worktree-module-skills-dropped",
        "worktree-open-failed",
        "worktree-opened",
        "worktree-seed-dropped",
        "worktree-seed-skipped",
        "worktree-teardown-degraded",
        "artifact-publication-refused",
    }
)

# The NAMED-HANDLE receivers a ``.append(...)`` call must hang off to be a journal
# write. Matched on the trailing name so `self.journal`, a bare `journal` parameter
# and `self._journal` (the plugin bus's optional handle) all resolve. The tree's
# fourth spelling — the constructor-inline `Journal(run_dir).append(...)` that
# runs.py's stop/restamp records use — is a Call receiver, not a name, and is
# matched structurally in `_is_journal_write` rather than through this set.
#
# ⚠️ STATED BOUND: a LOCALLY ALIASED handle is invisible. `j = self.journal` followed
# by `j.append(kind, customer_email=x)` produces no finding (verified by running it
# through `_scan_source`), and a handle bound from the constructor —
# `j = Journal(run_dir)` then `j.append(...)` — is the same shape. So is anything
# the constructor arm's bare-name anchor does not spell: a SUBCLASS constructed
# inline (`_RearmJournal(run_dir).append(...)` — `runs._RearmJournal(Journal)`
# exists), the `super().append(kind, **fields)` inside that subclass's override
# (runs.py's fifth receiver spelling, a `super` Call), and `Journal` reached
# through an import alias. None of these carries a literal the tree misses today
# (the subclass's one instance is bound to `journal`; its override forwards a
# parameter kind), and resolving them would be `_call_aliases`' shape rather than a
# new idea — but the guard does not do it, and a reader must not assume it does.
JOURNAL_RECEIVERS = {"journal", "_journal"}

# Files that may name a bare POSIX path, each on a line carrying a `# portability:`
# ack. process_host.py's Linux identity reader walks `/proc/<pid>/stat` behind a
# sys.platform branch; the Unity teardown scripts are POSIX-only. verify.py is the
# one non-platform case: git's *diff format* spells an absent file `/dev/null` on
# every platform, so `patch_new_files` compares against it as a protocol token.
PATH_ALLOW = {
    "data/plugins/unity/unity_cleanup.py",
    "data/plugins/unity/unity_teardown.py",
    "process_host.py",
    "verify.py",
}

# The detach helpers that legitimately request POSIX `start_new_session` (each
# branches on `sys.platform` for a Windows creationflags fallback).
DETACH_ALLOW = {
    "platform_util.py",
    "data/plugins/unity/unity_setup.py",
    "data/plugins/unity/unity_plugin.py",
}

# `os.kill(pid, 0)` is a read-only existence probe on POSIX but *destructive* on
# Windows (it maps to TerminateProcess). Confine it to the platform-guarded
# liveness helpers, each on a line carrying a `# portability:` ack; everything
# else routes through the ProcessHost seam (`get_process_host().is_alive`). The
# Unity teardown no longer probes directly — it delegates to the seam.
KILL_PROBE_ALLOW = {
    "process_host.py",
}

# Broader than the signal-0 probe: *any* `os.kill(` — a real signal send is just as
# destructive-on-Windows as the probe form. Only the ProcessHost may call it directly;
# everything else routes through the seam (terminate / force_kill / is_alive).
OS_KILL_ALLOW = {
    "process_host.py",
}

# The two sanctioned `shell=True` spots: operator-authored command strings whose
# cmd/PowerShell port is an explicit out-of-scope follow-up.
SHELL_ALLOW = {
    "verify.py",
    "plugins/bus.py",
}

# Bare POSIX paths that must not be hardcoded outside PATH_ALLOW. `os.devnull` is
# the portable replacement for "/dev/null".
POSIX_PATHS = ("/tmp", "/proc", "/dev/null")

# The subprocess spawn entry points a string-form git command could ride in on —
# `subprocess.run("git status", shell=True)`, or the same string with no shell at
# all, which Windows happily execs (CreateProcess takes a command line). Matched
# as `subprocess.<name>(...)` or as the bare from-import spelling. String
# detection anchors on these calls, unlike the sequence detector, because a
# string starting with "git " is routinely prose (an error message, a doc line)
# while a sequence literal headed by "git" is not.
SPAWN_CALL_NAMES = {"run", "Popen", "call", "check_call", "check_output"}

# Prefix that makes an environment variable this project's to register.
ENV_PREFIX = "BMAD_LOOP_"

# ``CONSTANT_NAME -> "BMAD_LOOP_…"`` for the registry's own public constants, read
# off the live module so the guard cannot drift from it: register a fourth var in
# envvars.py and the scan resolves reads spelled through it with no edit here.
# This is what lets a read reach the guard when it borrows the registry's constant
# but skips the registry's reader — the shape a well-meaning change actually takes.
REGISTRY_NAMES = {
    name: value
    for name, value in vars(envvars).items()
    if isinstance(value, str) and value.startswith(ENV_PREFIX)
}

# The session-protocol vars the engine injects into every child session so a
# stand-alone script can find the run it belongs to. They are not operator knobs:
# engine.py / resolve.py / probe.py / plugins.bus build them on the producing side,
# and these scripts read back what was handed to them.
SESSION_PROTOCOL_ENV = (
    "BMAD_LOOP_RUN_DIR",
    "BMAD_LOOP_EVENTS_DIR",
    "BMAD_LOOP_TASK_ID",
    "BMAD_LOOP_WORKTREE",
    "BMAD_LOOP_REPO_ROOT",
    "BMAD_LOOP_CLEAN_TMP",
    "BMAD_LOOP_QUIESCE_PHASE",
    "BMAD_LOOP_PROBE_CAPTURE_DIR",
)

# The plugin's own families, which AGENTS.md's second clause leaves with the plugin
# ("plugin-owned env-var families stay with their plugin"), plus the session
# protocol every injected script reads.
UNITY_ENV = ("BMAD_LOOP_UNITY_", "BMAD_LOOP_ENGINE_", *SESSION_PROTOCOL_ENV)

# ``rel -> the keys and key families that file may read straight out of the
# environment``. Scoped by FAMILY rather than by file on purpose: a
# file-wide exemption would let one of these read a core knob such as
# `BMAD_LOOP_MUX_BACKEND` inline and have the finding dropped on its path alone,
# which is the exact distinction the invariant draws.
#
# `envvars.py` *is* the registry — the one place a core var is named, typed and
# given a reader (AGENTS.md: "New core env vars register in `envvars.py`") — so it
# is scoped to the names it defines, read off the live module: register a fourth
# var there and this needs no edit. The two hook relays are copied OUT of the
# package into the target project and run inside the coding CLI's process under
# whatever interpreter the host has (both say "Stdlib only" in their docstrings),
# so they cannot import bmad_loop to reach the registry at all; the Unity helpers
# are stand-alone the same way. None of them may reach past the families below.
#
# Writes stay out of scope on purpose: engine/resolve/probe/plugins.bus/unity_plugin
# *build* a `BMAD_LOOP_*` env dict to inject into a child session, and that
# producing side is what these readers consume, not a second source of truth.
ENV_READ_ALLOW = {
    "envvars.py": tuple(REGISTRY_NAMES.values()),
    # `events.py` is the ONE in-package entry here, and the "cannot import
    # bmad_loop" justification above does not reach it — it obviously can. It is
    # exempt as the importable PARITY TWIN of the stdlib-only hook relay: the same
    # session-protocol vars, read at the same points in the same protocol, by
    # the code the hook config points at when it points at `bmad-loop relay`
    # instead of the copied script. Routing one twin through `envvars` and leaving
    # the other on `os.environ` would put the reads out of parity, and parity is
    # what the AST test on those two files exists to keep. Family-scoped like the
    # rest, so a core knob read inline here is still an offender.
    "events.py": SESSION_PROTOCOL_ENV,
    "data/bmad_loop_hook.py": SESSION_PROTOCOL_ENV,
    "data/bmad_loop_probe_hook.py": SESSION_PROTOCOL_ENV,
    "data/plugins/unity/unity_cleanup.py": UNITY_ENV,
    "data/plugins/unity/unity_dialog_probe.py": UNITY_ENV,
    "data/plugins/unity/unity_quiesce.py": UNITY_ENV,
    "data/plugins/unity/unity_ready.py": UNITY_ENV,
    "data/plugins/unity/unity_seed_assets.py": UNITY_ENV,
    "data/plugins/unity/unity_setup.py": UNITY_ENV,
    "data/plugins/unity/unity_teardown.py": UNITY_ENV,
}


def _env_key_allowed(key: str, entries: tuple[str, ...]) -> bool:
    """A trailing underscore marks a FAMILY, matched as a prefix; every other entry
    is one variable, matched exactly.

    The split is the difference between exempting a name and exempting everything
    built on it. Under a bare prefix test an entry for ``BMAD_LOOP_MUX_BACKEND``
    would also exempt an unregistered ``BMAD_LOOP_MUX_BACKEND_FALLBACK`` — the
    guard would wave through the very thing it exists to make someone register."""
    return any(key.startswith(e) if e.endswith("_") else key == e for e in entries)


def _env_read_offenders(findings) -> list[tuple[str, int, str, str]]:
    """The env reads no file's declared entries cover — the assertion's whole
    policy, factored out so it can be graded on synthetic findings rather than only
    on today's tree."""
    return [
        (rel, ln, txt, key)
        for _, rel, ln, txt, key in findings
        if not _env_key_allowed(key, ENV_READ_ALLOW.get(rel, ()))
    ]


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """Ids of the string-Constant nodes that are module/class/function docstrings
    — excluded from literal scans (prose, not code)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _classify_posix_path(value: str) -> str | None:
    """The POSIX path this string literal hardcodes, or None. Matches the whole
    value or a subpath of it, so big shell strings that merely *contain*
    ``2>/dev/null`` and lookalikes such as ``~/.gemini/tmp/...`` are not flagged."""
    for pat in POSIX_PATHS:
        if value == pat:
            return pat
        if pat != "/dev/null" and value.startswith(pat + "/"):
            return pat
    return None


def _is_os_environ(node: ast.expr) -> bool:
    """True for the ``os.environ`` / ``os.environb`` attribute access itself.

    ``environb`` is the bytes-keyed twin (POSIX-only, absent on Windows). Nobody
    reaches for it here, but it is the same mapping and costs one string to cover,
    which is cheaper than discovering it later."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr in ("environ", "environb")
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_name_aliases(tree: ast.AST) -> dict[str, str]:
    """``NAME -> "BMAD_LOOP_…"`` for every constant binding in the module, so a read
    spelled through a named constant still resolves. That indirection is the norm
    here, not an edge case: the registry reads ``os.environ.get(MUX_BACKEND)`` and
    gates.py names its notify vars ``_TITLE_ENV`` / ``_MESSAGE_ENV`` — matching the
    string literal alone would miss exactly the well-behaved shape.

    A ``bytes`` constant binds too, since ``os.environb`` can only be keyed by
    bytes: the registry's own constants are ``str`` and would raise there, so a
    bytes literal or a bytes constant are the only two spellings that axis has."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        else:
            continue
        value = node.value
        if not targets or not isinstance(value, ast.Constant):
            continue
        name = value.value
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if isinstance(name, str) and name.startswith(ENV_PREFIX):
            for target in targets:
                aliases[target.id] = name
    return aliases


def _git_name_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """``(head_names, command_names)`` — the names bound anywhere in the module
    to the constant ``"git"`` (a sequence head) and to a string-form git command
    (``"git"`` or a ``"git "`` prefix). The spawn-argv twin of
    ``_env_name_aliases``: a command factored into a named constant
    (``GIT = "git"``, ``GIT_STATUS = "git status"``) is the tidy spelling a
    well-meaning bypass takes, and matching the literal alone would miss exactly
    that shape — in both the sequence and the string branch. ANY binding
    qualifies a name — a later rebind must not launder a spawn that was git
    somewhere in the module — which can only over-flag, and a false positive is
    a review prompt, not a miss. The tmux detector keeps its literal-only head:
    widening that older tripwire is a separate decision from the git chokepoint
    invariant this one enforces."""
    heads: set[str] = set()
    commands: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        value = node.value
        if not targets or not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if value.value == "git":
            heads.update(targets)
        if value.value == "git" or value.value.startswith("git "):
            commands.update(targets)
    return heads, commands


def _env_call_key_node(call: ast.Call) -> ast.expr | None:
    """The node holding the looked-up key: the first positional arg, or the ``key=``
    keyword when the call passes none.

    The keyword form is not hypothetical. ``os.environ`` is ``os._Environ``, a
    Python-level ``MutableMapping``, so its ``get`` / ``pop`` / ``setdefault`` are
    the ABC's plain-Python defs and DO bind ``key=`` — unlike ``dict.get``, whose C
    signature is positional-only and would raise. ``os.getenv(key=...)`` binds for
    the same reason. All four were confirmed against the live interpreter rather
    than assumed, because the dict intuition points the wrong way here."""
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "key":
            return kw.value
    return None


def _env_read_key(node: ast.expr | None, aliases: dict[str, str]) -> str | None:
    """The ``BMAD_LOOP_*`` variable an environment-lookup key names, or None.

    No docstring exclusion here, unlike the POSIX-path scan: that one walks *every*
    string Constant in the tree and so must skip prose, but this one only ever
    inspects a key position (a call's first arg, a subscript's slice). A docstring
    is a standalone ``Expr`` statement and can never appear there, so a
    ``BMAD_LOOP_*`` mention in prose produces no finding to exclude. Verified by
    counting key-position nodes that are also docstring nodes across the whole
    tree: zero. An exclusion here would be unreachable code implying a check that
    is not happening.

    Four spellings resolve, because the interesting violation is the *half-right*
    one: someone who reuses the registry's own constant but skips its reader. A
    literal and a same-module alias were never the risky shapes — reaching for
    ``envvars.MUX_BACKEND`` is, precisely because it looks tidy.

    1. ``os.environ.get("BMAD_LOOP_X")``          — string literal
    2. ``os.environ.get(LOCAL)``                  — bound to a literal here
    3. ``os.environ.get(envvars.MUX_BACKEND)``    — qualified registry attribute
    4. ``os.environ.get(MUX_BACKEND)``            — registry constant imported in

    (3) matches on the attribute name alone rather than proving the object is the
    registry module: `import bmad_loop.envvars as ev` / `from . import envvars`
    and a rebound alias all spell it differently, and resolving that statically
    costs more than it buys. A false positive here is a review prompt on a line
    that reads like an env lookup, not a silent miss — the direction a tripwire
    should fail in."""
    if isinstance(node, ast.Constant):
        # bytes ride along for os.environb's b"BMAD_LOOP_…" keys
        if isinstance(node.value, bytes):
            decoded = node.value.decode("utf-8", "replace")
            return decoded if decoded.startswith(ENV_PREFIX) else None
        if isinstance(node.value, str) and node.value.startswith(ENV_PREFIX):
            return node.value
    if isinstance(node, ast.Name):
        # a same-module binding wins over the registry name it may shadow
        return aliases.get(node.id) or REGISTRY_NAMES.get(node.id)
    if isinstance(node, ast.Attribute):
        return REGISTRY_NAMES.get(node.attr)
    return None


def _called_name(func: ast.expr) -> str | None:
    """The trailing name of a call's callee, or None when the callee is neither a
    plain name nor an attribute access.

    Both spellings resolve to the same name, because both reach the same
    function: the bare name (inside the defining module, and after a
    ``from .verify import`` anywhere else) and the attribute form
    (``verify.verify_commands_outcome``, which is how every module outside core
    reaches it). The module qualifier is deliberately ignored — a bypass written
    as ``v.verify_commands_outcome`` under an aliased import is the same bypass,
    and the cost of the looser match is a false positive, which is a review
    prompt rather than a miss."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _call_aliases(tree: ast.AST, target: str) -> frozenset[str]:
    """Bare names statically bound to one guarded call target.

    The call-site spelling alone misses the ordinary Python aliases a future
    caller may use: rename-on-import and a local assignment from either the
    module attribute or an already-known alias. Resolve those cheap, explicit
    bindings while keeping this a single-file AST scan; computed names remain a
    review-time concern because proving their value requires executing code.
    """
    aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == target
    }
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            value_name = _called_name(value)
            if value_name != target and value_name not in aliases:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for assignment_target in targets:
                if isinstance(assignment_target, ast.Name) and assignment_target.id not in aliases:
                    aliases.add(assignment_target.id)
                    changed = True
    return frozenset(aliases)


def _names_guarded_verify_call(
    func: ast.expr, target: str, aliases: frozenset[str] = frozenset()
) -> bool:
    name = _called_name(func)
    if name == target or name in aliases:
        return True
    return (
        isinstance(func, ast.Call)
        and isinstance(func.func, ast.Name)
        and func.func.id == "getattr"
        and len(func.args) >= 2
        and isinstance(func.args[1], ast.Constant)
        and func.args[1].value == target
    )


def _names_verify_commands_outcome(func: ast.expr, aliases: frozenset[str] = frozenset()) -> bool:
    """Whether a call's callee names ``verify_commands_outcome``.

    Direct names, attributes, rename-on-import, assignment aliases, and literal
    ``getattr`` calls are covered. A computed target name is deliberately beyond
    this static tripwire and remains a review-time concern."""
    return _names_guarded_verify_call(func, "verify_commands_outcome", aliases)


def _names_verify_classifier(func: ast.expr, aliases: frozenset[str] = frozenset()) -> bool:
    """Whether a call's callee names ``verify_command_results_outcome`` — the
    classifier half of the composition. Same reach and computed-name bound as
    :func:`_names_verify_commands_outcome`."""
    return _names_guarded_verify_call(func, "verify_command_results_outcome", aliases)


def _is_str_composition(node: ast.expr) -> bool:
    """Whether this expression BUILDS a string rather than naming one, in three
    spellings — NOT "the three spellings a hand-minted task id can take", which is
    an overclaim the shapes below cannot support.

    ``JoinedStr`` is the f-string. ``BinOp`` with a str ``Constant`` on either side
    covers both concatenation (``story + "-review-1"``) and percent formatting
    (``"%s-dev-%d" % (key, n)``), whose operator is also a ``BinOp``. The third is
    ``"…".format(…)`` on a literal receiver.

    Three real compositions this deliberately does NOT recognise, verified silent:
    ``"-".join([key, "dev", "1"])``, ``fmt % (key, n)`` where ``fmt`` is a Name bound
    to the format string, and any of the three assembled a statement earlier and
    forwarded through a variable. See the ``NOT COVERED`` note on the detector for
    why the boundary sits where it does.

    A ``Name``, ``Attribute``, ``Subscript`` or ordinary ``Call`` is deliberately NOT
    a composition: those FORWARD a string someone else made, which is what every
    sanctioned mint site does with the chokepoint's return value."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and any(
        isinstance(side, ast.Constant) and isinstance(side.value, str)
        for side in (node.left, node.right)
    ):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    )


def _is_bare_str(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _mint_candidates(node: ast.expr, depth: int = 0):
    """``(sub-expression, depth)`` for every value position that could be minting a
    string here, where depth counts the CALL boundaries crossed to reach it.

    Conditionals and boolean fallbacks are descended at the same depth, since both
    branches are the same value position (``task_id = f"…" if x else base``).

    Call arguments are descended too, in EVERY position, because a call is the shape
    a mint hides behind in both of them. In a return it is the sanitizer the
    chokepoint itself uses — ``return safe_segment(f"{story_key}-{part}-{seq}{gen}")``
    — and in a binding it is the same line copied into one: ``task_id =
    safe_segment(f"{key}-dev-1")`` is the most likely fifth mint precisely because it
    is the chokepoint's own body moved. Refusing to descend there left that shape
    silent (verified), and it omits the ``-g<N>`` suffix, which is #705 re-opened.

    Depth is what makes descending safe. A bare string Constant is a mint only at
    depth 0 (``task_id = "triage-1"``); at depth it is an ARGUMENT and flagging it
    would hit ``os.environ.get("BMAD_LOOP_TASK_ID")`` and the ``"dev"`` part in every
    sanctioned ``_session_task_id(key, "dev", seq, gen)`` call. A COMPOSITION is a
    mint at any depth: nothing legitimate hands a freshly built string to a call in a
    ``task_id`` position."""
    yield node, depth
    if isinstance(node, ast.IfExp):
        yield from _mint_candidates(node.body, depth)
        yield from _mint_candidates(node.orelse, depth)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _mint_candidates(value, depth)
    elif isinstance(node, ast.Call):
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            yield from _mint_candidates(arg, depth + 1)


def _kind_param_default(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The string-literal default of ``fn``'s ``kind`` parameter — positional-or-
    keyword or keyword-only — or None when there is no such parameter or its default
    is not a string literal. The declared dynamic-kind positions mint their fallback
    kind here (``review-skipped``, ``sweep-bundle-closed``), and nothing else in
    the scan reads a parameter default."""
    args = fn.args
    positional = args.posonlyargs + args.args
    padded: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    padded.extend(args.defaults)
    for arg, default in [*zip(positional, padded), *zip(args.kwonlyargs, args.kw_defaults)]:
        if arg.arg == "kind":
            if isinstance(default, ast.Constant) and isinstance(default.value, str):
                return default.value
            return None
    return None


def _kind_param_index(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int | None:
    """Index of ``fn``'s ``kind`` parameter within a CALL's positional argument
    list, or None when it cannot arrive positionally — keyword-only, or absent.

    A leading ``self``/``cls`` is dropped: every declared dynamic-kind position is a
    method reached as ``self._log(...)``, where the receiver is bound and never
    occupies a slot in ``Call.args``. The unbound spelling (``Bus._log(bus, kind)``)
    would shift by one and is deliberately out of scope — it appears nowhere on this
    tree, and reading the receiver where a kind was expected yields a non-literal and
    therefore no finding, which is a miss rather than a false alarm.

    The sibling of :func:`_kind_param_default`, and needed for the same reason: a
    literal reaches a dynamic-kind position three ways — a caller's keyword, a
    caller's POSITIONAL argument, and the parameter default — and reading only two of
    them leaves the third ungraded while the inventory reports itself complete."""
    positional = fn.args.posonlyargs + fn.args.args
    if positional and positional[0].arg in ("self", "cls"):
        positional = positional[1:]
    for index, arg in enumerate(positional):
        if arg.arg == "kind":
            return index
    return None


# A `kind` argument at a declared dynamic-kind position that the scan could not
# resolve: a `*args` splat covering the parameter's slot, a `**` splat the scan cannot
# read into (a Name, a computed key, a nested `{**other}`, or a non-literal `kind`
# value), or a non-literal expression in that slot or in a `kind=` keyword. Emitted AS
# a kind so the inventory arm reddens naming the site: no row can declare it, and an
# unreadable argument must not share its silence with "this call passed no literal".
UNRESOLVED_DYNAMIC_KIND = "<unresolved-dynamic-kind>"


def _positional_kind_literal(node: ast.Call, index: int) -> str | None:
    """The string literal a call hands a declared dynamic-kind position
    POSITIONALLY; :data:`UNRESOLVED_DYNAMIC_KIND` when that slot is OCCUPIED by
    something the scan cannot read — a ``*args`` splat covering it, or a non-literal
    expression in it; None only when the slot is EMPTY, which is the parameter default
    the definition arm reports instead.

    Empty and occupied-but-unreadable are different answers and must not share one
    return value. ``sweep.py``'s own ``_close_bundle_ledger_when_spec_status(task,
    str(spec_file), success_status)`` omits ``kind`` and relies on the default, so
    folding the empty slot into the sentinel reddens the clean tree.

    A non-literal in an occupied slot is FLAGGED, not skipped. Deferring it to the
    literalness test — the rationale this arm used to carry, and the keyword arm with
    it — holds only at the declared FORWARDER (``plugins/bus.py::_log``), whose
    enclosing function at the CALL is not in ``JOURNAL_DYNAMIC_KIND_ALLOW``, so a
    variable there reddens
    ``test_journal_kinds_are_literal_or_the_position_is_declared`` anyway. At the two
    non-forwarder positions (``engine._skip_review_and_commit``,
    ``sweep._close_bundle_ledger_when_spec_status``) the declaration waives exactly that
    test for the write INSIDE the position, so nothing else grades the slot: a caller
    handing it a variable would reach the journal with a kind no row declares while
    every arm stayed green."""
    if any(isinstance(arg, ast.Starred) for arg in node.args[: index + 1]):
        return UNRESOLVED_DYNAMIC_KIND
    if index >= len(node.args):
        return None
    arg = node.args[index]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return UNRESOLVED_DYNAMIC_KIND


def _splat_kind_literal(value: ast.expr) -> str | None:
    """The string literal a ``**`` splat hands a declared dynamic-kind position;
    :data:`UNRESOLVED_DYNAMIC_KIND` when the splat is something the scan cannot read;
    None only when it IS readable and carries no ``kind`` key.

    Three-way for `_positional_kind_literal`'s reason: ABSENT and
    OCCUPIED-BUT-UNREADABLE must not share one return value. A dict literal with no
    ``kind`` key is a readable statement that the slot is empty — the position's own
    parameter default applies and the definition arm reports it — while ``**fields``
    is a statement the scan cannot read at all, and folding the two together would let
    a splat-carried kind reach the journal with no ``JOURNAL_KINDS`` row while the
    completeness assertion stayed green.

    EVERY key/value pair is scanned, and the LAST ``kind`` key wins, because that is
    what Python delivers: reading the first and returning made ``**{"kind": "a",
    "kind": "b"}`` inventory ``a`` while the call shipped ``b``, and let a trailing
    ``**other`` or computed key — either of which can override the entry just read —
    pass as the literal it displaced. The inventoried row has to be the kind that
    ships, or the arm grades a call that does not exist.

    Deliberately unresolved, each landing on the sentinel rather than a guess: an
    ``ast.IfExp`` between two dict literals (``_dict_literal_keys`` may union its KEYS,
    but two branches can carry two different kind VALUES), a Name bound to a dict in
    the same function (``_journal_splat_keys`` resolves those, but keys only), and a
    dict built by a CALL (``**dict(kind="z")``), which the ``ast.Dict`` gate refuses
    because the callee is not necessarily ``dict``."""
    if not isinstance(value, ast.Dict):
        return UNRESOLVED_DYNAMIC_KIND
    found: str | None = None
    for key, item in zip(value.keys, value.values):
        # A `None` key node is `{**other}`; anything else non-static is computed.
        # Either can carry a `kind` the scan cannot see, and a TRAILING one overwrites
        # the literal just read — `{"kind": "x", **other}` may ship anything. A LEADING
        # one is refused too: it is displaced by a later literal, but reading it would
        # mean tracking which side of the unreadable entry each key sits on, and the
        # shape does not occur, so the whole splat is unreadable wherever it sits.
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return UNRESOLVED_DYNAMIC_KIND
        if key.value == "kind":
            found = (
                item.value
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
                else UNRESOLVED_DYNAMIC_KIND
            )
    return found


def _journal_keyword_kinds(node: ast.Call) -> list[str]:
    """Every kind a call delivers through the KEYWORD channel, in source order: an
    explicit ``kind=`` (its literal, or :data:`UNRESOLVED_DYNAMIC_KIND` when the value
    is not a string literal) and each ``**`` splat read through
    :func:`_splat_kind_literal`, whose ``None`` — a readable splat spelling no ``kind``
    key — is skipped rather than inventoried.

    Written so the journal-write emit in ``_scan_source`` grades
    ``journal.append(**{"kind": "x"})`` the way the caller-side dynamic-kind arm grades
    ``self._skip_review_and_commit(task, **{"kind": "x"})``: through the same
    ``_splat_kind_literal``, with the same three-way answer and the same
    last-``kind``-key-wins rule. The two also diverge deliberately in WHEN they consult
    the channel — the caller-side arm reads it per expression, unconditionally, while
    the journal-write emit reads it only when nothing occupies the positional slot,
    because a filled slot already owns the kind (see the call site).

    Stated bound: this is NOT the single implementation of the keyword channel. The
    caller-side arm still carries its own inline copy of these two branches; only
    ``_splat_kind_literal`` is genuinely shared, and nothing asserts that the two
    readings agree. Folding that arm onto this helper was ruled out of scope, so a
    change to one branch here has to be mirrored there by hand.

    Every keyword is yielded, not just the first: a call cannot legally deliver two
    kinds, but a call the scan cannot fully read can deliver an unreadable one BESIDE a
    readable one, and collapsing those to a single answer is how a kind no row declares
    reaches the journal with nothing red."""
    kinds: list[str] = []
    for kw in node.keywords:
        if kw.arg is None:
            splat = _splat_kind_literal(kw.value)
            if splat is not None:
                kinds.append(splat)
        elif kw.arg == "kind":
            kinds.append(
                kw.value.value
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
                else UNRESOLVED_DYNAMIC_KIND
            )
    return kinds


def _is_journal_write(node: ast.AST, rel: str) -> bool:
    """Whether this node writes a journal entry — a ``<journal>.append(...)`` call in
    each of the four receiver spellings the scan reads: the three named handles (see
    ``JOURNAL_RECEIVERS``) and the constructor-inline ``Journal(run_dir).append(...)``
    — or a call to one of this file's declared ``JOURNAL_FORWARDERS``. The tree's
    fifth spelling, ``super().append(...)`` inside ``runs._RearmJournal``'s override,
    is a stated bound (``JOURNAL_RECEIVERS``), not a receiver.

    The forwarder half is not a convenience. ``plugins/bus.py::_log`` takes its own
    ``**fields`` and hands them to ``self._journal.append``, so its four call sites
    spell keywords that reach the journal while matching nothing the ``.append``
    scan looks at — `rc` and `blocking` were in neither routing set with this guard
    green. Keyed ``(file, name)``: a ``_log`` elsewhere forwards to something else.

    The receiver's qualifier is ignored for ``_called_name``'s reason: an aliased
    MODULE handle reaches the same method. A locally aliased receiver is a stated
    bound — see ``JOURNAL_RECEIVERS``."""
    if not isinstance(node, ast.Call):
        return False
    name = _called_name(node.func)
    if name is None:
        return False
    if (rel, name) in JOURNAL_FORWARDERS:
        return True
    if not (isinstance(node.func, ast.Attribute) and name == "append"):
        return False
    receiver = node.func.value
    if _called_name(receiver) in JOURNAL_RECEIVERS:
        return True
    # The constructor-inline spelling: `Journal(run_dir).append(...)`. The receiver
    # is an ast.Call, so the named-handle match above can never see it — runs.py's
    # stop/restamp records (and their kinds and fields) went unscanned exactly this
    # way. Name-anchored on `Journal` like the handle arm, so a lookalike
    # constructor stays silent — and so, by the same anchor, does a subclass
    # constructor or an import alias (the stated bound on `JOURNAL_RECEIVERS`).
    return isinstance(receiver, ast.Call) and _called_name(receiver.func) == "Journal"


def _dict_literal_keys(value: ast.expr) -> set[str] | None:
    """The string keys of a dict literal, or None when any key is not a static
    string. ``{**other}`` yields a ``None`` key node and is unresolvable by
    definition; a conditional between two literals resolves to their union, which is
    how ``engine._run_inner`` builds its ``extras``."""
    if isinstance(value, ast.Dict):
        keys: set[str] = set()
        for key in value.keys:
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                return None
            keys.add(key.value)
        return keys
    if isinstance(value, ast.IfExp):
        body, orelse = _dict_literal_keys(value.body), _dict_literal_keys(value.orelse)
        return None if body is None or orelse is None else body | orelse
    return None


def _journal_splat_keys(fn: ast.AST | None, name: str) -> set[str] | None:
    """The keys a ``**name`` splat can carry, resolved through the same-function
    literal stores that build it, or None when ANY store is not statically
    resolvable.

    Fails closed on purpose, in four directions, because a partially-resolved
    splat would under-report and read as green: an augmented assignment
    (``fields += …``), a method mutation (``fields.update(…)``,
    ``fields.setdefault(…)``), a non-literal store (a computed subscript key, a
    dict built from a call), and a SECOND NAME bound to the same dict
    (``alias = fields``) each return None rather than the keys seen so far. A
    splat with no store in the function at all — the forwarder shape, where ``name``
    is a parameter — is unresolvable too, not vacuously empty.

    The alias direction was the fourth leak in a docstring that claimed three:
    ``fields = {"a": 1}`` / ``alias = fields`` / ``alias["customer_email"] = 2``
    resolved to ``{"a"}``, because every store the resolver looks for is spelled on
    the OTHER name. Matched narrowly — the assigned value must BE ``Name(name)``,
    not merely mention it — so a read (``n = len(fields)``) still resolves."""
    if fn is None:
        return None
    keys: set[str] = set()
    stored = False
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node.value, ast.Name) and node.value.id == name:
                return None
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == name:
                    stored = True
                    resolved = None if node.value is None else _dict_literal_keys(node.value)
                    if resolved is None:
                        return None
                    keys |= resolved
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == name
                ):
                    stored = True
                    if not (
                        isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                    ):
                        return None
                    keys.add(target.slice.value)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return None
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
        ):
            return None
    return keys if stored else None


def _bound_names(target: ast.expr) -> set[str]:
    """Every ``Name`` appearing in an assignment/loop TARGET.

    Deliberately over-approximate — a subscript target (``d[key] = v``) reports both
    ``d`` and ``key``, neither of which it rebinds. Over-reporting a binding can only
    make :func:`_loop_literal_bindings` fail closed; under-reporting one would let a
    rebound name resolve to a stale literal."""
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}


def _arguments_bind(args: ast.arguments, name: str) -> bool:
    """True when a ``def``/``lambda`` parameter list binds ``name`` — positional-only,
    positional, keyword-only, ``*args`` and ``**kwargs`` alike.

    Shared by :func:`_rebinds_name`'s two callable arms so a ``lambda`` parameter
    shadowing a loop name cannot be read as the loop's value while the identical
    ``def`` shape refuses."""
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        every.append(args.vararg)
    if args.kwarg is not None:
        every.append(args.kwarg)
    return any(arg.arg == name for arg in every)


def _rebinds_name(node: ast.AST, name: str) -> bool:
    """True when ``node`` binds ``name`` by any route OTHER than a ``for``
    statement — the one route :func:`_loop_literal_bindings` can read.

    Every arm is a fail-closed direction, not a completeness claim: an assignment, a
    walrus, a ``with … as``, an ``except … as``, an import alias, a comprehension
    target (its own scope, but its literal is not the loop's), a ``global``/
    ``nonlocal`` declaration, a nested def/class of that name, and a parameter of a
    ``def`` or ``lambda`` — the enclosing function's own included. Any of them means
    the name is not solely the loop's, so the resolver refuses rather than answering
    from the ``for`` alone.

    A shape no arm names still resolves from the ``for`` alone; a ``match`` capture
    pattern (``case str() as family:``) is the known one. Each arm is pinned by a row
    of ``test_journal_minted_kind_probes_fail_loud_on_an_unresolvable_interpolation``,
    so a deleted arm reddens; an arm never added is a gap this docstring does not
    claim to close."""
    if isinstance(node, ast.Assign):
        return any(name in _bound_names(t) for t in node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return name in _bound_names(node.target)
    if isinstance(node, ast.comprehension):
        return name in _bound_names(node.target)
    if isinstance(node, ast.withitem):
        return node.optional_vars is not None and name in _bound_names(node.optional_vars)
    if isinstance(node, ast.ExceptHandler):
        return node.name == name
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return any((alias.asname or alias.name.split(".")[0]) == name for alias in node.names)
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return name in node.names
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name == name or _arguments_bind(node.args, name)
    if isinstance(node, ast.Lambda):
        return _arguments_bind(node.args, name)
    if isinstance(node, ast.ClassDef):
        return node.name == name
    return False


def _sequence_literal_elements(node: ast.expr) -> list[ast.expr] | None:
    """The elements of a literal tuple/list/set display, or None for anything else —
    a name, a call, a comprehension, or a display carrying a ``*`` unpacking, whose
    element count is not knowable statically."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)) and not any(
        isinstance(el, ast.Starred) for el in node.elts
    ):
        return list(node.elts)
    return None


def _loop_column_literals(iterable: ast.expr, column: int | None) -> set[str] | None:
    """The string literals a ``for`` target takes from ``iterable``: the elements
    themselves when ``column`` is None (a bare ``Name`` target), else index
    ``column`` of each element (a ``Tuple`` target unpacked from a literal sequence
    of literal sequences). None whenever any step is not statically readable."""
    elements = _sequence_literal_elements(iterable)
    if elements is None:
        return None
    values: set[str] = set()
    for element in elements:
        item = element
        if column is not None:
            # Unpacking is positional; a set display's AST order is not its
            # iteration order, so it cannot supply a statically known column.
            inner = (
                _sequence_literal_elements(element)
                if isinstance(element, (ast.Tuple, ast.List))
                else None
            )
            if inner is None or column >= len(inner):
                return None
            item = inner[column]
        if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
            return None
        values.add(item.value)
    return values


def _scoped_walk(fn: ast.AST):
    """``(node, nested)`` for every node under ``fn``, where ``nested`` is True once
    the walk has entered a nested ``def``/``lambda``/class.

    ``ast.walk`` cannot express this and answering without it is wrong, not merely
    coarse: a ``for family in ("PHANTOM",)`` inside a nested helper binds a name the
    f-string in the OUTER body never sees, and unioning it mints a spelling the code
    cannot write. It also put the binder at odds with :func:`_rebinds_name`, which
    already treats a nested ``def`` of that name as a reason to refuse."""
    stack: list[tuple[ast.AST, bool]] = [(fn, False)]
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    while stack:
        node, nested = stack.pop()
        yield node, nested
        inner = nested or (isinstance(node, scopes) and node is not fn)
        stack.extend((child, inner) for child in ast.iter_child_nodes(node))


def _loop_literal_bindings(fn: ast.AST | None, name: str) -> set[str] | None:
    """The string-literal values ``name`` takes from ``for`` targets in the enclosing
    function's OWN scope, or None when a binding the resolver models is not statically
    readable.

    The reader behind the minted-kind axis: ``f"{family}-pruned"`` in
    ``recovery_flow.prune_preserve_refs`` is only legible because ``family`` is bound
    by ``for family, prune in (("attempt-preserve", …), ("attempt-preserve-dirty",
    …))`` in the same function, over a literal tuple of literal tuples. Values are
    UNIONED across every such loop, so a name bound by two loops mints from both.

    Fails closed like :func:`_journal_splat_keys`, and for the same reason — a
    partially-resolved name would under-report and read as green. A name with NO
    ``for`` binding in the function (a parameter, a module global, a value from a
    call) is unresolvable, not vacuously empty; a ``for`` over anything but a literal
    sequence, a starred or nested target, a ``for`` binding in a NESTED scope the
    f-string cannot see, a name bound by a second route (:func:`_rebinds_name`), and a
    column the element sequence is too short for each return None rather than the
    values seen so far. The caller turns None into :data:`UNRESOLVED_DYNAMIC_KIND`,
    which no declaration can match.

    Bound, stated where :func:`_rebinds_name` states its own: the refusals are the
    enumerated ones, not every binding Python has. A rebinding route no arm names
    (a ``match`` capture pattern, say) still resolves from the ``for`` alone."""
    if fn is None:
        return None
    values: set[str] = set()
    bound = False
    for node, nested in _scoped_walk(fn):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            target = node.target
            if name not in _bound_names(target):
                continue
            if nested:
                # A loop inside a nested def/lambda binds a name the enclosing body's
                # f-string cannot see: refuse rather than union in a phantom spelling.
                return None
            bound = True
            column: int | None = None
            if not isinstance(target, ast.Name):
                if not isinstance(target, (ast.Tuple, ast.List)):
                    return None
                indices = [
                    index
                    for index, element in enumerate(target.elts)
                    if isinstance(element, ast.Name) and element.id == name
                ]
                # A nested, duplicated or starred target holding the name is legal
                # Python the resolver deliberately does not model.
                if len(indices) != 1 or any(
                    isinstance(element, (ast.Tuple, ast.List, ast.Starred))
                    for element in target.elts
                ):
                    return None
                column = indices[0]
            resolved = _loop_column_literals(node.iter, column)
            if resolved is None:
                return None
            values |= resolved
        elif _rebinds_name(node, name):
            return None
    return values if bound else None


def _fstring_kind_spellings(fn: ast.AST | None, joined: ast.JoinedStr) -> set[str]:
    """Every kind spelling an f-string in the KIND slot can mint: literal parts
    verbatim, each ``{name}`` expanded to that name's resolved loop bindings, crossed
    over the parts.

    Never empty and never skipped. A part the scan cannot reduce to string literals —
    a call, an attribute, an expression, a conversion (``!r``) or a format spec, or a
    name :func:`_loop_literal_bindings` refuses — contributes
    :data:`UNRESOLVED_DYNAMIC_KIND` instead, so the spelling that comes out cannot
    match any declaration and reddens the minting assertion naming the site. That is
    the same stance :func:`_positional_kind_literal` takes: unreadable must not read
    as clean."""
    parts: list[set[str]] = []
    for part in joined.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append({part.value})
            continue
        resolved = None
        if (
            isinstance(part, ast.FormattedValue)
            and part.conversion in (-1, None)
            and part.format_spec is None
            and isinstance(part.value, ast.Name)
        ):
            resolved = _loop_literal_bindings(fn, part.value.id)
        parts.append(resolved or {UNRESOLVED_DYNAMIC_KIND})
    return {"".join(combination) for combination in product(*parts)}


def _enclosing_function_names(tree: ast.AST) -> dict[int, str | None]:
    """``id(node) -> the name of the INNERMOST function definition containing it``
    (None at module level).

    ``ast`` nodes carry no parent link and ``ast.walk`` hands them out flat, so the
    journal detector — whose splat resolution and whose ``JOURNAL_SPLAT_ALLOW`` key
    are both scoped to the function a call sits in — has to build the mapping
    itself. Innermost rather than outermost, because that is the scope a ``**name``
    is stored in.

    Deliberately different from the sanctioned-position sets built inside
    ``_scan_source``: those use ``ast.walk(fn)``, which descends into nested defs so
    a closure inside a sanctioned helper stays sanctioned. Here the innermost answer
    is the correct one, and the two uses are not interchangeable."""
    names: dict[int, str | None] = {id(tree): None}

    def descend(node: ast.AST, fn: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            names[id(child)] = fn
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            descend(child, inner)

    descend(tree, None)
    return names


def _enclosing_function_nodes(tree: ast.AST) -> dict[int, ast.AST | None]:
    """The node-valued twin of :func:`_enclosing_function_names`, for the splat
    resolver, which must WALK the enclosing function rather than name it."""
    nodes: dict[int, ast.AST | None] = {id(tree): None}

    def descend(node: ast.AST, fn: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            nodes[id(child)] = fn
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            descend(child, inner)

    descend(tree, None)
    return nodes


def _names_rearm_escalation(func: ast.expr, aliases: frozenset[str] = frozenset()) -> bool:
    """True when ``func`` spells the re-arm transaction's entry point.

    Qualified and bare spellings are direct matches; ``aliases`` adds ordinary
    rename-on-import and assignment bindings. Matching an attribute without checking
    its value means an unrelated ``x.rearm_escalation(...)`` also registers — that
    false positive is a review prompt naming a real call to a function of that name,
    which is the trade every sibling detector in this file makes.
    """
    return _names_guarded_verify_call(func, "rearm_escalation", aliases)


def _names_isolation_refusal(
    func: ast.expr,
    predicate_aliases: frozenset[str] = frozenset(),
    wrapper_aliases: frozenset[str] = frozenset(),
) -> bool:
    """True when ``func`` spells a #414-family refusal entry point: the
    ``bmadconfig.worktree_isolation_conflict`` predicate or its rc-returning CLI
    wrapper ``_reject_isolation_conflict``. Both names are guarded because a new
    surface can reach the refusal through either — the ``96aa09a9`` site did so
    through the wrapper — and each resolves its own alias set. Same reach and
    computed-name bound as the sibling detectors, and the same trade: an unrelated
    ``x.worktree_isolation_conflict(...)`` is a review prompt, not a miss."""
    return _names_guarded_verify_call(
        func, "worktree_isolation_conflict", predicate_aliases
    ) or _names_guarded_verify_call(func, "_reject_isolation_conflict", wrapper_aliases)


def _block_exits(body: list[ast.stmt]) -> bool:
    """Whether this simple guard body cannot fall through to the re-arm below it."""
    return bool(body) and isinstance(body[-1], (ast.Return, ast.Raise))


def _liveness_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and LIVENESS_GATE_MARK in (_called_name(node.func) or "")


def _top_level_liveness_bindings(fn: ast.AST, lineno: int) -> set[str]:
    """Names bound by an earlier top-level liveness probe in ``fn``."""
    bindings: set[str] = set()
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    for stmt in fn.body:
        if stmt.lineno >= lineno or not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        value = stmt.value
        if value is None or not _liveness_call(value):
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        bindings.update(target.id for target in targets if isinstance(target, ast.Name))
    return bindings


def _test_uses_liveness(test: ast.expr, bindings: set[str]) -> bool:
    return any(
        _liveness_call(node) or (isinstance(node, ast.Name) and node.id in bindings)
        for node in ast.walk(test)
    )


def _consults_liveness_before(fn: ast.AST | None, lineno: int) -> bool:
    """True when a preceding liveness decision blocks fall-through to the re-arm.

    The two real callers keep the gate in their top-level statement sequence: the TUI
    calls its boolean helper directly in an ``if`` and the CLI binds ``engine_liveness``
    before testing that result. Requiring a terminating guard body deliberately rejects
    an ignored probe, a probe hidden in an uncalled nested function, and one conditional
    on an unrelated outer branch. A more deeply factored gate is a review prompt rather
    than a silent pass.
    """
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    bindings = _top_level_liveness_bindings(fn, lineno)
    for stmt in fn.body:
        if stmt.lineno >= lineno or not isinstance(stmt, ast.If):
            continue
        if _block_exits(stmt.body) and _test_uses_liveness(stmt.test, bindings):
            return True
    return False


def _scan():
    """Single pass over the tree → list of (kind, rel, lineno, line_text)."""
    findings = []
    for path in _py_files():
        findings.extend(_scan_source(path.read_text(encoding="utf-8"), _rel(path)))
    return findings


def _function_body_nodes(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Every node in ``fn``'s BODY, nested defs included.

    ``ast.walk(fn)`` also hands back the decorators, the default arguments and the
    return annotation — expressions Python evaluates where the function is DEFINED,
    not calls made from inside it. A sanctioned-position set built from the full
    walk therefore sanctions a call written in a decorator or a default, which is
    exactly the bypass those sets exist to refuse.

    Walking each body statement instead keeps the nested-def descent the sets rely
    on: a closure inside a sanctioned helper stays sanctioned, and that closure's
    OWN decorators and defaults stay in too, because those are evaluated in the
    enclosing body.
    """
    return [node for stmt in fn.body for node in ast.walk(stmt)]


def _scan_source(src: str, rel: str):
    """The whole per-file scan, over one source string → the same
    ``(kind, rel, lineno, line_text)`` tuples ``_scan`` collects.

    Split out from ``_scan`` so the detectors can be driven by a snippet and not
    only by what happens to be in the tree today. A repo-wide "nothing is flagged"
    assertion is green both when the invariant holds and when the detector has
    quietly stopped detecting; the probes below feed known-bad sources through
    THIS function — the same code path the real scan uses — so the two failure
    modes stop being indistinguishable."""
    findings = []
    lines = src.splitlines()
    tree = ast.parse(src, filename=rel)
    docs = _docstring_node_ids(tree)
    env_aliases = _env_name_aliases(tree)
    verify_command_aliases = _call_aliases(tree, "verify_commands_outcome")
    verify_classifier_aliases = _call_aliases(tree, "verify_command_results_outcome")
    rearm_aliases = _call_aliases(tree, "rearm_escalation")
    isolation_aliases = _call_aliases(tree, "worktree_isolation_conflict")
    isolation_wrapper_aliases = _call_aliases(tree, "_reject_isolation_conflict")

    # First positional args of `_run_git(...)` calls — the one position where a
    # git argv literal feeds the chokepoint instead of bypassing it. Collected up
    # front so the walk below can tag each git finding; an argv bound to a name
    # first is deliberately NOT resolved through the binding (same stance as the
    # tuple form: a false positive is a review prompt, not a miss).
    run_git_argvs = {
        id(call.args[0])
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_run_git"
        and call.args
    }
    git_heads, git_commands = _git_name_bindings(tree)

    # Calls inside the value of a `probe = ...` assignment that sits inside the
    # `try` of a bare `except Exception` — `deferredwork.py`'s ADVISORY pre-lock
    # probes (#736), which are neither arm of the DW-146 contract and keep their
    # bare read on purpose: they decide nothing, and the locked read below each one
    # is the repair/write site that does. The walk of `assign.value` covers the
    # `probe = ... if path.is_file() else ""` spelling, where the call is nested
    # inside an IfExp rather than being the value itself.
    #
    # BOTH halves are required, and neither alone would do. The assigned NAME rather
    # than the enclosing function, because allowlisting `_mark_done_many` wholesale
    # would re-sanction the very locked read the contract exists to route — the two
    # live in the same function. And the swallowing `try` on top of the name, because
    # the name alone exempted any read a future edit chose to call `probe`: a
    # write-bearing read spelled `probe = ledger.read_text(...)` — the shape whose
    # fault a repair/write site must escalate — would have inherited the advisory
    # sites' pass. What makes a probe advisory is that a fault in it decides nothing,
    # and `except Exception` around it is precisely that property written down.
    swallowing_try_bodies = {
        id(stmt)
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in node.handlers)
        for body_stmt in node.body
        for stmt in ast.walk(body_stmt)
    }
    advisory_probe_calls = {
        id(call)
        for assign in ast.walk(tree)
        if isinstance(assign, ast.Assign)
        and id(assign) in swallowing_try_bodies
        and any(isinstance(t, ast.Name) and t.id == "probe" for t in assign.targets)
        for call in ast.walk(assign.value)
        if isinstance(call, ast.Call)
    }

    # `verify_commands_outcome(...)` calls that sit inside a
    # `_verify_review_commands` definition — the review gates' single sanctioned
    # composition point. Collected up front, exactly like `run_git_argvs` above,
    # so the walk can tag each finding with the position bit instead of trying to
    # rediscover its enclosing function from a bare node.
    #
    # Nested defs are covered because `_function_body_nodes` walks each body
    # statement, and the enclosing-name check is paired with a FILE check in the
    # offender filter — a `_verify_review_commands` grown in some other module must
    # not sanction itself by name alone. Decorators and defaults are NOT the body,
    # so a call parked in one does not sanction itself.
    sanctioned_verify_command_calls = {
        id(call)
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and fn.name == VERIFY_COMMANDS_SANCTIONED_CALLER
        for call in _function_body_nodes(fn)
        if isinstance(call, ast.Call)
        and _names_verify_commands_outcome(call.func, verify_command_aliases)
    }

    # The same collection for the classifier half. `.get(rel)` is None in every
    # file that has no sanctioned position, and no function is named None, so the
    # set comes out empty there — which is what makes the file half of the filter
    # bite without a second membership test here.
    sanctioned_classify_calls = {
        id(call)
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and fn.name == VERIFY_CLASSIFY_CHOKEPOINT.get(rel)
        for call in _function_body_nodes(fn)
        if isinstance(call, ast.Call)
        and _names_verify_classifier(call.func, verify_classifier_aliases)
    }

    # String Constants that ARE the task-artifact list rather than a copy of it: the
    # elements of `journal.TASK_CYCLE_ARTIFACTS`' own assignment. Skipped by id, so
    # the definition needs no allowlist entry and a bare literal elsewhere in the
    # same file is still refused (see TASK_ARTIFACT_DEFINITION).
    artifact_definition_rel, artifact_definition_name = TASK_ARTIFACT_DEFINITION
    artifact_definition_nodes = {
        id(const)
        for stmt in ast.walk(tree)
        if rel == artifact_definition_rel
        and isinstance(stmt, (ast.Assign, ast.AnnAssign))
        and stmt.value is not None
        and any(
            isinstance(target, ast.Name) and target.id == artifact_definition_name
            for target in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target])
        )
        for const in ast.walk(stmt.value)
        if isinstance(const, ast.Constant)
    }

    # Everything inside this file's ONE sanctioned task-id composition point, if it
    # has one. Same `_function_body_nodes(fn)` shape as the verify sets above — a
    # nested def inside the chokepoint is still inside it, a decorator or default is
    # not — and empty in every other file, since `.get(rel)` is None there and no
    # function is named None.
    sanctioned_task_id_nodes = {
        id(inner)
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and fn.name == SESSION_TASK_ID_CHOKEPOINT.get(rel)
        for inner in _function_body_nodes(fn)
    }

    # `return` statements inside a function whose NAME contains `task_id` — the
    # second position a mint can hide in, and the one a helper like
    # `_sweep_task_id` would use. Matched on the name substring rather than on a
    # fixed list: naming the function after what it returns is the whole tell.
    task_id_returns = {
        id(ret)
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and "task_id" in fn.name
        for ret in ast.walk(fn)
        if isinstance(ret, ast.Return) and ret.value is not None
    }

    enclosing_names = _enclosing_function_names(tree)
    enclosing_nodes = _enclosing_function_nodes(tree)

    def line_at(lineno: int) -> str:
        return lines[lineno - 1] if 1 <= lineno <= len(lines) else ""

    for node in ast.walk(tree):
        # spawn-argv literals: ["tmux", ...] / ["git", ...] — each quarantined to
        # its owner. tmux matches lists only: the which-list *tuple*
        # ("tmux", ...) is a real lookup shape in the tree. git matches tuples
        # too — subprocess accepts any sequence, and git has no legitimate tuple
        # form to spare, so the tuple spelling of a bypass must not slip the
        # net. A path segment ("git" outside a sequence) and prose stay silent.
        # A git head also resolves through the module's own constant bindings
        # (`GIT = "git"` — see `_git_name_bindings`), and each git finding carries
        # one extra field: whether the literal sits in the argv position of a
        # `_run_git(...)` call — the only spot the chokepoint file's own
        # exemption covers.
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if (
                isinstance(first, ast.Constant)
                and first.value == "tmux"
                and isinstance(node, ast.List)
            ):
                findings.append(("tmux", rel, node.lineno, line_at(node.lineno)))
            if (isinstance(first, ast.Constant) and first.value == "git") or (
                isinstance(first, ast.Name) and first.id in git_heads
            ):
                findings.append(
                    ("git", rel, node.lineno, line_at(node.lineno), id(node) in run_git_argvs)
                )

        # string-form git spawn: `subprocess.run("git status", shell=True)`, or
        # the same string with no shell — a spelling Windows execs directly. The
        # sequence detector never sees it, and in the SHELL_ALLOW files the
        # shell guard is silent too, so it gets its own anchored check (see
        # SPAWN_CALL_NAMES). "git" exactly or a "git " prefix: `gitk` is a
        # different program. The command resolves through the module's constant
        # bindings the same way a sequence head does (`GIT_STATUS = "git
        # status"` — see `_git_name_bindings`). Never the chokepoint's feed
        # position — `_run_git` takes a sequence — so the extra field is
        # constant False.
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            is_spawn = (
                isinstance(func, ast.Attribute)
                and func.attr in SPAWN_CALL_NAMES
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ) or (isinstance(func, ast.Name) and func.id in SPAWN_CALL_NAMES)
            cmd = node.args[0]
            if is_spawn and (
                (
                    isinstance(cmd, ast.Constant)
                    and isinstance(cmd.value, str)
                    and (cmd.value == "git" or cmd.value.startswith("git "))
                )
                or (isinstance(cmd, ast.Name) and cmd.id in git_commands)
            ):
                findings.append(("git", rel, node.lineno, line_at(node.lineno), False))

        # A call to `verify_commands_outcome` — the run+classify composition the
        # three review gates reach through `_verify_review_commands`. Each finding
        # carries one extra field: whether it sits inside that helper, the only
        # position the exemption covers. Prose naming the function (its own
        # docstrings, `cli._reverify`'s "Deliberately NOT ...") is a Constant, not
        # a Call, so it never reaches here.
        if isinstance(node, ast.Call) and _names_verify_commands_outcome(
            node.func, verify_command_aliases
        ):
            findings.append(
                (
                    "verifycmd",
                    rel,
                    node.lineno,
                    line_at(node.lineno),
                    id(node) in sanctioned_verify_command_calls,
                )
            )

        # ... and the classifier half, so a gate that skips the wrapper and
        # composes run+classify by hand is caught by the same pass. Same shape:
        # the finding carries whether it sits in this file's one sanctioned
        # enclosing function.
        if isinstance(node, ast.Call) and _names_verify_classifier(
            node.func, verify_classifier_aliases
        ):
            findings.append(
                (
                    "verifyclassify",
                    rel,
                    node.lineno,
                    line_at(node.lineno),
                    id(node) in sanctioned_classify_calls,
                )
            )

        # bare POSIX path string literal (skip docstrings)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and _classify_posix_path(node.value)
        ):
            findings.append(("path", rel, node.lineno, line_at(node.lineno)))

        # A task-directory artifact name spelled as a literal, outside the one
        # assignment that defines the list. Matched by string EQUALITY, never by
        # containment: the dev/sweep prompts name `result.json` inside a sentence
        # ("…write tasks/<id>/result.json, then end your turn"), and flagging prose
        # would get the allowlist widened until it meant nothing. Docstrings are
        # skipped for the same reason the POSIX-path scan skips them. The finding
        # carries `(name, enclosing function)`: the exemption is per-name AND per
        # position, so a second literal in another function of an allowlisted file
        # is still refused.
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and id(node) not in artifact_definition_nodes
            and node.value in TASK_CYCLE_ARTIFACTS
        ):
            findings.append(
                (
                    "taskartifact",
                    rel,
                    node.lineno,
                    line_at(node.lineno),
                    (node.value, enclosing_names.get(id(node))),
                )
            )

        # A journal write's field names. Explicit keywords are read straight off the
        # call; a `**name` splat is resolved through the literal stores that built it
        # in the same function, and emits ONE finding with a None name when it
        # cannot be — an unresolvable splat is a hole in the inventory, so it fails
        # loud rather than being skipped. Each finding carries
        # `(field_or_None, enclosing_function, kind_or_None)`: the benign inventory
        # is keyed by field, the splat exemption by position, and the KIND is what
        # makes `diagnostics`' kind-scoped routing checkable at all.
        #
        # The kind is the first positional argument when it is a string literal, and
        # None otherwise. None is not "no kind": it is "this scan cannot tell", and
        # it emits its own `journalkind` finding so the site fails loud rather than
        # being graded against a kind that had to be guessed.
        #
        # Read keywords only when `_positional_kind_literal` reports an EMPTY slot.
        # An unreadable or starred positional argument keeps owning that slot. The
        # AST literal extraction below stays independent of the resolver's sentinel:
        # a literal spelling the sentinel is still a literal, including for fields.
        # Without this keyword inventory, the doubly-declared plugins/bus.py::_log
        # site can write append(**{"kind": "x"}) with both waivers hiding the kind.
        if _is_journal_write(node, rel):
            fn_name = enclosing_names.get(id(node))
            # The DEF IDENTITY behind the bare-name position key, once per journal
            # write. Every position table here is keyed by `(file, bare function
            # name)`, an assumption nothing enforced: two same-named journal-writing
            # functions in one module aggregate into one row. The name cannot express
            # the difference, so the enclosing def's lineno is emitted alongside it —
            # read off `enclosing_nodes`, the twin already built for the splat
            # resolver, so no second AST walk is needed. `(None, None)` at module
            # level, which `_journal_bare_name_collisions` skips.
            #
            # `None` is spelled, not defaulted: every node `_enclosing_function_nodes`
            # can hand back is a `FunctionDef`/`AsyncFunctionDef` and carries a
            # `lineno`, so a `getattr` default would never fire on today's tree and
            # would silently degrade the emit to `(name, None)` — the collision
            # helper's skip condition — if that ever stopped holding.
            enclosing_def = enclosing_nodes.get(id(node))
            findings.append(
                (
                    "journalfnscope",
                    rel,
                    node.lineno,
                    line_at(node.lineno),
                    (fn_name, None if enclosing_def is None else enclosing_def.lineno),
                )
            )
            first = node.args[0] if node.args else None
            kind = (
                first.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str)
                else None
            )
            if kind is None:
                findings.append(("journalkind", rel, node.lineno, line_at(node.lineno), fn_name))
                if isinstance(first, ast.JoinedStr):
                    # An f-string kind is MINTED rather than written: no literal for
                    # it exists anywhere in the tree, so the count row above is all
                    # that ever graded the site and a respelling stayed invisible.
                    # Read the spelling instead — literal parts verbatim,
                    # interpolations resolved through the loop bindings that supply
                    # them — one finding per spelling, graded against
                    # `JOURNAL_DYNAMIC_KIND_SPELLINGS`. Unreadable parts arrive as
                    # `UNRESOLVED_DYNAMIC_KIND`, which no row can declare.
                    #
                    # Only a JoinedStr in the POSITIONAL slot, and both halves of
                    # that are bounds. The other dynamic kinds here spell a parameter
                    # (`engine._skip_review_and_commit`,
                    # `sweep._close_bundle_ledger_when_spec_status`,
                    # `plugins/bus.py::_log`), whose literals reach the inventory from
                    # OUTSIDE the position through the `journalkindliteral` arms below.
                    # And an f-string arriving by KEYWORD (`append(kind=f"…")`) mints
                    # nothing here: `_journal_keyword_kinds` already reads that channel
                    # and reports `UNRESOLVED_DYNAMIC_KIND`, which
                    # `test_journal_kind_inventory_is_complete` refuses outright — a
                    # louder answer than a minted spelling, and the reason this axis
                    # does not duplicate it.
                    for spelling in sorted(
                        _fstring_kind_spellings(enclosing_nodes.get(id(node)), first)
                    ):
                        findings.append(
                            (
                                "journalkindminted",
                                rel,
                                node.lineno,
                                line_at(node.lineno),
                                (fn_name, spelling),
                            )
                        )
                if _positional_kind_literal(node, 0) is None:
                    # Nothing occupies the slot: the kind, if any, arrives by keyword.
                    # Unchanged on the `journalkind` axis — whether a POSITION may be
                    # dynamic stays the literalness arm's question, and a keyword-spelled
                    # kind is unusual enough that exempting it should be a deliberate
                    # decision, not a side effect of this one.
                    for keyword_kind in _journal_keyword_kinds(node):
                        findings.append(
                            (
                                "journalkindliteral",
                                rel,
                                node.lineno,
                                line_at(node.lineno),
                                keyword_kind,
                            )
                        )
            else:
                # The literal-kind twin, and the KIND inventory's only feed. NOT
                # derivable from the `journalfield` rows below, although each of
                # those carries the kind: a kind-only write like `run-complete`
                # (no keyword arguments at all, not even a `**` splat) has no
                # keyword row to ride on.
                findings.append(
                    ("journalkindliteral", rel, node.lineno, line_at(node.lineno), kind)
                )
            for kw in node.keywords:
                if kw.arg is not None:
                    findings.append(
                        (
                            "journalfield",
                            rel,
                            node.lineno,
                            line_at(node.lineno),
                            (kw.arg, fn_name, kind),
                        )
                    )
                    continue
                resolved = (
                    _journal_splat_keys(enclosing_nodes.get(id(node)), kw.value.id)
                    if isinstance(kw.value, ast.Name)
                    else None
                )
                if resolved is None:
                    findings.append(
                        (
                            "journalfield",
                            rel,
                            node.lineno,
                            line_at(node.lineno),
                            (None, fn_name, kind),
                        )
                    )
                else:
                    for field in sorted(resolved):
                        findings.append(
                            (
                                "journalfield",
                                rel,
                                node.lineno,
                                line_at(node.lineno),
                                (field, fn_name, kind),
                            )
                        )

        # Deferred-work ledger reads (DW-146). A `<recv>.read_text(...)` whose
        # receiver names the ledger — `ledger`/`ledger_path`/`deferred_work` anywhere,
        # plus `path`/`archive_path` inside the owning module — carries a
        # `sanctioned` bit so the guard can separate "names its arm" from "bare".
        # An attribute receiver (`paths.deferred_work`, `self.workspace.paths.deferred_work`,
        # `self.ledger`) is matched on the trailing attribute against `names` — the SAME
        # set the local branch uses, owner-module widening included. Anything narrower
        # splits the two branches apart: `self.ledger` would read as generic while the
        # bare `ledger` beside it is flagged, and inside `deferredwork.py` `self.path`
        # would be exempt while `path` is not. The ledger travels under these spellings
        # as readily on an attribute as in a local.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read_text"
        ):
            recv = node.func.value
            names = LEDGER_RECEIVER_NAMES | (
                LEDGER_OWNER_RECEIVER_NAMES if rel == LEDGER_OWNER else set()
            )
            is_ledger = (isinstance(recv, ast.Name) and recv.id in names) or (
                isinstance(recv, ast.Attribute) and recv.attr in names
            )
            if is_ledger:
                fn_name = enclosing_names.get(id(node))
                sanctioned = (
                    rel == LEDGER_OWNER
                    and (id(node) in advisory_probe_calls or fn_name in LEDGER_READER_BODIES)
                ) or (rel, fn_name) in LEDGER_READ_INLINE
                findings.append(
                    ("ledgerread", rel, node.lineno, line_at(node.lineno), fn_name, sanctioned)
                )

        # signal.SIGKILL attribute access (the guarded form is a "SIGKILL"
        # *string* passed to getattr — not an attribute access — so it's clean)
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "SIGKILL"
            and isinstance(node.value, ast.Name)
            and node.value.id == "signal"
        ):
            findings.append(("sigkill", rel, node.lineno, line_at(node.lineno)))

        # os.kill(<pid>, 0) — the existence-probe form (signal 0), not a real
        # signal send like os.kill(pid, SIGTERM)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "kill"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == 0
            and node.args[1].value is not False
        ):
            findings.append(("killprobe", rel, node.lineno, line_at(node.lineno)))

        # os.kill(...) in any form — every signal send maps to a destructive
        # TerminateProcess on Windows, so confine the call to the ProcessHost.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "kill"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ):
            findings.append(("oskill", rel, node.lineno, line_at(node.lineno)))

        # start_new_session=True as a call kwarg
        if (
            isinstance(node, ast.keyword)
            and node.arg == "start_new_session"
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        ):
            findings.append(("detach", rel, node.lineno, line_at(node.lineno)))

        # {"start_new_session": True} as a dict literal (the detach-kwargs form)
        if isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "start_new_session"
                    and isinstance(val, ast.Constant)
                    and val.value is True
                ):
                    findings.append(("detach", rel, key.lineno, line_at(key.lineno)))

        # shell=True as a call kwarg
        if (
            isinstance(node, ast.keyword)
            and node.arg == "shell"
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        ):
            findings.append(("shell", rel, node.lineno, line_at(node.lineno)))

        # A `BMAD_LOOP_*` variable READ out of the process environment:
        # os.environ.get(K) / os.environ.pop(K) / os.getenv(K) / os.environ[K].
        # Reads only — the env dicts modules *build* to inject into a child
        # session are the producing side, which the invariant does not constrain.
        env_key = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            key_node = _env_call_key_node(node)
            if key_node is None:
                pass
            elif func.attr in ("get", "pop", "setdefault") and _is_os_environ(func.value):
                env_key = _env_read_key(key_node, env_aliases)
            elif (
                # `getenvb` is the bytes twin, and POSIX-only like `environb`
                func.attr in ("getenv", "getenvb")
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                env_key = _env_read_key(key_node, env_aliases)
        elif (
            isinstance(node, ast.Subscript)
            and _is_os_environ(node.value)
            and isinstance(node.ctx, ast.Load)
        ):
            env_key = _env_read_key(node.slice, env_aliases)
        elif isinstance(node, ast.Compare):
            # `"BMAD_LOOP_X" in os.environ` / `not in` — a presence read, and the
            # most natural way to spell a boolean flag. A chain expands PAIRWISE
            # (`c == K in os.environ` means `c == K and K in os.environ`), so the
            # operand a membership tests is the one to its immediate left — the
            # PRECEDING comparator, not `node.left`, for any op past the first.
            # Carry the left operand across the pairs rather than re-reading
            # `node.left`, which resolves the wrong name on a chain.
            left = node.left
            for op, rhs in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and _is_os_environ(rhs):
                    env_key = _env_read_key(left, env_aliases)
                    if env_key:
                        break
                left = rhs
        if env_key:
            # The only 5-wide finding: the allowlist is keyed by variable FAMILY,
            # not by file, so the filter needs the resolved key and not just the
            # source line — a read spelled through a constant does not carry it.
            findings.append(("envread", rel, node.lineno, line_at(node.lineno), env_key))

    # A raw `Path(x.spec_file)` / `Path(x.dispatched_spec_file)`: the persisted value
    # may be worktree-RELATIVE, so this resolves against the reader's cwd rather than
    # the tree the run owns. Detected as the call shape rather than by name, so an
    # alias (`Path(t.spec_file)`, `Path(self._task.dispatched_spec_file)`) is caught
    # too; the enclosing `if x.spec_file else` ternary does not hide it.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Path"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr in SPEC_PATH_FIELDS
        ):
            findings.append(("specanchor", rel, node.lineno, line_at(node.lineno)))

    # A session task id COMPOSED rather than obtained from `engine._session_task_id`.
    # Two value positions, because those are the two a fifth mint can occupy: a
    # binding (`task_id = …`, `SessionSpec(task_id=…)`) and a return from a function
    # named for what it returns. A forward — `task_id=spec.task_id`,
    # `task_id=str(d["task_id"])`, `task_id=task_id` — reaches neither predicate,
    # which is the distinction the whole detector rests on.
    #
    # Collected into a dict keyed by node id so a value matching through two
    # candidate paths (a `.format()` call is both the candidate itself and the
    # parent of its arguments) reports once.
    #
    # NOT COVERED, deliberately, and stated rather than implied. This is a review
    # tripwire on the shapes the real mint sites use, not a sandbox; widening it is a
    # decision, not a bug fix. Each of these was run through `_scan_source` and
    # confirmed silent:
    #
    # * a store into a dict or an attribute — `record["task_id"] = f"…"`,
    #   `self.task_id = f"…"`. Neither is a Name binding, a `task_id=` keyword, nor a
    #   return from a `*task_id*` function.
    # * an INTERMEDIATE VARIABLE: `tid = f"{key}-dev-1"` on one line and
    #   `task_id=tid` on the next. The binding position holds a Name, which is a
    #   forward as far as this detector can see; following it would mean the
    #   flow-sensitive resolution `_journal_splat_keys` does for one dict, across
    #   every string in the file.
    # * `"-".join([key, "dev", "1"])` and `fmt % (key, n)` where `fmt` is a Name
    #   bound to the format string — two more real ways to build a string that
    #   `_is_str_composition` does not recognise (its own docstring lists them).
    minted: dict[int, ast.expr] = {}

    def record_mint(value: ast.expr, *, bare_at_depth: bool) -> None:
        for candidate, depth in _mint_candidates(value):
            if _is_str_composition(candidate) or (
                _is_bare_str(candidate) and (depth == 0 or bare_at_depth)
            ):
                minted.setdefault(id(candidate), candidate)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "task_id" for t in node.targets):
                record_mint(node.value, bare_at_depth=False)
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "task_id"
                and node.value is not None
            ):
                record_mint(node.value, bare_at_depth=False)
        elif isinstance(node, ast.keyword) and node.arg == "task_id":
            record_mint(node.value, bare_at_depth=False)
        elif isinstance(node, ast.Return) and id(node) in task_id_returns:
            # A function NAMED for the id it returns is already the whole tell, so a
            # bare literal stays a finding at depth there (`return safe_segment("x")`)
            # — unlike a binding, where a literal argument is the sanctioned
            # chokepoint call's own `"dev"` part.
            assert node.value is not None  # task_id_returns only holds valued returns
            record_mint(node.value, bare_at_depth=True)

    for mint in minted.values():
        findings.append(
            (
                "taskid",
                rel,
                mint.lineno,
                line_at(mint.lineno),
                id(mint) in sanctioned_task_id_nodes,
            )
        )

    # Every `rearm_escalation` CALL, carrying `(enclosing function, gated)` — the two
    # facts `REARM_ESCALATION_CALLERS` is an enumeration of. The `def` in `runs.py` is
    # not a Call and needs no exemption.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _names_rearm_escalation(node.func, rearm_aliases):
            findings.append(
                (
                    "rearmcall",
                    rel,
                    node.lineno,
                    line_at(node.lineno),
                    (
                        enclosing_names.get(id(node)),
                        _consults_liveness_before(enclosing_nodes.get(id(node)), node.lineno),
                    ),
                )
            )

    # The literal kinds that reach a declared dynamic-kind POSITION from outside it:
    # a `kind="..."` keyword at a call to one of this file's
    # `JOURNAL_DYNAMIC_KIND_ALLOW` functions, and that function's own `kind`
    # parameter default. The write inside such a position spells a parameter, so
    # the journal-write arm above reports it as `journalkind` and nothing more —
    # which is how a literal that reaches the journal ONLY through such a
    # position (`review-skipped-awaiting-operator`, for one) got there with no
    # inventory row anyone had to decide on (review pass 2). Same
    # `journalkindliteral` finding, same inventory; keyed `(file, name)` exactly
    # like the position it serves, so a same-named callee in a file that declares
    # no such position stays silent.
    # Where each declared position keeps its `kind`, so a caller that spells the
    # kind POSITIONALLY is read too. Keyed by name within this file, exactly like
    # the position mapping it is derived from.
    kind_positions = {
        node.name: _kind_param_index(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (rel, node.name) in JOURNAL_DYNAMIC_KIND_ALLOW
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and (rel, _called_name(node.func)) in JOURNAL_DYNAMIC_KIND_ALLOW
        ):
            index = kind_positions.get(_called_name(node.func))
            for kw in node.keywords:
                if kw.arg is None:
                    # A `**` splat: `self._skip_review_and_commit(task, **{"kind":
                    # "new-kind"})` is legal Python that reaches the journal, and this
                    # loop skipped it entirely — `kw.arg` is None, so the `!= "kind"`
                    # test below dropped it and the inventory reported itself complete.
                    # None means the splat is readable and carries no `kind` (the
                    # parameter default applies, which the definition arm reports);
                    # anything unreadable is the sentinel, never silence.
                    #
                    # Judged PER EXPRESSION, unconditionally: no guard here consults
                    # the call's other arguments. A reachability contract — go silent
                    # on an unreadable splat whenever an explicit `kind=`, a filled
                    # positional slot, or another literal splat already delivers a kind,
                    # since CPython raises `TypeError: got multiple values` on the
                    # duplicate — was put to a human on 2026-09-04 and REJECTED: it
                    # makes THIS arm reason per CALL rather than per expression,
                    # importing runtime-semantics inference into a reader that is
                    # otherwise expression-local (the positional arm below does consult
                    # the other keywords, but only to pick which arm owns the slot, not
                    # to decide a kind is unreachable), and it is indistinguishable from
                    # this contract on the real tree (zero `*`/`**` call sites into the
                    # declared callees). So
                    # `self._skip_review_and_commit(task, kind="lit", **fields)` yields
                    # BOTH `lit` and the sentinel, and that is the contract, not a
                    # defect; a future false alarm on such a shape is resolved by a
                    # deliberate decision at the call site, never by suppression here.
                    splat = _splat_kind_literal(kw.value)
                    if splat is not None:
                        findings.append(
                            ("journalkindliteral", rel, node.lineno, line_at(node.lineno), splat)
                        )
                    continue
                if kw.arg != "kind":
                    continue
                # A spelled-but-unreadable `kind=` is unresolvable, not absent, for
                # the reason `_positional_kind_literal` states: the write inside a
                # declared position spells a parameter and the literalness test is
                # waived there, so skipping it lets an undeclared kind reach the
                # journal with nothing red.
                findings.append(
                    (
                        "journalkindliteral",
                        rel,
                        node.lineno,
                        line_at(node.lineno),
                        (
                            kw.value.value
                            if isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)
                            else UNRESOLVED_DYNAMIC_KIND
                        ),
                    )
                )
            # A declared FORWARDER (`plugins/bus.py::_log`) is itself a journal
            # write, so the main emit above already read its positional kind; this
            # arm exists for the declared positions that are not forwarders, where
            # nothing else reads the slot. `index` is the one read above.
            if (
                index is not None
                and not any(kw.arg == "kind" for kw in node.keywords)
                and not _is_journal_write(node, rel)
            ):
                literal = _positional_kind_literal(node, index)
                if literal is not None:
                    findings.append(
                        ("journalkindliteral", rel, node.lineno, line_at(node.lineno), literal)
                    )
        elif (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (rel, node.name) in JOURNAL_DYNAMIC_KIND_ALLOW
        ):
            default = _kind_param_default(node)
            if default is not None:
                findings.append(
                    ("journalkindliteral", rel, node.lineno, line_at(node.lineno), default)
                )

    # Every refusal-helper DEFINITION (`_refuse_*` / `_reject_*`) and every
    # #414-family isolation-refusal CALL — the two surfaces `REFUSAL_HELPER_DEFS`
    # and `ISOLATION_CONFLICT_CALLERS` enumerate. The def side needs no alias
    # resolution (a definition IS its name); the call side resolves both guarded
    # names through `_call_aliases`, exactly like the re-arm detector above.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            ("_refuse_", "_reject_")
        ):
            findings.append(("refusaldef", rel, node.lineno, line_at(node.lineno), node.name))
        if isinstance(node, ast.Call) and _names_isolation_refusal(
            node.func, isolation_aliases, isolation_wrapper_aliases
        ):
            findings.append(
                (
                    "isolationcall",
                    rel,
                    node.lineno,
                    line_at(node.lineno),
                    enclosing_names.get(id(node)),
                )
            )

    return findings


FINDINGS = _scan()


def _of(kind: str):
    return [f for f in FINDINGS if f[0] == kind]


def test_no_tmux_invocation_outside_backend():
    """Only the tmux backend may build a ``["tmux", ...]`` argv — every other call
    site goes through the multiplexer seam."""
    offenders = [(rel, ln, txt) for _, rel, ln, txt in _of("tmux") if rel not in TMUX_BACKENDS]
    assert not offenders, (
        "tmux invoked outside the tmux backend (adapters/tmux_base.py, "
        "adapters/tmux_backend.py) — route it through get_multiplexer() instead:\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def _git_offenders(findings) -> list[tuple[str, int, str]]:
    """The chokepoint invariant as a filter: a git argv is sanctioned only in a
    ``GIT_CHOKEPOINT`` file AND only as the argv argument of a ``_run_git(...)``
    call — the file alone is not enough (see the allowlist's comment)."""
    return [
        (rel, ln, txt)
        for _, rel, ln, txt, feeds_chokepoint in findings
        if not (rel in GIT_CHOKEPOINT and feeds_chokepoint)
    ]


def test_no_git_invocation_outside_verify():
    """Only ``verify.py`` may build a ``["git", ...]`` argv, and only to hand it
    to ``_run_git`` — every other call site goes through the chokepoint's helpers
    (``git_bytes`` and siblings), which buy the engine-configured timeout, the
    ``LC_ALL=C`` pin, and the GitError taxonomy. AGENTS.md has stated this since
    the chokepoint existed; nothing enforced it, which is how both #390 bypasses
    survived."""
    offenders = _git_offenders(_of("git"))
    assert not offenders, (
        "git spawned outside the _run_git chokepoint — route it through "
        "verify.git_bytes or a sibling helper instead:\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def _ledger_read_offenders(findings) -> list[tuple[str, int, str, str]]:
    """The DW-146 contract as a filter: a deferred-work ledger read is sanctioned
    only when it NAMES its arm (in which case the detector never fires — a
    `read_for_write(...)` call has no `.read_text` attribute to match) or when its
    `(file, function)` is one of the classified inline sites."""
    return [(rel, ln, txt, fn) for _, rel, ln, txt, fn, sanctioned in findings if not sanctioned]


def test_no_bare_deferred_work_ledger_read():
    """Every deferred-work ledger read in `src/bmad_loop` names its arm
    (`deferredwork.read_for_write` / `read_for_observation`) or sits on
    `LEDGER_READ_INLINE`, the explicit list of sites that implement an arm inline
    because they carry behavior the helper cannot.

    This is what makes DW-146 "settled repo-wide" rather than "settled today".
    The contract's whole failure mode is silent drift: a new read is a single
    `read_text` line that looks locally reasonable, works on every valid ledger, and
    is wrong only for the one input nobody tests with — which is exactly how the
    pre-DW-146 tree accumulated a dozen sites each guarded, unguarded or broadly
    swallowed on its own reasoning. Comments cannot hold that line; this can, the
    same way `_run_git` holds the git chokepoint.

    Ablation: revert any converted site to a bare `read_text` — e.g.
    `deferredwork._mark_done_many`'s locked read, or `sweep._write_intent`'s — and
    this reddens naming that file, line and function (verified for both). Adding
    the reverted site's function to `LEDGER_READ_INLINE` greens it again, which is
    the intended escape hatch and why the list is annotated per entry."""
    offenders = _ledger_read_offenders(_of("ledgerread"))
    assert not offenders, (
        "deferred-work ledger read outside the DW-146 contract — route it through "
        "deferredwork.read_for_write (repair/write: the text decides published "
        "bytes) or deferredwork.read_for_observation (observation: nothing is "
        "written from it), or add it to LEDGER_READ_INLINE with the reason it "
        "must implement its arm inline:\n"
        + "\n".join(f"  {rel}:{ln} (in {fn}): {txt.strip()}" for rel, ln, txt, fn in offenders)
    )


def test_ledger_read_allowlist_has_no_stale_rows():
    """`LEDGER_READ_INLINE` is graded in both directions, like every other
    inventory here. A row whose function was renamed, deleted, or converted to a
    named arm stops describing anything — and a stale exemption is worse than a
    missing one, because it silently pre-authorizes a bare read the next time that
    name comes back.

    Ablation: convert `tui.data.deferred_entries` to `read_for_observation` without
    dropping its row, and this reddens naming the row."""
    seen = {(rel, fn) for _, rel, _, _, fn, _ in _of("ledgerread")}
    stale = LEDGER_READ_INLINE - seen
    assert (
        not stale
    ), "LEDGER_READ_INLINE rows that no longer name a ledger read — drop them:\n" + "\n".join(
        f"  {rel}: {fn}" for rel, fn in sorted(stale)
    )


# The ledger-read detector's scoping, as rows: `(label, rel, source, is_offender)`.
# The real tree is all-green by construction once the contract holds, so only
# synthetic sources can show that the detector still detects — the same reason the
# git rows below exist.
LEDGER_READ_SCOPE_CASES = [
    # The shape the contract exists to refuse, in the two receiver spellings the
    # tree actually uses.
    ("bare-local", "sweep.py", 'text = ledger.read_text(encoding="utf-8")\n', True),
    (
        "bare-attribute",
        "engine.py",
        'text = self.workspace.paths.deferred_work.read_text(encoding="utf-8")\n',
        True,
    ),
    # A named arm is invisible to the detector: there is no `.read_text` to match.
    ("named-arm", "sweep.py", 'text = deferredwork.read_for_write(ledger) or ""\n', False),
    # `path` is a ledger spelling ONLY in the owning module...
    ("owner-bare-path", "deferredwork.py", 'text = path.read_text(encoding="utf-8")\n', True),
    # ...and stays generic everywhere else, or the guard would flag every spec,
    # manifest and config read in the tree.
    ("foreign-path", "engine.py", 'text = path.read_text(encoding="utf-8")\n', False),
    # The advisory probes keep their bare read, matched on the ASSIGNED NAME *inside a
    # swallowing `try`* so the exemption cannot spread to the locked read in the same
    # function...
    (
        "owner-probe",
        "deferredwork.py",
        'try:\n    probe = path.read_text(encoding="utf-8")\nexcept Exception:\n    pass\n',
        False,
    ),
    (
        "owner-probe-ifexp",
        "deferredwork.py",
        'try:\n    probe = path.read_text(encoding="utf-8") if path.is_file() else ""\n'
        "except Exception:\n    pass\n",
        False,
    ),
    # ...and cannot be claimed by a write-bearing read that merely borrows the name:
    # what makes a probe advisory is that a fault in it decides nothing, which is what
    # the `except Exception` says. Without it, `probe` is just a variable.
    (
        "owner-probe-unguarded",
        "deferredwork.py",
        'probe = path.read_text(encoding="utf-8")\n',
        True,
    ),
    (
        "owner-probe-guarded-narrowly",
        "deferredwork.py",
        'try:\n    probe = path.read_text(encoding="utf-8")\nexcept OSError:\n    raise\n',
        True,
    ),
    # Attribute receivers carry the ledger under the same three names a local does.
    (
        "bare-self-ledger",
        "sweep.py",
        'text = self.ledger.read_text(encoding="utf-8")\n',
        True,
    ),
    (
        "bare-self-ledger-path",
        "tui/data.py",
        'def other(p):\n    return self.ledger_path.read_text(encoding="utf-8")\n',
        True,
    ),
    # ...owner-module widening included, so the two branches cannot drift apart on it.
    (
        "owner-attribute-path",
        "deferredwork.py",
        'text = self.path.read_text(encoding="utf-8")\n',
        True,
    ),
    # An allowlisted FUNCTION keeps its inline read...
    (
        "allowlisted-fn",
        "tui/data.py",
        'def deferred_entries(p):\n    return ledger.read_text(encoding="utf-8")\n',
        False,
    ),
    # ...and being in an allowlisted FILE buys a different function nothing.
    (
        "allowlisted-file-other-fn",
        "tui/data.py",
        'def something_else(p):\n    return ledger.read_text(encoding="utf-8")\n',
        True,
    ),
]


@pytest.mark.parametrize(
    ("label", "rel", "source", "is_offender"),
    LEDGER_READ_SCOPE_CASES,
    ids=[c[0] for c in LEDGER_READ_SCOPE_CASES],
)
def test_ledger_read_detector_scoping(label, rel, source, is_offender):
    """Drive known-good and known-bad sources through the REAL scan path
    (`_scan_source`), so "the tree is clean" and "the detector stopped detecting"
    stop being indistinguishable."""
    offenders = _ledger_read_offenders(
        [f for f in _scan_source(source, rel) if f[0] == "ledgerread"]
    )
    assert (
        bool(offenders) is is_offender
    ), f"{label!r} was {'not ' if is_offender else ''}flagged unexpectedly:\n{source}"


def test_proof_quiet_diff_is_owned_by_the_central_tri_state_probe():
    """Production has one proof-of-work quiet-diff body across the source tree.
    Whole-tree and literal public callers route through `_changes_since`;
    `attempt_dirty` retains the one separate quiet diff whose contract is rollback
    ownership, not proof of work.

    Ablation: restore `path_changed_since`'s inline `_git(..., "diff",
    "--quiet", ...)` body and this fails twice — the unexpected owner appears and
    the literal caller no longer calls the tri-state probe.
    """
    quiet_diff_owners: list[tuple[str, str]] = []
    tri_state_callers: Counter[tuple[str, str]] = Counter()

    class Visitor(ast.NodeVisitor):
        def __init__(self, rel: str) -> None:
            self.rel = rel
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            owner = self.functions[-1] if self.functions else "<module>"
            callee = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if callee == "_changes_since":
                tri_state_callers[(self.rel, owner)] += 1
            if (
                callee == "_git"
                and len(node.args) >= 3
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "diff"
                and isinstance(node.args[2], ast.Constant)
                and node.args[2].value == "--quiet"
            ):
                quiet_diff_owners.append((self.rel, owner))
            self.generic_visit(node)

    for source in SRC.rglob("*.py"):
        rel = source.relative_to(SRC).as_posix()
        Visitor(rel).visit(ast.parse(source.read_text(encoding="utf-8")))

    assert Counter(quiet_diff_owners) == Counter(
        {("verify.py", "_changes_since"): 1, ("verify.py", "attempt_dirty"): 1}
    )
    assert tri_state_callers[("verify.py", "has_changes_since")] == 1
    assert tri_state_callers[("verify.py", "path_changed_since")] == 1


def _verify_command_offenders(findings) -> list[tuple[str, int, str]]:
    """The review-gate chokepoint as a filter: a ``verify_commands_outcome`` call
    is sanctioned only in a ``VERIFY_COMMANDS_CHOKEPOINT`` file AND only from
    inside ``_verify_review_commands`` — the file alone is not enough, for the
    same reason the git exemption is not file-wide."""
    return [
        (rel, ln, txt)
        for _, rel, ln, txt, inside_helper in findings
        if not (rel in VERIFY_COMMANDS_CHOKEPOINT and inside_helper)
    ]


def test_verify_commands_outcome_called_only_from_the_review_chokepoint():
    """Only ``verify.py``'s ``_verify_review_commands`` may call
    ``verify_commands_outcome`` — every review gate goes through that helper.

    The helper is what pins the review legs' command cwd to ``paths.repo_root``
    (#695). Three gates previously each spelled the composition themselves against
    ``paths.project``; folding them onto one helper fixed all three at once, but
    nothing stopped a fourth gate from spelling it out again and reintroducing the
    bug in exactly the same shape — which is what this refuses.

    The bound is narrow on purpose and stated rather than implied: it does NOT
    extend to ``run_verify_commands``, whose three callers legitimately run on two
    different roots. See ``VERIFY_COMMANDS_CHOKEPOINT``."""
    offenders = _verify_command_offenders(_of("verifycmd"))
    assert not offenders, (
        "verify_commands_outcome called outside verify.py's _verify_review_commands "
        "— route the review gate through that helper so its command cwd stays "
        "repo_root (#695):\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def _verify_classify_offenders(findings) -> list[tuple[str, int, str]]:
    """The classifier half's invariant as a filter: a
    ``verify_command_results_outcome`` call is sanctioned only in a
    ``VERIFY_CLASSIFY_CHOKEPOINT`` file AND only inside that file's one listed
    enclosing function."""
    return [
        (rel, ln, txt)
        for _, rel, ln, txt, inside_helper in findings
        if not (rel in VERIFY_CLASSIFY_CHOKEPOINT and inside_helper)
    ]


def test_verify_command_results_outcome_called_only_from_its_two_compositions():
    """``verify_command_results_outcome`` is callable only from
    ``verify.verify_commands_outcome`` and ``Engine._verify_commands_with_results``.

    The sibling guard above fences the WRAPPER, which on its own leaves #695 fully
    reachable: a fourth review gate that skips `verify_commands_outcome` and writes
    ``verify_command_results_outcome(run_verify_commands(policy, paths.project),
    paths.project)`` picks its own root, twice, with that guard silent. And it is
    the shape such a gate would most likely take, since the dev side already spells
    that composition inline for its own (good) reason — it keeps the results
    between the two calls to build the hook payload.

    Two sanctioned positions rather than one because the two compositions are
    genuinely different functions in different modules; the pair is listed in
    ``VERIFY_CLASSIFY_CHOKEPOINT`` and both halves — file and enclosing function —
    are required.

    Still NOT extended to ``run_verify_commands``: the spec forbids it, and its
    three callers legitimately run on two roots."""
    offenders = _verify_classify_offenders(_of("verifyclassify"))
    assert not offenders, (
        "verify_command_results_outcome called outside its two sanctioned "
        "compositions (verify.verify_commands_outcome, "
        "Engine._verify_commands_with_results) — a review gate must reach the "
        "commands through verify._verify_review_commands so its cwd stays "
        "repo_root (#695):\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def test_spec_path_resolved_only_through_the_anchor():
    """A persisted `spec_file` is re-anchored through ``runs.task_spec_path``, never
    resolved with a bare ``Path(...)``, outside the tree-local consumers.

    ``StoryTask._serialized_worktree_path`` persists an isolated unit's spec RELATIVE
    to its mounted worktree and ``from_dict`` reads it back raw, so every reader that
    loads state from disk must say WHICH tree the value is relative to. The four
    allowlisted files run inside that tree already; everything else — the TUI, the
    resolve-context builder, the sweep and stories engines, the read-model
    projections — does not, and the main checkout carries the same
    implementation-artifacts-relative path that answers a bare ``Path(...)`` with the wrong
    copy. That is not a hypothetical: it shipped in ``tui/app.py::_paused_spec``,
    where ``_do_replan`` then WROTE to the main checkout's file and the operator's
    replan silently did not happen.

    This is the guard's whole point — the same defect was found and fixed one surface
    at a time over four review rounds, each round discovering the next unanchored
    reader, because nothing made the rule checkable.

    Ablation: revert ``_paused_spec``'s ``runs.task_spec_path(task, state)`` to
    ``Path(task.spec_file)`` and this reddens naming ``tui/app.py``."""
    offenders = [
        (rel, ln, txt) for _, rel, ln, txt in _of("specanchor") if rel not in SPEC_ANCHOR_CHOKEPOINT
    ]
    assert not offenders, (
        "a persisted spec path resolved against the reader's cwd — route it through "
        "runs.task_spec_path (or StoryTask.rebase_spec_paths_on) so the anchor names "
        "the tree the run owns:\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def test_spec_anchor_detector_flags_the_shipped_defect():
    """The guard above asserts an ABSENCE, so it passes for every reason a match could
    be missing. Feed it the exact line the defect shipped as, through the same
    ``_scan_source`` the real scan uses."""
    found = _scan_source("from pathlib import Path\npath = Path(task.spec_file)\n", "tui/app.py")
    assert [f[0] for f in found if f[0] == "specanchor"] == ["specanchor"]
    # and the dispatched twin, which carries the identical serialization hazard
    found = _scan_source(
        "from pathlib import Path\np = Path(self._task.dispatched_spec_file)\n", "tui/app.py"
    )
    assert [f[0] for f in found if f[0] == "specanchor"] == ["specanchor"]


def test_spec_anchor_detector_stays_silent_on_the_anchored_form():
    """The sanctioned spellings must not trip it, or the guard becomes noise that
    gets allowlisted away."""
    for src in (
        "p = runs.task_spec_path(task, state)\n",
        "task.rebase_spec_paths_on(wt)\n",
        "from pathlib import Path\np = Path(state.project)\n",
    ):
        assert not [f for f in _scan_source(src, "tui/app.py") if f[0] == "specanchor"]


def _task_artifact_offenders(findings) -> list[tuple[str, int, str, str]]:
    """The artifact-name literals no declared POSITION covers — the assertion's
    whole policy, factored out so it can be graded on synthetic findings rather than
    only on today's tree (the file's ``_env_read_offenders`` idiom).

    Both halves of the key bite: the file, then the enclosing function inside it.
    Dropping the function half exempts every ``"result.json"`` in
    ``adapters/generic.py``, which is what the allowlist's comment already said was
    not the case."""
    return [
        (rel, ln, txt, name)
        for _, rel, ln, txt, (name, fn) in findings
        if name not in TASK_ARTIFACT_LITERAL_ALLOW.get(rel, {}).get(fn, frozenset())
    ]


def test_task_cycle_artifacts_named_only_through_the_constant():
    """The task-directory artifact names live in ``journal.TASK_CYCLE_ARTIFACTS``,
    not as a literal in each site that touches them.

    Three sites share the list: both adapters clear it in ``start_session`` (a
    caller-supplied task_id may be reused, so a silent session must not inherit its
    predecessor's outputs) and ``resolve._gather_escalations`` reads it back. They
    were three independent literals, and the only parity claim was a sentence in a
    test docstring — so a third artifact added to the reader would silently miss
    both adapters, which is exactly how ``escalation.json`` reached the reader
    before either adapter cleared it.

    The exemption is per-POSITION and per-NAME, never per-file:
    ``adapters/generic.py::_result_path`` answers a genuinely single-artifact
    question and keeps ``"result.json"``, while ``"escalation.json"`` stays refused
    inside it and BOTH names stay refused in every other function of that file.

    ⚠️ What this assertion is worth on today's tree, said as candidly as its DW-66
    sibling says it: almost nothing. There is exactly ONE `taskartifact` finding in
    the whole tree and it is allowlisted, so the offender list is empty and would
    stay empty with the detector deleted. ``TASK_ARTIFACT_PROBES`` and
    ``TASK_ARTIFACT_SCOPE_CASES`` are what grade the detector and the scoping; this
    row grades the tree, and the tree is currently clean.

    ⚠️ And what it protects is narrower than "the constant is the list". It refuses
    the constant being UN-DONE — a name pulled back out into a literal at any of the
    three sites. It does NOT catch the constant being OUT-GROWN: a genuinely new
    artifact spelled only in the reader produces no finding at all, because the
    detector matches the names the constant already holds. Verified — a
    ``(task_dir / "verdict.json")`` added to ``resolve.py`` is silent here, and the
    parity it would break is the parity this guard exists for.

    Ablation: respell either adapter's loop as
    ``(task_dir / "escalation.json").unlink(missing_ok=True)`` and this reddens
    naming that file and line."""
    offenders = _task_artifact_offenders(_of("taskartifact"))
    assert offenders == [], (
        "a tasks/<task_id>/ artifact named as a bare literal — iterate "
        "journal.TASK_CYCLE_ARTIFACTS so the readers and both adapters cannot "
        "drift apart on the list:\n"
        + "\n".join(f"  {rel}:{ln}: {name!r} — {txt.strip()}" for rel, ln, txt, name in offenders)
    )


def test_task_cycle_artifact_docs_track_the_canonical_tuple():
    """The run inventory and extension boundary keep pace with the shared list."""
    project_root = Path(__file__).resolve().parents[1]
    features = (project_root / "docs/FEATURES.md").read_text(encoding="utf-8")
    inventory = features.split("- All run state in", 1)[1].split("\n- ", 1)[0]
    guide = (project_root / "docs/adapter-authoring-guide.md").read_text(encoding="utf-8")
    start_session_contract = guide.split("- `start_session", 1)[1].split(
        "- `wait_for_completion", 1
    )[0]

    def shared_artifacts(contract: str) -> set[str]:
        listing = contract.split("shared artifacts: [", 1)[1].split("]", 1)[0]
        return set(listing.split("`")[1::2])

    canonical = set(TASK_CYCLE_ARTIFACTS)
    assert shared_artifacts(inventory) == canonical
    assert shared_artifacts(start_session_contract) == canonical

    assert "`journal.TASK_CYCLE_ARTIFACTS`" in start_session_contract
    assert (
        "after creating the task directory and before\n  launching the session"
        in start_session_contract
    )
    assert "a missing artifact is a normal no-op" in start_session_contract
    assert "Adapter-private breadcrumbs" in start_session_contract


def _session_task_id_offenders(findings) -> list[tuple[str, int, str]]:
    """The chokepoint invariant as a filter: a composed task id is sanctioned only
    in a ``SESSION_TASK_ID_CHOKEPOINT`` file AND only inside that file's one listed
    enclosing function — the file alone is not enough, for the reason the git and
    verify-classifier exemptions are not file-wide."""
    return [(rel, ln, txt) for _, rel, ln, txt, at_chokepoint in findings if not at_chokepoint]


def test_session_task_id_composed_only_at_the_chokepoint():
    """Every session task id is composed in ``engine._session_task_id`` and nowhere
    else.

    The four mint sites (``engine.py`` ×3, ``resolve.py``) all call it and bind or
    pass the result; none spells the format. That is what makes
    ``_resumable_session``'s resume match byte-identical to what ``_run_session``
    stored, and what carries the ``-g<N>`` re-arm generation discriminator a
    hand-rolled fifth mint would omit — silently re-opening #705, correctly
    everywhere it was exercised and wrong only on a re-armed run.

    Nothing forbade a fifth. This does: a composition or a bare literal in a
    ``task_id`` binding, or returned from a function named for the id it makes, is
    refused wherever it is spelled. A FORWARD is not a mint and stays silent — see
    ``SESSION_TASK_ID_PROBES`` / ``SESSION_TASK_ID_NON_PROBES`` for that boundary as
    rows rather than prose.

    ⚠️ What this assertion grades, precisely — the halves are NOT the same, and
    both ablations were run rather than reasoned about:

    * the SANCTION, yes. The chokepoint's own ``return safe_segment(f"…")`` is a
      real finding on today's tree, cleared only by its position, so emptying
      ``SESSION_TASK_ID_CHOKEPOINT`` reddens this naming ``engine.py:393``. That is
      more than the sibling guards' repo-wide assertions can say for themselves.
    * the DETECTOR, no. Delete the ``taskid`` emit and this goes green with an empty
      finding list — indistinguishable from an invariant that holds.
      ``SESSION_TASK_ID_PROBES`` is where that is caught, and the two are not
      interchangeable.

    Ablation: respell ``resolve.py``'s mint as
    ``task_id=f"{story_key}-resolve-1"`` and this reddens naming that line."""
    offenders = _session_task_id_offenders(_of("taskid"))
    assert offenders == [], (
        "a session task id composed outside engine._session_task_id — call that "
        "function instead, so the id keeps its whole-composition sanitize and its "
        "-g<N> re-arm generation suffix (#705):\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def test_rearm_escalation_called_only_behind_a_liveness_gate():
    """``runs.rearm_escalation`` is reached from exactly two places, and each consults
    liveness before it.

    ``runs._rearm_commit_landed`` is protected by the shared run-state transaction
    lock, so this enumeration no longer supplies its writer-identity premise. It pins
    the separate safety rule that an operator surface refuses a provably-live engine
    before entering that serialized mutation turn.

    Note what the gate does and does not establish. It proves the engine is not
    PROVABLY alive, not that it is dead: ``"alive"`` is refused outright, while
    ``"unknown"`` proceeds under ``--force`` in ``cmd_resolve`` and counts as blocking
    in the TUI only for a pid-backed run. So this grades the falsifiable half — that
    an earlier liveness decision BLOCKS fall-through before the call.

    ``cli.cmd_resume`` is deliberately absent because it never re-arms. Its state
    publication is covered separately by the writer/transaction inventory below;
    listing it here would make this call-site enumeration unfalsifiable.

    ⚠️ What this assertion grades, precisely — the two halves differ, and the
    difference is the reason the probe rows below exist:

    * the ENUMERATION, yes, in both directions and with multiplicity. The count is
      non-empty on today's tree, so deleting the ``rearmcall`` emit reddens it — unlike the
      sibling repo-wide "nothing is flagged" guards, which go green when their detector
      dies. Adding a third call, even inside an existing caller, reddens it too.
    * the GATE, no. Both sites are gated today, so ``ungated == []`` would survive a
      ``_consults_liveness_before`` that always answered ``True`` — including one that
      had lost its line-position check, which is the half a late gate would exploit.
      ``REARM_CALL_PROBES`` is where that is caught, and the two are not
      interchangeable.

    Ablations to run against this row: drop the ``if
    self._resolve_blocked_by_liveness(...)`` block from ``tui.TuiApp._do_rearm`` and the
    gate half must redden naming ``tui/app.py``; add a call in a third function and the
    count comparison must redden."""
    findings = _of("rearmcall")
    sites = _rearm_callsite_counts(findings)
    declared = Counter(REARM_ESCALATION_CALLERS)
    assert sites == declared, (
        "the count of runs.rearm_escalation call sites moved. A new operator surface "
        "must retain the liveness refusal as well as the shared run-state transaction; "
        "do not widen this constant without reviewing both:\n"
        f"  scanned:  {sorted(sites.elements())}\n"
        f"  declared: {sorted(declared.elements())}"
    )
    ungated = [(rel, ln, txt) for _, rel, ln, txt, (_, gated) in findings if not gated]
    assert ungated == [], (
        "runs.rearm_escalation called without a preceding liveness refusal — the re-arm "
        "mutates persisted state for a run it must know is not being driven:\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in ungated)
    )


def _production_call_sites(name: str) -> set[tuple[str, str | None]]:
    sites: set[tuple[str, str | None]] = set()
    for source in SRC.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        enclosing = _enclosing_function_names(tree)
        rel = source.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node.func) == name:
                sites.add((rel, enclosing.get(id(node))))
    return sites


def test_run_state_writer_and_transaction_inventory_is_complete():
    """Every publisher uses save_state, and every known RMW gesture holds state_lock.

    Ablations: add ``save_state(run_dir, state)`` to a new production function, or
    delete the outer ``state_lock`` from ``runs.restamp_code_root``; the respective
    exact-set comparison reddens and names the changed site.
    """
    assert _production_call_sites("save_state") == SAVE_STATE_CALLERS
    assert _production_call_sites("state_lock") == RUN_STATE_TRANSACTIONS


def test_refusal_helper_inventory_is_complete():
    """Every `_refuse_*`/`_reject_*` helper definition in the tree has a declared
    row, in both directions. Review iteration 6 (and four bot rounds after it) kept
    finding one shape by hand — a refusal landed with no test row, caught only by a
    later review pass — and the journal-FIELD inventory beside this one is the gate
    that caught `reaches_redrive`; this is the same gate for the refusal surface. A
    row here is the PR-time decision, not the test itself: the helper's refusal
    behavior still needs its own test landed with the row.

    Anti-vacuity is structural, the exact-inventory property the rearm Counter
    relies on: the declared side is non-empty, so a scan that stops finding
    definitions reddens the staleness direction instead of passing green.

    Graded as a Counter WITH multiplicity, not a set of names —
    `ISOLATION_CONFLICT_CALLERS`' rationale, applied to definitions: a SECOND def
    of a declared name in the same file (a platform-conditional twin, say) is a new
    refusal body the emit reports twice, and a set comparison absorbed it silently
    (measured). Every declared row's count is 1 today, which `Counter` over the set
    encodes.

    Ablation: delete the `refusaldef` emit and this reddens with all nine declared
    rows stale; add `def _refuse_nothing()` to `runs.py` and this reddens naming
    it; add a platform-conditional TWIN def of `_refuse_live_session` to `runs.py`
    and the count comparison reddens with the set of names unchanged."""
    findings = _of("refusaldef")
    scanned = Counter((rel, name) for _, rel, _, _, name in findings)
    declared = Counter(REFUSAL_HELPER_DEFS)
    changed = {key for key in set(scanned) | set(declared) if scanned[key] != declared[key]}
    detail = [
        f"  {rel}:{ln}: {name} — {txt.strip()}"
        for _, rel, ln, txt, name in findings
        if (rel, name) in changed
    ]
    assert scanned == declared, (
        "the `_refuse_*`/`_reject_*` helper definitions moved. A NEW helper — a "
        "second same-named def in one file included — lands WITH its (file, name) "
        "row and the test asserting what it refuses in the same PR; a helper no "
        "module defines any more loses its row, which otherwise stands as a "
        "pre-approval for the next helper that reuses the name:\n"
        f"  scanned:  {sorted(scanned.elements())}\n"
        f"  declared: {sorted(declared.elements())}\n" + "\n".join(detail)
    )


def test_isolation_conflict_refusal_sites_are_enumerated():
    """The #414 refusal is reached from exactly the declared call sites, counted
    WITH multiplicity — both the `bmadconfig.worktree_isolation_conflict` predicate
    and its CLI wrapper `_reject_isolation_conflict`, so a new surface reaching the
    pair through either spelling reddens this row. That is the `96aa09a9` shape:
    `cmd_resolve`'s pre-session refusal landed as a wrapper call with no test row,
    and nothing structural named the omission until a review pass did.

    Multiplicity is load-bearing on today's tree — `cmd_resolve` legitimately calls
    the wrapper twice, so a set of keys would absorb a third call there silently
    (`test_isolation_callsite_count_does_not_hide_a_second_call_in_one_function`
    pins the counting itself).

    Ablation: delete the `isolationcall` emit and this reddens (eleven declared,
    zero scanned); add a third `_reject_isolation_conflict` call inside
    `cmd_resolve` and the count comparison reddens naming the site."""
    findings = _of("isolationcall")
    sites = _isolation_callsite_counts(findings)
    declared = Counter(ISOLATION_CONFLICT_CALLERS)
    changed = {key for key in set(sites) | set(declared) if sites[key] != declared[key]}
    detail = [
        f"  {rel}:{ln}: in {fn or '<module>'} — {txt.strip()}"
        for _, rel, ln, txt, fn in findings
        if (rel, fn) in changed
    ]
    assert sites == declared, (
        "the #414-family refusal call sites moved (worktree_isolation_conflict / "
        "_reject_isolation_conflict). A new surface refusing the pair lands WITH "
        "its own refusal test in the same PR; a removed one deletes its row — "
        "update ISOLATION_CONFLICT_CALLERS only alongside that decision:\n"
        f"  scanned:  {sorted(sites.elements())}\n"
        f"  declared: {sorted(declared.elements())}\n" + "\n".join(detail)
    )


def _journal_measured_splats(findings) -> Counter[tuple[str, str | None]]:
    """``(file, enclosing function) -> how many UNRESOLVABLE splat findings sit
    there``, over a ``journalfield`` population.

    ⚠️ THE UNIT IS ONE UNRESOLVED ``**`` KEYWORD ARGUMENT — never one journal write
    call, and never one call site. The scan emits one ``field is None`` finding per
    ``**`` keyword whose keys the resolver could not read, so
    ``journal.append(kind, **a, **b)`` counts 2 on its own and a position holding two
    one-splat calls counts 2 as well. The finer unit is deliberate: DW-150 exists to
    redden a second splat dropped inside an already-declared position, and that splat
    arrives either as a new CALL or as a second ``**`` on an existing one — a
    call-shaped unit sees only the first.

    One definition of "this finding is a hole", shared by the two graders that must
    agree about it: ``_journal_field_offenders`` (which flags the lines of an
    over-count) and ``_journal_splat_count_drift`` (which reports the numbers in every
    direction). Built twice, a change to what MARKS a hole — a sentinel in place of
    ``field is None``, a reshaped payload — would land in one and not the other, and
    the filter would flag while the grader stayed silent or the reverse."""
    return Counter((rel, fn) for _, rel, _, _, (field, fn, _) in findings if field is None)


def _journal_field_offenders(findings) -> list[tuple[str, int, str, str]]:
    """The routing invariant as a filter, in the two directions a finding can fail:
    a field name that neither ``diagnostics`` nor the benign inventory accounts for,
    and a ``**splat`` whose keys could not be resolved at a position that has not
    declared itself a hole.

    Routing is checked BY NAME first and then BY KIND, mirroring ``_scrub_entry``'s
    own order rather than a flattened union of the two. A kind-scoped name is routed
    only on its declared shapes and is an offender everywhere else unless that other
    shape is explicitly benign. That includes a call whose kind the scan could not
    resolve. `target` is the dangerous example: flattening it made
    ``journal.append("unit-merge-failed", target=branch)`` read as routed.

    The splat arm grades the declared COUNT, not mere membership. A position declares
    how many UNRESOLVED ``**`` KEYWORD ARGUMENTS it holds — the unit
    ``_journal_measured_splats`` defines, which is neither a write call nor a call
    site — so a SECOND splat dropped inside an already-declared position is an
    offender ONCE PER UNRESOLVED ``**`` ARGUMENT, whether it arrived as a new call or
    as a second ``**`` on an existing one. That is per LINE only while each call
    carries one splat: ``append(kind, **a, **b)`` yields two offenders on a single
    line. Membership alone waived it on arrival, and its
    field names escaped the inventory with this guard green. Only over-count is
    reported here: a position measuring FEWER such arguments than it declares has no
    finding to hang a message on, which is why
    ``test_journal_field_guard_actually_saw_the_producers`` grades the same numbers in
    both staleness directions. On the UNDECLARED direction the two overlap on purpose,
    and they answer different questions — LINES to fix here, NUMBERS to move there —
    which is the deliberate departure from ``_journal_kind_count_drift``'s
    declared-only restriction."""
    offenders: list[tuple[str, int, str, str]] = []
    measured_splats = _journal_measured_splats(findings)
    for _, rel, ln, txt, (field, fn, kind) in findings:
        where = f"{fn}()" if fn else "<module>"
        if field is None:
            declared = JOURNAL_SPLAT_ALLOW.get((rel, fn))
            if declared is None:
                offenders.append((rel, ln, txt, f"unresolvable **splat in {where}"))
            elif measured_splats[(rel, fn)] > declared:
                offenders.append(
                    (
                        rel,
                        ln,
                        txt,
                        f"unresolvable **splat in {where} beyond its declared hole "
                        f"(measured {measured_splats[(rel, fn)]}, declared {declared})",
                    )
                )
            continue
        if field in JOURNAL_ROUTED_FIELDS or field in JOURNAL_BENIGN_FIELDS:
            continue
        if kind is not None and (
            field in JOURNAL_KIND_ROUTED_FIELDS.get(kind, frozenset())
            or field in JOURNAL_KIND_BENIGN_FIELDS.get(kind, frozenset())
        ):
            continue
        on = f"on {kind!r}" if kind is not None else "on a non-literal kind"
        offenders.append((rel, ln, txt, f"{field!r} {on} in {where}"))
    return offenders


def _journal_kind_offenders(findings) -> list[tuple[str, int, str]]:
    """Journal writes whose KIND is not a string literal, at a position that has not
    declared itself one. Their fields cannot be graded against kind-scoped routing at
    all, so — like an unresolvable splat — they fail loud rather than pass by
    default."""
    return [
        (rel, ln, txt)
        for _, rel, ln, txt, fn in findings
        if (rel, fn) not in JOURNAL_DYNAMIC_KIND_ALLOW
    ]


def _journal_kind_count_drift(findings) -> dict[tuple[str, str], tuple[int, int]]:
    """The same positions on the other axis: each DECLARED position mapped to
    ``(declared, measured)`` wherever the two disagree, in both directions.

    Undeclared positions are deliberately absent — that is
    ``_journal_kind_offenders``' question, and answering it twice would report one
    defect through two messages."""
    measured = Counter((rel, fn) for _, rel, _, _, fn in findings)
    return {
        pos: (declared, measured[pos])
        for pos, declared in JOURNAL_DYNAMIC_KIND_ALLOW.items()
        if measured[pos] != declared
    }


def _journal_minted_kind_drift(
    findings,
) -> dict[tuple[str, str | None], tuple[frozenset[str], frozenset[str]]]:
    """Each position in the UNION of `JOURNAL_DYNAMIC_KIND_SPELLINGS` and the measured
    `journalkindminted` findings, mapped to ``(declared, measured)`` wherever the two
    SETS disagree.

    The union, not the declared keys, is what makes this grade in both directions at
    once: a new or renamed spelling shows up as measured-not-declared, a vanished one
    as declared-not-measured, an undeclared minting position as an empty declared
    half, and a stale row as an empty measured half. A rename moves both halves in one
    entry, so it cannot be reported as an addition now and a staleness a run later —
    the same shape `_journal_kind_inventory_drift` settled on for the literal
    inventory.

    Unlike `_journal_kind_count_drift` this does NOT restrict itself to declared
    positions: minting is not waived per position anywhere, so an f-string kind
    appearing at a new position is this helper's business and there is no sibling
    assertion to hand it to."""
    measured: dict[tuple[str, str | None], set[str]] = {}
    for _, rel, _, _, (fn, spelling) in findings:
        measured.setdefault((rel, fn), set()).add(spelling)
    drift: dict[tuple[str, str | None], tuple[frozenset[str], frozenset[str]]] = {}
    for position in set(JOURNAL_DYNAMIC_KIND_SPELLINGS) | set(measured):
        declared = JOURNAL_DYNAMIC_KIND_SPELLINGS.get(position, frozenset())
        found = frozenset(measured.get(position, ()))
        if declared != found:
            drift[position] = (declared, found)
    return drift


def _journal_splat_count_drift(findings) -> dict[tuple[str, str | None], tuple[int, int]]:
    """Each position in the UNION of ``JOURNAL_SPLAT_ALLOW`` and the measured
    unresolvable-splat findings, mapped to ``(declared, measured)`` wherever the two
    disagree.

    Both halves are counts of UNRESOLVED ``**`` KEYWORD ARGUMENTS, the unit
    ``_journal_measured_splats`` defines — not write calls and not call sites, so a
    single ``append(kind, **a, **b)`` moves the measured half by 2.

    The union, not the declared keys, is what makes this grade in every direction at
    once: an undeclared position arrives with a declared half of 0, a stale row with a
    measured half of 0, and an argument added or removed inside an already-declared
    position with two non-zero halves. That is broader than
    ``_journal_kind_count_drift``, which restricts itself to declared positions to
    avoid answering ``_journal_kind_offenders``' question twice — and the reason the
    splat axis differs is that its sibling filter (``_journal_field_offenders``) can
    only report over-count. A position that lost an unresolved argument, or lost every
    one, has no finding left for the filter to flag, so if this helper deferred to it
    the way the kind axis does, a stale hole would sit there sanctioning nothing with
    the suite green. Overlap on the undeclared direction is the accepted price, taken
    with eyes open rather than as the only one in this file: the filter names the LINE
    to fix, this names the NUMBER to move."""
    measured = _journal_measured_splats(findings)
    drift: dict[tuple[str, str | None], tuple[int, int]] = {}
    for position in set(JOURNAL_SPLAT_ALLOW) | set(measured):
        declared = JOURNAL_SPLAT_ALLOW.get(position, 0)
        found = measured[position]
        if declared != found:
            drift[position] = (declared, found)
    return drift


def _journal_bare_name_collisions(findings) -> dict[tuple[str, str], list[int]]:
    """Every ``(file, bare function name)`` a journal write's position key can name,
    where TWO distinct function definitions in that file answer to the name — mapped
    to both ``def`` linenos.

    ⚠️ WHAT THIS ESTABLISHES, exactly: that no module holds two journal-WRITING
    functions of one bare name. Nothing broader. Five tables are keyed
    ``(file, bare function name)`` — ``JOURNAL_SPLAT_ALLOW`` /
    ``JOURNAL_SPLAT_FIELDS``, ``JOURNAL_DYNAMIC_KIND_ALLOW`` /
    ``JOURNAL_DYNAMIC_KIND_SPELLINGS`` and ``JOURNAL_FORWARDERS`` — and the writer-pair
    property is what the four POSITION tables need: a pair of writers is what the bare
    name silently aggregates into one row, summing their counts, merging their declared
    sets, and letting a splat or a dynamic kind move between them without reddening
    anything. A definition that never writes contributes no finding to aggregate, so it
    is correctly absent here.

    ⚠️ THE LIMIT, on ``JOURNAL_FORWARDERS``. That table does not merely aggregate: it
    makes ``_is_journal_write`` treat a call to that NAME in that FILE as a journal
    write. So a same-named twin that never touches the journal still hands its callers'
    keywords to the field inventory — and being a non-writer, it is invisible to this
    helper, which reads journal-write findings. The forwarder table's full name safety
    is therefore NOT established here; widening the classifier to close it is out of
    scope (DW-152) and would change what every position table measures.

    Qualifying the key is deferred (DW-152); this makes the assumption behind the
    deferral enforceable instead of assumed, on the writer-pair half.

    Module-level writes (``fn is None``) are never a collision — a file has exactly one
    module scope, so it cannot collide with itself."""
    seen: dict[tuple[str, str], set[int]] = {}
    for _, rel, _, _, (fn, def_lineno) in findings:
        if fn is None or def_lineno is None:
            continue
        seen.setdefault((rel, fn), set()).add(def_lineno)
    return {position: sorted(linenos) for position, linenos in seen.items() if len(linenos) > 1}


def test_journal_fields_are_routed_or_declared_benign():
    """Every field name a journal producer SPELLS AT A CALL is either routed by
    ``diagnostics`` — by name, or by name-and-kind — or listed in the benign
    inventory.

    Two bounds on "every field name a journal producer writes", which is what this
    docstring used to claim, and neither is a detail. ``Journal.append`` mints
    ``log_task`` and ``log_pos`` itself with ``setdefault``, so no call spells them
    and this scan cannot see them (``JOURNAL_SELF_MINTED_FIELDS``;
    ``test_journal_append_writes_only_accounted_fields`` is the row that actually
    covers them). And a field arriving through a declared ``JOURNAL_SPLAT_ALLOW``
    hole is inventoried there by hand, not observed here.

    Routing is NOT flat, which the earlier wording implied by folding
    ``_JOURNAL_KIND_ALIAS_FIELDS`` into one by-name union. ``target`` is aliased on
    three merge kinds and deliberately left alone on the ``board-advance-*`` family,
    where it carries a sprint status — so it is checked per kind, and
    ``journal.append("unit-merge-failed", target=branch)``, a new kind reusing the
    name, is refused here rather than sailing through to ``scrub_json``.

    Nothing coupled the producers to the tables, and the tables route by field NAME.
    A measured ablation — renaming ``recovery_flow.py``'s ``patch=`` to
    ``patch_path=`` — left every row of ``tests/test_diagnostics.py`` green while
    the field dropped out of ``_JOURNAL_DROP_FIELDS`` and started shipping in
    ``--dump`` output. That is the failure this refuses, and it is a rename rather
    than an exotic shape.

    Direction matters, and the reverse would not work. Several routing rows are
    deliberately defensive (``paused_story_key``, ``bundle``, ``detail``,
    ``suggestion``, ``blocker``, ``stdout_path``) and have no static kwarg producer,
    so a "no dead row" assertion would need a large allowlist of CORRECT entries
    while catching nothing this direction misses. A rename shows up here as a NEW
    unrouted name — which is precisely the measured ablation.

    What the benign inventory claims is narrow and stated plainly on
    ``JOURNAL_BENIGN_FIELDS``: it is the set of unrouted names that existed when the
    guard landed, not a per-name safety audit. The guard's real assertion is that
    the NEXT name cannot appear without someone deciding which side it belongs on.

    A ``**splat`` is resolved through the literal stores that build it; when it
    cannot be, the site fails loud unless ``JOURNAL_SPLAT_ALLOW`` declares it a
    known hole with a reason. A silently-skipped splat would be a standing hole in
    the inventory — the guard would keep passing while new fields arrived through
    it.

    Ablation: rename ``recovery_flow.py``'s ``patch=`` to ``patch_path=`` and this
    reddens naming the new field."""
    offenders = _journal_field_offenders(_of("journalfield"))
    assert offenders == [], (
        "a journal field is neither routed by diagnostics' redaction tables nor "
        "declared benign, or an unresolvable **splat exceeds the hole its position "
        "declares — decide which it is: add a row to the right table in "
        "diagnostics.py if the name carries an identifier, a path or free text, or "
        "list it in JOURNAL_BENIGN_FIELDS if it does not; for a splat line naming "
        "measured vs declared, move the count in JOURNAL_SPLAT_ALLOW and the names "
        "it lets through in JOURNAL_SPLAT_FIELDS, or make its keys resolvable:\n"
        + "\n".join(f"  {rel}:{ln}: {what} — {txt.strip()}" for rel, ln, txt, what in offenders)
    )


def test_journal_kinds_are_literal_or_the_position_is_declared():
    """A journal write whose KIND is not a string literal cannot be graded against
    ``diagnostics``' kind-scoped routing, so it fails loud at an undeclared position
    — the same stance the guard takes on an unresolvable ``**splat``, and for the
    same reason: a site the scan cannot read must not read as clean.

    Such writes sit at four positions — one position can hold several, as the
    ``family`` f-strings in ``recovery_flow.prune_preserve_refs`` do — and all four
    journal only by-name routed fields today (``JOURNAL_DYNAMIC_KIND_ALLOW`` records
    which). Declaring one waives the kind resolution and nothing else: a kind-scoped
    name at one of them is still refused by the sibling assertion, because nothing can
    prove which kind it lands on.

    Ablation: empty ``JOURNAL_DYNAMIC_KIND_ALLOW`` and this reddens naming all four
    positions."""
    offenders = _journal_kind_offenders(_of("journalkind"))
    assert offenders == [], (
        "a journal write whose kind is not a string literal, at a position that has "
        "not declared itself one — pass a literal kind, or add the position to "
        "JOURNAL_DYNAMIC_KIND_ALLOW with what it journals:\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def test_journal_dynamic_kind_positions_write_what_they_declare():
    """Each ``JOURNAL_DYNAMIC_KIND_ALLOW`` position holds exactly the number of
    `journalkind` findings it declares — writes without a positional string-literal
    kind. The count lives in the declaration, where an edit has to move it, rather
    than in prose no assertion reads.

    Prose could not hold this. The waiver is granted per POSITION, so one more
    f-string write dropped inside ``recovery_flow.prune_preserve_refs`` is waived on
    arrival: the declaredness sibling above still passes (the position is declared),
    the routing rows still pass (the fields are by-name routed), and the only thing
    that was ever wrong is a comment. This row is what turns that into a red test,
    on the diff that adds the write.

    Bound: it grades DECLARED positions only, in both directions — an undeclared
    position is the sibling's business and stays its message, and a declared row
    whose writes all gained positional literal kinds reddens here at measured 0
    rather than lingering as a waiver for nothing.

    Ablation: add one ``self.journal.append(f"{family}-ablation", ...)`` inside
    ``recovery_flow.prune_preserve_refs`` beyond what it declares and this reddens at
    that position, measured one above declared, while the declaredness sibling stays
    green."""
    wrong = _journal_kind_count_drift(_of("journalkind"))
    assert wrong == {}, (
        "a declared dynamic-kind position no longer writes what it declares — the "
        "count is part of the declaration, so move it in the SAME PR as the write "
        "(a measured 0 means the row is stale: delete it):\n"
        + "\n".join(
            f"  {rel}::{fn}: declared {declared}, measured {found}"
            for (rel, fn), (declared, found) in sorted(wrong.items())
        )
    )


def test_journal_dynamic_kind_positions_mint_what_they_declare():
    """The kind SPELLINGS an f-string dynamic-kind position mints are exactly the ones
    `JOURNAL_DYNAMIC_KIND_SPELLINGS` declares, in both directions — the identity axis
    the count sibling above cannot hold.

    The count is blind to identity by construction. Respelling `f"{family}-pruned"` to
    `f"{family}-purged"`, or respelling the `"attempt-preserve-dirty"` literal the loop
    tuple carries, leaves the position at four writes: the count row stays green, the
    literalness row stays green (the position is declared), and `JOURNAL_KINDS`' stated
    bound keeps these kinds out of the literal inventory on purpose — so before this
    row the only thing that named them was a comment, and a comment cannot fail.

    The spellings are DERIVED, not restated: `_fstring_kind_spellings` expands the
    JoinedStr in the kind slot and resolves each interpolation through the same-function
    `for` bindings that supply it, so `src/` is the measurement and this table is the
    only place a spelling is written by hand. An edit to either side has to move the
    other.

    Fails loud rather than skipping. An interpolation the resolver cannot reduce —
    a call, a parameter, a name assigned from anything but a literal loop — mints a
    spelling carrying `UNRESOLVED_DYNAMIC_KIND`, which no row can declare, so an
    unreadable kind reddens here instead of quietly leaving the position under-measured.

    Ablation: respell `f"{family}-pruned"` to `f"{family}-purged"` in
    `recovery_flow.prune_preserve_refs` and this reddens naming the position on both
    directions at once (two spellings undeclared, two measured-absent) while
    `test_journal_dynamic_kind_positions_write_what_they_declare` and
    `test_journal_kinds_are_literal_or_the_position_is_declared` stay green. Emptying
    `JOURNAL_DYNAMIC_KIND_SPELLINGS` reddens it too, at declared-empty."""
    wrong = _journal_minted_kind_drift(_of("journalkindminted"))
    assert wrong == {}, (
        "a dynamic-kind position no longer mints what it declares — the spellings are "
        "read off the AST, so move JOURNAL_DYNAMIC_KIND_SPELLINGS in the SAME PR as "
        "the kind (an empty measured half means the row is stale: delete it; an empty "
        "declared half means the position is new: add it):\n"
        + "\n".join(
            f"  {rel}::{fn}: minted-but-undeclared {sorted(found - declared)}, "
            f"declared-but-unminted {sorted(declared - found)}"
            for (rel, fn), (declared, found) in sorted(
                wrong.items(), key=lambda item: (item[0][0], item[0][1] or "")
            )
        )
    )


def test_journal_kind_inventory_is_complete():
    """Every literal journal kind a producer writes is a declared `JOURNAL_KINDS`
    row, in both directions — the enumerate-vs-declare gate for the kind axis. Before
    this test the literal kinds had no inventory at all: a new record kind could land,
    with or without a test row, and only a later review pass would ask what covers it.
    Now the question is asked by CI, at PR time, on the diff that introduces the
    kind.

    Both directions matter. A NEW kind fails the undeclared arm naming its file,
    line and kind; a RENAME fails both arms at once — the new spelling undeclared,
    the old row stale — so the old row cannot survive as a pre-approval for the
    next kind that reuses it. The staleness arm's bound is stated on
    `JOURNAL_KINDS`: it sees the union of producers, so one writer of a SHARED
    kind can drop it without reddening anything while another still writes it.

    A declared dynamic-kind position writes a parameter, so its kinds are read
    where a literal reaches it — a caller's `kind="..."` keyword, or the parameter
    default — by the emit's second arm (`JOURNAL_KINDS`' header); only the f-string
    family is absent, by `JOURNAL_KINDS`' stated bound, and the sibling literalness
    test above governs whether a POSITION may be dynamic at all. Consumer-side kind
    parity — readers matching kinds by literal — stays DW-82's, out of scope here.

    Anti-vacuity is structural: the declared set is non-empty, so deleting the
    `journalkindliteral` emit reddens the staleness arm with the entire inventory
    rather than passing green.

    Both arms are graded from ONE scan in ONE assertion
    (`_journal_kind_inventory_drift`): as two sequential asserts a rename reported
    only the undeclared spelling, and the stale row surfaced a run later, after the
    new row had landed (review pass 2). The failure TEXT is split back out per arm by
    `_journal_kind_inventory_message`, which also carries the sentinel branch — one
    assertion, three separately-worded remedies.

    Ablation: delete the `journalkindliteral` emit and this reddens with EVERY
    declared row stale; duplicate engine.py's epic-boundary write under the kind
    `"guard-ablation-probe"` and ONLY this test reddens, naming the kind and site;
    add `self._skip_review_and_commit(task, kind="guard-ablation-probe")` to
    engine.py and ONLY this test reddens, naming the call; rename engine.py's
    `epic-boundary` write and the ONE failure names both the new spelling and the
    stale row."""
    undeclared, stale = _journal_kind_inventory_drift(_of("journalkindliteral"))
    assert (undeclared, stale) == ([], set()), _journal_kind_inventory_message(undeclared, stale)


def test_reader_minted_kind_is_deliberately_absent_from_the_inventory():
    """`journal.UNREADABLE_LINE_KIND` must NOT be a `JOURNAL_KINDS` row.

    This pins a hole a future reader will want to "fix". Every other kind in the
    codebase is declared above, so an undeclared one looks like an oversight — but
    this inventory is PRODUCER-side: `_journal_kind_inventory_drift` scans the literal
    kinds passed to `journal.append`, and no producer ever writes this one. Both
    readers (`Journal.entries`, `tui.data.JournalTail.read_new`) MINT it in place of a
    line they could not decode. Adding the row would therefore make it a row nothing
    writes, which is exactly what `test_journal_kind_inventory_is_complete`'s staleness
    arm reddens on — the row would break CI, not complete it.

    Ablation: add the kind to `JOURNAL_KINDS` and BOTH this test and
    `test_journal_kind_inventory_is_complete` (staleness arm) redden — verified."""
    assert UNREADABLE_LINE_KIND not in JOURNAL_KINDS, (
        f"{UNREADABLE_LINE_KIND!r} is reader-minted, never written by a producer, so a "
        "JOURNAL_KINDS row for it is a stale row by construction and reddens "
        "test_journal_kind_inventory_is_complete. Remove the row."
    )
    # Anti-vacuity: the scan this test reasons about must actually be running, or the
    # absence above would be true for the uninteresting reason that nothing is scanned.
    assert JOURNAL_KINDS and {kind for _, _, _, _, kind in _of("journalkindliteral")}


def test_unresolved_kind_sentinel_is_deliberately_absent_from_the_inventory():
    """`UNRESOLVED_DYNAMIC_KIND` must NOT be a `JOURNAL_KINDS` row either — the
    dedicated prohibition for the scan's unresolved-kind marker.

    It is the more tempting of the two, because unlike the reader-minted kind this one
    arrives in `_journal_kind_inventory_drift`'s UNDECLARED list looking exactly like a
    real kind, which is what makes an unreadable site fail loud. A reader who takes
    that failure at face value declares the row, the undeclared arm goes quiet, and the
    guard has been taught to accept every site whose kind it cannot read. An emitted
    sentinel is part of the scanned inventory, so declaring it does not necessarily
    make it stale. This dedicated prohibition catches that declaration directly.

    Ablation: add `UNRESOLVED_DYNAMIC_KIND` to `JOURNAL_KINDS` and this test reddens,
    even when an unreadable call site emits it into the inventory."""
    assert UNRESOLVED_DYNAMIC_KIND not in JOURNAL_KINDS, (
        f"{UNRESOLVED_DYNAMIC_KIND!r} is a SENTINEL this scan mints for a kind it "
        "could not read. Declaring it would hide unreadable sites from the inventory; "
        "this dedicated prohibition forbids that declaration. Remove the row and fix "
        "the unreadable CALL SITE instead."
    )
    # Anti-vacuity: the sentinel has to be a value this scan can actually emit, and the
    # inventory has to be non-empty, or the absence above holds for reasons that have
    # nothing to do with the decision it records.
    assert (
        JOURNAL_KINDS
        and UNRESOLVED_DYNAMIC_KIND
        == [
            f[4]
            for f in _scan_source("def f(self):\n    self._journal.append(**fields)\n", "engine.py")
            if f[0] == "journalkindliteral"
        ][0]
    )


def test_unresolved_kind_sentinel_is_deliberately_absent_from_the_minted_declaration():
    """`UNRESOLVED_DYNAMIC_KIND` must not be a `JOURNAL_DYNAMIC_KIND_SPELLINGS` value
    either — the same prohibition as the row above, for the minting axis.

    Same trap, worse blast radius. An unreadable interpolation reaches
    `_journal_minted_kind_drift` as a spelling carrying the sentinel, looking exactly
    like a real kind in the minted-but-undeclared list; a reader who takes that failure
    at face value declares it, and the position is then permanently blind — every
    future f-string the resolver cannot read at that position matches the declared
    sentinel and passes. Unlike a stale `JOURNAL_KINDS` row, the declaration does not
    even go stale, because the site keeps emitting it.

    It is also what makes `_fstring_kind_spellings`' and
    `_loop_literal_bindings`' "no declaration can match this" claims true; without this
    row they are aspiration.

    Ablation: add `"<unresolved-dynamic-kind>-pruned"` to the `recovery_flow.py` row —
    spelled as the literal, because the constant is defined further down the file than
    the table — and this reddens. It is the shape a reader copies straight out of a
    failure message. Siblings redden with it today only because nothing on the tree
    mints the sentinel; the moment something did, `mint_what_they_declare` would go
    QUIET at that position and this row would be the only one left objecting, which is
    the case it exists for."""
    offenders = {
        position
        for position, spellings in JOURNAL_DYNAMIC_KIND_SPELLINGS.items()
        if any(UNRESOLVED_DYNAMIC_KIND in spelling for spelling in spellings)
    }
    assert offenders == set(), (
        f"{UNRESOLVED_DYNAMIC_KIND!r} is a SENTINEL the scan mints for an interpolation "
        "it could not read. Declaring a spelling that carries it blinds the minting "
        "axis at that position for good. Remove it and make the f-string READABLE — a "
        "loop over string literals in the same scope — or leave the site red:\n"
        + "\n".join(f"  {rel}::{fn}" for rel, fn in sorted(offenders))
    )
    # Anti-vacuity: the declaration has to be non-empty, and the sentinel has to be a
    # spelling this scan can actually mint, or the absence above holds for reasons that
    # have nothing to do with the decision it records.
    assert JOURNAL_DYNAMIC_KIND_SPELLINGS and _fstring_kind_spellings(
        None, ast.parse('f"{family}-pruned"').body[0].value
    ) == {f"{UNRESOLVED_DYNAMIC_KIND}-pruned"}


def _journal_kind_inventory_drift(
    findings,
) -> tuple[list[tuple[str, int, str, str]], set[str]]:
    """Both arms of the kind inventory from one set of `journalkindliteral`
    findings: the literal kinds written but undeclared (with their sites), and the
    declared rows nothing writes any more. Returned together so the inventory test
    can grade them in one assertion — a rename is one defect with two faces."""
    scanned = {kind for _, _, _, _, kind in findings}
    undeclared = [
        (rel, ln, txt, kind) for _, rel, ln, txt, kind in findings if kind not in JOURNAL_KINDS
    ]
    return undeclared, JOURNAL_KINDS - scanned


def _journal_kind_inventory_message(
    undeclared: list[tuple[str, int, str, str]], stale: set[str]
) -> str:
    """`test_journal_kind_inventory_is_complete`'s failure text, one paragraph per arm
    and each emitted ONLY when its arm is non-empty.

    Split because the single paragraph it replaced gave every failure the same advice,
    and on one arm that advice was actively harmful. `UNRESOLVED_DYNAMIC_KIND` arrives
    in the undeclared list like any other kind — deliberately, so an unreadable site
    fails loud — but it is a SENTINEL: "add its row IN THE SAME PR" would hide the
    unreadable site from the undeclared arm. The dedicated sentinel-prohibition test
    prevents that declaration; staleness is not guaranteed for an emitted sentinel.
    The fix is upstream, at the call site.

    Per-arm emission is the other half: a message that recites the stale-row remedy
    while nothing is stale reads as three unrelated defects, and the reader has to
    work out which paragraph their failure is."""
    unreadable = [row for row in undeclared if row[3] == UNRESOLVED_DYNAMIC_KIND]
    unknown = [row for row in undeclared if row[3] != UNRESOLVED_DYNAMIC_KIND]
    paragraphs = ["the literal journal kinds and JOURNAL_KINDS disagree."]
    if unknown:
        paragraphs.append(
            "A kind a producer writes but no row declares — add its row IN THE SAME "
            "PR as what covers its record: a diagnostics routing row if any field "
            "carries an identifier, a path or free text, and the test asserting the "
            "record at the layer that reads it; or drop the write:\n"
            + "\n".join(
                f"  undeclared {rel}:{ln}: {kind!r} — {txt.strip()}"
                for rel, ln, txt, kind in unknown
            )
        )
    if unreadable:
        paragraphs.append(
            f"A site emitting the unresolved-kind marker ({UNRESOLVED_DYNAMIC_KIND!r}) "
            "— fix it at the CALL SITE: this spelling is reserved; if already written "
            "literally, replace it with a different kind. Otherwise spell the kind as a "
            "string literal, or hand the declared position one, instead of a variable, "
            "an f-string or a `**` splat the scan cannot read into. Do NOT add a "
            "JOURNAL_KINDS row for the sentinel: the dedicated "
            "test_unresolved_kind_sentinel_is_deliberately_absent_from_the_inventory "
            "forbids declaring it, which would hide unreadable sites:\n"
            + "\n".join(f"  unreadable {rel}:{ln}: {txt.strip()}" for rel, ln, txt, _ in unreadable)
        )
    if stale:
        paragraphs.append(
            "A row no producer writes any more — delete it and retire its "
            "routing/test rows deliberately, because a stale row pre-approves the "
            "next record that reuses the name:\n"
            + "\n".join(f"  stale row: {kind!r}" for kind in sorted(stale))
        )
    return "\n".join(paragraphs)


def test_journal_kind_inventory_drift_reports_a_rename_on_both_arms():
    """A rename is one undeclared spelling AND one stale row, from the same findings.

    Ablation: make `_journal_kind_inventory_drift` return the stale arm only when
    the undeclared arm is empty (the sequential-assert shape) and this reddens."""
    synthetic = [
        ("journalkindliteral", "engine.py", 1, f'journal.append("{kind}")', kind)
        for kind in sorted(JOURNAL_KINDS)
        if kind != "epic-boundary"
    ] + [
        (
            "journalkindliteral",
            "engine.py",
            7728,
            'self.journal.append("epic-boundary-renamed", epic=e)',
            "epic-boundary-renamed",
        )
    ]
    undeclared, stale = _journal_kind_inventory_drift(synthetic)
    assert [(rel, ln, kind) for rel, ln, _, kind in undeclared] == [
        ("engine.py", 7728, "epic-boundary-renamed")
    ]
    assert stale == {"epic-boundary"}


def test_journal_kind_inventory_message_gives_each_arm_its_own_remedy():
    """The failure text names ONLY the arms that actually failed, and the sentinel arm
    gets the opposite advice from the undeclared arm.

    `UNRESOLVED_DYNAMIC_KIND` reaches `_journal_kind_inventory_drift`'s undeclared list
    like a real kind — that is what makes an unreadable site fail loud — so the single
    paragraph this replaced handed the reader "add its row IN THE SAME PR" for a
    sentinel. Following it hides the unreadable site from the undeclared arm; the
    dedicated sentinel-prohibition test catches that declaration even when the
    sentinel is emitted and therefore is not stale.

    Ablation: drop the `row[3] == UNRESOLVED_DYNAMIC_KIND` partition so both kinds
    share one paragraph and the sentinel row's `not in` assertions redden; emit every
    paragraph unconditionally and the stale/undeclared exclusions redden."""
    site = ("plugins/bus.py", 42, "self._journal.append(**fields)", UNRESOLVED_DYNAMIC_KIND)
    real = ("engine.py", 7, 'journal.append("brand-new-kind")', "brand-new-kind")

    sentinel_only = _journal_kind_inventory_message([site], set())
    assert "CALL SITE" in sentinel_only and "Do NOT add a JOURNAL_KINDS row" in sentinel_only
    assert "plugins/bus.py:42" in sentinel_only
    assert "this spelling is reserved" in sentinel_only
    assert "if already written literally, replace it with a different kind" in sentinel_only
    # The harmful advice, absent: neither the add-a-row remedy nor the stale-row one.
    assert "IN THE SAME PR" not in sentinel_only
    assert "stale row" not in sentinel_only

    undeclared_only = _journal_kind_inventory_message([real], set())
    assert "IN THE SAME PR" in undeclared_only and "'brand-new-kind'" in undeclared_only
    assert "CALL SITE" not in undeclared_only
    assert "stale row" not in undeclared_only

    stale_only = _journal_kind_inventory_message([], {"epic-boundary"})
    assert "  stale row: 'epic-boundary'" in stale_only
    assert "IN THE SAME PR" not in stale_only and "CALL SITE" not in stale_only

    # All three at once still reads as three remedies, not one blurred paragraph.
    everything = _journal_kind_inventory_message([site, real], {"epic-boundary"})
    assert all(
        clue in everything for clue in ("CALL SITE", "IN THE SAME PR", "  stale row: ")
    ), everything


def test_journal_field_guard_actually_saw_the_producers():
    """The sibling assertion is an ABSENCE, so it is green both when every field is
    accounted for and when the scan stopped finding journal writes at all. This is
    the half that cannot be: empty the inventories and the guard must name a real
    producer, which proves the scan reached them.

    Also pins the three shapes the scan must not lose — the routed names really are
    produced (so ``JOURNAL_ROUTED_FIELDS`` is coupled to live producers rather than
    to a copied list), every declared splat hole still holds exactly the number of
    unresolved ``**`` keyword arguments it declares (so neither a stale
    ``JOURNAL_SPLAT_ALLOW`` entry sanctioning nothing nor a splat added inside a
    declared one can pass), and every declared BENIGN name still has a producer.

    The splat half grades COUNTS, not membership, and the count's unit is ONE
    UNRESOLVED ``**`` KEYWORD ARGUMENT — never one write call and never one call site;
    ``_journal_measured_splats`` is where that unit is defined. A set comparison is
    blind to a second splat dropped inside an already-declared position — the position
    is still in both sets — which is exactly how such a splat's field names escaped
    the inventory with this row green. The finer unit is what makes the second splat
    visible whether it arrives as a new CALL or as a second ``**`` on an existing one:
    ``append(kind, **a, **b)`` at a position declaring 1 is measured 2 here.
    ``_journal_field_offenders`` reports the same over-count as offending LINES, and
    that overlap is deliberate: this row is the only one that can see the two
    staleness directions, because a position that lost an unresolved argument leaves
    no finding for a filter over findings to flag.

    That benign direction is the one nothing held before. The benign inventory is a
    pre-approval list, so a name whose producer was deleted does not just sit there
    inertly: it pre-approves a future, unrelated field that happens to reuse the
    spelling, with no one making the decision the inventory exists to force. The two
    exemptions are the names no CALL can spell — what ``Journal.append`` mints itself
    and what arrives through a declared splat hole."""
    findings = _of("journalfield")
    produced = {field for _, _, _, _, (field, _, _) in findings if field is not None}
    assert len(produced) > 100, f"the scan found only {len(produced)} journal fields"
    assert produced & JOURNAL_ROUTED_FIELDS, "no routed field has a static producer"
    drift = _journal_splat_count_drift(findings)
    assert drift == {}, (
        "JOURNAL_SPLAT_ALLOW no longer matches the unresolvable splats in the tree — "
        "it counts UNRESOLVED ** KEYWORD ARGUMENTS at the position (not write calls: "
        "append(kind, **a, **b) is 2), and the count is part of the declaration, so "
        "move it in the SAME PR as the splat, and record what the hole lets through "
        "in JOURNAL_SPLAT_FIELDS alongside it:\n"
        + "\n".join(
            f"  {rel}::{fn}: declared {declared}, measured {found}"
            + (
                " (undeclared position: declare the hole or resolve its keys)"
                if declared == 0
                else (
                    " (stale row: no unresolvable splat here any more, delete it)"
                    if found == 0
                    else " (drift inside a declared position)"
                )
            )
            for (rel, fn), (declared, found) in sorted(
                drift.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
            )
        )
    )
    unscannable = JOURNAL_SELF_MINTED_FIELDS.union(*JOURNAL_SPLAT_FIELDS.values())
    stale = JOURNAL_BENIGN_FIELDS - produced - unscannable
    assert stale == set(), (
        "JOURNAL_BENIGN_FIELDS names fields no producer writes any more — a benign "
        "entry outlives its producer as a standing pre-approval for the next field "
        "that reuses the name. Delete them, or record where they now come from in "
        f"JOURNAL_SPLAT_FIELDS / JOURNAL_SELF_MINTED_FIELDS: {sorted(stale)}"
    )


def test_journal_splat_tables_declare_the_same_positions():
    """``JOURNAL_SPLAT_ALLOW`` and ``JOURNAL_SPLAT_FIELDS`` are keyed identically and
    co-extensively — every declared hole has a field inventory (possibly empty) and
    every inventory has a count of the unresolved ``**`` keyword arguments it holds.

    Splitting one table into two is what makes this assertable and what makes it
    necessary. A count row with no field row would sanction a hole whose names are
    inventoried nowhere, so the benign staleness check above would start calling a
    splat-borne name dead; a field row with no count row would be a set of names the
    tree-wide count grader never visits. Neither shape reddens anywhere else — the
    field consumers subscript the positions they name, and the count grader iterates
    its own keys — so the coupling the single table used to get from its shape is
    asserted here instead.

    Also refuses a declared count of ZERO, a spelling the count shape newly makes
    expressible and which is silent everywhere else: the drift grader compares
    ``0 != 0`` and says nothing, no ``field is None`` finding exists for the offender
    filter to flag, the key-set half above passes while the ``JOURNAL_SPLAT_FIELDS``
    sibling survives — and that sibling's names keep suppressing the
    ``JOURNAL_BENIGN_FIELDS`` staleness check through ``unscannable``. The old set
    comparison reddened on exactly that row, so this is the coverage the count rewrite
    would otherwise have dropped.

    Ablation: delete either table's ``plugins/bus.py::_log`` row and this reddens; set
    its count to ``0`` and the zero half reddens."""
    assert set(JOURNAL_SPLAT_ALLOW) == set(JOURNAL_SPLAT_FIELDS), (
        "the splat count and field declarations disagree about which positions are "
        "holes — one of the two was edited alone; every declared hole needs both an "
        "unresolved-** argument count and a field inventory (an empty frozenset is a "
        "real answer):\n"
        f"  count without fields: {sorted(set(JOURNAL_SPLAT_ALLOW) - set(JOURNAL_SPLAT_FIELDS))}\n"
        f"  fields without count: {sorted(set(JOURNAL_SPLAT_FIELDS) - set(JOURNAL_SPLAT_ALLOW))}"
    )
    empty = sorted(pos for pos, count in JOURNAL_SPLAT_ALLOW.items() if count < 1)
    assert empty == [], (
        "a declared splat hole holds at least one unresolved ** argument — a count "
        "of 0 sanctions nothing while its field inventory still suppresses the benign "
        f"staleness check, so delete the row instead: {empty}"
    )


def test_journal_writers_do_not_share_a_bare_name():
    """No scanned module holds TWO journal-writing functions answering to one bare
    name — the assumption every position table in this file rests on.

    Five tables are keyed by ``(file, bare function name)``:
    ``JOURNAL_SPLAT_ALLOW`` / ``JOURNAL_SPLAT_FIELDS``,
    ``JOURNAL_DYNAMIC_KIND_ALLOW`` / ``JOURNAL_DYNAMIC_KIND_SPELLINGS`` and
    ``JOURNAL_FORWARDERS``. Such a pair aggregates into ONE row: their counts sum,
    their declared sets merge, and a splat or a dynamic kind moving from one to the
    other reddens nothing while the row it lands in still reads as accurate.

    ⚠️ The property is a pair of WRITERS, which is what the four POSITION tables need;
    it is not full name safety for ``JOURNAL_FORWARDERS``. That table makes
    ``_is_journal_write`` read a call to that NAME in that FILE as a journal write, so
    a same-named twin that never touches the journal still routes its callers' keywords
    into the field inventory — and a non-writer emits nothing, so this row cannot see
    it. ``_journal_bare_name_collisions``' docstring carries the limit; closing it
    would mean widening the classifier, which DW-152 does not ask for.

    DW-152 deliberately deferred qualifying the key to ``class.method``; what it did
    not defer is knowing when the writer-pair half of the deferral stops being safe.
    That day is this row going red.

    The identity comes from the scan, not from a name: the journal-write emit carries
    each write's enclosing ``def`` lineno alongside the bare name, so two definitions
    are two linenos even when the name is one. Module-level writes are never a
    collision — a file has one module scope.

    Ablation (observed): appending a second journal-writing ``_log`` to
    ``src/bmad_loop/plugins/bus.py`` reddens this naming
    ``plugins/bus.py::_log defined at lines [257, 263]``. The probe rows in
    ``test_journal_bare_name_collision_probes_read_def_identity`` carry the mutations
    that ablation cannot reach — the lookalikes that must stay silent."""
    scoped = _of("journalfnscope")
    collisions = _journal_bare_name_collisions(scoped)
    assert collisions == {}, (
        "two journal-writing functions in one module share a bare function name, "
        "which is the key every journal position table uses — their declarations "
        "have silently merged. Rename one, or qualify the position keys (DW-152) "
        "and move every table with them:\n"
        + "\n".join(
            f"  {rel}::{name} defined at lines {linenos}"
            for (rel, name), linenos in sorted(collisions.items())
        )
    )
    # Anti-vacuity, over the SAME population the absence assertion just ran on. The
    # assertion above is an ABSENCE over an emit, so it is green both when no module
    # holds a duplicate and when the emit stopped firing. Every position DECLARED on
    # any of the five bare-name-keyed tables must show up in the measured population
    # — `JOURNAL_FORWARDERS` included, which is otherwise covered only by happening
    # to also be a splat row — the same coupling
    # `test_journal_field_guard_actually_saw_the_producers` makes for the field emit,
    # and the reason deleting the emit is not a way to pass this.
    measured_positions = {(rel, fn) for _, rel, _, _, (fn, _) in scoped}
    missing = (
        set(JOURNAL_SPLAT_ALLOW) | set(JOURNAL_DYNAMIC_KIND_ALLOW) | JOURNAL_FORWARDERS
    ) - measured_positions
    assert missing == set(), (
        "the def-identity emit did not reach every declared journal position, so the "
        f"collision assertion above proves nothing about them: {sorted(missing)}"
    )


def test_journal_patch_field_covers_every_kind_that_carries_it():
    """`patch` is dropped BY NAME, so the rule reaches every kind that spells it — and
    this pins which kinds those are, in both directions.

    `_JOURNAL_DROP_FIELDS`' comment described one pair of records while by-name routing
    reaches every kind spelling the name, and nothing held that reach: a further
    producer could start journalling a `patch` and inherit the drop with no one
    deciding it should. The drop happens to be the right answer for each kind declared
    today (a bare feature- or spec-named patch has no separators for `scrub_json`'s
    fallback to redact, so only removal covers it), but "happens to be right" is what
    an inventory exists to convert into a decision. This is `JOURNAL_BENIGN_FIELDS`'
    argument on a routed name.

    The unattributable-kind assertion is what keeps the equality honest. Filtering
    `kind is None` out of the scan — a write whose kind the guard cannot read — would
    drop exactly the site this test is most needed at: a `patch` added inside any
    `JOURNAL_DYNAMIC_KIND_ALLOW` function has no readable kind, so it would inherit the
    by-name drop with the equality still green. Such a write fails loud instead.

    Stated bound: a `patch` arriving through an UNRESOLVABLE `**` splat has no field
    name at all (`field=None`), so it never reaches either assertion here. That hole is
    held by the `JOURNAL_SPLAT_ALLOW` declaration and its sibling above, which pins the
    declared holes against the tree — not by this test.

    The membership assertion is the last half, and it does not stand alone: were
    `patch` to leave `_JOURNAL_DROP_FIELDS`, `test_journal_fields_are_routed_or_declared_benign`
    and `test_journal_field_offenders_split_routed_benign_and_holes[routed-name]` would
    redden too, as would several `tests/test_diagnostics.py` rows. It is asserted here
    anyway because those name the FIELD while this names the consequence for the kinds
    below, and because a future benign declaration of `patch` would quiet them while
    leaving these records shipping the path verbatim.

    Ablation: drop one kind from `JOURNAL_PATCH_KINDS` and the equality reddens naming
    it; give one of those producers an unreadable kind and the unattributable assertion
    reddens; remove `"patch"` from `diagnostics._JOURNAL_DROP_FIELDS` and the membership
    assertion reddens (with the siblings named above)."""
    rows = [
        (rel, ln, kind)
        for _, rel, ln, _, (field, _, kind) in _of("journalfield")
        if field == "patch"
    ]
    unattributable = [(rel, ln) for rel, ln, kind in rows if kind is None]
    assert not unattributable, (
        "a `patch` field is journalled at a site whose KIND this scan cannot read, so "
        "JOURNAL_PATCH_KINDS below cannot account for it and the by-name drop would be "
        "inherited with nothing deciding it. Spell the kind as a literal at the write, "
        f"or move the field: {unattributable}"
    )
    scanned = {kind for _, _, kind in rows}
    assert scanned == JOURNAL_PATCH_KINDS, (
        "the journal kinds carrying a `patch` field and JOURNAL_PATCH_KINDS disagree. "
        "`patch` is routed by NAME, so a new kind inherits the drop silently. Decide "
        "here that the drop is right for it (or that the field belongs elsewhere), then "
        "carry the decision to every surface that enumerates these kinds: this test, "
        "`JOURNAL_PATCH_KINDS`' own comment, `diagnostics._JOURNAL_DROP_FIELDS`' "
        "comment, and `tests/test_diagnostics.py::_PATCH_PATH_ROUTING_ROWS`, which "
        "asserts the drop per kind at the routing seam and stays green on its own; "
        f"undeclared: {sorted(scanned - JOURNAL_PATCH_KINDS)}, "
        f"stale: {sorted(JOURNAL_PATCH_KINDS - scanned)}"
    )
    assert "patch" in diagnostics._JOURNAL_DROP_FIELDS, (
        "`patch` left `_JOURNAL_DROP_FIELDS`: every kind in JOURNAL_PATCH_KINDS now "
        "ships an operator-selected patch path, and `scrub_json`'s fallback is the "
        "IDENTITY on a bare feature- or spec-named one."
    )


def test_journal_append_writes_only_accounted_fields(tmp_path):
    """The static guard reads CALL SITES, and ``Journal.append`` adds two field names
    that no call site spells: ``entry.setdefault("log_task", …)`` and
    ``entry.setdefault("log_pos", size)``. Both were invisible to it, and ``log_pos``
    was in neither routing set while the guard stayed green — so the sibling's claim
    about "every field a producer writes" was false by two names.

    This closes it from the only side that can: RUN an append, read the JSONL line
    back, and hold every key it actually contains to the same two inventories. A
    third ``setdefault`` cannot be added to ``Journal.append`` without landing in one
    of them.

    Ablation: add ``entry.setdefault("log_seq", 0)`` to ``Journal.append`` and this
    row reddens naming ``log_seq`` while every static assertion above stays green —
    which is the whole point of the row existing beside them."""
    run_dir = tmp_path / "run"
    j = Journal(run_dir)
    j.set_active_log("1-1-story-dev-1")
    j.append("run-start", run_type="stories")

    lines = (run_dir / JOURNAL_FILE).read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    minted = set(entry) - {"ts", "kind"}
    assert (
        "log_pos" in minted and "log_task" in minted
    ), f"Journal.append stopped stamping the pane-log pointer: {sorted(minted)}"
    unaccounted = minted - JOURNAL_ROUTED_FIELDS - JOURNAL_BENIGN_FIELDS
    assert unaccounted == set(), (
        "Journal.append writes a field name neither routed by diagnostics nor "
        "declared benign — the static guard cannot see a field the append mints "
        f"itself, so decide which side it belongs on here: {sorted(unaccounted)}"
    )


def test_no_hardcoded_posix_paths():
    """No bare ``/tmp`` / ``/proc`` / ``/dev/null`` literal outside the allowlisted
    platform-guarded Unity files; each allowed line carries a `# portability:` ack.
    Use ``os.devnull`` / ``tempfile`` / the psutil fallback instead."""
    bad = []
    for _, rel, ln, txt in _of("path"):
        if rel not in PATH_ALLOW:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (not an allowlisted file)")
        elif ACK not in txt:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (missing '{ACK}' ack)")
    assert not bad, "hardcoded POSIX path(s):\n" + "\n".join(bad)


def test_no_unguarded_sigkill():
    """``signal.SIGKILL`` is absent on Windows — reference it only via the
    ``getattr(signal, "SIGKILL", signal.SIGTERM)`` guard, never as a bare
    attribute access."""
    offenders = _of("sigkill")
    assert not offenders, "unguarded signal.SIGKILL attribute access:\n" + "\n".join(
        f"  {rel}:{ln}: {txt.strip()}" for _, rel, ln, txt in offenders
    )


def test_pid_existence_probe_only_in_liveness_helpers():
    """``os.kill(pid, 0)`` is read-only on POSIX but destructive on Windows
    (TerminateProcess) — confine it to the platform-guarded liveness helpers, each
    line carrying a `# portability:` ack. Other call sites route through
    ``platform_util.pid_alive``."""
    bad = []
    for _, rel, ln, txt in _of("killprobe"):
        if rel not in KILL_PROBE_ALLOW:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (route through platform_util.pid_alive)")
        elif ACK not in txt:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (missing '{ACK}' ack)")
    assert not bad, "os.kill(pid, 0) outside liveness helpers:\n" + "\n".join(bad)


def test_os_kill_only_in_process_host():
    """Any reachable ``os.kill`` maps to a destructive TerminateProcess on Windows —
    confine it to ``process_host.py``. Detects the literal ``os.kill(`` form only;
    import aliases and assigned aliases are deliberately not tracked — this is a
    review tripwire, not a sandbox. Other call sites route through the ProcessHost
    seam (``terminate`` / ``force_kill`` / ``is_alive``)."""
    offenders = [(rel, ln, txt) for _, rel, ln, txt in _of("oskill") if rel not in OS_KILL_ALLOW]
    assert (
        not offenders
    ), "os.kill( outside process_host.py — route it through the ProcessHost seam:\n" + "\n".join(
        f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders
    )


def test_start_new_session_only_in_detach_helpers():
    """``start_new_session=True`` is POSIX-only — confine it to the detach helpers
    (which branch on ``sys.platform``), each line carrying a `# portability:` ack."""
    bad = []
    for _, rel, ln, txt in _of("detach"):
        if rel not in DETACH_ALLOW:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (not a detach helper)")
        elif ACK not in txt:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (missing '{ACK}' ack)")
    assert not bad, "start_new_session=True outside detach helpers:\n" + "\n".join(bad)


def test_shell_true_only_in_sanctioned_spots():
    """``shell=True`` only in the two operator-authored-command spots, each line
    carrying a `# portability:` ack."""
    bad = []
    for _, rel, ln, txt in _of("shell"):
        if rel not in SHELL_ALLOW:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (not a sanctioned shell spot)")
        elif ACK not in txt:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (missing '{ACK}' ack)")
    assert not bad, "shell=True outside verify.py / plugins/bus.py:\n" + "\n".join(bad)


def test_bmad_loop_env_reads_only_in_the_registry():
    """AGENTS.md's env invariant, enforced: "New core env vars register in
    ``envvars.py``; plugin-owned env-var families stay with their plugin." Reading a
    knob inline is what made these undiscoverable before the registry existed, so a
    core module must call an ``envvars`` reader rather than touch ``os.environ``
    itself.

    COVERED — the access shape: ``os.environ.get/pop/setdefault``, ``os.getenv``,
    ``os.environ[K]``, the ``key=`` keyword form of each, and ``K in os.environ`` /
    ``not in`` (including as a link in a chained comparison). Each has a POSIX-only
    bytes twin — ``os.environb`` for the mapping forms, ``os.getenvb`` for the
    function — and both twins are covered.

    Crossed with the key spelling: a literal, a same-module constant,
    ``envvars.MUX_BACKEND``, and the registry constant imported in (see
    ``_env_read_key``). Borrowing the registry's own constant while skipping its
    reader is the *likeliest* violation rather than an exotic one — the
    tidy-looking version is the one that gets written — so every key spelling
    resolves. The bytes twins take only the first two: their keys must be bytes,
    and the registry's constants are ``str``, so that half of the cross product
    cannot be written at all rather than being an uncovered case.

    ``ENV_READ_PROBES`` / ``ENV_READ_NON_PROBES`` are that matrix, executable. Read
    them for what is covered; this docstring only argues the boundary.

    NOT COVERED, deliberately — both obscure the *lookup* rather than the key:
    rebinding the mapping (``e = os.environ; e.get(K)``) and ``from os import
    environ``. This is a review tripwire, not a sandbox: it exists to catch the
    change someone writes while trying to do the right thing, not to withstand
    someone routing around it.

    Bulk copies (``dict(os.environ)``, ``{**os.environ}``) are correctly silent:
    they name no variable, so there is no var being defined outside the registry.

    Scoped to reads. Writes are a different act: engine.py, resolve.py, probe.py,
    plugins/bus.py and unity_plugin.py all BUILD a ``BMAD_LOOP_*`` dict to inject
    into a child session, and gates.py hands notify text to osascript/PowerShell the
    same way — all producing side, none of it a second place a var is *defined*.
    Reads of a SessionSpec's ``spec.env`` (adapters/generic.py) are likewise out:
    that is a plain dict handed down in-process, not the environment.

    ⚠️ THIS assertion cannot grade the detector. It says only that today's tree
    carries no unallowlisted finding — equally green when the scan has silently
    stopped scanning. Delete any single branch of the ``envread`` detector and this
    test still passes while exactly the matching ``ENV_READ_PROBES`` rows redden.
    The assertion is the invariant; the probes are the proof it is being checked,
    and neither replaces the other. What this test does grade alone is the
    allowlist: empty ``ENV_READ_ALLOW`` and it fails naming every real read, so a
    green run means the scan saw those reads rather than that it found nothing.

    ⚠️ The prose row in ``ENV_READ_NON_PROBES`` is a CONTROL, not an ablation. A
    ``BMAD_LOOP_*`` mention in a docstring creates no key-position node at all, so
    it stays silent no matter what the detector does — an earlier revision cited it
    as proof of a docstring exclusion in ``_env_read_key`` that was in fact
    unreachable. Keep the row for the property, never as evidence.

    ⚠️ New uncovered shapes keep surfacing here, and that is a property of the
    design rather than a run of bad luck: this is a denylist of access forms, so it
    is only ever as complete as the last sweep over them, and the NOT COVERED list
    is the honest boundary rather than an oversight. Sweep an axis when you touch
    it — every mapping form at once, not the one that prompted the visit — and
    extend the matrix before the branch: add the probe row, watch it fail, then fix
    the scan.

    The exemption is scoped by variable FAMILY, not by file — see
    ``ENV_READ_ALLOW`` for why, and ``ENV_SCOPE_CASES`` for that claim as rows
    rather than prose."""
    offenders = _env_read_offenders(_of("envread"))
    assert not offenders, (
        "BMAD_LOOP_* read outside envvars.py and the families each stand-alone "
        "script owns — name the var in envvars.py and call its reader instead of "
        "widening the allowlist:\n"
        + "\n".join(f"  {rel}:{ln}: {key} — {txt.strip()}" for rel, ln, txt, key in offenders)
    )


# Every access form the env-read detector claims to cover, as a source snippet that
# MUST produce an `envread` finding. These are the executable half of the matrix the
# test above documents: that test asserts only that today's tree is clean, which stays
# green both when the invariant holds and when the detector has silently stopped
# detecting. Driving known-bad sources through the real `_scan_source` separates
# those. Snippets are parsed, never imported, so a nonexistent relative import is fine.
# Fix order when a new form turns up: add the row here FIRST and watch it fail.
ENV_READ_PROBES = [
    ("get-literal", 'import os\nX = os.environ.get("BMAD_LOOP_X")\n'),
    ("get-local-const", 'import os\nK = "BMAD_LOOP_X"\nX = os.environ.get(K)\n'),
    (
        "get-qualified-registry",
        "import os\nfrom . import envvars\nX = os.environ.get(envvars.MUX_BACKEND)\n",
    ),
    (
        "get-aliased-registry",
        "import os\nfrom . import envvars as ev\nX = os.environ.get(ev.MUX_BACKEND)\n",
    ),
    (
        "get-imported-registry",
        "import os\nfrom .envvars import MUX_BACKEND\nX = os.environ.get(MUX_BACKEND)\n",
    ),
    ("getenv", 'import os\nX = os.getenv("BMAD_LOOP_X")\n'),
    ("subscript", 'import os\ndef f():\n    return os.environ["BMAD_LOOP_X"]\n'),
    ("getenv-keyword", 'import os\nX = os.getenv(key="BMAD_LOOP_X")\n'),
    ("get-keyword", 'import os\nX = os.environ.get(key="BMAD_LOOP_X")\n'),
    ("pop-keyword", 'import os\ndef f():\n    return os.environ.pop(key="BMAD_LOOP_X")\n'),
    ("setdefault-keyword", 'import os\nX = os.environ.setdefault(key="BMAD_LOOP_X", value="v")\n'),
    ("membership-in", 'import os\nX = "BMAD_LOOP_X" in os.environ\n'),
    ("membership-not-in", 'import os\nX = "BMAD_LOOP_X" not in os.environ\n'),
    (
        "membership-registry",
        "import os\nfrom .envvars import MUX_BACKEND\nX = MUX_BACKEND in os.environ\n",
    ),
    # A chain, where the membership's left operand is the PRECEDING comparator.
    # Keyed by a literal on purpose: the registry row above already grades key
    # resolution, so this row reddens for one reason only — chain position.
    ("membership-chained", 'import os\nc = "x"\nX = c == "BMAD_LOOP_X" in os.environ\n'),
    # The `os.environb` axis, swept rather than sampled: every mapping form above
    # has a bytes twin, and `os.getenvb` is the twin of `os.getenv`. Keys here are a
    # bytes literal or a bytes constant, which is the whole spelling axis — the
    # registry's constants are `str` and would raise against a bytes mapping.
    ("environb-get", 'import os\nX = os.environb.get(b"BMAD_LOOP_X")\n'),
    ("environb-subscript", 'import os\ndef f():\n    return os.environb[b"BMAD_LOOP_X"]\n'),
    ("environb-local-const", 'import os\nK = b"BMAD_LOOP_X"\nX = os.environb.get(K)\n'),
    ("environb-pop-keyword", 'import os\nX = os.environb.pop(key=b"BMAD_LOOP_X")\n'),
    ("environb-setdefault", 'import os\nX = os.environb.setdefault(b"BMAD_LOOP_X", b"v")\n'),
    ("environb-membership", 'import os\nX = b"BMAD_LOOP_X" in os.environb\n'),
    (
        "environb-membership-chained",
        'import os\nc = b"y"\nX = c == b"BMAD_LOOP_X" in os.environb\n',
    ),
    ("getenvb", 'import os\nX = os.getenvb(b"BMAD_LOOP_X")\n'),
    ("getenvb-keyword", 'import os\nX = os.getenvb(key=b"BMAD_LOOP_X")\n'),
]

# The other half: shapes that must stay SILENT. Without these the detector could pass
# every probe above by flagging everything, which would be just as broken — a guard
# that cries wolf gets its allowlist widened until it means nothing.
ENV_READ_NON_PROBES = [
    ("bulk-dict-copy", "import os\nX = dict(os.environ)\n"),
    ("bulk-splat-copy", "import os\nX = {**os.environ}\n"),
    ("bulk-copy-method", "import os\nX = os.environ.copy()\n"),
    ("foreign-var", 'import os\nX = os.environ.get("PATH")\n'),
    (
        "prose-in-docstring",
        'import os\ndef f():\n    """Injects BMAD_LOOP_X downstream."""\n    return 1\n',
    ),
    ("write-not-read", 'import os\nos.environ["BMAD_LOOP_X"] = "1"\n'),
    ("session-spec-env", 'def f(spec):\n    return spec.env.get("BMAD_LOOP_X")\n'),
]


@pytest.mark.parametrize(("label", "source"), ENV_READ_PROBES, ids=[p[0] for p in ENV_READ_PROBES])
def test_env_read_detector_flags_every_claimed_access_form(label, source):
    """Each documented access form really does produce a finding.

    This is the check the repo-wide assertion cannot be: delete any single branch of
    the `envread` scan and the tree-wide test stays green (nothing in `src/` uses that
    branch today), while exactly the matching row here reddens. The coverage claim
    lives here rather than in the guard's docstring alone, because prose does not
    fail a build."""
    found = [f for f in _scan_source(source, "probe.py") if f[0] == "envread"]
    assert found, (
        f"the {label!r} access form produced no `envread` finding — the detector does "
        f"not cover a shape the guard's docstring claims:\n{source}"
    )


@pytest.mark.parametrize(
    ("label", "source"), ENV_READ_NON_PROBES, ids=[p[0] for p in ENV_READ_NON_PROBES]
)
def test_env_read_detector_stays_silent_on_non_reads(label, source):
    """The complement: a bulk environment copy, a foreign variable, prose, a WRITE,
    and a `SessionSpec.env` lookup are all silent. Pins the scoping decisions the
    guard's docstring argues for, so narrowing or widening the detector has to be
    deliberate — and stops a future fix from passing the probes by flagging
    everything."""
    found = [f for f in _scan_source(source, "probe.py") if f[0] == "envread"]
    assert not found, (
        f"the {label!r} shape was flagged as an env read; it is deliberately out of "
        f"scope:\n{source}"
    )


# The git-argv detector's probe matrix, same rationale as the env pair above:
# nothing in `src/` builds a bare ["git", ...] today, so deleting the detector
# branch leaves the tree-wide guard green — only these rows redden.
GIT_ARGV_PROBES = [
    ("bare-run", 'import subprocess\nsubprocess.run(["git", "-C", str(p), "status"])\n'),
    ("bare-popen", 'import subprocess\nsubprocess.Popen(["git", "ls-files"])\n'),
    ("argv-built-first", 'argv = ["git", "log", "-1"]\n'),
    # subprocess accepts any sequence, so the tuple spelling is a legal spawn —
    # unlike tmux there is no which-tuple shape to spare, so it is flagged even
    # unattached to a call (a false positive is a review prompt, not a miss).
    ("tuple-argv", 'import subprocess\nsubprocess.run(("git", "status"))\n'),
    # The executable factored into a named constant — the head resolves through
    # the module's own bindings, as the env detector's aliases do.
    (
        "named-executable",
        'import subprocess\nGIT = "git"\nsubprocess.run([GIT, "status"])\n',
    ),
    # …and a rebind does not launder it: any binding to "git" qualifies the name.
    (
        "named-executable-rebound",
        'import subprocess\nGIT = "git"\nGIT = "other"\nsubprocess.run([GIT, "status"])\n',
    ),
    # The string spellings: a shell command, and the same string with no shell —
    # which Windows execs directly — plus the from-import spawn name.
    (
        "string-shell",
        'import subprocess\nsubprocess.run("git status", shell=True)\n',
    ),
    (
        "string-no-shell",
        'import subprocess\nsubprocess.Popen("git -C . log")\n',
    ),
    (
        "string-from-import",
        'from subprocess import run\nrun("git status", shell=True)\n',
    ),
    # …and the string command factored into a constant resolves the same way a
    # sequence head does — the last cell of the spelling matrix
    # ({sequence, string} × {inline, named}).
    (
        "string-named-command",
        'import subprocess\nGIT_STATUS = "git status"\nsubprocess.run(GIT_STATUS, shell=True)\n',
    ),
]
GIT_ARGV_NON_PROBES = [
    ("path-segment", 'from pathlib import Path\nX = Path(h) / "git" / "ignore"\n'),
    ("prose-in-docstring", 'def f():\n    """Runs `git add -A` downstream."""\n    return 1\n'),
    ("chokepoint-args-tail", 'proc = git_bytes(repo, "ls-files", "-z")\n'),
    # A named head that binds to a DIFFERENT executable, and one that never binds
    # at all (a parameter), stay silent — the alias reach is exactly the names
    # the module itself ties to "git".
    (
        "named-other-executable",
        'import subprocess\nRG = "rg"\nsubprocess.run([RG, "--files"])\n',
    ),
    (
        "named-unbound-head",
        'import subprocess\ndef run(exe):\n    return subprocess.run([exe, "status"])\n',
    ),
    # The string check anchors on spawn calls and on the word boundary: a git
    # command in a NON-spawn call (the message shape — an exception, a logger)
    # and a different program that merely starts with "git" both stay silent.
    (
        "string-in-message-call",
        'raise RuntimeError("git status failed")\n',
    ),
    (
        "string-other-program",
        'import subprocess\nsubprocess.run("gitk", shell=True)\n',
    ),
    # The named-command reach is exactly the strings the module ties to git:
    # a different command and a "git"-prefixed different program stay silent
    # through the alias path too.
    (
        "string-named-other-command",
        'import subprocess\nLS = "ls -la"\nsubprocess.run(LS, shell=True)\n',
    ),
    (
        "string-named-other-program",
        'import subprocess\nGITK = "gitk"\nsubprocess.run(GITK, shell=True)\n',
    ),
]


@pytest.mark.parametrize(("label", "source"), GIT_ARGV_PROBES, ids=[p[0] for p in GIT_ARGV_PROBES])
def test_git_argv_detector_flags_every_spawn_shape(label, source):
    """Each spawn shape produces a `git` finding — including an argv bound to a
    name first, which is how a bypass would most tidily be written."""
    found = [f for f in _scan_source(source, "probe.py") if f[0] == "git"]
    assert found, f"the {label!r} shape produced no `git` finding:\n{source}"


@pytest.mark.parametrize(
    ("label", "source"), GIT_ARGV_NON_PROBES, ids=[p[0] for p in GIT_ARGV_NON_PROBES]
)
def test_git_argv_detector_stays_silent_on_lookalikes(label, source):
    """The complement: a path segment, prose, and the chokepoint's own args-tail
    name git without building an argv — flagging them would get the allowlist
    widened until it means nothing."""
    found = [f for f in _scan_source(source, "probe.py") if f[0] == "git"]
    assert not found, f"the {label!r} shape was flagged; it is not a git argv:\n{source}"


# The git exemption's scoping, as rows: `(rel, source, is_offender)`. Every git
# argv in verify.py today already sits in a `_run_git(...)` call, so a file-wide
# filter and the call-position one are indistinguishable on the real tree — only
# synthetic sources can tell them apart.
GIT_SCOPE_CASES = [
    # The hole a file-wide exemption leaves open: a verify.py helper spawning git
    # directly, past the timeout, the locale pin, and the GitError taxonomy.
    (
        "verify-bare-spawn",
        "verify.py",
        'import subprocess\nsubprocess.run(["git", "status"])\n',
        True,
    ),
    # The tuple spelling of the same bypass stays refused inside the file too.
    (
        "verify-bare-tuple",
        "verify.py",
        'import subprocess\nsubprocess.run(("git", "status"))\n',
        True,
    ),
    # …while the chokepoint's real feed line stays exempt: the argv as
    # `_run_git`'s first argument, the shape of every sanctioned site today.
    (
        "verify-chokepoint-arg",
        "verify.py",
        'proc = _run_git(["git", "-C", str(repo), "status"], repo)\n',
        False,
    ),
    # An argv bound to a name first is flagged even en route to `_run_git` — the
    # detector's documented stance (a false positive is a review prompt, not a
    # miss), and today's tree has no such site to spare.
    (
        "verify-argv-built-first",
        "verify.py",
        'argv = ["git", "log", "-1"]\nproc = _run_git(argv, repo)\n',
        True,
    ),
    # The private spelling does not travel: `_run_git` imported into another
    # module is an offender there, argv position notwithstanding.
    (
        "engine-calls-run-git",
        "engine.py",
        'proc = _run_git(["git", "fetch"], repo)\n',
        True,
    ),
    # The string form is refused inside verify.py too — there `shell=True` is
    # allowlisted (SHELL_ALLOW), so without this the spelling would slip both
    # tripwires at once; it can never be the chokepoint's feed position, since
    # `_run_git` takes a sequence.
    (
        "verify-string-shell",
        "verify.py",
        'import subprocess\nsubprocess.run("git status", shell=True)\n',
        True,
    ),
]


@pytest.mark.parametrize(
    ("label", "rel", "source", "is_offender"),
    GIT_SCOPE_CASES,
    ids=[c[0] for c in GIT_SCOPE_CASES],
)
def test_git_argv_exemption_is_scoped_to_the_chokepoint_call(label, rel, source, is_offender):
    """Being verify.py buys the file its `_run_git(...)` feed lines and nothing
    wider. Without this, `_git_offenders` could go back to exempting the file
    wholesale and every assertion in this file would stay green — the difference
    only shows up on a bypass that does not exist yet, which is the only kind a
    tripwire is for."""
    offenders = _git_offenders([f for f in _scan_source(source, rel) if f[0] == "git"])
    assert bool(offenders) is is_offender, (
        f"a git argv in {rel} here should {'be refused' if is_offender else 'be allowed'}:\n"
        f"{source}"
    )


# The review-gate chokepoint's scoping, as rows: `(rel, source, is_offender)`.
# The repo-wide assertion above cannot distinguish a working detector from a
# broken one — today's tree has exactly one call, inside the sanctioned helper, so
# "nothing is flagged" is green both when the invariant holds and when the scan
# stopped seeing calls at all. Only synthetic sources separate the two, and only
# they can carry the bypass that does not exist yet.
VERIFY_COMMANDS_SCOPE_CASES = [
    # The bug this refuses, in the shape it would actually take: a fourth review
    # gate composing run+classify itself, against whichever root it picked (#695).
    (
        "fourth-gate-direct-call",
        "verify.py",
        "def verify_review_epic(task, paths, policy):\n"
        "    return verify_commands_outcome(policy, paths.project)\n",
        True,
    ),
    # Same bypass reached through the module attribute, from outside core — the
    # spelling any non-verify caller would use.
    (
        "engine-attribute-call",
        "engine.py",
        "from . import verify\n"
        "def _verify_review(self, task):\n"
        "    return verify.verify_commands_outcome(self.policy, self.workspace.root)\n",
        True,
    ),
    # Being verify.py is not enough on its own: a second helper in the same file
    # calling the composition with some other cwd is exactly what the position
    # bit exists to catch, and a file-wide exemption would wave it through.
    (
        "verify-other-helper",
        "verify.py",
        "def _verify_something_else(policy, paths):\n"
        "    return verify_commands_outcome(policy, paths.project)\n",
        True,
    ),
    # The name does not travel: a `_verify_review_commands` grown in another
    # module cannot sanction itself, which is why the filter pairs the enclosing
    # function with the FILE.
    (
        "helper-name-in-another-file",
        "sweep.py",
        "def _verify_review_commands(policy, paths):\n"
        "    return verify_commands_outcome(policy, paths.repo_root)\n",
        True,
    ),
    # …while the real sanctioned site stays silent.
    (
        "sanctioned-helper",
        "verify.py",
        "def _verify_review_commands(policy, paths, *, on_results=None):\n"
        "    return verify_commands_outcome(policy, paths.repo_root, on_results=on_results)\n",
        False,
    ),
    (
        "rename-on-import",
        "engine.py",
        "from .verify import verify_commands_outcome as classify\n"
        "def _verify_review(self, task):\n"
        "    return classify(self.policy, self.workspace.root)\n",
        True,
    ),
    (
        "assignment-alias",
        "engine.py",
        "from . import verify\n"
        "classify = verify.verify_commands_outcome\n"
        "def _verify_review(self, task):\n"
        "    return classify(self.policy, self.workspace.root)\n",
        True,
    ),
    (
        "annotated-assignment-alias",
        "engine.py",
        "from . import verify\n"
        "classify: object = verify.verify_commands_outcome\n"
        "def _verify_review(self, task):\n"
        "    return classify(self.policy, self.workspace.root)\n",
        True,
    ),
    (
        "literal-getattr",
        "engine.py",
        "from . import verify\n"
        "def _verify_review(self, task):\n"
        "    return getattr(verify, 'verify_commands_outcome')(self.policy, self.workspace.root)\n",
        True,
    ),
    (
        "sanctioned-assignment-alias",
        "verify.py",
        "classify = verify_commands_outcome\n"
        "def _verify_review_commands(policy, paths, *, on_results=None):\n"
        "    return classify(policy, paths.repo_root, on_results=on_results)\n",
        False,
    ),
    # A nested def inside the helper is still inside it — `_function_body_nodes`
    # walks each body statement and `ast.walk` descends from there, and a closure
    # that forwards the composition is not a second call site.
    (
        "nested-inside-helper",
        "verify.py",
        "def _verify_review_commands(policy, paths, *, on_results=None):\n"
        "    def run():\n"
        "        return verify_commands_outcome(policy, paths.repo_root, on_results=on_results)\n"
        "    return run()\n",
        False,
    ),
    # The bound this guard deliberately does NOT claim: `run_verify_commands` has
    # three legitimate callers on two roots, so calling it directly is not an
    # offence here. Widening to it would turn the allowlist into a caller list.
    (
        "run_verify_commands-untouched",
        "cli.py",
        "for result in verify.run_verify_commands(pol, cwd):\n    pass\n",
        False,
    ),
    # Prose naming the function is a Constant, not a Call — `cli._reverify`'s
    # "Deliberately NOT `verify_commands_outcome`" docstring must stay silent, or
    # the first fix would be to delete the sentence that explains the design.
    (
        "prose-in-docstring",
        "cli.py",
        'def _reverify(project, cwd):\n    """Deliberately NOT verify_commands_outcome."""\n',
        False,
    ),
    # A decorator and a default argument are evaluated where the function is
    # DEFINED, not inside its body, so a composition parked in one is a second call
    # site wearing the sanctioned helper's name. `ast.walk(fn)` hands both back and
    # would sanction them; `_function_body_nodes` does not. ABLATION for these two
    # rows: restore `for call in ast.walk(fn)` in `sanctioned_verify_command_calls`
    # and both must go green-as-allowed, i.e. FAIL here.
    (
        "default-arg-bypass",
        "verify.py",
        "def _verify_review_commands(policy, paths, *, outcome=verify_commands_outcome(POLICY, ROOT)):\n"
        "    return outcome\n",
        True,
    ),
    (
        "decorator-bypass",
        "verify.py",
        "@register(verify_commands_outcome(POLICY, ROOT))\n"
        "def _verify_review_commands(policy, paths):\n"
        "    return None\n",
        True,
    ),
]


# The classifier half's scoping, as rows: `(rel, source, is_offender)`. Same
# reason the wrapper's matrix is executable — today's tree has exactly two calls,
# both sanctioned, so the repo-wide assertion is green whether the invariant holds
# or the scan stopped seeing calls.
VERIFY_CLASSIFY_SCOPE_CASES = [
    # THE hole the wrapper guard leaves open, in the shape it would actually be
    # written: a fourth gate composing run+classify by hand and picking its own
    # root, twice. Note `run_verify_commands` inside it is deliberately NOT an
    # offence — only the classifier call is flagged.
    (
        "hand-composed-fourth-gate",
        "verify.py",
        "def verify_review_epic(task, paths, policy):\n"
        "    return verify_command_results_outcome(\n"
        "        run_verify_commands(policy, paths.project), paths.project\n"
        "    )\n",
        True,
    ),
    # The same bypass from outside core, through the module attribute.
    (
        "sweep-attribute-call",
        "sweep.py",
        "from . import verify\n"
        "def _verify_review(self, task):\n"
        "    results = verify.run_verify_commands(self.policy, self.workspace.paths.project)\n"
        "    return verify.verify_command_results_outcome(results, self.workspace.paths.project)\n",
        True,
    ),
    # Being verify.py is not enough: a second helper there calling the classifier
    # is exactly what the position bit exists to catch.
    (
        "verify-other-helper",
        "verify.py",
        "def _classify_somewhere_else(results, cwd):\n"
        "    return verify_command_results_outcome(results, cwd)\n",
        True,
    ),
    # The two sanctioned positions stay silent — and they are FILE-SPECIFIC ...
    (
        "sanctioned-wrapper-in-verify",
        "verify.py",
        "def verify_commands_outcome(policy, cwd, *, on_results=None):\n"
        "    results = run_verify_commands(policy, cwd)\n"
        "    return verify_command_results_outcome(results, cwd)\n",
        False,
    ),
    (
        "rename-on-import",
        "sweep.py",
        "from .verify import verify_command_results_outcome as classify\n"
        "def _verify_review(self, task):\n"
        "    return classify(results, self.workspace.root)\n",
        True,
    ),
    (
        "assignment-alias",
        "sweep.py",
        "from . import verify\n"
        "classify = verify.verify_command_results_outcome\n"
        "def _verify_review(self, task):\n"
        "    return classify(results, self.workspace.root)\n",
        True,
    ),
    (
        "annotated-assignment-alias",
        "sweep.py",
        "from . import verify\n"
        "classify: object = verify.verify_command_results_outcome\n"
        "def _verify_review(self, task):\n"
        "    return classify(results, self.workspace.root)\n",
        True,
    ),
    (
        "sanctioned-dev-side-in-engine",
        "engine.py",
        "def _verify_commands_with_results(self, task, verification_stage):\n"
        "    results = tuple(verify.run_verify_commands(self.policy, self.workspace.root))\n"
        "    return verify.verify_command_results_outcome(list(results), self.workspace.root)\n",
        False,
    ),
    # ... which is the half a NAME-ONLY collection would lose: each sanctioned
    # function name, in the OTHER file, is an offender. Note where that half is
    # actually enforced — `sanctioned_classify_calls` keys the enclosing name off
    # `VERIFY_CLASSIFY_CHOKEPOINT.get(rel)`, so a call in the wrong file never
    # enters the set at all. The `rel in VERIFY_CLASSIFY_CHOKEPOINT` test in
    # `_verify_classify_offenders` is therefore belt-and-braces, kept for symmetry
    # with the wrapper filter (where it IS load-bearing, since that sanctioned
    # caller is a bare name). ABLATION for these two rows: relax the collection to
    # `fn.name in set(VERIFY_CLASSIFY_CHOKEPOINT.values())` — dropping the filter's
    # redundant file test does NOT redden them, and mistaking one for the other
    # would leave the real keying untested.
    (
        "dev-side-name-in-verify",
        "verify.py",
        "def _verify_commands_with_results(self, task, verification_stage):\n"
        "    return verify_command_results_outcome(results, self.workspace.root)\n",
        True,
    ),
    (
        "wrapper-name-in-engine",
        "engine.py",
        "def verify_commands_outcome(policy, cwd):\n"
        "    return verify_command_results_outcome(run_verify_commands(policy, cwd), cwd)\n",
        True,
    ),
    # A nested def inside a sanctioned function is still inside it.
    (
        "nested-inside-sanctioned",
        "verify.py",
        "def verify_commands_outcome(policy, cwd, *, on_results=None):\n"
        "    def classify(results):\n"
        "        return verify_command_results_outcome(results, cwd)\n"
        "    return classify(run_verify_commands(policy, cwd))\n",
        False,
    ),
    # Prose is a Constant, not a Call: the docstrings that explain this very
    # split must not be the thing that trips it.
    (
        "prose-in-docstring",
        "verify.py",
        "def _verify_review_commands(policy, paths):\n"
        '    """Kept separate from verify_command_results_outcome."""\n',
        False,
    ),
    # The decorator/default bypass, for the classifier half. Same reason as the
    # wrapper rows above. ABLATION: restore `for call in ast.walk(fn)` in
    # `sanctioned_classify_calls` and both rows must FAIL.
    (
        "default-arg-bypass",
        "verify.py",
        "def verify_commands_outcome(policy, cwd, *, outcome=verify_command_results_outcome(RESULTS, ROOT)):\n"
        "    return outcome\n",
        True,
    ),
    (
        "decorator-bypass",
        "verify.py",
        "@register(verify_command_results_outcome(RESULTS, ROOT))\n"
        "def verify_commands_outcome(policy, cwd):\n"
        "    return None\n",
        True,
    ),
]


@pytest.mark.parametrize(
    ("label", "rel", "source", "is_offender"),
    VERIFY_CLASSIFY_SCOPE_CASES,
    ids=[c[0] for c in VERIFY_CLASSIFY_SCOPE_CASES],
)
def test_verify_classify_detector_is_scoped_to_its_two_compositions(
    label, rel, source, is_offender
):
    """Both halves of the classifier detector, driven through `_scan_source` — the
    same code path the real scan uses — so "flags the hand-composed gate" and
    "stays silent on the two real compositions" are asserted rather than inferred
    from an empty repo-wide result."""
    findings = [f for f in _scan_source(source, rel) if f[0] == "verifyclassify"]
    offenders = _verify_classify_offenders(findings)
    assert bool(offenders) is is_offender, (
        f"a verify_command_results_outcome call in {rel} here should "
        f"{'be refused' if is_offender else 'be allowed'}:\n{source}"
    )


def test_verify_classify_detector_leaves_run_verify_commands_alone():
    """The bound this guard does NOT claim, asserted so it cannot drift shut.

    `run_verify_commands` has three legitimate callers on two different roots (the
    dev side in `Workspace.root`, `_verify_review_commands` in `repo_root`, and
    `cli._reverify`), so it is not a chokepoint of this shape and the spec forbids
    widening to it. The hand-composed probe above contains such a call precisely so
    a future widening reddens here instead of silently turning the allowlist into a
    caller list."""
    source = (
        "def verify_review_epic(task, paths, policy):\n"
        "    return verify_command_results_outcome(\n"
        "        run_verify_commands(policy, paths.project), paths.project\n"
        "    )\n"
    )
    findings = _scan_source(source, "verify.py")
    # exactly ONE finding from that snippet, and it is the classifier call
    assert [f[0] for f in findings if f[0].startswith("verify")] == ["verifyclassify"]


@pytest.mark.parametrize(
    ("label", "rel", "source", "is_offender"),
    VERIFY_COMMANDS_SCOPE_CASES,
    ids=[c[0] for c in VERIFY_COMMANDS_SCOPE_CASES],
)
def test_verify_commands_detector_is_scoped_to_the_review_helper(label, rel, source, is_offender):
    """Both halves of the detector, driven through `_scan_source` — the same code
    path the real scan uses — so "flags the bad shape" and "stays silent on the
    good one" are asserted rather than inferred from an empty repo-wide result."""
    findings = [f for f in _scan_source(source, rel) if f[0] == "verifycmd"]
    offenders = _verify_command_offenders(findings)
    assert bool(offenders) is is_offender, (
        f"a verify_commands_outcome call in {rel} here should "
        f"{'be refused' if is_offender else 'be allowed'}:\n{source}"
    )


# The allowlist's scoping, as rows: `(rel, key, is_offender)`. Same reason the
# access-form matrix is executable — a file-scoped exemption and a family-scoped one
# are indistinguishable on today's tree, where every read already sits inside its
# own family, so only synthetic findings can tell them apart.
ENV_SCOPE_CASES = [
    # A core knob read inline from a file that is exempt for OTHER reasons. This is
    # the case a file-wide allowlist drops on the path alone.
    ("unity-reads-core-knob", "data/plugins/unity/unity_ready.py", "BMAD_LOOP_MUX_BACKEND", True),
    ("hook-reads-core-knob", "data/bmad_loop_hook.py", "BMAD_LOOP_SESSION_TIMEOUT_S", True),
    # The registry is scoped to the names it defines, not to the prefix at large.
    ("registry-reads-session-var", "envvars.py", "BMAD_LOOP_RUN_DIR", True),
    # …and the reads each file genuinely owns stay exempt.
    ("unity-reads-own-family", "data/plugins/unity/unity_ready.py", "BMAD_LOOP_UNITY_PATH", False),
    (
        "unity-reads-engine-family",
        "data/plugins/unity/unity_setup.py",
        "BMAD_LOOP_ENGINE_MCP",
        False,
    ),
    ("unity-reads-session-var", "data/plugins/unity/unity_cleanup.py", "BMAD_LOOP_WORKTREE", False),
    ("hook-reads-session-var", "data/bmad_loop_hook.py", "BMAD_LOOP_RUN_DIR", False),
    ("registry-reads-own-name", "envvars.py", "BMAD_LOOP_MUX_BACKEND", False),
    # A non-allowlisted core module is refused whatever the key.
    ("core-module-any-key", "verify.py", "BMAD_LOOP_RUN_DIR", True),
    # An entry naming ONE variable must not exempt every longer name built on it,
    # or a new unregistered knob rides in on a registered one's spelling.
    ("registry-name-extended", "envvars.py", "BMAD_LOOP_MUX_BACKEND_FALLBACK", True),
    ("session-name-extended", "data/bmad_loop_hook.py", "BMAD_LOOP_RUN_DIR_EXTRA", True),
    # …while a real family (trailing underscore) still covers a member it has
    # never seen, which is what makes it a family rather than a list.
    (
        "unity-family-unseen-member",
        "data/plugins/unity/unity_ready.py",
        "BMAD_LOOP_UNITY_NEW",
        False,
    ),
]


@pytest.mark.parametrize(
    ("label", "rel", "key", "is_offender"),
    ENV_SCOPE_CASES,
    ids=[c[0] for c in ENV_SCOPE_CASES],
)
def test_env_read_allowlist_is_scoped_by_family_not_by_file(label, rel, key, is_offender):
    """Being allowlisted buys a file its own variable families and nothing wider.

    Without this, `ENV_READ_ALLOW` could go back to a set of paths and every
    assertion in this file would stay green — the distinction only shows up on a
    read that does not exist yet, which is the only kind a tripwire is for."""
    offenders = _env_read_offenders([("envread", rel, 1, f"os.environ.get({key!r})", key)])
    assert bool(offenders) is is_offender, (
        f"{rel} reading {key} should {'be refused' if is_offender else 'be allowed'}; "
        f"declared families for that file: {ENV_READ_ALLOW.get(rel, ())}"
    )


# The artifact-literal detector's probe matrix. Today's tree has exactly ONE
# `taskartifact` finding (generic.py's `_result_path`, allowlisted), so deleting the
# detector branch leaves every tree-wide assertion green — only these rows redden.
#
# Every source below is BUILT BY ITERATING `TASK_CYCLE_ARTIFACTS` rather than by
# indexing it. Two reasons, and the second is the load-bearing one: a renamed
# artifact cannot leave a probe grading a string nothing produces any more, and a
# constant that SHRINKS cannot raise `IndexError` while this module is being
# imported. That error arrives at COLLECTION and takes every guard in this file down
# with it — the POSIX, git, env-read and spec-anchor ones included — which is a very
# large blast radius for a one-line edit in `journal.py`. Iteration degrades to
# fewer rows instead, and `test_artifact_probe_tables_are_not_empty` states the floor.
_ARTIFACT_TUPLE_SRC = ", ".join(f'"{name}"' for name in TASK_CYCLE_ARTIFACTS)
TASK_ARTIFACT_PROBES = [
    *(
        (f"unlink-literal:{name}", f'(task_dir / "{name}").unlink(missing_ok=True)\n')
        for name in TASK_CYCLE_ARTIFACTS
    ),
    *(
        (f"read-literal:{name}", f'doc = json.loads((d / "{name}").read_text())\n')
        for name in TASK_CYCLE_ARTIFACTS
    ),
    # The re-introduced pair, in the shape the extraction removed: an inline tuple
    # in a for-loop, which is how the reader spelled it.
    ("inline-tuple-loop", f"for fname in ({_ARTIFACT_TUPLE_SRC}):\n    pass\n"),
    # A second module re-declaring the constant is a COPY, not the definition — the
    # definition skip is keyed to journal.py (see TASK_ARTIFACT_DEFINITION).
    ("constant-redeclared-elsewhere", f"TASK_CYCLE_ARTIFACTS = ({_ARTIFACT_TUPLE_SRC})\n"),
]
TASK_ARTIFACT_NON_PROBES = [
    # The detector matches string EQUALITY, never containment: the dev and sweep
    # prompts name the artifact inside a sentence, and flagging prose is how a
    # tripwire gets allowlisted into meaninglessness.
    *(
        (
            f"prompt-prose:{name}",
            f'PROMPT = "Write your verdict to tasks/<id>/{name}, then stop."\n',
        )
        for name in TASK_CYCLE_ARTIFACTS
    ),
    *(
        (f"docstring-prose:{name}", f'def f():\n    """Reads {name} beside it."""\n    return 1\n')
        for name in TASK_CYCLE_ARTIFACTS
    ),
    # A different artifact in the same directory: the guard's claim is about the
    # SHARED list, not about every filename a task dir holds. `heartbeat.json` and
    # `messages.json` are real siblings that stay outside it (see the constant).
    ("sibling-artifact", 'p = task_dir / "prompt.txt"\n'),
    ("adapter-owned-sibling", 'p = task_dir / "heartbeat.json"\n'),
    # The sanctioned spelling everywhere: iterate the constant.
    (
        "iterating-the-constant",
        "for artifact in TASK_CYCLE_ARTIFACTS:\n    (task_dir / artifact).unlink(missing_ok=True)\n",
    ),
]


def test_artifact_probe_tables_are_not_empty():
    """The tables above are derived from `TASK_CYCLE_ARTIFACTS` by iteration, which
    is what stops a shrunk constant erroring this module's collection — but the same
    derivation would quietly EMPTY a parametrized table, and an empty parametrize
    passes for exactly the reason an empty scan does. This is that floor, stated as
    a requirement rather than left to an `IndexError` nobody would read as one."""
    assert len(TASK_CYCLE_ARTIFACTS) >= 2, (
        "TASK_CYCLE_ARTIFACTS is down to "
        f"{list(TASK_CYCLE_ARTIFACTS)}; the scope cases below need one allowlisted "
        "name and one refused name to tell a name-scoped exemption from a file-wide one"
    )
    assert TASK_ARTIFACT_PROBES and TASK_ARTIFACT_NON_PROBES and TASK_ARTIFACT_SCOPE_CASES


@pytest.mark.parametrize(
    ("label", "source"), TASK_ARTIFACT_PROBES, ids=[p[0] for p in TASK_ARTIFACT_PROBES]
)
def test_task_artifact_detector_flags_every_literal_spelling(label, source):
    """Each way of re-introducing a literal produces a `taskartifact` finding, driven
    through the same `_scan_source` the real scan uses."""
    found = [f for f in _scan_source(source, "sweep.py") if f[0] == "taskartifact"]
    assert found, f"the {label!r} spelling produced no `taskartifact` finding:\n{source}"


@pytest.mark.parametrize(
    ("label", "source"), TASK_ARTIFACT_NON_PROBES, ids=[p[0] for p in TASK_ARTIFACT_NON_PROBES]
)
def test_task_artifact_detector_stays_silent_on_lookalikes(label, source):
    """The complement: prose that CONTAINS the name, a docstring, a sibling
    filename, and the sanctioned loop over the constant are all silent."""
    found = [f for f in _scan_source(source, "sweep.py") if f[0] == "taskartifact"]
    assert not found, f"the {label!r} shape was flagged; it is not a copied list:\n{source}"


# The artifact exemption's scoping, as rows: `(rel, source, is_offender)`. On the
# real tree a file-scoped allowlist and this position-and-name-scoped one are
# indistinguishable — generic.py's single literal is the only finding — so only
# synthetic sources can tell them apart, and only they carry the drift that does not
# exist yet. Built by iterating the allowlist and the constant, so a rename cannot
# leave a row grading a name nothing declares.
_ALLOWED_IN_GENERIC = TASK_ARTIFACT_LITERAL_ALLOW["adapters/generic.py"]["_result_path"]
TASK_ARTIFACT_SCOPE_CASES = [
    # The sanctioned single-name read: one artifact, named because the question is
    # about that one artifact — and named INSIDE the one function that asks it.
    *(
        (
            f"generic-result-path:{name}",
            "adapters/generic.py",
            f'def _result_path(self, task_id):\n    return self.tasks_dir / task_id / "{name}"\n',
            False,
        )
        for name in sorted(_ALLOWED_IN_GENERIC)
    ),
    # …which buys that file NOTHING about the other name. This is the case a
    # file-wide allowlist drops on the path alone — and it is the exact drift the
    # extraction removed.
    *(
        (
            f"generic-other-name:{name}",
            "adapters/generic.py",
            f'def _result_path(self, task_id):\n    (task_dir / "{name}").unlink(missing_ok=True)\n',
            True,
        )
        for name in TASK_CYCLE_ARTIFACTS
        if name not in _ALLOWED_IN_GENERIC
    ),
    # …and it buys no OTHER FUNCTION of that file the allowlisted name either. A
    # file-keyed allowlist waves this through on the path alone, which is what the
    # allowlist's comment claimed was already impossible and was not.
    *(
        (
            f"generic-other-function:{name}",
            "adapters/generic.py",
            f'def start_session(self, spec):\n    (task_dir / "{name}").unlink(missing_ok=True)\n',
            True,
        )
        for name in sorted(_ALLOWED_IN_GENERIC)
    ),
    # A module-level literal in the allowlisted file has no enclosing function at
    # all, so it cannot inherit a function-keyed exemption.
    *(
        (f"generic-module-level:{name}", "adapters/generic.py", f'STALE = "{name}"\n', True)
        for name in sorted(_ALLOWED_IN_GENERIC)
    ),
    # The twin adapter has no entry at all, so even the allowlisted NAME is refused
    # there: nothing in it answers a single-artifact question.
    *(
        (
            f"opencode-literal:{name}",
            "adapters/opencode_http.py",
            f'def _result_path(self, task_id):\n    return self.tasks_dir / task_id / "{name}"\n',
            True,
        )
        for name in sorted(_ALLOWED_IN_GENERIC)
    ),
    # journal.py's own definition is not a copy — skipped by POSITION, so it needs
    # no allowlist entry and cannot cover a literal elsewhere in the file.
    (
        "journal-definition",
        "journal.py",
        f"TASK_CYCLE_ARTIFACTS: tuple[str, ...] = ({_ARTIFACT_TUPLE_SRC})\n",
        False,
    ),
    *(
        (
            f"journal-bare-literal-beside-it:{name}",
            "journal.py",
            f"TASK_CYCLE_ARTIFACTS: tuple[str, ...] = ({_ARTIFACT_TUPLE_SRC})\n"
            f'STALE = "{name}"\n',
            True,
        )
        for name in TASK_CYCLE_ARTIFACTS
    ),
]


@pytest.mark.parametrize(
    ("label", "rel", "source", "is_offender"),
    TASK_ARTIFACT_SCOPE_CASES,
    ids=[c[0] for c in TASK_ARTIFACT_SCOPE_CASES],
)
def test_task_artifact_allowlist_is_scoped_by_position_and_name(label, rel, source, is_offender):
    """Being allowlisted buys a file's ONE declared function the artifact NAMES it
    declares, and nothing wider. Without this, `TASK_ARTIFACT_LITERAL_ALLOW` could go
    back to a set of paths — or to a file -> names map — and every assertion in this
    file would stay green."""
    findings = [f for f in _scan_source(source, rel) if f[0] == "taskartifact"]
    offenders = _task_artifact_offenders(findings)
    assert bool(offenders) is is_offender, (
        f"an artifact literal in {rel} here should "
        f"{'be refused' if is_offender else 'be allowed'}:\n{source}"
    )


# The task-id detector's probe matrix. Today's tree has exactly one `taskid`
# finding — the chokepoint's own return — so the tree-wide guard would stay green
# with the composition branches deleted; only these rows redden.
SESSION_TASK_ID_PROBES = [
    ("fstring-assignment", 'task_id = f"{task.story_key}-dev-{task.attempt}"\n'),
    ("concat-in-keyword", 'spec = SessionSpec(task_id=story + "-review-1", prompt=p)\n'),
    ("percent-format", 'task_id = "%s-dev-%d" % (key, seq)\n'),
    ("str-format", 'task_id = "{}-dev-1".format(key)\n'),
    ("bare-literal-keyword", 'spec = SessionSpec(task_id="triage-1", prompt=p)\n'),
    ("annotated-assignment", 'task_id: str = f"{key}-sweep-1"\n'),
    # A helper named for what it returns, in both the bare and the wrapped shape —
    # the wrapped one is how a fifth mint copied from the chokepoint would look.
    ("returned-from-task_id_fn", 'def _sweep_task_id(key):\n    return f"{key}-sweep"\n'),
    (
        "returned-through-sanitizer",
        'def _sweep_task_id(key):\n    return safe_segment(f"{key}-sweep")\n',
    ),
    # Both branches of a conditional are the same value position.
    ("conditional-branch", 'task_id = base if base else f"{key}-dev-1"\n'),
    # The chokepoint's own `return safe_segment(f"…")` copied into a BINDING and into
    # a KEYWORD — the most likely fifth mint, because it is the sanctioned line moved
    # rather than a new idea, and the one that silently drops the `-g<N>` re-arm
    # suffix (#705). Both were silent before the binding and keyword legs descended
    # through call arguments.
    ("binding-wrapped-in-sanitizer", 'task_id = safe_segment(f"{key}-dev-1")\n'),
    (
        "keyword-wrapped-in-sanitizer",
        'spec = SessionSpec(task_id=safe_segment(f"{key}-dev-1"), prompt=p)\n',
    ),
    # …and one level further in, since a wrapper can nest.
    ("binding-wrapped-twice", 'task_id = safe_segment(str(f"{key}-dev-1"))\n'),
]
SESSION_TASK_ID_NON_PROBES = [
    # The sanctioned call, and the three FORWARD shapes. A forward is not a mint,
    # and this is the distinction the whole detector rests on.
    ("chokepoint-call", 'task_id = _session_task_id(key, "dev", 1, gen)\n'),
    ("forward-attribute", "handle = SessionHandle(task_id=spec.task_id, native_id=w)\n"),
    ("forward-coerced", 'task_id = str(entry.get("task_id", ""))\n'),
    ("forward-name", "handle = SessionHandle(task_id=task_id, native_id=w)\n"),
    # The parts handed TO the chokepoint are not the id. The binding leg DOES descend
    # into call arguments now, so this row is what makes the depth rule load-bearing:
    # a bare literal is a mint only at depth 0, or the `"dev"` in every sanctioned
    # mint site becomes a finding.
    ("chokepoint-call-with-literal-part", 'task_id = _session_task_id(k, "dev", n, gen)\n'),
    (
        "chokepoint-keyword-with-literal-part",
        'spec = SessionSpec(task_id=_session_task_id(k, "dev", n, gen), prompt=p)\n',
    ),
    # The same rule is what keeps the env read silent — `events.py` and both hook
    # scripts spell exactly this, and the variable name is a `task_id` binding.
    ("env-read", 'task_id = os.environ.get("BMAD_LOOP_TASK_ID")\n'),
    ("env-read-with-default", 'task_id = os.environ.get("BMAD_LOOP_TASK_ID", "probe")\n'),
    # The shapes the detector deliberately does not reach, pinned as rows so the
    # boundary is executed rather than only described in the `NOT COVERED` comment.
    ("intermediate-variable", 'tid = f"{key}-dev-1"\nspec = SessionSpec(task_id=tid)\n'),
    ("join-composition", 'task_id = "-".join([key, "dev", "1"])\n'),
    ("percent-against-a-name", "task_id = fmt % (key, seq)\n"),
    # A composition bound to something else entirely — the detector is scoped to the
    # `task_id` positions, not to f-strings at large.
    ("composition-elsewhere", 'log_name = f"{task_id}.log"\n'),
    # A *task_id* function that FORWARDS: its returned literal-keyed subscript is
    # not a string Constant in a value position (`tui.data.active_task_id`).
    (
        "task_id_fn-forwards",
        'def active_task_id(entries):\n    return str(entries[-1]["task_id"])\n',
    ),
    # Prose is a docstring Expr, never a binding or a return value.
    (
        "prose-in-docstring",
        'def f():\n    """Ids look like task_id = f\'{key}-dev-1\'."""\n    return 1\n',
    ),
]


@pytest.mark.parametrize(
    ("label", "source"), SESSION_TASK_ID_PROBES, ids=[p[0] for p in SESSION_TASK_ID_PROBES]
)
def test_session_task_id_detector_flags_every_mint_shape(label, source):
    """Each spelling of a hand-minted id produces a `taskid` finding. `sweep.py` is
    an unsanctioned file, so a finding here is also an offender."""
    found = [f for f in _scan_source(source, "sweep.py") if f[0] == "taskid"]
    assert found, f"the {label!r} shape produced no `taskid` finding:\n{source}"


@pytest.mark.parametrize(
    ("label", "source"),
    SESSION_TASK_ID_NON_PROBES,
    ids=[p[0] for p in SESSION_TASK_ID_NON_PROBES],
)
def test_session_task_id_detector_stays_silent_on_forwards(label, source):
    """The complement: the chokepoint call, the three forward shapes, the literal
    PARTS handed to the chokepoint, the environment read every hook script uses, a
    composition bound elsewhere, and prose are all silent — a guard that flags
    forwards would be allowlisted away within a week.

    The last three rows are the DISCLOSED gaps rather than desired silences:
    an intermediate variable, `str.join`, and `%` against a Name-bound format
    string. They are here so the boundary is executed and cannot drift into a
    coverage claim the detector does not make."""
    found = [f for f in _scan_source(source, "sweep.py") if f[0] == "taskid"]
    assert not found, f"the {label!r} shape was flagged; it is not a mint:\n{source}"


# The task-id exemption's scoping, as rows: `(rel, source, is_offender)`.
SESSION_TASK_ID_SCOPE_CASES = [
    # The real chokepoint, in the shape it ships.
    (
        "sanctioned-chokepoint",
        "engine.py",
        "def _session_task_id(story_key, part, seq, generation):\n"
        '    gen = f"-g{generation}" if generation > 0 else ""\n'
        '    return safe_segment(f"{story_key}-{part}-{seq}{gen}")\n',
        False,
    ),
    # Being engine.py is NOT enough: it already binds `task_id` three times, so a
    # file-wide exemption would leave the invariant unguarded exactly where a fifth
    # mint would be written.
    (
        "engine-other-function",
        "engine.py",
        "def _run_sweep(self, task):\n"
        '    task_id = f"{task.story_key}-sweep-{task.attempt}"\n'
        "    return task_id\n",
        True,
    ),
    # The name does not travel: the same function grown in another module cannot
    # sanction itself, which is why the sanction pairs the function with the FILE.
    (
        "chokepoint-name-in-another-file",
        "sweep.py",
        "def _session_task_id(story_key, part, seq, generation):\n"
        '    return safe_segment(f"{story_key}-{part}-{seq}")\n',
        True,
    ),
    # The measured ablation: resolve.py's mint respelled as an f-string.
    (
        "resolve-respelled",
        "resolve.py",
        'spec = SessionSpec(task_id=f"{story_key}-resolve-1", prompt=p)\n',
        True,
    ),
    # …and its real spelling stays silent there.
    (
        "resolve-real-spelling",
        "resolve.py",
        'spec = SessionSpec(task_id=_session_task_id(story_key, "resolve", 1, generation), prompt=p)\n',
        False,
    ),
    # A nested def inside the chokepoint is still inside it (`ast.walk` descends),
    # matching how the verify sanctions treat closures.
    (
        "nested-inside-chokepoint",
        "engine.py",
        "def _session_task_id(story_key, part, seq, generation):\n"
        "    def compose():\n"
        '        return f"{story_key}-{part}-{seq}"\n'
        "    return safe_segment(compose())\n",
        False,
    ),
    # A decorator and a default argument are evaluated where the chokepoint is
    # DEFINED, not inside its body, so a mint parked in one is a fifth mint wearing
    # the chokepoint's name. The body's own return stays sanctioned in both rows, so
    # the offence is the decorator/default alone. ABLATION: restore
    # `for inner in ast.walk(fn)` in `sanctioned_task_id_nodes` and both rows FAIL.
    (
        "decorator-bypass",
        "engine.py",
        '@register(SessionSpec(task_id=f"{story_key}-dev-1", prompt=p))\n'
        "def _session_task_id(story_key, part, seq, generation):\n"
        "    return safe_segment(story_key)\n",
        True,
    ),
    (
        "default-arg-bypass",
        "engine.py",
        "def _session_task_id(\n"
        '    story_key, part, seq, generation, *, spec=SessionSpec(task_id=f"{k}-dev-1", prompt=p)\n'
        "):\n"
        "    return safe_segment(story_key)\n",
        True,
    ),
]


@pytest.mark.parametrize(
    ("label", "rel", "source", "is_offender"),
    SESSION_TASK_ID_SCOPE_CASES,
    ids=[c[0] for c in SESSION_TASK_ID_SCOPE_CASES],
)
def test_session_task_id_exemption_is_scoped_to_the_chokepoint(label, rel, source, is_offender):
    """Being engine.py buys the file its `_session_task_id` body and nothing wider.
    Without this, the sanction could go back to a bare file set and every assertion
    here would stay green — the difference only shows up on a fifth mint, which is
    the only kind a tripwire is for."""
    findings = [f for f in _scan_source(source, rel) if f[0] == "taskid"]
    offenders = _session_task_id_offenders(findings)
    assert bool(offenders) is is_offender, (
        f"a composed task id in {rel} here should "
        f"{'be refused' if is_offender else 'be allowed'}:\n{source}"
    )


# The re-arm caller detector's probe matrix. Today's tree has exactly two `rearmcall`
# findings and BOTH are gated, so the tree-wide guard's `ungated == []` half would stay
# green with the gate logic deleted, or with its line-position check dropped — only
# these rows redden. Each is driven through the real `_scan_source`.
REARM_CALL_PROBES = [
    # (label, source, expected enclosing function, expected `gated`)
    (
        "qualified-call-behind-the-gate",
        "def cmd_resolve(args):\n"
        "    live = runs.engine_liveness(run_dir)\n"
        '    if live == "alive":\n'
        "        return\n"
        "    runs.rearm_escalation(run_dir, story_key)\n",
        "cmd_resolve",
        True,
    ),
    # The TUI's spelling, which reaches `runs.liveness` rather than `engine_liveness`.
    # This is the row that makes the substring match load-bearing rather than lax.
    (
        "tui-spelling-of-the-gate",
        "def _do_rearm(self, run_id, run_dir):\n"
        "    if self._resolve_blocked_by_liveness(run_id, run_dir):\n"
        "        return\n"
        "    runs.rearm_escalation(run_dir, story_key)\n",
        "_do_rearm",
        True,
    ),
    # A rename-on-import third caller — the alias resolver's first ordinary shape.
    (
        "renamed-call-from-import",
        "from .runs import rearm_escalation as rearm\n"
        "def cmd_something(args):\n"
        "    if runs.engine_liveness(run_dir):\n"
        "        return\n"
        "    rearm(run_dir, story_key)\n",
        "cmd_something",
        True,
    ),
    # Assignment aliases are just as callable as import aliases.
    (
        "assigned-call-alias",
        "handler = runs.rearm_escalation\n"
        "def cmd_something(args):\n"
        "    if runs.engine_liveness(run_dir):\n"
        "        return\n"
        "    handler(run_dir, story_key)\n",
        "cmd_something",
        True,
    ),
    # Merely reading liveness is not a gate when the result is ignored.
    (
        "ignored-liveness-result",
        "def cmd_something(args):\n"
        "    live = runs.engine_liveness(run_dir)\n"
        "    runs.rearm_escalation(run_dir, story_key)\n",
        "cmd_something",
        False,
    ),
    # Nor is a guard hidden in a closure that the caller never invokes.
    (
        "uninvoked-nested-guard",
        "def cmd_something(args):\n"
        "    def guard():\n"
        "        if runs.engine_liveness(run_dir):\n"
        "            return\n"
        "    runs.rearm_escalation(run_dir, story_key)\n",
        "cmd_something",
        False,
    ),
    # An ungated third caller: the defect this guard exists for.
    (
        "no-gate-at-all",
        "def cmd_something(args):\n    runs.rearm_escalation(run_dir, story_key)\n",
        "cmd_something",
        False,
    ),
    # The gate present but BELOW the call, which is not a gate. Without the line
    # comparison in `_consults_liveness_before` this row reads as `True` and the whole
    # position rule is unheld.
    (
        "gate-below-the-call",
        "def cmd_something(args):\n"
        "    runs.rearm_escalation(run_dir, story_key)\n"
        "    live = runs.engine_liveness(run_dir)\n",
        "cmd_something",
        False,
    ),
]
REARM_CALL_NON_PROBES = [
    # The definition is not a call and needs no exemption.
    ("the-definition", "def rearm_escalation(run_dir, story_key=None):\n    return None\n"),
    # A different function whose name merely starts the same way.
    ("similar-name", "def f():\n    runs.rearm_escalation_notice(run_dir)\n"),
    # A mere mention as a value, not a call.
    ("reference-not-a-call", "def f():\n    handler = runs.rearm_escalation\n"),
]


@pytest.mark.parametrize(
    "label,source,fn,gated", REARM_CALL_PROBES, ids=[p[0] for p in REARM_CALL_PROBES]
)
def test_rearm_call_detector_reports_the_site_and_its_gate(label, source, fn, gated):
    """Each call shape is found, attributed to its enclosing function, and graded on
    whether an earlier liveness guard blocks fall-through. `cli.py` is passed because
    nothing in this detector is file-scoped — the enumeration lives in the tree-wide
    assertion, not here."""
    found = [f for f in _scan_source(source, "cli.py") if f[0] == "rearmcall"]
    assert len(found) == 1, f"the {label!r} shape produced {len(found)} findings:\n{source}"
    assert found[0][4] == (fn, gated), f"the {label!r} shape graded as {found[0][4]}"


def _rearm_callsite_counts(findings) -> Counter:
    """Call-site multiplicity, not just distinct enclosing functions."""
    return Counter((rel, fn) for _, rel, _, _, (fn, _) in findings)


def test_rearm_callsite_count_does_not_hide_a_second_call_in_one_function():
    source = (
        "def cmd_resolve(args):\n"
        "    if runs.engine_liveness(run_dir):\n"
        "        return\n"
        "    runs.rearm_escalation(run_dir, first)\n"
        "    runs.rearm_escalation(run_dir, second)\n"
    )
    found = [f for f in _scan_source(source, "cli.py") if f[0] == "rearmcall"]
    assert _rearm_callsite_counts(found) == Counter({("cli.py", "cmd_resolve"): 2})


@pytest.mark.parametrize(
    "label,source", REARM_CALL_NON_PROBES, ids=[p[0] for p in REARM_CALL_NON_PROBES]
)
def test_rearm_call_detector_stays_silent_on_non_calls(label, source):
    """A definition, a reference and a similarly-named neighbour are not call sites. A
    detector that flagged these would push noise into the tree-wide enumeration, which
    is an equality assertion and so fails on a false positive as loudly as on a miss."""
    found = [f for f in _scan_source(source, "cli.py") if f[0] == "rearmcall"]
    assert not found, f"the {label!r} shape produced a `rearmcall` finding:\n{source}"


# The refusal-helper detector's matrix: `(label, source, expected def names)`. The
# surface is DEFINITIONS — a new `_refuse_*`/`_reject_*` helper is a new refusal
# behavior that must land with an inventory row and its own test — so calls,
# lookalike prefixes and prose must all stay silent or the inventory fills with
# noise it cannot force a decision about.
REFUSAL_DEF_PROBES = [
    (
        "plain-def",
        "def _refuse_live_session(project, run_id, verb):\n    return None\n",
        {"_refuse_live_session"},
    ),
    (
        "reject-spelling",
        "def _reject_bad_run_id(run_id):\n    return None\n",
        {"_reject_bad_run_id"},
    ),
    (
        "async-def",
        "async def _refuse_slow_probe(target):\n    return None\n",
        {"_refuse_slow_probe"},
    ),
    # A method is a definition too — `engine.Engine._refuse_gated_story` is one of
    # the nine rows the real tree declares.
    (
        "method-def",
        "class Engine:\n    def _refuse_gated_story(self, story_key):\n        return None\n",
        {"_refuse_gated_story"},
    ),
]
REFUSAL_DEF_NON_PROBES = [
    # A CALL is not a definition: call sites belong to each helper's own tests, and
    # flagging them would report every use as a new refusal behavior.
    ("call-not-a-def", 'def f():\n    _refuse_live_session(project, run_id, "stop")\n'),
    # The prefix is `_refuse_`/`_reject_` WITH the trailing underscore: a name that
    # merely starts `_refus` is not claiming to be a refusal helper.
    ("similar-prefix", "def _refusal_note(story_key):\n    return None\n"),
    # …and a public spelling makes no `_refuse_*` claim either.
    ("public-spelling", "def refuse_everything():\n    return None\n"),
    # Prose naming a helper is a Constant, not a def.
    ("prose", 'def f():\n    """Calls _refuse_live_session first."""\n    return 1\n'),
]


@pytest.mark.parametrize(
    ("label", "source", "expected"), REFUSAL_DEF_PROBES, ids=[p[0] for p in REFUSAL_DEF_PROBES]
)
def test_refusal_def_detector_flags_every_definition_shape(label, source, expected):
    """Each definition shape is found and reported by name. `runs.py` is passed
    because nothing in this detector is file-scoped — the enumeration lives in the
    tree-wide inventory test, not here.

    Ablation: delete the `refusaldef` emit and every row here reddens."""
    found = {f[4] for f in _scan_source(source, "runs.py") if f[0] == "refusaldef"}
    assert found == expected, f"the {label!r} shape resolved to {sorted(found)}:\n{source}"


@pytest.mark.parametrize(
    ("label", "source"), REFUSAL_DEF_NON_PROBES, ids=[p[0] for p in REFUSAL_DEF_NON_PROBES]
)
def test_refusal_def_detector_stays_silent_on_lookalikes(label, source):
    """A call, a lookalike prefix, a public spelling and prose are not refusal-helper
    definitions. The inventory is an equality assertion, so a false positive fails as
    loudly as a miss.

    Ablation: widen the emit's prefix match to `_refus` and the similar-prefix row
    reddens."""
    found = [f for f in _scan_source(source, "runs.py") if f[0] == "refusaldef"]
    assert not found, f"the {label!r} shape produced a `refusaldef` finding:\n{source}"


# The #414-family call detector's matrix: `(label, source, expected enclosing
# function)`. Both spellings of the refusal are probed — the `bmadconfig` predicate
# and the rc-returning CLI wrapper — because a new surface can reach the pair
# through either, and the `96aa09a9` site (cmd_resolve's pre-session arm) arrived
# through the wrapper.
ISOLATION_CALL_PROBES = [
    (
        "qualified-predicate-call",
        "def cmd_validate(args):\n"
        "    conflict = bmadconfig.worktree_isolation_conflict(paths, pol.scm.isolation)\n",
        "cmd_validate",
    ),
    (
        "bare-wrapper-call",
        "def cmd_run(args):\n"
        "    if (rc := _reject_isolation_conflict(paths, pol)) is not None:\n"
        "        return rc\n",
        "cmd_run",
    ),
    # A rename-on-import and an assignment alias are just as callable — the
    # `_call_aliases` shapes, one per guarded name.
    (
        "renamed-predicate-import",
        "from .bmadconfig import worktree_isolation_conflict as conflict_for\n"
        "def f(args):\n"
        "    conflict_for(paths, isolation)\n",
        "f",
    ),
    (
        "assigned-wrapper-alias",
        "check = _reject_isolation_conflict\ndef f(args):\n    check(paths, pol)\n",
        "f",
    ),
]
ISOLATION_CALL_NON_PROBES = [
    # The definitions are not calls. The wrapper's own predicate call is a real
    # finding on today's tree — `("cli.py", "_reject_isolation_conflict")` is a row
    # of the declared Counter — so the bodies here are stubs on purpose.
    (
        "predicate-definition",
        "def worktree_isolation_conflict(paths, isolation):\n    return None\n",
    ),
    ("wrapper-definition", "def _reject_isolation_conflict(paths, pol):\n    return None\n"),
    # A different function whose name merely embeds the guarded one.
    ("similar-name", "def f():\n    worktree_isolation_conflicts(paths)\n"),
    ("reference-not-a-call", "def f():\n    handler = bmadconfig.worktree_isolation_conflict\n"),
    (
        "prose",
        'def f():\n    """bmadconfig.worktree_isolation_conflict(paths, mode) decides."""\n'
        "    return 1\n",
    ),
]


@pytest.mark.parametrize(
    ("label", "source", "fn"), ISOLATION_CALL_PROBES, ids=[p[0] for p in ISOLATION_CALL_PROBES]
)
def test_isolation_call_detector_reports_the_site(label, source, fn):
    """Each call shape is found and attributed to its enclosing function — the key
    the declared Counter is built on. `cli.py` is passed because nothing in this
    detector is file-scoped.

    Ablation: delete the `isolationcall` emit and every row here reddens."""
    found = [f for f in _scan_source(source, "cli.py") if f[0] == "isolationcall"]
    assert len(found) == 1, f"the {label!r} shape produced {len(found)} findings:\n{source}"
    assert found[0][4] == fn, f"the {label!r} shape attributed to {found[0][4]!r}"


@pytest.mark.parametrize(
    ("label", "source"), ISOLATION_CALL_NON_PROBES, ids=[p[0] for p in ISOLATION_CALL_NON_PROBES]
)
def test_isolation_call_detector_stays_silent_on_non_calls(label, source):
    """Definitions, a similarly-named neighbour, a bare reference and prose are not
    call sites. The tree-wide assertion is a Counter equality, so a false positive
    fails as loudly as a miss.

    Ablation: relax `_names_guarded_verify_call`'s name equality to a substring
    match and the similar-name row reddens."""
    found = [f for f in _scan_source(source, "cli.py") if f[0] == "isolationcall"]
    assert not found, f"the {label!r} shape produced an `isolationcall` finding:\n{source}"


def _isolation_callsite_counts(findings) -> Counter:
    """Call-site multiplicity, not just distinct enclosing functions — the
    `_rearm_callsite_counts` idiom, and load-bearing on the real tree:
    `cli.cmd_resolve` legitimately calls the wrapper twice."""
    return Counter((rel, fn) for _, rel, _, _, fn in findings)


def test_isolation_callsite_count_does_not_hide_a_second_call_in_one_function():
    """Ablation: collapse `_isolation_callsite_counts` to a set of keys and this
    reddens — `cmd_resolve` would then absorb a third call silently."""
    source = (
        "def cmd_resolve(args):\n"
        "    if (rc := _reject_isolation_conflict(paths, pol)) is not None:\n"
        "        return rc\n"
        "    if (rc := _reject_isolation_conflict(paths, pol)) is not None:\n"
        "        return rc\n"
    )
    found = [f for f in _scan_source(source, "cli.py") if f[0] == "isolationcall"]
    assert _isolation_callsite_counts(found) == Counter({("cli.py", "cmd_resolve"): 2})


# The journal detector's probe matrix, as `(label, source, expected)` where
# `expected` is the exact set of field names the scan must extract — `None` standing
# for an unresolvable splat. Asserting the SET rather than "something was found" is
# what makes a partial splat resolution fail here instead of quietly under-reporting.
JOURNAL_FIELD_PROBES = [
    # The four receiver spellings in the tree.
    ("self-journal", 'self.journal.append("k", story_key=s, patch=p)\n', {"story_key", "patch"}),
    ("bare-journal", 'journal.append("k", branch=b)\n', {"branch"}),
    ("private-journal", "self._journal.append(kind, plugin=name)\n", {"plugin"}),
    # The constructor-inline spelling `Journal(run_dir).append(...)` — three live
    # sites in runs.py use it, and the receiver is an ast.Call, so the named-handle
    # match alone left them (and their kinds and fields) entirely unscanned.
    (
        "constructor-inline-receiver",
        'def f(run_dir):\n    Journal(run_dir).append("k", pid=1)\n',
        {"pid"},
    ),
    # A splat resolved through the literal stores that build it, in both store
    # shapes and across the conditional-dict form `engine._run_inner` uses.
    (
        "splat-dict-literal",
        "def f(self):\n"
        '    fields = {"story_key": k, "checkpoint": "story"}\n'
        '    self.journal.append("k", **fields)\n',
        {"story_key", "checkpoint"},
    ),
    (
        "splat-subscript-store",
        "def f(self):\n"
        '    fields = {"story_key": k}\n'
        '    fields["reason"] = "graceful-stop"\n'
        '    self.journal.append("k", **fields)\n',
        {"story_key", "reason"},
    ),
    (
        "splat-conditional-dict",
        "def f(self):\n"
        '    extras = {"via": stop.via} if stop.via is not None else {}\n'
        '    self.journal.append("k", **extras)\n',
        {"via"},
    ),
    # Explicit keywords and a splat on the SAME call: both halves are collected, so
    # a resolvable splat does not shadow its siblings and vice versa.
    (
        "splat-mixed-with-explicit",
        'def f(self):\n    d = {"a": 1}\n    self.journal.append("k", b=2, **d)\n',
        {"a", "b"},
    ),
    # The unresolvable shapes, each of which must fail LOUD rather than resolve to
    # the keys seen so far — a partially-resolved splat is a silent hole.
    (
        "splat-computed-key",
        'def f(self):\n    d = {}\n    d[f"{kind}_path"] = p\n    self.journal.append("k", **d)\n',
        {None},
    ),
    (
        "splat-update-mutation",
        'def f(self):\n    d = {"a": 1}\n    d.update(b=2)\n    self.journal.append("k", **d)\n',
        {None},
    ),
    (
        "splat-augmented-store",
        'def f(self):\n    d = {"a": 1}\n    d += other\n    self.journal.append("k", **d)\n',
        {None},
    ),
    (
        "splat-nested-splat",
        'def f(self):\n    d = {"a": 1, **other}\n    self.journal.append("k", **d)\n',
        {None},
    ),
    (
        "splat-from-call",
        'def f(self):\n    self.journal.append("k", **self._extras(result))\n',
        {None},
    ),
    (
        "splat-parameter-forwarder",
        "def _log(self, kind, **fields):\n    self._journal.append(kind, **fields)\n",
        {None},
    ),
    ("splat-at-module-level", 'journal.append("k", **fields)\n', {None}),
    # The fourth direction the resolver has to fail closed in: a SECOND NAME bound to
    # the same dict, mutated through the alias. Every store the resolver looks for is
    # spelled on `alias`, so the tracked name resolves to `{"a"}` and the new field
    # is invisible — a partially-resolved splat reading as green, which is precisely
    # what the other three rows exist to prevent.
    (
        "splat-aliased-then-mutated",
        "def f(self):\n"
        '    fields = {"a": 1}\n'
        "    alias = fields\n"
        '    alias["customer_email"] = 2\n'
        '    self.journal.append("k", **fields)\n',
        {None},
    ),
    # …and a plain READ of the dict is not an alias, so it still resolves.
    (
        "splat-read-not-aliased",
        'def f(self):\n    fields = {"a": 1}\n    n = len(fields)\n'
        '    self.journal.append("k", **fields)\n',
        {"a"},
    ),
]
# The forwarder leg, which needs its own `rel` because `JOURNAL_FORWARDERS` is keyed
# `(file, name)`: `(label, rel, source, expected)`. Without the declaration the plugin
# bus's four `self._log(...)` sites were a wall — the scan saw only the `.append`
# inside `_log`, which is an unresolvable splat, so `rc` and `blocking` reached the
# journal while sitting in neither routing set with the guard green.
JOURNAL_FORWARDER_PROBES = [
    (
        "declared-forwarder-call",
        "plugins/bus.py",
        'self._log("plugin-hook", plugin=lp.name, stage=hook.stage, rc=rc, blocking=True)\n',
        {"plugin", "stage", "rc", "blocking"},
    ),
    # The declaration is keyed by FILE as well as name: a `_log` in another module
    # forwards to something else entirely and must stay invisible.
    ("forwarder-name-in-another-file", "stories_engine.py", 'self._log("k", rc=rc)\n', set()),
    # …and it does not turn every call in the declared file into a journal write.
    ("other-call-in-forwarder-file", "plugins/bus.py", 'self._emit("k", rc=rc)\n', set()),
]


@pytest.mark.parametrize(
    ("label", "rel", "source", "expected"),
    JOURNAL_FORWARDER_PROBES,
    ids=[p[0] for p in JOURNAL_FORWARDER_PROBES],
)
def test_journal_forwarder_calls_enter_the_inventory(label, rel, source, expected):
    """A declared forwarder's CALL SITES are journal writes, so their explicit
    keywords are graded like any other producer's — and the declaration is scoped to
    the one file that owns the forwarder."""
    found = {f[4][0] for f in _scan_source(source, rel) if f[0] == "journalfield"}
    assert found == expected, f"the {label!r} shape resolved to {sorted(found, key=str)}:\n{source}"


JOURNAL_FIELD_NON_PROBES = [
    # `.append` on anything that is not a journal handle — the method name alone is
    # the most common in the language, so anchoring on the receiver is load-bearing.
    ("list-append", "results.append(SessionResult(status=s, stop_seen=True))\n"),
    ("attribute-list-append", "self.entries.append(dict(kind=k, story_key=s))\n"),
    # A constructor that merely ends in a `.append` is not a journal write unless
    # the constructed thing IS a Journal — the constructor arm is name-anchored
    # exactly like the handle arm.
    ("constructor-lookalike", 'NotAJournal(run_dir).append("k", pid=1)\n'),
    # A journal write with no fields at all produces nothing to route.
    ("kind-only", 'self.journal.append("run-start")\n'),
    # Prose naming the call is a Constant, not a Call.
    ("prose-in-docstring", 'def f():\n    """Calls journal.append(patch=p)."""\n    return 1\n'),
]


@pytest.mark.parametrize(
    ("label", "source", "expected"),
    JOURNAL_FIELD_PROBES,
    ids=[p[0] for p in JOURNAL_FIELD_PROBES],
)
def test_journal_field_detector_extracts_the_declared_names(label, source, expected):
    """The names (and the unresolvable-splat marker) the scan must extract from each
    producer shape. Deleting the splat resolver, or letting it return the keys it
    managed to see, reddens exactly the rows that describe that behaviour — which
    the tree-wide assertion cannot, since it is an absence."""
    found = {f[4][0] for f in _scan_source(source, "sweep.py") if f[0] == "journalfield"}
    assert found == expected, f"the {label!r} shape resolved to {sorted(found, key=str)}:\n{source}"


@pytest.mark.parametrize(
    ("label", "source"),
    JOURNAL_FIELD_NON_PROBES,
    ids=[p[0] for p in JOURNAL_FIELD_NON_PROBES],
)
def test_journal_field_detector_stays_silent_on_non_journal_appends(label, source):
    """The complement: `.append` on a list, on some other attribute, a kind-only
    journal write, and prose are all silent. Without this the detector could pass
    every row above by flagging every `.append` in the tree."""
    found = [f for f in _scan_source(source, "sweep.py") if f[0] == "journalfield"]
    assert not found, f"the {label!r} shape was flagged as a journal field:\n{source}"


# The journal offender filter's scoping, as rows:
# `(rel, fn, field, kind, is_offender)`. On the real tree every field is accounted
# for, so a filter that accepted EVERYTHING would look identical — only synthetic
# findings separate them.
JOURNAL_FIELD_SCOPE_CASES = [
    # The measured DW-82 ablation: a routed field renamed by its producer. `patch`
    # is routed (dropped); `patch_path` is nothing, and the dump leaks.
    ("routed-name", "recovery_flow.py", "_restore", "patch", "stale-restore", False),
    ("renamed-off-the-table", "recovery_flow.py", "_restore", "patch_path", "stale-restore", True),
    # A declared-benign name stays silent, and a name in neither set is refused
    # wherever it appears — the inventory is global, not per-file.
    ("declared-benign", "engine.py", "_run_inner", "attempt", "run-start", False),
    ("undeclared-new-field", "engine.py", "_run_inner", "customer_email", "run-start", True),
    ("undeclared-in-another-file", "sweep.py", "_triage", "customer_email", "sweep-start", True),
    # KIND-SCOPED routing, which a flattened by-name union got wrong in the dangerous
    # direction. `target` is aliased to a branch on exactly three merge kinds …
    ("kind-alias-on-its-own-kind", "worktree_flow.py", "_merge", "target", "unit-merged", False),
    # … and is NOT routed on a new kind that reuses the name. Flattened, this passed
    # while `_scrub_entry` handed the branch to `scrub_json` verbatim.
    ("kind-alias-on-a-new-kind", "worktree_flow.py", "_merge", "target", "unit-merge-failed", True),
    # … nor at a call whose kind the scan could not resolve: nothing there can prove
    # which kind it lands on, so the name is not routed by default.
    ("kind-alias-on-a-non-literal-kind", "worktree_flow.py", "_merge", "target", None, True),
    # The board-advance family carries a sprint STATUS under the same name, declared
    # benign per kind rather than by widening the by-name set.
    (
        "kind-benign-on-its-own-kind",
        "engine.py",
        "_advance_board",
        "target",
        "board-advance-carried",
        False,
    ),
    # …and that declaration does not travel to a kind outside the family either.
    (
        "kind-benign-on-another-kind",
        "engine.py",
        "_advance_board",
        "target",
        "board-advance-invented",
        True,
    ),
    # An unresolvable splat is refused unless its POSITION is a declared hole …
    ("undeclared-splat", "sweep.py", "_triage", None, "sweep-start", True),
    ("declared-splat-hole", "plugins/bus.py", "_log", None, None, False),
    # … and the declaration does not travel: the same function name in another
    # module, or another function in the same module, is still a hole.
    ("declared-hole-wrong-file", "stories_engine.py", "_log", None, None, True),
    ("declared-hole-wrong-function", "plugins/bus.py", "_dispatch", None, None, True),
]


@pytest.mark.parametrize(
    ("label", "rel", "fn", "field", "kind", "is_offender"),
    JOURNAL_FIELD_SCOPE_CASES,
    ids=[c[0] for c in JOURNAL_FIELD_SCOPE_CASES],
)
def test_journal_field_offenders_split_routed_benign_and_holes(
    label, rel, fn, field, kind, is_offender
):
    """The filter's decision, as rows: routed by name, routed on THIS kind, declared
    benign globally or on this kind, or an offender — and, for a splat, whether its
    `(file, function)` is a declared hole.

    Pins two scopings the real tree cannot show. `JOURNAL_SPLAT_ALLOW` is keyed by
    POSITION rather than by function name (no two of its four holes share a name),
    and kind-scoped routing is keyed by KIND rather than flattened by name (every
    `target` in the tree today sits on a kind that routes or declares it)."""
    offenders = _journal_field_offenders(
        [("journalfield", rel, 1, f"journal.append(k, {field}=v)", (field, fn, kind))]
    )
    assert bool(offenders) is is_offender, (
        f"{rel}::{fn} journalling {field!r} on kind {kind!r} should "
        f"{'be refused' if is_offender else 'be allowed'}"
    )


# The dynamic-kind declaration's scoping, as rows: `(rel, fn, is_offender)`.
JOURNAL_KIND_SCOPE_CASES = [
    ("declared-position", "plugins/bus.py", "_log", False),
    ("declared-position-recovery", "recovery_flow.py", "prune_preserve_refs", False),
    # The declaration does not travel by function name, nor by file.
    ("undeclared-function-same-file", "plugins/bus.py", "_dispatch", True),
    ("declared-name-another-file", "stories_engine.py", "_log", True),
    ("undeclared-position", "sweep.py", "_triage", True),
]


@pytest.mark.parametrize(
    ("label", "rel", "fn", "is_offender"),
    JOURNAL_KIND_SCOPE_CASES,
    ids=[c[0] for c in JOURNAL_KIND_SCOPE_CASES],
)
def test_journal_kind_declaration_is_scoped_by_position(label, rel, fn, is_offender):
    """A non-literal kind is waived at the exact `(file, function)` that declared
    itself, and nowhere else — the `JOURNAL_SPLAT_ALLOW` idiom, for the same reason:
    a site the scan cannot read must not read as clean because a same-named function
    elsewhere is allowed to be unreadable."""
    offenders = _journal_kind_offenders([("journalkind", rel, 1, "journal.append(kind)", fn)])
    assert bool(offenders) is is_offender, (
        f"a non-literal kind in {rel}::{fn} should "
        f"{'be refused' if is_offender else 'be allowed'}"
    )


# The count axis of the same declaration, as rows. Each case is a MUTATION of the
# declared population plus the drift it must report, written as deltas off
# `JOURNAL_DYNAMIC_KIND_ALLOW` rather than as literal counts, so a deliberate change
# to the declaration moves these rows with it instead of leaving a second hardcoded
# count behind — the defect this whole row family retires.
#
# The positions are DERIVED from the declaration for the same reason: naming one
# would be a hardcoded key, and subscripting it at import turns "delete the stale
# row", which is the remedy the tree-wide failure message hands out, into a
# collection error for every row in this file. `default` keeps import total under the
# declaredness sibling's "empty the declaration" ablation too.
_MULTI_WRITE_POSITION, _DECLARED_WRITES = max(
    JOURNAL_DYNAMIC_KIND_ALLOW.items(), key=lambda kv: kv[1], default=(("", ""), 0)
)
_OTHER_POSITION, _OTHER_DECLARED = min(
    ((pos, n) for pos, n in JOURNAL_DYNAMIC_KIND_ALLOW.items() if pos != _MULTI_WRITE_POSITION),
    key=lambda kv: kv[1],
    default=(("", ""), 0),
)


def test_journal_kind_count_cases_rest_on_a_multi_write_position():
    """The rows below mutate the declared position holding the MOST writes, derived
    from the declaration rather than named. This pins the premise that makes the
    derivation worth anything: some declared position holds two or more writes."""
    assert _DECLARED_WRITES >= 2, (
        "no declared dynamic-kind position holds 2+ writes any more, so the "
        "`write-removed` and `position-went-literal` cases below collapse into each "
        "other — both become measured 0 — and stop covering separate directions. "
        f"Declared: {dict(JOURNAL_DYNAMIC_KIND_ALLOW)}"
    )


JOURNAL_KIND_COUNT_CASES = [
    # The tree as declared: silent.
    ("as-declared", dict(JOURNAL_DYNAMIC_KIND_ALLOW), {}),
    # A write ADDED inside an already-declared position — the shape prose could not
    # hold, and the one the real-tree ablation exercises.
    (
        "write-added",
        {**JOURNAL_DYNAMIC_KIND_ALLOW, _MULTI_WRITE_POSITION: _DECLARED_WRITES + 1},
        {_MULTI_WRITE_POSITION: (_DECLARED_WRITES, _DECLARED_WRITES + 1)},
    ),
    # …and the other direction: a write REMOVED is drift too, not an improvement.
    (
        "write-removed",
        {**JOURNAL_DYNAMIC_KIND_ALLOW, _MULTI_WRITE_POSITION: _DECLARED_WRITES - 1},
        {_MULTI_WRITE_POSITION: (_DECLARED_WRITES, _DECLARED_WRITES - 1)},
    ),
    # Every write at the position gained a literal kind: the row is now a waiver for
    # nothing, and measured 0 is what says so.
    (
        "position-went-literal",
        {**JOURNAL_DYNAMIC_KIND_ALLOW, _MULTI_WRITE_POSITION: 0},
        {_MULTI_WRITE_POSITION: (_DECLARED_WRITES, 0)},
    ),
    # Two positions drifting at once: the report is a mapping, not a first-offender,
    # so both pairs come back and the tree-wide message has more than one line to
    # sort.
    (
        "two-positions-drift",
        {
            **JOURNAL_DYNAMIC_KIND_ALLOW,
            _MULTI_WRITE_POSITION: _DECLARED_WRITES + 1,
            _OTHER_POSITION: _OTHER_DECLARED + 2,
        },
        {
            _MULTI_WRITE_POSITION: (_DECLARED_WRITES, _DECLARED_WRITES + 1),
            _OTHER_POSITION: (_OTHER_DECLARED, _OTHER_DECLARED + 2),
        },
    ),
    # An UNDECLARED position is the declaredness sibling's business; this axis stays
    # silent rather than reporting one defect through two messages.
    (
        "undeclared-position",
        {**JOURNAL_DYNAMIC_KIND_ALLOW, ("sweep.py", "_triage"): 3},
        {},
    ),
]


@pytest.mark.parametrize(
    ("label", "population", "expected"),
    JOURNAL_KIND_COUNT_CASES,
    ids=[c[0] for c in JOURNAL_KIND_COUNT_CASES],
)
def test_journal_kind_count_drift_reports_both_directions(label, population, expected):
    """`_journal_kind_count_drift`'s decision, as rows — the mutations the real tree
    cannot show without editing `src/`.

    Anti-vacuity is NOT why these exist, and the absence idiom does not apply here:
    the tree-wide row grades declared positions, so `_journal_kind_count_drift([])`
    reports every one of them at `(declared, 0)` and a scan that stopped finding
    dynamic-kind writes reddens there rather than passing green. Reach is why. None
    of these mutations can arise on the real tree without editing `src/`, so each
    feeds a synthetic population and pins the helper's decision on both directions
    where the tree cannot show it."""
    findings = [
        ("journalkind", rel, 1, "journal.append(kind)", fn)
        for (rel, fn), count in population.items()
        for _ in range(count)
    ]
    assert _journal_kind_count_drift(findings) == expected, label


# The SPLAT axis of the same idiom: `(label, population, expected drift)` rows written
# as deltas off `JOURNAL_SPLAT_ALLOW` rather than as literal counts, so a deliberate
# change to the declaration moves these rows with it instead of leaving a second
# hardcoded count behind.
#
# The positions are DERIVED for the same reason as the dynamic-kind rows above:
# naming one would be a hardcoded key, and subscripting it at import turns "delete the
# stale row" — the remedy the tree-wide failure message hands out — into a collection
# error for every row in this file. `default` keeps import total even under an emptied
# declaration.
_SPLAT_MULTI_POSITION, _SPLAT_DECLARED = max(
    JOURNAL_SPLAT_ALLOW.items(), key=lambda kv: kv[1], default=(("", ""), 0)
)
_SPLAT_SINGLE_POSITION, _SPLAT_SINGLE_DECLARED = min(
    ((pos, n) for pos, n in JOURNAL_SPLAT_ALLOW.items() if pos != _SPLAT_MULTI_POSITION),
    key=lambda kv: kv[1],
    default=(("", ""), 0),
)
# A position no declaration names, for the undeclared direction.
_SPLAT_UNDECLARED_POSITION = ("stories_engine.py", "_advance")


def test_journal_splat_count_cases_rest_on_a_multi_splat_position():
    """The rows below mutate the declared hole holding the MOST unresolved ``**``
    arguments, derived from the declaration rather than named. This pins the premise
    that makes the derivation worth anything: some declared position holds two or more
    splats — which is the shape DW-150 exists for, and without it `splat-removed` and
    `position-went-resolvable` collapse into each other at measured 0.

    And that a SECOND declared position exists, which is the other half of the same
    premise: with a one-row table `_SPLAT_SINGLE_POSITION` falls through `min`'s
    `default` to `("", "")` at declared 0, and the `stale-row` case below would then
    fail naming `_journal_splat_count_drift` — blaming the helper for a shrunken
    declaration this test exists to name instead.

    `test_journal_measured_splats_counts_arguments_not_calls` rides that second
    assertion too, and harder: `_SPLAT_SINGLE_DECLARED` is what makes its
    `_SPLAT_ONE_CALL_MEASURED` two rather than a vacuous one — at declared 0 its
    "two splats in one call" row would generate ONE splat and assert `1 == 1`, and
    `_SPLAT_SINGLE_POSITION`'s empty function name would render `def (self, result):`,
    dying in `ast.parse` with a SyntaxError that names none of this."""
    assert _SPLAT_DECLARED >= 2, (
        "no declared splat hole holds 2+ unresolved ** arguments any more, so the "
        "`splat-removed` and `position-went-resolvable` cases below stop covering "
        f"separate directions. Declared: {dict(JOURNAL_SPLAT_ALLOW)}"
    )
    assert _SPLAT_SINGLE_DECLARED >= 1, (
        "the declaration no longer holds a second position, so `_SPLAT_SINGLE_POSITION` "
        "fell through to `min`'s default and the `stale-row` case below grades a "
        f"phantom. Declared: {dict(JOURNAL_SPLAT_ALLOW)}"
    )
    assert _SPLAT_UNDECLARED_POSITION not in JOURNAL_SPLAT_ALLOW


JOURNAL_SPLAT_COUNT_CASES = [
    # The tree as declared: silent.
    ("as-declared", dict(JOURNAL_SPLAT_ALLOW), {}),
    # A splat ADDED inside an already-declared position — DW-150's shape, the one
    # membership was blind to and the one the real-tree ablation exercises.
    (
        "splat-added",
        {**JOURNAL_SPLAT_ALLOW, _SPLAT_MULTI_POSITION: _SPLAT_DECLARED + 1},
        {_SPLAT_MULTI_POSITION: (_SPLAT_DECLARED, _SPLAT_DECLARED + 1)},
    ),
    # …and the other direction: a splat REMOVED from a multi-splat hole is drift
    # too, not an improvement — the declaration now over-states the hole.
    (
        "splat-removed",
        {**JOURNAL_SPLAT_ALLOW, _SPLAT_MULTI_POSITION: _SPLAT_DECLARED - 1},
        {_SPLAT_MULTI_POSITION: (_SPLAT_DECLARED, _SPLAT_DECLARED - 1)},
    ),
    # Every splat at the multi-splat position became resolvable: the row is a waiver
    # for nothing, and measured 0 off a declared 2+ is what says so.
    (
        "position-went-resolvable",
        {**JOURNAL_SPLAT_ALLOW, _SPLAT_MULTI_POSITION: 0},
        {_SPLAT_MULTI_POSITION: (_SPLAT_DECLARED, 0)},
    ),
    # The same measured 0 off a SINGLE-splat row, which is the stale-row shape the
    # old set comparison caught and which must survive the move to counts: the
    # position is gone from the tree and the declaration still names it.
    (
        "stale-row",
        {**JOURNAL_SPLAT_ALLOW, _SPLAT_SINGLE_POSITION: 0},
        {_SPLAT_SINGLE_POSITION: (_SPLAT_SINGLE_DECLARED, 0)},
    ),
    # An UNDECLARED position: declared half 0, so the union keying is what reports it.
    # Unlike the kind axis this is NOT deferred to the offender filter — that filter
    # names the line, this names the number, and the producer test's docstring says
    # why both.
    (
        "undeclared-position",
        {**JOURNAL_SPLAT_ALLOW, _SPLAT_UNDECLARED_POSITION: 1},
        {_SPLAT_UNDECLARED_POSITION: (0, 1)},
    ),
]


@pytest.mark.parametrize(
    ("label", "population", "expected"),
    JOURNAL_SPLAT_COUNT_CASES,
    ids=[c[0] for c in JOURNAL_SPLAT_COUNT_CASES],
)
def test_journal_splat_count_drift_reports_every_direction(label, population, expected):
    """`_journal_splat_count_drift`'s decision, as rows — the mutations the real tree
    cannot show without editing `src/`, which this guard must not do.

    Each population also carries a RESOLVABLE field at an undeclared position, which
    must never reach the drift: the count reads the `field is None` rows only, and a
    helper that counted every journal field would report a position for every module
    in the tree."""
    findings = [
        ("journalfield", rel, 1, "journal.append(k, **extras)", (None, fn, None))
        for (rel, fn), count in population.items()
        for _ in range(count)
    ]
    findings.append(
        ("journalfield", "sweep.py", 1, "journal.append(k, story_key=s)", ("story_key", "_t", None))
    )
    assert _journal_splat_count_drift(findings) == expected, label


def test_journal_field_offenders_flag_every_line_of_an_over_declared_splat():
    """A splat added inside an already-declared hole is an offender ONCE PER
    UNRESOLVED `**` ARGUMENT, naming measured against declared — the half of DW-150
    that lands in the routing guard's remedy rather than in the producer test's
    numbers. This row spells each argument on its own line, so the offenders are
    per-line here; `test_journal_measured_splats_counts_arguments_not_calls` carries
    the other shape, where two arguments in ONE call yield two offenders on one line.

    Membership alone waived all of them: the position was declared, so a second,
    third or tenth `**splat` dropped in beside the first read as sanctioned and its
    field names never entered the inventory. Every unresolved argument is reported
    rather than the surplus one, because nothing in a count says WHICH of them is new.

    The under-count direction is deliberately absent here and lives in
    `test_journal_splat_count_drift_reports_every_direction`: a position that lost an
    unresolved argument leaves no finding for a filter over findings to flag."""
    rel, fn = _SPLAT_MULTI_POSITION
    findings = [
        ("journalfield", rel, 10 + i, "journal.append(k, **extras)", (None, fn, None))
        for i in range(_SPLAT_DECLARED + 1)
    ]
    offenders = _journal_field_offenders(findings)
    assert [ln for _, ln, _, _ in offenders] == [10 + i for i in range(_SPLAT_DECLARED + 1)]
    assert all(
        f"measured {_SPLAT_DECLARED + 1}, declared {_SPLAT_DECLARED}" in what
        for *_, what in offenders
    ), offenders
    # …and the declared population itself stays silent, so the row above is not
    # passing because the branch refuses every splat.
    assert (
        _journal_field_offenders(findings[:_SPLAT_DECLARED]) == []
    ), "the declared count is refused, so the over-count assertion proves nothing"


# ONE call carrying TWO unresolvable `**` arguments, at a position declaring one. The
# shape the two candidate count units disagree on, written as SOURCE rather than as a
# synthetic finding population, because the disagreement is about what the SCAN emits:
# a call-shaped unit reads this as 1, the argument unit `_journal_measured_splats`
# defines reads it as 2. Neither dict is built from literals in this function, so the
# resolver reads neither and both arrive as `field is None`.
#
# The position AND the number of splats are DERIVED from the declaration, for the same
# reason as the count rows above: naming either would leave a hardcoded value behind
# that a deliberate change to the table would not move. One splat MORE than the
# position declares is what makes the call an over-count in every declaration shape —
# two unresolved arguments in one call today, against that row's declared 1.
_SPLAT_ONE_CALL_MEASURED = _SPLAT_SINGLE_DECLARED + 1
_TWO_SPLATS_IN_ONE_CALL_SOURCE = """\
class A:
    def {fn}(self, result):
        self.journal.append('session-end', {args})
""".format(
    fn=_SPLAT_SINGLE_POSITION[1],
    args=", ".join(f"**self._extras{i}(result)" for i in range(_SPLAT_ONE_CALL_MEASURED)),
)

# The UNRESOLVED half of the unit, at the multiplicity level: one READABLE `**` beside
# an unreadable one in the SAME call. Only the unreadable argument is a hole, so the
# position measures 1 — a counter that keyed off `**` syntax rather than off the
# resolver's verdict would say 2 here, and `JOURNAL_FIELD_PROBES` cannot catch that
# because it compares field-name SETS, where multiplicity is structurally invisible.
_ONE_RESOLVABLE_ONE_UNREADABLE_SPLAT_SOURCE = """\
class A:
    def {fn}(self, result):
        known = {{"story_key": result.key}}
        self.journal.append('session-end', **known, **self._extras(result))
""".format(fn=_SPLAT_SINGLE_POSITION[1])


def test_journal_measured_splats_counts_arguments_not_calls():
    """A single `journal.append(kind, **a, **b)` measures TWO, not one — the unit
    pinned by a test rather than only by a comment, so the next reader inherits the
    decision instead of re-opening it. (Two is `_SPLAT_SINGLE_DECLARED + 1`, derived
    so the row stays an over-count if that declaration ever moves.)

    DW-150 exists to redden a second splat dropped inside an already-declared position.
    That splat arrives two ways: as a NEW CALL beside the first, or as a second `**`
    on a call already there. A write-call unit sees only the first, and the second
    would keep escaping — a whole further dict of unreadable names flowing through a
    hole whose declaration never moved. The argument unit is the finer of the two
    readings and was chosen for exactly that reason.

    Runs the real `_scan_source`, so it grades the EMIT and not a hand-built
    population: the arithmetic is only correct if the scan really emits one
    `field is None` finding per unresolvable `**` keyword rather than one per call.

    Ablation: make `_scan_source` emit once per call with any unresolvable splat and
    the first assertion drops to 1; the over-count assertion below then goes silent
    too, which is precisely the escape this unit closes."""
    rel, fn = _SPLAT_SINGLE_POSITION
    findings = [
        f for f in _scan_source(_TWO_SPLATS_IN_ONE_CALL_SOURCE, rel) if f[0] == "journalfield"
    ]
    # Every unresolvable argument is emitted, and they all sit on the ONE call's line —
    # which is what makes "one call" and "two findings" the same shape here.
    assert [payload for *_, payload in findings] == [
        (None, fn, "session-end")
    ] * _SPLAT_ONE_CALL_MEASURED, findings
    assert len({ln for _, _, ln, _, _ in findings}) == 1, findings
    assert _journal_measured_splats(findings) == Counter(
        {(rel, fn): _SPLAT_ONE_CALL_MEASURED}
    ), findings

    # …and the consequence: measured 2 against the position's declared 1 is an
    # over-count, so the routing guard flags the call rather than waiving it. The
    # remedy is to declare 2, never to dedupe by call.
    offenders = _journal_field_offenders(findings)
    assert len(offenders) == _SPLAT_ONE_CALL_MEASURED, offenders
    assert all(
        f"measured {_SPLAT_ONE_CALL_MEASURED}, declared {_SPLAT_SINGLE_DECLARED}" in what
        for *_, what in offenders
    ), offenders
    # …and the NUMBERS half, which the I/O matrix names for this shape: the producer
    # test reports (declared, measured) drift at the same position. Asserted directly
    # rather than inferred from the shared counter, so the matrix row has a test.
    drift = _journal_splat_count_drift(findings)
    assert drift.get(_SPLAT_SINGLE_POSITION) == (
        _SPLAT_SINGLE_DECLARED,
        _SPLAT_ONE_CALL_MEASURED,
    ), drift

    # The UNRESOLVED half of the unit: a READABLE `**` beside an unreadable one in the
    # same call contributes 0. The count is of holes the resolver could not read, not
    # of `**` tokens — without this, a counter keyed on splat SYNTAX would pass every
    # assertion above while inflating every position that splats a literal dict.
    mixed = [
        f
        for f in _scan_source(_ONE_RESOLVABLE_ONE_UNREADABLE_SPLAT_SOURCE, rel)
        if f[0] == "journalfield"
    ]
    assert _journal_measured_splats(mixed) == Counter({(rel, fn): 1}), mixed
    # Anti-vacuity: the readable half really was read, so the 1 above is the resolver
    # discriminating rather than the scan missing an argument.
    assert len(mixed) == 2, mixed
    assert {field for *_, (field, _, _) in mixed} == {None, "story_key"}, mixed


# Two same-named journal-writing defs in ONE module: the collision the position keys
# cannot express. Both write the journal, so both reach the emit; the bare name is one
# and the `def` linenos are two.
_BARE_NAME_COLLISION_SOURCE = """\
class A:
    def _log(self, kind, **fields):
        self.journal.append(kind, **fields)


class B:
    def _log(self, kind, **fields):
        self.journal.append(kind, **fields)
"""

# The control: ONE journal-writing def of that name, beside a same-named def that
# writes nothing. A collision helper keyed on definitions rather than on journal
# WRITES would flag this, and the position tables would be right to ignore it for the
# four POSITION tables — a non-writing function contributes no findings to aggregate
# into their rows.
#
# ⚠️ Not `JOURNAL_FORWARDERS`, the fifth bare-name-keyed table. That row makes
# `_is_journal_write` read a CALL to `_log` inside `plugins/bus.py` as a journal
# write, so a same-named non-journaling `_log` there would still route its callers'
# keywords into the field inventory. This row is the position tables' answer, not a
# claim that a non-writing twin is harmless everywhere.
_BARE_NAME_SINGLE_WRITER_SOURCE = """\
class A:
    def _log(self, kind, **fields):
        self.journal.append(kind, **fields)


class B:
    def _log(self, kind, **fields):
        return None
"""


def test_journal_bare_name_collision_probes_read_def_identity():
    """`_journal_bare_name_collisions`' decision, as probes — the mutations the real
    tree cannot show, since it holds no duplicate today and `src/` must not be edited
    to invent one.

    Five rows, one per way the helper or the emit could be wrong: two same-named
    writing defs in one module ARE a collision (with both `def` linenos, which is the
    identity the bare name cannot carry); one writing def beside a same-named
    non-writing one is NOT (the position tables aggregate WRITES, not definitions);
    the same name in two different FILES is not, because the key is `(file, name)`; a
    module-level write is not, because a file has exactly one module scope; and a
    non-journal `.append` inside a def emits nothing at all — the must-stay-silent
    lookalike every new emit in this file owes, without which the emit could pass
    every row above by firing on every `.append` in the tree.

    Each silent row pins its POPULATION as well as the empty result, so none of them
    passes for the wrong reason. `== {}` alone is green with the emit deleted.

    Ablation (observed): dropping the `def` lineno from the emit — leaving the bare
    name alone — reddens the collision row rather than silencing it, because
    `_journal_bare_name_collisions` skips `def_lineno is None` and the result
    collapses to `{}`. Emitting a CONSTANT lineno is the mutation that would make one
    name one key, and it reddens the same row."""
    collisions = _journal_bare_name_collisions(
        [
            f
            for f in _scan_source(_BARE_NAME_COLLISION_SOURCE, "plugins/bus.py")
            if f[0] == "journalfnscope"
        ]
    )
    assert collisions == {("plugins/bus.py", "_log"): [2, 7]}, collisions

    single_writer = [
        f
        for f in _scan_source(_BARE_NAME_SINGLE_WRITER_SOURCE, "plugins/bus.py")
        if f[0] == "journalfnscope"
    ]
    assert [f[4] for f in single_writer] == [("_log", 2)], single_writer
    assert _journal_bare_name_collisions(single_writer) == {}

    # Same bare name, two FILES — the shape the real tree already holds
    # (`plugins/bus.py::_log` and a `_log` elsewhere), and not a collision.
    one_writer = "class A:\n    def _log(self, kind, **fields):\n        self.journal.append(kind, **fields)\n"
    across_files = [
        f
        for rel in ("plugins/bus.py", "stories_engine.py")
        for f in _scan_source(one_writer, rel)
        if f[0] == "journalfnscope"
    ]
    assert len(across_files) == 2, across_files
    assert _journal_bare_name_collisions(across_files) == {}

    # Module-level writes: `fn is None`, so they are skipped rather than aggregated
    # into a phantom `(rel, None)` row that two files could never disambiguate.
    module_level = [
        f
        for f in _scan_source(
            'journal.append("run-start", attempt=1)\njournal.append("run-stop", attempt=2)\n',
            "cli.py",
        )
        if f[0] == "journalfnscope"
    ]
    assert [f[4] for f in module_level] == [(None, None), (None, None)], module_level
    assert _journal_bare_name_collisions(module_level) == {}

    # The must-stay-silent lookalike: an `.append` on something that is not a journal
    # receiver, inside a def. No `journalfnscope` at all — not a `(name, lineno)` pair
    # the collision helper then has to filter out. Scanned in a file with no
    # `JOURNAL_FORWARDERS` row, so the name cannot be what makes it a write.
    not_a_journal = _scan_source(
        "def _triage(self):\n    self.items.append(story)\n    other.append(1)\n", "sweep.py"
    )
    assert [f for f in not_a_journal if f[0] == "journalfnscope"] == [], not_a_journal


# The `recovery_flow.prune_preserve_refs` shape, as a snippet: a `for` over a literal
# tuple of literal tuples, and the SAME four writes the real function makes — two
# spelling `-pruned` (the partial-prune and the clean paths) and two `-prune-failed`
# (with and without the failed refs). Four rather than a convenient two so the
# "reproduces today's tree" claim below is true against the declared COUNT as well as
# the declared spellings, and so the pair of sites minting one spelling exercises the
# drift helper's de-duplication.
#
# Everything the minted-kind probes need to mutate lives here rather than in `src/`,
# which the guard must not edit — and scanning it as `recovery_flow.py` puts the
# findings at the very position `JOURNAL_DYNAMIC_KIND_SPELLINGS` declares, so the
# drift helper grades them against the real row.
MINTED_KIND_PROBE_SOURCE = """\
def prune_preserve_refs(self):
    for family, prune in (
        ("attempt-preserve", verify.prune_preserve_refs),
        ("attempt-preserve-dirty", verify.prune_preserve_dirty_refs),
    ):
        try:
            deleted = prune(root, keep)
        except Exception as exc:
            partial = getattr(exc, "deleted", [])
            if partial:
                self.journal.append(f"{family}-pruned", count=len(partial), refs=partial)
            failed = getattr(exc, "failed", [])
            if failed:
                self.journal.append(f"{family}-prune-failed", error=str(exc), failed=failed)
            else:
                self.journal.append(f"{family}-prune-failed", error=str(exc))
            continue
        if deleted:
            self.journal.append(f"{family}-pruned", count=len(deleted), refs=deleted)
"""

# The position the snippet lands on and the spellings declared for it. READ from the
# declaration rather than restated, avoiding a second inventory of full spellings.
# The source fixture still mirrors the production family literals and suffixes and
# must be updated alongside them when their spelling changes.
_MINTED_POSITION = ("recovery_flow.py", "prune_preserve_refs")
_MINTED_SPELLINGS = JOURNAL_DYNAMIC_KIND_SPELLINGS[_MINTED_POSITION]


def _minted(source: str, rel: str = "recovery_flow.py"):
    """The `journalkindminted` findings a snippet yields, as `_of` would hand them to
    the drift helper."""
    return [f for f in _scan_source(source, rel) if f[0] == "journalkindminted"]


def _minted_spellings(source: str, rel: str = "recovery_flow.py") -> set[str]:
    """Just the spellings, de-duplicated across sites."""
    return {spelling for *_, (_, spelling) in _minted(source, rel)}


def _shadowed_loop_write(kind: str, *body: str) -> str:
    """A function whose `for family` loop resolves to `attempt-preserve`, with ``body``
    spliced in ahead of a journal write spelling ``kind``.

    The shared shape behind the fail-loud rows: because the loop alone resolves to the
    DECLARED spelling `attempt-preserve-pruned`, a guard deleted from `_rebinds_name`,
    `_loop_literal_bindings` or `_fstring_kind_spellings` does not redden the tree-wide
    assertion — it reads the wrong value and stays green. These rows are what turn each
    guard into something ablation can reach."""
    lines = [
        "def prune_preserve_refs(self):",
        '    for family in ("attempt-preserve",):',
        *(f"        {line}" for line in body),
        f"        self.journal.append({kind}, count=n)",
        "",
    ]
    return "\n".join(lines)


def _shadowed_loop(*body: str) -> str:
    """:func:`_shadowed_loop_write` over the plain `f"{family}-pruned"` kind — the
    shape every `_rebinds_name` row uses, where the mutation is the spliced body."""
    return _shadowed_loop_write('f"{family}-pruned"', *body)


def test_journal_minted_kind_probes_expand_the_fstring():
    """The detector half: an f-string kind is expanded through the loop bindings that
    supply it, over the shapes the tree cannot show — a `Tuple` target unpacked from a
    literal tuple of literal tuples (the tree's shape), a bare `Name` target over a
    literal tuple, and a name bound by TWO loops, whose values are unioned.

    The two-loop row pins `values |= resolved`: ablate it to `values = resolved` and
    only the last loop's spellings survive, which on the real tree is invisible because
    `family` is bound once.

    Ablation: delete the `isinstance(first, ast.JoinedStr)` emit and every row here
    reddens with an empty set."""
    assert _minted_spellings(MINTED_KIND_PROBE_SOURCE) == set(_MINTED_SPELLINGS)
    # The snippet reproduces today's tree on both axes, so it must drift against the
    # real declaration by nothing — and land on the declared write count.
    assert _journal_minted_kind_drift(_minted(MINTED_KIND_PROBE_SOURCE)) == {}
    assert _MINTED_POSITION not in _journal_kind_count_drift(
        [
            f
            for f in _scan_source(MINTED_KIND_PROBE_SOURCE, "recovery_flow.py")
            if f[0] == "journalkind"
        ]
    )

    bare_target = (
        "def prune_preserve_refs(self):\n"
        '    for family in ("attempt-preserve", "attempt-preserve-dirty"):\n'
        '        self.journal.append(f"{family}-pruned", count=n)\n'
    )
    assert _minted_spellings(bare_target) == {s for s in _MINTED_SPELLINGS if s.endswith("-pruned")}

    two_loops = (
        "def prune_preserve_refs(self):\n"
        '    for family in ("attempt-preserve",):\n'
        "        pass\n"
        '    for family in ("attempt-preserve-dirty",):\n'
        '        self.journal.append(f"{family}-pruned", count=n)\n'
    )
    assert _minted_spellings(two_loops) == {s for s in _MINTED_SPELLINGS if s.endswith("-pruned")}


# `(old, new)` fragments that respell one half of the f-string. Each is a substring
# of the SOURCE (so the mutation is a plain replace) and of the SPELLINGS it produces
# (so the expected drift halves are derived, not restated): `-pruned` is the literal
# suffix, `attempt-preserve-dirty` the loop tuple's family literal.
MINTED_KIND_RESPELLINGS = [
    ("kind suffix respelled", "-pruned", "-purged"),
    ("family literal respelled", "attempt-preserve-dirty", "attempt-preserve-grubby"),
]


@pytest.mark.parametrize(
    ("label", "old", "new"),
    MINTED_KIND_RESPELLINGS,
    ids=["suffix", "family"],
)
def test_journal_minted_kind_probes_catch_a_respelling(label, old, new):
    """DW-151's gap, as rows: a respelling on EITHER half of the f-string — the literal
    suffix or the loop tuple's family literal — reddens the minting assertion in both
    directions at once, while the write COUNT it also declares is untouched.

    That second clause is the whole point. Both mutations leave the position at four
    `journalkind` findings, so `_journal_kind_count_drift` reports nothing and the
    literalness row still sees a declared position; before this axis existed the only
    thing that named these kinds was a comment.

    The expected halves are DERIVED from the declaration and the mutation — the
    spellings the mutated substring appears in vanish, the rewritten ones arrive.
    Production renames also require updating the matching source fixture fragments;
    these expected sets do not need a separate inventory edit.

    Ablation: delete the `isinstance(first, ast.JoinedStr)` emit, or make
    `_fstring_kind_spellings` return the literal parts only, and both rows redden. The
    filter's `!=` cannot be ablated from HERE — a respelling adds as well as removes,
    so an additions-only reading (`found - declared` in place of `!=`) still reports
    these rows; `test_journal_minted_kind_drift_reports_a_stale_declared_row` is the
    row that holds the removal direction."""
    source = MINTED_KIND_PROBE_SOURCE.replace(old, new)
    assert source != MINTED_KIND_PROBE_SOURCE, label
    gone = {spelling for spelling in _MINTED_SPELLINGS if old in spelling}
    added = {spelling.replace(old, new) for spelling in gone}
    assert gone and added.isdisjoint(_MINTED_SPELLINGS), label
    declared, found = _journal_minted_kind_drift(_minted(source))[_MINTED_POSITION]
    assert declared - found == gone, label
    assert found - declared == added, label

    # The count axis is blind to all of it: the mutation renames a kind, it does not
    # add or remove a write, so the position still measures the four writes it declares
    # and `_journal_kind_count_drift` has nothing to say about it.
    assert _MINTED_POSITION not in _journal_kind_count_drift(
        [f for f in _scan_source(source, "recovery_flow.py") if f[0] == "journalkind"]
    ), label


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "parameter, no loop binding",
            "def prune_preserve_refs(self, family):\n"
            '    self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        (
            "loop over a call",
            "def prune_preserve_refs(self):\n"
            "    for family in _families():\n"
            '        self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        (
            "interpolated call",
            "def prune_preserve_refs(self):\n"
            '    self.journal.append(f"{_family_for(x)}-pruned", count=n)\n',
        ),
        # `_fstring_kind_spellings`' two per-part guards. Both change what the code
        # actually writes while leaving the name resolvable: `{family!r}` ships
        # `'attempt-preserve'` quotes and all, `{family:.4}` ships `atte`.
        ("conversion applied", _shadowed_loop_write('f"{family!r}-pruned"')),
        ("format spec applied", _shadowed_loop_write('f"{family:.4}-pruned"')),
        # `_sequence_literal_elements`' starred guard, in the two places it sits. On
        # the OUTER iterable the `Constant` check would refuse the `Starred` element
        # anyway; on an INNER element it is the only thing that refuses, because
        # column 0 is a perfectly good literal whose position depends on an unknown
        # arity.
        (
            "starred loop iterable",
            "def prune_preserve_refs(self):\n"
            '    for family in ("attempt-preserve", *rest):\n'
            '        self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        (
            "starred element of the loop tuple",
            "def prune_preserve_refs(self):\n"
            '    for family, prune in (("attempt-preserve", *rest),):\n'
            '        self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        # A `for` binding the enclosing body cannot SEE. `ast.walk` unioned it in and
        # minted a spelling the code never writes.
        (
            "loop inside a nested def",
            "def prune_preserve_refs(self):\n"
            '    for family in ("attempt-preserve",):\n'
            "        def inner():\n"
            '            for family in ("PHANTOM",):\n'
            "                pass\n"
            '        self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        # A `lambda` parameter shadowing the loop name, with the write INSIDE the
        # lambda — `_enclosing_function_nodes` maps the call to the enclosing `def`,
        # so without the `ast.Lambda` arm the resolver answers from the outer loop.
        (
            "lambda parameter shadows the loop",
            "def prune_preserve_refs(self):\n"
            '    for family in ("attempt-preserve",):\n'
            '        f = lambda family: self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        (
            "variadic lambda parameter shadows the loop",
            "def prune_preserve_refs(self):\n"
            '    for family in ("attempt-preserve",):\n'
            '        f = lambda *family: self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        (
            "keyword variadic lambda parameter shadows the loop",
            "def prune_preserve_refs(self):\n"
            '    for family in ("attempt-preserve",):\n'
            '        f = lambda **family: self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        (
            "class-local loop cannot supply the outer write",
            "def prune_preserve_refs(self):\n"
            "    class Inner:\n"
            '        for family in ("attempt-preserve",):\n'
            "            pass\n"
            '    self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        (
            "nested unpacking overwrites the selected column",
            "def prune_preserve_refs(self):\n"
            '    for family, (family, extra) in (("attempt-preserve", ("PHANTOM", 1)),):\n'
            '        self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        (
            "unpacked set has no fixed column order",
            "def prune_preserve_refs(self):\n"
            '    for family, other in ({"attempt-preserve", "PHANTOM"},):\n'
            '        self.journal.append(f"{family}-pruned", count=n)\n',
        ),
        # One row per `_rebinds_name` arm, each spliced into a loop that would
        # otherwise resolve to the DECLARED spelling — so deleting the arm reddens here
        # rather than passing green against the declaration.
        ("assign", _shadowed_loop("family = family.upper()")),
        ("annassign", _shadowed_loop('family: str = "PHANTOM"')),
        ("augassign", _shadowed_loop('family += "-x"')),
        ("walrus", _shadowed_loop('if (family := "PHANTOM"):', "    pass")),
        ("comprehension", _shadowed_loop('rows = [family for family in ("PHANTOM",)]')),
        ("with-as", _shadowed_loop("with lock() as family:", "    pass")),
        (
            "except-as",
            _shadowed_loop("try:", "    pass", "except Exception as family:", "    pass"),
        ),
        ("import-as", _shadowed_loop("import collections as family")),
        ("from-import-as", _shadowed_loop("from collections import Counter as family")),
        ("global", _shadowed_loop("global family")),
        ("nonlocal", _shadowed_loop("nonlocal family")),
        ("nested-def-name", _shadowed_loop("def family():", "    return 1")),
        ("nested-def-param", _shadowed_loop("def inner(family):", "    return family")),
        ("class-name", _shadowed_loop("class family:", "    pass")),
    ],
    ids=[
        "parameter",
        "call-iterable",
        "call-part",
        "conversion",
        "format-spec",
        "starred-iterable",
        "starred-element",
        "nested-loop",
        "lambda-param",
        "lambda-vararg",
        "lambda-kwarg",
        "class-loop",
        "nested-target",
        "inner-set",
        "assign",
        "annassign",
        "augassign",
        "walrus",
        "comprehension",
        "with-as",
        "except-as",
        "import-as",
        "from-import-as",
        "global",
        "nonlocal",
        "nested-def-name",
        "nested-def-param",
        "class-name",
    ],
)
def test_journal_minted_kind_probes_fail_loud_on_an_unresolvable_interpolation(label, source):
    """Every direction `_loop_literal_bindings`, `_sequence_literal_elements` and
    `_fstring_kind_spellings` refuse mints a spelling carrying
    `UNRESOLVED_DYNAMIC_KIND` — never nothing, and never the loop's literal.

    Skipping is one failure mode this forbids: an unreadable interpolation that emitted
    no finding would leave the position measured short, which reads exactly like a
    write that was deliberately removed. Answering ANYWAY is the worse one, and it is
    why every `_rebinds_name` arm gets a row rather than a comment. Each row splices
    its shadowing construct into a loop that resolves to `attempt-preserve-pruned` — a
    DECLARED spelling — so an arm deleted from `_rebinds_name` does not redden the
    tree-wide assertion or anything else in this file; it just reads the wrong value
    and stays green. AGENTS.md's ablation rule, applied per arm.

    Ablation: return an empty set (or drop the part) instead of the sentinel in
    `_fstring_kind_spellings` and every row reddens; delete any single arm of
    `_rebinds_name`, the `nested` refusal in `_loop_literal_bindings`, or the starred
    guard in `_sequence_literal_elements` and exactly the rows named above redden."""
    assert _minted_spellings(source) == {f"{UNRESOLVED_DYNAMIC_KIND}-pruned"}, label
    # …and it cannot be declared away: the drift helper reports the position.
    assert _journal_minted_kind_drift(_minted(source)), label


def test_loop_literal_bindings_is_unresolvable_without_a_loop():
    """`_loop_literal_bindings` answers None — not an empty set — for a name no `for`
    in the function binds, which is the `values if bound else None` flag.

    Unpinnable through the spelling probes: the caller's `resolved or {sentinel}` turns
    an empty set into the same sentinel, so ablating the flag to `return values` leaves
    every row above green while the helper's own contract (`_journal_splat_keys`' —
    "a name with no store at all is unresolvable, not vacuously empty") is broken. A
    later caller that distinguished the two would inherit the bug silently."""
    fn = ast.parse("def prune_preserve_refs(self):\n    return 1\n").body[0]
    assert _loop_literal_bindings(fn, "family") is None
    assert _loop_literal_bindings(None, "family") is None
    # …and the positive direction, so the None above is not the only answer it gives.
    bound = ast.parse('def f():\n    for family in ("attempt-preserve",):\n        pass\n').body[0]
    assert _loop_literal_bindings(bound, "family") == {"attempt-preserve"}


def test_journal_minted_kind_drift_reports_an_undeclared_position():
    """A position that starts minting f-string kinds without a declaration reddens with
    an EMPTY declared half — the union arm of `_journal_minted_kind_drift`.

    Nothing waives minting per position (unlike the kind-resolution waiver
    `JOURNAL_DYNAMIC_KIND_ALLOW` grants), so there is no sibling assertion to hand this
    to and the helper must answer it itself. The snippet is the same one, scanned as a
    file that declares no spellings.

    The declared `recovery_flow.py` row rides along at measured-empty, because a
    snippet population holds no findings for it — that is the staleness arm below, and
    the reason this row reads ONE key rather than comparing the whole dict.

    Ablation: iterate `JOURNAL_DYNAMIC_KIND_SPELLINGS`' keys instead of the union and
    this reddens with a KeyError."""
    drift = _journal_minted_kind_drift(_minted(MINTED_KIND_PROBE_SOURCE, "sweep.py"))
    assert drift[("sweep.py", "prune_preserve_refs")] == (
        frozenset(),
        frozenset(_MINTED_SPELLINGS),
    )


def test_journal_minted_kind_drift_reports_a_stale_declared_row():
    """The other end of the union: a declared row whose position stopped minting
    reddens at measured-empty, so the declaration cannot survive as a pre-approval for
    whatever kind reuses those spellings next.

    This is also the anti-vacuity floor for the tree-wide row — a scan that quietly
    stopped emitting `journalkindminted` findings reddens there rather than passing
    green, which the `[]` population demonstrates directly."""
    assert _journal_minted_kind_drift([]) == {
        position: (frozenset(declared), frozenset())
        for position, declared in JOURNAL_DYNAMIC_KIND_SPELLINGS.items()
    }
    assert JOURNAL_DYNAMIC_KIND_SPELLINGS, "an empty declaration would make the row vacuous"


def test_journal_minted_kind_probes_stay_silent_on_a_non_fstring_kind():
    """Only a JoinedStr in the KIND slot mints. A Name kind, a call kind, a `**` splat
    covering the slot and a plain literal each emit nothing on this axis — the other
    dynamic-kind positions (`engine._skip_review_and_commit`,
    `sweep._close_bundle_ledger_when_spec_status`, `plugins/bus.py::_log`) spell a
    parameter, whose literals reach the inventory from outside through
    `journalkindliteral`, and must not acquire a phantom minted spelling here.

    Vacuous on its own — deleting the emit leaves it green — which is what the positive
    rows above are for; this pins the emit's REACH, not its existence. An f-string
    ANYWHERE else in the call is silent too — a field value, and the KEYWORD kind
    channel — which are the arms most likely to be widened by accident.

    `append(kind=f"…")` is silent here but not unguarded: `_journal_keyword_kinds`
    reads that channel and reports `UNRESOLVED_DYNAMIC_KIND`, which
    `test_journal_kind_inventory_is_complete` refuses. The last assertion holds that
    second half, so this row cannot be read as "a keyword f-string kind is fine"."""
    for source, rel in (
        ("def f(self):\n    self.journal.append(kind, story_key=s)\n", "recovery_flow.py"),
        ("def f(self):\n    self.journal.append(_kind_for(x), count=n)\n", "recovery_flow.py"),
        ("def f(self):\n    self.journal.append(**everything)\n", "recovery_flow.py"),
        ('def f(self):\n    self.journal.append("run-start", story_key=s)\n', "recovery_flow.py"),
        (
            'def f(self):\n    self.journal.append("run-start", ref=f"{family}-pruned")\n',
            "recovery_flow.py",
        ),
        ('def f(self):\n    results.append(f"{family}-pruned")\n', "recovery_flow.py"),
        (
            'def f(self):\n    self.journal.append(kind=f"{family}-pruned", count=n)\n',
            "recovery_flow.py",
        ),
    ):
        assert not _minted(source, rel), source

    # The keyword channel's own guard, so its silence above is a division of labour
    # rather than a hole: the same call reaches the literal inventory as the sentinel.
    keyword_kind = 'def f(self):\n    self.journal.append(kind=f"{family}-pruned", count=n)\n'
    assert [
        f[4] for f in _scan_source(keyword_kind, "recovery_flow.py") if f[0] == "journalkindliteral"
    ] == [UNRESOLVED_DYNAMIC_KIND]


def test_journal_kind_probes_flag_a_non_literal_kind():
    """The detector half: a journal write whose kind is a Name, an f-string or a
    call emits a `journalkind` finding, and a literal one does not. Without this the
    tree-wide assertion is green with the emit deleted."""
    for source in (
        "def f(self):\n    self.journal.append(kind, story_key=s)\n",
        'def f(self):\n    self.journal.append(f"{family}-pruned", count=n)\n',
        "def f(self):\n    self.journal.append(_kind_for(x), count=n)\n",
        "def f(self):\n    self.journal.append(**everything)\n",
    ):
        assert [f for f in _scan_source(source, "sweep.py") if f[0] == "journalkind"], source
    for source in (
        'def f(self):\n    self.journal.append("run-start", story_key=s)\n',
        "def f(self):\n    results.append(kind)\n",
    ):
        assert not [f for f in _scan_source(source, "sweep.py") if f[0] == "journalkind"], source


def test_journal_kind_literal_probes_extract_the_kind():
    """The kind inventory's detector half: a journal write whose kind IS a string
    literal emits that kind — including a kind-only write like `run-complete` (no
    keyword arguments at all, not even a `**` splat), which the FIELD detector
    never reports, and a declared forwarder's call site, whose kind would
    otherwise stop at `plugins/bus.py::_log`'s wall.

    Ablation, per arm: delete the journal-write emit and the `run-start`,
    `run-complete`, `plugin-loaded` and `plugin-hook` rows redden; delete the caller's
    `kind=` emit and the `review-skipped-awaiting-operator` row reddens; delete the
    declared position's parameter-default emit and the `review-skipped` and
    `sweep-bundle-closed` rows redden. The keyword kind over an empty positional slot,
    the `**` splat literal, and the positional literal at a declared non-forwarder call
    site feed no row here; deleting any of them leaves this test green."""
    for source, rel, kind in (
        (
            'def f(self):\n    self.journal.append("run-start", story_key=s)\n',
            "sweep.py",
            "run-start",
        ),
        # The kind-only shape: no keywords, so no `journalfield` finding exists to
        # derive the kind from — this emit is the only reader.
        ('def f(self):\n    journal.append("run-complete")\n', "engine.py", "run-complete"),
        (
            'def f(self):\n    self._journal.append("plugin-loaded", plugin=name)\n',
            "plugins/registry.py",
            "plugin-loaded",
        ),
        ('def f(self):\n    self._log("plugin-hook", rc=rc)\n', "plugins/bus.py", "plugin-hook"),
        # The kinds a declared dynamic-kind POSITION receives from outside it: the
        # literal `kind=` a caller hands it, and the position's own parameter
        # default — keyword-only (`engine._skip_review_and_commit`) or
        # positional-or-keyword (`sweep._close_bundle_ledger_when_spec_status`).
        # The write inside spells a parameter, so nothing else reads these.
        (
            'def f(self):\n    self._skip_review_and_commit(task, kind="review-skipped-awaiting-operator")\n',
            "engine.py",
            "review-skipped-awaiting-operator",
        ),
        (
            'def _skip_review_and_commit(self, task, *, kind="review-skipped"):\n'
            "    self.journal.append(kind, story_key=s)\n",
            "engine.py",
            "review-skipped",
        ),
        (
            "def _close_bundle_ledger_when_spec_status(self, task, spec_file, status, "
            'kind="sweep-bundle-closed"):\n    return None\n',
            "sweep.py",
            "sweep-bundle-closed",
        ),
    ):
        found = [f[4] for f in _scan_source(source, rel) if f[0] == "journalkindliteral"]
        assert found == [kind], f"extracted {found} from:\n{source}"


def test_journal_kind_literal_probes_stay_silent_on_lookalikes():
    """The complement: a non-literal kind (the literalness test's territory), an
    `.append` on a non-journal receiver, a forwarder NAME outside its declared
    file, and prose are all silent — the inventory must not fill itself with
    strings that never reach `Journal.append`.

    Ablation: drop `_is_journal_write`'s receiver anchor (accept any `.append`) and
    the list-append row reddens; make `_splat_kind_literal` return the sentinel instead
    of None for a readable splat with no `kind` key and the `**{"story_key": s}` row
    reddens; drop the `JOURNAL_DYNAMIC_KIND_ALLOW` membership test on the call arm and
    the undeclared-callee splat row reddens with `['new-kind']`; key that arm by name
    alone and the wrong-file splat row reddens with `['x']`."""
    for source, rel in (
        ("def f(self):\n    self.journal.append(kind, story_key=s)\n", "sweep.py"),
        (
            'def f(self):\n    self.journal.append(f"{family}-pruned", count=n)\n',
            "recovery_flow.py",
        ),
        ('def f(self):\n    results.append("done")\n', "sweep.py"),
        ('def f(self):\n    self._log("plugin-hook", rc=rc)\n', "stories_engine.py"),
        (
            'def f():\n    """journal.append("prose-kind") is described here."""\n    return 1\n',
            "sweep.py",
        ),
        # The forwarder-kind arm is keyed `(file, name)` like the position it
        # serves: the same call in a file that declares no such position, a
        # `kind=` keyword on an undeclared callee, and a non-string default on its
        # def are all silent.
        (
            'def f(self):\n    self._skip_review_and_commit(task, kind="review-skipped")\n',
            "sweep.py",
        ),
        ('def f(self):\n    self.emit(kind="review-skipped")\n', "engine.py"),
        ("def _skip_review_and_commit(self, task, *, kind=None):\n    return None\n", "engine.py"),
        # The `**` splat arm's silence, in the three directions it must not invent a
        # kind: a READABLE splat that carries no `kind` (the position's parameter
        # default applies, and the definition arm reports that instead — folding it
        # into the sentinel would fail loud on a call that says nothing), a splat at an
        # UNDECLARED callee, and the same splat call in a file declaring no position.
        ('def f(self):\n    self._skip_review_and_commit(task, **{"story_key": s})\n', "engine.py"),
        ('def f(self):\n    self.emit(**{"kind": "new-kind"})\n', "engine.py"),
        (
            'def f(self):\n    self._skip_review_and_commit(task, **{"kind": "x"})\n',
            "stories_engine.py",
        ),
    ):
        assert not [f for f in _scan_source(source, rel) if f[0] == "journalkindliteral"], source

    # The one shape here that is NOT silent, and used to be. A non-literal `kind=` AT
    # a declared position is unresolvable rather than absent: `JOURNAL_DYNAMIC_KIND_ALLOW`
    # waives the literalness test for the write inside, so the inventory is the only
    # arm left that can fail loud on it.
    #
    # Ablation: return None instead of the sentinel for a non-literal `kind=` and this
    # reddens.
    unresolvable = [
        f[4]
        for f in _scan_source(
            "def f(self):\n    self._skip_review_and_commit(task, kind=chosen)\n", "engine.py"
        )
        if f[0] == "journalkindliteral"
    ]
    assert unresolvable == [UNRESOLVED_DYNAMIC_KIND], unresolvable


def test_journal_kind_literal_reads_a_splat_dynamic_kind():
    """A `kind` handed a declared dynamic-kind position through a `**` SPLAT is
    inventoried, not just a `kind=` keyword or a positional slot.

    `self._skip_review_and_commit(task, **{"kind": "new-kind"})` is legal Python that
    reaches the journal, and the `for kw in node.keywords` loop skipped it outright: a
    splat's `kw.arg` is None, so the `!= "kind"` test dropped it and the kind landed in
    the journal with no `JOURNAL_KINDS` row while the completeness assertion stayed
    green.

    The rows expecting `UNRESOLVED_DYNAMIC_KIND` are the fail-loud half — a splat over
    a Name, a non-literal `kind` value, a non-static or `{**other}` key wherever it sits
    — each yielding a kind no row can declare, so the inventory reddens naming the site
    rather than under-reporting.

    Ablation: delete the `kw.arg is None` branch from the `node.keywords` loop and
    every row here reddens."""
    for source, rel, kinds in (
        (
            'def f(self):\n    self._skip_review_and_commit(task, **{"kind": "new-kind"})\n',
            "engine.py",
            ["new-kind"],
        ),
        (
            "def f(self):\n    self._skip_review_and_commit(task, **fields)\n",
            "engine.py",
            [UNRESOLVED_DYNAMIC_KIND],
        ),
        (
            'def f(self):\n    self._skip_review_and_commit(task, **{"kind": chosen})\n',
            "engine.py",
            [UNRESOLVED_DYNAMIC_KIND],
        ),
        (
            'def f(self):\n    self._skip_review_and_commit(task, **{key: "x"})\n',
            "engine.py",
            [UNRESOLVED_DYNAMIC_KIND],
        ),
        # `{**other}` spells a None key node: unresolvable by definition, exactly as
        # `_dict_literal_keys` reads it — and it is unresolvable wherever it sits. A
        # LEADING one is displaced by the later literal, so Python does ship `x`; it is
        # refused anyway because reading it would mean tracking which side of the
        # unreadable entry each key sits on. A TRAILING one (next row) genuinely
        # OVERRIDES the `kind` just read. Same for a computed key. Reading the first
        # `kind` and returning inventoried the entry Python then threw away.
        (
            'def f(self):\n    self._skip_review_and_commit(task, **{**other, "kind": "x"})\n',
            "engine.py",
            [UNRESOLVED_DYNAMIC_KIND],
        ),
        (
            'def f(self):\n    self._skip_review_and_commit(task, **{"kind": "x", **other})\n',
            "engine.py",
            [UNRESOLVED_DYNAMIC_KIND],
        ),
        (
            'def f(self):\n    self._skip_review_and_commit(task, **{"kind": "x", key: "y"})\n',
            "engine.py",
            [UNRESOLVED_DYNAMIC_KIND],
        ),
        # A duplicated literal key is legal, and Python's last-wins is the kind that
        # ships: `b` is journalled, so `b` is what the inventory must grade.
        (
            'def f(self):\n    self._skip_review_and_commit(task, **{"kind": "a", "kind": "b"})\n',
            "engine.py",
            ["b"],
        ),
    ):
        found = [f[4] for f in _scan_source(source, rel) if f[0] == "journalkindliteral"]
        assert found == kinds, f"extracted {found} from:\n{source}"


def test_journal_kind_literal_splat_is_judged_per_expression():
    """An unreadable `**` splat is a finding on its own terms, even when the SAME call
    also delivers a kind the scan can read. Each row yields BOTH the readable kind and
    `UNRESOLVED_DYNAMIC_KIND`, in the four routes the readable half can arrive by: an
    explicit `kind=`, a second and literal splat, a filled positional slot at a declared
    non-forwarder position, and the same at the declared FORWARDER, whose kind never
    appears as a keyword at all. Two findings from one call is the contract, not a
    defect — the rejected reachability reading and why it lost are recorded on the
    `kw.arg is None` branch in `_scan_source`.

    Findings are compared as a MULTISET: the two halves arrive from different arms
    (keyword, positional, journal-write emit) at different points in the walk, so
    emission order is not contractual.

    Ablation: reintroduce the two-clause guard the rejected contract used —
    `any(kw.arg == "kind" ...)` or a FILLED positional slot — and rows 1, 3 and 4 lose
    their `UNRESOLVED_DYNAMIC_KIND` and redden. Row 2 is untouched by those clauses,
    because neither sees a kind delivered by a literal SPLAT: it is what defends the
    rejected contract's third clause, and its ablation is deleting the `kw.arg is None`
    branch, which drops both of its findings."""
    for source, rel, kinds in (
        (
            'def f(self):\n    self._skip_review_and_commit(task, kind="lit", **fields)\n',
            "engine.py",
            ["lit", UNRESOLVED_DYNAMIC_KIND],
        ),
        (
            'def f(self):\n    self._skip_review_and_commit(task, **{"kind": "x"}, **fields)\n',
            "engine.py",
            ["x", UNRESOLVED_DYNAMIC_KIND],
        ),
        # The non-forwarder positional route — the shape closest to real code, and the
        # half of the deleted guard that the forwarder row below does NOT cover: here
        # the positional arm reads the slot, not the journal-write emit.
        (
            _POSITIONAL_KIND_DEF + "def caller(self):\n"
            '    self._close_bundle_ledger_when_spec_status(task, spec, status, "k2", **fields)\n',
            "sweep.py",
            ["k2", UNRESOLVED_DYNAMIC_KIND],
        ),
        (
            "def _log(self, kind, **fields):\n"
            "    self._journal.append(kind, **fields)\n"
            "\n"
            'def caller(self):\n    self._log("plugin-hook", **fields)\n',
            "plugins/bus.py",
            ["plugin-hook", UNRESOLVED_DYNAMIC_KIND],
        ),
    ):
        found = [f[4] for f in _scan_source(source, rel) if f[0] == "journalkindliteral"]
        assert sorted(found) == sorted(kinds), f"extracted {found} from:\n{source}"


def test_journal_kind_literal_splat_and_positional_arms_do_not_double_report():
    """The one call shape both new-ish arms can fire on: a POSITIONAL-OR-KEYWORD
    declared position, where a `**` splat may fill the `kind` slot the positional arm
    also reads. Both rows here use a READABLE splat, so exactly one arm can read the
    kind and each site must yield exactly one finding. (An UNREADABLE splat beside a
    filled slot yields TWO — the readable kind and the sentinel — which is the accepted
    per-expression contract, graded by
    `test_journal_kind_literal_splat_is_judged_per_expression`, not a double report.)

    Neither arm may hand off blindly, and each row grades the opposite handoff: with
    the slot EMPTY only the splat can see the kind, and with the slot FILLED only the
    positional arm can. A dedupe condition widened to "the other arm will get it"
    silences one row or the other, which no probe outside this one catches.

    Ablation: delete the `kw.arg is None` branch — or make the splat emit defer on any
    declared position rather than reading the expression — and the first row goes empty;
    widen the positional arm's `not any(kw.arg == "kind" ...)` guard to bail on ANY
    keyword — the shape of a "let the splat arm own it" edit — and the second row goes
    empty. The second row's splat is READABLE and carries no `kind`, so
    `_splat_kind_literal` returns None and the splat arm contributes nothing to it
    either way: only the positional arm can grade it."""
    for source, rel, kinds in (
        (
            _POSITIONAL_KIND_DEF + "def caller(self):\n"
            '    self._close_bundle_ledger_when_spec_status(task, spec, status, **{"kind": "k1"})\n',
            "sweep.py",
            ["k1"],
        ),
        (
            _POSITIONAL_KIND_DEF + "def caller(self):\n"
            '    self._close_bundle_ledger_when_spec_status(task, spec, status, "k2", **{"other": 1})\n',
            "sweep.py",
            ["k2"],
        ),
    ):
        found = [f[4] for f in _scan_source(source, rel) if f[0] == "journalkindliteral"]
        assert found == kinds, f"extracted {found} from:\n{source}"


# The declared position whose `kind` is positional-or-keyword, with NO default, so the
# definition arm contributes nothing and each row's only finding is the one the
# positional arm read. `self` is dropped from the parameter list because a bound call
# never fills its slot — the offset this arm has to get right.
_POSITIONAL_KIND_DEF = (
    "def _close_bundle_ledger_when_spec_status(self, task, spec_file, status, kind):\n"
    "    self.journal.append(kind, story_key=s)\n"
    "\n"
)


def test_journal_kind_literal_reads_a_positional_dynamic_kind():
    """A literal handed a declared dynamic-kind position POSITIONALLY is inventoried,
    not just a `kind=` keyword.

    `sweep._close_bundle_ledger_when_spec_status(task, spec, status, "new-kind")` is
    legal Python — `kind` is positional-or-keyword — and reached the journal with no
    `JOURNAL_KINDS` row anyone had to decide on: the arm read `node.keywords` only, so
    the kind was never graded and the inventory reported itself complete. The three
    ways a literal reaches such a position (keyword, POSITIONAL, parameter default) now
    all feed the same emit.

    The unresolvable row is the fail-loud half: a `*args` splat covering the slot yields
    a kind no row can declare, so the inventory reddens naming the site instead of
    sharing its silence with "this call passed no literal".

    Ablation: restore the `for kw in node.keywords` loop as the arm's only reader and
    the first row reddens; delete the `ast.Starred` branch and the second does."""
    for source, rel, kinds in (
        (
            _POSITIONAL_KIND_DEF + "def caller(self):\n"
            '    self._close_bundle_ledger_when_spec_status(task, spec, status, "sweep-bundle-closed")\n',
            "sweep.py",
            ["sweep-bundle-closed"],
        ),
        (
            _POSITIONAL_KIND_DEF + "def caller(self):\n"
            '    self._close_bundle_ledger_when_spec_status(*rest, "sweep-bundle-closed")\n',
            "sweep.py",
            [UNRESOLVED_DYNAMIC_KIND],
        ),
    ):
        found = [f[4] for f in _scan_source(source, rel) if f[0] == "journalkindliteral"]
        assert found == kinds, f"extracted {found} from:\n{source}"


def test_journal_kind_literal_positional_arm_stays_silent_on_lookalikes():
    """The complement, in the four directions the positional arm must not invent a kind:
    a KEYWORD-ONLY `kind` (no positional slot exists, so a string in that argument
    position is some other parameter's), an EMPTY slot (the parameter default, which the
    definition arm reports instead — `sweep.py`'s own
    `_close_bundle_ledger_when_spec_status(task, str(spec_file), success_status)` call
    relies on it, so flagging an omitted `kind` would redden the clean tree), the same
    call in a file declaring no such position, and a declared FORWARDER whose positional
    kind the journal-write emit already reports — double-reporting one site would make a
    `found == [kind]` probe redden for a reason that is not a defect.

    A slot OCCUPIED by a non-literal is the direction that is deliberately not here: it
    is asserted RED below, because the literalness test that would otherwise catch it is
    waived at a declared position.

    Ablation: drop the `_is_journal_write` guard and the forwarder row reddens with two
    findings; key `kind_positions` by name alone and the wrong-file row reddens; fold the
    empty slot into `UNRESOLVED_DYNAMIC_KIND` and the omitted-`kind` row reddens."""
    for source, rel in (
        (
            "def _skip_review_and_commit(self, task, *, kind):\n"
            "    self.journal.append(kind, story_key=s)\n"
            "\n"
            "def caller(self):\n"
            '    self._skip_review_and_commit(task, "review-skipped")\n',
            "engine.py",
        ),
        (
            _POSITIONAL_KIND_DEF + "def caller(self):\n"
            "    self._close_bundle_ledger_when_spec_status(task, spec, status)\n",
            "sweep.py",
        ),
        (
            _POSITIONAL_KIND_DEF + "def caller(self):\n"
            '    self._close_bundle_ledger_when_spec_status(task, spec, status, "sweep-bundle-closed")\n',
            "stories_engine.py",
        ),
    ):
        assert not [f for f in _scan_source(source, rel) if f[0] == "journalkindliteral"], source

    # The slot this arm used to share with "no literal passed": OCCUPIED by a
    # non-literal. `JOURNAL_DYNAMIC_KIND_ALLOW` waives the literalness test for the
    # write inside the position, so the inventory is the only arm that can fail loud —
    # and it stayed green while a variable carrying an undeclared kind reached the
    # journal.
    #
    # Ablation: return None instead of the sentinel for a non-literal slot and this
    # reddens.
    unresolvable = [
        f[4]
        for f in _scan_source(
            _POSITIONAL_KIND_DEF + "def caller(self):\n"
            "    self._close_bundle_ledger_when_spec_status(task, spec, status, chosen)\n",
            "sweep.py",
        )
        if f[0] == "journalkindliteral"
    ]
    assert unresolvable == [UNRESOLVED_DYNAMIC_KIND], unresolvable

    # The forwarder: exactly one finding, from the journal-write emit, not two.
    forwarder = (
        "def _log(self, kind, **fields):\n"
        "    self._journal.append(kind, **fields)\n"
        "\n"
        "def caller(self):\n"
        '    self._log("plugin-hook", rc=rc)\n'
    )
    found = [
        f[4] for f in _scan_source(forwarder, "plugins/bus.py") if f[0] == "journalkindliteral"
    ]
    assert found == ["plugin-hook"], found


# The doubly-declared site DW-109 names: `plugins/bus.py::_log` is in
# `JOURNAL_SPLAT_ALLOW` (its `**fields` is an accepted hole) AND in
# `JOURNAL_DYNAMIC_KIND_ALLOW` (its kind is a parameter), so both arms that would
# otherwise grade a splat-carried kind are waived there at once.
_DOUBLY_DECLARED_FORWARDER = "def _log(self, kind, **fields):\n"


def test_journal_write_reads_a_keyword_or_splat_kind():
    """Read keyword kinds only over an EMPTY positional slot, preserving literalness.

    At plugins/bus.py::_log both dynamic-kind and splat waivers apply, so the recovered
    kind must independently reach the inventory. Filled, unreadable, and starred
    positional slots retain their existing behavior and never consult keywords.

    Ablation: delete the empty-slot keyword branch and keyword rows fail; remove the
    empty-slot gate and unreadable/starred rows fail, as does the real _log write."""
    for source, rel, kinds in (
        # DW-109's shape, at the doubly-declared position.
        (
            _DOUBLY_DECLARED_FORWARDER + '    self._journal.append(**{"kind": "brand-new-kind"})\n',
            "plugins/bus.py",
            ["brand-new-kind"],
        ),
        # An unreadable splat over an empty slot is the sentinel, never silence: no row
        # can declare it, so the inventory reddens naming the site.
        (
            "def f(self):\n    self._journal.append(**fields)\n",
            "engine.py",
            [UNRESOLVED_DYNAMIC_KIND],
        ),
        ('def f(self):\n    self._journal.append(kind="lit")\n', "engine.py", ["lit"]),
        (
            "def f(self):\n    self._journal.append(kind=chosen)\n",
            "engine.py",
            [UNRESOLVED_DYNAMIC_KIND],
        ),
        # A READABLE splat carrying no `kind` key is a statement that no kind is
        # spelled — the third answer `_splat_kind_literal` exists to give — so nothing
        # is inventoried and nothing is invented.
        (
            'def f(self):\n    self._journal.append(**{"story_key": s})\n',
            "engine.py",
            [],
        ),
        # A starred slot is occupied-but-unreadable, never EMPTY. Both keyword
        # spellings remain unconsulted at the doubly-declared position.
        (
            _DOUBLY_DECLARED_FORWARDER + '    self._journal.append(*args, **{"kind": "x"})\n',
            "plugins/bus.py",
            [],
        ),
        (
            _DOUBLY_DECLARED_FORWARDER + '    self._journal.append(*args, kind="x")\n',
            "plugins/bus.py",
            [],
        ),
        # TWO kinds through the one channel — the shape `_journal_keyword_kinds` returns
        # a list for. The call cannot legally ship both, but the scan cannot see which
        # `**fields` carries, so collapsing to the readable half would let an undeclared
        # kind through; both are inventoried and the sentinel fails loud.
        (
            'def f(self):\n    self._journal.append(kind="lit", **fields)\n',
            "engine.py",
            ["lit", UNRESOLVED_DYNAMIC_KIND],
        ),
        # A readable literal slot. This row pins the pre-existing OUTER branch — a
        # readable kind is reported as itself and nothing else is consulted — not the
        # new gate, which the outer `if kind is None` never reaches here.
        (
            'def f(self):\n    self._journal.append("epic-boundary", **fields)\n',
            "engine.py",
            ["epic-boundary"],
        ),
        # The gate's discriminating row: a slot genuinely FILLED with an unreadable
        # expression still owns the kind, so the `journalkind` arm reports the site and
        # `**fields` mints no sentinel. This is the real tree's shape.
        (
            _DOUBLY_DECLARED_FORWARDER + "    self._journal.append(kind, **fields)\n",
            "plugins/bus.py",
            [],
        ),
    ):
        found = [f[4] for f in _scan_source(source, rel) if f[0] == "journalkindliteral"]
        assert found == kinds, f"extracted {found} from:\n{source}"

    # The `journalkind` finding is still emitted BESIDE a keyword-spelled kind, and
    # that is deliberate: whether a POSITION may be dynamic stays the literalness arm's
    # question, so a keyword-spelled kind is refused outside a declared position exactly
    # as before. Pinned here because the rows above read `journalkindliteral` only, and
    # dropping the co-emission would silently widen the literalness waiver.
    both = [
        (f[0], f[4])
        for f in _scan_source('def f(self):\n    self._journal.append(kind="lit")\n', "engine.py")
        if f[0].startswith("journalkind")
    ]
    assert both == [("journalkind", "f"), ("journalkindliteral", "lit")], both

    # The reason this matters, asserted end to end rather than left to the emit: the
    # recovered kind has to reach the inventory's UNDECLARED arm, which is the only
    # thing that reddens CI. Graded at the doubly-declared position, so the assertion
    # is exactly the one both waivers used to swallow.
    undeclared, _ = _journal_kind_inventory_drift(
        [
            f
            for f in _scan_source(
                _DOUBLY_DECLARED_FORWARDER
                + '    self._journal.append(**{"kind": "brand-new-kind"})\n',
                "plugins/bus.py",
            )
            if f[0] == "journalkindliteral"
        ]
    )
    assert [(rel, kind) for rel, _, _, kind in undeclared] == [
        ("plugins/bus.py", "brand-new-kind")
    ], undeclared
    # Anti-vacuity: the assertion above is only interesting because the kind is not a
    # declared row AND the position is doubly waived. Both are pinned here so a future
    # edit to either declaration cannot quietly make this test pass for free.
    assert "brand-new-kind" not in JOURNAL_KINDS
    assert ("plugins/bus.py", "_log") in JOURNAL_SPLAT_ALLOW
    assert ("plugins/bus.py", "_log") in JOURNAL_DYNAMIC_KIND_ALLOW


def test_journal_write_preserves_sentinel_spelled_literal():
    """Resolver sentinel equality must not reclassify a positional AST literal."""
    line = f"    self._journal.append({UNRESOLVED_DYNAMIC_KIND!r}, patch=path)"
    findings = _scan_source(f"def f(self):\n{line}\n", "engine.py")
    assert findings == [
        ("journalfnscope", "engine.py", 2, line, ("f", 1)),
        ("journalkindliteral", "engine.py", 2, line, UNRESOLVED_DYNAMIC_KIND),
        ("journalfield", "engine.py", 2, line, ("patch", "f", UNRESOLVED_DYNAMIC_KIND)),
    ]


def test_journal_routing_tables_are_read_from_diagnostics():
    """`JOURNAL_ROUTED_FIELDS` and `JOURNAL_KIND_ROUTED_FIELDS` are built from the
    live `diagnostics` tables, not copied, so the guard cannot drift from the module
    it grades. Asserted rather than left to the comment: a future refactor that
    inlined the names would pass every other test here while quietly freezing the
    routing set."""
    for table in (
        diagnostics._JOURNAL_ALIAS_FIELDS,
        diagnostics._JOURNAL_DROP_FIELDS,
        diagnostics._JOURNAL_KEYLIST_FIELDS,
    ):
        assert set(table) <= JOURNAL_ROUTED_FIELDS
    expected_kind_routing: dict[str, frozenset[str]] = {}
    for table in JOURNAL_KIND_ROUTING_TABLES:
        for kind, row in table.items():
            expected_kind_routing[kind] = expected_kind_routing.get(kind, frozenset()) | frozenset(
                row
            )
    assert JOURNAL_KIND_ROUTED_FIELDS == expected_kind_routing
    # …and the kind-scoped names are deliberately NOT in the by-name union. This is
    # the assertion that would have caught the flattening: `target` routed by name
    # says the board-advance family is covered when `_scrub_entry` does not cover it.
    for table in JOURNAL_KIND_ROUTING_TABLES:
        for row in table.values():
            assert not set(row) & JOURNAL_ROUTED_FIELDS, (
                "a kind-scoped field name leaked into the by-name routed union; "
                "`_scrub_entry` consults its kind tables per kind, so a by-name "
                "claim about it is false on every other kind"
            )
    # `_JOURNAL_KIND_SCHEMAS` is the fail-closed schema table `_scrub_entry` consults,
    # and it was
    # coupled to this guard by prose alone: deleting its `preference-escalation` row
    # left every assertion here green while the fail-closed arm stopped running and
    # `customer="AcmeVault"` went back to shipping verbatim (measured). Read it here
    # so that cannot recur.
    schemas = diagnostics._JOURNAL_KIND_SCHEMAS
    assert schemas, (
        "`_JOURNAL_KIND_SCHEMAS` is empty — `_scrub_entry`'s fail-closed arm is now "
        "unreachable and every off-schema key falls back to `scrub_json`"
    )
    # The kind whose keys are LLM-authored is the reason the table exists, and
    # `JOURNAL_SPLAT_FIELDS`' comment for `engine.py::_review_and_commit` names this
    # table as the mechanism that covers that hole. Pinned rather than trusted: a
    # comment naming a mechanism that is not there is the failure this file exists
    # to refuse.
    assert "preference-escalation" in schemas
    assert (
        schemas["preference-escalation"]
        == JOURNAL_SPLAT_FIELDS[("engine.py", "_review_and_commit")]
    ), (
        "the declared schema and the splat inventory that cites it disagree — one "
        "of the two was edited alone"
    )
    for kind, names in schemas.items():
        # An empty declared set would collapse a record ENTIRELY, presence-marking
        # every field including the ones the record is read for. Never the intent:
        # a kind with nothing worth showing should not be in this table at all.
        assert names, f"{kind} declares an empty schema, which collapses its whole record"
        # Every declared name is accounted for on the guard's side too, so a schema
        # can neither name a field nothing produces nor quietly introduce one that
        # bypassed the routed/benign decision. `type` and `severity` reach the
        # journal only through the allowlisted splat, so the inventory there is
        # where they are declared.
        unaccounted = names - JOURNAL_ROUTED_FIELDS - JOURNAL_BENIGN_FIELDS
        unaccounted -= frozenset().union(*JOURNAL_SPLAT_FIELDS.values())
        assert unaccounted == set(), (
            f"{kind}'s declared schema names fields the guard does not account for: "
            f"{sorted(unaccounted)}"
        )
        # A declared name must not also be kind-aliased on the same kind: the alias
        # arm runs FIRST, so such a name would never reach the schema arm and the
        # declaration would be a dead letter that reads as live.
        assert not names & JOURNAL_KIND_ROUTED_FIELDS.get(kind, frozenset())

    # and the sets are disjoint: a routed name must never also be declared
    # benign, which would make the routing row unfalsifiable from this side.
    assert not JOURNAL_ROUTED_FIELDS & JOURNAL_BENIGN_FIELDS
    for kind, row in JOURNAL_KIND_ROUTED_FIELDS.items():
        assert not row & JOURNAL_KIND_BENIGN_FIELDS.get(kind, frozenset())
        assert not row & JOURNAL_BENIGN_FIELDS


def test_guard_actually_scanned_files():
    """Sanity: the scan walked a non-trivial number of files (catches a broken
    SRC root silently passing every assertion)."""
    assert len(_py_files()) > 20
