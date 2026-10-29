# Ticket-tree mode (BMAD v7)

Ticket-tree mode drives a BMAD v7 initiative ticket tree. bmad-loop picks the next startable ticket, dispatches `bmad-build-auto ticket <ref>` in a fresh session, verifies and reviews the result, and marks the plan `done` through the project's own `tickets.py`. It follows the orchestrator contract upstream wrote for bmad-loop (BMAD-METHOD `docs/build/autonomous-development-loops.md`). The v6 sprint-status and stories.yaml modes are unchanged and run side by side.

Tickets mode is **experimental**. It is built and tested against BMAD-METHOD main at `61e01f82` (`6.13.0-next`); the contract tests run against `bmad-agent-po`'s `tickets.py` vendored at that commit.

## Prerequisites

1. **Install BMAD v7 and bmad-loop.** Follow [Installing on BMAD v7](setup-guide.md#installing-on-bmad-v7-bmad-setup): `npx skills add bmad-code-org/bmad-loop`, then `/bmad setup bmad-loop`, then `/bmad-loop-setup`. The module record requires `bmad` and `bmad-build-auto` at 6.13.0 or newer and recommends `bmad-agent-po` and `bmad-retrospective`, which tickets mode uses.
2. **Plan the tree with Sofia.** Talk to Sofia, the Product Owner agent (`bmad-agent-po`): she creates the initiative, slices it into epics and stories, writes and refines the tickets, and readies them. She also replans mid-flight and runs the epic closure check. On v7 she replaces `bmad-spec`, `bmad-ticket` and `bmad-correct-course`.
3. **Select the source.** Set `[stories] source = "tickets"` in `.bmad-loop/policy.toml`.
4. **Pick an initiative.** `[core] active_initiative` in `_bmad/custom/config.user.toml` must name an initiative whose folder exists. A parent initiative passes the preflight only under `[scm] isolation = "worktree"` without `gates.retrospective = "auto"`. Sofia records the initiative she creates there, and the `bmad` skill shows, switches, creates or clears it, so you rarely edit the file by hand.
5. **Use a repo store.** With no `_bmad/custom/ticketing-store-config.toml` the store is the repo store. Sofia writes that file only when you connect a tracker or switch stores, and it must then say `store = "repo"`: tracker stores are refused.
6. **Check it.** Run `bmad-loop validate --probes`. Every `run` repeats the same `tickets.*` preflight and refuses on any problem.

| Check id                                                 | Requires                                                                                               |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `tickets.script-missing`                                 | `_bmad/method/scripts/tickets.py` exists. This file is also what marks the project as v7.              |
| `tickets.config-utils-missing`                           | `_bmad/scripts/config_utils.py` exists; `tickets.py` loads it.                                         |
| `tickets.uv-missing`                                     | `uv` is on `PATH`. build-auto runs `tickets.py` with `uv run`.                                         |
| `tickets.initiative-unset`, `tickets.initiative-missing` | `active_initiative` is set and its folder exists.                                                      |
| `tickets.initiative-parent`                              | the active initiative is no parent, unless worktree isolation runs without the auto retrospective.     |
| `tickets.store-not-repo`                                 | the store is a repo store, not a tracker.                                                              |
| `tickets.store-topology` and its siblings                | the store layout is one tickets runs can drive (see [Store layouts](#store-layouts)).                  |
| `tickets.branch-per-run`                                 | a warning: `[scm] branch_per = "run"` puts every ticket on one branch.                                 |
| `tickets.capability`                                     | `tickets.py --help` offers every verb and status bmad-loop needs. Runs only under `validate --probes`. |

`validate` also checks the dev skill's ticket route (`skills.tickets-dispatch*`). A project with leftover v6 artifact keys still resolves v7 paths under `source = "tickets"`, and `validate` warns about them (`bmad-config.v6-keys-ignored`).

### `tickets.py` runs project code

bmad-loop never parses the tree itself. It runs the project's installed `tickets.py` (`next`, `find`, `status`, `mark`) and reads its JSON. `tickets.py` in turn executes the project's `_bmad/scripts/config_utils.py`, so treat both as project code. bmad-loop runs the script with its own interpreter (`-I -B -X utf8`) when the script's `requires-python` allows, and through `uv run --no-project` otherwise.

These surfaces run it: `run` (including `--dry-run` and the selectors), `resume`, `resolve`, `confirm`, the `status` board, `validate --probes`, and plain `validate` only while an unlanded ledger entry declares a `gate:`. The TUI's tickets board and start-run preview run it too, on a worker thread rather than the UI thread, and the board re-reads only when the initiative's files change.

## Tree layout

An initiative folder holds its own `<initiative>.md` and, in a `tickets/` folder, its `tickets.toml` and one folder per epic. Each epic folder holds `<epic>.md` and its retrospective, with its `tickets.toml`, leaf ticket files and plans in its own `tickets/` folder. `backlog/` stays flat.

bmad-loop reads each folder the way the installed `tickets.py` does: a folder with a `tickets/` folder inside it is read there, and any other folder is read as the preview-era flat layout. A preview-era tree therefore still runs with the preview-era `tickets.py` it was installed with. The current `tickets.py` refuses that layout; `bmad migrate method` moves the tree into `tickets/` folders.

A parent initiative, whose `tickets.toml` lists child initiatives in `[[initiative]]` tables, holds no tickets of its own, and each child's folder sits beside the parent's. The preflight refuses a parent (`tickets.initiative-parent`) under `[scm] isolation = "none"`, because sessions in the main checkout read the parent from `config.user.toml`, and under `gates.retrospective = "auto"`, whose session also runs in the main checkout. Set `active_initiative` to one child, or switch to worktree isolation without the auto retrospective. There the parent is an ok line naming its children, and the store checks run once for each child that has a folder: a child declared but not sliced yet is skipped. Each child is held to the `config.user.toml` rule of a shared store whatever its layout (`tickets.own-repo-isolation`). An epic the initiative declares but has not sliced yet has no folder; it holds no tickets, and the tickets gated after it wait for it.

`bmad-loop run` on a parent builds its children one run at a time, taking the first child `next <parent>` lists as ready. It passes over a ready child that has no folder yet (`no-folder`) and one whose first pick finds nothing to dispatch (`drained`). Each child's run records the parent as `parent_initiative`. Before every pick, bmad-loop closes each drained child (below), so the children waiting on it become ready. One line per switch names the next child or why the chain ended. The chain ends when:

- a child's run does not finish (paused, escalated, stopped or crashed). Once `resume` or `resolve` (from the CLI or the TUI) drives that run to finished, the chain goes on to the next child as it does after `run`.
- a stop was asked of the run.
- the `--max-stories` budget is spent (`max-stories`).
- it would build the same child again after a run that landed no ticket (`stalled`).
- no child can start: some still wait on a prerequisite (`waiting`), every child left was passed over (`passed-over`), or every child is done or dropped (`all-done`). A child waiting on a dropped prerequisite still counts as waiting. The end line names each waiting child's unmet prerequisites with their status, for example `initiative-billing on initiative-search (dropped), initiative-ledger (no-folder)`; `open` is a prerequisite whose file sets no status.

Closing a child writes `status: done` into its `<child>.md`. It happens before every pick, both when `run` starts and after each child's run finishes, for every child whose tickets are all done or dropped. That includes a child a stopped or interrupted chain left open, and one drained outside the chain. A line names each child closed. A drained child is left open, with a line naming why, while a run of it is live, or while a run that builds it is paused or otherwise unfinished (resume that run). A sweep of it holds it only while live. It is also left open while its first pick still finds work, such as an epic awaiting closure, which a run of the child then does, and while its store is refused.

When `run` starts, it closes only once the launch checks that no child changes have passed (a clean worktree, the skills, plugin trust), so a run they refuse writes and commits nothing. The first child's run journal then records those closes, ahead of its own.

Where the store is versioned, a close is committed as `chore(tickets): <child> done [bmad-loop <run>]` (`[bmad-loop chain <parent>]` for a close before the first child's run), in the code repo (T1) or in the store's own repository (T3/T4). A close that cannot be committed ends the chain (`store-commit-failed`, exit 1).

Each child's run pauses at `epic-boundary` after every epic it finishes, as a one-initiative run does, and no policy key skips that pause. Run `bmad-loop resolve <run-id>` to close the epic with bmad-agent-po and resume, or close and commit it yourself and `bmad-loop resume <run-id>` (see [closure](#epic-retrospective-and-closure)). The resumed run goes on with that child, and once its run finishes the chain goes on to the next child.

`--epic` and `--story` select within one initiative, so `run` on a parent, and its dry run, refuse them with exit 1. `--run-id` names the first child's run; each later child's run gets its own id. `--max-stories` caps the whole chain: each child's run starts with what the runs before it left, counting every ticket they dispatched.

`bmad-loop run --dry-run` on a parent writes nothing. It lists the children in build order, each with its status, closed and total tickets, the prerequisites it waits on and a tag (`next`, `ready`, `ready, passed over (<why>)`, `waiting`, `no folder`, `drained (the run closes it first)`, `done` or `dropped`). It then prints the `next` child's dry run, read through that child's folder, and ends `predicted: chain starts with <child>; later children are chosen as each finishes`, or with the end line `run` prints when no child can start. It closes no drained child, but picks as the tree would read once the run has closed them (a drained child with an epic still to close or break down stays open, as `run` leaves it); the predicted line names those the run closes before its first pick. Its `--max-stories` caps the first child's listing only, and its banner warns when the preflight would refuse the parent.

The TUI's start-run preview lists the same children, tags each drained child `closes first`, and shows the board of the first ready child once those are closed; `run --dry-run` names the exact first pick. A chained child run's [tickets board](tui-guide.md#tickets-board-middle-tickets-mode) shows that child's tickets.

## Store layouts

The ticket store is the initiative folder and its plans. Where it lives decides where a ticket's `done` mark lands. `validate --json` reports the layout as `ticket_store`, and every run records it. Resume, resolve and the TUI refuse a store whose layout changed since the run started.

| Layout | Store                                                      | Where the mark lands                                                  |
| ------ | ---------------------------------------------------------- | --------------------------------------------------------------------- |
| T1     | tracked in the code checkout                               | inside the unit's commit, so the plan lands with its code             |
| T2     | an ignored plain folder inside the checkout                | after the code lands; the store is unversioned                        |
| T3     | its own git repository, nested in the checkout and ignored | after the code lands, then a plan-only commit in the store repository |
| T4     | its own git repository outside the checkout                | as T3                                                                 |
| T6     | a plain folder outside any repository                      | as T2                                                                 |

In T1 a commit failure puts the pre-mark plan back; if a resumed commit window finds the plan already marked since the commit started and its commit then fails, the escalation names the plan, for you to restore by hand before resuming.

For T2, T3, T4 and T6 the code commit cannot carry the plan, so the mark is latched on the task when the commit starts and made after `finalize_commit`, or after the merge-back under worktree isolation. A resume replays a latched mark. A failed mark pauses the run at `ticket-tree` and keeps the latch. An unversioned store journals `store-unversioned` once per run.

In T3 and T4 each marked plan (plus the ledger, when the unit wrote to it) gets its own commit, `chore(tickets): <ref> <target> [bmad-loop <run>]`, with `Bmad-Code-*` trailers naming the code commit. Under `mark_done = "never"` nothing is marked, and the plan is committed at `built`. A store commit never overwrites a version of one of its paths that a person has staged, even when the file is gone from disk. A failed store commit is journaled (`store-commit-failed` or `store-commit-foreign-dirt`) and listed by `status`, and the run goes on; it is retried before the run can finish, and one still owed pauses at `ticket-tree` (`store-commit-owed`) so the resume retries it. A landed ticket's harvested deferrals ride its `chore(tickets)` commit. An isolated deferred ticket's harvest, or a resume's replay of it, gets its own best-effort commit, `chore(deferred-work): carry harvested findings from <ticket>`, whose failure is journaled but not retried.

Under worktree isolation a shared store is read in place: the unit worktree's `config.user.toml` is redirected to it. `tickets.own-repo-isolation` fails when that file is tracked or not ignored.

**Refused layouts:**

- **Workspace (T5):** a project root that is not a git repository, with code in several repositories. Every ticket would need a code-repository binding and a done-mark rule across repositories, which bmad-loop does not have yet (`tickets.workspace-unsupported`).
- **Partly tracked or untracked:** an initiative folder only partly tracked, or untracked but not ignored (`tickets.tree-untracked`).
- **Tracked outside the checkout:** a store the code repository tracks but that lies outside `repo_root`, the checkout bmad-loop commits in; the commits bmad-loop makes by path would skip its plans. Under worktree isolation a tracked store must also lie inside the project (`tickets.store-topology`).
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

A unit's commit carries a `Bmad-Ticket:` trailer, and a `Bmad-Plan:` trailer when the plan lies inside the code checkout (an external store's plan has none). Under worktree isolation its branch is `bmad-loop/<run>/<epic-slug>/<ref>`.

## Pauses and how to clear them

A pick pause fires before the ticket is registered, so a resume re-reads the tree and picks again. A resume never rebinds a unit it already started: it pauses at `ticket-tree` instead. `bmad-loop status <run-id>` and the TUI show the stage; the TUI badges the ticket stages `hitl`, `refine` and `tree`.

| Stage              | Why the run paused                                                                                                                                                            | Clear it                                                                                                                                                             |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ticket-hitl`      | the next ticket is a hitl ticket                                                                                                                                              | build it with a person, then `bmad-loop resume <run-id>`                                                                                                             |
| `ticket-refine`    | the next ticket needs refining, or a blocked ticket has an open unknown                                                                                                       | refine it with bmad-agent-po, then resume                                                                                                                            |
| `ticket-tree`      | `active_initiative` no longer matches the run's pinned initiative                                                                                                             | set it back, then resume                                                                                                                                             |
| `ticket-tree`      | `tickets.py` failed, or a mark (done, park or rollback re-arm) failed                                                                                                         | fix the tree, then resume; the mark is retried                                                                                                                       |
| `ticket-tree`      | a store-repository commit is still owed at run end (`store-commit-owed`)                                                                                                      | commit or unstage the store's own changes to those paths, then resume; the commit is retried                                                                         |
| `ticket-tree`      | only tickets this run did not start stand in the way, or the tree is stalled                                                                                                  | finish or unblock those tickets, then resume                                                                                                                         |
| `ticket-tree`      | the initiative folder holds uncommitted changes the run does not own                                                                                                          | commit or stash them, then resume                                                                                                                                    |
| `ticket-tree`      | under worktree isolation the code repo gitignores a tracked store's plan (`ignored-plan`)                                                                                     | commit the plans with the initiative folder, or gitignore the whole store, then resume                                                                               |
| `ticket-tree`      | on resume, an in-flight unit's ticket was moved (`tickets.py move`) or its plan is gone (`binding-moved`); the reason names the old and new ref and plan, or the missing plan | undo the move (`move` renumbers, so restore the entry and plan, e.g. with git), then resume; or archive the run and start a new one                                  |
| `plan-checkpoint`  | a refined `plan_checkpoint` ticket waits for story-file approval                                                                                                              | review the story file; `resume` records the approval and dispatches                                                                                                  |
| `plan-checkpoint`  | an unrefined `plan_checkpoint` ticket finished its planning leg                                                                                                               | review the plan, then resume; TUI **Request replan** sends it back to `draft`                                                                                        |
| `story-checkpoint` | a `done_checkpoint` ticket committed                                                                                                                                          | resume                                                                                                                                                               |
| `epic-boundary`    | an epic awaits closure, its verdict is `rejected`, or a later ticket waits on an unclosed epic                                                                                | `bmad-loop resolve <run-id>` closes it with bmad-agent-po and resumes; or close and commit it yourself, then resume (see [closure](#epic-retrospective-and-closure)) |
| `epic-boundary`    | an epic's retrospective document could not be committed in the store repository                                                                                               | commit or unstage the store's own changes to it, then resume; the commit is retried                                                                                  |
| `escalation`       | a plan is `blocked` and this run did not park it, or a session escalated                                                                                                      | `bmad-loop resolve <run-id>` (see [Parking and resolve](#parking-and-resolve))                                                                                       |
| `story-gate`       | a deferred-work `gate:` names the ticket, or a gate is a v6 story key no single ticket claims                                                                                 | close the entry or rewrite the token, then resume (see [Ledger and sweeps](#ledger-and-sweeps))                                                                      |
| `story-gate`       | a `tickets.toml` cannot be read for its `v6_key` claims while a v6 story-key gate stands                                                                                      | fix the file, then resume                                                                                                                                            |

Every resume also checks that this bmad-loop knows the run's work source, that the store layout is unchanged, and the isolation settings. The [escalations guide](escalations-guide.md) covers the stages tickets mode shares with the other modes.

## Marking done and review depth

`[tickets] mark_done` decides who marks a ticket done:

- `"auto"` (default): after the ticket passes verify and review, bmad-loop runs `tickets.py mark <ref> done`. That unblocks the ticket's dependents in `next`.
- `"never"`: the plan stays at `built` for a person to mark.

The build skill writes a plan's `status` during its own session; every other write of `status` and `blocked_*` during a run is bmad-loop's, and none lands while a session for that ticket is live. Each bmad-loop write goes through `tickets.py mark` under a lock, is read back from the plan's frontmatter, and must be a move the plan transition table allows. A plan never moves backward without an explicit reason (park, repair, rearm, rollback, replan or salvage), and `done` and `dropped` are final. bmad-loop never writes `dropped` and never edits epic or initiative files.

`[tickets] thorough_review` asks build-auto for a thorough pass: `"never"`, `"high-risk"` (default) or `"always"`. Under `"high-risk"` a dev leg reads the ticket's risk from `tickets.py find` and a review leg reads the plan's `risk`. See the [configuration reference](reference/configuration.md) for both keys.

A review converges when the plan is `built` or `done`. A review that times out lands its plan at `built`, never `done`.

## Checkpoints

Checkpoints follow upstream's entries in the epic file:

- **Refined `plan_checkpoint`:** the run pauses at `plan-checkpoint` **before** dispatch. Review the ticket's story file; resuming records the approval (`ticket-story-approved`) and dispatches.
- **Unrefined `plan_checkpoint`:** the run dispatches `ticket <ref> Halt after planning.`, verifies the plan reached `ready-for-dev`, and pauses at `plan-checkpoint`. Nothing is committed yet. Resuming dispatches the build leg on the uncommitted plan, and the unit's commit carries both. A session that builds past the halt pauses the run before its commit.
- **`done_checkpoint`:** the run pauses at `story-checkpoint` after the ticket commits. It skips the pause for the last ticket, at `--max-stories`, and on a graceful stop.

## Epic retrospective and closure

Before each pick and at run end, the run checks the epic table. An open epic is finished when every ticket is `built`, `done` or `dropped`. If this run committed one of its tickets, the epic gets its retrospective:

- `gates.retrospective = "auto"` runs `/bmad-retrospective -H <epic-slug>`, verifies `<epic-slug>-retrospective.md` beside the epic file, and commits only that document (in the store repository for T3 and T4; a failed store commit pauses at `epic-boundary` before the closure is recorded, and the resume commits it). A valid document already on disk counts. A dirty tree or a git fault pauses at `epic-boundary` before the session and a stop request stops the run; neither records a closure, so the resume runs the retrospective.
- `"notify"` and `"never"` run no session.

The run records the document's `verdict` (or `unknown` when there is none) and pauses at `epic-boundary`. bmad-loop never writes the epic's file. `bmad-loop resolve <run-id>` opens bmad-agent-po on its closure check, then checks that the epic reads `done` or `dropped` with its epic folder committed, and resumes; `--no-interactive` only checks an epic you closed and committed yourself. Closing and committing it yourself, then `bmad-loop resume`, also works. A `rejected` verdict pauses every later pick while the epic stays open, though fix-up tickets added to the rejected epic still run. Any other verdict pauses once; resuming past an epic still open journals `epic-closure-not-recorded`. A `--story` run checks no epic: it finishes past the epic its ticket finished and journals `retro-run-end-skipped` (`out-of-scope`). Details, flags and refusals: [Epic closure in tickets mode](escalations-guide.md#epic-closure-in-tickets-mode).

## Parking and resolve

When a session parks a ticket for a person (an `awaiting-operator` result, or a review demotion under `[operator] on_review_demotion = "park"`), the unit commits and its plan is marked `blocked` with the reason `awaiting operator: <actions>`, never `done`. Do the actions, then run `bmad-loop confirm <ref>`: it marks the plan `done` (`built` under `mark_done = "never"`) and clears `blocked_*`. The plan is committed with the park record's deletion when the code checkout tracks the store, in the store's own repository when it has one, and not at all in an unversioned store.

When the build session recommended a follow-up review, the park keeps that recommendation (`confirm --list`, `followup_review_recommended` in `confirm --json` and `status --json`) and `confirm <ref>` repeats it with the plan's path. Run the pass after confirming: the plan is then `done` (`built` under `mark_done = "never"`), and bmad-build-auto routes either status to a follow-up review. A parked plan reads `blocked`, which the skill refuses. See [When a parked story's build recommended a follow-up review](escalations-guide.md#when-a-parked-storys-build-recommended-a-follow-up-review).

An escalated ticket is recovered with `bmad-loop resolve <run-id>`. The ticket's plan is the frozen spec, and its `<intent-contract>` block is the intent. The re-arm moves the plan through `tickets.py mark` (back to `ready-for-dev`, or `in-review` for a restore) and clears `blocked_*`; a refused move aborts the re-arm and puts the plan back byte for byte. See [Resolve in tickets mode](escalations-guide.md#resolve-in-tickets-mode).

A code rollback re-arms the plan too: a plan past `ready-for-dev` returns to `ready-for-dev`, and a plan that holds only frontmatter is deleted.

## Ledger and sweeps

On v7 the deferred-work ledger is `deferred-work.md` in the initiative folder. When a ticket's plan reaches `built`, its frontmatter `deferred:` findings are harvested into the ledger, each naming the plan by its path inside the initiative folder.

A ledger `gate:` holds a ticket until the entry closes. In tickets mode a gate token names a ticket ref (`1.2`), or an epic by id (`1`) or folder (`epic-cart`). A backlog file name gates nothing, since backlog rows never dispatch, and neither does an id of `true` or `false`, which `tickets.py` reads as a boolean. `validate` warns about both.

A v6 story key (`1-2-foo`, or its `1-2` prefix) is resolved through the `v6_key` the v7 migration records on each ticket's `tickets.toml` entry: when exactly one ticket claims it, it gates that ticket as its ref would, and messages still show the token as written. A key that no ticket claims, or that two or more claim (both halves of a split `1-2a-…` / `1-2b-…`), fails `deferred.hard-gate` in `validate` and pauses the run at `story-gate` before every ticket until it is rewritten. While an unlanded entry carries such a token, a `tickets.toml` that cannot be read for its `v6_key` claims pauses the run at `story-gate` (journaled `story-gate-unreadable`) and fails `validate` with kind `probe-failed`.

Sweeps run on v7 initiatives with no sprint board; each bundle writes `plan-<slug>.md` in the initiative folder and closes at `built`. A sweep is pinned to the initiative it started on, and pre-answers are kept per initiative in `.bmad-loop/decisions-<initiative>.json`. See [Sweeps on BMAD v7](sweep-guide.md#sweeps-on-bmad-v7) and [Hard gates](sweep-guide.md#hard-gates).

## Watching a run

- `bmad-loop status <run-id>` prints the tickets board: the initiative's tickets grouped by epic in build order, with plan status, flags and the run's phase.
- `bmad-loop status --json` adds a run-level `tickets` object (initiative, store layout, epic closures) and a per-task `ticket` object (ref, epic, plan, pending marks). See the [CLI reference](reference/cli.md).
- The TUI shows a [tickets board](tui-guide.md#tickets-board-middle-tickets-mode), pause-reason and epic-closure viewers, and a tickets preview in the start-run dialog.

## Do not downgrade bmad-loop mid-run

A run records its work source. A bmad-loop that does not know `tickets` refuses to resume such a run ("this run's work source 'tickets' is unknown to this bmad-loop") instead of resuming it as a sprint run. Finish or stop tickets runs before installing an older bmad-loop.
