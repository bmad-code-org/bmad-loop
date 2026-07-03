# Changelog

All notable changes to `bmad-loop` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the project is pre-1.0,
breaking changes may land in a minor release.

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
