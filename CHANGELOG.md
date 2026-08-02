# Changelog

All notable changes to `bmad-loop` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the project is pre-1.0,
breaking changes may land in a minor release.

## [0.9.1] — 2026-08-01

Compatibility hotfix for the BMad Method's `bmad-dev-auto` → `bmad-build-auto` rename
(BMAD-METHOD#2651, first shipped in bmad-method 6.10.1-next.33) and the two upstream changes
that rode the same window. Both skill eras are supported; the rename itself needs no
`policy.toml` edit.

### Fixed

- **The dev primitive is now resolved on disk, so the upstream rename no longer breaks a
  project (#405).** `bmad-build-auto` is preferred and a marker-complete `bmad-dev-auto` is
  accepted, so `validate`/`run`/`sweep`/`resume` pass on either era. The forwarding shim
  upstream left behind is refused by a new `skills.base-shim` check — its migration prompt is
  interactive and would HALT an unattended session with nothing written to disk. The ok line
  and `validate --json` name the primitive that actually resolved, and worktree isolation
  copies the new skill directory. `[dev] skill` stays `bmad-dev-auto`: it is the adapter
  discriminator, not the invoked name.

- **Every session prompt spells the resolved primitive (#405).** Dev, review, repair, restore,
  stories dispatch and all three sweep bundle legs hardcoded `/bmad-dev-auto`, which
  post-rename dispatches the shim. The name is resolved per skill tree, so a run mixing
  `.claude/skills` and `.agents/skills` gets the right one per role, and the no-spec fallback
  result marker is read under both the `bmad-build-auto-result-*` and legacy prefixes.

- **`--dry-run` says when its preview is not runnable (#405).** `run`, `sweep` and stories dry
  runs returned before their own skill preflight, so a broken install still got a
  plausible-looking schedule — and post-rename the previewed `/bmad-dev-auto` is a _valid_
  command that would HALT on the shim. The preflight failures now print to stderr under a "NOT
  runnable as-is" banner. Exit code stays 0 and stdout is untouched: a dry run is a diagnostic,
  and rc 0 has always meant "the preview rendered", not "the project is ready".

- **The skills preflight no longer gates a triage-only CLI's skill tree (#405).** Every
  `skills.*` check asks a dev-primitive question — which primitive resolves, whether the three
  review hunters it invokes inline are installed, whether a renderer stub's script unit is
  whole — yet the tree list was built from all three adapter roles. So a `[adapter.triage]` of
  `gemini` beside a claude dev/review pair demanded the whole BMad Method module in
  `.agents/skills`, a tree whose only prompt (`/bmad-loop-sweep`) ships in this wheel and is
  laid down by `bmad-loop init`. The over-breadth is pre-existing and was already a hard block —
  0.9.0 emitted `skills.base-missing` as a problem over the same over-broad tree list and its run
  preflight refused on it — but the new renderer checks route through the same list, so 0.9.1
  added new ways for one to fire. The probe — and `validate`'s own copy of it, which had the same defect twice
  — is now scoped to the dev and review roles, the same set `Engine._worktree_profiles`
  provisions into a worktree, and the `skills.base` and `skills.stories-dispatch` ok lines
  report that narrower `trees` list. Single-CLI projects and dev/review splits across two CLIs
  are unaffected.

- **A worktree that could not be given its upstream skills now pauses instead of stalling
  (#405).** `provision_worktree` copies the BMad Method skills from the main repo behind a
  containment guard, and skipped anything resolving outside the repo — which is exactly what a
  skill tree symlinked to a shared machine-wide BMad install does. The run-start preflight stats
  through that symlink and passes, so an isolated run dispatched into a worktree holding none of
  its skills and every session stalled on `Unknown command` having written nothing. Provisioning
  now reports the skills it could not deliver through the existing `worktree-seed-skipped`
  journal channel, and the engine escalates and pauses before dispatch, naming them — the same
  environment-fault treatment the renderer surface already got, since the seed reads the same
  repo for every story. Unlike the renderer legs this carries no _renderer_-era condition: a
  missing skill stalls a session whether its `SKILL.md` renders or is inline. It is scoped to the
  primitive era the run actually dispatches, though — the skills list it reads names both eras
  because copying one that isn't there is free, but a leftover `bmad-dev-auto` shim no prompt
  spells is not a stall to pause over. The copy itself now merges per _file_, so a worktree that
  already holds part of a skill directory is completed rather than skipped whole, and the
  completeness check walks the repo's skill directory exactly the way the copy walks it,
  reporting every file the seed could not deliver, naming the file rather than the directory
  whenever the directory itself arrived (a wholly-absent skill still reports the coarse rel). A
  fixed list of required files would have covered three of the thirteen a real `bmad-build-auto`
  install carries — and would go stale the next time upstream renames a step file. Containment
  is checked per file too, so one skill file, or one whole sub-directory, symlinked to a shared
  install outside the repo is refused rather than read through, and reported rather than
  silently missing. A skill tree that is committed (symlink and all)
  is checked out into the worktree normally and is unaffected. Pre-existing since 0.7.0, which
  is where the containment-guarded seed loop first shipped; found while reviewing this release.

- **A renderer stub that cannot compose a prompt fails the preflight (#405).** Three new checks
  block `validate`/`run`/`sweep`/`resume`: `skills.dev-renderer`, when the resolved `SKILL.md`
  is the new renderer stub (BMAD-METHOD#2601) but its script unit is not whole —
  `_bmad/scripts/render_skill.py` or the `_bmad/scripts/config_utils.py` it imports at module
  scope; `skills.dev-renderer-config`, when a stub resolved but `_bmad/config.toml` — the
  renderer's one required config layer — is absent; and `skills.dev-renderer-sources`, when the
  skill's own render sources are short: no `workflow.md` for the renderer to compose from, or a
  `[[bmad-snapshot:…]]` token naming something the renderer will not load as a source. Every
  route ends in a
  session that Stops having written no spec, and every one of them is a fact about the install
  rather than the story, so every story after it does the same. A missing config exits `HALT:`;
  a missing entry document or snapshot target does too. A missing script or sibling loses even
  that line — `uv` fails before the renderer runs, or `ModuleNotFoundError` fires above its own
  guard — leaving only the stub's own instruction to report the output and halt. The sibling is
  required only when the installed `render_skill.py` actually imports it, and the source check
  asks what the install itself declares — save the `workflow.md` entry name, which upstream
  hardcodes too — so a later renderer that inlines the helper or
  reorganizes its step files is not refused. They block rather than warn because only a green is
  untrustworthy (uv on PATH is never probed) — a red is conclusive. The config check is emitted
  once per project and only when a stub actually resolved, so a pre-renderer install stays
  silent, and `--dry-run` names all three under its "NOT runnable as-is" banner. A fourth check,
  `skills.customize-legacy`, is a warning: it fires when a tree resolved to `bmad-build-auto`
  while an override still sits at `_bmad/custom/bmad-dev-auto[.user].toml`, where it no longer
  applies — the session still runs, just unstyled.

- **Worktree isolation carries the `_bmad/` config surface (#405).** The renderer-era primitive
  is handed the worktree as its project root and hard-fails when that root has no `_bmad/` —
  there is no walk-up — so on a project that gitignores it (most do, this one included) every
  isolated session HALTed with nothing written. `provision_worktree` now merge-copies the repo's
  `_bmad/` per file, copy-when-absent, so a checkout that commits it keeps every tracked file and
  only the gitignored layers are filled in. `_bmad/scripts/` is seeded whole rather than curated
  because `render_skill.py` bare-imports its sibling `config_utils`, and a seed that comes up
  short — the realistic trigger is a symlinked `_bmad/`, how a shared BMad install is wired —
  pauses the run _when the resolved dev primitive is a renderer stub_. Every story would drive
  the same incomplete seed into the same result-less Stop, so it escalates once with the
  worktree left mounted for inspection, rather than dispatching the whole backlog and reporting
  `0 done`. `_bmad/config.toml` is checked the same way and named separately in the escalation:
  a repo that centralises only that file behind a symlink seeds a complete `_bmad/scripts/` and
  no config at all, which was silent from every angle — the repo-side preflight follows the
  symlink and passes. On a pre-BMAD-METHOD#2601 inline `SKILL.md` nothing reads either path,
  so the short seed costs the run nothing and stays an ordinary journaled report. Neither
  sentinel can be forged: a `worktree_seed` entry spelling one is dropped rather than reported,
  so a checkout that commits its whole `_bmad/` is never paused on a healthy worktree.
  `_bmad/render/` is never seeded and is git-excluded inside the worktree, so the renderer's
  in-session rewrite of it cannot be swept into a story commit; `init` now gitignores it as
  well, which is the only protection under the default `isolation = "none"`.

- **Worktree provisioning survives a filesystem it cannot fully read (#405).** The `_bmad/` seed
  and the detector that checks it now share the skill seed's walk: `rglob` does not descend a
  symlinked _sub_-directory and the detector mirrored the same `rglob`, so a symlinked
  `_bmad/scripts/lib` one level in was seeded as nothing and reported as complete — the
  under-seed and the check that should have caught it were wrong together. Pointing inside the
  repo it is now seeded; pointing outside it is still dropped by the containment guard, but the
  gate can see it.
  That walk descends symlinks, so it carries a branch-local cycle guard — without one,
  `_bmad/scripts/loop -> ..` is copied at every depth until the kernel's `ELOOP` stops it, a risk
  the old `rglob` never ran because it refused the descent to begin with. Per file, a source the
  walk cannot see inside, an occupied destination and a failed read or write are each a skip the
  skills gate then names — a source that could never be delivered at all, a dangling link or a
  FIFO, is still dropped by both halves in silence, and `_bmad/`'s own report stays the two
  renderer sentinels — rather than an exception out of the seed: `provision_worktree` runs inside
  `Engine._run_isolated` with no `try` around it, so one unreadable skill file ended the whole run
  with a crash dump where a named, resumable escalation belonged. A dangling _destination_ symlink
  now counts as occupied: writing through one landed the bytes at the link's target and left the
  gate reading green through the now-live link. Every probe the skills and `_bmad/` seeds make is
  now total — and, the part a `try` alone does not buy, reads the same on every supported
  interpreter. `is_file()`/`exists()`/`is_dir()` raise on an unsearchable parent through 3.13 and
  answer `False` on 3.14, where `False` is the silent drop and not the cautious reading, so a
  `False` from `is_dir()` is re-asked of `stat()`, which still raises there. Two faults reach that
  split: a directory readable but _not searchable_ (mode `0o444`, where listing succeeds and
  stat-ing each child does not) and an unreadable skill tree one level above the walk's own root.
  What the report can say degrades with what the filesystem gives up: a directory that cannot be
  listed is named once, and one that can be listed but not stat'd through is named file by file.
  The eager copies are unchanged and can still raise — user-authored `worktree_seed`, the
  adapter-default and plugin seeds beside it, and the per-CLI hook-config write.

- **A seed the worktree never got is now reported, and the `_bmad/` shield writes one exclude line,
  not two (#405).** The two explicit seed loops drop an entry with a bare `continue` when the
  resolve-and-contain guard refuses it, so a config the repo carries as a symlink _out_ of itself —
  a dotfile-managed `.claude/settings.json`, a shared MCP config — delivered nothing and said
  nothing: `worktree-seed-skipped` reports only the opposite case, an entry whose destination
  already exists. Provisioning's result is now re-probed against the repo and the drops journaled
  as `worktree-seed-dropped`, asking the two trees on disk rather than the loops' bookkeeping, so a
  drop by any future path is caught the same way. Journaled and never escalated, unlike the
  `_bmad/` and skills gates beside it: those name files the orchestrator dispatches or the renderer
  HALTs on, while a seed entry is arbitrary user config whose canonical trigger is an ordinary
  working setup, so pausing would refuse every run of such a project over a guard doing its job. A
  broken link stays silent in both halves, as it already does for the skills gate; a FIFO does
  not — these loops gate on `exists()`, so the copy reaches it and raises. The eager copies are
  still unchanged: a copy that _fails_ raises rather than landing here.
  `/_bmad/render/` goes into `.git/info/exclude` only when the blanket `/_bmad` this release also
  adds is not — that file lives under the git common dir a linked worktree shares with the main
  checkout and nothing here ever prunes it, and `/_bmad` prunes the directory before git descends,
  so a sibling line would be permanent state in the operator's own repo that git never consults. `worktree-opened` is
  journaled as soon as the worktree is mounted rather than after every provisioning gate has
  passed, so the escalations that leave a half-provisioned worktree mounted for inspection now say
  where it is.

- **Seeding never writes _through_ a symlink, and a hook config is no longer its own alibi
  (#405).** Both explicit seed loops resolved the destination and then used the resolved path for
  the occupancy probe, the `mkdir` and the copy. `Path.resolve()` is non-strict, so a dangling
  link answers for its target: `exists()` reported the slot free and the copy landed at the link's
  target instead — an unrequested path inside the worktree that the exclude does not name and the
  unit's `git add -A` would stage into the story branch. `git worktree add` produces exactly that
  state for a tracked symlink whose target is untracked, so it arrives on a project's _first_
  story. Both loops now compare the raw destination against the resolved one and refuse when they
  differ, each with its own guard — they share no code — and strictly _after_ the
  copy-when-absent skip arm, since a live symlinked destination differs too and hoisting the
  refusal would turn that ordinary no-op into a silent drop. What is refused is named afterwards
  by `worktree-seed-dropped`, as every other refusal in those loops already is. The drop report in
  turn stops trusting the destination for the one rel that cannot answer for itself: the per-CLI
  hook step writes `profile.hooks.config_path` after both seed loops, so a config the loops
  dropped is answered for by the hook's own bytes — and for gemini, copilot and antigravity that
  path is the profile's _only_ default seed, so the false green was the gate's entire answer.
  Those rels are now asked whether the source escapes the repo, the destination is a symlink, or
  the destination escapes the worktree, replacing the existence probe rather than adding to it so
  a destination that trips two of them is still named once. A live symlinked destination for a
  hook config is consequently named here as well as in `worktree-seed-skipped`; both facts are
  true at once and this channel journals rather than pauses. Two destination-containment guards —
  the `_bmad/` merge's and the skill merge's — also gain their first tests: their existing
  siblings arm the source guard and the occupancy check instead, so a live symlinked _directory_
  left them unwitnessed.

- **`isolation = "worktree"` is refused when `repo_root` is overridden (#405).** A project whose
  `_bmad/bmm/config.yaml` sets `repo_root` decouples the git root from the project dir — the
  documented monorepo knob — but worktree provisioning reads `repo_root` for every surface it
  seeds off disk (the upstream skill trees, `_bmad/` and its `_bmad/custom/` overrides, every
  `seed_files`/`seed_globs` entry) and bakes the absolute hook-relay path from it into the
  worktree's hook config, while `init`, `validate` and the run
  preflight write and probe them under `project`. `load_paths` requires
  `project/_bmad/bmm/config.yaml`, so that surface is under `project` by definition and
  `repo_root/_bmad/` generally does not exist: the preflight approved a surface the isolated run
  never received, every seed-completeness gate above went inert rather than firing, and each
  story ended as a result-less Stop with nothing naming the cause. `validate` now reports the
  pair as a `policy.isolation-repo-root` problem, and `run`, `sweep`, `resume`, `resolve`'s
  re-arm and the auto-triggered child sweep refuse to start, naming both remediations — drop the
  `repo_root` override, or set `isolation = "none"`. The child sweep raises rather than declining
  quietly, so a mid-run flip is journaled as `sweep-auto-failed` instead of being recorded as a
  sweep that ran. The dry-run banner lists the refusal ahead of the skill
  ones it aborts before, and the TUI's pre-launch guard mirrors it — ahead of its own clean-tree
  gate, as the CLI does — so the operator gets a toast rather than a pane that dies.
  Pre-existing since worktree isolation shipped. Plumbing
  `project` through provisioning instead — which would make the combination work rather than
  refuse it — stays open as #414.

- **`validate` warns when the renderer's output is already committed (#405).** The two shields
  0.9.1 adds — the `_bmad/render/` line `init` writes into `.gitignore`, and the `/_bmad/render/`
  line provisioning writes into the repo's local git exclude — only help going forward, because a
  tracked path ignores both entirely. A project that committed `_bmad/render/` earlier keeps
  churning it into every story commit, gaining a snapshot directory per machine, per checkout path
  and per upstream renderer bump, since the path is keyed on a hash of the absolute project root
  plus a generation hash over the renderer, its sources and the resolved config. A new
  `git.render-tracked` warning names the one-time
  `git rm -r --cached _bmad/render`. A warning and not a problem — nothing about a tracked
  `_bmad/render/` stops a session — and outside a git repo it stays quiet rather than fabricating
  an ok (#409). All three sites now spell the path from one `install.RENDER_DIR_REL`, so the
  probe cannot drift away from the shields it reports on.

- **`verify`'s emptiness probes no longer read their answer out of git's stderr (#405).**
  `worktree_clean` — and `path_tracked`, added earlier in this release — tested stdout and stderr
  merged, but `ls-files` and
  `status` exit 0 while still writing to stderr — a `core.fsmonitor` hook that cannot exec, an
  unknown `core.fsyncMethod` — and that chatter is indistinguishable from an index entry or a
  porcelain line. Both answers were silently inverted: the new `git.render-tracked` check would
  have told operators to `git rm -r --cached` a path that was never committed, and a checkout with
  nothing in it was reported dirty, blocking `run`, `sweep` and `validate` outright. Both now read
  stdout alone; the error paths keep the merge, where stderr is the informative half.

- **An undecodable `policy.toml` or `config.yaml` is reported, not a traceback (#405).**
  `read_text` raises `UnicodeDecodeError` on a file saved as UTF-16 or latin-1, and that is a
  `ValueError`, not an `OSError` — so it escaped every `except (PolicyError, OSError)` handler in
  the codebase, which are precisely the ones whose job is to degrade to defaults. The TUI could
  not open its dashboard, and `_configure_mux` runs before argument dispatch on every command, so
  the CLI died at startup on the raw codec message its catch-all prints, naming no file, instead
  of any of its named findings. `policy.load` and `bmadconfig.load_paths` now convert it to their
  own typed errors, which fixes every caller at once; `validate` reports the file by name.

- **Deferred review findings are harvested out of the spec's frontmatter (#405).**
  BMAD-METHOD#2640 moved `defer`-triaged findings from `deferred-work.md` into the spec's own
  `deferred:` list, silently starving the sweep pipeline. A successful dev or review session
  now files each finding into the ledger with its `location` and `severity`, journals
  `spec-deferrals-harvested`, and dedups on a fingerprint of the summary and location so a
  _fixable_ retry, a resume replay or a second review pass never re-files one — including after a
  sweep has already marked it done. (A NON-fixable retry re-files deliberately: the bullet below
  reverts its entry along with the attempt, and the next attempt harvests it fresh.) Malformed items do not block their well-formed siblings: the loss is
  journaled and filed as one aggregated ledger entry. The spec's frontmatter is never rewritten,
  and the harvest keys on content rather than the installed skill name, so it works on both eras.
  The review prompt no longer also asks the session to file the finding itself: that produced two
  entries per finding which could never dedup, since an agent-written one carries neither the
  fingerprinted `origin:` nor a `source_spec:` line. It stays neutral rather than banning ledger
  edits outright — on a pre-BMAD-METHOD#2640 skill there is no frontmatter to harvest and the
  session's own append is the finding's only record — and still forbids rewriting existing entries.

- **A harvested deferral is reverted when its attempt rolls back (#405).** The harvest keys on
  the spec's status and runs before the artifact gate, so a session that finalized its spec and
  then failed a non-fixable check (a `baseline_revision` mismatch is the canonical trigger) left
  its ledger entry behind, describing code the rollback had just discarded. The reset alone does
  not remove it: the ledger sits under a protected artifact folder, which `_safe_reset`'s `keep`
  shields from the untracked-file cleanup. The dev phase now snapshots the ledger before the
  harvest and restores it around the rollback — unlinking the file when the harvest created it,
  and on the stop-and-wait path too, which raises. Lossless, because the spec's `deferred:`
  frontmatter is never mutated and the next attempt re-harvests from it. The snapshot is taken
  ahead of every engine-side ledger write in that window rather than immediately ahead of the
  harvest, so one growing inside the status reconcile or the state sync is covered by
  construction. And it is persisted with the attempt rather than held in a local, so a
  host death between the harvest and the rollback no longer loses it: the replayed attempt
  writes back the same bytes the live path would have. That replaces a presence bit which could
  only say whether a ledger existed at the attempt baseline — enough to delete one the harvest
  had created, never enough to restore one that was already there, so a pre-existing _untracked_
  or _gitignored_ ledger kept the dead attempt's finding for good. Nothing else reverts one:
  `reset --hard` skips ignored paths, there is no `git clean`, and the artifacts dir is
  `keep`-shielded besides. The restore never unlinks a ledger git tracks — that one is the reset's
  to restore, and deleting it would hand the next attempt's `git add -A` a deletion to commit. The
  snapshot is armed at the attempt's pre-harvest save and cleared once that attempt's decision is
  acted on, so a finished story carries no copy of the ledger in `state.json`. The clear on the
  PROCEED path persists immediately, because a hard kill there resumes straight into the review
  leg and would otherwise strand that copy on a task that is already terminal; the retry site
  deliberately does not, since the same kill replays that attempt and it still wants the
  snapshot — except when that retry pauses, which the next bullet covers. A defer still
  keeps its harvested entries: on the in-place path `_stash_deferred_artifacts` has already moved
  the spec out of the artifacts dir, so the ledger entry is the finding's only surviving record.

- **The pre-harvest snapshot is spent by a stop-and-wait pause instead of riding it (#405).**
  `rollback_on_failure = off` is the default and does not roll back: it hands the tree to a human
  and raises. The window that opens is not a crash window but a human-scale one — the operator
  reads the notice, inspects the tree, and may well append to the deferred-work ledger before
  typing `resume`. The armed snapshot used to survive that wait in `state.json`, and the resume,
  which replays the same attempt, wrote those stale bytes back over whatever the operator had
  done: an unlink when no ledger existed at the attempt baseline, and otherwise an overwrite,
  which has no tracked-file guard, so a committed ledger was rewritten too. Nothing spent the arm
  either, so it happened again on every subsequent resume. The pause leg now disarms in the same
  `finally` that restores, strictly after it, and persists that immediately — the point is the
  value the resume reads off disk. The replayed attempt then re-arms from the tree as the
  operator left it, so its own harvest has something to revert to if it fails in turn. Replays
  that never reached the arm re-arm the same way rather than proceeding blind, which retires the
  `ledger-snapshot-missing` journal line for that case.

- **A harvest is reverted even when the ledger lives outside the repo (#405).**
  `implementation_artifacts` may be configured out of tree — `ProjectPaths.rebased` keeps such a
  dir put on purpose, as shared rather than per-checkout. The restore classified any ledger
  outside the workspace as git's and left it alone, which is backwards: `reset --hard` runs in
  the workspace and cannot reach a path outside it, so it restored nothing and the harvest's file
  is a creation of the orchestrator's own. Every harvest revert on such a project was therefore a
  silent no-op, and the entry outlived the code it described — open in the shared ledger, and
  swept later as if that work still existed. It now reads as "not git's", on both retry legs and
  under either isolation mode. The containment test is re-asked with both sides resolved before
  that answer authorizes a delete, since a workspace root reached through a symlink is not
  lexically a prefix of the ledger's real path; an `OSError` there degrades to leaving the file
  alone, like every other probe in that `finally`.

- **The harvest's own ledger write is no longer the session's proof of work (#405).** The harvest
  runs four statements above the dev artifact gate, and that gate deliberately does not exclude
  the ledger — a story whose whole authorized scope is ledger reconciliation has to register as
  real work. So the gate could not tell the orchestrator's write from the session's: a session
  that finalized its spec, changed no code and recorded one `deferred:` finding proceeded to done
  on the strength of the line the engine had just written for it. The gate now excludes the ledger
  on exactly the attempts whose harvest filed into it, keyed on a flag persisted with the attempt
  rather than re-derived — a crash replay re-runs the harvest, which dedupes against the dead
  attempt's entries and so reports filing nothing while those entries are still in the tree.
  Narrow by construction: a session's own ledger edit still counts as work on every attempt that
  did not harvest, and the flag is cleared per attempt, so a later attempt is never charged for an
  earlier one's harvest. All three dev legs pass it through — sprint story, sweep bundle, and
  folder+id stories. The bundle leg is the costlier one, since an attempt let through there marks
  real ids `done` and `open_ids` re-bundles only `open` entries.

- **…and a session's own ledger edit on that same attempt still is (#405).** That exclusion is
  path-granular — it hides the whole ledger relpath, not the harvest's lines — so on an attempt
  where both authors wrote, the session's edit went out with the orchestrator's. A
  ledger-reconciliation story that also recorded one `deferred:` finding therefore read as "no
  changes since baseline", and the non-fixable retry that follows _pauses the whole run_ under the
  default `scm.rollback_on_failure = false`. The gate now stands down whenever the ledger had
  already moved off the dev phase's baseline by the time that attempt's pre-harvest snapshot was
  armed: its diff is then provably not the harvest's alone, and the gate judges the tree in full —
  which is the conservative direction, since it sees more of the diff rather than less. The
  baseline is a digest persisted beside `baseline_commit`, and persisting it is the point: a
  resumed dev phase deliberately does not re-capture its baselines, so a phase-local reference
  would be absent for every attempt after a crash-resume and the mask would survive there.
  Introduced earlier in this same release.

- **A sweep bundle's ledger closes are withheld until its attempt is accepted (#405).** The
  orchestrator marks a bundle's deferred-work ids `done` itself, and did so above the artifact
  gate — so an attempt that finalized its spec and then failed a non-fixable check was discarded
  with the ledger already claiming its work resolved. A surviving close is worse than a surviving
  finding: `open_ids` only ever re-bundles open entries, and the one reopen — `mark_open`, which
  undoes only a close its own caller wrote — is not a general one, so no
  later sweep looks at that id again and the work is silently lost rather than mis-recorded. On
  the retry leg the dev phase's ledger snapshot reverted it; on the DEFER terminus nothing in the
  dev phase could, because `_defer` takes its own snapshot after the close and writes it back
  over its own reset — deliberately, so review-found deferrals survive a defer. The close now
  runs below the gate, for a PROCEED decision only, which also excludes a CRITICAL escalation
  (it preempts even a passing outcome) and a failing `[verify] commands` run. What remains after
  acceptance is covered by the bullet below rather than by a revert. One deliberate consequence: that write no longer counts toward the gate's
  proof-of-work diff, so a bundle session which changed no code now fails the gate instead of
  passing on the orchestrator's own bookkeeping.

- **A review-leg defer takes the bundle's ledger closes back (#405).** The bullet above stops a
  DISCARDED dev attempt from closing anything; a bundle accepted at dev that then defers in
  review is a different shape. There the closes were correct when written and are required —
  `verify_review_bundle` gates on them — and only the later failure makes them wrong. `_defer`
  then writes its own post-close ledger snapshot back over the `reset --hard` that had reverted
  them, deliberately, since a harvested finding's ledger entry is its only surviving record once
  the spec is stashed. But the restore replays the whole file, so the closes rode it too and
  named code that no longer existed — on every route into a post-acceptance defer: review budget
  exhausted, the repair phase exhausted after a clean review, the budget rescue's own verify
  failing, `review.enabled = false` failing at its gate, and a blocking workflow deferring from
  `post_dev_phase` / `post_review_result` / `pre_commit_gate`. The defer now re-opens the ids it
  closed itself. `deferredwork.mark_open` is the undo of one specific `mark_done`, not a general
  reopen: it refuses any entry that is not closed carrying exactly the resolution note the caller
  wrote, so a close from an earlier sweep, from the legacy path where the session edits the
  ledger, or from a human is never revoked — and the round trip restores what the close changed,
  character for character. Unchanged on the
  stop-and-wait path (`rollback_on_failure = off` keeps the tree, so there is nothing to undo)
  and under worktree isolation (those closes live in the unit's own worktree, dropped unmerged).

- **Three small contracts brought back into line (#405).** A ledger entry filed with no file:line
  now carries `location: n/a` rather than no `location:` line: `deferred-work-format.md` has
  always specified that value, and of the fields an entry is created with, `severity:` is the
  only one it calls optional — so the orchestrator's own writer was the one producer disagreeing
  with the format the sweep skill reads. The harvest's 200-character clamp can land on a word
  break; the value is now stripped where it is produced rather than where it is written, so the
  `origin:` dedup key and the `location:` line stay derived from the same string. Entries already
  on disk without the line stay valid — read an absent `location:` as `n/a`. A plugin workflow's
  completion-marker filename is now resolved on the skill tree of the adapter its own `role` runs
  on, not always the dev one, so a run mixing `.claude/skills` and `.agents/skills` at different
  upstream eras no longer spells a review session's marker after a primitive only the dev tree
  carries. That one is the two halves agreeing rather than a repair: read-back was never at risk,
  because both prefixes are matched unconditionally. And `diagnose` now pseudonymizes the `spec`
  journal field, which is the customer's feature name and was shipping verbatim — the egress
  backstop could only ever rescue the ones that happened to embed the story key. The value is
  reduced to its basename first: the harvest kinds journal a bare filename while the reconcile
  kinds journal an absolute path, so aliasing it raw would have given one spec two aliases in a
  dump and parked a home path in the local `--legend` file.

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

[0.9.1]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.9.1
[0.9.0]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.9.0
[0.8.1]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.8.1
[0.8.0]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.8.0
[0.7.12]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.12
[0.7.11]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.11
[0.7.9]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.9
[0.7.7]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.7
[0.7.6]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.6
[0.7.5]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.5
[0.7.4]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.4
[0.7.3]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.3
[0.7.2]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.2
[0.7.0]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.7.0
[0.6.4]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.6.4
[0.6.3]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.6.3
[0.6.2]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.6.2
[0.6.1]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.6.1
[0.6.0]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.6.0
[0.5.0]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.5.0
[0.4.4]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.4.4
[0.4.3]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.4.3
[0.4.2]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.4.2
[0.4.1]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.4.1
[0.4.0]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.4.0
[0.3.2]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.3.2
[0.3.1]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.3.1
[0.3.0]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.3.0
[0.2.0]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.2.0
[0.1.0]: https://github.com/bmad-code-org/bmad-auto/releases/tag/v0.1.0
