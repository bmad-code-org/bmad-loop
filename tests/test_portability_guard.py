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

Three later invariants ride the same machinery, each one previously held by
docstring prose alone:

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

If this test flags something unexpected, fix the source (route it through the
seam / a platform helper) rather than widening an allowlist.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path

import pytest

import bmad_loop
from bmad_loop import diagnostics, envvars
from bmad_loop.journal import (
    JOURNAL_FILE,
    SELF_MINTED_FIELDS,
    TASK_CYCLE_ARTIFACTS,
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
# ``(file, enclosing function)``. The re-arm transaction's own commit probe
# (``runs._rearm_commit_landed``) proves "did MY save_state land?" with nothing but
# ``(generation, phase)`` over the reloaded task, and that is a sufficient IDENTITY
# only under a sole-writer model: no engine advancing the task underneath, and one
# control command at a time. Its docstring argues that model from this enumeration.
#
# Prose cannot hold it. A third call site — or either existing gate deleted — leaves
# every test in the repo green while the probe's premise quietly becomes false, and
# the failure it opens is DW-79/DW-83's own shape: a spec left re-armed against a task
# the run still calls ESCALATED. So the enumeration is scanned instead of asserted.
#
# Deliberately NOT a lock and not a durable per-re-arm token: the spec's ``Never``
# forbids both (a lock only ``rearm_escalation`` takes excludes nobody; a token buys a
# precision ``save_state`` cannot honour). It forbids no guard, and this is the cheap
# half — it does not make overlapping callers safe, it makes the day someone adds one
# impossible to miss. Overlapping control commands stay out of the model, as DW-93.
REARM_ESCALATION_CALLERS = {
    ("cli.py", "cmd_resolve"),
    ("tui/app.py", "_do_rearm"),
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
        "condition",
        "contradiction",
        "converted",
        "count",
        "cycle",
        "cycles",
        "decision",
        "decisions",
        "deduped",
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
        "field",
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
        "items",
        "kept",
        "key",
        "ledger",
        "log_pos",
        "malformed",
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
        "original",
        "owed_after_implement",
        "phase",
        "platform",
        "plugin",
        "plugins",
        "policy_changed",
        "preserve_ref",
        "problem",
        # `question` is NOT here any more: it moved to `_JOURNAL_DROP_FIELDS`
        # (schema v3) once a one-token `decision-pending` question was shown to
        # ship verbatim. Left as a note rather than a silent deletion, because a
        # name leaving this set is the guard working — a benign declaration that
        # turned out to be wrong.
        "rc",
        "re_review_capped",
        "rearmed",
        "record",
        "redrive",
        "ref",
        "refiled",
        "refs",
        "refused",
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
        "strategy",
        "teardown_s",
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

# ``(file, enclosing function) -> the field names that actually flow through it`` for
# every ``journal.append(**name)`` whose keys are NOT statically resolvable. An
# unresolved splat is a HOLE in the inventory above — the guard cannot tell whether a
# new field arrived through it — so it fails loud and each hole is declared here with
# why it is one, rather than being silently skipped. A new splat site anywhere else
# reddens the guard until someone either makes its keys resolvable or adds a line here.
#
# All four are unresolvable for the same structural reason: the dict is not built
# from literals in the calling function. The VALUES are an inventory read off the
# producer, not an assertion the scan can check — they are what keeps the staleness
# check on ``JOURNAL_BENIGN_FIELDS`` from calling a splat-borne name dead, and they
# are the honest answer to "which names does this hole let through".
JOURNAL_SPLAT_ALLOW = {
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

# ``(file, enclosing function)`` of every journal write whose KIND is not a string
# literal. Kind-scoped routing (`JOURNAL_KIND_ROUTED_FIELDS` /
# `JOURNAL_KIND_BENIGN_FIELDS`) cannot be evaluated at such a call, so — exactly like
# an unresolvable splat — the site fails loud rather than being graded against a kind
# the scan had to guess.
#
# Declaring a position waives the KIND resolution and NOTHING else: a kind-scoped
# name at one of these sites is still refused, because nothing here can prove which
# kind it lands on.
JOURNAL_DYNAMIC_KIND_ALLOW = {
    # `kind` is a keyword parameter defaulting to `review-skipped`, flipped to
    # `review-skipped-awaiting-operator` by the park path. Journals `story_key` only.
    ("engine.py", "_skip_review_and_commit"),
    # `kind` is chosen by the two ledger-close call sites. Journals `story_key` and
    # `dw_ids` only.
    ("sweep.py", "_close_bundle_ledger_when_spec_status"),
    # Four writes, each an f-string over the `family` loop variable:
    # `attempt-preserve` / `attempt-preserve-dirty` × `-pruned` / `-prune-failed`.
    ("recovery_flow.py", "prune_preserve_refs"),
    # The forwarder passes its caller's `kind` straight through; every CALLER spells
    # a literal, and `JOURNAL_FORWARDERS` is what lets the scan read them there.
    ("plugins/bus.py", "_log"),
}

# The receivers a ``.append(...)`` call must hang off to be a journal write. Matched
# on the trailing name so `self.journal`, a bare `journal` parameter and
# `self._journal` (the plugin bus's optional handle) all resolve — the three
# spellings in the tree.
#
# ⚠️ STATED BOUND: a LOCALLY ALIASED handle is invisible. `j = self.journal` followed
# by `j.append(kind, customer_email=x)` produces no finding (verified by running it
# through `_scan_source`). No such site exists in the tree today, and resolving the
# binding would be `_call_aliases`' shape rather than a new idea — but the
# guard does not do it, and a reader must not assume it does.
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


def _is_journal_write(node: ast.AST, rel: str) -> bool:
    """Whether this node writes a journal entry — a ``<journal>.append(...)`` call in
    each of the three receiver spellings the tree uses (see ``JOURNAL_RECEIVERS``),
    or a call to one of this file's declared ``JOURNAL_FORWARDERS``.

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
    return (
        isinstance(node.func, ast.Attribute)
        and name == "append"
        and _called_name(node.func.value) in JOURNAL_RECEIVERS
    )


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
        if _is_journal_write(node, rel):
            fn_name = enclosing_names.get(id(node))
            first = node.args[0] if node.args else None
            kind = (
                first.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str)
                else None
            )
            if kind is None:
                findings.append(("journalkind", rel, node.lineno, line_at(node.lineno), fn_name))
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

    ``runs._rearm_commit_landed`` decides whether the re-arm transaction COMMITTED —
    and therefore whether to roll the spec back — from ``(generation, phase)`` over the
    reloaded task, nothing more. Those two conjuncts are a sufficient identity only
    while ``rearm_escalation`` is the sole writer of that run's ``state.json``, and that
    model is argued from this enumeration: two callers, each behind a liveness
    consultation, with no engine running. A third caller, or either gate deleted, makes
    the premise false — and the defect it reopens is DW-79/DW-83's own: a spec left
    flipped against a task the run still calls ESCALATED.

    Note what the gate does and does not establish. It proves the engine is not
    PROVABLY alive, not that it is dead: ``"alive"`` is refused outright, while
    ``"unknown"`` proceeds under ``--force`` in ``cmd_resolve`` and counts as blocking
    in the TUI only for a pid-backed run. So this grades the falsifiable half — that
    an earlier liveness decision BLOCKS fall-through before the call. The rest of the
    model (one control command at a time) is out of scope here and tracked as DW-93.

    ``cli.cmd_resume`` is deliberately absent: it writes this run's ``state.json``
    through ``_resume_paused_run``, so the sole-writer claim must account for it, but it
    never re-arms and so is not a call site. Listing it here would make the enumeration
    unfalsifiable in the direction that matters.

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
        "the count of runs.rearm_escalation call sites moved. That enumeration is what "
        "runs._rearm_commit_landed's (generation, phase) commit probe argues its "
        "sole-writer premise from — a new caller needs that docstring revisited (and "
        "DW-93 consulted), not this constant widened:\n"
        f"  scanned:  {sorted(sites.elements())}\n"
        f"  declared: {sorted(declared.elements())}"
    )
    ungated = [(rel, ln, txt) for _, rel, ln, txt, (_, gated) in findings if not gated]
    assert ungated == [], (
        "runs.rearm_escalation called without a preceding liveness refusal — the re-arm "
        "mutates persisted state for a run it must know is not being driven:\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in ungated)
    )


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
    ``journal.append("unit-merge-failed", target=branch)`` read as routed."""
    offenders: list[tuple[str, int, str, str]] = []
    for _, rel, ln, txt, (field, fn, kind) in findings:
        where = f"{fn}()" if fn else "<module>"
        if field is None:
            if (rel, fn) not in JOURNAL_SPLAT_ALLOW:
                offenders.append((rel, ln, txt, f"unresolvable **splat in {where}"))
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
        "declared benign — decide which it is: add a row to the right table in "
        "diagnostics.py if it carries an identifier, a path or free text, or list "
        "it in JOURNAL_BENIGN_FIELDS if it does not:\n"
        + "\n".join(f"  {rel}:{ln}: {what} — {txt.strip()}" for rel, ln, txt, what in offenders)
    )


def test_journal_kinds_are_literal_or_the_position_is_declared():
    """A journal write whose KIND is not a string literal cannot be graded against
    ``diagnostics``' kind-scoped routing, so it fails loud at an undeclared position
    — the same stance the guard takes on an unresolvable ``**splat``, and for the
    same reason: a site the scan cannot read must not read as clean.

    Seven such writes exist, at four positions, and all four journal only by-name
    routed fields today (``JOURNAL_DYNAMIC_KIND_ALLOW`` records which). Declaring one
    waives the kind resolution and nothing else: a kind-scoped name at one of them is
    still refused by the sibling assertion, because nothing can prove which kind it
    lands on.

    Ablation: empty ``JOURNAL_DYNAMIC_KIND_ALLOW`` and this reddens naming all four
    positions."""
    offenders = _journal_kind_offenders(_of("journalkind"))
    assert offenders == [], (
        "a journal write whose kind is not a string literal, at a position that has "
        "not declared itself one — pass a literal kind, or add the position to "
        "JOURNAL_DYNAMIC_KIND_ALLOW with what it journals:\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def test_journal_field_guard_actually_saw_the_producers():
    """The sibling assertion is an ABSENCE, so it is green both when every field is
    accounted for and when the scan stopped finding journal writes at all. This is
    the half that cannot be: empty the inventories and the guard must name a real
    producer, which proves the scan reached them.

    Also pins the three shapes the scan must not lose — the routed names really are
    produced (so ``JOURNAL_ROUTED_FIELDS`` is coupled to live producers rather than
    to a copied list), every declared splat hole still exists (so a stale
    ``JOURNAL_SPLAT_ALLOW`` entry cannot sit there sanctioning nothing), and every
    declared BENIGN name still has a producer.

    That last one is the direction nothing held before. The benign inventory is a
    pre-approval list, so a name whose producer was deleted does not just sit there
    inertly: it pre-approves a future, unrelated field that happens to reuse the
    spelling, with no one making the decision the inventory exists to force. The two
    exemptions are the names no CALL can spell — what ``Journal.append`` mints itself
    and what arrives through a declared splat hole."""
    findings = _of("journalfield")
    produced = {field for _, _, _, _, (field, _, _) in findings if field is not None}
    assert len(produced) > 100, f"the scan found only {len(produced)} journal fields"
    assert produced & JOURNAL_ROUTED_FIELDS, "no routed field has a static producer"
    holes = {(rel, fn) for _, rel, _, _, (field, fn, _) in findings if field is None}
    assert holes == set(JOURNAL_SPLAT_ALLOW), (
        "JOURNAL_SPLAT_ALLOW no longer matches the unresolvable splats in the tree; "
        f"undeclared: {sorted(holes - set(JOURNAL_SPLAT_ALLOW))}, "
        f"stale: {sorted(set(JOURNAL_SPLAT_ALLOW) - holes)}"
    )
    unscannable = JOURNAL_SELF_MINTED_FIELDS.union(*JOURNAL_SPLAT_ALLOW.values())
    stale = JOURNAL_BENIGN_FIELDS - produced - unscannable
    assert stale == set(), (
        "JOURNAL_BENIGN_FIELDS names fields no producer writes any more — a benign "
        "entry outlives its producer as a standing pre-approval for the next field "
        "that reuses the name. Delete them, or record where they now come from in "
        f"JOURNAL_SPLAT_ALLOW / JOURNAL_SELF_MINTED_FIELDS: {sorted(stale)}"
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


# The journal detector's probe matrix, as `(label, source, expected)` where
# `expected` is the exact set of field names the scan must extract — `None` standing
# for an unresolvable splat. Asserting the SET rather than "something was found" is
# what makes a partial splat resolution fail here instead of quietly under-reporting.
JOURNAL_FIELD_PROBES = [
    # The three receiver spellings in the tree.
    ("self-journal", 'self.journal.append("k", story_key=s, patch=p)\n', {"story_key", "patch"}),
    ("bare-journal", 'journal.append("k", branch=b)\n', {"branch"}),
    ("private-journal", "self._journal.append(kind, plugin=name)\n", {"plugin"}),
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
    # `JOURNAL_SPLAT_ALLOW`'s comment for `engine.py::_review_and_commit` names this
    # table as the mechanism that covers that hole. Pinned rather than trusted: a
    # comment naming a mechanism that is not there is the failure this file exists
    # to refuse.
    assert "preference-escalation" in schemas
    assert (
        schemas["preference-escalation"] == JOURNAL_SPLAT_ALLOW[("engine.py", "_review_and_commit")]
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
        unaccounted -= frozenset().union(*JOURNAL_SPLAT_ALLOW.values())
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
