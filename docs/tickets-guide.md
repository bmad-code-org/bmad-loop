# Ticket-tree mode (BMAD v7)

Ticket-tree mode drives a BMAD v7 initiative ticket tree. bmad-loop picks the next startable ticket, dispatches `bmad-build-auto ticket <ref>` in a fresh session, verifies and reviews the result, and marks the plan `done` through the project's own `tickets.py`. It follows the orchestrator contract upstream wrote for bmad-loop (BMAD-METHOD `docs/build/autonomous-development-loops.md`). The v6 sprint-status and stories.yaml modes are unchanged and run side by side.

Tickets mode is **experimental**. It is built and tested against BMAD-METHOD main at `8f0d50e1` (`6.13.0-next`); the contract tests run against `tickets.py` vendored at that commit.

## Prerequisites

1. **Install BMAD v7 and bmad-loop.** Follow [Installing on BMAD v7](setup-guide.md#installing-on-bmad-v7-bmad-setup): `npx skills add bmad-code-org/bmad-loop`, then `/bmad setup bmad-loop`, then `/bmad-loop-setup`. The module record requires `bmad` and `bmad-build-auto` at 6.13.0 or newer and recommends `bmad-ticket` and `bmad-retrospective`, which tickets mode uses.
2. **Select the source.** Set `[stories] source = "tickets"` in `.bmad-loop/policy.toml`.
3. **Pick an initiative.** Set `[core] active_initiative` (in `_bmad/custom/config.user.toml`) to an initiative whose folder exists.
4. **Use a repo store.** `_bmad/custom/ticketing-store-config.toml` must say `store = "repo"`. Tracker stores are refused.
5. **Check it.** Run `bmad-loop validate --probes`. Every `run` repeats the same `tickets.*` preflight and refuses on any problem.

| Check id                                                 | Requires                                                                                               |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `tickets.script-missing`                                 | `_bmad/method/scripts/tickets.py` exists. This file is also what marks the project as v7.              |
| `tickets.config-utils-missing`                           | `_bmad/scripts/config_utils.py` exists; `tickets.py` loads it.                                         |
| `tickets.uv-missing`                                     | `uv` is on `PATH`. build-auto runs `tickets.py` with `uv run`.                                         |
| `tickets.initiative-unset`, `tickets.initiative-missing` | `active_initiative` is set and its folder exists.                                                      |
| `tickets.store-not-repo`                                 | the store is a repo store, not a tracker.                                                              |
| `tickets.store-topology` and its siblings                | the store layout is one tickets runs can drive (see [Store layouts](#store-layouts)).                  |
| `tickets.branch-per-run`                                 | a warning: `[scm] branch_per = "run"` puts every ticket on one branch.                                 |
| `tickets.capability`                                     | `tickets.py --help` offers every verb and status bmad-loop needs. Runs only under `validate --probes`. |

`validate` also checks the dev skill's ticket route (`skills.tickets-dispatch*`). A project with leftover v6 artifact keys still resolves v7 paths under `source = "tickets"`, and `validate` warns about them (`bmad-config.v6-keys-ignored`).

### `tickets.py` runs project code

bmad-loop never parses the tree itself. It runs the project's installed `tickets.py` (`next`, `find`, `status`, `mark`) and reads its JSON. `tickets.py` in turn executes the project's `_bmad/scripts/config_utils.py`, so treat both as project code. bmad-loop runs the script with its own interpreter (`-I -B -X utf8`) when the script's `requires-python` allows, and through `uv run --no-project` otherwise.

These surfaces run it: `run` (including `--dry-run` and the selectors), `resume`, `resolve`, `confirm`, the `status` board, `validate --probes`, and plain `validate` only while an unlanded ledger entry declares a `gate:`. The TUI's tickets board and start-run preview run it too, on a worker thread rather than the UI thread, and the board re-reads only when the initiative's files change.

## Store layouts

The ticket store is the initiative folder and its plans. Where it lives decides where a ticket's `done` mark lands. `validate --json` reports the layout as `ticket_store`, and every run records it. Resume, resolve and the TUI refuse a store whose layout changed since the run started.

| Layout | Store                                                      | Where the mark lands                                                  |
| ------ | ---------------------------------------------------------- | --------------------------------------------------------------------- |
| T1     | tracked in the code checkout                               | inside the unit's commit, so the plan lands with its code             |
| T2     | an ignored plain folder inside the checkout                | after the code lands; the store is unversioned                        |
| T3     | its own git repository, nested in the checkout and ignored | after the code lands, then a plan-only commit in the store repository |
| T4     | its own git repository outside the checkout                | as T3                                                                 |
| T6     | a plain folder outside any repository                      | as T2                                                                 |

For T2, T3, T4 and T6 the code commit cannot carry the plan, so the mark is latched on the task when the commit starts and made after `finalize_commit`, or after the merge-back under worktree isolation. A resume replays a latched mark. A failed mark pauses the run at `ticket-tree` and keeps the latch. An unversioned store journals `store-unversioned` once per run.

In T3 and T4 each marked plan (plus the ledger, when the unit wrote to it) gets its own commit, `chore(tickets): <ref> <target> [bmad-loop <run>]`, with `Bmad-Code-*` trailers naming the code commit. A failed store commit is journaled (`store-commit-failed` or `store-commit-foreign-dirt`) and listed by `status`, and the run goes on; it is retried before the run can finish, and one still owed pauses at `ticket-tree` (`store-commit-owed`) so the resume retries it.

Under worktree isolation a shared store is read in place: the unit worktree's `config.user.toml` is redirected to it. `tickets.own-repo-isolation` fails when that file is tracked or not ignored.

**Refused layouts:**

- **Workspace (T5):** a project root that is not a git repository, with code in several repositories. Every ticket would need a code-repository binding and a done-mark rule across repositories, which bmad-loop does not have yet (`tickets.workspace-unsupported`).
- **Partly tracked or untracked:** an initiative folder only partly tracked, or untracked but not ignored (`tickets.tree-untracked`).
- **Tracker store:** `store` is not `"repo"` (`tickets.store-not-repo`). The vendored `tickets.py` refuses a tracker store as well; the preflight refusal is the earlier, clearer one.

## How a run picks

Each pick reads `tickets.py next` and `status`. Candidates are the tickets ready to start or ready to refine, in initiative build order: the epic table's order, then each epic's entry order. Backlog rows are reported but never dispatched. The first candidate decides:

1. an earlier epic that is still open has a `rejected` retrospective → pause at `epic-boundary`;
2. the ticket needs refining → pause at `ticket-refine`;
3. it is a hitl ticket → pause at `ticket-hitl`;
4. it is refined, has a `plan_checkpoint`, and its story file is not approved → pause at `plan-checkpoint`;
5. otherwise dispatch `ticket <ref>`.

Tickets this run already touched are routed by their task state, never re-dispatched blindly. Tickets `in-progress` or `built` that this run did not start are never dispatched or marked: they are journaled `ticket-foreign-active`, and when only they stand in the way the run pauses at `ticket-tree`. When the remaining tickets wait only on tickets this run parked, the run ends with a parked summary. When nothing is left, the run finishes.

`bmad-loop run --epic <id|slug>` limits picks to one epic; the slug may omit its `epic-` prefix. `--story <ref>` runs one ticket, resolved through `tickets.py find`; it must be ready to start or refine. `bmad-loop run --dry-run` prints the build order, each `ticket <ref>` dispatch and the first pause a run would reach, and marks nothing.

A unit's commit carries `Bmad-Ticket:` and `Bmad-Plan:` trailers. Under worktree isolation its branch is `bmad-loop/<run>/<epic-slug>/<ref>`.

## Pauses and how to clear them

A pick pause fires before the ticket is registered, so a resume re-reads the tree and picks again. `bmad-loop status <run-id>` and the TUI show the stage; the TUI badges the ticket stages `hitl`, `refine` and `tree`.

| Stage              | Why the run paused                                                                             | Clear it                                                                                                             |
| ------------------ | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `ticket-hitl`      | the next ticket is a hitl ticket                                                               | build it with a person, then `bmad-loop resume <run-id>`                                                             |
| `ticket-refine`    | the next ticket needs refining, or a blocked ticket has an open unknown                        | refine it with bmad-ticket, then resume                                                                              |
| `ticket-tree`      | `active_initiative` no longer matches the run's pinned initiative                              | set it back, then resume                                                                                             |
| `ticket-tree`      | `tickets.py` failed, or a mark (done, park or rollback re-arm) failed                          | fix the tree, then resume; the mark is retried                                                                       |
| `ticket-tree`      | a store-repository commit is still owed at run end (`store-commit-owed`)                       | commit or unstage the store's own changes to those paths, then resume; the commit is retried                         |
| `ticket-tree`      | only tickets this run did not start stand in the way, or the tree is stalled                   | finish or unblock those tickets, then resume                                                                         |
| `ticket-tree`      | the initiative folder holds uncommitted changes the run does not own                           | commit or stash them, then resume                                                                                    |
| `ticket-tree`      | under worktree isolation the code repo gitignores a tracked store's plan (`ignored-plan`)      | commit the plans with the initiative folder, or gitignore the whole store, then resume                               |
| `plan-checkpoint`  | a refined `plan_checkpoint` ticket waits for story-file approval                               | review the story file; `resume` records the approval and dispatches                                                  |
| `plan-checkpoint`  | an unrefined `plan_checkpoint` ticket finished its planning leg                                | review the plan, then resume; TUI **Request replan** sends it back to `draft`                                        |
| `story-checkpoint` | a `done_checkpoint` ticket committed                                                           | resume                                                                                                               |
| `epic-boundary`    | an epic awaits closure, its verdict is `rejected`, or a later ticket waits on an unclosed epic | close the epic with bmad-ticket, then resume (see [Epic retrospective and closure](#epic-retrospective-and-closure)) |
| `epic-boundary`    | an epic's retrospective document could not be committed in the store repository                | commit or unstage the store's own changes to it, then resume; the commit is retried                                  |
| `escalation`       | a plan is `blocked` and this run did not park it, or a session escalated                       | `bmad-loop resolve <run-id>` (see [Parking and resolve](#parking-and-resolve))                                       |
| `story-gate`       | a deferred-work `gate:` names the ticket, or a gate is a v6 story key                          | close the entry or rewrite the token, then resume (see [Ledger and sweeps](#ledger-and-sweeps))                      |

Every resume also checks that this bmad-loop knows the run's work source, that the store layout is unchanged, and the isolation settings. The [escalations guide](escalations-guide.md) covers the stages tickets mode shares with the other modes.

## Marking done and review depth

`[tickets] mark_done` decides who marks a ticket done:

- `"auto"` (default): after the ticket passes verify and review, bmad-loop runs `tickets.py mark <ref> done`. That unblocks the ticket's dependents in `next`.
- `"never"`: the plan stays at `built` for a person to mark.

bmad-loop is the only writer of a plan's `status` and `blocked_*` fields during a run. Every write goes through `tickets.py mark` under a lock, is read back from the plan's frontmatter, and must be a move the plan transition table allows. A plan never moves backward without an explicit reason (park, repair, rearm, rollback, replan or salvage), and `done` and `dropped` are final. bmad-loop never writes `dropped` and never edits epic or initiative files.

`[tickets] thorough_review` asks build-auto for a thorough pass: `"never"`, `"high-risk"` (default) or `"always"`. Under `"high-risk"` a dev leg reads the ticket's risk from `tickets.py find` and a review leg reads the plan's `risk`. See the [configuration reference](reference/configuration.md) for both keys.

A review converges when the plan is `built` or `done`. A review that times out lands its plan at `built`, never `done`.

## Checkpoints

Checkpoints follow upstream's entries in the epic file:

- **Refined `plan_checkpoint`:** the run pauses at `plan-checkpoint` **before** dispatch. Review the ticket's story file; resuming records the approval (`ticket-story-approved`) and dispatches.
- **Unrefined `plan_checkpoint`:** the run dispatches `ticket <ref> Halt after planning.`, verifies the plan reached `ready-for-dev`, and pauses at `plan-checkpoint`. Nothing is committed yet. Resuming dispatches the build leg on the uncommitted plan, and the unit's commit carries both. A session that builds past the halt pauses the run before its commit.
- **`done_checkpoint`:** the run pauses at `story-checkpoint` after the ticket commits. It skips the pause for the last ticket, at `--max-stories`, and on a graceful stop.

## Epic retrospective and closure

Before each pick and at run end, the run checks the epic table. An open epic is finished when every ticket is `built`, `done` or `dropped`. If this run committed one of its tickets, the epic gets its retrospective:

- `gates.retrospective = "auto"` runs `/bmad-retrospective -H <epic-slug>`, verifies `<epic-slug>/<epic-slug>-retrospective.md`, and commits only that document (in the store repository for T3 and T4; a failed store commit pauses at `epic-boundary` before the closure is recorded, and the resume commits it). A valid document already on disk counts. A dirty tree or a git fault pauses at `epic-boundary` before the session and a stop request stops the run; neither records a closure, so the resume runs the retrospective.
- `"notify"` and `"never"` run no session.

The run records the document's `verdict` (or `unknown` when there is none) and pauses at `epic-boundary`. bmad-loop never writes the epic's file: close the epic with bmad-ticket, then resume. A `rejected` verdict pauses every later pick while the epic stays open, though fix-up tickets added to the rejected epic still run. Any other verdict pauses once; resuming past an epic still open journals `epic-closure-not-recorded`. Details: [Epic closure in tickets mode](escalations-guide.md#epic-closure-in-tickets-mode).

## Parking and resolve

When a session parks a ticket for a person (an `awaiting-operator` result, or a review demotion under `[operator] on_review_demotion = "park"`), the unit commits and its plan is marked `blocked` with the reason `awaiting operator: <actions>`, never `done`. Do the actions, then run `bmad-loop confirm <ref>`: it marks the plan `done` (`built` under `mark_done = "never"`) and clears `blocked_*`. The plan is committed with the park record's deletion when the code checkout tracks the store, in the store's own repository when it has one, and not at all in an unversioned store.

An escalated ticket is recovered with `bmad-loop resolve <run-id>`. The ticket's plan is the frozen spec, and its `<intent-contract>` block is the intent. The re-arm moves the plan through `tickets.py mark` (back to `ready-for-dev`, or `in-review` for a restore) and clears `blocked_*`; a refused move aborts the re-arm and puts the plan back byte for byte. See [Resolve in tickets mode](escalations-guide.md#resolve-in-tickets-mode).

A code rollback re-arms the plan too: a plan past `ready-for-dev` returns to `ready-for-dev`, and a plan that holds only frontmatter is deleted.

## Ledger and sweeps

On v7 the deferred-work ledger is `deferred-work.md` in the initiative folder. When a ticket's plan reaches `built`, its frontmatter `deferred:` findings are harvested into the ledger, each naming the plan by its path inside the initiative folder.

A ledger `gate:` holds a ticket until the entry closes. In tickets mode a gate token names a ticket ref (`1.2`), an epic by id (`1`) or folder (`epic-cart`), or a backlog file name. A v6 story key (`1-2-foo`) cannot be matched: it fails `deferred.hard-gate` in `validate` and pauses the run at `story-gate` until it is rewritten.

Sweeps run on v7 initiatives with no sprint board; each bundle writes `plan-<slug>.md` in the initiative folder and closes at `built`. A sweep is pinned to the initiative it started on, and pre-answers are kept per initiative in `.bmad-loop/decisions-<initiative>.json`. See [Sweeps on BMAD v7](sweep-guide.md#sweeps-on-bmad-v7) and [Hard gates](sweep-guide.md#hard-gates).

## Watching a run

- `bmad-loop status <run-id>` prints the tickets board: the initiative's tickets grouped by epic in build order, with plan status, flags and the run's phase.
- `bmad-loop status --json` adds a run-level `tickets` object (initiative, store layout, epic closures) and a per-task `ticket` object (ref, epic, plan, pending marks). See the [CLI reference](reference/cli.md).
- The TUI shows a [tickets board](tui-guide.md#tickets-board-middle-tickets-mode), pause-reason and epic-closure viewers, and a tickets preview in the start-run dialog.

## Do not downgrade bmad-loop mid-run

A run records its work source. A bmad-loop that does not know `tickets` refuses to resume such a run ("this run's work source 'tickets' is unknown to this bmad-loop") instead of resuming it as a sprint run. Finish or stop tickets runs before installing an older bmad-loop.
