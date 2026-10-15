# Recovering paused runs

A run pauses when it needs a human: an escalation, an environment fault, a merge-back that failed, or a gate. This guide shows how to find out why a run paused and which command clears each pause. Deferred-work pauses (hard gates, ledger faults, migration) are covered in the [sweep guide](sweep-guide.md).

## Where a pause shows up

- **Notification.** A desktop notice (`[notify] desktop`, default on) and a line appended to `.bmad-loop/runs/<run-id>/ATTENTION` (`[notify] file`, default on). If neither channel works, the run warns that no alert channel is configured. Displayed text is capped at 2000 characters; the full reason is in `journal.jsonl`.
- **`bmad-loop status <run-id>`** shows the pause stage, the paused story and the reason. A parked attempt's recovery ref appears as `[ref]` (`preserve_ref` in `status --json`).
- **The TUI** shows the same, and its keys launch the commands below: `R` resolve, `V` re-verify, `e` resume, `p` review the paused run.

| Pause stage                           | Cause                                                                                                                                                                                                                 | Clear it with                                                                 |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `escalation`                          | a CRITICAL escalation, or a story still escalated at run end                                                                                                                                                          | [`bmad-loop resolve`](#resolving-an-escalation)                               |
| `environment`                         | an environment probe failed before a session launched                                                                                                                                                                 | fix the environment, then `bmad-loop resume` ([details](#environment-faults)) |
| `story-gate`                          | a ledger `gate:`, or a ledger that cannot be read                                                                                                                                                                     | see the [sweep guide](sweep-guide.md#hard-gates)                              |
| `spec-approval`, `epic-boundary`      | a configured gate or a retrospective                                                                                                                                                                                  | `bmad-loop resume`                                                            |
| `plan-checkpoint`, `story-checkpoint` | a stories- or tickets-mode checkpoint                                                                                                                                                                                 | `bmad-loop resume`                                                            |
| `ticket-hitl`, `ticket-refine`        | tickets mode: the next ticket is a hitl ticket, or needs refining                                                                                                                                                     | build or refine it, then `bmad-loop resume`                                   |
| `ticket-tree`                         | tickets mode: the active initiative changed, `tickets.py` failed (including a park or post-rollback re-arm mark), only tickets the run did not start are in the way, or the initiative folder has uncommitted changes | fix the tree, or commit or stash the changes, then `bmad-loop resume`         |

## Escalations

Escalations come in two severities:

- **CRITICAL**: the story moves to `escalated`, `story-escalated` is journaled, you get a `CRITICAL escalation: <story>` notice, and the run pauses at `escalation`.
- **PREFERENCE**: journaled as `preference-escalation`; the run continues.

A story stays escalated until you resolve it. A plain `bmad-loop resume` skips it and runs the rest of the queue, then pauses again with `story '<key>' is still escalated — run bmad-loop resolve <id>`. A run is never marked finished while a story is escalated.

## Resolving an escalation

`bmad-loop resolve <run-id>` works on a run paused at `escalation` whose engine is not running. `--story <key>` picks a story other than the paused one. By default it asks before it re-arms and then resumes the run itself:

- `--resume` re-arms and resumes without asking.
- `--no-resume` only re-arms and prints `resume when ready: bmad-loop resume <id>`.
- `--force` proceeds when the engine's liveness cannot be verified. A running engine always blocks: run `bmad-loop stop <run-id>` first.

`--restore-patch`, `--adopt-branch`, `--reverify` and `--abandon` are mutually exclusive.

### Re-arm with the resolve agent

1. `bmad-loop resolve <run-id>` (TUI: `R`). An interactive resolve agent starts with the escalation and the frozen spec.
2. Work through the problem with the agent until the spec is correct, then exit the agent.
3. Answer `re-arm <story> and resume run <id>?`. `y` resets the story to `pending` and its spec to `ready-for-dev`, and resumes. `N` leaves the run paused.

If the agent exits without writing a resolution, resolve says `no resolution recorded for <story>` and still asks. Answer `N` to stay paused.

### Re-arm after fixing the spec yourself

1. Edit the frozen spec. Under worktree isolation, commit the edit.
2. `bmad-loop resolve <run-id> --no-interactive`. No agent runs. Answer the re-arm prompt, or pass `--resume`/`--no-resume`.

Use this path too when the dev CLI has no interactive mode.

### Restore an intent-gap patch

When an escalation names a saved patch (in the spec's `## Auto Run Result`), you can re-apply it instead of re-implementing:

1. `bmad-loop resolve <run-id> --no-interactive --restore-patch <path>`. (The resolve agent can record the same choice as `restore_patch` in `resolution.json`.)
2. The spec is re-armed to `in-review` and the patch re-applied, so the re-drive resumes at review. A patch that does not apply escalates again.

In-place runs only: it is refused under worktree isolation, for a task with a worktree, and for a story with no recorded spec.

### Resolve in tickets mode

A tickets run's frozen spec is the ticket's plan, and its intent is the plan's `<intent-contract>` block. The resolve agent's context adds a `tickets` block: the ticket's ref, the plan's status and blocked reason, and an excerpt of its `## Auto Run Result`.

The re-arm moves the plan through `tickets.py mark` (reason `rearm`): to `ready-for-dev`, or `in-review` for a restore. This clears the plan's `blocked_*` fields. A move the plan transition table refuses aborts the re-arm, and the ticket stays escalated. An aborted re-arm puts the plan back byte for byte.

build-auto saves an intent-gap patch beside the plan as `<plan>.patch`. The re-arm moves it to the run's `restore/` directory either way, because a patch left in the tree fails the re-drive's clean-tree check. A restore that names the patch latches the moved copy. A later `--restore-patch` naming the original path finds the copy.

### Adopt the kept branch

Under worktree isolation, when the escalation was a false alarm and the kept branch is good:

1. `bmad-loop resolve <run-id> --adopt-branch [--story <key>]`. A warning says that review, the `[verify]` commands and `pre_commit_gate` are **not** re-run.
2. Confirm `adopt kept branch …?`, or pass `--resume`/`--no-resume`.
3. On resume the branch is committed and merged, and the story becomes `done` (or `awaiting-operator` if it declared operator actions). No session runs.

Refused for sweep runs, when there is no kept worktree or branch, when the worktree directory is gone, and when there is no spec. Not offered in the TUI.

<a id="resolve-reverify"></a>

### Re-verify kept work

When a story was deferred, or escalated by an environment fault, and its work is sound, replay its verification instead of re-implementing it:

1. Fix the environment first (container, database, credentials).
2. `bmad-loop resolve <run-id> --reverify` (TUI: `V`, which also lists other deferred worktree units). For a worktree unit, add `--story <key>`; that works under any pause stage.
3. Read the summary: the target, HEAD and baseline, and a warning that commits and changes made since the pause go into the story's squashed commit. Confirm, or pass `--resume`/`--no-resume`.
4. On resume, the `[verify]` commands re-run on the kept work. On a pass the story is reviewed per policy and committed (a unit is merged). A failure is never retried: a deferred story is deferred again with `reverify failed: …`, and an escalated story (or an environment fault or contradiction) pauses with that reason.

No dev session and no resolve agent run. `--reverify` is refused, with the run unchanged and the remedy named, when:

- it is a sweep run;
- the escalation is not an environment fault a replay can clear (use plain `resolve`);
- there is no completed dev result, or the latest dev session failed or was partial;
- an in-place story is not the one the run stopped on, or a later story was already picked;
- the baseline is not an ancestor of HEAD, or nothing sits above the baseline (the attempt was rolled back);
- a worktree unit's worktree is gone, on the wrong branch, or has nothing above its baseline.

Plain `resolve` on a deferred story points you here: `to re-verify its kept work: bmad-loop resolve <id> --reverify`.

<a id="resolve-reverify-finished"></a>

### Replay a finished run's kept unit

A finished worktree run can keep deferred units. To replay one:

1. Check out the run's target branch in the main checkout and make sure it is clean.
2. `bmad-loop resolve <finished-run> --reverify --story <key>`. `--story` is required. Not available for stories-mode runs.
3. Confirm. A new replay run is created, the unit's worktree moves into it, verification replays, review follows policy, and the unit merges into the target branch. The original run stays finished.
4. If the replay run pauses at `escalation` (journaled `replay-merge-refused`) because the story is already `done`, close it with `--abandon` (below). If the board could not be read, fix it, then `bmad-loop resolve <replay-run> --adopt-branch`.

Replay before the next `run` or `sweep` starts: `[cleanup] auto_clean_on_finish` (default on) reclaims kept worktrees of finished runs.

### Abandon a replay run

When an escalated replay run is no longer needed:

1. `bmad-loop resolve <replay-run> --abandon`. `--no-interactive` skips the confirm. Do not combine it with `--resume` or `--no-resume` (usage error).
2. The unmerged changes are parked on `refs/attempt-preserve-dirty/<run>-<head8>-abandon` and the printed ref names it. The worktree is removed, the branch is kept, and the run is recorded finished. The board, the ledger and the original run are untouched.
3. Copy the ref to a branch if you need it longer than `scm.preserve_keep` keeps it.

Refused on a run that is not a replay run, a story that is not escalated, or a story with no kept unit.

### When resolve does not resume

- `NOT resuming in this gesture — …`: the story is re-armed, but the correction must reach the re-drive first. Do what the line says (commit the corrected spec on the named branch, correct it in the main checkout, or restore the recorded spec path), then `bmad-loop resume <run-id>`.
- `the escalation for <story> changed while resolve was in progress — not re-arming`: the run moved on. Check `bmad-loop status` and start over.
- `run <id> is still live — stop it first`: run `bmad-loop stop <run-id>`; `--force` does not help.
- `run <id>: engine may still be live (unverifiable pid) — refusing to re-arm …`: confirm the engine process is gone, then re-run with `--force` (`stop` cannot clear an unverifiable pid).

## Preserved attempts

Before work is reset, bmad-loop parks it on git refs:

| Ref                                        | Holds                                                                 |
| ------------------------------------------ | --------------------------------------------------------------------- |
| `attempt-preserve/<run>-<tip8>` (a branch) | an attempt's commits above the baseline                               |
| `refs/attempt-preserve-dirty/<run>-…`      | a snapshot commit of uncommitted work, or of an abandoned replay unit |
| `refs/merge-preflight-preserve/<sha>`      | your edits in the main checkout restored before a merge-back          |

`scm.preserve_keep` (default 20) is the number of refs kept per family. Older ones are pruned when a run starts; `0` keeps them all. Copy a ref to a branch to keep it for good.

To recover work:

1. Inspect: `git log --oneline <baseline>..attempt-preserve/<run>-<tip8>`, or `git diff <snap>~1 <snap>` for a dirty snapshot.
2. Restore after the run has stopped. For an in-place defer, the notice gives the exact line: `git -C <root> merge --ff-only <preserve_ref>`. Otherwise `git cherry-pick <sha>` or `git restore --source=<snap> -- <path>`.
3. If a preserve step failed, the ATTENTION line gives a `git branch <name> <head>` to run before `git gc` prunes the commits, or points you to the reflog. A failed snapshot of uncommitted work means that work was not saved.

### Manual rollback pause

In place, `[scm] rollback_on_failure` defaults to `false`: instead of resetting, a failed attempt pauses with an `ACTION REQUIRED` notice and the tree as it was. To recover:

1. Save the work: `git -C <root> branch my-rescue HEAD`, or `git -C <root> diff > rescue.patch` plus any new untracked files.
2. Check whether any of it was merged, pushed or opened as a PR.
3. `git -C <root> reset --hard <baseline>` and remove leftover untracked files.
4. `bmad-loop resume <run-id>`.

To keep the commits instead, check `git log --oneline <baseline>..HEAD`, then run `bmad-loop resume <run-id> --accept-baseline`. It adopts everything at HEAD as the new baseline of each in-place story the resume restarts, and treats every current untracked file as pre-existing. Uncommitted tracked changes still go through the normal rollback. You do not need it right after `resolve` (the re-arm already advances the baseline), but commits you made after `resolve --no-resume` do.

## Plateau-defer

When a story runs out of budget without a CRITICAL reason (`[limits] max_dev_attempts`, default 2; `[limits] max_review_cycles`, default 3; or a review timeout with `[review] on_timeout = "defer"`), it is deferred and you get a `story deferred: <story>` notice. What happens next depends on where the attempt ran:

- **In place, `[scm] rollback_on_failure = false` (the default), with attempt work in the tree:** the tree is not rolled back and the run pauses for a [manual rollback](#manual-rollback-pause). The notice points to `bmad-loop resolve <run-id> --reverify` for the kept work.
- **In place with `rollback_on_failure = true`, or a clean tree:** the attempt is parked and reset, its spec is stashed under the run's `deferred/<story>/`, and the run continues. The notice gives the recovery ref and the `git merge --ff-only` line that restores it (marked commits-only when the uncommitted part could not be saved).
- **Under worktree isolation:** the unit's branch is kept, the run continues, and the notice names the branch plus any earlier attempt's ref.

To finish a deferred story, use [`resolve --reverify`](#resolve-reverify) while the run is paused on that defer, or for a kept worktree unit. After an auto-rollback, restore the ref and re-run the story instead. A story being re-driven after you resolved it re-escalates instead of deferring.

## Human-only actions

A story that needs an action no agent can take (buy a domain, publish a DNS record, issue an API key) commits everything an agent can do, lists what is owed in its spec's `operator_actions:`, and parks at `awaiting-operator`. The run continues. To finish it:

1. `bmad-loop confirm --list` shows the parked stories and what each owes (`--json` for scripts).
2. Do the actions.
3. `bmad-loop confirm <story-key>`. You acknowledge each action in turn (`--yes` skips the prompts). The spec gets an `## Operator Confirmation` section, spec and board move to `done`, and the change is committed.
4. Add `--reverify` to re-run the environment probes and `[verify]` commands first; a failure blocks the confirmation.

If `confirm` is interrupted, run the same command again; it finishes without prompting twice. If the park is not found, pull the branch that carries the park commit. If the park record has drifted, `confirm` refuses and changes nothing: fix what `bmad-loop validate` reports (`operator.*` checks). `[operator] enabled = false` turns parking off.

In tickets mode the park lives on the ticket's plan, not a board. The unit's commit marks the plan `blocked` with a reason starting `awaiting operator: ` that lists the actions, and the park record names the ticket ref. Tickets that wait on it end the run with a parked summary. `confirm <ref>` appends the same section, then marks the plan through `tickets.py mark`: `done` under `[tickets] mark_done = "auto"`, `built` under `"never"`. A review that demotes a built ticket parks the same way under `[operator] on_review_demotion = "park"`.

## Environment faults

An environment fault is a failure caused by the environment rather than the code. It never charges a dev attempt.

How faults are detected:

- `[environment] probes`: health-check commands run before verify, before each session launch, and before a failed attempt is charged. A failing probe is a fault.
- `[verify] env_fault_rc`: a verify command exiting with that status (0 disables it). Exit 126 or 127 always counts.
- A provider error in the session's log tail that matches the CLI profile's `env_fault_patterns` (claude ships patterns for an "API Error … Unable to connect to API"-style failure; extend them in a profile overlay). A session that consumed tokens is not treated as an environment fault.

What to do depends on where it hit:

**Before a session launched** (pause stage `environment`):

1. Fix the environment.
2. `bmad-loop resume <run-id>` (TUI: `e`). The probes run again. If they pass, the same session launches with no rollback (`env-fault-cleared`); if not, the run pauses again unchanged.

`resolve` refuses this pause: `an environment pause needs no resolve: fix the environment, then bmad-loop resume <id>`.

**During verify or after a session** (a CRITICAL escalation with an `env_fault_site`):

1. Fix the environment.
2. If the work is sound: `bmad-loop resolve <run-id> --reverify` ([details](#resolve-reverify)). The notice offers this only when a replay can clear that site.
3. Otherwise: `bmad-loop resolve <run-id> --no-interactive` to re-arm (restoring the attempt budget) and resume. The spec needs no fix, so the resolve agent is not needed.

| `env_fault_site`                                                                                 | `--reverify` can clear it?                     |
| ------------------------------------------------------------------------------------------------ | ---------------------------------------------- |
| `verify:dev`, `verify:fix`, `verify:review`                                                      | yes                                            |
| `probe:decision:review`, `probe:decision:workflow`, `probe:claim:review`, `probe:claim:workflow` | yes                                            |
| `probe:decision:dev`, `probe:decision:fix`, `probe:claim:dev`, `probe:claim:fix`                 | yes, if the latest dev session completed       |
| `probe:dispatch:dev`, `probe:dispatch:review`                                                    | no: it is an `environment` pause; use `resume` |

## Worktree merge-back failures

Under `[scm] isolation = "worktree"`, each story merges back into the target branch in the main checkout. When the merge fails, the unit escalates: its branch `bmad-loop/<run-id>[/<story>]` and worktree are kept, the diff is saved to `.bmad-loop/runs/<run-id>/failed/<unit>/changes.patch`, and the run pauses at `escalation`.

To recover:

1. Read the escalation reason. It names the failure (table below).
2. Fix the main checkout as the table says.
3. Finish the story:
   - `bmad-loop resolve <run-id> --adopt-branch` merges the kept branch as it is (not for sweep runs; review and verify are not re-run);
   - or `bmad-loop resolve <run-id>` (or `--no-interactive`) to re-arm and re-drive the story.

   A plain `resume` does not retry the merge: the story is still escalated.

| Failure                                  | What happened                                                                                                                                                                            | Fix                                                                                                                                                             |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| merge refused at pre-flight              | git refused before starting: an untracked file would be overwritten, a staged change on an incoming path, a file/directory clash, or a fast-forward that cannot happen. Nothing changed. | fix the clash git names                                                                                                                                         |
| merge half-applied                       | git stopped partway through the checkout, usually a required clean/smudge filter that cannot run                                                                                         | if the reason says the paths were not restored, `git checkout HEAD -- <paths>` (never a repo-wide reset); remove the untracked residue it names; fix the filter |
| post-merge state unverified              | a check after the merge failed, so bmad-loop repaired nothing                                                                                                                            | `git status` in the main checkout, clear residue that is not yours, fix the cause                                                                               |
| merge commit refused, rolled back        | a `pre-merge-commit` or `commit-msg` hook, or `commit.gpgsign`, refused the commit                                                                                                       | fix the hook or signing                                                                                                                                         |
| merge commit refused, result left staged | squash strategy; the rollback was withheld or failed                                                                                                                                     | stash or commit your own work, `git reset --hard HEAD`, then fix the hook or signing                                                                            |
| merge commit refused mid-merge           | merge strategy; the abort also failed                                                                                                                                                    | `git merge --abort` (or reset to the pre-merge commit), then fix the hook or signing                                                                            |
| merge conflict                           | a real content conflict                                                                                                                                                                  | resolve it by hand                                                                                                                                              |
| stray changes in the main checkout       | staged changes to tracked files outside the branch, or changes to a board or ledger this run commits                                                                                     | commit, stash or revert them; the run never cleans these                                                                                                        |
| other git error                          | not classified                                                                                                                                                                           | `git status`; git's text names the cause                                                                                                                        |

Two pauses resume directly instead of escalating:

- `merge-target-not-checked-out`: the main checkout is not on the target branch. `git checkout <target>`, then `bmad-loop resume <run-id>`.
- A failed snapshot before the pre-flight cleanup of a sweep bundle: leave the named paths exactly as they are, fix the fault, then `bmad-loop resume <run-id>`.

Untracked strays, and tracked files changed only in the working tree, do not block a merge (`merge-target-tolerated`). If the pre-flight restored one of your edits, the journal entry `merge-target-preserved` names its `ref` and `paths`: get it back with `git show <ref>:<path>` before `scm.preserve_keep` prunes the ref.
