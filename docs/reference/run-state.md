# Run state reference

Where bmad-loop keeps the state of runs and sweeps, what each file holds, how multiplexer sessions are named and torn down, and what `stop`, `cleanup`, `clean`, `delete` and `archive` remove. Command options are in the [CLI reference](cli.md).

## Run inventory

- All run state in `.bmad-loop/runs/<run-id>/` (gitignored by `init`): `state.json`, `journal.jsonl`, `engine.pid`, `logs/<task_id>.log`, and `tasks/<task_id>/` per agent session, which holds the session prompt, the heartbeat, adapter-private records, and the shared artifacts: [`result.json`, `escalation.json`]. The hook-event channel and the config digest live outside the tree, under the [state root](#state-root).

A directory under `.bmad-loop/runs/` counts as a run only if it contains a `state.json` file.

`state.json` and `journal.jsonl` are written through a descriptor on the run directory, never by path. The descriptor is held to the identity (device and inode) recorded when the run directory was created, and both files are created owner-only (`0600`). When that identity is recorded, a run directory that is a link, has been replaced, or is missing refuses the write with an error instead of writing elsewhere or recreating it. On a run with no recorded identity, or one whose device number changed (a reboot or remount) while its inode did not, the write still refuses a link at the run directory, and each writer records one `journal-unpinned` row (`reason`: `no-record` or `stale-device`) before its first row. `resume` records or re-binds the identity before its own first row.

Re-reads of `state.json` by a caller that already holds the run's state (the locked re-checks in `resolve` and in the TUI's replan and re-arm, and the TUI state watcher after its first read) go through the same pinned descriptor, and so do the engine's journal reads. A run directory or `runs/` that no longer matches the recorded identity, or a link at the file, refuses with an error instead of reading another tree's record. The first `state.json` read and journal reads with no run state in hand use plain paths. The TUI journal pane reads by path too, and shows a fault when the run directory no longer matches the identity or `journal.jsonl` is a link. The other run-directory files (`engine.pid`, `ATTENTION`, `tasks/<task_id>/heartbeat.json`, adapter `.jsonl` records, `logs/`) use plain paths.

## State root

Out-of-tree state is user-scoped. The first rule that applies sets the root:

| Platform | Root                                                                                                                                                       |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| any      | `$BMAD_LOOP_STATE_DIR`, used as-is (see [environment variables](configuration.md#environment-variables)). Must be absolute; an empty value reads as unset. |
| POSIX    | `$XDG_STATE_HOME/bmad-loop` when that variable is absolute, else `~/.local/state/bmad-loop`                                                                |
| Windows  | `%LOCALAPPDATA%\bmad-loop\state`, else `%USERPROFILE%\AppData\Local\bmad-loop\state`                                                                       |

Each project gets a subtree keyed by the first 16 hex characters of the SHA-256 of its resolved path (`runs.project_tag`). Moving or renaming a project changes its key; the old subtree is left behind and `clean` does not reach it.

```text
<state root>/
├── locks/                      advisory-lock files, shared across this user's projects
└── <project key>/
    ├── <run-id>/               the run's control plane
    │   ├── events/             hook-event files written by `bmad-loop relay` (BMAD_LOOP_EVENTS_DIR)
    │   └── config-digest       baseline digest of the host-exec config, checked on resume
    ├── _mux/                   per-project multiplexer registry (psmux)
    ├── _plugin-snapshots/      verified copies of granted project plugins
    └── plugin-grants.json      project-plugin trust grants (`bmad-loop plugins`)
```

## Project `.bmad-loop/` directory

| Path                                 | Contents                                                                                                | Gitignored by `init` |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------- | -------------------- |
| `.bmad-loop/policy.toml`             | per-machine [policy](configuration.md)                                                                  | yes                  |
| `.bmad-loop/runs/<run-id>/`          | run directories (below)                                                                                 | yes                  |
| `.bmad-loop/cache/`                  | plugin caches                                                                                           | yes                  |
| `.bmad-loop/archive/<run-id>.tar.gz` | runs compressed by `archive` or `clean`                                                                 | no                   |
| `.bmad-loop/decisions.json`          | sweep decisions answered for the next sweep (BMAD v7: one `decisions-<initiative>.json` per initiative) | no                   |
| `.bmad-loop/operator/`               | per-story `awaiting-operator` park records (committed; a tickets park also records `source` and `ref`)  | no                   |
| `.bmad-loop/profiles/*.toml`         | project adapter-profile overlays                                                                        | no                   |
| `.bmad-loop/plugins/`                | project-local plugins                                                                                   | no                   |

## Run directory

```text
.bmad-loop/runs/<run-id>/
├── state.json                  resumable run state
├── journal.jsonl               append-only engine decision log
├── engine.pid                  engine pid and process identity; rewritten on resume
├── sweep.json                  sweep runs: the launch options
├── tasks/<task_id>/            one per agent session (see Task directory)
├── logs/<task_id>.log          session log (pane capture; opencode: the server transcript)
├── logs/<task_id>.server.out   opencode: the server's own stdout/stderr
├── logs/<task_id>.provider-errors.log  opencode: one line per provider-error event
├── logs/<task_id>.sse.jsonl    opencode: structured trace of the raw SSE frames
├── verify/                     verifier stdout/stderr streams the journal points at
├── worktrees/                  per-story git worktrees ([scm] isolation = "worktree")
├── stop-request.json           pending stop request (see Stopping a run)
├── ATTENTION                   human-readable alerts appended by gates
├── crash.txt                   traceback of an unexpected engine exception
├── feedback/<story>-<n>.md     verification-failure feedback handed to the next session
├── deferred/<story>/           stashed spec of a deferred story
├── failed/<unit>/changes.patch diff of a dropped unit
├── resolve/<story>/            escalation context.json and the resolve agent's resolution.json
├── restore/<story>.patch       tickets mode: a plan's intent-gap patch, moved out of the tree at re-arm
├── ctl-window                  TUI: the control window that last accepted this run
├── decisions.json              sweep runs: run-scoped triage decisions
└── bundles/<name>/intent.md    sweep runs: per-bundle intent
```

Entries other than `state.json`, `journal.jsonl`, `engine.pid`, `tasks/` and `logs/` appear only when the run reaches the step that writes them. Plugins may add their own files.

The opencode adapter's environment-fault classifier scans `<task_id>.server.out` and `<task_id>.provider-errors.log`, never the model-written `<task_id>.log`. On OpenCode 2.x provider failures appear only as session events, so the adapter writes each `session.retry.scheduled` / `session.step.failed` / `session.execution.failed` frame as one `provider-error event=… type=… status=… attempt=… message="…"` line built from the event's structured fields (the provider's response body is never copied). The file is created at spawn on both majors and is empty on a healthy session.

- **`state.json`** — the serialized run state (stories, phases, tokens, the `finished` / `stopped` / `crashed` flags, and `paused_reason` with the pause context). Written atomically under a per-run lock, through the pinned run-directory descriptor above; `bmad-loop status` reads it.
- **`journal.jsonl`** — append-only, one JSON object per line with `ts` and `kind`; the first entry is `run-start` (after one `journal-unpinned` row on a filesystem that reports no inode numbers). Lines end in LF on every platform. Written through the pinned run-directory descriptor by the engine, and by commands that act on the run (for example `stop`, `resume`, `resolve`). From the first session launch on, lines carry `log_task` / `log_pos`, pointing into the most recent session's log.
- **`heartbeat.json`** (in `tasks/<task_id>/`) — the wait loop's proof of life, overwritten at most every 30 seconds while a session runs: remaining budget, stall-recovery state and probe or sampling failure counts.

`state.json` records the run's work source. A bmad-loop that does not know that source refuses to resume the run rather than drive it as a sprint run, so finish or abandon a run before downgrading bmad-loop.

A tickets run's `state.json` also records `initiative` and `initiative_folder`, the BMAD v7 initiative the run is pinned to: every ticket-tree read refuses while the live `[core] active_initiative` names another. Each task carries a `ticket` binding resolved when it was picked (ref, id, epic slug and id, title, plan, risk, and the refined, hitl and checkpoint flags). `ticket_epic_filter`, `ticket_story_approvals`, `ticket_epic_closures`, `ticket_epic_closure_resumed` and `ticket_epic_closure_acked` hold the epic selector, the refs whose story file an operator approved (resuming the pick's `plan-checkpoint` pause records the ref), each finished epic's closure (its retrospective `verdict`, or `unknown`), the epics whose closure pause a resume answered (a closure recorded without one pauses again), and the epics whose one closure pause was resumed past. Tickets pauses add the `paused_stage` values `ticket-hitl`, `ticket-refine` and `ticket-tree` (causes below). `run` resolves a tickets run's selectors before it starts: `ticket_epic_filter` holds the `--epic` epic slug, `story_filter` the `--story` ticket ref, and `epic_filter` stays null.

A v7 sweep records `initiative` and `initiative_folder` too, and its resume, `resolve` and the dashboard's re-arm refuse while the live `[core] active_initiative` names another, because its ledger and pre-answers belong to the initiative it started on.

A pick pause journals `ticket-pick-halt` with its `cause` and `stage`:

| `cause`                                                                                       | `paused_stage`    |
| --------------------------------------------------------------------------------------------- | ----------------- |
| `hitl`                                                                                        | `ticket-hitl`     |
| `refine`                                                                                      | `ticket-refine`   |
| `plan-checkpoint`                                                                             | `plan-checkpoint` |
| `awaiting-closure`, `epic-closure`, `epic-rejected`                                           | `epic-boundary`   |
| `blocked-plan`                                                                                | `escalation`      |
| `foreign-active`, `stalled`, `tree-fault`, `pin-mismatch`, `dirty-initiative`, `ignored-plan` | `ticket-tree`     |

`store_topology` is the ticket store classified at the start of a tickets run or a v7 sweep: `layout`, `placement`, `versioning`, `store_root` and `store_repo`. Resume, resolve and the TUI re-classify the store and refuse when it differs, or when the initiative folder is gone. When no checkout carries the store, every artifact dir under it stays at the main checkout for a worktree unit, and the mount's `_bmad/custom/config.user.toml` is rewritten so `[core] output_folder` names that store.

For a store tracked in the code checkout (T1), the commit window marks the plan itself, so the marked plan rides the unit's own commit. The save that records `COMMITTING` latches the plan's status and the sha256 of its bytes before that mark: `ticket_premark_status` and `ticket_premark_digest`, cleared when no T1 mark is owed or the plan cannot be read. A commit failure after a mark this window wrote puts the pre-mark bytes back. A resumed window whose mark is a no-op on a plan that reads another status than the latched one and no longer matches the digest finds it marked since that save, for example by an interrupted earlier window, and no bytes survive to restore it. If its commit then fails, the escalation names the plan left marked for a commit that does not exist, so the operator restores it by hand.

For a shared store, the unit's commit cannot carry its plan, so the ticket's terminal mark waits until the code has landed on the target: after the commit in place, or after the merge-back under worktree isolation. The task latches it in the save that records `COMMITTING`. `ticket_done_pending` stands for `mark <ref> done`, and `ticket_park_pending` holds a park's `blocked_reason`. Under `mark_done = "never"` there is no mark, but a store that is its own git repository still owes the commit of the `built` plan the session wrote there: `ticket_commit_pending`. A resume makes any latched mark whose code had landed before it picks (`resume-ticket-mark`), and replays a lost merge-back first. A mark already made is a no-op. The latch is cleared once the mark is made. A mark that fails pauses the run at `ticket-tree` and keeps the latch.

A store that is its own git repository (T3 nested, T4 external) then gets a plan-only commit there, `chore(tickets): <ref> <status> [bmad-loop <run>]`, holding the marked plan and the deferred-work ledger when the task wrote to it. Its `Bmad-Code-Repo`, `Bmad-Code-Commit` and `Bmad-Code-Range` trailers name the code it describes. Nothing else in the store is staged, and an epic's retrospective document is committed there the same way. A store commit is bookkeeping: a failure (`store-commit-failed`), or a human's staged version of a path it would overwrite (`store-commit-foreign-dirt`), keeps the latch, never pauses mid-run, and is listed by `bmad-loop status`. The run retries it before it can finish (`run-end-ticket-mark`); one still owed pauses at `ticket-tree` (`store-commit-owed`), and the resume retries it. A retrospective document whose store commit fails pauses at `epic-boundary` before the epic's closure is recorded, and the resume commits it before recording the closure. An unversioned store (T2, or a plain folder outside the checkout) journals `store-unversioned` once per run.

## Task directory

`tasks/<task_id>/` is created per agent session. The task id is `<story_key>-<part>-<n>`, where `part` is `dev`, `review`, `triage` or a workflow label and `n` is the attempt or review cycle; a re-armed story adds `-g<generation>`.

| File                      | Written by        | Contents                                                                                                                                                         |
| ------------------------- | ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `prompt.txt`              | adapter           | the session prompt, rewritten each launch                                                                                                                        |
| `result.json`             | session           | the session's result contract (verdict, escalations)                                                                                                             |
| `escalation.json`         | sweep session     | escalations raised during triage                                                                                                                                 |
| `heartbeat.json`          | adapter           | proof of life (above)                                                                                                                                            |
| `resultless-stops.jsonl`  | built-in adapters | one line per Stop event whose result could not be read                                                                                                           |
| `session-lifecycle.jsonl` | built-in adapters | one line per lifecycle event (timeout, budget trip, environment-fault classification)                                                                            |
| `messages.json`           | opencode adapter  | raw server messages, kept as the transcript; the shape is the server's own (1.x: each message's info and parts; 2.x: user/assistant/idle messages, oldest first) |

`result.json` and `escalation.json` are the shared cycle artifacts (`journal.TASK_CYCLE_ARTIFACTS`). Both built-in adapters delete them before launching a session, so a reused task id never inherits a previous session's result. Adapters refuse a task directory that is a symlink or junction, and an adapter-written file (heartbeat, breadcrumb logs, `messages.json`, session logs) that is a link or special file.

## Run ids

A run id is `YYYYMMDD-HHMMSS-<4 hex>` in local time, for example `20261012-011529-6a63`. The segment after the last `-` is the short ref used by [run references](cli.md#run-references). Retention reads the run's age from the timestamp prefix.

## Multiplexer sessions

| Object          | Name                                                                                                                                    |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| agent session   | `bmad-loop-<run-id>`, one per run, tagged with the project key (`@bmad_project`); multiplexer-driven adapters only (opencode runs none) |
| agent window    | one per agent session, named after the task id (last 40 characters); its pane is piped to `logs/<task_id>.log`                          |
| control session | `bmad-loop-ctl`; on psmux one per project registry. A `bmad-loop-ctl-<16 hex>` session also counts as one, but nothing launches into it |
| control window  | `<kind>-<run-id>`, where `kind` is `run`, `sweep`, `resume` or `resolve`; opened by the TUI, parks after the command exits              |

Teardown:

| Event                                    | Agent session `bmad-loop-<run-id>`                                  |
| ---------------------------------------- | ------------------------------------------------------------------- |
| an agent session ends                    | its window is killed; the session is kept                           |
| run finishes, or a graceful stop ends it | killed when `[adapter] cleanup_session_on_finish` is true (default) |
| hard stop, SIGTERM or SIGINT             | killed                                                              |
| engine crash                             | killed; `crash.txt` written; run marked `crashed`                   |
| run pauses                               | kept                                                                |
| engine process dies                      | left behind (the run reads as `interrupted`)                        |
| `resume`                                 | a leftover session is killed before the relaunch                    |
| `bmad-loop cleanup` / TUI `c`            | killed for this project's runs whose engine is not provably alive   |

## Derived run status

`list` and the TUI show the first status that applies:

1. `finished` — `state.json` has `finished`
2. `paused` — `state.json` has a non-null `paused_reason`
3. `stopped` — `state.json` has `stopped`
4. `crashed` — `state.json` has `crashed`
5. `running` — the engine process is alive
6. `interrupted` — the engine process is dead
7. `unknown` — engine liveness cannot be determined, or `state.json` cannot be read

## Stopping a run

Both stop modes write `stop-request.json` (`{"requested_at": …, "mode": "hard" | "graceful"}`) into the run directory; the engine consumes it.

|                  | Hard: `bmad-loop stop` (TUI `x`)                                                        | Graceful: `bmad-loop stop --graceful` (TUI `S`)                                   |
| ---------------- | --------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Request          | replaces any pending request                                                            | lodged only if no request is pending                                              |
| Signal           | SIGTERM to the engine; force-kill after 10 s only if the process identity still matches | none                                                                              |
| Engine honors it | at the next item boundary, and mid-session (the session is aborted)                     | at the next item boundary; the in-flight story or bundle completes through commit |
| Agent session    | always killed                                                                           | killed under `[adapter] cleanup_session_on_finish`                                |
| Auto-sweeps      | —                                                                                       | suppressed                                                                        |
| End state        | `stopped`, resumable                                                                    | `stopped`, resumable from the next item                                           |

- `stop --cancel-graceful` removes a pending request of either mode.
- `resume` discards any pending request before restarting the engine.
- The engine treats SIGTERM and SIGINT (Ctrl+C in its terminal) as a hard stop. On native Windows it ignores console Ctrl+C / Ctrl+Break; use `bmad-loop stop`.
- A request still pending when the engine exits is deleted and journaled as `stop-request-discarded`.

## Reclaiming sessions and disk

|               | `cleanup`                                                                                                    | `clean`                                                                                                                                                                                                                | `delete <run>`                                                                                                                     | `archive <run>`                                                      |
| ------------- | ------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Reclaims      | multiplexer sessions and windows                                                                             | disk, per `[cleanup]`                                                                                                                                                                                                  | one run                                                                                                                            | one run                                                              |
| Removes       | `bmad-loop-<run-id>` sessions of this project's runs, and parked control windows whose run is no longer live | leaked worktrees; runs past retention are trimmed then archived (or deleted with `--hard` / `archive_old = false`); runs inside retention are trimmed when `trim_artifacts` is set; orphaned control-plane directories | the run directory, its git worktree entries, its control-plane directory                                                           | same as `delete`, after writing `.bmad-loop/archive/<run-id>.tar.gz` |
| Eligible runs | engine not provably alive (any status, including paused, and runs whose directory is gone)                   | `finished` or `stopped`, engine not provably alive                                                                                                                                                                     | any                                                                                                                                | any                                                                  |
| Never touches | live runs, other projects' sessions and windows, the window you run it from                                  | `--keep` runs, paused / crashed / interrupted / running runs, runs whose agent session may be live                                                                                                                     | a live engine or agent session; `--force` hard-stops a live engine first and skips the session guard (the session is left running) | same as `delete`                                                     |
| TUI           | `c`                                                                                                          | —                                                                                                                                                                                                                      | `D`                                                                                                                                | `A`                                                                  |

- "Trimmed" means the heavy `worktrees/` and `verify/` entries are removed; a trimmed run stays resumable.
- Retention: concluded runs are counted oldest first; all but the newest `[cleanup] run_retention` (or `clean --retain N`) are past retention. When `retention_days` is above 0, a run must also be older than that many days.
- Unverifiable engine liveness produces a warning; it does not block.
- `[cleanup] auto_clean_on_finish` makes each `run` and `sweep` start by tearing down worktrees left under finished runs.

See the [`[cleanup]` settings](configuration.md#cleanup).
