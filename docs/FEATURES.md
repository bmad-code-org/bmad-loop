# bmad-loop — Features

## Overview

bmad-loop drives a BMAD sprint unattended: a deterministic Python loop launches coding-CLI sessions in a terminal multiplexer, checks their work itself, and commits it. This file catalogs what the tool does, one entry per user-facing capability, grouped by category. Each entry lists its stability, the guarantees you can rely on, where they are tested, and where to read more. For the narrative overview see the [README](../README.md); for installation see the [setup guide](setup-guide.md).

**Stability levels:**

- **stable** — shipped, covered by tests, and kept compatible.
- **in-progress** — shipped and usable, but its surface is still growing or may change.
- **experimental** — available behind an opt-in or on a limited platform; expect rough edges.
- **planned** — designed or partly scaffolded, not yet functional; see the [roadmap](ROADMAP.md).

**Maintaining this file:** add one `###` entry per user-facing capability, never per internal mechanism. Every property must be a short, testable statement that holds in the current code, and its **Tested by** files must cover it. No issue or ledger references and no history: mechanics live in [reference/](reference/cli.md), the guides, and module docstrings; change history lives in the [CHANGELOG](../CHANGELOG.md).

## Core loop

### Deterministic story pipeline

- **Stability:** stable
- **Description:** Each story runs dev, verify, a review session when triggered, verify, then an orchestrator commit, driven by plain Python with no LLM in the control loop. LLMs work only inside disposable coding-CLI sessions, and bmad-loop is the only writer of story rows in `sprint-status.yaml` while a run is in flight.
- **Properties:**
  - Per story, the engine runs a dev session, verifies its artifacts, runs a bounded review loop when triggered, runs verify commands, then commits the work itself.
  - Phase moves are checked against one transition table; an illegal move raises instead of advancing the story.
  - Board writes never move a row backward. The only exception is done → awaiting-operator, and only when the caller explicitly opts in.
  - Board updates are serialized across processes, so two concurrent bmad-loop processes cannot interleave an advance.
  - A session completes only on a hook Stop event or on window death, never because of what the model writes in its reply.
  - In tickets mode there is no board: the build skill writes a plan's status during its own session, every other status write during a run is bmad-loop's, through `tickets.py mark`, and the plan reaches `done` only after verify and review.
- **Config:** `bmad-loop run --epic N`, `bmad-loop run --story KEY`, `bmad-loop run --max-stories N`, `bmad-loop run --dry-run`; `[limits] max_dev_attempts`
- **Tested by:** `tests/test_engine.py`, `tests/test_statemachine.py`, `tests/test_sprintstatus_advance.py`, `tests/test_tickets_engine.py`
- **Docs:** [CLI reference: run](reference/cli.md#run), [Configuration: [limits]](reference/configuration.md#limits), [Run state reference](reference/run-state.md)

### Stories mode

- **Stability:** stable
- **Description:** An opt-in second story source. It drives the same pipeline from a `bmad-spec` `stories.yaml` instead of `sprint-status.yaml`, and dispatches each story by spec folder and story id.
- **Properties:**
  - Each story is dispatched as `/<dev skill> Spec folder: <folder>. Story id: <id>.`, and its spec is read back from `<folder>/stories/<id>-*.md`.
  - Stories run in list order. Done stories are skipped, and interrupted non-terminal stories are re-dispatched.
  - A blocked, sentinel, ambiguous or unknown-status story stops the scan and pauses the run for resolve; later stories never leapfrog it.
  - If the installed dev skill lacks folder+id dispatch, the run aborts before spawning any session. Sprint mode does not need this support.
  - On re-arm, a pre-planning sentinel spec is deleted, and a copy is kept under the run's `sentinels/` directory.
- **Config:** `[stories] source`, `[stories] spec_folder`; `bmad-loop run --spec FOLDER`, `bmad-loop run --dry-run --spec FOLDER`
- **Tested by:** `tests/test_stories.py`, `tests/test_stories_engine.py`, `tests/test_stories_e2e.py`, `tests/test_install.py`
- **Docs:** [Configuration: [stories]](reference/configuration.md#stories), [CLI reference: run](reference/cli.md#run), [Recovering paused runs](escalations-guide.md#resolving-an-escalation)

### Ticket-tree mode

- **Stability:** experimental
- **Description:** An opt-in third story source for BMAD v7 projects. It drives the same pipeline from the active initiative's ticket tree through the project's own `tickets.py`, dispatching each ticket as `/<dev skill> ticket <ref>` in initiative build order, and, under the default `mark_done`, marks a verified ticket `done` itself.
- **Properties:**
  - A run is pinned to `[core] active_initiative` at start; a changed initiative pauses the run at `ticket-tree` instead of building another initiative's ticket.
  - A hitl ticket pauses at `ticket-hitl` and a ticket that needs refining at `ticket-refine`, both before any session runs for it.
  - bmad-loop writes a plan's `status` and `blocked_*` only through `tickets.py mark`, under a lock, read back afterwards and checked against one plan transition table; it never writes `dropped` or an epic file.
  - Tickets this run did not start are never dispatched or marked.
  - A store tracked in the checkout is marked inside the unit's commit; a shared store's mark is latched and made after the code lands, and a store with its own repository gets a plan-only commit.
  - `run` refuses a tracker store and the multi-repo workspace layout before creating a run directory; resume and resolve refuse a store whose layout changed.
  - When a run without `--story` finishes an epic, it runs the retrospective and pauses once at `epic-boundary` for a person to close the epic; a `rejected` verdict pauses every later pick while the epic stays open, and an epic gated on an open one waits for its closure.
- **Config:** `[stories] source = "tickets"`, `[core] active_initiative`, `[tickets] mark_done`, `[tickets] thorough_review`, `[gates] retrospective`; `bmad-loop run --epic ID|SLUG`, `--story REF`, `--dry-run`; `bmad-loop confirm <ref>`
- **Tested by:** `tests/test_tickets_engine.py`, `tests/test_tickets_source.py`, `tests/test_tickettree.py`, `tests/test_ticketmodel.py`, `tests/test_ticketstore.py`, `tests/test_statemachine.py`, `tests/test_tickets_e2e.py`
- **Docs:** [Ticket-tree mode guide](tickets-guide.md), [Setup: BMAD v7](setup-guide.md#installing-on-bmad-v7-bmad-setup), [Configuration: [tickets]](reference/configuration.md#tickets), [Resolve in tickets mode](escalations-guide.md#resolve-in-tickets-mode), [TUI: tickets mode](tui-guide.md#tickets-mode)

### Trust-nothing verification

- **Stability:** stable
- **Description:** After every session, bmad-loop checks the work itself: what is on disk, the git state, and your own test and lint commands. Optional environment probes separate a broken environment from broken code.
- **Properties:**
  - A dev result is accepted only if the spec exists with the expected workflow tag and status, the board matches in sprint mode, and code changed (operator parks and plan-only legs excepted).
  - `[verify] commands` run in the code's git root, which is the unit's worktree under worktree isolation, after dev, fix and review sessions.
  - With probes configured, a failing, timed-out or unstartable probe stops verification before any `[verify]` command runs.
  - `bmad-loop validate --probes` runs every probe, not stopping at the first failure, and fails validate if any probe fails.
  - A `repo_root` key in `_bmad/bmm/config.yaml` moves verify commands and git checks to a monorepo's code root.
- **Config:** `[verify] commands`, `[verify] command_timeout_min`, `[verify] env_fault_rc`, `[environment] probes`, `[environment] probe_timeout_s`; `bmad-loop validate --probes`
- **Tested by:** `tests/test_verify.py`, `tests/test_engine.py`, `tests/test_cli.py`, `tests/test_bmadconfig.py`
- **Docs:** [Configuration: [verify]](reference/configuration.md#verify), [Configuration: [environment]](reference/configuration.md#environment), [Environment faults](escalations-guide.md#environment-faults), [Re-verify kept work](escalations-guide.md#resolve-reverify)

### Fresh-context review

- **Stability:** stable
- **Description:** When the dev pass flags a follow-up (or on every story with `trigger = "always"`), a separate review session re-reviews the finished spec without the implementer's context, before anything is committed. The review loop is bounded and you can turn it off.
- **Properties:**
  - The review runs as its own session, apart from the dev session, and can use a different CLI and model via `[adapter.review]`.
  - With `trigger = "recommended"`, the review session runs only when the dev pass flags a follow-up; `always` reviews every story.
  - `[review] enabled = false` skips the review session. The verify commands still gate the commit.
  - Review passes are capped at `max_review_cycles`. A pass that keeps recommending its own follow-up is honored at most `max_followup_reviews` times, then the story converges.
  - If a clean review leaves verify commands failing, a fix session repairs them and the story is reviewed again.
- **Config:** `[review] enabled`, `[review] trigger`, `[limits] max_review_cycles`, `[limits] max_followup_reviews`, `[adapter.review]`
- **Tested by:** `tests/test_engine.py`, `tests/test_escalation.py`
- **Docs:** [Configuration: [review]](reference/configuration.md#review), [Configuration: [limits]](reference/configuration.md#limits), [Configuration: [adapter.review]](reference/configuration.md#adapterreview)

## Resilience

### Bounded retries and attempt preservation

- **Stability:** stable
- **Description:** Failed dev and review sessions retry within fixed budgets, silent sessions are nudged and then classified stalled, and any attempt work that is reset is first parked on git refs you can restore from.
- **Properties:**
  - A failed dev attempt retries until `[limits] max_dev_attempts` (default 2); a failed review retries until `max_review_cycles` (default 3).
  - A dev attempt that fails only its verify commands keeps its tree, and the failing output is handed to the retry as feedback.
  - A session silent for `dev_stall_grace_s` (default 600) gets up to `dev_stall_nudges` wake nudges, then ends `stalled` and is charged as a failed attempt.
  - Every dev and review session ends as `timeout` after `[limits] session_timeout_min` (default 90).
  - A dev session that did no work, or any session parked on a prompt only a human should answer, pauses the run instead of being charged.
  - Before an attempt is reset, its commits go to an `attempt-preserve/<run>-<tip8>` branch and uncommitted work to a `refs/attempt-preserve-dirty/*` snapshot.
- **Config:** `[limits] max_dev_attempts`, `[limits] max_review_cycles`, `[limits] session_timeout_min`, `[limits] dev_stall_grace_s`, `[limits] dev_stall_nudges`, `[limits] dev_stall_nudges_cap`, `[scm] preserve_keep`
- **Tested by:** `tests/test_escalation.py`, `tests/test_engine.py`, `tests/test_generic_tmux.py`, `tests/test_verify.py`, `tests/test_recovery_flow.py`
- **Docs:** [Preserved attempts](escalations-guide.md#preserved-attempts), [[limits] reference](reference/configuration.md#limits), [[scm] reference](reference/configuration.md#scm)

### Plateau-defer

- **Stability:** stable
- **Description:** A story that uses up its retry budget without a CRITICAL reason is deferred and skipped, and the run moves on to the next story.
- **Properties:**
  - A story that exhausts `max_dev_attempts` or `max_review_cycles` without a CRITICAL reason is deferred and the run continues.
  - With `[review] on_timeout = "defer"`, a timed-out, stalled or over-budget review defers at once instead of retrying.
  - Each defer journals `story-deferred` and sends a `story deferred: <story>` notice naming where the attempt's work survives.
  - In place with `[scm] rollback_on_failure = false` (default) and attempt work in the tree, the tree is left untouched and the run pauses for manual rollback.
  - A re-drive of a human-resolved CRITICAL escalation that exhausts its budget re-escalates instead of deferring.
- **Config:** `[limits] max_dev_attempts`, `[limits] max_review_cycles`, `[review] on_timeout`, `[scm] rollback_on_failure`
- **Tested by:** `tests/test_escalation.py`, `tests/test_engine.py`, `tests/test_recovery_flow.py`
- **Docs:** [Plateau-defer](escalations-guide.md#plateau-defer), [Manual rollback pause](escalations-guide.md#manual-rollback-pause), [[limits] reference](reference/configuration.md#limits)

### Typed escalations and resolve

- **Stability:** stable
- **Description:** Sessions report escalations as CRITICAL, which pauses the run for a human, or PREFERENCE, which is only journaled. `bmad-loop resolve` clears a CRITICAL pause by re-arming the story, re-verifying work that was kept, or adopting a kept branch.
- **Properties:**
  - A CRITICAL escalation marks the story `escalated`, sends a `CRITICAL escalation: <story>` notice and pauses the run at `escalation`.
  - A PREFERENCE escalation is journaled as `preference-escalation` and the run continues.
  - `--abandon` (in-progress) closes an escalated replay run unmerged, keeping its branch and parking dirty work on a preserve ref.
  - Re-arming resets the story to `pending` with fresh dev-attempt and review-cycle budgets.
  - `--reverify` re-runs `[verify]` on a deferred or environment-fault story's kept work without a dev session; on a finished run it replays a kept worktree unit in a new run.
  - In tickets mode the re-arm moves the ticket's plan through `tickets.py mark` and clears its `blocked_*` fields; a refused move puts the plan back byte for byte.
- **Config:** `bmad-loop resolve --story`, `--no-interactive`, `--restore-patch`, `--adopt-branch`, `--reverify`, `--resume` / `--no-resume`, `--force`, `--abandon`
- **Tested by:** `tests/test_engine.py`, `tests/test_cli.py`, `tests/test_escalation.py`, `tests/test_resolve.py`
- **Docs:** [Escalations](escalations-guide.md#escalations), [Resolving an escalation](escalations-guide.md#resolving-an-escalation), [Replay a finished run's kept unit](escalations-guide.md#resolve-reverify-finished), [Abandon a replay run](escalations-guide.md#abandon-a-replay-run), [Resolve in tickets mode](escalations-guide.md#resolve-in-tickets-mode), [resolve CLI reference](reference/cli.md#resolve)

### Human-only actions

- **Stability:** stable
- **Description:** A story whose acceptance criteria need an action no agent can take, such as buying a domain, commits the agent's work, parks at `awaiting-operator`, and waits for `bmad-loop confirm`.
- **Properties:**
  - In sprint mode with `[operator] enabled` (default on), a story owing human-only actions commits its agent work, parks at `awaiting-operator`, and the run continues.
  - In tickets mode the park commits with the ticket's plan marked `blocked` (reason `awaiting operator: <actions>`), never `done`; `confirm <ref>` marks it `done`, or `built` under `[tickets] mark_done = "never"`, through `tickets.py mark`.
  - `bmad-loop confirm --list` shows each parked story and what it owes; `--json` emits it as a machine-readable document.
  - A confirmation adds an `## Operator Confirmation` section, moves spec and board to `done`, and commits.
  - `confirm --reverify` runs the environment probes and `[verify]` commands first; a failure blocks the confirmation.
  - `[operator] on_review_demotion = "park"` lets a review demote a `done` story to `awaiting-operator` (sprint and tickets mode); the default `escalate` rejects the demotion.
- **Config:** `[operator] enabled`, `[operator] on_review_demotion`, `bmad-loop confirm --list`, `bmad-loop confirm --yes`, `bmad-loop confirm --reverify`, `bmad-loop confirm --json`
- **Tested by:** `tests/test_engine.py`, `tests/test_cli.py`, `tests/test_operatoractions.py`, `tests/test_tickets_engine.py`
- **Docs:** [Human-only actions](escalations-guide.md#human-only-actions), [confirm CLI reference](reference/cli.md#confirm), [[operator] reference](reference/configuration.md#operator)

### Environment-fault pause

- **Stability:** stable
- **Description:** Failures caused by the environment rather than the code, such as a dead database, a missing binary or an unreachable provider, pause the run without charging the story an attempt.
- **Properties:**
  - A failing `[environment] probes` command before a session launch pauses the run at `environment`; nothing is started or charged.
  - `bmad-loop resume` re-runs the probes; if they pass, the withheld session launches with no rollback, and otherwise the run pauses again unchanged.
  - A verify command exiting 126, 127 or `[verify] env_fault_rc` is an environment fault, not a code failure.
  - A non-completed session whose log tail matches its profile's `env_fault_patterns` pauses instead of spending a dev attempt.
  - opencode is classified from model-free files only: the server's stdout, plus on OpenCode 2.x the adapter-written `provider-errors.log` (one line per provider-error event). Its 2.x patterns cover the `provider.rate-limit`, `provider.quota` and `provider.transport` error types.
  - A pattern match is overruled when the session's tracked token usage is nonzero; that session is charged like any failure.
- **Config:** `[environment] probes`, `[environment] probe_timeout_s`, `[verify] env_fault_rc`; profile key `env_fault_patterns`
- **Tested by:** `tests/test_engine.py`, `tests/test_escalation.py`, `tests/test_env_fault_patterns.py`, `tests/test_env_fault.py`, `tests/test_generic_tmux.py`, `tests/test_opencode_http.py`
- **Docs:** [Environment faults](escalations-guide.md#environment-faults), [[environment] reference](reference/configuration.md#environment), [[verify] reference](reference/configuration.md#verify), [Profile field reference](adapter-authoring-guide.md#cliprofile)

### Resumable runs and graceful stop

- **Stability:** stable
- **Description:** A paused, stopped or interrupted run picks up where it left off with `bmad-loop resume`. A run can be stopped at once, or gracefully after the item in flight commits.
- **Properties:**
  - `bmad-loop resume` continues a paused, stopped or interrupted run; a finished run, or one whose engine is alive, is refused.
  - A session that completed before the interruption is not re-run; its recorded result is used on resume.
  - `bmad-loop stop` signals the engine and kills the agent session; a wedged engine is force-killed after 10 s only while its process identity still matches.
  - `stop --graceful` lets the in-flight story or bundle finish through commit, suppresses pending auto-sweeps, then stops resumable.
  - `resume --accept-baseline` keeps commits above an in-place story's baseline instead of parking and resetting them, for that resume only.
  - A run's two durable records, `state.json` and `journal.jsonl`, are written only into the run directory recorded at its creation: a link at, or replacement of, that directory refuses the write, and a run with no usable record writes one `journal-unpinned` row.
- **Config:** `bmad-loop resume --accept-baseline`, `bmad-loop stop --graceful`, `bmad-loop stop --cancel-graceful`, `[adapter] cleanup_session_on_finish`
- **Tested by:** `tests/test_engine.py`, `tests/test_cli.py`, `tests/test_runs.py`, `tests/test_journal.py`
- **Docs:** [Stopping a run](reference/run-state.md#stopping-a-run), [Derived run status](reference/run-state.md#derived-run-status), [Run inventory](reference/run-state.md#run-inventory), [resume CLI reference](reference/cli.md#resume), [stop CLI reference](reference/cli.md#stop)

## Human control

### Gates and checkpoints

- **Stability:** stable
- **Description:** Run-global gates pause an unattended run at epic boundaries or after each story's dev pass so a human can review. Per-story checkpoints in stories and tickets mode pause before implementation or after the commit. `bmad-loop resume <run-id>` continues the run.
- **Properties:**
  - `[gates] mode = "none"` never pauses at a gate. `per-epic` (the default) pauses at each epic boundary. Any other value fails policy load.
  - Stories mode has no epics, so `per-epic` never pauses there.
  - `per-story-spec-approval` also pauses each story after its verified dev pass and before review and commit. On resume the story goes straight to review.
  - In stories mode, a `stories.yaml` entry with `spec_checkpoint: true` pauses after planning, before any code. On resume the implementation leg runs.
  - `done_checkpoint: true` pauses after the story commits. It is skipped for the last story to dispatch, at `--max-stories`, or when a graceful stop is pending.
  - In tickets mode, an entry's `plan_checkpoint` follows the BMAD v7 contract. A refined ticket pauses before dispatch, and resuming approves its story file. An unrefined ticket pauses after a `Halt after planning.` leg, and resuming dispatches it again on the uncommitted plan. `done_checkpoint` pauses after the ticket commits, with the same skips as stories mode.
  - `[gates] on_escalation` is reserved. It is parsed but has no effect, and CRITICAL escalations always pause whatever its value.
- **Config:** `[gates] mode`, `[gates] on_escalation` (reserved); stories.yaml per-entry `spec_checkpoint` / `done_checkpoint`; ticket-entry `plan_checkpoint` / `done_checkpoint`; `bmad-loop resume <run_id>`
- **Tested by:** `tests/test_engine.py`, `tests/test_stories_engine.py`, `tests/test_checkpoints.py`, `tests/test_tickets_engine.py`, `tests/test_policy.py`, `tests/test_settings_schema.py`
- **Docs:** [Configuration: [gates]](reference/configuration.md#gates), [Where a pause shows up](escalations-guide.md#where-a-pause-shows-up), [Reviewing a paused run in the TUI](tui-guide.md#reviewing-a-paused-run-p), [Tickets checkpoints](tickets-guide.md#checkpoints), [bmad-loop resume](reference/cli.md#resume)

### Retrospective handling

- **Stability:** in-progress
- **Description:** When an epic finishes, bmad-loop can stay silent, suggest a retrospective, or run a headless `/bmad-retrospective` session and commit its output. In sprint mode the retro's verdict is reported to you but never gates the run; in tickets mode the run pauses for you to close the epic.
- **Properties:**
  - `[gates] retrospective = "notify"` (the default) sends one "retrospective suggested" notice per finished epic. `never` sends nothing.
  - `auto` runs `/bmad-retrospective -H <epic>` on the `[adapter.retro]` role (falling back to the base `[adapter]` when that is unset), before the per-epic auto-sweep.
  - An auto retro succeeds only if the session completed, result.json says done, the board reads done and a retro doc exists. It is then committed as `chore(retro): epic N retrospective`.
  - A failed or refused auto retro (unless already done) tells you to run it by hand and never fails the run. If it leaves uncommitted changes, the run pauses at the epic boundary.
  - The retro doc's verdict is reported in the done notice. A `rejected` verdict sends an extra attention notice, and the run continues.
  - Tickets mode keys the epic by slug. Once this run finishes an epic, `auto` runs `/bmad-retrospective -H <epic-slug>` and commits only `<epic-slug>-retrospective.md`; a valid document already there counts as done. Whatever the mode, the run records the verdict and pauses at `epic-boundary` for you to close the epic with bmad-ticket. A `rejected` verdict pauses again at every pick while the epic stays open; any other pauses once.
- **Config:** `[gates] retrospective`, `[adapter.retro] name` / `model` / `effort` / `extra_args`
- **Tested by:** `tests/test_engine.py`, `tests/test_policy.py`, `tests/test_tickets_engine.py`, `tests/test_tickets_e2e.py`
- **Docs:** [Escalations: epic closure in tickets mode](escalations-guide.md#epic-closure-in-tickets-mode), [Tickets guide: retrospective and closure](tickets-guide.md#epic-retrospective-and-closure), [Configuration: [gates]](reference/configuration.md#gates), [Configuration: [adapter.retro]](reference/configuration.md#adapterretro), [Pointing retrospectives at run journals](setup-guide.md#pointing-retrospectives-at-run-journals), [Roadmap: automate epic retro action items](ROADMAP.md#automate-epic-retro-action-items)

## Deferred work

### Ledger sweeps

- **Stability:** stable
- **Description:** `bmad-loop sweep` triages the deferred-work ledger in a fresh session, then builds the buildable entries as bundles through the normal dev → verify → review → commit pipeline. It can repeat on newly deferred work, archive closed entries, and run automatically from `bmad-loop run`.
- **Properties:**
  - A bundle's review gate passes only when every ledger entry it owns is `status: done` on disk.
  - With `--repeat`, the sweep re-triages after each cycle and ends with a `sweep-repeat-done` journal entry naming its `stop_cause`.
  - `--archive` moves dated closed entries to `deferred-work-archive.md`, leaves an id-preserving `status: done` stub, and refuses while any run may be live.
  - Legacy (non-`DW-<n>`) entries are migrated automatically at a sweep's start; a rewrite that drops a `gate:` token or changes a status fails validation.
  - `[sweep] auto` starts a child sweep per epic or at run end, at most once per trigger; a failed child never fails the parent run.
  - On BMAD v7 a sweep reads no sprint board, keeps its ledger and bundle plans in the initiative folder, closes bundles at `built`, and is pinned to the initiative it started on.
- **Config:** `[sweep] auto`, `[sweep] max_bundles`, `[sweep] repeat`, `[sweep] max_cycles`, `[sweep] max_triage_attempts`; `bmad-loop sweep --dry-run`, `--only DW-ID,...`, `--min-severity`, `--max-bundles`, `--repeat`/`--no-repeat`, `--max-cycles`, `--no-prompt`, `--decisions-only`, `--archive`, `--before DATE`
- **Tested by:** `tests/test_sweep.py`, `tests/test_cli.py`, `tests/test_deferredwork.py`, `tests/test_engine.py`
- **Docs:** [Sweep guide](sweep-guide.md), [Repeat mode](sweep-guide.md#repeat-mode), [Legacy-ledger migration](sweep-guide.md#legacy-ledger-migration), [Sweeps on BMAD v7](sweep-guide.md#sweeps-on-bmad-v7), [CLI: sweep](reference/cli.md#sweep), [Configuration: [sweep]](reference/configuration.md#sweep)

### Deferred-work decisions

- **Stability:** stable
- **Description:** A sweep's triage can raise questions only a human can answer. You answer them during an attended sweep, or afterwards with `bmad-loop decisions` or `d` in the TUI; each answer builds, closes, or keeps the entry open.
- **Properties:**
  - `bmad-loop decisions` offers only ids still open, surfaced by a past triage, and without a usable stored answer.
  - An out-of-band `close` marks the ledger entry done immediately; `build` and `keep-open` are saved to `.bmad-loop/decisions.json` (on BMAD v7, the active initiative's `decisions-<initiative>.json`) for the next sweep.
  - A sweep reads its run store first and uses a project-store answer only for an id this triage asks that the run store has not answered.
  - A keep-open answer whose option is gone or re-authored is dropped as `stale-option`, notified, and removed from the project store.
  - An unattended sweep (`--no-prompt`) asks nothing, builds only decision-free bundles, and notifies `N deferred-work decisions pending`.
- **Config:** `bmad-loop decisions`, `bmad-loop decisions --list`, `bmad-loop decisions --json`, `bmad-loop sweep --no-prompt`, `bmad-loop sweep --decisions-only`
- **Tested by:** `tests/test_decisions.py`, `tests/test_sweep.py`, `tests/test_cli.py`, `tests/test_tui_app.py`
- **Docs:** [Answering decisions](sweep-guide.md#answering-decisions), [Project store and run store](sweep-guide.md#project-store-and-run-store), [Stale answers](sweep-guide.md#stale-answers), [CLI: decisions](reference/cli.md#decisions), [TUI: answering missed decisions](tui-guide.md#answering-missed-decisions-d)

### Story closure and hard gates

- **Stability:** stable
- **Description:** A story can declare the ledger entries it closes with `closes_deferred`, and a ledger entry can block named stories with a `gate:` line until it is done.
- **Properties:**
  - At a story's commit, each `closes_deferred` id becomes `status: done <date>` with `resolution: resolved by story <key>`.
  - A story that defers or escalates closes none of its declared entries.
  - A story named by an entry's `gate:` line pauses at stage `story-gate` until that entry is `status: done`; any other status still gates.
  - A gate token matches a key it equals or prefixes at a boundary: `3-2` gates `3-2-login` and `3-2a-x`, not `3-20-x`.
  - `bmad-loop validate` fails `deferred.hard-gate` for a gated actionable story and warns on unknown or malformed `closes_deferred` ids.
- **Config:** `closes_deferred:` in story-spec frontmatter or a `stories.yaml` entry; `gate:` line in a ledger entry; `bmad-loop validate`
- **Tested by:** `tests/test_engine.py`, `tests/test_deferredwork.py`, `tests/test_cli.py`, `tests/test_stories_engine.py`
- **Docs:** [Story-declared closure](sweep-guide.md#story-declared-closure-closes_deferred), [Hard gates](sweep-guide.md#hard-gates), [CLI: validate](reference/cli.md#validate)

### Retro action items

- **Stability:** in-progress
- **Description:** A fresh sweep files the action items a retrospective recorded in sprint-status.yaml into the deferred-work ledger, where they are triaged like any other entry.
- **Properties:**
  - A fresh sweep files each not-`done`, id-keyed `action_items` entry from sprint-status.yaml as a low-severity ledger entry before triage.
  - Ingest runs only at a fresh sweep's start: never on `--dry-run`, on a resumed sweep, or while the ledger is in doubt.
  - Each item is filed once: its `retro action item <id>` origin dedupes against entries of any status, archived stubs included.
  - An unreadable board or a non-list `action_items` is journaled `sweep-retro-ingest-unavailable` and the sweep proceeds.
  - A BMAD v7 project has no board: the ingest reads nothing and journals `sweep-retro-ingest-unavailable` with `ingest_cause="no-board"`.
  - An auto-sweep that did not run is reported: `bmad-loop status` prints `auto-sweep not run`; `status --json` and `diagnose` list `sweeps_refused`.
- **Config:** `action_items:` list in sprint-status.yaml (written by bmad-retrospective); `[sweep] auto`
- **Tested by:** `tests/test_sweep.py`, `tests/test_sprintstatus.py`, `tests/test_cli.py`
- **Docs:** [What gets published](sweep-guide.md#what-gets-published), [Auto-sweep](sweep-guide.md#auto-sweep), [Roadmap: retro action items](ROADMAP.md#automate-epic-retro-action-items)

## Isolation

### Worktree isolation

- **Stability:** stable
- **Description:** Opt-in mode that runs each story (and each sweep bundle) in its own git worktree and branch, then merges the result back into a target branch locally, so your main checkout is never edited mid-session.
- **Properties:**
  - Off by default: with `isolation = "none"` work happens in place on the checked-out branch; only `none` and `worktree` are accepted.
  - Each unit gets a `bmad-loop/<run-id>/<story>` branch (`bmad-loop/<run-id>` with `branch_per = "run"`), mounted under `.bmad-loop/runs/<run-id>/worktrees/`.
  - A gitignored sprint board is seeded into the unit, and only the story's own status advance is carried back to the main board.
  - A ticket store the code checkout does not track is shared: the unit reads and marks the project's store in place, never a copy.
  - A finished unit merges into the target branch with `ff`, `merge` or `squash`; on success the worktree is removed and, by default, the branch deleted.
  - A deferred unit's diff is saved to `.bmad-loop/runs/<run-id>/failed/<unit>/changes.patch`; its worktree and branch stay with `keep_failed`. An escalated unit's worktree stays mounted.
  - A failed merge-back keeps the unit's branch and worktree and escalates; if the main checkout is off the target branch, the run pauses (`merge-target-not-checked-out`).
- **Config:** `[scm] isolation`, `[scm] branch_per`, `[scm] target_branch`, `[scm] merge_strategy`, `[scm] delete_branch`, `[scm] keep_failed`, `[scm] seed_adapter_defaults`, `[scm] worktree_seed`, `[scm] failed_diff_max_mb`, `[scm] failed_diff_unlimited`, `[scm] commit_message_template`; `bmad-loop resolve <run-id> --adopt-branch`
- **Tested by:** `tests/test_engine_worktree.py`, `tests/test_sweep.py`, `tests/test_policy.py`
- **Docs:** [[scm] settings](reference/configuration.md#scm), [Worktree merge-back failures](escalations-guide.md#worktree-merge-back-failures), [Adopt the kept branch](escalations-guide.md#adopt-the-kept-branch), [Run directory](reference/run-state.md#run-directory)

### Parallel units

- **Stability:** planned
- **Description:** Running several units at once, each in its own worktree, with merge-back serialized. The config key exists and is validated, but runs execute one unit at a time.
- **Properties:**
  - `[scm] max_parallel` accepts integers ≥ 1; 0 or less fails policy load.
  - Any value above 1 is clamped to 1, so units always run and merge back one at a time.
  - The key is hidden from the TUI settings editor and from the configuration reference until the scheduler lands.
- **Config:** `[scm] max_parallel`
- **Tested by:** `tests/test_policy.py`, `tests/test_settings_schema.py`
- **Docs:** [Roadmap: parallel unit execution](ROADMAP.md#parallel-unit-execution-scm-max_parallel)

## Agents & platforms

### Multi-CLI profiles

- **Stability:** stable
- **Description:** bmad-loop can drive several coding CLIs: claude, codex, gemini, copilot, antigravity and opencode. What differs between them lives in declarative TOML profiles, so adding a CLI that reuses an existing hook dialect needs a profile file, not Python. Antigravity is experimental.
- **Properties:**
  - Bundled profiles: claude, codex, gemini, copilot, antigravity and opencode-http. The names `opencode` and `claude-code-tmux` resolve as aliases.
  - A `.bmad-loop/profiles/*.toml` file whose `name` matches a bundled profile overrides it; a new `name` adds a CLI. Project files win over packaged and entry-point profiles.
  - opencode runs as a headless `opencode serve` over HTTP/SSE. It needs no tmux window and registers no hooks. It speaks both OpenCode server APIs, 1.x and 2.x, picked per session from `opencode --version`; an unsupported major fails the session start with a clear error, and `validate` reports it as `adapter.opencode-version`.
  - `bmad-loop probe-adapter <cli>` does a zero-launch scan by default. `--probe` opts in to one live, content-free turn. Every report is sanitized before it is printed.
  - Antigravity (experimental) never reports token usage. Under worktree isolation, it trusts each unit worktree only when the project root is already trusted.
- **Config:** `[adapter] name`; `bmad-loop probe-adapter <cli> --probe`; `bmad-loop probe-adapter <cli> --workspace <path>`; `bmad-loop adapters`
- **Tested by:** `tests/test_profile.py`, `tests/test_probe.py`, `tests/test_adapter_registry.py`, `tests/test_workspace_trust.py`
- **Docs:** [Choosing which CLIs to drive](setup-guide.md#choosing-which-clis-to-drive), [Adapter authoring guide: finalizing a profile](adapter-authoring-guide.md#walkthrough-finalizing-a-profile), [CLI reference: probe-adapter](reference/cli.md#probe-adapter), [CLI reference: adapters](reference/cli.md#adapters)

### Per-stage adapters

- **Stability:** stable
- **Description:** Each stage (dev, review, sweep triage, auto-retrospective) can run on its own CLI, model and reasoning effort. Any setting a stage leaves out is taken from `[adapter]`.
- **Properties:**
  - `[adapter.dev]`, `[adapter.review]`, `[adapter.triage]` and `[adapter.retro]` override `name`, `model`, `effort` and `extra_args` per stage. A key left unset inherits from `[adapter]`.
  - A stage that switches to a different CLI does not inherit the base model, effort or extra_args. It uses that CLI's own defaults.
  - Naming an alias of the base CLI, such as `opencode` versus `opencode-http`, counts as the same CLI, not a switch.
  - `effort` is free-form and only opencode sends it, as the model variant. On OpenCode 2.x it must be one of the model's variants, or the session refuses to start. `bmad-loop validate` warns when it is set on a tmux CLI.
  - `extra_args` replaces the profile's permission-bypass flags instead of adding to them. `validate` warns with `policy.bypass-dropped` when a bypass flag would go missing.
- **Config:** `[adapter] name` / `model` / `effort` / `extra_args`; `[adapter.dev]`, `[adapter.review]`, `[adapter.triage]`, `[adapter.retro]` (same keys)
- **Tested by:** `tests/test_policy.py`, `tests/test_opencode_http.py`, `tests/test_cli.py`
- **Docs:** [Configuration reference: [adapter]](reference/configuration.md#adapter), [Configuration reference: [adapter.dev]](reference/configuration.md#adapterdev)

### Multiplexer and OS seams

- **Stability:** stable
- **Description:** Sessions run in a terminal multiplexer chosen from a registry: tmux on POSIX, psmux on native Windows, or an installed external backend. Session completion comes from hook-written event files, never from screen text.
- **Properties:**
  - Backend selection order: `BMAD_LOOP_MUX_BACKEND`, then `[mux] backend`, then the platform default if available, then the first available match, then a fallback.
  - A forced backend name that matches no registered backend fails with an error listing the known backends. It never falls back to tmux silently.
  - `bmad-loop mux` lists the backends and the current selection. `mux set <name>` saves a choice to `.bmad-loop/policy.toml`. `mux set --clear` goes back to auto-select.
  - A tmux-driven session completes only on a hook event or when its window dies. The pane log's contents never decide completion.
  - Hook events are written outside the project tree, under the per-user state root. Sessions get that path in `BMAD_LOOP_EVENTS_DIR`.
- **Config:** `[mux] backend`; `BMAD_LOOP_MUX_BACKEND`; `bmad-loop mux set <name>`; `bmad-loop mux set --clear`; `bmad-loop mux set <name> --force`
- **Tested by:** `tests/test_backend_registry.py`, `tests/test_cli.py`, `tests/test_generic_tmux.py`, `tests/test_signals.py`
- **Docs:** [Which backend is running, and how to switch](multiplexer-backends.md#which-backend-is-running-and-how-to-switch), [Configuration reference: [mux]](reference/configuration.md#mux), [CLI reference: mux](reference/cli.md#mux), [Porting bmad-loop to a new OS](porting-to-a-new-os.md)

### psmux on native Windows

- **Stability:** experimental
- **Description:** On native Windows, the bundled psmux backend (a ConPTY tmux re-implementation driven through PowerShell) runs sessions without WSL. It is experimental.
- **Properties:**
  - On a win32 host, psmux is the default backend whenever it reports itself available.
  - psmux counts as available only when `psmux` and `pwsh` are both on PATH and psmux is 3.4.0 or newer. Otherwise selection moves on to the next backend.
  - psmux refuses to open a window under PowerShell older than 7.3, or when the version cannot be read. This applies even when psmux is forced.
  - psmux sessions are registered in a per-project registry under the state root. To use an ambient `PSMUX_DATA_DIR` instead, set `[mux] honor_ambient_psmux_data_dir`.
- **Config:** `[mux] backend`; `[mux] honor_ambient_psmux_data_dir`; `BMAD_LOOP_MUX_BACKEND=psmux`
- **Tested by:** `tests/test_psmux_backend.py`, `tests/test_psmux_live.py`, `tests/test_backend_registry.py`, `tests/test_runs.py`
- **Docs:** [psmux (native Windows, experimental)](multiplexer-backends.md#psmux-native-windows-experimental), [Platform prerequisites](setup-guide.md#platform-prerequisites), [Roadmap: native Windows multiplexer backend](ROADMAP.md#native-windows-multiplexer-backend)

### Out-of-tree adapters and backends

- **Stability:** stable
- **Description:** A separately installed package can add a coding-CLI adapter class, a profile, or a multiplexer backend through Python entry points. Nothing in core needs to change, and once installed it is selectable without configuration.
- **Properties:**
  - An adapter class registers with `register_adapter` from a `bmad_loop.adapters` entry point. The profile's `adapter` field then selects it.
  - A package can ship profiles through the `bmad_loop.profiles` entry point. A project's `.bmad-loop/profiles/` file still overrides them.
  - A multiplexer backend registers with `register_multiplexer` from a `bmad_loop.mux_backends` entry point.
  - An external package cannot replace a bundled adapter kind or backend name. The bundled ones register first, and the first registration of a name wins.
  - A broken third-party package is recorded as a failure and reported by `bmad-loop adapters`, `bmad-loop mux` and `validate`. Selection keeps working.
- **Config:** entry-point groups `bmad_loop.adapters`, `bmad_loop.profiles`, `bmad_loop.mux_backends`; `bmad-loop adapters`; `bmad-loop mux`
- **Tested by:** `tests/test_adapter_registry.py`, `tests/test_external_backends.py`, `tests/test_profile.py`, `tests/test_backend_registry.py`
- **Docs:** [Shipping a new adapter class out-of-tree](adapter-authoring-guide.md#shipping-a-new-adapter-class-out-of-tree), [Two axes: CLI vs transport](adapter-authoring-guide.md#two-axes-cli-vs-transport), [External backends](multiplexer-backends.md#external-backends), [Shipping a mux backend out-of-tree](porting-to-a-new-os.md#shipping-out-of-tree-the-bmad_loopmux_backends-entry-point)

## Extensibility & ops

### Plugins

- **Stability:** stable
- **Description:** Extend a run without changing core. A `plugin.toml` manifest can add lifecycle hooks, settings and extra workflow sessions. A project plugin that can execute anything runs only after the operator grants its exact contents.
- **Properties:**
  - A project plugin with hooks, workflows, `[python]` or seeds never runs until `bmad-loop plugins enable <name>` records a content grant.
  - An ungranted plugin not listed in `[plugins] enabled` is skipped with a `note:` on stderr, and the run proceeds.
  - If a granted plugin's files, name or location change, startup fails with exit 1 until `bmad-loop plugins accept <name>` re-pins it.
  - Granted plugins run from a verified out-of-tree snapshot, so edits to the live plugin directory have no effect mid-run.
  - A hook that raises is journalled and disabled for the rest of the run. It never crashes the run.
- **Config:** `[plugins] enabled`, `[plugins.<name>]`; `bmad-loop plugins [list|enable|accept|revoke] [name]`, `bmad-loop plugins --json`
- **Tested by:** `tests/test_plugin_trust.py`, `tests/test_plugin_grants.py`, `tests/test_hook_bus.py`, `tests/test_plugin_snapshot.py`
- **Docs:** [Plugin authoring guide](plugin-authoring-guide.md), [Plugin trust & safety](plugin-authoring-guide.md#trust--safety), [CLI: plugins](reference/cli.md#plugins)

### Unity plugin

- **Stability:** stable
- **Description:** A bundled, opt-in plugin for Unity projects. It lets dev and sweep sessions drive a live Unity Editor through an Editor MCP (IvanMurzak or CoplayDev).
- **Properties:**
  - The plugin is inert unless `"unity"` is in `[plugins] enabled`. Until then its hooks, workflows and seeds stay off.
  - Startup refuses `editor_mode = "shared"` unless `[scm] isolation = "none"`, and `per_worktree` unless `isolation = "worktree"`.
  - Before each unit, a readiness gate waits for the Editor and MCP. If they are not ready, the unit is deferred instead of started.
  - In `per_worktree` mode each worktree gets its own managed Editor. If setup fails, the unit is deferred.
  - Around a failed-attempt rollback, the plugin saves and closes open scenes on a best-effort basis. A failure never blocks the rollback.
- **Config:** `[plugins] enabled`, `[plugins.unity] editor_mode`, `[plugins.unity] mcp`, `[scm] isolation`, `[cleanup] clean_tmp`
- **Tested by:** `tests/test_engine_plugin.py`, `tests/test_unity_quiesce.py`, `tests/test_unity_scene_guard.py`, `tests/test_unity_dialog_probe.py`
- **Docs:** [Bundled Unity plugin reference](game-engine-plugin-guide.md#reference-the-bundled-unity-plugin), [editor_mode and isolation coupling](game-engine-plugin-guide.md#the-editor_mode--scm-isolation-coupling), [Editor MCP guide](game-engine-mcp-guide.md), [Roadmap: game-engine work](ROADMAP.md#integrate-bmad-gds-game-test-items)

### TEA plugin

- **Stability:** experimental
- **Description:** A bundled, opt-in plugin that adds BMAD Test Architect (TEA) workflow sessions to every run and sweep. The steps are advisory by default; any of the three gate steps can be made blocking.
- **Properties:**
  - The plugin is inert unless `"tea"` is in `[plugins] enabled`.
  - With `require_tea` on (the default), a run fails at startup if `_bmad/tea/config.yaml` is missing.
  - The plugin adds six TEA sessions: three after the dev phase and three just before every commit. Runs and sweep bundles get the same steps.
  - If a gate set to `<gate>_blocking = true` returns FAIL or CONCERNS, the unit pauses for review instead of committing.
  - Gates are advisory by default. A missing or unparseable gate artifact never blocks a commit.
- **Config:** `[plugins] enabled`, `[plugins.tea] require_tea`, `[plugins.tea] <step>_enabled`, `[plugins.tea] trace_blocking` / `nfr_blocking` / `review_blocking`
- **Tested by:** `tests/test_plugin_tea.py`
- **Docs:** [TEA plugin guide](tea-plugin-guide.md), [How blocking gates work](tea-plugin-guide.md#escalate-on-gate-how-blocking-works), [Roadmap: TEA integration](ROADMAP.md#integrate-bmad-test-design--test-automation-runs-tea--testarch)

### Token budgets

- **Stability:** stable
- **Description:** Token spend is weighted by cost, so cache reads are discounted. An advisory per-story budget warns once. A per-session guard can warn, or can end a runaway session.
- **Properties:**
  - Every token total counts cache reads at `cache_read_weight` (default 0.1), so budgets follow cost rather than context re-reads.
  - A story's spend is checked at every session boundary. Crossing `max_tokens_per_story` warns once and journals `token-budget-exceeded`; the story keeps running.
  - With `session_budget_mode = "enforce"`, a session over `max_tokens_per_session` gets a wrap-up nudge, then ends `over_budget` once `session_budget_grace_s` has passed.
  - In `warn` mode (the default) the crossing is flagged once and the session finishes normally. In `off` mode usage is never sampled.
  - The session guard stays inert for profiles that cannot report usage mid-session.
- **Config:** `[limits] max_tokens_per_story`, `[limits] cache_read_weight`, `[limits] session_budget_mode`, `[limits] max_tokens_per_session`, `[limits] session_budget_grace_s`
- **Tested by:** `tests/test_engine.py`, `tests/test_generic_tmux.py`, `tests/test_opencode_http.py`, `tests/test_policy.py`
- **Docs:** [[limits] settings](reference/configuration.md#limits), [CLI: status](reference/cli.md#status)

### TUI dashboard

- **Stability:** stable
- **Description:** An optional Textual dashboard (`bmad-loop tui`) for watching runs and launching them. It never runs an engine in its own process.
- **Properties:**
  - The dashboard needs the `tui` extra. Without it, `bmad-loop tui` exits with an install hint and every other command still works.
  - Runs, sweeps and resumes started from the TUI run as detached processes in a control session, so they survive the TUI exiting or crashing.
  - The dashboard has no private channel to the engine. It reads the files a run writes, so runs started from a shell show up exactly like TUI-started runs.
  - The settings editor checks edits with the same policy parser a run uses, and will not save an invalid value.
  - Resized pane sizes are saved per project under `[tui]` in policy.toml and restored on the next launch.
  - In tickets mode the middle pane is a tickets board, read off the UI thread only when the initiative's files change, and **Request replan** goes through `tickets.py mark`.
- **Config:** `bmad-loop tui --low-frame-rate`, `[tui] low_frame_rate`
- **Tested by:** `tests/test_tui_app.py`, `tests/test_tui_launch.py`, `tests/test_tui_settings.py`, `tests/test_tui_data.py`
- **Docs:** [TUI guide](tui-guide.md), [Observer/launcher architecture](tui-guide.md#architecture-observerlauncher-never-the-engine), [TUI: tickets mode](tui-guide.md#tickets-mode), [CLI: tui](reference/cli.md#tui)

### Session and disk cleanup

- **Stability:** stable
- **Description:** `cleanup` removes this project's leftover multiplexer sessions. `clean` reclaims disk from concluded runs according to `[cleanup]`. `delete` and `archive` remove a single run.
- **Properties:**
  - `cleanup` removes sessions only for this project's runs whose engine is not live. Other projects' sessions are left alone.
  - `clean` only touches runs that are finished or stopped and have no live engine. Paused, interrupted and running runs are never reclaimed.
  - Runs past retention are archived to `.bmad-loop/archive/<run-id>.tar.gz`, or deleted with `--hard` or `archive_old = false`.
  - `delete` and `archive` refuse a run whose engine is provably live, unless `--force` is given.
  - With `auto_clean_on_finish` on, every `run` and `sweep` start removes worktrees left behind by finished runs. Stopped runs keep theirs for `resume` or `clean`.
- **Config:** `[cleanup] run_retention`, `retention_days`, `trim_artifacts`, `archive_old`, `auto_clean_on_finish`, `clean_tmp`; `bmad-loop cleanup --dry-run --json`; `bmad-loop clean --dry-run --keep RUN_ID --retain N --hard --json`; `bmad-loop delete|archive --force <run-id>`
- **Tested by:** `tests/test_cleanup.py`, `tests/test_runs.py`, `tests/test_cli.py`
- **Docs:** [Reclaiming sessions and disk](reference/run-state.md#reclaiming-sessions-and-disk), [[cleanup] settings](reference/configuration.md#cleanup), [CLI: clean](reference/cli.md#clean), [CLI: cleanup](reference/cli.md#cleanup)

### Init and validate

- **Stability:** stable
- **Description:** `init` installs the hook relay, the bundled `bmad-loop-*` skills, a policy template and `.gitignore` entries. `validate` checks every prerequisite before a run.
- **Properties:**
  - `init` writes `.bmad-loop/policy.toml` only when the file is missing. An existing policy is never overwritten.
  - `init` appends only the `.gitignore` lines that are missing, so running it again changes nothing.
  - `validate` exits 1 only when a finding has severity `problem`. Warnings never change the exit code.
  - Every finding carries a stable check id taken from a fixed, documented registry.
  - `validate` runs no project-controlled commands unless `--render-probe` or `--probes` is given. The one exception is tickets mode, which runs `tickets.py next` while an unlanded ledger entry declares a `gate:`.
  - On a BMAD v7 project `validate` reports the era, the active initiative, the ticket-store layout and the `tickets.*` preflight.
- **Config:** `bmad-loop init --cli PROFILE --no-skills --force-skills`; `bmad-loop validate --spec FOLDER --render-probe --probes --json`
- **Tested by:** `tests/test_cli.py`, `tests/test_install.py`
- **Docs:** [Setup guide](setup-guide.md), [Ticket-tree prerequisites](tickets-guide.md#prerequisites), [CLI: init](reference/cli.md#init), [CLI: validate](reference/cli.md#validate), [validate check ids](reference/cli.md#validate-checks)

### Diagnostics and JSON output

- **Stability:** stable
- **Description:** `bmad-loop diagnose` writes a sanitized dump of a run that is safe to share with maintainers. Every `--json` mode follows one schema-versioned contract, and exit codes come from a fixed set.
- **Properties:**
  - In `--json` mode stdout holds exactly one JSON object, or nothing when `--out` redirects it. On an error stdout stays empty and the exit code is nonzero.
  - Every document carries an integer `schema_version` owned by its command. Breaking changes bump it; new fields are only ever added.
  - Tickets mode added fields without a bump: `validate --json` gains `era`, `initiative` and `ticket_store`, and `status --json` gains a run-level `tickets` and a per-task `ticket` object, null outside tickets runs.
  - Exit codes are limited to 0 (OK), 1 (FAILURE), 2 (USAGE) and 130 (INTERRUPTED).
  - `diagnose` defaults to the latest run. Its dump contains no code, specs, prompts, paths or PII; story keys and branches are pseudonymized.
  - If the final leak check finds PII or a secret, `diagnose` emits no dump and exits 1.
- **Config:** `bmad-loop diagnose [run_id] --all --out FILE --json --max-journal-entries N`; `--json` on status, list, decisions, confirm, validate, clean, cleanup, plugins, diagnose, probe-adapter
- **Tested by:** `tests/test_cli.py`, `tests/test_diagnostics.py`, `tests/test_sanitize.py`, `tests/test_entry_point.py`
- **Docs:** [Machine-readable output](reference/cli.md#machine-readable-output---json), [Exit codes](reference/cli.md#exit-codes), [CLI: diagnose](reference/cli.md#diagnose)
