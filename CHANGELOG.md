# Changelog

All notable changes to `bmad-loop` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the project is pre-1.0,
breaking changes may land in a minor release.

## [Unreleased]

Much of this section is the `release/0.9.x` hotfix line brought forward onto `main` (#433).
**Upgrading from 0.9.1, you already have those fixes** — the entries below re-state them for `main`,
whose seams had diverged enough that several ports needed a different fix, and they close holes
0.9.1 left open. `main` has not been released since 0.9.0 and does not descend from v0.9.1.

### Added

- **Deferred review findings are harvested from spec frontmatter (#433).** BMAD-METHOD#2640 moved
  `defer`-triaged findings into the spec's unfiled `deferred:` list. A successful dev, review,
  repair or review-timeout-salvage pass now files each as `### DW-<n>` (`spec-deferrals-harvested`),
  keyed on a fingerprinted `origin:` so a retry or replay never re-files; the spec's list is
  untouched. Malformed items become one `severity: low` entry (`spec-deferrals-malformed`); a
  `spec_file` outside the orchestrator's roots is refused (`spec-deferrals-skipped-out-of-tree`).

- **`validate` warns when an old bmad-loop left shield patterns in `.git/info/exclude` (#384).**
  The writer is fixed, but nothing removes the lines already there — and they hide every new file
  under paths like `.claude/skills` from `git add -A` in that checkout. `git.exclude-legacy-pollution`
  names the file and the exact lines. Candidates cover every version that wrote that shared file:
  all registered profiles rather than the ones this policy resolves today (a project that switched
  CLIs still carries the old one's lines), plugin seeds and glob expansions, and v0.9.1's `_bmad`
  family — `main` does not descend from v0.9.1, so those repos upgrade into this check. Warn only,
  never auto-removed: lines are matched byte-for-byte, and those naming paths the repo **tracks**
  are reported as the shield's (they can only be hiding new files) while the rest are flagged to
  review, since a project can legitimately have written the same line for itself.

- **A park travels with its story's commit, so `bmad-loop confirm` works from any clone (#356).**
  Each parked story writes one JSON record to `.bmad-loop/operator/<key>.json` inside the story's
  own commit window, so it rides the park's commit — worktree merge-back included — into every
  clone. The machine-local `.bmad-loop/operator-actions.json` index is retired: still read and
  pruned, never written. `validate` reports a park with no record as `operator.park-record-missing`
  — a failed write, a pre-upgrade park, or a checkout lacking the park commit's branch (pull it).

- **`bmad-loop confirm` completes a parked story (#335, part 3 of 4).** Once you have carried out
  the external actions a story owed, `bmad-loop confirm <story-key>` walks them one at a time
  (`--yes` skips the prompts), writes the spec's `## Operator Confirmation` audit section, advances
  the spec and board to `done`, and commits the pair. Nothing is re-driven. `--reverify` re-runs the
  project's `[verify]` commands first and blocks on failure; `--list`/`--json` show every parked
  story and what it owes. An interrupted confirmation resumes (`operator.confirm-interrupted`).

- **Stories can park at `awaiting-operator` (#335, part 2 of 4).** A dev session whose story needs
  an action only a human can take outside the repo — publish a DNS record, grant an API key — now
  commits everything an agent can do, records what is owed in the spec's `operator_actions:`
  frontmatter, and parks. The run moves on and the board advances, in place of the old dishonest
  pair: `done`, which hid the work behind a green board, or `blocked`, which halted the run. A park
  clears the gates a `done` story clears and skips review; `[operator] enabled = false` reverts.

- **`awaiting-operator` vocabulary, no writer yet (#335, part 1 of 4).** Names the state at every
  layer: `Phase.AWAITING_OPERATOR` (terminal, reachable only from `COMMITTING`), a sprint-status
  token ordered immediately before `done`, `operator_actions` on each task, and the matching
  run-summary count, `status` output and TUI glyphs. A `state.json` carrying the new phase is
  forward-only — an older binary rejects it. Because the token is now ordered rather than unknown, a
  review session writing it onto a `done` board escalates as a sign-off regression (#334).

- **Defer notifications name where the work survives (#333).** A deferred story's rollback parks the
  attempt on an `attempt-preserve/*` branch (or a `refs/attempt-preserve-dirty/*` snapshot), but the
  ref only ever reached `journal.jsonl`, leaving the operator to hunt with `git log --all`. The
  notice now names the ref and the `git merge --ff-only` that restores it — commits-only when the
  dirty snapshot could not be captured. New `preserve_ref` on each task, projected into `status` and
  `--json` (additive; schema stays 1) from run state, so a ref `scm.preserve_keep` pruned is named.

- **Stories can close deferred-work entries (#234).** A story can declare what it closes —
  `closes_deferred: [DW-5, DW-6]` on its `stories.yaml` entry or in the spec's frontmatter, unioned
  — and on commit each flips to `status: done <date>` plus `resolution: resolved by story <id>`. No
  upstream skill emits the field yet (BMAD-METHOD#2619). Advisory, at the commit boundary: a failed
  or escalated story closes nothing, and bad ids only warn, reported up front by `validate`
  (`deferred.closes-unknown` / `-malformed` / `-entry-unreadable`, `deferred.ledger-unreadable`).

- **Readable run logs for `opencode-http` (#306).** Contributed by
  [@jackmcintyre](https://github.com/jackmcintyre). The HTTP adapter has no tmux pane to replay, so
  a finished opencode run left `logs/<task-id>.log` holding nothing but the server's own INFO
  stdout. Per-session logs now split three ways off the SSE stream the adapter already reads:
  `<task-id>.log`, a curated role-prefixed transcript with `[bmad]` marker lines for tool calls,
  edits, permission asks and errors; `<task-id>.server.out`; and `<task-id>.sse.jsonl`, a raw trace.

- **`review.on_timeout` policy knob (#271).** A timeout-like review verdict (`timeout` / `stalled` /
  `over_budget`) always burned a review cycle until `max_review_cycles` and then deferred — even
  when the dev product was already finalized and verify-green. Beside the default `"retry"`:
  `"salvage-if-done"` commits the verified dev product, refiles the outstanding follow-up
  recommendation to deferred work and journals `review-timeout-salvage`; `"defer"` gives up on the
  first timeout-like verdict. Env-fault (#194) and `crashed` verdicts keep their own routing.

- **Transport failures pause instead of burning attempts (#194).** A session whose CLI lost its API
  connection stayed alive but idle, printing `API Error: Unable to connect …` until the session
  clock ran out — indistinguishable from a timeout, so two outages exhausted `max_dev_attempts` and
  deferred it untouched. Adapters classify it from per-profile `env_fault_patterns`; dev, review and
  fix pause like an `rc 126/127` verify fault; re-arm restores the budget. A blocking plugin
  workflow and the sweep's migration and triage loops escalate instead, before charging a retry.

- **Documented the `BMAD_LOOP_*` environment variables (#246).** The three runtime override vars —
  `BMAD_LOOP_MUX_BACKEND`, `BMAD_LOOP_PROCESS_HOST`, `BMAD_LOOP_SESSION_TIMEOUT_S` — now have a
  reference table in the README, and are read through one `bmad_loop.envvars` registry so the
  supported knobs are discoverable in one place. Behavior is unchanged.

- **`python -m bmad_loop` (#240).** The package is now runnable as a module, mirroring the installed
  `bmad-loop` console script via a thin `__main__.py`. Characterization tests pin the current CLI
  exit codes (typed errors and the broad backstop → 1, argparse usage → 2).

### Changed

- **Every spec-frontmatter status read goes through `status_of` (#358 follow-up).** Five inline
  status reads remained in the engine and the generic adapter, each reading a blank `status:` as the
  token `none` — the defect #358 fixed at the shared reader. Three were neutral; the pair that was
  not is the review-launch snapshot and the mid-session status-transition tick, which compare
  against each other. One behavior change falls out: a session that ERASES a previously-set status
  no longer records a transition — a blank is not an observed live status.

- **`set_frontmatter_status`'s tests now live in `tests/test_frontmatter.py` (#357, part 3).** They
  had stayed in `tests/test_resolve.py` next to `set_frontmatter_field`'s. Tests only — no behavior
  change.

- **The story token budget is checked while the story runs (#336).** `max_tokens_per_story` was read
  once, after the story was marked done, so an overrun surfaced only after every token was spent —
  and a story that deferred or escalated went unchecked. Cumulative weighted spend is now re-checked
  at every session boundary; the first crossing raises an ATTENTION plus a desktop notice naming
  spend and cap, latched per story and persisted so a resume does not re-warn. Still advisory —
  nothing is terminated; `limits.max_tokens_per_session` remains the session-ending cap.

- **A failed snapshot now blocks the rollback reset (#340).** An auto-rollback refused to reset past
  commits it could not park, yet journaled a failed _uncommitted_-work snapshot and reset anyway —
  destroying the tracked edits and run-created untracked files it existed to capture. Both preserve
  steps now refuse alike, pausing with rescue instructions naming the tree. A capture failure over a
  fully committed tree still resets; a resolved re-drive stays pause-free (#338). Inert under
  shipped defaults; it bites `scm.rollback_on_failure = true` and in-worktree retries.

- **A review revoking the sprint sign-off now escalates (#334).** `sprint-status` is advanced to
  `done` at dev time; a review session writing it back contradicts that, and nothing re-advances it
  — the remaining cycles re-read the same failure and the story deferred, work rolled back. The
  review-verify gate now pauses with the two ways out: finish and re-arm, or accept and advance the
  board. Keys on `sprint-status` alone; `status: blocked` stays the hand-back channel. New
  `[review] on_status_contradiction` (default `escalate`; `retry` restores the old behavior).

- **`bmad-loop-setup` stops registering BMAD config; the installer owns it (#258).** The skill wrote
  the pre-v6.10 `_bmad/config.yaml` layout, which BMAD's own resolver never reads. Since v6.10.0
  bmad-loop is an installer-installed module, so the installer already stages `_bmad/bmad-loop/`,
  writes the manifests, rebuilds the help catalog and regenerates the central `config.toml`
  wholesale, discarding outside writes. The PEP 723 scripts are **removed**, closing the
  bare-`python3` bug (#259); setup now writes one help CSV, installs the tool and preflights.

- **Ctrl+C outside a run now exits `130` cleanly (#241).** A `KeyboardInterrupt` escaping `main()`
  outside `engine.run()` (config load, engine construction) now prints a one-line `interrupted` to
  stderr and returns the new `ExitCode.INTERRUPTED` (`130` = 128 + SIGINT). The shell exit code is
  **unchanged** — uncaught, CPython re-raised SIGINT and died at `130` — but previously as a bare
  traceback dumped after any partial `--json` stdout; it is now a clean exit, so a Python caller's
  `subprocess.returncode` shows `130` rather than `-2`. A Ctrl+C _during_ a run is unchanged.

### Removed

- **The `bmad-auto` → `bmad-loop` rename compatibility is gone.** The rename shipped in 0.8.0 and no
  pre-rename installs remain in the wild, so `init` no longer strips `bmad_auto`-marked hooks,
  deletes `bmad-auto-*` skill dirs, carries `.automator/policy.toml` over to `.bmad-loop/`, or
  prints the leftover-`.automator/` note; `bmad-loop-setup` drops its migration section with them.
  A project still on `bmad-auto` should migrate on 0.9.1 — the last release carrying the shims —
  before upgrading past it.

### Fixed

- **An isolated sweep bundle can land when the deferred-work ledger is gitignored (#426).**
  `git worktree add` checks out tracked files only, so a project that gitignores its ledger — the
  default — gave the unit worktree none: `mark_done` returned False, `verify_review_bundle` never
  saw the ids `done`, and the bundle deferred on a fixable retry for ever. Provisioning now seeds it
  when the checkout cannot deliver one, moving the failure onto a leg the carry below can rescue. A
  ledger symlinked to an untracked target is seeded to the wrong path and still hits this (#462).

- **Every ledger write an isolated unit makes now reaches the main checkout (#425, #458).** Of the
  producers writing the deferred-work ledger from inside a unit worktree, a damped review round's
  refiled follow-up and a story's `closes_deferred:` flips died with it: `finalize_commit`'s
  `git add -A` skips the gitignored path in silence. Both now re-file after the merge, journaled
  `review-followup-carried` / `review-followup-carry-uncommitted` and `story-deferred-close-carried`
  / `story-deferred-close-carry-uncommitted`; the follow-up's record is persisted before its append.

- **A sweep bundle running in a worktree no longer loses its ledger closures.** The closure landed
  in the unit worktree, `git add -A` skipped the gitignored path, and the merge brought nothing
  back: the entries stayed `open`, triage re-bundled them, and every later sweep re-drove work that
  had already landed — an unbounded loop, not a one-time drop. The closure is now re-applied to the
  main checkout after the merge (`sweep-bundle-close-carried`), best effort, recording
  `sweep-bundle-close-carry-uncommitted` rather than costing the run its integration.

- **A host lost in the merge-to-carry window replays a carry that filed no findings.** The resume
  pre-pass only replayed units carrying harvested findings, so a unit whose sole ledger payload was
  a bundle's closures, a damped review follow-up or a story's `closes_deferred:` flips was skipped
  and that write stranded. Replay eligibility now names every payload the carry hook delivers, and
  runs before the sweep reads the open set so a replayed closure leaves it before triage re-bundles.
  Resume also replays an accepted state sync and an accepted repair session.

- **A deferred bundle's closure is no longer carried by the resume replay.** The deferral path
  deliberately re-files the harvest and withholds the closure — a defer discarded the code that
  closure claims to have resolved — but the replay routed through the shared hook, which now also
  carries closures, and `open_ids` re-bundles only `open` entries, so a wrongly `done` entry is
  invisible to every later sweep. The replay now mirrors the deferral exactly: harvest only.

- **A sweep bundle's ledger closes are withheld until its attempt is accepted (#433).** The
  orchestrator marked a bundle's ids `done` above the dev artifact gate, so an attempt that failed a
  non-fixable check was discarded with the ledger already claiming its work resolved — and since
  `open_ids` re-bundles only open entries, no later sweep looks at that id again. The close now runs
  below the gate on a PROCEED decision only, and a review-leg defer re-opens the ids it closed
  itself. One consequence: a bundle session that changed no code now fails the gate.

- **A harvested deferral is reverted when its attempt rolls back (#420).** The harvest runs before
  the artifact gate, so a session failing a non-fixable check left a ledger entry describing
  discarded code — unremovable by the reset, since the ledger sits under a `keep`-protected artifact
  folder and git reverts a ledger only when it tracks it. The dev phase now snapshots the ledger
  before every engine-side write in that window, persists it with the attempt, and restores it
  around the rollback.

- **The ledger snapshot degrades loudly (#420).** A scope or tracked probe that cannot answer keeps
  the file (`ledger-scope-probe-failed`, `ledger-tracked-probe-failed`), and reaching the restore
  unarmed records `ledger-snapshot-missing`. The revert is lossless either way: the spec's
  `deferred:` frontmatter is never mutated, so the next attempt re-harvests from it.

- **The harvest's own ledger write is no longer the session's proof of work (#433).** The harvest
  runs above the dev artifact gate, and that gate deliberately does not exclude the ledger — a story
  whose whole scope is ledger reconciliation must register as real work — so a session that
  finalized its spec, changed no code and recorded one `deferred:` finding proceeded on the line the
  engine had just written for it. The gate now excludes the ledger relpath on exactly the attempts
  whose harvest filed into it, keyed on a flag latched and persisted before the first append.

- **A session's own ledger edit on that attempt is still proof of work (#433).** The exclusion is
  path-granular — the whole ledger relpath, not the harvest's lines — so where both wrote, the
  session's edit went out with the orchestrator's and the following non-fixable retry paused the run
  under the default `scm.rollback_on_failure = false`. The gate now stands down whenever the ledger
  moved off a digest persisted beside `baseline_commit`, re-checked after `post_session` hooks
  return — a hook can be the session's last writer.

- **A state file predating that digest falls back to git evidence (#433).** A failed probe there
  keeps the path excluded (`legacy-ledger-attribution-failed`).

- **A worktree that could not be given its required upstream skills now pauses instead of stalling
  (#433).** Provisioning skips a skill tree resolving outside the repo — exactly what a symlink to a
  shared machine-wide BMad install is — while the run-start preflight stats through that symlink and
  passes, so an isolated run was dispatched into a worktree holding none of its skills and every
  session stalled on `Unknown command`. Undelivered rels are journaled `worktree-seed-skipped`, and
  the engine re-probes disk — never the copy bookkeeping — and escalates before dispatch.

- **Only the deterministic skill contract can pause that run (#433).** The gate asks what the
  run-start preflight does: the resolved dev primitive plus the review skills this project's
  `customize.toml` requires. A reviewer owes its `SKILL.md`; the primitive additionally owes
  `step-04-review.md`, `customize.toml` and every renderer source its worktree copy resolves.
  Everything else in the catalogue is copied best-effort and can never pause a run.

- **Skill-tree containment is checked per file — a behavior change (#433).** It used to be checked
  on the skill directory alone, so a child symlinked to a shared install outside the repo was
  followed by `shutil.copy2` and its bytes landed silently, and the configuration worked.
  Provisioning refuses to read through it now, and where the refused file is one of the contract
  files above the run pauses, naming the remedy: commit the file, or point the link inside the repo.

- **Worktree isolation carries the `_bmad/` config surface (#433).** The renderer-era dev primitive
  (BMAD-METHOD#2601) takes the worktree as its project root and hard-fails when that root has no
  `_bmad/` — there is no walk-up — so on a project that gitignores it (most do) every isolated
  session HALTed with nothing written. Provisioning now merge-copies the repo's `_bmad/` per file,
  copy-when-absent; the generated `_bmad/render/` is never seeded and is git-excluded inside the
  worktree through its OWN private exclude (#384), never the shared `.git/info/exclude`.

- **A short `_bmad/` seed pauses the run rather than driving the backlog into HALTs (#433).** The
  pause fires only for the two surfaces whose absence is a proven HALT — `_bmad/config.toml` and the
  renderer's own script unit under `_bmad/scripts` — and only when the resolved dev primitive is a
  content-confirmed renderer stub, so a pre-BMAD-METHOD#2601 inline `SKILL.md` project still
  proceeds. The realistic trigger is a symlinked `_bmad/`; it escalates once with the worktree left
  mounted for inspection, which `worktree-opened` now names, since it fires at mount.

- **A seed entry the worktree never got is reported (#433).** The seed loops dropped an entry with a
  bare `continue` when the containment guard refused it, so a config the repo carries as a symlink
  _out_ of itself delivered nothing and said nothing. Drops are now journaled
  `worktree-seed-dropped`, re-probed against the trees on disk and required to arrive whole; the
  per-CLI hook config — the only default seed for gemini, copilot and antigravity — is asked about
  its source and symlinks rather than answering with the bytes it writes after both loops.

- **Worktree provisioning survives a filesystem it cannot fully read (#422).** It ran with no `try`
  around it, so a single unreadable file, dangling link, symlink cycle or FIFO in the repo's skill
  trees or seed sources ended the whole run with a traceback where a named, resumable escalation
  belonged. Every probe and copy the isolated seed makes is now total, on one shared walk that
  descends symlinked source directories — which the renderer's `rglob` does not — without looping;
  a dangling _destination_ symlink counts as occupied. Only provisioning degrades.

- **A worktree's hook config is never registered through a symlink (#421).** The registration loop
  took `worktree / profile.hooks.config_path` at face value, so a checkout carrying its per-CLI
  settings file as a symlink out of the tree had the merge read the _outside_ file and the write
  land there — mutating the operator's real dotfile while the worktree, left with no Stop hook,
  never reported completion. The loop now refuses the whole profile unless the raw path is inside
  the worktree, no component up to the worktree root is a symlink, and `resolve()` changes nothing.

- **A renderer stub that cannot compose its prompt now fails the preflight (#410).** A stub
  `SKILL.md` (BMAD-METHOD#2601) shells out to `_bmad/scripts/render_skill.py`; when that renderer
  cannot run it writes `HALT: …` and the session Stops with no spec — a fact about the install, so
  every story does the same. Three `problem` findings refuse it: `skills.dev-renderer` for a short
  script unit, `skills.dev-renderer-config` for an absent `_bmad/config.toml` (its only required
  layer), and `skills.dev-renderer-sources` for a missing `workflow.md` or snapshot target.

- **`init` gitignores the renderer's output, and `validate` says when it is already committed
  (#409).** `_bmad/render/` is regenerated with checkout-absolute paths, so a committed tree grows a
  directory per checkout path and per renderer bump. `init` now writes `_bmad/render/` into
  `.gitignore` idempotently; since that cannot help a path already tracked, `validate` warns
  `git.render-tracked`, naming the one-time `git rm -r --cached _bmad/render`.

- **Refuse `isolation = "worktree"` combined with a `repo_root` override (#414).** The pair produced
  a green preflight and then an isolated session with no dev primitive, no result, and nothing
  journaled naming the cause. `validate` now reports it; `run`, `sweep`, `resume` and the child
  sweep refuse to start; the dry-run banner names it first; the TUI toasts it ahead of its
  clean-tree gate. Plumbing `project` through provisioning so both work together is #443.

- **A configured path carrying `[`, `]`, `*` or `?` no longer makes git act on the wrong files
  (#423).** `implementation_artifacts` reaches git verbatim out of `_bmad/bmm/config.yaml`, and git
  reads a positional operand as a _pathspec_ — so such a name always matched wider than it named:
  `commit_paths` staged an unrelated sibling under a story's name, `has_changes_since` and
  `attempt_dirty` reported a changed attempt as CLEAN, and `safe_rollback`'s preserve restore handed
  back a change the reset had just discarded. Every operand is now literal.

- **A noisy git config no longer makes a pristine tree read as dirty.** `worktree_clean` compared
  git's stdout and stderr merged, so `status` exiting 0 while warning about an unexecutable
  `core.fsmonitor` hook or an unknown `core.fsyncMethod` was indistinguishable from a porcelain
  record — and callers that refuse the command outright meant such a host could never start a run.
  Only stdout is read now; the error path still reports both.

- **An undecodable `policy.toml` or `_bmad/bmm/config.yaml` is reported, not a crash.**
  `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so a config saved in UTF-16 or latin-1
  escaped every `except (PolicyError, OSError)` handler — the ones whose whole job is to degrade to
  defaults. It hit `_configure_mux`, which runs before argument dispatch on **every** command, and
  the TUI's constructor. Both loaders now convert it to their own typed errors, so `validate` names
  the file under its `policy` / `bmad-config` finding and the TUI degrades to defaults.

- **`diagnose` pseudonymizes the spec name, and gives one spec one alias.** A journal record's
  `spec` field carries the customer's feature name, and a bare basename is identifier-shaped, so the
  scrub fallback shipped it verbatim. `spec` now has an alias namespace of its own and the value is
  reduced to its basename first — the producers disagree on shape, so without it one spec drew two
  aliases and an absolute home path landed in the local `--legend` file. Either separator splits.

- **The upstream `bmad-dev-auto` → `bmad-build-auto` rename no longer breaks a project (#393).** The
  dev primitive is resolved on disk — `bmad-build-auto` preferred, a marker-complete `bmad-dev-auto`
  accepted — so `validate`/`run`/`sweep`/`resume` pass on either era with no `policy.toml` edit.
  `skills.base-shim` refuses the forwarding shim as marker-incomplete: it is a valid slash command,
  and where a legacy `_bmad/custom/bmad-dev-auto*.toml` sits beside it, it HALTs an unattended
  session on its interactive migration gate. `[dev] skill` stays `bmad-dev-auto`, the discriminator.

- **Every session prompt spells the resolved primitive, per skill tree (#393).** A run mixing
  `.claude/skills` and `.agents/skills` at different eras gets the right name per role, resolved
  against the **workspace** so a run resumed into an existing worktree spells the era that worktree
  carries. Mid-upgrade, the orphaned-override warning says to **copy** the legacy
  `_bmad/custom/bmad-dev-auto.toml`, never rename it; `--dry-run` says on stderr when its preview is
  not runnable; and the skills preflight stopped gating a triage-only CLI's skill tree.

- **The git-add shield no longer hides new files in your own checkout (#384).** It appended the tool
  files it writes (skill trees, hook config, seeded configs) to `.git/info/exclude` — shared with
  the main checkout and every sibling worktree, permanent, unversioned — and projects legitimately
  track those paths, so every **new** file under them silently stopped being staged by `git add -A`.
  It now writes a private exclude in the worktree's gitdir, activated by a worktree-scoped
  `core.excludesFile` that dies with `git worktree remove`, copying yours in byte for byte.

- **The shield now proves it applies, or skips with a reason (#384).** Storing the worktree-scoped
  key proved only that the value was stored, and a `!` line below a copied pattern cancelled it —
  either way the tool files stayed stageable with nothing reported. It now asks git which excludes
  file actually resolves, re-appends past any inherited negation, and honors an **explicitly empty**
  `core.excludesFile` as "no excludes file at all". An unreadable file, an unresolvable git home, a
  peer-accessible `core.sharedRepository` or an unanswerable probe skips it — journaled, notified.

- **Caveats for the worktree shield (#384).** It enables `extensions.worktreeConfig`, a
  **permanent** repo-format flag; it is rolled back wherever it could be left set without a working
  shield, surviving only where a sibling worktree depends on it or the rollback itself failed — the
  reason says which. It needs **git 2.20**: below that the shield is skipped and no repo-format
  change is made, since git that old refuses a repo carrying it. Concurrent runs serialize on a
  `.git` lock; lines an older bmad-loop wrote into `.git/info/exclude` stay — delete them by hand.

- **The git-add shield's git calls run on the shared chokepoint (#389).** They were the last bare
  `git` spawns outside `verify._run_git`, so they missed its `LC_ALL=C` pin and used a hardcoded
  120s timeout instead of the configured `[limits] git_timeout_s`. Both now apply.

- **A non-UTF-8 filename no longer crashes the run past every git guard (#377).** `verify._run_git`
  decoded git's output strictly, and `UnicodeDecodeError` — a `ValueError` raised before any return
  code exists — matched neither its timeout nor its spawn-`OSError` arm, so it escaped untyped past
  every `except GitError`: the `-z` merge pre-flight, `worktree list --porcelain` (crashing the
  stale-run reconcile at every run and sweep start), and `git diff`, whose failure bypassed the
  valve that preserves a failed unit's work. Now a `GitError` like any other git failure.

- **`scm.worktree_seed` entries are validated at policy load (#456, #384).** An empty entry made
  worktree provisioning resolve its copy source to the repo **root** and its destination to the
  worktree, both passing the loop's containment checks — so it copied the whole project, gitignored
  and untracked files included, and since a worktree mounts under `.bmad-loop/runs/`, the copy
  recursed into itself until the path length failed, silently. The `/` it then emitted is inert, so
  none of that surplus was shielded from the unit's `git add -A`. Entries must be project-relative,
  the rule adapter profiles and plugin manifests already apply to their own seed lists;
  `worktree_seed` was the only unchecked source.

- **One undecodable byte of verify output no longer crashes the run (#378).** Verify commands are
  arbitrary operator tools and their captured output was decoded strictly, so a child emitting bytes
  invalid in the run's encoding raised `UnicodeDecodeError` out of `run_verify_commands`, losing
  every command's result instead of classifying the failure. Output now decodes with
  `errors="replace"`; the tail is display-only, while exit codes still drive classification.

- **A short write no longer truncates the worktree exclude (#375).** The update is a
  read-modify-rewrite and `write_text` truncates before writing, so an ENOSPC or EIO partway through
  cut the operator's own excludes mid-content while the degrade reason still reported nothing
  written. The surviving tail parses as valid patterns, so nothing reported the damage: cut shield
  lines simply stop shielding, and a cut landing on a path boundary widens a surviving pattern over
  a whole subtree. Now written to a scratch file and moved into place — fully updated or untouched.

- **The exclude's git query no longer crashes on a non-UTF-8 repo path (#374).** POSIX filenames are
  bytes and `git rev-parse --git-common-dir` was decoded strictly, so a repo path carrying bytes
  invalid in the locale encoding raised a `UnicodeDecodeError` that neither arm of a
  documented-best-effort helper caught. Git's output is now captured as bytes and decoded with the
  filesystem codec inside the guarded tail.

- **The worktree's local git exclude is best-effort now (#359).** `_worktree_local_exclude` guarded
  only its `git rev-parse`; the filesystem tail crashed the run on a symlink loop, a read-only
  `.git`, or a non-UTF-8 exclude file or seed path — the payload is now read, deduped and written as
  bytes, so the codec faults are gone. It degrades to a journaled `worktree-exclude-degraded`
  reason; without the exclude the unit's `git add -A` would commit the provisioned skill trees and
  tool configs into the merge. Git unqueryable at all stays a silent, expected skip.

- **A spec left with a blank `status:` is rescued again (#369).** `devcontract.synthesize_result`
  carried a second #358 stringification: a blank-but-present `status:` read as the truthy token
  `"none"`, so the prose `## Auto Run Result` fallback never fired and synthesis read inconsistent —
  and since `status_consistent` gates the post-kill rescue (#61), a session that lost only its final
  Stop was discarded as `stalled`/`timeout`. Both now use `frontmatter.status_of`; blank frontmatter
  with prose `blocked`/`awaiting-operator` gets a truthful label too, routing unchanged.

- **A blank frontmatter `status:` reads as blank, not as the token `none` (#358).** YAML parses a
  bare `status:` line as null and `status_of` stringified it, so every gate saw `"none"`, a token
  nothing in the project writes: `devcontract.RECONCILABLE_FROM` read it as a deliberate custom
  status and left a half-finalized generic spec untouched. A YAML-null status now reads `""`, the
  same as a missing key, while a literal `status: none` stays the string. `confirm` renders
  `status: (blank)`; the stories-mode board reads `present`.

- **A trailing inline comment on a spec's `status:` line survives the write (#357, part 2).**
  `set_frontmatter_status` and `set_frontmatter_field` kept everything through the colon and dropped
  the rest, so `status: draft  # set by hand` came back as `status: done`. The comment and its
  separating whitespace now carry through whenever a conservative token pattern can certify where
  the scalar ends; anything it cannot read as a bare token falls back to the full drop. The value's
  own quotes are still dropped, deliberately.

- **Spec writers no longer relay a spec's line endings (#357, part 1).** `set_frontmatter_status`,
  `set_frontmatter_field`, `reset_spec_status` and `strip_auto_run_result` read specs through
  `read_text`, whose universal-newline translation handed each writer an all-LF copy of a CRLF spec
  — so a write meant to move one value rewrote every line ending in the file. All four now read
  bytes, and a replaced line carries its own terminator, so each line keeps the ending it had. A
  CR-only spec is now a clean no-op through the two strippers.

- **A spec status the writer cannot rewrite is no longer a silent no-op.** `set_frontmatter_status`
  found the line with `lstrip().startswith("status:")` while every reader parses the block as YAML,
  so a quoted key, a space before the colon or a flow mapping wrote nothing and returned `False` —
  which no caller read — while a block scalar, a continued value or a nested `status:` was rewritten
  into corruption. The edit is now re-parsed with `yaml.safe_load` as an oracle and kept only if the
  block still parses as a mapping, its `status` is the target, and every other key is unchanged.

- **A status no line edit can safely move now raises instead of reporting success.** It raises
  `FrontmatterWriteError`, and `False` now means "nothing to change" — so
  `set_frontmatter_status(spec, "done")` over an already-`done` status returns `False` without
  rewriting. `runs.rearm_escalation` translates it to `RearmError` and aborts before persisting, so
  a re-drive is not routed to the wrong step. `verify.set_frontmatter_field`, which also appended a
  duplicate key on a scan miss, shares the edit with `devcontract.reset_spec_status`.

- **A symlink-loop fault at park no longer loses the run's park (#335).** `_park_spec_relpath`
  guarded its `resolve()` with `(OSError, ValueError)` and the park-index writer — since superseded
  by #356's record — with `except OSError`, but below Python 3.13 `Path.resolve` reports a symlink
  loop as `RuntimeError`, neither. That writer ran outside every `try` in `_finalize_commit_phase`,
  so an escape skipped the park notification, the `post_commit` hook and the state save. Both guards
  now hold the type, and the writer degrades to its `operator-index-failed` journal line.

- **An unwritable `ATTENTION` file can no longer crash a run.** `gates.notify` promised "never
  raises", but only its desktop half was guarded — the `ATTENTION` append was bare IO, so an
  unwritable run dir turned an advisory notice into a run crash at every site that journals a
  decision and then announces it (defer, escalate, plugin veto, manual-recovery pause, budget
  warnings). The file sink now degrades like the desktop one; the journal entry each caller writes
  first remains the durable record.

- **`limits.max_tokens_per_story` is validated like its per-session sibling.** It was parsed with a
  bare `int()`, so `true` silently became a 1-token story cap and `0` was accepted, both against the
  documented `int >= 1`. Non-integers and values below 1 now raise `PolicyError` at load. Note this
  rejects a `policy.toml` that previously loaded; there is no `0 = off` semantic — set the cap high
  instead, it only warns.

- **A pause inside a defer's rollback no longer loses the defer record (#342).** `_defer` advanced
  the task to terminal DEFERRED before recovering the tree, so a rollback that paused instead
  (rollback OFF — the default — or a preserve/snapshot failure) unwound past the tail forever: no
  `story-deferred` journal entry, no defer notification, an under-counted diagnose `defer_count`.
  The record is now emitted before the pause re-raises, and its notice points at the ACTION REQUIRED
  manual-recovery notice instead of describing a rollback that never ran.

- **Spawn-level `OSError` is translated at the git chokepoint (#343).** `_run_git` translated only a
  timeout, so an EMFILE/ENOMEM/ENOENT out of `subprocess.run` bypassed every OSError-blind
  `except GitError` guard and crashed the run — under exactly the resource pressure those guards
  exist for. Spawn faults now raise `GitSpawnError` (a `GitError`) with the errno on `__cause__`: a
  fault opening a unit worktree pauses instead of marching the queue into DEFERRED, and one during
  merge-target reconciliation keeps the unit branch and escalates.

- **`safe_rollback` no longer swallows a failed `git stash create`.** The empty snapshot silently
  disabled the whole `preserve` restore, so the hard reset reverted exactly the paths the caller
  asked to keep — a resolved re-drive's corrected spec — with no error anywhere. It now raises
  before the reset, and only when a restore was actually requested, so the no-`preserve` callers
  degrade as before.

- **A spawn-level `OSError` during the attempt snapshot no longer crashes the run.** An EMFILE or
  ENOMEM from `subprocess.run` escaped untyped out of the middle of a rollback. Preservation is
  observation rather than a repair write, so it now degrades into the same journal-and-decide path a
  `GitError` takes, keeping the errno as the breadcrumb; `safe_reset` still raises.

- **A git fault while counting the attempt's commits no longer crashes the rollback (#343).**
  `preserve_attempt_commits` enumerated the range above baseline with no guard at all, so an
  ordinary git timeout or a spawn `OSError` took the run down mid-rollback. An un-determinable range
  now reads as "there may be work above baseline" and refuses the reset instead of taking the
  clean-tree early return, journalling `attempt-preserve-enumerate-failed` apart from
  `attempt-preserve-failed`; the dirty check degrades, a failed restore-patch apply escalates.

- **The orchestrator's ledger writers no longer inject lines from a multiline value (#305).** Found
  by [@Haven2026](https://github.com/Haven2026) in #274. The deferred-work ledger is line-oriented,
  but `deferredwork`'s mutators interpolated their arguments verbatim: a note could mint a phantom
  entry, truncate an entry's span and re-surface its tail as a phantom legacy item, or leave one
  entry carrying two `status:` lines. Line breaks in free text now collapse to a space — sanitized
  rather than rejected, because a formatting defect must not cost a triage attempt.

- **Ledger writers validate the orchestrator-owned fields (#305).** `mark_done`, `mark_done_many`,
  `append_decision` and `append_entry` raise `ValueError` on a non-ISO `date`, and `append_entry` on
  a `status` outside `open`/`done <date>` or a `severity` outside `critical|high|medium|low`. The
  inner dev session and the sweep's migration session still write ledger markdown directly, and a
  `DW-<n>` token surviving a sanitized break counts toward `next_seq`. The sweep skill now states
  the single-line expectation, picked up on `bmad-loop init --force-skills`.

- **A deferred finding appended after the last ledger entry is no longer lost (#304).** Found and
  first fixed by [@Haven2026](https://github.com/Haven2026) in #274. The inner dev session appends
  each defer as a flat `- source_spec:` block, which the last canonical `### DW-<n>` entry's span
  absorbed while `parse_legacy()` masks canonical spans before scanning — so it was invisible to the
  sweep, to `--dry-run` and to the TUI's legacy view. Spans now end at a flat block, and
  `append_entry` writes the documented `location:` field it had been omitting.

- **`return_attached_client` no longer claims a return that never happened (#227).** It answered
  `True` unconditionally, discarding `switch_client`'s result, so a failed switch plus a failed `-l`
  fallback still journaled the return and sent the sweep unattended. `RETURN_OPTION` is now cleared
  only on a real return, a failed one being left for the parked window's trailer, and the failures
  report apart: `ATTENDED` (a human is still there) versus `UNREACHABLE` (no client at all), which
  journals `sweep-return-no-client`.

- **Both mux client verbs now owe effect, not dispatch (#317).** `TerminalMultiplexer.detach_client`
  widens from `None` to `bool`. tmux reads the effect off the exit code; psmux cannot — every arm of
  its `detach-client`/`switch-client` exits 0 whether or not a client moved — so it measures the
  session's attached-client count across the call and answers on the drop, degrading to `False`
  rather than a vacuous `True` when that is unobservable. Out-of-tree backends still returning
  `None` read as "nothing detached" — degraded, not broken.

- **Give psmux a working per-window option channel (#310).** psmux keeps one user-option scope per
  server and answers `''` to any `-w` read of an `@`-prefixed name, so the ctl-window project tag
  bled across rows — letting a prune in one project `kill-window` another's — and the parked-return
  option always read empty. Both now use a session-scoped key carrying the window id
  (`@bmad_project_@3`), routed with `-t <session>` and freed on `kill_window`; the `switch-client`
  leg stays inert on builds predating psmux/psmux#483 — still inert at 3.3.7.

- **Session-qualify the psmux TUI-side window ids (#291).** #254 covered the engine seam but left
  the launcher's surfaces bare, and that process usually runs outside any pane — where a bare `@N`
  resolves through psmux's most-recent-session fallback, not the session that minted it, so the
  ctl-window prune could `kill-window` another server's identically-numbered window with rc 0.
  `new_parked_window`, the `window_id` columns of `list_windows` and `current_window_id` now emit
  `session:@N`, and `select_window` resolves the id to an index first.

- **Verify environment faults are classified per shell, so Windows stops burning dev attempts
  (#302).** `ENV_FAULT_RCS = {126, 127}` is `sh`'s convention; `cmd` has no equivalent — a missing
  tool exits `1`, like the "tests failed" that should route to repair — so the arm never fired on
  win32, leaving #130's charged-attempt regression live there. It now classifies on rc 9009, `cmd`'s
  `is not recognized` / `access is denied`, and a `shutil.which` probe that also catches a file
  outside `PATHEXT` exiting `0` unrun — a pass verify never earned.

- **psmux window ids are now session-qualified (#254).** psmux mints window ids per server, so the
  bare `@N` returned from `new_window` routed by the caller's `$TMUX` when replayed as a `-t` target
  — from a `bmad-loop-ctl` pane, the ctl server rather than the agent's. The log sink then bound to
  the engine's own window (empty run logs), nudges went to the engine's pane, and teardown killed it
  instead of the agent's. Both verbs now emit `session:@N`, degrading to the bare id when the
  session name contains `:` (#221). tmux ids are server-global and untouched.

- **A session's read-back could adopt another story's spec (#261).** The generic dev/review
  read-back picked its artifact by mtime from the shared implementation-artifacts dir — which under
  worktree isolation also covers the main checkout's copy, and under `isolation="none"` _is_ that
  copy. A foreign spec landing there after launch won, so a review that produced nothing scored
  `completed:done` and a dev leg's `followup_review_recommended: false` skipped review. Unlike the
  rest of this family (#127/#160/#224) it failed toward _landing_ unverified work.

- **The read-back is pinned to the spec the orchestrator named (#261).** Wherever the dispatched
  prompt names the path — every review leg, dev repair and patch-restore —
  `SessionSpec.expected_spec` pins the read-back and the directory scan is never reached; the #224
  missing-marker fallback rides the same seam, and a sweep bundle's differently-named spec (#161)
  needs no exemption. A bare-story-key re-drive and a dev attempt 1 keep the scan. The skill's
  no-spec fallback marker is no longer recorded as a story's spec.

- **A dead session with no evidence it ran can no longer be upgraded to `completed` (#261).** A
  read-back artifact is refused unless a turn ended — a `Stop` event specifically — or the pane log
  grew past a small floor; the two are ORed because each has a blind spot, and the log is created
  empty at launch so a window dying before the tee attaches reports "rendered nothing". Scoped to
  the shared-directory read-back; a task-scoped `result.json` stays authoritative. Applies to the
  crash path and `_post_kill_reconcile`, journalling `readback-refused-no-proof-of-work`.

- **`validate` requires the review skills your `bmad-dev-auto` really invokes (#260).** The
  preflight held every project to a catalog naming `bmad-review-verification-gap`, which no tagged
  BMAD-METHOD release ships, misdiagnosing it as a missing bmm module even where bmm was installed —
  so `validate`/`run`/`resume`/`sweep` all failed on a stock install. The reviewers now come from
  the installed skill: its `customize.toml` `[[workflow.review_layers]]`, else what
  `step-04-review.md` names inline, overrides merged as BMAD's resolver does (`code` before `id`).

- **New `skills.*` findings for a review-layer config (#260).** A configured layer naming a skill
  the tree lacks is a `skills.review-layer-missing` problem instead of passing and then failing on
  every dev run; disabling every layer is `skills.review-layers-empty`; an unreadable override is a
  `skills.customize-unreadable` warning; a layer gated by a run-time `when` is
  `skills.review-layer-unresolved`. `validate` and the run preflight branch on severity, so an
  advisory can no longer abort a run; a `workflow` key of the wrong TOML shape no longer raises.

- **Isolated worktrees get the review skills that were validated (#260).** `provision_worktree`
  copies the skills the project's own layers name, not just the fixed base catalog, and seeds
  `_bmad/custom/` — whose `*.user.toml` layer the upstream installer gitignores — so the preflight
  and the worktree run no longer resolve different layer sets.

- **`notify.desktop` works on macOS/Windows, and warns when it can't (#231).** The channel was
  `notify-send`-only, so on macOS and Windows `notify.desktop` (default `true`) was inert and every
  "a human is needed" path reached no one. It now dispatches natively (`osascript`, a best-effort
  WinRT PowerShell toast, `notify-send`), passing untrusted text through the environment or argv,
  never a command string. With none available, `validate` warns `notify.desktop-unavailable` and the
  run start prints one, journalling `notify-desktop-unavailable`.

- **Scrollable modal dialogs (#275).** The decision, escalation, confirm, sweep-options and
  story-checkpoint TUI dialogs now scroll their bodies and dock their action buttons, so the buttons
  stay reachable with long content down to the dialog's minimum frame height. Safety warnings that
  gate an enabled Resume/Re-arm dock with them, and the Resume confirm re-checks engine liveness at
  click time.

- **A finished story whose session omitted `## Auto Run Result` no longer livelocks and DEFER-drops
  (#224).** The review HALT intermittently finalizes the spec to `status: done` without the terminal
  marker the harvest scan keys on, so every Stop read `no-artifact`, stall nudges re-invoked an
  already-exited workflow (#149), and after `max_review_cycles` the finished, verify-passing work
  was rolled back.

- **The harvest scan synthesizes a result from terminal frontmatter (#224).** It fires once the
  spec's (path, mtime, status) fingerprint holds stable across two resultless Stops, and the
  post-kill reconcile applies it on a single sighting once the window is provably dead. Synthesized
  results carry `synthesized_from_frontmatter` and journal `session-synthesized-from-frontmatter`;
  the verdicts `terminal-frontmatter-pending` and `ambiguous-frontmatter` record the fingerprint's
  progress.

- **Deterministic missing-marker catch and repair (#276).** The #224 fallback was heuristic: a
  review killed after an mtime bump but before its `in-review` flip could score `done` without
  running. The engine now snapshots the spec at review launch and refuses a candidate still hashing
  equal to it (`unmodified-since-launch`, crumb `frontmatter-unmodified-refused`); a mid-session
  status transition (`spec-status-transition-observed`) proves the session ran and outranks that
  gate, collapsing the two-Stop fingerprint to one sighting.

- **A synthesized result repairs the spec (#276).** `devcontract.append_auto_run_result` writes back
  the marker the skill owed, refused outside the orchestrator-owned roots or when the fresh
  frontmatter disagrees (`spec-marker-repaired`, `spec-marker-repair-failed`,
  `spec-marker-repair-skipped`); it and the #160 strip rewrite the spec atomically. The launch
  `status:` is **never** mutated: it is load-bearing routing input to the upstream skill.

- **`limits.dev_contract_nudge` (default `true`) asks the skill to repair its own omission (#276).**
  On the first `terminal-frontmatter-pending` Stop the dev adapter sends one nudge, once per session
  (`contract-nudge-sent`); set it `false` to rely on harness-side synthesis alone.

## [0.9.1] — 2026-08-02

Compatibility hotfix for the BMad Method's `bmad-dev-auto` → `bmad-build-auto` rename
(BMAD-METHOD#2651, first shipped in bmad-method 6.10.1-next.33) and the two upstream changes
that rode the same window. Both skill eras are supported; the rename itself needs no
`policy.toml` edit.

### Fixed

- **The dev primitive is resolved on disk, so the upstream rename no longer breaks a project
  (#405).** `bmad-build-auto` is preferred and a marker-complete `bmad-dev-auto` accepted, so
  `validate`/`run`/`sweep`/`resume` pass on either era and report which one resolved. A new
  `skills.base-shim` check refuses the forwarding shim, which HALTs an unattended session when a
  legacy `_bmad/custom/bmad-dev-auto*.toml` sits beside it. `[dev] skill` stays `bmad-dev-auto`.

- **Every session prompt spells the resolved primitive (#405).** Dev, review, repair, restore,
  stories dispatch and all three sweep bundle legs hardcoded `/bmad-dev-auto`, which post-rename
  dispatches the shim. The name is now resolved per skill tree, so a run mixing `.claude/skills`
  and `.agents/skills` gets the right one per role, and the fallback marker matches both prefixes.

- **`--dry-run` says when its preview is not runnable (#405).** Dry runs returned before their own
  skill preflight, so a broken install still got a plausible-looking schedule. Preflight failures
  now print to stderr under a "NOT runnable as-is" banner; exit code stays 0 and stdout is
  untouched, since rc 0 means "the preview rendered", not "ready to run".

- **The skills preflight no longer gates a triage-only CLI's skill tree (#405).** Every `skills.*`
  check asks a dev-primitive question, yet the tree list came from all three adapter roles — so a
  `[adapter.triage]` of `gemini` demanded the whole BMad Method module in `.agents/skills`. It is
  now scoped to dev and review; single-CLI and dev/review-split projects are unaffected.

- **A worktree that could not be given its upstream skills now pauses instead of stalling (#405).**
  The containment guard skips a skill tree symlinked to a shared machine-wide install while the
  run-start preflight stats through that symlink and passes, so every isolated session stalled on
  `Unknown command`. Undelivered skills are journaled `worktree-seed-skipped` and the engine
  escalates before dispatch.
  The seed also merges per _file_, and checks each file for containment — a behaviour change.

- **A renderer stub that cannot compose a prompt fails preflight (#405).** Three new checks block
  `validate`/`run`/`sweep`/`resume` when the resolved `SKILL.md` is the renderer stub
  (BMAD-METHOD#2601): `skills.dev-renderer` for an incomplete script unit,
  `skills.dev-renderer-config` for an absent `_bmad/config.toml` (the renderer's only required
  config layer), and `skills.dev-renderer-sources` for a missing `workflow.md` or an unloadable
  `[[bmad-snapshot:…]]` token.

- **A stale customization override is reported, not obeyed (#405).** `skills.customize-legacy` warns
  when an override still sits at `_bmad/custom/bmad-dev-auto[.user].toml`, where it no longer
  applies — the session runs, unstyled.

- **Worktree isolation carries the `_bmad/` config surface (#405).** The renderer-era primitive
  takes the worktree as its project root and hard-fails when that root has no `_bmad/` — there is
  no walk-up — so on a project that gitignores it (most do) every isolated session HALTed with
  nothing written. Provisioning now merge-copies `_bmad/` per file, and a short seed pauses the run
  when the resolved primitive is a renderer stub, naming what is missing.

- **The renderer's `_bmad/render/` output stays out of story commits (#405, #409).** Outside a git
  repo the check stays quiet rather than fabricating an ok (#409). It is never
  seeded into a worktree and is git-excluded inside one, and `init` now gitignores it — the only
  protection under the default `isolation = "none"`. Neither shield helps a path already tracked,
  so a new `git.render-tracked` warning names the one-time `git rm -r --cached _bmad/render`.

- **Worktree provisioning survives a filesystem it cannot fully read (#405).** The `_bmad/` seed
  and its detector both used `rglob`, which does not descend a symlinked _sub_-directory, so a
  symlinked `_bmad/scripts/lib` was under-seeded and reported complete; both now share the skill
  seed's walk. Per-file faults in the walk are skips the gate names, not an exception out of
  provisioning; the eager `worktree_seed`, plugin and hook-config copies still raise.

- **A seed entry the worktree never got is now reported (#405).** The seed loops dropped an entry
  with a bare `continue` when the containment guard refused it, so a config the repo carries as a
  symlink _out_ of itself delivered nothing and said nothing. Drops are journaled as
  `worktree-seed-dropped`, and `worktree-opened` now fires at mount rather than after the gates.

- **Seeding never writes _through_ a symlink (#405).** Both seed loops probed and copied against
  the _resolved_ destination, and `Path.resolve()` is non-strict — so a dangling link reported its
  slot free and the copy landed at the link's target, a path `git add -A` would stage into the
  story branch. `git worktree add` produces exactly that state for a tracked symlink whose target
  is untracked, so it arrives on a project's _first_ story.

- **A hook config is no longer its own alibi (#405).** The drop report probed the destination for
  existence, but the per-CLI hook step writes `profile.hooks.config_path` after both seed loops, so
  a dropped config was answered for by the hook's own bytes — and for gemini, copilot and
  antigravity that path is the profile's only default seed. Those rels are now asked whether the
  source escapes the repo, or the destination is a symlink or escapes the worktree.

- **`isolation = "worktree"` is refused when `repo_root` is overridden (#405).** A project whose
  `_bmad/bmm/config.yaml` sets `repo_root` — the documented monorepo knob — decouples the git root
  from the project dir. Provisioning seeds every surface off `repo_root` while `init`, `validate`
  and the run preflight write and probe them under `project`, so the preflight approved a surface
  the isolated run never received and every story ended as a result-less Stop.

- **The `repo_root` refusal covers every entry point (#405).** `validate` reports
  `policy.isolation-repo-root`, and `run`, `sweep`, `resume`, `resolve` and the child sweep all
  refuse — drop the override, or set `isolation = "none"`. The child raises rather than declining
  quietly, so a mid-run flip is journaled `sweep-auto-failed`. Plumbing `project` through
  provisioning stays open as #414 and #443.

- **`worktree_clean` and `path_tracked` no longer read their answer out of git's stderr (#405).**
  `ls-files` and `status` exit 0 while still writing to stderr, and that chatter was
  indistinguishable from an index entry or a porcelain line — so `git.render-tracked` named an
  uncommitted path and a clean checkout read dirty. Other callers still merge, which is #442.

- **An undecodable `policy.toml` or `config.yaml` is reported, not a traceback (#405).**
  `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it escaped every
  `except (PolicyError, OSError)` handler — the TUI could not open and the CLI died at startup on a
  raw codec message naming no file. Both loaders now raise typed errors; `validate` names the file.

- **Deferred review findings are harvested out of the spec's frontmatter (#405).** BMAD-METHOD#2640
  moved `defer`-triaged findings from `deferred-work.md` into the spec's own `deferred:` list,
  silently starving the sweep pipeline. A successful dev, repair or review session now files each
  into the ledger with its `location` and `severity` (`spec-deferrals-harvested`), deduped on a
  fingerprint of its summary and location; the frontmatter is never rewritten, so a revert is
  lossless. The review prompt no longer files the finding itself, which double-filed it.

- **A transient harvest read cannot silently drop recorded findings (#405).** An unreadable,
  missing, undecodable or temporarily malformed spec at the harvest step now follows the existing
  bounded retry paths instead of letting a later verifier accept and commit without the ledger
  repair. A persistent fault defers with the artifacts preserved, or re-escalates a CRITICAL.

- **Post-session ledger work keeps its proof attribution across crash replay (#405).** The
  completed-session checkpoint is refreshed after hooks return, so a retained retry cannot hide a
  hook-authored ledger repair behind an earlier harvest's engine-written exclusion. A pre-feature
  checkpoint with no ledger digest reconstructs attribution from its Git baseline, so a
  ledger-only session still resumes while the new harvest stays excluded from proof.

- **A harvested deferral is reverted when its attempt rolls back (#405).** The harvest runs above
  the artifact gate, so a session that finalized its spec and then failed a non-fixable check left
  an entry describing discarded code the reset cannot remove — the ledger sits under a
  `keep`-shielded artifacts folder. The dev phase now snapshots it and restores around the rollback,
  persisting the snapshot with the attempt and never unlinking a ledger git tracks.

- **That restore spans the whole retry chain (#405).** On the `rollback_on_failure = off` default it
  reverts every attempt's ledger writes, not just the last — matching the
  `git reset --hard <baseline_commit>` the pause notice tells the operator to run. Reaching the
  restore unarmed still records `ledger-snapshot-missing`.

- **The pre-harvest snapshot is spent by a stop-and-wait pause, and scoped to the retry chain
  (#405).** Under the default `rollback_on_failure = off` the operator may append to the ledger
  before typing `resume`, but the armed snapshot survived that wait and the resume wrote stale
  bytes over their edit; the pause leg now disarms as it restores. A fixable retry neither re-arms
  nor spends it, so the revert reaches as far as the reset does. Not closed: `_finish_inflight`'s
  restart arm resets to the same baseline with no ledger restore at all.

- **A harvest is reverted even when the ledger lives outside the repo (#405).**
  `implementation_artifacts` may be configured out of tree, and the restore classified any such
  ledger as git's — backwards, since `reset --hard` cannot reach outside the workspace, so every
  revert was a no-op and the entry outlived the code it described. It now reads as "not git's".

- **The harvest's own ledger write is no longer the session's proof of work, and the session's own
  edit still is (#405).** The dev artifact gate deliberately does not exclude the ledger — a story
  whose whole scope is ledger reconciliation has to count as real work — so it could not tell the
  orchestrator's write from the session's, and a session that changed no code but recorded one
  finding proceeded to done on the engine's line. The gate now excludes the harvest's own attempts,
  and stands down again when the ledger had already moved off the dev phase's baseline.

- **Both of those answers span a fixable retry (#405).** A fixable retry keeps the harvest's ledger
  line with its tree by design, so the repair session met a stale baseline: an attempt that merely
  reverted the offending change passed on the orchestrator's own write, and a stalled attempt
  deferred with an empty record of the harvest's intent. Both records now survive the continuation,
  and the ledger baseline re-bases: forward onto the kept tree, back onto the restored snapshot on a
  rollback. (A gitignored ledger is invisible to the gate either way.)

- **A sweep bundle's ledger closes are withheld until its attempt is accepted (#405).** The
  orchestrator marked a bundle's ids `done` above the artifact gate, so a discarded attempt left the
  ledger claiming its work resolved, and `open_ids` re-bundles only open entries. The close now runs
  below the gate, for a PROCEED decision only. One deliberate consequence: a bundle session that
  changed no code now fails the gate rather than passing on the orchestrator's own bookkeeping.

- **A review-leg defer takes those closes back (#405).** A defer after acceptance — every route, the
  stock `trigger = "recommended"` included — re-opens the ids it closed itself, since `_defer`
  writes its post-close snapshot over the reset that reverted them.

- **An isolated unit's ledger writes reach the main checkout — findings and bundle closes alike
  (#405).** A defer drops the unit worktree unmerged, and a landing unit's ledger edit rides the
  branch only when the ledger is tracked: `finalize_commit` stages with `git add -A`, which skips a
  gitignored path in silence. Both legs now re-file what the harvest intended and re-apply what the
  close **intended**, the DONE leg **after** the merge so entries dedup; `mark_done` is idempotent
  and a DEFER carries findings only. An _unseeded_ gitignored ledger still defers rather than lands.

- **A carry lost to a crash mid-landing replays on resume (#405).** `Phase.DONE` is persisted
  before the merge, the merge removes the worktree, and only then does the carry fire — so a host
  death there skipped it and resume never looked again. Resume now re-runs an unlatched carry once,
  behind a persisted latch and a durable `unit-merged` record; the merge writes its intent before
  invoking git, so an interrupted merge is re-run rather than assumed to have landed.

- **Tracked ledger carry failures stop and remain replayable (#405).** A rejected `git add`,
  `status` or `commit` used to be journaled and swallowed, letting an isolated unit finish with a
  dirty or staged main ledger. A committable carry now persists its commit intent and raises, and
  resume retries it. Gitignored and external ledgers keep their advisory degrade behavior.

- **An artifacts dir whose name holds `[` or `]` no longer misdirects those two paths (#405,
  #423).** Git reads a positional operand as a pathspec, so such a path was a glob that also
  matched its neighbours: the harvest revert's tracked-probe answered off a neighbour and skipped
  its unlink, and the carry's commit swept a neighbour's edit in. Both now name the path literally.

- **Three small contracts brought back into line (#405).** A ledger entry filed with no file:line
  carries `location: n/a`, the value `deferred-work-format.md` has always specified — entries
  already on disk without the line stay valid, so read an absent `location:` as `n/a`. A plugin
  workflow's completion marker resolves on its own role's skill tree, not always the dev one. And
  `diagnose` pseudonymizes the `spec` journal field, which was shipping the feature name verbatim.

## [0.9.0] — 2026-07-21

### Added

- **Active agent indicator (#153).** The TUI run header and tasks table now name who is driving —
  the resolved adapter·model for the live stage, or the run's configured adapters when no session is
  open. Session-start journal entries and session records stamp the resolved adapter identity, and
  `bmad-loop status --json` gains an additive run-level `adapters` (the snapshot-resolved
  dev/review/triage identity, `null` on a run predating stamping) and per-story `adapters_used` (the
  identity actually recorded per role) — both are a projection of the run's persisted policy snapshot
  (re-stamped from current config on resume) and its recorded sessions, not live policy read at status
  time; a run whose snapshot predates adapter stamping reports `adapters: null` rather than a
  fabricated default. Schema version unchanged.

- **Graceful stop (`stop --graceful`, TUI `S`).** Ask a live run to finish its in-flight item —
  a story or sweep bundle through commit, or an in-progress sweep triage — then finalize cleanly
  and stop as a resumable `stopped` run, instead of the hard SIGTERM stop that kills mid-item.
  Delivered through a `stop-request.json` control file consumed at the next item boundary, so it
  needs no signal and works on every platform and multiplexer backend. Pending auto-sweeps are
  suppressed; `--cancel-graceful` withdraws a request, and a hard stop still wins over a pending one.
  `status --json` gains an additive `graceful_stop_pending` field (schema version unchanged).

- **`bmad-loop validate --json` (#205).** A stable, schema-versioned JSON document of the
  preflight: the `ok` verdict, the queue `mode`/`spec_folder`, per-severity `counts`, and every
  check as a flat emission-ordered finding. Each finding carries a **stable `check` id** —
  `hooks.registered`, `adapter.binary`, `skills.base-incomplete`, … — so CI can branch on a
  particular failing gate instead of matching remediation prose, which is the part most likely
  to be reworded. `detail` keeps what each check knew before it flattened itself into a
  sentence: `mux.backends-detected` keeps every detected backend row rather than the text's
  `tmux*, psmux (unavailable)` soup, whose trailing `*` a consumer had to parse to learn which
  backend was selected, and `skills.base-incomplete` keeps `missing_markers` as a list rather
  than a `", "`-joined string. **A failing check emits the whole document and still exits 1** —
  the nonzero code is the verdict being reported, not a failure to produce one, so the existing
  `bmad-loop validate || exit 1` in CI is untouched. `machine.py` gains the clause that makes
  this well-defined: parse non-empty stdout whatever the exit code, and read the verdict from
  the document's own `ok`, which unlike `rc` separates "the checks failed" from "the command
  broke". The text output is byte-for-byte unchanged.

- **`bmad-loop clean --json` / `bmad-loop cleanup --json` (#204).** Stable, schema-versioned
  JSON documents (one per command — they are separate contracts) reporting what a reclaim
  removed, or under `--dry-run` would remove: for `clean` the worktree paths, trimmed,
  archived, deleted and protected run ids, the effective retention policy, and `freed_bytes`
  as a raw integer — the text's `~1.2MB` is a rendering of that number, and formatting is the
  renderer's job; for `cleanup` the run ids whose sessions went, the live ids left alone, and
  the ctl windows closed. Plan and outcome share one schema — same fields, same meanings, with
  `dry_run` saying which one you are holding — so a script can pre-flight and then compare
  against what actually happened. The values are each invocation's own sample rather than a
  promise the two agree: `freed_bytes` is re-measured, and the world can move between the
  preview and the commit. **The real paths are now scriptable at all** — they previously discarded
  the per-item data and printed only a summary line, and `protected` was a bare count. Both
  commands printed progress as they mutated, and both warned mid-loop about an unverifiable
  engine pid; under `--json` that warning becomes a document field, so stderr stays empty and
  stdout stays one pure document.

- **`bmad-loop decisions --json` (#203).** A stable, schema-versioned JSON document of the
  pending deferred-work decisions, so a script can select an option by policy and pre-answer
  it rather than scraping the numbered text. It is strictly richer than that text, which drops
  each decision's `context` and shows only key/label/effect per option — hiding the `intent`,
  `resolution` and `bundle_name` that decide what the next sweep actually builds or writes.
  The recommendation, a `(recommended)` suffix on a free-text line in the text form, becomes a
  derived boolean on the option it names. `--json` implies the listing and never prompts (the
  interactive prompter reads stdin and cannot coexist with a pure document), and nothing
  pending is a valid empty document with exit 0.

- **`bmad-loop list --json` (#192).** A stable, schema-versioned JSON document — one entry
  per run, oldest first (short ref, run id, type, started-at, liveness-aware status, paused
  stage) — replaces the text table when passed. Unparseable runs are included as status
  `unknown`, and an empty runs dir yields a valid empty document with exit 0.

- **`bmad-loop status --json` (#190).** A stable, schema-versioned JSON document (run
  id/type/source, derived status + pause fields, snapshot `cache_read_weight`, raw +
  weighted token totals, and per-story phase/attempt/review-cycle/tokens/commit/defer
  reason) replaces the text output when passed. This is the supported machine-readable
  surface — the human text, whose layout #129 already changed once with no warning
  path, is now explicitly best-effort.

- **`session-end` journal entries carry `tokens_weighted` beside `tokens` (#129).** Only the
  raw scalar was persisted, and the weight cannot be backed out of it, so a session's
  cost-weighted spend was unreconstructible after the fact — the weighted figure existed
  only for sessions that tripped the budget guard. Every entry whose usage was read now
  records both. `null` (never `0`) when the usage read failed, since untracked is not free;
  both fields stay absent on an `aborted` end, where no read happened. Distinct from a
  tripped session's `budget_weighted`, which is the guard's mid-session sample at trip time
  rather than the end-of-session total. The `cache_read_weight` knob also gained a
  description in the TUI settings screen, where it had none.

- **Mid-session token-budget guard (#158).** Both adapter wait loops now sample cumulative
  weighted usage every ~30s and act on crossing the new per-session cap
  (`limits.max_tokens_per_session`, default 4M weighted) per `limits.session_budget_mode`:
  `warn` = one ATTENTION + lifecycle breadcrumb; `enforce` = wrap-up nudge +
  `limits.session_budget_grace_s` (default 240s) to finish, then termination with the new
  `over_budget` session status, which rides the ordinary retry→defer routing. Defaults to
  `warn`: on upgrade, existing installs gain visibility (one ATTENTION line per over-cap
  session) but no terminations — set `session_budget_mode = "enforce"` to opt into the
  hard bound, or `"off"` to silence the guard entirely. Session-end
  journal entries carry `budget_weighted`/`budget`/`budget_mode` for tripped sessions.
  Live-verified on `claude`; other transcript-reading profiles sample best-effort, and
  adapters with no mid-session usage signal (`usage_parser = "none"`, Copilot) stay inert.

- **OpenCode adapter (`opencode-http` profile, alias `opencode`).** Drives
  [OpenCode](https://opencode.ai) ≥ 1.18 entirely over HTTP/SSE — one headless
  `opencode serve` per session (no tmux window), SSE `session.idle` as the completion
  signal with an HTTP poll fallback, per-session server password, hermetic skills, and
  token usage read back over the API. Full dev/review synthesis parity via the new
  `_ResultFileMixin`/`_DevSynthesisMixin` seams in `generic.py`; profiles gained a
  hookless `[hooks] dialect = "none"` mode (no hook registration anywhere). Install the
  HTTP client with `pip install 'bmad-loop[opencode]'`; set `model` as `provider/model`.
  The pinned 1.18.2 API contract is recorded in the adapter's module docstring, guarded
  by a zero-token real-binary smoke test (`tests/test_opencode_live.py`, skipped when
  the binary is absent).

- **Native-Windows `psmux` multiplexer backend (experimental).** A bundled builtin that
  drives runs on native Windows through psmux — a ConPTY tmux re-implementation that speaks
  the tmux CLI via its own `psmux` binary — so tmux's session/window model and the
  `bmad-loop-<run-id>`/`bmad-loop-ctl` session names carry over unchanged. It registers for
  `win32` and is the platform default there, selected automatically when the `psmux` and
  `pwsh` binaries are on `PATH` and psmux reports newer than 3.3.6 (older releases can
  force-kill a recycled PID during teardown, so they read as unavailable and selection falls
  through). Native Windows stays experimental — window hosting, attach/detach mapping, and
  Unity cache-path correctness are tracked in the roadmap — but the dev→review→verify→commit
  loop and TUI observation run. WSL is unaffected (it _is_ Linux and uses tmux). (#58)

- **Out-of-tree multiplexer backends (`bmad_loop.mux_backends` entry points).** A backend
  package installed next to bmad-loop (e.g. `uv tool install bmad-loop --with <adapter>`) now
  registers itself with no config step: before every selection, core imports each module
  advertised under the `bmad_loop.mux_backends` entry-point group, whose import-time
  `register_multiplexer(...)` call makes the backend selectable exactly like a bundled one
  (builtins load first, so default selection is unchanged by installing an adapter). A package
  that fails to import can never break selection — the failure is recorded and surfaced as a
  `warning:` line by `bmad-loop mux` and a note in the `validate` preflight
  (`external_backend_errors()`).

- **Unity modal-dialog guards (`[plugins.unity]`).** A chronically-dirty Unity scene raises modal
  Editor dialogs ("scene changed on disk", "save changes before closing") that freeze the MCP
  dispatch loop and stall the whole run. The bundled Unity plugin now defends in depth: it seeds an
  editor-only `SceneAutoSaveGuard` into the project (`install_scene_guard`, default on), quiesces
  the Editor around a failed-attempt rollback so `git reset --hard` can't leave a stale scene open
  (`quiesce_on_rollback`, default on), and appends the scene-save discipline (from a shipped
  `unity_facts.md`) to every dev/review prompt so the agent saves at the boundaries that would
  otherwise trip a modal. As a last-resort observability net, an opt-in **detect-only** probe
  (`dialog_probe`, default off) watches — via xdotool, X11/Linux only — for those dialogs and
  _reports_ any it sees (a JSONL record, an `ATTENTION` line, and a best-effort `notify-send`); it
  never clicks or keys anything, no-ops where there is no X display, and self-reaps when the engine
  exits (`dialog_probe_interval_sec`, `dialog_probe_notify`).

- **Follow-up-review damping (`limits.max_followup_reviews`, default 1).** Bounds how many extra
  review rounds a story is granted _solely_ because a completed round finalized `status: done` yet
  still set `followup_review_recommended: true`. Once spent, the next such round force-converges —
  verify, then re-file the lingering recommendation to the deferred-work ledger, then commit —
  instead of burning cycles up to `max_review_cycles`. This damps the structurally non-convergent
  case where every review pass patches findings and therefore recommends another pass. The damped
  converge is the expected steady state and stays quiet (no ATTENTION); only the re-review cap (a
  story that itself originated from a `review-budget-followup` entry and still won't converge) still
  notifies. Verify-repair rounds, non-terminal rounds, and `PAUSE`/`DEFER`/`RETRY` never spend the
  grant; `0` never honors a pass's own recommendation. `runs.rearm_escalation` resets the counter so
  a human-resolved re-drive gets a fresh budget.

- **Resizable dashboard panes.** Every pane boundary is now adjustable by mouse-drag (divider
  bars, which also carry the Sprint / Deferred Work headings) or a keyboard resize mode (`ctrl+w`,
  then `←`/`→` for the sidebar, `↑`/`↓` for the active horizontal split, `Tab` to pick it, `Esc` to
  exit). Sizes persist per-project to a new `[tui]` section in `policy.toml` and re-apply on the
  next launch; untouched projects keep the previous fixed proportions.

- **Dev-choosable multiplexer backend selection (#87).** `get_multiplexer()` now resolves by
  precedence — `BMAD_LOOP_MUX_BACKEND` env var → the new machine-scoped `[mux] backend` key in
  policy.toml → the platform default (win32: `psmux`, elsewhere: `tmux`) when installed → the first
  registered backend that matches the platform _and_ is `available()` — so two same-platform
  backends (psmux / tmux-windows) no longer collide by registration order. Forced names are trusted
  (no availability gate) and fail loudly when unregistered, naming the policy file. New
  `bmad-loop mux` lists registered backends (platform / available / version / selected + why);
  `bmad-loop mux set <name>` persists the choice (`--clear` reverts to auto, `--force` allows a
  name that only registers on the target machine); no interactive prompts anywhere. `validate`'s
  preflight lists all detected backends when more than one is registered and notes an env/policy
  forced selection. A tmux-less POSIX host still selects `TmuxMultiplexer` and reports it
  unavailable, exactly as before.

- **Seam-canonical window targets.** The `=session[:window]` target grammar is now owned by the
  `TerminalMultiplexer` seam instead of living as hand-assembled tmux syntax in core: a new
  concrete `target(session, window=None)` encoder (overridable per backend, tmux inherits the
  default and passes it straight through) and a module-level `parse_target()` decoder that
  native-id backends reuse instead of re-deriving the grammar (the out-of-tree herdr
  adapter's `_parse_target` delegates to it). `runs.py`/`tui/launch.py`/`tui/app.py` format every
  target via `target()` (new `runs.session_target` / `launch.ctl_target` helpers) — output is
  byte-identical, so no backend or operator behavior changes; the contract is documented in the
  adapter authoring guide's new "Window targets" section.
- **Herdr multiplexer backend — shipped out-of-tree.** A complete non-tmux-family
  `TerminalMultiplexer` backend for [herdr](https://herdr.dev)'s cross-platform
  workspace/tab/pane model was developed in-tree (engine run path #136, TUI-launch surface
  #137) and extracted before ever shipping in a release to
  [`bmad-loop-adapter-herdr`](https://github.com/pbean/bmad-loop-adapter-herdr), where it
  co-installs with bmad-loop and registers through the `bmad_loop.mux_backends` entry-point
  discovery above. Core bundles only tmux; herdr's capabilities, remaining degradations, and
  operator notes live in the adapter repo's docs.
- **Stories mode — a second planning pipeline that drives the loop off a typed `stories.yaml` (folder+id dispatch) instead of `sprint-status.yaml`.** Opt in with `[stories] source = "stories"` + `spec_folder`, or per run with `bmad-loop run --spec <folder>` (overrides policy); `--story` then filters by story id. Each entry dispatches by folder + id — the dev skill creates-or-resumes the story spec at `<folder>/stories/<id>-<slug>.md` and the orchestrator reads that id-keyed path back deterministically (no shared board to line-edit, no result-artifact mtime-scan). Strictly linear schedule (list order, no `depends_on`); `bmad-loop run --dry-run --spec <folder>` and `bmad-loop status` print the board (id · live disk state · checkpoint markers · title). Sprint mode is unchanged and remains the default. Requires a `bmad-dev-auto` new enough for folder+id dispatch — the run preflight checks and remediates.
- **Per-story human checkpoints (stories mode).** Independent `spec_checkpoint` (pause before code to review the plan — dev halts at `ready-for-dev`; approve to implement, or request a replan that resets the spec to `draft`) and `done_checkpoint` (pause after the story commits, skipped when it is the last story); both additive to `gates.mode`. A blocked story escalates + resolves as in sprint mode, with a pre-planning-halt sentinel auto-deleted (a copy preserved under the run dir) on re-arm.
- **TUI human-in-the-loop surface for stories mode.** The sprint tree is replaced by a stories board (id · live disk state · spec/done checkpoint markers · title) when a stories-mode run is selected; paused runs carry a per-run pause-kind badge and the run list shows a global _⚑ N need attention_ count; `p` opens the stage-appropriate viewer — plan-checkpoint spec review (Approve & resume / Request replan), story-checkpoint summary card (Continue / Stop), escalation with story context (Resolve / Re-arm & resume), and a gate spec viewer that the existing spec-approval/epic pauses reuse. The start-run modal gains a source select + spec-folder field with a live schedule preview. Every TUI action calls the same code paths as the CLI.
- **Intent-gap patch-restore recovery.** When review halts on an `intent gap`, `bmad-dev-auto`
  now saves the attempted change as a patch before reverting (BMAD-METHOD#2564). If the attempted
  reading was correct, `bmad-loop resolve` re-arms the spec to `in-review` and re-applies the patch
  onto baseline after every reset, so the re-driven session resumes review on the restored diff
  instead of re-implementing. New `--restore-patch <path>` flag for the `--no-interactive` path; a
  patch that fails to apply escalates instead of running on a half-restored tree (a resolve session
  that committed over the patched lines triggers exactly this — re-resolve without a restore).
  Restore is rejected up front for worktree-isolation runs and for stories-mode pre-planning
  sentinels, and the latched patch file itself never counts as proof-of-work. Deferred-work
  `sweep` bundles get the same recovery — an escalated bundle re-arms to `in-review` and the
  re-driven bundle session resumes review on the re-applied patch (#75).
- **Preflight covers the inline review layers.** `bmad-loop validate` (and run-start) now require the
  three upstream review-hunter skills `bmad-dev-auto`'s step-04 invokes — `bmad-review-adversarial-general`,
  `bmad-review-edge-case-hunter`, and `bmad-review-verification-gap` (new in BMAD-METHOD#2550) — plus a
  `customize.toml` in `bmad-dev-auto` (its review-layer config, BMAD-METHOD#2535/#2550). A pre-July bmm
  install missing any is reported with remediation before a run stalls.

### Changed

- **The TUI's validate modal (`v`) renders `validate --json` instead of the text output (#210).**
  One row per check — glyph, stable `check` id, message — with the verdict taken from the
  document's `ok` rather than the exit code, which cannot tell "the checks failed" from "the
  command broke". A check's `detail` is now reachable at all: inline for warnings and problems,
  and `d` toggles it on for everything, so `mux.backends-detected` expands to a row per backend
  instead of the text's `tmux*, psmux (unavailable)`. A failure adds a footer noting the gates
  are chained — the later gates emit nothing after one fails, so a short list is not a short
  list of problems. An unrenderable document (a newer schema, unparseable stdout) re-runs
  validate in text mode and shows the old modal unchanged.

- **`validate` reports a failed external mux backend as a warning, not a note (#210).** The
  `mux.external-backend` finding has always read as a failure — "external mux backend 'x'
  failed to load: …" — while carrying severity `ok`, so it counted as a passing check; it is
  now `warning`, matching what `bmad-loop mux` has always printed for the same condition. It
  stays below `problem` deliberately: selection degrades past a broken external, so the verdict
  and exit code are unchanged. On an affected host `validate --json`'s `counts` shift by one
  (`warning` +1, `ok` −1) while `ok` and rc do not; the schema version is deliberately
  unchanged, since the document contracts each `check` id, not a given check's outcome. The
  text line gains the doubled `ok:   warning:` prefix that `render()` preserves by design.

- **BREAKING: `probe-adapter` now runs `diagnose`'s egress leak self-check, and captured hook
  payloads ship as a schema instead of scrubbed values (#199).** The rendered report re-scans
  itself before emitting (the guard moved to `sanitize.guard`, one audited implementation for
  both commands): an email / secret / home path / username in the final bytes makes the command
  refuse to emit — message on stderr, empty stdout, exit ≠ 0, no `--out` file — and a stray
  occurrence of the pseudonymized project directory name is repaired to its alias and disclosed.
  Each captured event now reports dotted key paths with leaf types (`tool_input.command:str`),
  never payload values, so the `--json` document's `schema_version` bumps to 2
  (`captured_events[].payload` removed, `payload_schema` added; `payload_keys` stays, now
  identifier-gated). Collection hardening rides along: transcript-location components that embed
  the username are redacted, the project dir name is aliased in locations, a home-rooted
  `--binary` hint renders `~`-relative, and a credential-shaped dict key can no longer surface
  in the token key paths.
- **BREAKING: `diagnose --json` and `probe-adapter --json` now emit a pure JSON document
  (#195).** Both used to print their human-readable report with a fenced ` ```json ` block
  appended, so a consumer had to scrape the fence out of prose. `--json` now emits the
  document _instead of_ the report — stdout parses whole, and every human-facing line (`ok:`
  trailers, the leak-backstop warning, the `unknown profile` notice) moves to stderr. With
  `--out FILE` the document goes to the file, stdout stays empty and the confirmation goes to
  stderr; **no file written in JSON mode carries markdown fences any more**. That file is held to
  the same standard as the stream — it is validated and newline-terminated identically, so
  `--json --out FILE` and `--json > FILE` produce byte-identical files. The text mode (no
  `--json`) is unchanged. `diagnostics.SCHEMA_VERSION` deliberately stays at 1 — it versions the
  document, and only the packaging changed — while the probe document gains a `schema_version`
  of 1 alongside its existing `version` key, which still holds the _probed CLI's_ `--version`
  output. Scripts that split on ` ```json ` must switch to parsing stdout directly; the break is
  in the flag's output shape, not in either payload. Two
  incidental fixes ride along: `diagnose --json` no longer renders the markdown report it was
  about to discard, which was double-counting every leak-backstop repair in the warning, and
  the probe document is now `sort_keys`-stable so two probes of the same CLI diff cleanly.
- **Machine-output (`--json`) contract codified in `machine.py`.** The pure-document conventions
  from #190 — one JSON object on stdout, inline `schema_version`, additive-only evolution,
  errors → stderr with empty stdout — now live in one module with shared `emit`/`add_json_flag`
  helpers; `status --json` uses them (output byte-identical) and the duplicated token-total math
  folded into `run_token_totals`. All four `--json` commands share the contract (#195); `--json`
  adoption on more commands is tracked in #196.
- **Backend-neutral naming for the seam-backed helpers and operator messages.** The multiplexer
  seam has non-tmux backends now, so the helpers that wrap it drop their legacy tmux names —
  `launch.tmux_available` → `mux_available`, `app._tmux_missing` → `_mux_missing`,
  `runs.tmux_sessions` → `mux_sessions` (internal, no deprecation aliases) — and the operator-facing
  strings stop naming tmux when they mean the selected backend: launch errors say
  `multiplexer new-session/new-window failed` and `multiplexer backend unavailable (binary not on
PATH)`, the TUI notifies `multiplexer backend unavailable — launch/attach disabled` and
  `launched (control session bmad-loop-ctl)`, and the "attach to … bmad-loop-ctl" hints say
  _control session_. The TUI-guide troubleshooting table matches. Behavior is unchanged.
- **Docs: multiplexer backend guide (`docs/multiplexer-backends.md`).** The user-facing docs no
  longer claim tmux is the only multiplexer backend. The new page covers backend selection
  (`bmad-loop mux` / `mux set`) and how external backends are installed and discovered;
  backend-specific operator guidance (what changes from your seat on herdr, its degradations)
  moved out with the extraction and lives in each adapter repo's docs. README, setup guide,
  TUI guide, and FEATURES name the mechanism and link the page.
- **Docs: `followup_review_recommended` is now scored upstream.** BMAD-METHOD#2580 replaced the
  skill's convergence-prone significance judgment with a severity-weighted score over patched
  findings and added a fourth default review layer (Intent Alignment Auditor, #2560). README,
  FEATURES, TUI guide, the `[review].enabled` setting description, and the engine's damping
  comments now describe the scored flag; `limits.max_followup_reviews` is unchanged and remains
  the orchestrator-side bound.
- **`bmad-loop init` now gitignores `.bmad-loop/policy.toml`.** Policy is per-machine-per-repo —
  it carries the machine-specific `[mux] backend` choice (and the TUI settings editor rewrites
  it), so it must not travel to teammates on other machines or OSes. A `.gitignore` entry does not
  untrack an already-committed file: existing repos run `git rm --cached .bmad-loop/policy.toml`
  once (the local copy is kept; `init` prints this hint when it detects a tracked policy).
  bmad-loop's own worktree-clean preflight already exempted policy.toml — this additionally stops
  inner dev sessions and plain `git status` from reading a policy edit as a dirty tree.
- **The patch-restore seam is now one validator, one path normalizer, and one exclusion site.**
  `runs.validate_restore_latch` holds every latch precondition (sentinel wedge, spec-less escalation,
  worktree isolation) — the worktree check lived only in the CLI, so `rearm_escalation` called
  programmatically could latch a patch the re-drive can never honor; it now rejects it too.
  `verify.resolve_restore_path` replaces four copies of the maybe-relative→absolute join, and the
  shared verify gate derives the restore-patch proof-of-work exclusion from the task instead of
  threading it in from three call sites. The resolve context's `restore_supported` signal is now the
  validator's verdict too, so the agent never negotiates a restore for a sentinel-wedged or spec-less
  escalation either. Otherwise behavior-neutral. (closes #91)
- **Test helper fidelity.** `make_engine` seeds the launching scope (`max_stories`, `story_filter`,
  `epic_filter`) on `RunState` like `cmd_run` does, so resume tests no longer silently ran uncapped;
  the three `_escalated_run` fixtures collapse into one parameterized conftest builder. (closes #84)

### Fixed

- **Locale-stable rollback (#236).** Git subprocesses now run with `LC_ALL=C`, so `safe_rollback`'s
  benign "pathspec did not match" no-op is no longer misread as a hard failure under a localized git
  (e.g. `LANG=it_IT.UTF-8`) — which had turned a resolvable re-drive into a rollback pause. Forced at
  the single `_run_git` spawn point, so every git message the orchestrator inspects stays English.

- **The parked-window return target is now backend-composed (#221).** An interactive attach
  recorded the client's origin as a bare pane id (`%N`) and replayed it as `switch-client -t %N`
  from inside the control session — sound under tmux's one-server model, but on psmux (one
  server per session, upstream-final per psmux/psmux#483) a bare id is session-local: at best
  unresolvable, at worst colliding with a real control-session pane and landing the client on
  the wrong one with exit 0, past the `switch-client -l` fallback. No single form resolves on
  every backend (tmux's window resolver rejects a pane id in the `session:%N` slot, and a
  native-id backend needs its own id passed through untouched), so the recording seam now asks
  the backend: `TerminalMultiplexer.current_return_target()` defaults to the bare native pane
  id — tmux and native-id backends behave exactly as before — and psmux overrides it to emit
  `=session:%N`, which releases carrying the psmux/psmux#483 fix resolve cross-server,
  degrading to the bare id only if the session probe fails. The replay sides treat the value
  as an opaque target and are unchanged.

- **A `worktree_seed` entry that silently copies nothing is now journaled (#230).** Under
  worktree isolation `provision_worktree` copies a seed only when the destination is absent —
  right for a file the checkout legitimately carries, but a _directory_ entry is skipped whole
  the moment any child is tracked, so `worktree_seed = ["_bmad"]` with a tracked `_bmad/custom`
  copies nothing at all, including the absent children that would clobber nothing. Provisioning
  is quiet by contract (it runs under the TUI), so the skip was invisible: user-authored config
  that reads as applied was a no-op. It now returns the skipped entries and the engine records a
  `worktree-seed-skipped` journal event; glob-expanded matches are excluded, since a plugin glob
  is expected to hit paths the checkout already carries. Behavior is otherwise unchanged —
  nothing new is copied.

- **A dry run the TUI cannot spawn opens a modal instead of taking the app down (#210).** The
  `run --dry-run` / `sweep --dry-run` workers called `run_captured` unguarded, and
  `@work(thread=True)` defaults to `exit_on_error=True` — so an `OSError` from the spawn itself
  (a venv deleted out from under `sys.executable`, `EAGAIN` off a loaded process table) escaped
  the worker and killed the whole app rather than the one modal. Both workers and the validate
  degrade now share a guard that reports the reason in the modal body.

- **`--json` output survives a console that cannot encode it (#200).** A JSON document is not
  necessarily ASCII: `diagnostics.render_json` serializes with `ensure_ascii=False` so its leak
  guard can scan values unescaped (#195, below), which lets a _non_-sensitive non-ASCII field —
  a localized `platform.release()`, say — reach stdout verbatim. Printing it to a console whose
  encoding could not carry it raised `UnicodeEncodeError`, in practice a legacy non-UTF-8
  Windows one. It failed safe rather than silently — the encode runs before any write, so
  stdout stayed empty instead of half-written — but `diagnose --json` still died on a machine
  where `--out FILE` would have worked. `machine.emit_document` now switches stdout to UTF-8
  before writing. Re-serializing the document as escaped ASCII would have been the smaller
  change and the wrong one: the leak check verified the unescaped bytes, and emitting anything
  re-derived from them is what that helper exists to prevent. `--out FILE` was never affected;
  it has always written `encoding="utf-8"`.

- **Leak self-check now matches JSON-escaped values (#195).** Two evasions became reachable
  the moment `diagnose --json` stopped also rendering the markdown report, since that raw-text
  pass was what had been catching them: `json.dumps` doubles backslashes, so a Windows home
  path (`C:\Users\…`) serialized to a form `_ABS_HOME_RE` did not match, and its default
  `ensure_ascii=True` escaped non-ASCII sensitive values to `\uXXXX`, hiding them from the
  pseudonymizer's stray-original check while `json.loads` handed the consumer back the
  original. The home-path rule now matches either separator form, and `diagnostics.render_json`
  serializes with `ensure_ascii=False` so the guard sees values as themselves. Both apply to
  the markdown path too; neither changes what a clean dump contains.

- **Resumed runs display the policy they actually enforce (#189).** `policy_snapshot` was
  stamped only at run creation. `resume` reloads `policy.toml` and enforces it — the
  per-story budget, every `SessionSpec` — but left the launch-time snapshot in place, and
  every display reads the snapshot: the run summary, `bmad-loop status`, the TUI, and the
  `policy` block of the `diagnose` bundle, which claimed to describe the run that was
  executed. Edit `limits.cache_read_weight` between launch and resume and the run enforced
  at the new weight while every surface reported the old one, silently up to 10x apart at
  the legal extremes (0.0–1.0). Resume now re-stamps the whole snapshot and persists it
  before the engine starts, restoring the documented contract that policy edits apply to
  resumes. A single `session-end` entry could likewise carry `tokens_weighted` at the
  snapshot weight beside `budget_weighted` at the live one; the two now agree by
  construction. Run scope and mode (`source`, `spec_folder`, `epic_filter`, …) stay pinned
  at launch as before — a policy edit still cannot redirect a live run.
  **Visible output change:** a run resumed across a weight edit re-weights its _whole_
  history, not just post-resume sessions, since totals are recomputed from raw counts (this
  is what the budget always did). A pre-0.8.2 run with no snapshot at all gets one on its
  first resume, so it stops displaying at the hardcoded 0.1 default. `run-resume` journal
  entries now carry `cache_read_weight`, `policy_changed`, and `cache_read_weight_was` when
  it moved, keeping per-session totals written under the old weight reconstructible.

- **Run summaries and `bmad-loop status` report weighted tokens, with both units labeled
  (#129).** The run-finished summary — stdout, the `ATTENTION` file, and the desktop
  notification all render from one place — reported the **raw** total, counting cache reads
  at full price, while every budget judges the **cost-weighted** total. On a cache-heavy run
  that overstates spend by ~6.5x, and neither figure said which unit it was. Both surfaces
  now lead with weighted and name both: `<weighted> weighted tokens (<raw> raw incl. cache
reads)`, matching the TUI, which has shown weighted since 0.7.12. `bmad-loop status` also
  gained a run-level `tokens:` line (it previously printed no run total at all).
  **Visible output change:** per-story `status` cells go from `<raw>t` to
  `<weighted>t (<raw> raw)`, so the number is both differently scaled and differently
  shaped — scripts scraping that column need updating. A story with only cache reads under
  `cache_read_weight = 0` correctly renders `0`, not `-` (which means no tokens at all).
  Displayed weights come from the run's persisted policy snapshot, so every observer
  reproduces the same number from `state.json` alone.

- **The TUI guide's task-table reference described the pre-0.7.12 columns.** It documented
  `tokens` as the raw total and omitted the `raw` column entirely; the run-header and
  journal sections were likewise silent on the weighted/raw split. Docs only.

- **`diagnose` leak self-check is now recoverable (#186).** A stray pseudonymized
  identifier (a per-field routing gap) is repaired by substituting its alias and disclosed
  in the report and on stderr, instead of refusing to emit any dump; residual failures name
  `sensitive[<ns>:<alias>]` instead of an opaque index, and the local `--legend` file is
  written even on refusal so the operator can decode it. PII/secret/path/username hits
  still fail closed.

- **Deferred-work bundles that adopt an existing story spec pass the baseline gate (#161).** A
  "follow-up review of story X" bundle is routed by `bmad-dev-auto` into that story's done
  spec, whose `baseline_revision` is the story's original dev baseline — necessarily older
  than the bundle's worktree cut, so the exact-match gate failed every such bundle after the
  session had already done its work. The bundle gate now accepts a claimed baseline that is
  an _ancestor_ of the orchestrator-recorded one (the session diffed a superset of the
  unit's changes); diverged or unknown baselines still fail, any git fault in the probe
  reads as not-an-ancestor, and sprint/stories modes keep the exact-match requirement.

- **A failed attempt inside a unit worktree auto-recovers instead of pausing with in-place
  instructions (#161).** The mid-drive dev retry was the only recovery path without an
  isolation guard: with `rollback_on_failure = false` it paused the run with manual-recovery
  instructions aimed at the operator's checkout — whose HEAD _is_ the baseline under
  worktree isolation, while the commits sat on the unit branch, so following them literally
  did nothing and invited a destructive reset of a tree the attempt never touched. A mounted
  unit worktree is disposable: the attempt's commits are parked on `attempt-preserve/` refs
  and the worktree resets regardless of the flag, which gates in-place (`isolation = "none"`)
  recovery only. The remaining reachable pauses name their tree (`git -C "<root>" …`).

- **A failed worktree teardown no longer crashes the run after the merge landed (#139).** When a
  process the just-ended session left running (e.g. pytest recreating `.pytest_cache`) makes
  `git worktree remove` fail with ENOTEMPTY, git still drops its admin entry, so the `force=True`
  retry failed with "is not a working tree" and that second `GitError` crashed the run. The
  teardown tail of `close_unit_workspace` never raises now: a failed worktree removal falls back to
  `rmtree` + `worktree prune` (the rmtree confined to the run's own worktrees dir — the path can
  arrive from persisted state), a failed branch delete is reported and swallowed, and both journal a
  `worktree-teardown-degraded` event — teardown is post-merge housekeeping. A failed forensic diff
  capture instead preserves the worktree + branch (they hold the only copy of a dropped unit's
  changes). `discard_worktree` gains the same removal fallback so a stuck dir can't block the
  resume re-mount.

- **A git call exceeding its timeout no longer crashes the whole run (#156).** Every git
  subprocess the orchestrator spawns now translates `subprocess.TimeoutExpired` into
  `GitError`, so the existing degrade guards handle a slow git like any other git failure.
  The rollback gate specifically (`_rollback_or_pause`'s dirty check — the reported crash
  path) degrades to assume-dirty: rollback OFF pauses with the manual-recovery notice and
  the worktree kept; ON / resolved re-drives still auto-recover behind their preserve
  steps. A `rollback-dirty-check-failed` journal entry records the fault. The bound is now
  configurable as `limits.git_timeout_s` (default 120).

- **Session timeouts now fire on time and leave a forensic trail (#157).** A
  `session_timeout_min` that fired but journaled its session-end 2h19 late — with zero record
  of _when_ the deadline was declared or why — is now timely and observable on three fronts.
  (1) `wait_for_completion` gains a **wall-clock co-bound**: a host suspend (macOS sleep)
  freezes `time.monotonic()`, silently extending the monotonic deadline by the nap's length;
  the wall clock keeps counting through a suspend, so it may now EXPIRE the deadline — never
  extend it (a stepped-back wall clock changes nothing, and all sub-waits stay monotonic).
  (2) The fire moment stamps the result (`timeout_fired_at`, `timeout_expired_clock` —
  `"wall"` alone is the suspend fingerprint) and appends a `timeout-fired` line to
  `tasks/<id>/session-lifecycle.jsonl`; each wait tick tops up a throttled
  `tasks/<id>/heartbeat.json` whose staleness under a still-live session diagnoses a frozen
  orchestrator (the previously uninstrumented gap). The engine journals **session-end
  unconditionally** — even a teardown that throws still records the ended session (status
  `aborted` when the outcome is unknowable), carrying `fired_at`/`teardown_s`/`expired_clock`.
  (3) Teardown is now a **verified kill escalation**: `terminate → wait → force_kill`, where
  `limits.teardown_grace_s` bounds the liveness-wait before escalating (default 20; `0` = a
  single unverified best-effort kill) and every escalation step carries its own bound, so a
  timeout can no longer hang on an unkillable session. Covers the tmux (`generic`) and
  `opencode-http` adapters alike. A frozen
  process still cannot run this code while frozen, but recurrence is now diagnosable rather
  than silent.

- **`validate` and `probe-adapter` no longer report antigravity's hooks as unregistered
  (#159).** The `antigravity-hooks-json` dialect keys `.agents/hooks.json` by hook-group name
  at the top level, with no `"hooks"` wrapper — but both readers looked up `"hooks"`, got `{}`,
  and reported a correctly-installed relay as missing (`FAIL: bmad-loop hooks not registered
for antigravity`, immediately after a successful `init --cli antigravity`). Both now share
  one `install.relay_registered()` helper that resolves each dialect's container shape, so the
  two call sites can no longer drift apart. `init`'s merge dedup keys on the narrow bmad-loop
  script markers rather than the bare `bmad_loop` substring, so an unrelated hook command whose
  path merely contains `bmad_loop` can't make init skip a registration that validate would then
  report missing.
- **The antigravity hook relay now reads agy's payload keys.** agy encodes hook payloads as
  protojson — `conversationId`, `transcriptPath`, `workspacePaths` — while the relay only tried
  snake_case plus copilot's `sessionId`. Every agy event therefore recorded a null
  `session_id`, and `cwd` was never populated (agy sends no `cwd`, only a `workspacePaths`
  list). Both the relay and the probe capture hook now try agy's casing, verified against a
  live 1.1.3 turn.
- **`probe-adapter antigravity` finds the transcript.** The shipped convention glob had the
  wrong filename — agy writes `transcript_full.jsonl`, not `transcript.jsonl`, under
  `~/.gemini/antigravity-cli/brain/<conversationId>/.system_generated/logs/`. Corrected against
  a live capture. A live `--probe` now also prefers the `transcriptPath` the CLI hands the hook
  on stdin over the convention glob: the payload names _this_ turn's file, while a glob can
  only take the newest match and may land on an unrelated session.
- **antigravity: `usage_parser = "none"` is now documented as permanent, not pending.** A live
  capture confirmed agy's transcript carries only
  `step_index`/`source`/`type`/`status`/`created_at`/`content`/`thinking` — no usage block
  anywhere. agy does count tokens, but only inside `conversations/<id>.db`, an undocumented
  SQLite/protobuf store outside the `(transcript_path) -> TokenUsage` parser contract. Runs
  work; token columns stay empty.
- **antigravity: trust is exact-path (verified against `agy` 1.1.3).** `agy` blocks on an
  interactive "trust this folder" dialog for any workspace not listed verbatim in
  `settings.json` `trustedWorkspaces`; a trusted parent does not cover subdirectories, and
  `--dangerously-skip-permissions` does not bypass it (it covers tool permissions only).
  `isolation = "none"` (the default) works; `isolation = "worktree"` hangs on every run, since
  each worktree is a fresh untrusted path — now called out in the profile and setup guide, and
  tracked in #169. Replaces the profile's previous "verify during probe" placeholder.
- **Follow-up review sessions are no longer killed on their first Stop by the dev pass's stale
  `## Auto Run Result` (#160).** The review leg re-invokes bmad-dev-auto on the finalized (`done`)
  spec whose dev pass left that terminal marker; the review's own entry write lifted it past the
  adapter's launch-mtime floor, so the first result-less Stop read the stale marker as this
  session's result and ended the review mid-flight (the #109 stall grace never armed). The engine
  now strips the marker before every review launch — the frontmatter `done` stays, so step-01
  still routes to a review pass. The review-budget exhaustion defer reason now reports the last
  pass's actual status instead of always claiming a lingering follow-up recommendation.
- **`branch_per=run` + `keep_failed` no longer poisons a multi-story run after the first kept
  failure (#138).** The first story to end deferred under `keep_failed=true` left its worktree
  checked out on the single shared run branch, so every subsequent story's `git worktree add`
  collided ("branch already checked out") and insta-deferred with zero dev activity — one kept
  failure turned an N-story run into a 1-story run. A kept worktree under `branch_per=run` now
  detaches its HEAD (`git checkout --detach`), freeing the shared branch name for the next story
  while preserving the working tree, uncommitted changes, and the branch ref (still at the kept
  commit) for inspection; subsequent stories mount the run branch normally and get genuine
  attempts. Best effort — if the detach ever fails, the existing `worktree-open-failed` defer
  still surfaces the collision (no regression). The escalate-and-pause path was already safe: it
  halts the run rather than continuing, and resume frees the kept worktree before any sibling mounts.

- **Dev/review sessions can no longer livelock on their own wake nudges (#149).** The idle
  wake nudge is delivered as a submitted turn, so a session that merely _answers_ it ends in
  another result-less Stop — which refilled the nudge budget, re-armed the grace window, and
  repeated until `session_timeout_min`, burning a turn per cycle. Dev/review sessions now get the
  same monotonic cap injected workflow sessions already had: after `limits.dev_stall_nudges_cap`
  (default 6) total nudges the session is declared stalled instead (post-kill reconcile still
  rescues a finished one whose terminal artifact is on disk). The nudge text now also states that
  a prose reply cannot end the session. And each result-less Stop leaves a diagnostic breadcrumb
  (`tasks/<task_id>/resultless-stops.jsonl`: pending / not-terminal / stale-mtime / ambiguous /
  no-artifact / no-result-json) so _why_ a completed-looking session read as result-less is
  answerable from the run dir.

- **Split-story keys (`2-6a-…`) are no longer silently skipped (#144).** The sprint-status
  parser rejected story numbers carrying BMAD's split-story letter suffix, dropping exactly the
  stories that were split to be loop-tractable — invisible to `run`/`--story`/the TUI tree, and
  skipped by the epic-lift. The suffix is now a first-class `Story`/selector field: `--story 2-6a`
  (or `2.6a`, or `--epic 2 --story 6a`) selects exactly that half, while a plain `2-6` selects the
  whole `2-6a`/`2-6b` family in file order. `run` and `--dry-run` also print a stderr warning when
  sprint-status keys remain unparseable, instead of only journaling them.

- **Review leg repairs a finalize-tail death.** A review session that died between writing its
  terminal `## Auto Run Result` (`Status: done`) and flipping the spec frontmatter off the transient
  `in-review` marker left the orchestrator re-reviewing already-finished work — a burned review
  cycle. The review leg now runs the same terminal-status reconcile the dev leg does: when the prose
  says done and the frontmatter sits at a reconcilable non-terminal status, it advances the spec to
  `done` and re-folds the frontmatter's `followup_review_recommended` flag (only when present) before
  the convergence/damping gate reads it. Bookkeeping-only — every deterministic verify gate still
  runs against real on-disk/git state, so it cannot pass uncompleted work.

- **Claude sessions launch with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` (#109).** Claude Code
  could bias a dev session toward backgrounding its implementation sub-agent despite the
  bmad-dev-auto prompt ban; the session then ended its turn to await a completion notification,
  and a harness exit at that turn boundary stranded the sub-agent with the story stuck
  `in-progress` → manual-rollback pause. The shipped claude profile now forces subagents and bash
  to run synchronously — the behavior the skill contract already requires. Opt out with a custom
  profile in `.bmad-loop/profiles/`.

- **Resume no longer discards a story that already passed its pre-commit gates.** A host death in
  the COMMITTING window (phase persisted before `finalize_commit` ran and the DONE save stamped
  `commit_sha`) matched no resume arm — there is no COMMITTING-keyed session record to replay — and
  fell through to resume-restart, rolling back or pausing over fully-verified work. Resume now
  finishes the commit in place: the `pre_commit_gate` workflows are not re-charged (the persisted
  phase is durable proof they passed), the `pre_commit` hook re-fires (message regeneration and
  pause veto honored), and `finalize_commit`'s content-idempotence covers both the pre- and
  post-squash crash states. Sweep bundles get the same recovery in `_recover_inflight_bundle` (#115).

- **Resume no longer asks for a rollback of a completed session's committed work.** A host death
  in the post-verify decision window left the task persisted at `DEV_VERIFY`/`REVIEW_VERIFY`,
  where the resume replay matcher (which only knew the `*_RUNNING` phases) missed the
  durably-recorded completed session and fell through to resume-restart — pausing with a
  `git reset --hard <baseline>` instruction that would discard the attempt's finished, possibly
  already-pushed commits. Those phases now replay the recorded result through the normal
  verify/decide pipeline, and the rollback-OFF manual-recovery notice detects commits above
  baseline and leads with saving/checking them instead of a bare reset (#100).

- **The TUI no longer crashes on a private-mode CSI sequence in an adapter log.** The gemini
  CLI's startup burst includes XTMODKEYS `CSI > 4 ; ? m`; the marker byte sat _inside_ the
  params, so the private-marker strip filter missed it and pyte 0.8.2 raised a `TypeError`
  that killed the poll worker — and the whole dashboard. The filter now matches a marker
  anywhere in the params, and any escape sequence pyte still can't parse is dropped instead
  of propagating (upstream fix exists but was never released — selectel/pyte#202) (#111).

- **An unreadable spec no longer crashes the whole run.** Every spec read-back — the four verify
  gates, the reconcile/sprint/ledger bookkeeping passes, and the generic adapter's Stop poll — raced
  the dev skill's own writes, so a transient `OSError` (a TOCTOU truncation, a lock, an EACCES)
  escaped to `engine.run()` and abandoned every remaining story. Observation now degrades where
  repair still raises: verify gates return a retryable outcome naming the read fault (never a phantom
  status mismatch), bookkeeping passes skip and journal `spec-read-failed`, and the read-back poll
  treats it as not-yet-terminal, falling through to the existing stall/timeout → post-kill-reconcile
  ladder. Review routing re-derives `followup_review_recommended` from the finalized spec when a
  replayed result lacks it, so a fault that skips the reconcile re-fold can no longer silently skip
  a recommended follow-up review on resume (#97).

- **A resumed sweep re-drives its in-flight bundles by identity, not by bundle name.** `SweepEngine`
  recovered a bundle only from inside `_run_bundle`, which a cycle reaches after re-deriving the key
  from the _current_ triage plan — so a bundle re-armed by `bmad-loop resolve` survived a resume only
  because the cached `triage.json` reloaded and re-emitted the same name. Lose that cache and a fresh
  triage partitioned the ids under new names, silently orphaning the human's resolution. The sweep
  loop now opens with `_finish_inflight_bundles`, mirroring the base engine: every non-terminal `dw*`
  task is re-driven under its own persisted `story_key`, before the ledger is read, so its ids leave
  the open set and no fresh plan can re-bundle them. A still-escalated bundle stays terminal and
  untouched. A missing bundle intent file is regenerated from the task (the verbatim ledger entries
  become the contract; the triage prose is the only unrecoverable piece), and a bundle that survives
  to a cycle anyway is journaled + notified rather than dropped. Relatedly, a truncated or
  wrong-shaped `triage.json` now degrades to a fresh triage instead of crashing the whole run — the
  corrupt leg of this bug was previously unreachable. New journal events: `sweep-inflight-redrive` /
  `-stranded`, `sweep-intent-regenerated`. (#94)
- **Run ids are validated, so a run ref can no longer escape the runs directory.** A positional ref
  (`delete`, `stop`, `archive`, `resume`, `status`) was recomposed into a path raw, so
  `bmad-loop delete ../../x` deleted any outside directory holding a `state.json`; the hidden
  `--run-id` flag on `run`/`sweep` reached a directory name, a multiplexer session name and a git ref
  unchecked. A supplied id must now match `[A-Za-z0-9][A-Za-z0-9_-]*` (≤ 120 chars, no reserved
  Windows device name) — rejected, never sanitized, so ids stay bijective with paths and sessions —
  and a ref that is absolute, climbs with `..`, or carries a separator skips the exact-match branch,
  falling through to partial matching over enumerated run dirs only. Ids recovered from the outside
  world — a `bmad-loop-<id>` session name, a `<kind>-<id>` control-session window name — pass the
  same validator before they steer a path. Partial refs unaffected (#104).

- **An abandoned patch-restore no longer smuggles its files into the corrected story's commit.**
  Re-arming a story whose previous re-drive had already applied a restore patch snapshotted that
  patch's new (untracked) files as _pre-existing_, so every later rollback preserved them and
  `finalize_commit`'s `add -A` swept the abandoned attempt into the corrected commit. The re-arm now
  parses the old latch (`verify.patch_new_files`) and subtracts its creations from the refreshed
  baseline snapshot — the re-drive's own reset then removes them. Best-effort: a missing or
  unreadable patch degrades to the old behavior instead of failing the resolve. Commits the
  escalated attempt left below the advanced baseline can't be reverted mechanically (the resolve
  session's own commits share that range), so they are journaled and echoed to stderr for the human
  to classify. New journal events: `stale-restore-excluded` / `-unparseable` / `-commits`.
  (closes #90)
- **Baseline-era untracked residue no longer vacuously satisfies the proof-of-work gate.**
  `has_changes_since` counted every untracked file. After an intent-gap halt the saved patch is
  untracked residue under the artifact dirs every reset deliberately protects, so a from-scratch
  re-arm — which never learns the patch's path — let a re-driven session that produced nothing but a
  spec status flip pass the gate on that file's mere presence, and `finalize_commit`'s `add -A` swept
  it into the story commit. The gate now subtracts the task's `baseline_untracked` snapshot. A `None`
  snapshot (a pre-upgrade run) still counts every untracked file — deliberately the opposite of
  `attempt_dirty`'s ignore-all, because a proof-of-work gate has to fail open toward "work happened".
  (closes #88)
- **The baseline-match verify gate was dead code for generic dev sessions.** The gate read the spec's
  `baseline_commit` and skipped itself when that key was absent — but `bmad-dev-auto` stamps
  `baseline_revision`; `baseline_commit` exists only in the orchestrator's synthesized `result.json`.
  In production the check never fired, so a spec claiming a stale or foreign baseline sailed through.
  The gate now reads either key, the idiom `devcontract` already used. The test fixture stamps
  `baseline_revision` like the real skill does, so it can no longer fabricate the key that hid this.
  (closes #89)
- **Unit keys with git-ref-illegal characters no longer break worktree runs.** `unit_branch_name`
  built `bmad-loop/<run_id>/<unit_key>` from the raw ids, so a key or `--run-id` carrying `:`, `..`,
  `@{`, a space or a trailing `.lock` cleared the (already-sanitized) worktree dir only to die at
  `git worktree add` with _"is not a valid branch name"_. Both segments now go through a new
  `platform_util.safe_ref_segment` — identity for clean ids, `-<hex8>` digest suffix otherwise, on
  git's alphabet rather than Windows' (`CON` is a legal ref; `a..b` is a legal filename). A
  `git check-ref-format` oracle test pins the agreement; the `attempt-preserve` recovery-ref slugs
  now reuse the same sanitizer instead of their own inline one. (closes #102)
- **The deferred-artifact stash overwrites its target atomically.** A story deferring a second time
  re-stashes the same spec filename over the previous one. `shutil.move` fell back to a non-atomic
  `copy2` there — which tears the stash on a mid-copy crash and fails outright on Windows when an
  AV/indexer handle turns the rename into a sharing violation. The stash now stages a copy inside the
  destination dir and routes through `platform_util.atomic_replace`, inheriting its win32 retry; the
  source removal gets the same retry via a new `platform_util.retrying_unlink`, since Windows denies a
  delete against an open handle exactly as it denies a rename-over. (closes #101)
- **A finished session whose final `Stop` hook was lost no longer loses its work.** A dev/review
  session that wrote its terminal spec but never delivered the `Stop` ended `stalled` — or `timeout`,
  when hooks were misconfigured and no event ever arrived — and the on-disk result was discarded.
  The adapter now re-reads the spec after the window is provably dead, rescuing a self-consistent
  successful terminal; every rescue still faces the full deterministic verify, and the journal records
  `session-rescued-post-kill` so it stays distinguishable from a live completion. (#95, closes #61)
- **A corrupt terminal artifact no longer crashes the whole run.** A spec truncated mid-write (a
  multi-byte UTF-8 sequence cut in half) raised out of the read-back and past the per-task boundary,
  marking the run `CRASHED` and abandoning every remaining story. The read-back now degrades an
  undecodable spec to "no result yet" — the session retries or keeps its verdict — and the post-kill
  rescue additionally keeps its verdict on _any_ read fault, so a best-effort rescue can never make
  things worse. The repair path still raises on purpose. (#95, closes #96)
- **Windows installs now pull `psutil` automatically** — moved from the opt-in `non-linux` extra to a
  platform-scoped core dependency (`sys_platform == 'win32'`), so the TUI liveness column no longer
  shows every run as `?` on a stock install. macOS keeps the `non-linux` extra; Linux stays dep-free.
  (#72, closes #71)
- **`bmad-loop-setup` no longer deletes live core BMAD config or the installer manifest.** In a
  multi-module BMAD v6 project the setup scripts hardcoded `core` (and `--also-remove _config`) into
  their delete lists, destroying `_bmad/core/config.yaml`, per-module config, and the whole
  `_bmad/_config/` manifest — breaking future `npx bmad-method install` upgrades. Cleanup now removes
  a directory only when it is a verified-redundant skill payload (has a `SKILL.md`, carries no
  config/manifest, and its skills are installed); live config dirs are protected and reported under
  `directories_protected`. The merge scripts read legacy config as fallback but never delete it. Same
  root cause as upstream `bmad-code-org/bmad-builder#96`. (closes #64)

## [0.8.1] — 2026-07-05

### Fixed

- **A session that finished its work but crashed before the run recorded it no longer loses that work
  to a restart-and-rollback.** The engine now records a completed dev/review session behind a
  durability barrier _before_ running post-session hooks (usage is folded in afterward as best-effort
  metadata); on resume it consumes that durably-recorded result straight into verify/decide instead of
  restarting the attempt from baseline. A raw `KeyboardInterrupt` now records a controlled stop, and
  replay preserves the attempt/cycle/baseline. (#62, closes #57)
- **Several narrower resume-replay edges opened by that durability work are closed.** A host death in
  the post-session window of the _last_ allowed review cycle no longer drops a recorded clean review to
  a defer; a reconcile early-return no longer persists a pre-reconcile dict that could silently skip a
  recommended follow-up review; and triage/sweep and labeled plugin-workflow sessions no longer persist
  large, never-consumed result payloads into `state.json`. (#63)
- **A dev session is now completed only on a real Stop or window death, never on a terminal artifact
  glimpsed while the agent window is still alive.** The idle-tick and grace-expiry shortcuts could
  return "completed" mid-turn and let the run's cleanup kill a working agent, handing the engine
  half-written state. Both shortcuts are removed, liveness is re-probed immediately before the
  grace-expiry stall verdict, and a re-driven spec has its stale `## Auto Run Result` section stripped
  on both re-arm paths so a resolved re-drive can't be misread as its own terminal result. Result scans
  are now fence-aware, so an `## Auto Run Result` heading quoted inside a fenced code block is never
  mistaken for a real section (nor destructively stripped). (#53, closes #48 #52)
- **Long journal fields in the TUI now wrap with a hanging indent instead of spilling back under the
  timestamp and kind columns.** Each row renders as a fixed-width grid whose fields cell folds within
  its own column.

### Added

- **Attempt-preserve recovery refs are now bounded.** With `scm.rollback_on_failure` on, auto-rollback
  parks a failed attempt's committed work under `attempt-preserve/*` and its dirty worktree snapshot
  under `attempt-preserve-dirty/*`; nothing pruned them, so they grew unbounded. Run start now keeps
  only the newest `scm.preserve_keep` (default 20, `0` = never prune) per family by committer date and
  best-effort-deletes the tail — a stuck ref never wedges the ones behind it, and prune failures are
  journaled but never block the run. (#50 #54, closes #32 #49)

### Changed

- **The adapter shell-dialect seam is now an explicit, documented contract.** `new_window` /
  `new_parked_window` factor their shell-dialect fragments into overridable hooks (POSIX output stays
  byte-identical), and the `command` parameter's semantics are pinned in the ABC docstring
  (shlex-joined argv; operator handling is backend-defined). Relevant only to authors porting the
  adapter to a non-POSIX backend. (#47 #60)

## [0.8.0] — 2026-07-03

### Changed

- **BREAKING: the project is renamed `bmad-auto` → `bmad-loop`.** The distribution, console script,
  and CLI are now `bmad-loop`; the Python package is `bmad_loop` (was `automator`); the BMAD module
  code and marketplace plugin are `bmad-loop` (was `bauto`); per-project state moves from
  `.automator/` to `.bmad-loop/`. The GitHub repo is now
  [bmad-code-org/bmad-loop](https://github.com/bmad-code-org/bmad-loop) — old web and git URLs
  redirect. Clean break: no compatibility shims.
- **BREAKING: renamed public identifiers.** Env vars `BMAD_AUTO_*` → `BMAD_LOOP_*`; plugin
  entry-point group `bmad_auto.plugins` → `bmad_loop.plugins`; hook relays `bmad_auto_hook.py` /
  `bmad_auto_probe_hook.py` → `bmad_loop_hook.py` / `bmad_loop_probe_hook.py`; skills
  `/bmad-auto-{setup,sweep,resolve}` → `/bmad-loop-{setup,sweep,resolve}`; tmux session/window
  prefixes `bmad-auto-*` → `bmad-loop-*`; worktree branches `automator/<run-id>` →
  `bmad-loop/<run-id>`; TUI class `BmadAutoApp` → `BmadLoopApp`. Custom plugins, CLI profiles, and
  policy files that reference any of these must be updated.

### Added

- **New `pre_commit_gate` plugin workflow-injection stage.** Gate workflows can bind to
  `pre_commit_gate`, which fires unconditionally just before every commit — on the review-converged,
  review-skipped, and review-budget-rescue paths alike — while the phase can still legally defer.
  TEA's trace/nfr/review gates rebind to it, fixing blocking gates that were previously inert
  whenever a dev session recommended no review follow-up (so `on_pre_commit` fail-opened on the
  missing artifacts).

### Fixed

- **A workflow session that finishes its work but never writes a completion marker no longer
  livelocks the run.** Each result-less Stop used to refill the stall-nudge budget, re-nudging a
  responsive-but-signal-less session until `session_timeout_min`. The engine now appends an explicit
  completion contract (absolute marker path + frontmatter shape) to every workflow-session prompt,
  and a new `limits.workflow_stall_nudges_cap` (default 3) caps the total nudges a workflow session
  may receive — degrading a still-missing marker to "stalled" in ~30 min instead of hours. Dev/review
  session nudging is unchanged.
- **On Windows, a live engine whose process identity can't be read now shows `UNKNOWN` instead of a
  false `INTERRUPTED`.** psutil raises `ERROR_ACCESS_DENIED` for a process in another session or
  elevation, which the identity-aware liveness read had surfaced as dead — mislabeling running runs
  and weakening the resume/delete guards. Liveness is now tri-state (`alive`/`dead`/`unknown`) and
  biased away from false-dead; `resume`, `resolve`, `delete`, `archive`, and cleanup all surface and
  warn on `unknown` without ever letting an unverifiable pid block cleanup forever, and `resolve`
  gains `--force` to override a squatted-pid block.
- **A review session that appends to the deferred-work ledger no longer leaves a sweep bundle
  unclosed.** The bundle ledger is reclosed after review (journaled distinctly as
  `sweep-bundle-reclosed`), and the review prompt now states the ledger is append-only for sessions.
- **Worktree git-exclude patterns now anchor correctly on native Windows.** `install.py` normalizes
  backslashes in the per-worktree exclude paths so the ignore rules match (a no-op on POSIX).

### Migration

- **Reinstall the tool under its new name** — uv can't rename a package in place:
  `uv tool uninstall bmad-auto`, then
  `uv tool install "bmad-loop[tui] @ git+https://github.com/bmad-code-org/bmad-loop.git"`.
- **Re-run `/bmad-loop-setup`** (or `bmad-loop init` directly). `init` migrates a project in place:
  it strips the old `.automator/` Stop hook from each CLI's settings, removes the `bmad-auto-*`
  skill dirs, and carries `.automator/policy.toml` over to `.bmad-loop/policy.toml`. Setup folds the
  old `bauto` config into `bmad-loop` and clears the leftover `bauto` config section, stale
  `BMAD Automator Skills` help rows, and the `_bmad/bauto/` installer dir.
- **Legacy `.automator/` is left in place** (runs, archives, profiles, plugins) and can be deleted
  or hand-moved once the migration is confirmed; stale `.automator/*` gitignore lines are left
  untouched.

## [0.7.12] — 2026-07-01

### Added

- **The TUI dashboard now shows the cost-proportional weighted token total, with the raw total in a new
  column.** The `tokens` column and the run-header summary discount cache-read tokens by the run's
  `cache_read_weight` — the same weighting the per-story budget enforces — so the headline number
  tracks spend rather than context re-reads; the previous unweighted total moves to a new `raw` column.

### Fixed

- **The dashboard no longer crashes when a background poll lands as the screen is torn down or
  switched away.** A poll worker delivers its refresh on the UI thread; if that arrived just as the
  app quit or another screen opened, the query for the run table raised `NoMatches`. The apply now
  drops stale refreshes for a screen that is no longer running (while still updating one merely
  backgrounded under a modal).
- **A failed attempt's work is preserved before an auto-rollback hard reset instead of being silently
  discarded.** With `scm.rollback_on_failure` on (or on a resolved re-drive), a deferred or stopped
  attempt's commits above baseline are now parked under an `attempt-preserve/<run_id>-<head8>` branch,
  and its uncommitted working-tree diff — tracked edits and run-created untracked files alike — under
  `refs/attempt-preserve-dirty/`; both are recoverable by name and survive gc. A plain rollback that
  cannot create the ref refuses to reset and pauses for manual recovery rather than destroying work.
  The uncommitted snapshot is scoped to this run's own changes (never a pre-existing untracked file),
  commits under a synthetic identity so it works with no git user configured, and is keyed per retry
  so repeated rollbacks against the same baseline no longer overwrite each other's recovery ref.
- **Process liveness is now identity-aware, so a reused PID no longer reads as a live run.** A recycled
  pid (common on Windows) used to register as a false "alive" — blocking resume of a dead run,
  stranding worktree reclaim, leaking sessions, and showing dead runs as RUNNING. The pid file now
  carries a process-identity token that resume, stop, and the TUI verify against; on win32 the engine
  also ignores console SIGINT/SIGBREAK during a run so a ConPTY Ctrl+C broadcast can't kill it.
- **A story from a resolved escalation that still can't finish now re-escalates instead of being
  silently deferred.** When a human-resolved CRITICAL `blocked` escalation was re-driven and the
  re-drive couldn't reach `status: done` (e.g. the environment was still broken), the story used to
  exhaust its dev/review budget and plateau-defer — filing an unresolved blocker as deferred work and
  rolling back the implemented code. While `resolved_redrive` is latched, budget exhaustion now
  re-escalates (pauses for the human) instead of deferring, and the attempt's tree is preserved.
- **A resumed `--epic N` run stays scoped to its epic and no longer declares the epic "done" while
  stories remain.** `resume` rebuilt the engine without the run's `--epic`/`--story`/`--max-stories`,
  so a scoped run silently widened to every epic; with strict file-order story selection, deferring or
  finishing a story in an epic placed out of numeric order in the sprint board (e.g. one appended last)
  bounced selection to an earlier-in-file epic and fired a spurious "epic N complete" boundary,
  stranding the epic's remaining stories. The selector and cap are now persisted and restored on
  resume, and story selection exhausts the current epic before advancing — so an epic boundary fires
  only when that epic has no actionable stories left. Document-order epic execution is unchanged.

## [0.7.11] — 2026-06-30

### Fixed

- **A finalized story the review just won't stop recommending a follow-up for is now committed, not
  rolled back, when the review budget runs out.** Exhausting `limits.max_review_cycles` previously
  always deferred + reverted — discarding completed, review-passing work (frontmatter `status: done`,
  verify green) whose only "failure" was a never-clearing `followup_review_recommended`. The
  orchestrator now commits that work and re-files the lingering recommendation as a fresh open
  deferred-work entry; a story that itself came from such an entry is committed without re-filing, so
  a second non-convergence reaches a human instead of looping across sweeps. Worktree-isolation runs
  were unaffected — a deferred unit already keeps its worktree.

## [0.7.10] — 2026-06-29

### Fixed

- **Completed work left at the transient `in-review` frontmatter is no longer falsely deferred and
  rolled back.** A `bmad-dev-auto` session that dies in its step-04 Finalize tail can leave the spec
  frontmatter at the transient `in-review` marker while the `## Auto Run Result` prose already says
  `Status: done` — the same stale-frontmatter gate bug as 0.7.8 with a different value. The 0.7.8
  reconcile skipped `in-review` to protect the legacy `bmad-auto-dev` review-handoff, but that fork
  is retired and `in-review` is now only ever a transient marker, so it is reconciled to `done`
  before the gates run. Every deterministic gate still runs afterward, and a `followup_review_recommended: true`
  spec still triggers the follow-up review pass. Closes a re-sweep loop that re-ran and discarded the
  same completed bundles (~47M tokens/cycle).

## [0.7.9] — 2026-06-29

### Fixed

- **The multiplexer seam no longer leaks raw subprocess timeouts.** Every contract method now raises
  `MultiplexerError`/`TmuxError` or returns its documented sentinel instead of letting a 30 s tmux
  hang escape as a raw `subprocess.TimeoutExpired`.
- **A transient tmux hang no longer crashes a run or mis-reads a working session as dead.** The
  wait-loop tolerates an unknowable liveness probe — a persistent hang degrades to an honest
  `timeout`, never a false `crashed`.
- **An unexpected engine exception is now recorded instead of being lost to the parked control
  window.** The orchestrator writes a `run-crash` journal line + a `crash.txt` traceback, sets a
  `CRASHED` run status, and tears down the orphaned agent session — rather than dying with a
  traceback printed only to the pruned control pane.

## [0.7.8] — 2026-06-29

### Fixed

- **Completed `bmad-dev-auto` work is no longer falsely deferred when the skill leaves the spec
  frontmatter `status` stale.** The skill can finalize a run in its `## Auto Run Result` prose
  (`Status: done`) yet leave the YAML frontmatter at the template default `draft`; since every gate
  reads frontmatter, the sprint/ledger sync no-op'd and the story or sweep bundle deferred — losing
  tested work on rollback. The orchestrator now reconciles the frontmatter to the success status
  from the terminal prose before the gates run (journaled `spec-status-reconciled`). It reconciles
  only a `done` outcome from a non-terminal status, and every deterministic gate (worktree diff,
  dw-ids, verify commands, ledger) still runs — so the bookkeeping is repaired without trusting prose
  to pass a gate.

## [0.7.7] — 2026-06-28

### Fixed

- **Spec-status gates are now case- and whitespace-insensitive.** A hand-edited spec whose
  frontmatter carried a stray-cased `Done`/`In-Review` silently failed the dev/review gate and the
  story never advanced; every spec-frontmatter status read now normalizes through a single
  `verify.status_of` helper. The well-formed lowercase path is unchanged. Also fixes the
  manual-rollback notice, which rendered an invalid `git reset --hard the run's baseline commit`
  when no baseline was recorded — it now shows a `<baseline_commit>` placeholder.
- **Project-relative path guards reject `..` traversal and OS-foreign absolute paths.** A CLI
  profile or plugin manifest could declare a `config_path`/`skill_tree`/`seed_files`/module path
  that climbed out of the project with `../` — or, on Windows, a POSIX-absolute `/etc/...` that
  `Path.is_absolute()` failed to flag — and slip past the "must be project-relative" check. The
  guards now reject both, on every platform.
- **Persisted relative paths serialize with forward slashes.** A worktree run's `spec_file` and the
  resolve context's `resolution_path` were written with the host separator, so a `state.json` or
  context file produced on Windows read back with backslashes. Both now persist via `as_posix()` for
  a single cross-OS contract (a no-op on POSIX).
- **The TUI no longer shows a stale run after a same-size state rewrite.** The dashboard's
  stat-gated cache keyed on `(mtime_ns, size)`, so an atomic `state.json` rewrite of identical size
  within one coarse mtime tick (e.g. WSL2 drvfs) could be served stale. The engine rewrites
  atomically onto a fresh inode, so the cache signature now includes `st_ino`.
- **A dev session is no longer mis-stalled while it is actively working or legitimately waiting.**
  Building on `dev_stall_grace_s` (0.7.5), the idle-grace window now measures genuine _inactivity_
  rather than time-since-last-Stop: any growth of the session's pane log (a long productive turn, a
  streaming subagent) re-arms it, so a session that has finished implementation and is mid-review is
  no longer killed and rolled back. And because bmad-auto cannot re-invoke a turn that ended to await
  a background process, the grace window no longer dead-ends in a stall — on real silence the
  orchestrator wakes the session with up to `limits.dev_stall_nudges` (new, default 2) nudges before
  giving up; a genuine Stop restores the budget, so a slow-but-cooperative session waits up to
  `session_timeout_min` while a truly unresponsive one still stalls. `0` restores stall-on-grace-expiry.

## [0.7.6] — 2026-06-28

### Changed

- **The OS is now abstracted behind a registry of seams, so a non-tmux/native-Windows port is new
  files plus one registration line each — no core edits.** The terminal multiplexer is selected
  through a registry (`register_multiplexer`, by `sys.platform` with a `BMAD_AUTO_MUX_BACKEND`
  override) rather than hardcoded in `get_multiplexer()`, and the tmux backend split into a reusable
  `BaseTmuxBackend` extension point — every tmux invocation funnels through one overridable `_run()`
  primitive — with `TmuxMultiplexer` as a thin POSIX leaf, so a tmux-family backend (an eventual
  "psmux") overrides only the spawn primitive and the few divergent methods.
- **Process-lifecycle primitives moved behind a `ProcessHost` seam.** Politely-stop / force-kill /
  liveness / PID-reuse-identity now route through `get_process_host()` (registered like the
  multiplexer, with a `BMAD_AUTO_PROCESS_HOST` override); a `WindowsProcessHost` ships ready to
  register. Hook registration no longer hardcodes `python3` — it takes the interpreter prefix from
  `ProcessHost.hook_interpreter()` (POSIX `python3`; Windows `uv run --no-project python`), and
  `bmad-auto validate` runs a platform preflight that reports the selected backend's readiness and
  names the process host. Behavior on Linux/macOS/WSL is unchanged.
- **The bundled Unity plugin's teardown delegates pid lifecycle to `ProcessHost` and its helper
  scripts run under the orchestrator's own interpreter.** Plugin helper scripts are spawned via
  `sys.executable` (not a PATH-resolved `python3`), so a bundled script may import `automator` seams;
  `unity_teardown.py` now reaps leaked Editor/MCP processes through `get_process_host()` instead of
  re-implementing kill/liveness, gaining Windows behavior for free.

### Added

- **A consolidated [porting guide](docs/porting-to-a-new-os.md)** maps the four OS seams (terminal
  multiplexer, process lifecycle, hook interpreter, validate preflight), their registries and
  test-override env vars, and exactly what a native-Windows port costs end to end. The
  adapter-/plugin-authoring guides, ROADMAP, README, and FEATURES are updated to the post-registry
  world.

## [0.7.5] — 2026-06-28

### Added

- **Select and copy text from the TUI Log and Attention panes.** Click-drag highlights and `ctrl+c`
  copies (those panes are now selectable `RichLog`s with a working `get_selection`); `y` copies the
  whole active pane in one keystroke. The other panes are interactive widgets — hold your terminal's
  bypass modifier (Shift on most Linux terminals, Option on iTerm) to use its native selection. Copy
  rides OSC 52, so under tmux it needs `set -g set-clipboard on` to reach the system clipboard.
- **`bmad-auto diagnose` emits a sanitized diagnostic dump of a run/sweep** so a user can hand
  maintainers what's needed to debug a run without shipping any code, spec/story content, prompts,
  transcripts, file paths, or PII. It derives the diagnostic _shape_ — phase/token/session
  histograms, escalation counts, adapter/model, env, run-dir file sizes — and routes every
  content-bearing value through the audited `sanitize` chokepoint: identifiers (story keys,
  branches, SHAs) are pseudonymized to stable per-dump aliases so events still correlate, free text
  collapses to presence booleans, and the rendered output is re-scanned by a fail-closed leak
  self-check before writing. Defaults to the latest run; `--all`, `--out`, `--json`, and a
  local-only `--legend` are supported.

### Changed

- **The `sanitize` chokepoint now redacts credential-shaped strings.** Identifier-shaped secrets
  (`ghp_`/`sk-`/`AKIA`/`xoxb-`/JWTs and long high-entropy blobs) previously passed the slug gate
  verbatim; they are now `<redacted:secret>`, closing the same hole for `probe-adapter`.

### Fixed

- **A dev session that ends its turn to await a long-running background process is no longer
  mis-stalled.** A `bmad-dev-auto` session that yields to wait on a slow job (a Unity PlayMode run, a
  long test) and expects to be re-invoked on completion fired a result-less Stop — and since the
  generic dev adapter runs zero nudges, that was ruled stalled after only the 15s result grace,
  driving a retry that (with `scm.rollback_on_failure` off) paused the whole sweep for a manual
  reset. A new `limits.dev_stall_grace_s` (default 600s) opens an idle-grace window on a result-less
  dev Stop and re-arms it on each re-invocation, so only a genuinely idle gap with no terminal spec —
  or the session timeout — counts as a stall. Non-dev adapters keep zero grace and are unchanged.

## [0.7.4] — 2026-06-26

### Fixed

- **Deferred-work sweep no longer defers every bundle it just finished.** After the migration to
  the generic upstream `bmad-dev-auto` primitive, bundle dev sessions completed the work but were
  rejected by `verify_dev_bundle` with `result.json dw_ids [] do not match the bundle's […]`,
  retried to budget, deferred, and rolled the work back — so a sweep could never close a bundled
  entry. The retired dev fork used to echo the dw ids; the generic skill doesn't. The orchestrator
  already owns the bundle→dw-id binding, so the cross-check now passes when the session claims no
  ids, and the run exports `BMAD_AUTO_DW_IDS` so the synthesized result still carries them and the
  check stays live.

## [0.7.3] — 2026-06-26

### Fixed

- **The Log tab no longer shows only a single page for Claude runs.** Claude Code's new
  fullscreen TUI (an opt-in research preview) draws on the terminal's alternate screen and repaints
  in place, so the pane capture the Log tab emulates collapsed to the final frame — while
  line-oriented CLIs like codex still showed in full. The `claude` profile now forces the classic
  inline/scrollback renderer (`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`), which overrides any `tui`
  setting and keeps output in native scrollback so the whole session is reconstructable. As a
  safety net, the log view also detects an alternate-screen switch and flags such a capture as
  showing only the final frame, pointing at the agent's full JSONL transcript.

## [0.7.2] — 2026-06-26

### Fixed

- **Worktree isolation no longer false-stalls a story that actually finished.** Under
  `scm.isolation = worktree` the `bmad-dev-auto` session runs with its cwd set to the worktree and
  leaves its terminal spec in the worktree's `_bmad-output/implementation-artifacts`, but the dev
  adapter searched the main checkout's dir (resolved once at startup). The completed `status: done`
  spec was never found, so the orchestrator misread the session as stalled, rolled the unit branch
  back to baseline, and re-ran the same story. The adapter now resolves the spec directory from the
  live session cwd; in-place runs and artifact dirs configured outside the project tree are
  unaffected.

- **An uncommitted `policy.toml` edit no longer vanishes on rollback.** `policy.toml` is tracked but
  lives inside the kept `.automator` dir, so a rollback's `git reset --hard` to baseline silently
  reverted operator edits — a freshly enabled `scm.rollback_on_failure` could disappear before it ever
  took effect — and a lone policy edit could register as attempt dirtiness, trapping the
  manual-recovery loop. Rollback now restores `policy.toml` from its on-disk content unconditionally —
  so a config change committed after the baseline on an otherwise-clean tree survives too, not only
  one that rode a non-empty `git stash` snapshot — and the dirty check always excludes it, regardless
  of the preserve set.

- **Fixed a decision-toast notification race in the TUI test suite on Python 3.14.** Test-only; no
  runtime change.

## [0.7.1] — 2026-06-25

### Fixed

- **The Log tab no longer renders whole CLI sessions underlined.** Modern CLIs emit an XTMODKEYS
  sequence (`CSI > 4 ; 2 m`, "modifyOtherKeys") at startup that the pane emulator (pyte) misread as
  SGR 4 / underline-on — with no matching off present in a live capture — so every line came out
  underlined and hard to read. The log view now strips private-marker CSI sequences before emulation;
  genuine color, bold, and properly-closed underline styling is preserved.

- **Resolving a CRITICAL escalation no longer loops on a manual-rollback prompt when the resolve
  edited the spec.** 0.7.0 fixed the loop only for an already-clean tree, but the resolve workflow's
  whole job is to correct the frozen spec under the BMAD artifact folder (`_bmad-output/...`, which is
  tracked). So on resume the orchestrator saw a dirty tree and — with the default
  `scm.rollback_on_failure = false` — paused for a manual reset; because the dirty check diffs against
  the frozen `baseline_commit`, even committing the spec re-paused on the next resume, an endless loop.
  A resolved re-drive is human-initiated, so it now always auto-recovers regardless of the flag: the
  BMAD artifact folders are treated as orchestrator-owned — excluded from the dirty check and preserved
  through every reset of the re-drive (not just the resume-time cleanup) — so the spec correction
  survives while the failed attempt's source changes revert to baseline. This closes a latent sibling
  bug: with `rollback_on_failure = true` a _later_ mid-re-drive retry/defer reset previously ran with no
  preserve set and reverted the just-corrected spec silently, looping the re-drive.
  `scm.rollback_on_failure` still defaults OFF and now governs only unattended/stopped attempts; the
  manual-recovery notice (reached by stopped attempts only now) drops its resolved-cause wording.

- **A failed artifact restore during rollback now surfaces instead of silently dropping the
  correction.** When `safe_rollback` restores the preserved BMAD folders from its pre-reset snapshot, a
  genuine `git checkout` failure (corrupt snapshot, lock, IO) was swallowed alongside the benign
  empty-dir "pathspec did not match" case — so a corrected spec could vanish with no error and loop the
  re-drive. Real failures now raise; the empty-dir case stays tolerated.

## [0.7.0] — 2026-06-24

### Changed

- **Retired the `bmad-auto-dev` fork; the orchestrator now drives the upstream `bmad-dev-auto`
  skill unmodified** (bmad-code-org/BMAD-METHOD#2500, merged upstream) as its sole dev primitive.
  The skill is the inner autonomous coding session; everything automator-specific — escalation,
  sprint/ledger bookkeeping, repair-resume — stays in the orchestrator, which synthesizes
  `result.json` from the spec the skill leaves on disk. There is no fork to keep in sync with upstream.

- **Review is now a re-invocation of `bmad-dev-auto` on the done spec, not a separate skill.**
  `bmad-dev-auto` routes a `status: done` spec to a fresh step-04 review pass (BMAD-METHOD#2508), so
  the orchestrator's follow-up review just re-runs `/bmad-dev-auto <done spec>` in a fresh context.
  `review.enabled` still gates whether that follow-up pass runs at all; the new `review.trigger` knob
  (see Added) decides when it fires. The loop converges when a pass finishes `done` without the skill
  setting `followup_review_recommended`, still bounded by `limits.max_review_cycles` (default 3).

- **The skill commits each iteration; the orchestrator squashes to one commit per story.**
  `bmad-dev-auto` now commits its own work at the end of a successful run (BMAD-METHOD#2506). At
  finalize the orchestrator collapses that chain plus its own sprint/ledger writes back onto the
  pre-dev baseline into a single commit carrying the configured message — `pre_commit`/`post_commit`
  hooks and `scm.commit_message_template` stay authoritative.

### Added

- **Skill-recommended review (`review.trigger`).** `bmad-dev-auto` self-reviews inline and sets
  `followup_review_recommended` on a `done` spec when its changes warrant an independent pass
  (BMAD-METHOD#2505). The orchestrator consumes it: `review.trigger = "recommended"` (new default)
  runs the follow-up `bmad-dev-auto` review pass only when flagged; `"always"` keeps the old
  run-every-story behavior. Adjustable in the TUI and `policy.toml`. The follow-up loop stays bounded
  by `limits.max_review_cycles` (default 3) — the oscillation guard — so a skill-recommended review
  can never loop indefinitely.

- **Non-bundled-skill preflight.** `bmad-auto validate` and run/sweep/resume start verify that
  `bmad-dev-auto` and the two adversarial review hunters (`bmad-review-adversarial-general`,
  `bmad-review-edge-case-hunter`) — which `bmad-dev-auto`'s step-04 invokes inline on every run — are
  installed in each active CLI skill tree, failing loudly with remediation instead of stalling mid-run
  on an `Unknown command`. Worktree provisioning copies these upstream skills from the main repo,
  since they are not bundled in the wheel.

- **`result.json` `workflow` is now an enforced contract on the dev path.** `verify_dev` /
  `verify_dev_bundle` reject a mismatch against `verify.DEV_WORKFLOW` (`"auto-dev"`); the synthesized
  result carries `"auto-dev"`. Review re-runs the same skill, so it carries the same tag, and
  `verify_review` stays purely disk-derived — it is never handed the result.json.

- **Pluggable terminal-multiplexer seam (groundwork for native Windows).** All tmux usage now goes
  through a `TerminalMultiplexer` ABC (`get_multiplexer()`); `TmuxMultiplexer` is the only code that
  shells out to `tmux` and the only place the POSIX `sh -c` parked-window trailer lives. The generic
  adapter (renamed `generic_tmux.py` → `generic.py`), `runs.py`, `tui/launch.py`, `probe.py`, and
  `tui/data.py` all route through it, so a future non-tmux backend slots in with no engine changes.
  Behavior on Linux/macOS/WSL is byte-identical; **no native-Windows backend ships yet** (see [ROADMAP](docs/ROADMAP.md)).

- **POSIX portability hardening + CI guard.** Scattered POSIX-only primitives are guarded behind a
  platform seam — `SIGKILL` fallback, detach kwargs, `terminate_pid`, `os.devnull` — and the Unity
  plugin degrades off Linux (`/proc` → `psutil` via a new optional `windows` extra, `/tmp`, `cp -a`
  CoW, symlinks, `start_new_session`), keeping every Linux fast path unchanged. A new
  `tests/test_portability_guard.py` AST/byte scan blocks new POSIX-only patterns from creeping back,
  with sanctioned exceptions carrying `# portability:` acks.

- **Adapter & profile authoring guide.** `docs/adapter-authoring-guide.md` now carries the complete
  `CLIProfile` / `HookSpec` field reference (the single canonical schema home) and a "writing a new
  adapter class" section for non-tmux transports — linked from the README documentation index.

### Removed

- **Retired the bundled `bmad-auto-dev` and `bmad-auto-review` skills.** `bmad-auto init` now installs
  three bundled skills — `bmad-auto-resolve`, `bmad-auto-sweep`, `bmad-auto-setup` — and the upstream
  `bmad-dev-auto` skill (from a recent bmm module) is a hard prerequisite. `bmad-auto-review`'s
  adversarial review is fully covered by `bmad-dev-auto`'s inline step-04 (Blind + Edge-Case hunters);
  the independent Acceptance Auditor layer is dropped, and the two hunters are now always-required base
  skills rather than gated on `review.enabled`. The canonical `deferred-work-format.md` moved into
  `bmad-auto-sweep`, its remaining owner.

### Fixed

- **Resolving a CRITICAL escalation no longer loops on a manual-rollback prompt.** Re-arming an
  escalation requests a clean rebuild, which in non-worktree (in-place) runs means resetting to the
  story baseline. With the default `scm.rollback_on_failure = false` the orchestrator paused for a
  manual reset — but never cleared `baseline_commit`, so following the instructions (`git reset --hard`,
  then `resume`) re-paused on the next resume, an endless loop. `_rollback_or_pause` now no-ops when the
  tree is already at baseline (nothing this attempt touched), so a clean tree — including one the
  operator just reset — proceeds straight to the re-drive. The same guard suppresses the spurious prompt
  when an escalation left no changes at all.
- **Manual-recovery notice wording.** The prompt no longer claims the story "failed" — it now reflects
  the real cause ("escalation was resolved; re-driving needs a clean baseline" vs. "attempt was stopped").
- **Resolved escalations now actually re-drive instead of HALTing on a stale `blocked` spec.** `verify_dev`
  only recorded `task.spec_file` on a fully successful session, so a dev session that escalated with a
  `blocked` spec (the common escalation case) left it unset. `rearm_escalation` then had no spec path to
  flip to `ready-for-dev`, so on resume `bmad-dev-auto`'s step-01 routing re-HALTed on the still-`blocked`
  frontmatter — a second loop. The orchestrator now captures the spec the session produced when it
  escalates or defers (the synthesized result names it even on a HALT), so re-arm flips the status and
  the re-drive proceeds, and a deferred story's spec is stashed as intended.
- **Copilot dev stage no longer stalls on a subagent `agentStop`.** Copilot fires `agentStop` for
  every subagent turn too — with an empty `transcriptPath` and a tool-use session id, not the main
  session's turn-end. With dev decoupled to `bmad-dev-auto` (which implements via subagents), that
  premature Stop reached the dev stage, where 0 nudges made the orchestrator declare an outright stall
  before the skill wrote its terminal spec (same root cause as the 0.6.4 review stall). A new
  per-profile `subagent_stop_without_transcript` (true for `copilot`) ignores a `Stop` carrying no
  transcript, so the main session's real turn-end drives completion — and restores usage tallying,
  since that Stop carries the transcript.
- **Process liveness/termination no longer risks signaling the wrong process.** A corrupt
  `engine.pid` read as `0` or negative would make `os.kill` target a process group — for `0`, the
  orchestrator's own — so `pid_alive`/`terminate_pid` now reject non-positive PIDs before signaling.
  The remaining liveness checks (`runs.py`, `tui/data.py`) that called `os.kill(pid, 0)` directly now
  route through `pid_alive`, since on Windows `os.kill(pid, 0)` maps to `TerminateProcess` and is
  destructive; a CI guard blocks bare `os.kill(_, 0)` from regressing.

## [0.6.4] — 2026-06-21

### Fixed

- **Copilot token usage now records (was always 0).** Copilot writes its token totals only in
  the trailing `session.shutdown` events line, ~1s after `agentStop` — usage was sampled before
  it landed. `read_usage` now polls the transcript for a short grace, driven by a new per-profile
  `usage_grace_s` (8s for `copilot`, 0 elsewhere = read once).
- **Copilot multi-turn reviews no longer stall.** `agentStop` fires per response turn, so a
  parallel-subagent review ends several turns and tripped the global `stop_without_result_nudges`
  default of 1. New per-adapter floor (5 for `copilot`), overridable per stage via `[adapter.review]`.

### Added

- **`[adapter] usage_grace_s` / `stop_without_result_nudges`** (base + per-stage
  `[adapter.dev|review|triage]`), editable in the settings TUI. Unset = inherit the CLI profile's
  shipped default.

### Changed

- **Copilot docs.** Pin a capable model — the free GPT-5 mini default silently skips steps in
  multi-step dev/review — and it's the Copilot **CLI** binary that's supported, not the VS Code
  extension.

## [0.6.3] — 2026-06-21

### Fixed

- **GitHub Copilot adapter (CLI 1.0.63).** Turn-end is `agentStop`, not PascalCase `Stop`
  (which never fires) — every session previously read as a timeout. Remapped events, dropped
  the non-existent `PreCompact`, and the shared hook relay now reads camelCase payload keys
  (`sessionId`/`transcriptPath`). Probe mode sends its prompt verbatim so a skill-templating
  `prompt_template` no longer renders a missing-skill path that stalls the turn.

### Added

- **Copilot token accounting.** New `copilot-events` `usage_parser` reads
  `~/.copilot/session-state/*/events.jsonl` (`data.modelMetrics.<model>.usage.*`); the `copilot`
  profile is wired to it (was `usage_parser = "none"`).

## [0.6.2] — 2026-06-21

### Added

- **`bmad-auto probe-adapter` (alias `collect-adapter-data`).** A self-service command that
  collects and sanitizes everything needed to finalize a CLI adapter profile — the hook payload
  shape, transcript location/format, and token-usage schema for a `usage_parser` — so a user of
  any coding CLI can paste back a clean, content-free report. A default zero-launch **scan** reads
  on-disk conventions; opt-in `--probe` does a live capture in an ephemeral workspace. All output
  passes through one audited PII sanitizer (token counts and field names survive; paths, prose, and
  emails are redacted).
- **GitHub Copilot CLI profile.** Bundled `copilot` profile (Copilot CLI ≥ 2026-02): `-i`
  interactive launch, VS Code-compatible `Stop` hook, `--allow-all-tools` for unattended runs.
  Still pending live E2E and a `usage_parser` — `probe-adapter` captures the token schema to write
  one.

### Docs

- **Adapter authoring guide.** New [adapter authoring guide](docs/adapter-authoring-guide.md)
  walks through finalizing a CLI profile with `probe-adapter` (scan vs probe, the PII model, and
  the parser-writing loop); `probe-adapter` is added to both command references.

## [0.6.1] — 2026-06-20

### Added

- **Short run refs (Docker-style).** Every command that takes a run id (`status`, `attach`,
  `resume`, `resolve`, `stop`, `delete`, `archive`) now accepts a partial — the tail after the
  last `-` (e.g. `a1b2`, or as few chars as stay unique). Full ids still work; an ambiguous ref
  fails listing the candidates. New `bmad-auto list` (alias `ls`) prints each run/sweep with its
  short ref, type, and status.
- **Flexible `--story` selection.** `bmad-auto run --story` now takes more than the exact full
  key: an epic+number (`--epic 3 --story 1`, `--story 3-1`, or `--story 3.1`) or a slug fragment
  (`--story user-auth`). Full keys still work. Mismatches are caught before the run launches with a
  targeted error — no match, ambiguous slug, or matched-but-not-actionable.

## [0.6.0] — 2026-06-20

### Fixed

- **Rollback no longer wipes non-automator files.** A failed in-place attempt previously ran
  `git reset --hard` + a blanket `git clean -fd` over the whole checkout, which could delete a
  project's `_bmad-output/` and any other untracked files (only `.automator/` and two artifact
  subdirs were spared). The orchestrator now never runs a blanket `git clean`: it reverts the
  attempt's tracked changes and removes only the untracked files **that run created**, preserving
  pre-existing untracked files and the entire `_bmad-output/` tree.

### Changed

- **Auto-rollback is now opt-in (`[scm] rollback_on_failure`, default off).** With it off the
  orchestrator never touches your working tree on a failed attempt — it pauses the run with bold
  manual-recovery instructions (back up untracked files → `git reset --hard <baseline>` → restore).
  Turn it on for the safe automatic rollback above (it discards the attempt's uncommitted work, so
  it warns when it fires). Worktree isolation (`scm.isolation = "worktree"`) sidesteps this entirely.

## [0.5.1] — 2026-06-20

### Added

- **`bmad-auto clean` + `[cleanup]` retention.** Reclaims disk from concluded runs: tears down
  git worktrees a mid-flight stop orphaned (freeing each worktree's Unity `Library/` + MCP-server
  build — the main accumulation source), trims the heavy `worktrees/` tree from runs kept for
  history (they still list in the TUI), and archives/deletes runs past `[cleanup] run_retention`
  (default 10). Only finished/stopped runs are touched; `--keep`/`--dry-run`/`--retain`/`--hard`.
  Every `run`/`sweep` start auto-reconciles worktrees a prior **finished** run leaked
  (`auto_clean_on_finish`); the Unity plugin's new `post_run` hook clears the IvanMurzak MCP
  server's `/tmp/<company>/<product>/*.zip` downloads + truncates its editor log (`clean_tmp`).
- **Test Architect (TEA) plugin.** New bundled, opt-in `tea` plugin that wires the BMAD
  Test Architect Enterprise module into every run and sweep as advisory-by-default quality steps.
  Enable with `[plugins] enabled = ["tea"]`; it injects six TEA workflows — test-design, ATDD,
  automate (after dev) and trace, NFR, test-review (after review) — and fails fast at startup if
  TEA isn't installed (`npx bmad-method install` → Test Architect). Each step is individually
  toggleable; the three gate steps (`trace`/`nfr`/`review`) can be flipped to **blocking**, so a
  failing FAIL/CONCERNS gate escalates the unit for human review at commit instead of landing
  (fail-open: a missing or unparseable artifact never blocks). See the
  [TEA plugin guide](docs/tea-plugin-guide.md).
- **Settings-driven workflow `enabled` / `blocking`.** A plugin can let an operator disable a
  `[workflows.<name>]` step or flip its gate from policy via the `<name>_enabled` / `<name>_blocking`
  setting convention — no code, and byte-identical for plugins that don't declare them. Documented
  in the [plugin authoring guide](docs/plugin-authoring-guide.md#making-a-workflow-configurable).
- **Manage plugins from the TUI.** The settings screen (`g`) gains a **Plugins** section: a roster
  of every discovered plugin with an enable toggle (writing `[plugins] enabled`). A plugin's
  settings appear only once it is enabled — revealed live, hidden otherwise — so the form stays
  scannable; data-only plugins are always on. Saving now also runs each enabled plugin's coupling
  check (e.g. unity `editor_mode` ↔ `scm.isolation`), blocking an invalid combo at save time
  instead of mid-run.

- **MIT license + open-source community files.** The project is now MIT-licensed (© BMad Code, LLC)
  with a trademark notice, and ships `CONTRIBUTING`, `SECURITY`, `CODE_OF_CONDUCT`, and GitHub
  issue/PR templates as it becomes a first-class citizen in the BMAD org.

### Changed

- **Renamed the project and package to `bmad-auto`.** The distributable is now `bmad-auto`
  (install with `uv tool install 'bmad-auto[tui]'`) and the repo has moved to the BMAD org at
  [bmad-code-org/bmad-auto](https://github.com/bmad-code-org/bmad-auto). The CLI command, skills
  (`bmad-auto-*`), tmux sessions, and `BMAD_AUTO_*` env vars are unchanged. The separate legacy
  [bmad-automator](https://github.com/bmad-code-org/bmad-automator) project is unrelated and stays
  as-is. Re-run `uv tool upgrade bmad-auto --reinstall` to move an existing install onto the new name.

### Docs

- **Uninstall procedure.** The [setup guide](docs/setup-guide.md#uninstalling) now documents a
  full teardown — reclaim disk, remove `.automator/`, skills, hooks, and gitignore lines, then
  `uv tool uninstall`.

## [0.5.0] — 2026-06-20

### Added

- **Plugin system.** New `automator.plugins` package — a general extension layer: a `plugin.toml`
  manifest (metadata, declarative `[hooks.<stage>]`, a `[[settings]]` schema, optional in-process
  `[python]`), a folder-drop loader with builtin/project overlay (and a locked seam for
  entry-point packaging later), a trust allowlist (`[plugins] enabled` in `policy.toml`), and a
  registry that isolates plugin failures. A dropped `[python]` plugin is never imported unless
  explicitly enabled. Plugins can **observe, veto (defer/pause/skip), and mutate** a shared
  context at every run/sweep lifecycle stage via the hook bus, with an O(1) no-op fast path so
  zero-plugin runs stay byte-identical.
- **Dynamic, TOML-driven settings.** The settings schema moves to `data/settings/core.toml`
  (presentation only; defaults/options referenced from the `policy.py` dataclasses, never
  duplicated), the TUI settings screen renders from a registry, and an enabled plugin's
  `[[settings]]` appear under `[plugins.<name>]`.
- **Workflow plugins.** A plugin can declare a `[workflows.<name>]` table that injects an extra
  agent session at a lifecycle stage (`post_dev_phase` / `post_review_result`, run by the `dev` or
  `review` adapter); the prompt substitutes `{story_key}`/`{run_id}`/`{scripts}`. Non-blocking by
  default (advisory); a blocking workflow that fails routes through the normal defer path. Ships
  with a worked-example plugin (`examples/plugins/guardrails/`) exercising every extension point and
  a full [plugin-authoring guide](docs/plugin-authoring-guide.md).

### Changed

- **The game-engine layer is now a plugin.** Unity runs entirely through the plugin system, with
  no engine-specific code in the core loop. Enable it with `[plugins] enabled = ["unity"]` and
  configure it under `[plugins.unity]` (`editor_mode`, `mcp`, `unity_path`, `ready_timeout_sec`,
  `ready_grace_sec`). Behavior — the readiness gate, `per_worktree` Editor setup/teardown, MCP
  agent routing, and Library priming — is unchanged.

### Deprecated

- The `[engine]` policy block is deprecated in favor of `[plugins] enabled = ["unity"]` +
  `[plugins.unity]`. Existing `[engine]` configs still load but emit a deprecation warning and are
  folded onto the `unity` plugin; explicit `[plugins.unity]` values win. `[engine]` will be
  removed in a future release.

## [0.4.4] — 2026-06-19

### Fixed

- Unity `per_worktree`: auto-recover merge-back when a competing Editor leaks asset writes
  (`.cs.meta` GUIDs, asmdef edits) into the **main** checkout. Previously git refused the merge
  pre-flight because the target already held the unit's incoming files as dirt, escalating the unit
  spuriously. Merge-back now cleans only the leaked copies of this branch's incoming files (journaled
  as `merge-target-cleaned`); dirt outside the branch's path set still escalates as possible operator
  work, with a distinct message.
- Unity `per_worktree`: route **every** worktree CLI's MCP config at the worktree's Editor, not
  just the dev agent. When dev and review use different CLIs (e.g. `dev=claude`, `review=codex`),
  the review agent could read a main-repo-seeded config and route its asset writes into the main
  checkout. Each agent's config is now written to the deterministic per-path port and verified; a
  mismatch fails the setup hook (the unit defers) instead of leaking writes.

### Changed

- Unity engine plugin: pin the `unity-mcp-cli` verification stamp to **v0.81.1** (subcommand
  signatures re-checked; no call-site changes). Documents the new upstream **dev-control HTTP
  bridge** (dev-only, off by default, not wired) in the [Game Engine MCP guide](docs/game-engine-mcp-guide.md).

## [0.4.3] — 2026-06-18

### Added

- **Game-engine plugin layer (opt-in; Unity).** New `[engine]` policy section adapts the
  dev/sweep cycle to projects that drive a live engine Editor through an MCP (Unity via
  [IvanMurzak/Unity-MCP](https://github.com/IvanMurzak/Unity-MCP) or
  [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp)); off by default. Plugins ship
  like CLI profiles — bundled under `automator/data/engines/<name>/`, overridable in
  `.automator/engines/<name>/`. `editor_mode` couples to `[scm] isolation`: `shared` runs the
  agent in place on the operator's open Editor; `per_worktree` gives each unit its own managed
  Editor. A readiness gate blocks until the Editor + MCP report ready before each unit, deferring
  on timeout instead of starting against a half-open Editor.
- **Unity `per_worktree` mode.** Each unit runs in its own git worktree with a dedicated Editor:
  - Launches in local (Custom) mode — `bootstrap-local` plus `open --start-server true` so the
    Editor hosts its own per-path MCP server; this makes `wait-for-ready` a real readiness signal
    before any client connects. Connection knobs overridable via `BMAD_AUTO_UNITY_MCP_*`
    (`…_LOCAL=0` keeps the prior cloud launch).
  - Primes the worktree `Library` with a reflink/CoW copy of the warm main `Library`, so Unity
    reimports incrementally rather than cold — a cold import on a large project crashes the import
    workers (Burst `SIGFPE` writing `VirtualArtifacts`). Tunable via `BMAD_AUTO_UNITY_LIBRARY_SEED`
    and `…_SEED_MODE` (`reflink`|`copy`|`symlink`|`off`).
  - Teardown quits the Editor and reaps its child `gamedev-mcp-server` on completion or pause, so
    neither leaks across runs (a leaked server holds its port and breaks the next run).
  - Cold-launch grace via `[engine] ready_grace_sec`; MCP skill tree seeded into each worktree via
    `seed_globs`; `init` now gitignores `.automator/cache/`.
- **Worktree config seeding.** A fresh worktree checks out tracked files only, so a project's
  gitignored MCP/CLI configs (`.mcp.json`, `.claude/settings.json`, …) were missing — isolated
  sessions then timed out reaching their MCP and escalated as spurious spec errors. Each loaded
  adapter's configs are now copied in before launch, via new `[scm]` knobs `seed_adapter_defaults`
  (default on) and `worktree_seed` (extra paths). Both are in the TUI settings editor.
- **Game Engine settings in the TUI.** All six `[engine]` keys (`name`, `editor_mode`, `mcp`,
  `unity_path`, `ready_timeout_sec`, `ready_grace_sec`) are now editable in the settings editor
  (`g`) under a collapsible titled **Game Engine**; the `editor_mode` ↔ `[scm] isolation` coupling
  is validated on save. New authoring docs: [Writing a Game Engine plugin](docs/game-engine-plugin-guide.md)
  and [Writing a plugin for a specific Editor MCP](docs/game-engine-mcp-guide.md) (full
  `BMAD_AUTO_UNITY_*` env-var reference).

### Changed

- Default `limits.session_timeout_min` raised from 45 to 90 minutes — the old default cut off
  substantial units, especially MCP-driven Unity sessions where each Editor step is a slow
  round-trip. Override per project under `[limits]`.

### Fixed

- `bmad-auto cleanup` (and the TUI `c` action) no longer stops other projects' live runs. tmux
  sessions are global but were named only `bmad-auto-<run_id>`, so a run id absent from the current
  project looked like a prunable orphan and matched another project's active run. Sessions and
  windows are now stamped with their project (`@bmad_project`); cleanup prunes only the current
  project's, while still clearing true same-project orphans. Pre-existing untagged sessions are
  left untouched.

## [0.4.2] — 2026-06-17

### Fixed

- Answering sweep decisions over an attach now returns you to your terminal. After the
  last decision in a cycle was answered, the session previously stayed in the orchestrator
  window instead of handing control back. The sweep now returns the terminal as soon as the
  current cycle's decisions are answered and continues running bundles in the background.
  `bmad-auto attach` lands on the orchestrator window when a decision is pending and restores
  your previous session on exit.

## [0.4.1] — 2026-06-16

### Fixed

- Worktree isolation (`[scm] isolation = "worktree"`) now works. Isolated runs previously
  failed on the first session with `Unknown command: /bmad-auto-dev`. Worktrees are now
  created under the run directory (`.automator/runs/<run_id>/worktrees/<unit>`) instead of
  inside `.git/`, and each worktree is provisioned with the bundled skills and signal hook so
  project commands resolve correctly.

## [0.4.0] — 2026-06-16

First release with **opt-in git-worktree isolation** for runs and sweeps. The default is
unchanged: with no `[scm]` configuration, work happens in place on the checked-out branch
exactly as before (`isolation = "none"` is byte-for-byte identical to prior behavior).

### Added

- **Configurable `repo_root` + Workspace seam.** `_bmad/bmm/config.yaml` gains an optional
  `repo_root` key (defaults to the project dir) that decouples "where git work + code sessions
  happen" from "where run state lives." All code/git/artifact access now routes through a single
  `Workspace` indirection, so redirecting work into a worktree is a localized change rather than a
  sweep across the engine.
- **Worktree isolation** — `[scm] isolation = "worktree"` runs each story (and each sweep bundle)
  in its own `git worktree` on a dedicated `automator/<run_id>[/<story>]` branch cut from the
  target branch, then merges it back into the target **locally** (merge strategy `ff`, `merge`, or
  `squash`). The main checkout stays free while a run is in flight, and run state stays in the main
  repo's `.automator/` — never inside a worktree. Knobs: `branch_per` (`story` | `run`; `run`
  shares one branch across the run and forces `delete_branch = false`), `target_branch` (default =
  the branch checked out at run start; a configured branch is created/checked out in the main repo
  and never inside a worktree), `delete_branch`, and `keep_failed`.
- **Failed-unit forensics.** A deferred/escalated unit's full diff (tracked + untracked) is
  preserved to `run_dir/failed/<unit>/changes.patch`; with `keep_failed` (default) its worktree +
  branch stay mounted for inspection. `failed_diff_max_mb` (default `5`) caps the per-file size of
  untracked files in that patch — oversized files are skipped with a labelled marker — and
  `failed_diff_unlimited` lifts the cap entirely (logs a warning when active).
- **`commit_message_template`** — optional `[scm]` template (`{story_key}` / `{run_id}`
  substituted) used for story and sweep-bundle commits when set.
- The full `[scm]` section (isolation, `branch_per`, `target_branch`, `merge_strategy`,
  `delete_branch`, `keep_failed`, the failed-diff caps, and the commit template) is editable from
  the TUI settings screen. (`max_parallel` is omitted while it stays inert.)
- **Low-frame-rate TUI mode.** `bmad-auto tui --low-frame-rate` (or `[tui] low_frame_rate = true`,
  editable from the settings screen) caps Textual to 15fps and disables animations by setting
  `TEXTUAL_FPS` / `TEXTUAL_ANIMATIONS` before the app starts. Fixes the repaint tearing/garbage
  seen when driving the dashboard over a slow or high-latency link (SSH, Tailscale), where a 60fps
  update stream can't drain in time and partial frames paint over old ones. The setting takes
  effect the next time the TUI launches; an explicit `TEXTUAL_FPS` in the environment still wins.
- **git worktree / branch / merge / diff primitives** in `verify.py` (worktree add/remove/list/
  prune, `create_branch`, `merge_branch`, `capture_diff`, …), unit-tested in isolation.

### Changed

- Worktree-mode integration is always **serialized** — unit branches merge into the target one at
  a time. `max_parallel` exists as a validated knob but is clamped to `1` (inert) until internal
  parallel fan-out is built.
- Story spec paths are persisted **relative to the worktree** in `state.json`, so a kept-failed
  run's state stays portable if the worktree is later moved.
- The run reclaims its worktree scaffolding on clean completion (deliberately-kept failed/escalated
  worktrees are left in place and journalled so they can be found).
- **TUI settings editor now collapses every section by default.** Each policy section
  (`gates`, `limits`, `scm`, …) starts collapsed with a one-line description in its header, so the
  grown-large form scans at a glance — expand only the section you want to edit. `ctrl+e` toggles
  all sections open/closed at once.

### Fixed

- A detached HEAD or unborn repo no longer lands worktree merges on an unreferenced commit — the
  run pauses with a clear reason instead. A merge conflict against the target keeps the unit branch
  for manual merge and escalates; `capture_diff` now raises on a genuine `git` error (rather than
  silently truncating the patch) and `merge_branch` reports a failed abort/reset.
- **Editing settings no longer dirties the worktree for validation.** `worktree_clean()` (the
  pre-flight gate for `run`/`sweep`/`validate`) now ignores `.automator/policy.toml`, so saving a
  change in the settings editor no longer forces a commit of the config before the next command.
  Only that one file is exempt — the deferred-work ledger under `.automator/` still commits as
  before.

## [0.3.2] — 2026-06-15

### Added

- **Arrow-key navigation and Enter-to-edit on the settings screen.** Up/Down now move focus
  between fields (additive — Tab/Shift+Tab still work), and Enter activates the focused field
  by type: it opens a dropdown (`Select`), toggles a switch, or enters cursor-edit mode on the
  multi-line box (`TextArea`), where the box's own Up/Down then move the cursor; Escape leaves
  edit mode without leaving the screen. Plain text/number inputs stay editable on focus, so
  Enter is a no-op there. Implemented with priority bindings gated by `check_action` so an open
  dropdown or an editing TextArea keeps Up/Down, and Escape still pops the screen in nav mode.

### Fixed

- **Attaching to answer a deferred-work decision now returns you where you came from.**
  When a prompting sweep blocks on a decision (or you open a resolve session), pressing
  `a`/`R` switches a tmux client into the orchestrator's control window so you can answer
  there — but on exit it left you stranded in the control session on the parked
  exit-status prompt instead of back at the TUI. The control window now records where the
  attach came from and, once you press enter, returns you: it switches the client back to
  the TUI's own pane when the TUI runs inside tmux (i.e. your original session), or
  detaches the throwaway attach client so the suspended TUI resumes when it runs outside
  tmux. Windows nobody attached to interactively still park unchanged.

- **Empty optional numeric fields no longer flash a red "invalid" outline.** The start-run
  and start-sweep modals draw their numeric inputs (`epic`, `max stories`, `max bundles`)
  with `type="integer"`, which under Textual validates on blur and — with the default
  `valid_empty=False` — treats an empty string as invalid. Tabbing past a blank field that
  is explicitly optional ("blank for all", "blank for no limit") therefore tripped the red
  `$error` border. The inputs now pass `valid_empty=True`, matching the settings screen, so
  leaving them blank is accepted silently while a typed integer still validates.

### Changed

- **Clearer review toggle on the settings screen.** The `[review]` switch showed only the
  raw key `enabled`, with no hint about what it controls. It is now relabelled "separate
  review session" and carries a muted caption spelling out both states (ON: triple review
  runs in a dedicated 2nd session · OFF: quick-dev runs its own tri-review inline). The
  change is display-only — the config key and save logic are unchanged.

- **`bmad-auto-setup` now upgrades, not just installs.** Re-running the skill (or invoking
  it with `upgrade`) on an already-installed project is detected as an upgrade — it runs
  `uv tool upgrade bmad-automator --reinstall` (the `--reinstall` is required for a git
  source) and re-lays the per-project skills with `bmad-auto init --force-skills`, then
  reports the before → after version. Previously a re-run was treated as a config-only
  update: it left `--force-skills` off, so `init` silently skipped every existing skill
  dir and the project kept stale skills against the upgraded tool. Upgrade is detected from
  an existing `bauto` config section and/or a uv-managed `bmad-automator`, and the tool
  follows `main` by default with an offer to pin a release tag. Docs (README "Upgrading",
  `docs/setup-guide.md`) now describe the skill-driven upgrade alongside the manual ritual,
  and the stale `uv tool upgrade bmad-automator` hint (missing `--reinstall`) is corrected.

## [0.3.1] — 2026-06-14

Maintenance release. Also backfills the previously-undocumented `[0.3.0]` notes below.

### Changed

- `scripts/sync_version.py` now runs `uv lock` as part of the version stamp, so a
  version bump regenerates the pinned lock in one command. CI runs `uv sync --locked`,
  which fails the install step on a stale lock (hit while cutting 0.3.0); folding the
  relock into the stamp keeps a bump a single command. Idempotent, with a loud non-zero
  exit if `uv` is missing or the lock fails.

## [0.3.0] — 2026-06-14

First release carrying the optional review toggle.

### Added

- **Optional review pass** — new policy `[review] enabled` toggle (default `true`). When
  disabled, a run skips the separate fresh-context `bmad-auto-review` session: the dev pass
  runs quick-dev's own internal triple-review unattended and finalizes the story straight to
  `done` — one session per story instead of two, with verify commands still gating the
  commit. The flag flows to the dev session via `BMAD_AUTO_SKIP_REVIEW=1`; the dev skill (not
  the engine) writes the `done` status, preserving the engine-never-writes-status invariant.
  Global scope: also governs deferred-work sweep bundles. Exposed as a switch in the TUI
  settings screen.

### Changed

- **Install / upgrade docs** — the README install block now offers main-tracking vs.
  pinned-tag installs, and a new "Upgrading" section documents the two-step ritual
  (`uv tool upgrade --reinstall` — required for a git source — then re-lay per-project skills
  with `init --force-skills`). The `bmad-auto-setup` skill is corrected to use `--reinstall`
  (plain `uv tool upgrade` reuses the cached git commit and won't pull new code) and notes the
  skill re-lay step plus tag pinning.
- Regenerated `uv.lock` for the 0.3.0 version pin.

## [0.2.0] — 2026-06-14

First versioned release since the initial `0.1.0`. Consolidates everything built since then and
realigns the version across the Python package and the BMAD-module metadata (which had drifted to a
placeholder `1.0.0`). All version-bearing fields are now kept in sync by `scripts/sync_version.py`,
enforced in CI.

### Added

- **TUI dashboard** (`bmad-auto tui`) — live, read-only view of runs, the sprint tree, the
  deferred-work ledger, a per-story phase/token table, and tailing of the journal / pane log /
  ATTENTION file, plus an integrated launcher for new runs and an in-app policy editor.
- **Deferred-work sweeps** — `bmad-auto sweep` triages the ledger against the real codebase and
  runs full dev → review → verify → commit on actionable bundles; `--repeat` re-triages each cycle;
  `bmad-auto decisions` surfaces and pre-answers human decisions earlier sweeps left open.
- **Interactive escalation resolution** — `bmad-auto resolve <run-id>` opens a resolve agent to
  disambiguate a frozen spec on a CRITICAL escalation, then re-arms the story and resumes.
- **Multi-CLI / multi-agent support** — a generic tmux adapter driven by declarative TOML profiles,
  with built-in `claude` (default), `codex`, and `gemini` profiles and per-stage overrides
  (`[adapter.dev|review|triage]`) for client/model/extra args.
- **Run operations** — `stop`, `delete`, `archive`, and `cleanup` for tmux artifacts of finished or
  stopped runs.
- **Cost-weighted token budgeting** — per-story `max_tokens_per_story` using cache-read weighting.
- **Bundled skill module** — the `bmad-auto-*` skills ship inside the wheel and are laid down by
  `bmad-auto init` into `.claude/skills/` and/or `.agents/skills/`.

### Changed

- **BREAKING:** policy `[adapter]` no longer accepts the flat `model_dev` / `model_review` keys; use
  the `[adapter.dev]` / `[adapter.review]` / `[adapter.triage]` sections instead (a clear error
  points at the replacement).
- **BREAKING:** build system migrated from setuptools + pip to **hatchling + uv**. Install with
  `uv tool install "bmad-automator[tui] @ git+…"`; develop with `uv sync --all-extras`. All docs,
  CLI hints, the `bmad-auto-setup` skill, and the eval-runner Dockerfile now use uv.
- **BREAKING:** module layout renamed `module/` → `skills/`; the canonical skills live under
  `src/automator/data/skills/`.

### Fixed

- BMAD-method installer could not locate `module.yaml` for the `bauto` module
  (`collectAgentsFromModuleYaml` / `writeCentralConfig` warnings): restored a repo-root
  `module.yaml` descriptor so the installer's shallow lookup resolves the module again.
- Replaced stale `pip install` instructions across docs, CLI hints, the setup skill, the
  eval-runner Dockerfile, and the module greeting with their uv equivalents.

## [0.1.0]

- Initial release: deterministic dev → review → verify → commit orchestrator for the BMAD
  implementation phase, driven by a Python control loop with hook-based session transport and
  resumable on-disk run state.

[Unreleased]: https://github.com/bmad-code-org/bmad-loop/compare/v0.9.0...HEAD
[0.9.1]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.9.1
[0.9.0]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.9.0
[0.8.1]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.8.1
[0.8.0]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.8.0
[0.7.12]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.12
[0.7.11]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.11
[0.7.10]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.10
[0.7.9]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.9
[0.7.8]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.8
[0.7.7]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.7
[0.7.6]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.6
[0.7.5]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.5
[0.7.4]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.4
[0.7.3]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.3
[0.7.2]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.2
[0.7.1]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.1
[0.7.0]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.0
[0.6.4]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.6.4
[0.6.3]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.6.3
[0.6.2]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.6.2
[0.6.1]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.6.1
[0.6.0]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.6.0
