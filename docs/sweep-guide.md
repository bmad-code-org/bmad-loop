# Deferred-work sweeps

A sweep works through the deferred-work ledger (`deferred-work.md`), the list of follow-ups that dev and review sessions defer. A fresh triage session checks every selected open entry against the code and sorts it: already resolved, buildable bundles, blocked, skip, or a decision that needs a human. You answer the decisions. Each bundle then runs through the normal story pipeline (dev → verify → review → commit), and its review gate also requires every bundled entry to be `status: done` in the ledger.

```text
bmad-loop sweep [--only DW-1,DW-3 | --min-severity LEVEL] [--no-prompt] [--decisions-only]
                [--max-bundles N] [--repeat | --no-repeat] [--max-cycles N] [--dry-run]
  │
  ├─ TRIAGE   fresh session: /bmad-loop-sweep verifies each selected open entry against
  │           the code and returns a validated partition: already-resolved (closed by the
  │           orchestrator, with evidence) · bundles · blocked · skip · decisions
  ├─ DECIDE   attended runs ask you each decision (build / close / keep-open, with a
  │           recommendation); answers land in the ledger as `decision:` lines.
  │           Unattended runs leave decisions open.
  └─ BUNDLES  each bundle runs the normal pipeline: dev on the bundle spec → verify
              commands → review (per [review] trigger) → commit
```

## Running a sweep

1. Commit or stash everything in the code repo. A sweep refuses a dirty worktree: `git worktree is not clean — commit or stash first`.
2. Preview: `bmad-loop sweep --dry-run`. It lists the open entries and spawns nothing. It fails, naming the file and the fault, when the ledger cannot be read.
3. Run one of:
   - `bmad-loop sweep`: triage every open entry.
   - `bmad-loop sweep --only DW-1,DW-3`: triage only these open entries. Every id must exist and be open.
   - `bmad-loop sweep --min-severity high`: triage entries at `low` < `medium` < `high` < `critical` or above. Entries with no or an unknown severity are left out.
   - Add `--max-bundles N` to cap the bundles built (overrides `[sweep] max_bundles`).
4. Answer each decision when asked (see [Answering decisions](#answering-decisions)).
5. Watch it with `bmad-loop status <run-id>`. For a sweep, status prints a `sweep options:` line with `max_bundles`, `repeat` and `max_cycles`, each marked `override` or `policy`.

Variants:

- `bmad-loop sweep --no-prompt` runs unattended. It skips every decision prompt, builds only the bundles that need no decision, and sends a `N deferred-work decisions pending` notice.
- `bmad-loop sweep --decisions-only` triages and records your answers, and builds nothing.

`--only` and `--min-severity` are mutually exclusive. A paused sweep continues with `bmad-loop resume <run-id>`. An escalated bundle is resolved with `bmad-loop resolve <run-id>` (see the [escalations guide](escalations-guide.md)). `--adopt-branch` and `--reverify` do not apply to sweep runs.

### Repeat mode

`bmad-loop sweep --repeat [--max-cycles N]` (or `[sweep] repeat = true` and `max_cycles`, default 5) re-triages after each cycle and keeps going on newly deferred work. `--no-repeat` overrides the policy for one run. The run ends with a `sweep-repeat-done` journal entry whose `stop_cause` says why:

| `stop_cause`          | Meaning                                                   | What to do                                                   |
| --------------------- | --------------------------------------------------------- | ------------------------------------------------------------ |
| `no-open`             | no open entries left                                      | nothing                                                      |
| `no-selected`         | the selector matches nothing in a later cycle             | nothing, or widen the selector                               |
| `no-progress`         | the cycle completed nothing addressable                   | check the skipped and blocked entries                        |
| `max-cycles`          | `max_cycles` reached                                      | run again, or raise `--max-cycles`                           |
| `legacy-appeared`     | legacy-format entries appeared after cycle 1              | start a fresh `bmad-loop sweep` to migrate them              |
| `ledger-unreadable`   | the ledger bytes do not decode, or it is unfit to publish | see [When the ledger won't read](#when-the-ledger-wont-read) |
| `ledger-inaccessible` | the OS refused the read                                   | see [When the ledger won't read](#when-the-ledger-wont-read) |

### Archiving closed entries

1. Stop every live run. The archive refuses while any run may be live: `run <id> may still be live — stop it before archiving ledger entries`.
2. Preview: `bmad-loop sweep --archive [--before YYYY-MM-DD] --dry-run`.
3. Run: `bmad-loop sweep --archive [--before YYYY-MM-DD]`. Entries closed (`status: done <date>`) before that date move to `deferred-work-archive.md`. A stub with `status: done` and `archived:` stays in the live ledger so ids still resolve.
4. If the ledger is tracked in git, commit both files.

`--archive` takes no other sweep flag, and `--before` needs `--archive`.

### Auto-sweep

Set `[sweep] auto = "per-epic"` or `"run-end"` (default `"never"`) to start a sweep from inside `bmad-loop run`. An auto-sweep runs at most once per trigger. A failed or refused child sweep never fails the parent run. When one did not run, `bmad-loop status` prints `auto-sweep not run: <trigger> (<why>) — deferred work is untouched`. Run `bmad-loop sweep` yourself with a clean worktree. A dirty worktree is the usual cause.

### Legacy-ledger migration

A ledger with entries that are not in `DW-<n>` format is migrated automatically at the start of `bmad-loop sweep`, in one session. `--dry-run` labels such entries as pre-migration. If the migration pauses:

| Pause                                                                       | What to do                                                                                                                                                     |
| --------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `migration refused` — duplicate ids                                         | renumber or merge the duplicates, commit, `bmad-loop resume <run-id>`                                                                                          |
| `migration refused` — the tracked ledger differs from its committed version | commit the ledger, `bmad-loop resume <run-id>`                                                                                                                 |
| escalated after `max_migration_attempts` (default 2) failed validation      | restore the pre-migration ledger, delete the `migrate-*` records in the run dir, resume; or `bmad-loop resolve <run-id>` to grade the live ledger as it stands |
| `sweep-migration-recovery-invalid` — HEAD moved past the baseline           | leave HEAD where it is, clean the working tree, resume                                                                                                         |
| asked to delete `migrate-*` records by hand (native Windows)                | delete every `migrate-*` file in the run dir, resume                                                                                                           |

A migration fails validation if it drops an existing `gate:` token, changes an existing status, or breaks the numbering.

## Story-declared closure (`closes_deferred`)

A story can close ledger entries when it commits.

1. Add `closes_deferred: [DW-5, DW-6]` to the story spec's frontmatter or, in stories mode, to the `stories.yaml` entry. Both are combined.
2. Run `bmad-loop validate`. It warns on unknown ids (`deferred.closes-unknown`), a malformed declaration (`deferred.closes-malformed`) and entries whose status is neither open nor done (`deferred.closes-entry-unreadable`).
3. Run as usual. At the story's commit each entry becomes `status: done <date>` with `resolution: resolved by story <key>`. A story that fails or escalates closes nothing.

A non-list `closes_deferred` in `stories.yaml` stops the manifest from loading. In a spec it is only journaled. If the ledger cannot be read at commit time, the story still commits, nothing is closed, and you get a `declared deferred closes unapplied` notice. Repair the ledger, then run `bmad-loop sweep` to reconcile.

If the ledger lives outside the repo (`implementation_artifacts` configured outside the project tree), the annotation is still written at the commit but is part of no commit; the run journals `deferred-close-external-ledger`. The `stories.yaml` channel is hand-authored: upstream Story Breakdown does not emit `closes_deferred`, so re-deriving `stories.yaml` drops it. Note the intent beside the story if you expect to re-derive.

## Hard gates

A ledger entry can block stories until it is done.

1. In the entry, add a line `gate: 3-2, 3-3`: lowercase, at the start of the line, comma-separated story keys. A token matches a key it equals or prefixes at a `-` (and `3-2` also matches `3-2a-…`), so `3-2` matches `3-2-login` but not `3-20-x`.
2. `bmad-loop validate` fails `deferred.hard-gate` for each actionable story a gate matches, and warns `deferred.hard-gate-unstructured` for a malformed or misspelled gate line (`Gate:`, an indented line, or prose `HARD GATE:` with no `gate:` line).
3. If a run pauses at stage `story-gate` with `<story> is gated by unlanded deferred work`, either set the entry to `status: done <date>`, drop the token, or run `bmad-loop sweep`. Then `bmad-loop resume <run-id>`.

Only an explicit `status: done` releases a gate; a missing or misspelled status still gates. Sweep runs are never gated. If the ledger cannot be read, validate fails `deferred.ledger-unreadable` and a run pauses at stage `story-gate` with `<ledger> cannot be read (…)` (journaled `story-gate-unreadable`): fix the file, then resume.

## Answering decisions

Each decision offers options, and each option has one effect:

| Effect      | What it does                                        |
| ----------- | --------------------------------------------------- |
| `build`     | the entry becomes a bundle and is built             |
| `close`     | the entry is set to `status: done` now              |
| `keep-open` | the answer is recorded and the entry is not bundled |

**During a sweep.** An attended `bmad-loop sweep` asks each decision in the terminal: `choice [k1/k2/…] (enter = <recommended>)`. Enter takes the recommendation, and an invalid key asks again. In the TUI, a `⚑ decision needed` banner appears and `a` attaches to the prompt. When you attached this way, the sweep hands the terminal back once the cycle's decisions are answered and prints one of the lines below (a sweep run directly in the foreground keeps the terminal and prints none of them):

- `✓ decisions recorded — sweep continues in the background`
- `! answers saved, but not every decision reached the deferred-work ledger …`
- `! answers saved, but the deferred-work ledger is not fit to publish … — repair the ledger and re-run`

**After a sweep** (decisions an unattended or interrupted sweep left open):

1. `bmad-loop status` shows `deferred-work decisions awaiting an answer: N (bmad-loop decisions)`.
2. Preview with `bmad-loop decisions --list` (or `--json`).
3. Answer with `bmad-loop decisions`, or `d` in the TUI. Each answer prints `closed now`, `queued — the next sweep will build it`, or `kept open (recorded)`.
4. Run `bmad-loop sweep` to build the queued answers.

### Project store and run store

| Store         | Path                                      | Written by                     | Holds                                  |
| ------------- | ----------------------------------------- | ------------------------------ | -------------------------------------- |
| Project store | `.bmad-loop/decisions.json`               | `bmad-loop decisions`, TUI `d` | `build` and `keep-open` answers        |
| Run store     | `.bmad-loop/runs/<run-id>/decisions.json` | answers given during a sweep   | every in-run answer, including `close` |

An out-of-band `close` goes straight to the ledger and is not stored. A sweep reads its run store first. It uses a project-store answer only for an id that this triage asks and the run store has not answered (journaled `decision-preanswered`). `bmad-loop decisions` offers only ids that are still open, appear in a past triage, and have no usable project-store answer.

### Stale answers

A stored answer is checked against the option its key selects in the current triage (same label and effect); a mismatch is journaled as `sweep-decision-option-mismatch`. A mismatched keep-open answer is dropped as `stale-option`. A mismatched build answer is still built from its own stored intent, and is dropped only if it has none. An answer that cannot be carried out is dropped: the run journals `sweep-decision-answer-dropped` with a `drop_cause`, notifies you, leaves the entry open, and does not ask that id again for the rest of the run.

| `drop_cause`      | Meaning                                                                    | What to do                                                                  |
| ----------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `effect-unlanded` | a build answer from this run never reached the ledger (entry or file gone) | restore the entry if you still want it; a new sweep asks again              |
| `entry-not-open`  | a stored build answer names an entry that is no longer open                | nothing, or reopen the entry                                                |
| `no-intent`       | a build answer has no intent and no matching option supplies one           | answer again in a later sweep                                               |
| `name-collision`  | no unique bundle name, even after `-2` … `-9` suffixes                     | rename the clashing bundles or run again later                              |
| `stale-option`    | a keep-open answer's option is gone or was rewritten                       | `bmad-loop decisions` offers the id again; answer it if it should stay open |

Only a `stale-option` drop removes the project-store entry (committed as `chore(sweep): drop stale deferred-work pre-answer`). At the end of each cycle, store entries for ids that are no longer open are pruned (`decision-preanswers-pruned`). If the ledger cannot be read, the prune is skipped (`sweep-preanswer-prune-refused`) and the stored answers survive.

### Validating decision stores

A stored answer is usable only if it is a JSON object whose `effect` is `build`, `close` or `keep-open`, and whose `key`, `label`, `intent` and `bundle_name` are strings when present. A `close` is accepted only from the run store. `intent` and `bundle_name` count only on a `build`.

| Store problem                                     | Result                                                                                             | What to do                                                   |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| a project-store value fails the check             | treated as unanswered; `bmad-loop decisions` offers the id again                                   | answer it; `build` or `keep-open` overwrites the bad value   |
| project store missing, not JSON, or not an object | read as empty, nothing journaled                                                                   | fix or delete the file                                       |
| a run-store value fails the check                 | kept in the file, reported in `sweep-decisions-reload-failed`                                      | none needed; the id is asked again                           |
| run store cannot be read (permissions, I/O)       | prompts withheld (`sweep-decisions-prompt-withheld`), `N deferred-work decisions not asked` notice | fix the file, then run `bmad-loop sweep` interactively again |

## What gets published

The sweep commits each ledger or store change it makes as its own file-scoped commit: only that file is staged, and the whole file is committed.

| Commit message                                                              | When                                                       |
| --------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `chore(sweep): record deferred-work decisions`                              | after the decision phase                                   |
| `chore(sweep): close resolved deferred-work entries`                        | already-resolved entries and decided closes                |
| `chore(sweep): commit ledger at the sweep cycle boundary`                   | between repeat cycles                                      |
| `chore(sweep): commit ledger after recovering in-flight bundles`            | on resume, after in-flight bundles are recovered           |
| `chore(sweep): commit a ledger write an interrupted phase left unpublished` | on resume, for a write an interrupted phase did not commit |
| `chore(sweep): drop consumed deferred-work pre-answers`                     | end-of-cycle prune of the project store                    |
| `chore(sweep): drop stale deferred-work pre-answer`                         | a `stale-option` drop                                      |
| `chore(sweep): migrate legacy deferred-work entries to DW format`           | legacy-ledger migration                                    |
| `chore(sweep): ingest retro action items`                                   | retro action items added to the ledger at sweep start      |

`bmad-loop decisions` commits each file it wrote separately as `chore(decisions): pre-answer <id>`: the ledger only if a `decision:` line landed, and the project store only for `build` or `keep-open`. A ledger the sweep marked as in doubt blocks every further ledger publish for that run (`sweep-ledger-commit-withheld`).

### Publication refusals

Before committing, the sweep checks the target file. A refusal journals `sweep-ledger-commit-refused` with a `refuse_cause`. A ledger refusal also marks the ledger as in doubt, which withholds later bundles. `bmad-loop decisions` reports the same causes in a `not committed to git: <file> (<cause>)` note, keeps the answer on disk, and still exits 0.

| `refuse_cause`       | Meaning                                                                                 | Fix                                                 |
| -------------------- | --------------------------------------------------------------------------------------- | --------------------------------------------------- |
| `target-absent`      | the file is missing (including a dangling symlink)                                      | restore the file or correct its path                |
| `target-not-a-file`  | the path is a directory, FIFO, device or socket                                         | put a regular file there                            |
| `target-undecodable` | the ledger is not valid UTF-8 (ledger only)                                             | repair the encoding                                 |
| `target-unreadable`  | the OS refused the probe or read (permissions, I/O, symlink loop)                       | fix the path, permissions or storage                |
| `commit-unavailable` | `bmad-loop decisions` only: git ran and failed (not a repo, ignored path, locked index) | commit by hand, or ignore it for a file outside git |

An empty ledger is publishable. A store that is not valid JSON is still publishable.

### Diagnostics

`bmad-loop diagnose [run-id]` collects the publication entries in a "Sweep publication and repeat stops" section. For the raw `message`, `repo` and `error` text, read `.bmad-loop/runs/<run-id>/journal.jsonl`.

| Journal kind                        | Meaning                                                                                           |
| ----------------------------------- | ------------------------------------------------------------------------------------------------- |
| `sweep-ledger-commit`               | committed (`message`, `commit`, `file`)                                                           |
| `sweep-ledger-commit-clean`         | nothing to commit; also seen when the path is gitignored                                          |
| `sweep-ledger-commit-refused`       | refused before staging (`refuse_cause`)                                                           |
| `sweep-ledger-commit-unavailable`   | the ledger is in no git repo, or a store commit failed (`repo`, `error`)                          |
| `sweep-ledger-commit-withheld`      | blocked because the ledger is in doubt (`reason: ledger-in-doubt`)                                |
| `sweep-bundles-withheld`            | bundles not dispatched (this cycle's, or in-flight ones on resume) because the ledger is in doubt |
| `sweep-bundle-skipped`              | a bundle skipped whole (`entry-not-open`, `failed-or-escalated-earlier`, `human-chose-keep-open`) |
| `sweep-decision-effect-unavailable` | an answer's ledger effect did not land                                                            |
| `sweep-decision-effect-reapplied`   | a stored `close` the ledger had not taken was applied again                                       |
| `sweep-repeat-done`                 | a repeat run stopped (`stop_cause`)                                                               |

A ledger `git status` fault other than "not a repository", or a commit git refuses, ends the sweep as a crash: the run records `run-crash` and writes `crash.txt` in the run dir.

## Recovering unpublished decisions and bundles

**An out-of-band answer was not committed** (`not committed to git: …`):

1. Fix the cause using the [refusal table](#publication-refusals).
2. Commit the named file yourself, e.g. `git add <file> && git commit -m "chore(decisions): pre-answer DW-7"`. Skip this for a file outside git.
3. Do not answer again: the answer is on disk and the next sweep reads it.

**`no decision line was written`.** The entry or the ledger file was removed while the prompt was open (`: the ledger file is gone`, `: the ledger holds no entry for this id`) or could not be checked (`; ledger state unavailable`). A build or keep-open answer is still saved to the project store and is used only if the id is open again. Nothing needs undoing.

**`error: could not record DW-N: …`** (exit 1, the walk stops). Fix the cause it names (config changed mid-prompt, undecodable ledger, lock or write error) and run `bmad-loop decisions` again. Answered ids are not offered again.

**A sweep paused at `story-gate` because a bundle's intent cannot be rebuilt** (`sweep-intent-regen-refused` or `sweep-intent-ledger-refused`):

1. Restore the ledger file or the missing entries, or fix the read fault.
2. Commit the fix if the ledger is tracked.
3. `bmad-loop resume <run-id>`. The intent is regenerated and the bundle re-driven.

**A sweep paused because a bundle close could not read the ledger** (`sweep-bundle-close-refused`): repair the ledger, then `bmad-loop resume <run-id>`. The close repeats without a new session, except after a review-leg close: there resume rolls the attempt back and runs the bundle again from dev.

## When the ledger won't read

A ledger that does not decode as UTF-8 (`ledger-unreadable`) or that the OS refuses (`ledger-inaccessible`) stops or pauses the run, and the ATTENTION notice names the file.

If the sweep **stopped** (the run is finished and cannot be resumed):

1. Repair the ledger: make it valid UTF-8, or fix permissions or storage.
2. Commit the repaired ledger, and commit or stash anything else in the code repo.
3. Start a fresh `bmad-loop sweep`. Completed cycles keep their work.

If the run **paused** (a story run publishing a close, a bundle close, an intent rebuild):

1. Repair the ledger as the notice says.
2. `bmad-loop resume <run-id>`. Resume retries the write, reusing the recorded session result where one exists and otherwise running that leg again. `resume` and `resolve` refuse a sweep run while its ledger still cannot be read.

A missing ledger is not a fault: the sweep finds nothing open.
