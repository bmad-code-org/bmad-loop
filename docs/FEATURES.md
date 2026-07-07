# bmad-loop — Features & Functionality

For BMAD users who have run `bmad-sprint-planning` and have a `sprint-status.yaml` full of `ready-for-dev` stories. This is what the tool actually does and the problem each capability addresses.

See [README.md](../README.md) for the narrative overview and [setup-guide.md](setup-guide.md) for installation.

---

## Capability matrix (feature → problem addressed)

| Capability                           | What it does                                                                                                                                                                         | Problem it addresses                                                                      |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| Deterministic control loop           | Story selection, retries, gates, completion checks run in plain Python                                                                                                               | LLM-as-orchestrator is nondeterministic, hard to debug, and costs tokens for control flow |
| Dual planning pipelines              | Same loop from either `sprint-status.yaml` (sprint mode, default) or a typed `stories.yaml` dispatched by folder+id (stories mode, opt-in)                                           | Sprint boards need `bmad-sprint-planning`; a `bmad-spec` Story Breakdown has no board     |
| Per-story human checkpoints          | Stories-mode `spec_checkpoint` pauses to review the plan before code; `done_checkpoint` pauses after the commit; both independent, both surfaced in the TUI                          | Coarse run-global gates can't ask for a plan review on _this_ story only                  |
| Trust-nothing verification           | Checks on-disk artifacts (spec status, baseline-commit match, non-empty diff, sprint sync) + runs your test/lint commands before commit                                              | Agents claim success without working code; broken builds slip through                     |
| Fresh-context adversarial review     | Dev and review are separate sessions; review uses 2 parallel layers (Blind Hunter / Edge Case Hunter)                                                                                | Self-review anchoring bias; implementer marks own work correct                            |
| Hook-based transport                 | Coding-agent hooks write structured event files; skills write `result.json`                                                                                                          | Brittle terminal pane-scraping                                                            |
| Resumable state machine              | Every run is on-disk state, resumable after gate/escalation/crash                                                                                                                    | Long unattended runs lost to interruptions                                                |
| Plateau-defer                        | Stuck stories are skipped, stashed, and the run continues                                                                                                                            | One unconvergeable story blocking a whole sprint                                          |
| Typed escalations + resolve workflow | CRITICAL pauses + notifies; interactive resolve agent re-arms the story                                                                                                              | Ambiguous specs silently producing wrong code                                             |
| Deferred-work sweeps                 | Triages an append-only ledger against real code, bundles + executes                                                                                                                  | Split-off goals and review findings get lost                                              |
| Multi-CLI adapter + profiles         | Generic driver runs claude/codex/gemini; per-stage overrides; TOML profiles; transport + process-lifecycle + hook-interpreter behind a pluggable OS-seam registry (tmux/POSIX today) | Vendor lock-in; no way to mix models per stage; future non-tmux/Windows transport         |
| Cost-weighted token budgets          | Per-story budget counts cache reads at ~0.1x; raw totals displayed                                                                                                                   | Naive token caps misjudge real cost (cache reads dominate)                                |
| Non-invasive skill forks             | Drives its own `bmad-loop-*` skill forks; reads `sprint-status.yaml` only                                                                                                            | Modifying a user's standard BMAD install                                                  |
| Read-only TUI + launcher             | Live dashboard over run-dir artifacts; launches detached runs                                                                                                                        | No visibility into what an unattended run is doing                                        |
| Git worktree isolation (opt-in)      | Each unit runs in its own worktree/branch (seeded with the adapters' gitignored MCP/CLI configs), merging back into the target locally; failed units kept for inspection             | A long unattended run mutating the working tree you're actively using                     |

---

## Full feature list

### Core orchestration loop

- Automated per-story pipeline: `dev → verify → review → verify → commit`, end-to-end, no human in the loop.
- Deterministic control flow in plain Python — story selection, retry budgets, gate checks, and completion checks are code, not an LLM session.
- Reads `sprint-status.yaml` as the single source of truth (owned by BMAD skills; orchestrator only reads it); selects the next `ready-for-dev` story; advances by epic/story.
- Scoping flags: `--epic N`, `--story KEY`, `--max-stories N`, `--dry-run` (prints the plan, spawns nothing).

### Spec + implementation (dev stage)

- Drives the upstream `bmad-dev-auto` skill (unmodified) in a fresh tmux session: it plans a 1.5–4k-token spec, auto-approves it, implements, and self-finalizes the spec; the orchestrator syncs `sprint-status` and synthesizes `result.json` from the spec the skill leaves on disk.
- Spec-only contract between stages — review consumes the frozen spec, not the dev session's context.

### Verification (trust-nothing gate)

- After each session, checks on-disk artifacts before proceeding: spec frontmatter status, independent baseline-commit match (an LLM-lie detector), non-empty diff, sprint-status sync.
- Runs _your_ commands (`[verify].commands`, e.g. `pytest -q`, `ruff check .`) — a broken build never reaches review or commit.

### Adversarial review (review stage)

- The follow-up review is a re-invocation of `bmad-dev-auto` on the `done` spec — a fresh-context session with no anchoring bias from the implementer (BMAD-METHOD#2508 routes a `done` spec to a fresh step-04 review pass), so there is no separate review skill.
- Two parallel adversarial layers (Blind Hunter / Edge Case Hunter) → verify findings against code → triage → auto-apply patches → log → defer ambiguity → commit.
- Bounded review loop (`limits.max_review_cycles`, default 3 cycles); done when the pass finishes `done` and no longer recommends a follow-up. This bound is also the oscillation guard for skill-recommended follow-up review.
- Optional (`[review].enabled`, default `true`): set `false` to skip the follow-up review session. The dev pass's own inline review (same two layers, in-context) is then the only review and it finalizes the story to `done` — one session per story instead of two. Verify commands still gate the commit. Applies to story runs and deferred-work sweeps alike.
- Trigger (`[review].trigger`, default `recommended`): when review is enabled, decides _when_ the follow-up pass runs. `recommended` runs it only when `bmad-dev-auto` sets `followup_review_recommended` on a `done` spec (it self-reviews inline and flags an independent pass only when its review-driven changes were significant — BMAD-METHOD#2505). `always` runs it on every story (pre-0.7.0 behavior).

### Failure handling & resilience

- Bounded dev retries (default 2): verify-failures keep the tree and feed the failing output to the next session via `--feedback`; other failures roll back to baseline.
- Plateau-defer: when review won't converge, the story is skipped, the spec stashed into the run dir, deferred-work preserved, the run continues.
- Typed escalations: `CRITICAL` pauses the run + notifies (desktop + `ATTENTION` file); `PREFERENCE` is journaled and continues.
- CRITICAL resolution: `bmad-loop resolve <run-id>` opens an interactive resolve agent seeded with the escalation + frozen spec; you disambiguate, it re-arms the story (`escalated → pending`, spec reset to `ready-for-dev`) and resumes. `--no-interactive` skips to re-arm if you fixed the spec yourself.

### Git worktree isolation (opt-in)

- Off by default (`[scm] isolation = "none"` — work in place on the checked-out branch, byte-for-byte the prior behavior). Set `isolation = "worktree"` and each story (and each sweep bundle) runs in its own `git worktree` on a `bmad-loop/<run_id>[/<story>]` branch cut from the target branch, then merges back **locally** — the main checkout stays free while a run is in flight.
- Merge knobs: `merge_strategy` (`ff` / `merge` / `squash`), `target_branch` (default = branch checked out at run start; created if missing — a detached HEAD or unborn repo pauses the run instead of merging onto an unreferenced commit), `branch_per` (`story` or a shared `run` branch; `run` forces `delete_branch = false`), and `delete_branch`.
- Failed-unit forensics: a deferred/escalated unit's worktree + branch stay mounted (`keep_failed`, default on) and its full diff is preserved to `run_dir/failed/<unit>/changes.patch`; `failed_diff_max_mb` caps per-file untracked-file size (oversized skipped with a marker), `failed_diff_unlimited` lifts the cap.
- Config seeding: a worktree checks out _tracked_ files only, so a project's gitignored MCP/CLI configs (`.mcp.json`, `.claude/settings.json`, `.codex/config.toml`, `.gemini/settings.json`) would be missing — an isolated session couldn't reach its MCP server. With `seed_adapter_defaults` (default on) each loaded adapter's own `seed_files` are copied in from the main repo before the session launches; `worktree_seed` adds extra paths. Copy-when-absent, seeded before the hook-merge (a seeded `settings.json` keeps its content and just gains the Stop hook), and shielded from the unit's `git add -A`.
- Run state never moves into a worktree — `.bmad-loop/` always lives in the main repo; spec paths are persisted relative to the worktree so a kept-failed run stays portable.
- Merge-back is serialized; `max_parallel` is a validated knob clamped to `1` until parallel fan-out is built. The `repo_root` key in `_bmad/bmm/config.yaml` (defaults to the project dir) decouples where git/code work happens from where run state lives (monorepos).
- `commit_message_template` (`{story_key}` / `{run_id}` substituted) customizes story/bundle commit messages.

### Plugins (extensibility)

- A first-class **plugin system** extends the orchestrator without touching the core loop. A plugin is a folder-drop `plugin.toml` manifest (under `.bmad-loop/plugins/<name>/`, overlaying bundled `bmad_loop/data/plugins/<name>/`) that can: **observe / veto / mutate** the run at every lifecycle stage via a hook bus; contribute **settings** that render in the settings TUI and persist to `[plugins.<name>]`; and inject its own **workflow** sessions at `post_dev_phase` / `post_review_result`.
- **Two trust tiers:** a data-only / declarative plugin (settings + `[hooks.<stage>]` shell commands) runs on discovery; a plugin that ships an in-process `[python]` module is **never imported unless listed in `[plugins] enabled`** — dropping a folder in never runs code. Every hook (subprocess or Python) is failure-isolated: a raise is caught, journalled, and disables that instance for the run — never crashes it.
- Veto maps onto the engine's **existing** control flow (`skip`/`defer`/`pause`), and mutation is confined to a per-stage whitelist (`proposed_prompt`, `proposed_commit_message`, …) plus a persisted `shared` dict — no new abort path. Distribution is folder-drop now, with a documented `bmad_loop.plugins` entry-point seam for pip-installed plugins later.
- See **[Writing a bmad-loop plugin](plugin-authoring-guide.md)** for the manifest, settings, hook, stage, trust, and workflow reference, plus a worked walkthrough; a complete example ships under [`examples/plugins/guardrails/`](../examples/plugins/guardrails/).

### Game-engine projects (opt-in)

- A niche engine layer — built **on the plugin system** — for projects whose dev/sweep cycle drives a **live engine Editor** via an Editor MCP (Unity bundled as `bmad_loop/data/plugins/unity/`; Godot/Unreal later). Off by default; enable with `[plugins] enabled = ["unity"]` + a `[plugins.unity]` table. (The legacy `[engine]` policy block still loads, folded onto `[plugins.unity]` with a deprecation warning; project-local overrides now live under `.bmad-loop/plugins/<name>/`.)
- `editor_mode` is coupled to `[scm] isolation` because a live Editor MCP can only act on the folder its Editor has open: **`shared`** (requires `isolation = "none"`) runs the agent in place on the project the operator's warm Editor already has open — zero relaunches, full live MCP; **`per_worktree`** (requires `isolation = "worktree"`) gives each worktree its own managed Editor.
- Readiness gate: before each unit, the plugin's `pre_ready_gate` hook blocks until the Editor + MCP report ready (Unity: `wait-for-ready` for IvanMurzak, connectivity check for CoplayDev); on timeout the unit is deferred with an `ATTENTION` notice rather than starting a session against a half-open Editor.
- `per_worktree` lifecycle (Unity/IvanMurzak): a setup hook launches the worktree's own Editor (MCP port auto-derived from the worktree path, so it self-isolates from the operator's main Editor), writes the worktree `.mcp.json`, and primes the worktree's `Library` with a reflink/CoW copy of the warm main `Library` (so Unity reimports incrementally instead of a cold full reimport that crashes the import workers; deep-copy then symlinked-empty-cache fallbacks off-CoW); the readiness gate then waits for it; a teardown hook quits the Editor on completion **and** on pause/escalation. The MCP-generated skill tree (gitignored) is copied into each worktree via the plugin's `seed_globs`; a setup failure defers the unit instead of running it against no Editor.
- The Unity plugin's settings are editable in the TUI under its plugin section. To target another engine or a different Editor MCP, see [Writing a Game Engine plugin](game-engine-plugin-guide.md) (now built on the general [plugin system](plugin-authoring-guide.md)) and [Writing a plugin for a specific Editor MCP](game-engine-mcp-guide.md).

### Resumability & state

- Every run is a resumable on-disk state machine: `bmad-loop resume <run-id>` continues from a gate, escalation, or interruption.
- All run state in `.bmad-loop/runs/<run-id>/` (gitignored): `state.json`, `journal.jsonl` (every decision), `events/` (hook signals), `tasks/<id>/`, `logs/`, `deferred/`, `resolve/`, `ATTENTION`.

### Hook-based transport (no pane-scraping)

- Coding-agent hooks (`Stop` / `SessionStart` / `SessionEnd` / `PreCompact`) write structured event files the orchestrator watches; skills write a machine-readable `result.json`.

### Deferred-work sweeps

- Skills accumulate an append-only ledger (`deferred-work.md`, `DW-<n>` entries): split-off goals, pre-existing findings, "needs human decision" items.
- `bmad-loop sweep` triages every open entry against the actual code (ledger statuses treated as unreliable) → partition: already-resolved (auto-closed with evidence) / bundles / blocked / skip / decisions.
- Bundles run the full pipeline (dev `--dw-bundle` → review → verify → commit); the review gate checks every bundle entry is `status: done`.
- Interactive decision walkthrough (build / close / keep-open per option, with a recommendation); answers written back as `decision:` lines. Unattended runs leave decisions open.
- Answer skipped/missed decisions out of band with `bmad-loop decisions` (or `d` in the TUI): reconstructed from past triage output, saved to `.bmad-loop/decisions.json`, and consumed by the next sweep with no re-prompt (build → bundle, close → closed, keep-open → recorded).
- Auto-sweep at epic boundaries or run-end (`[sweep] auto`); a failed/paused child sweep never interrupts the parent run.
- Repeat mode (`--repeat` / `[sweep] repeat`): re-triages after each cycle to absorb newly generated deferred work, stopping when a cycle does nothing addressable or hits `max_cycles`.
- Sweeps are their own resumable runs (`bmad-loop resume <id>`).

### Stories mode (folder+id dispatch)

- Opt-in second story source (`[stories] source = "stories"` + `spec_folder`, or `bmad-loop run --spec <folder>`): drives the same loop off a typed `stories.yaml` (a `bmad-spec` Story Breakdown, sibling of `SPEC.md`) instead of `sprint-status.yaml`.
- Dispatches each entry by **folder + id** (`/bmad-dev-auto Spec folder: <folder>. Story id: <id>.`); the story spec lands at `<folder>/stories/<id>-<slug>.md` and is read back by a deterministic id-keyed glob — no shared board to line-edit, no result-artifact mtime-scan.
- Strictly linear schedule (list order, no `depends_on`); `done` skipped, non-terminal statuses resumed on re-dispatch, `blocked`/sentinel/ambiguous stops the run for resolve. `bmad-loop run --dry-run --spec <folder>` and `bmad-loop status` print the schedule/board (id · live disk state · checkpoint markers · title).
- Preflight content-probe: stories mode requires a `bmad-dev-auto` new enough for folder+id dispatch, or the run aborts with remediation. Sprint mode keeps working with any installed version.
- Sentinel recovery: a pre-planning-halt sentinel spec (`<id>-unresolved.md` / `<id>-ambiguous.md`) is auto-deleted with a preserved copy under the run dir on re-arm, matching the contract's delete-to-retry.

### Gates & human checkpoints

- Gate modes (`[gates].mode`): `none` (fully unattended) / `per-epic` (pause at epic boundaries, default) / `per-story-spec-approval` (pause after each spec for approval). Note: `per-epic` is inert in stories mode — the flat `stories.yaml` list has no epics, so the boundary never fires; use the per-story checkpoints (below) or `per-story-spec-approval` there.
- Per-story checkpoints (stories mode): independent `spec_checkpoint` (pause before code to review the plan; approve → implement, or request a replan) and `done_checkpoint` (pause after the story commits; skipped when it is the last story). Additive to `gates.mode` — a story can pause twice.
- Every mid-run pause is surfaced in the TUI: a per-run pause-kind badge, a global attention count, and a `p` viewer per stage (plan-checkpoint spec review, story-checkpoint summary card, escalation with story context, gate spec review) — all calling the same CLI code paths.
- Retrospective handling (`retrospective = never | notify | auto`) and notification on epic boundaries.

### Multi-CLI / multi-agent support

- Generic adapter drives any CLI fitting the injection + hook-signal transport; CLI specifics live in declarative TOML profiles. Two independent axes: the **CLI** (`CodingCLIAdapter` + profile) and the **terminal transport** (`TerminalMultiplexer`) — tmux is the only backend today, behind a pluggable seam so a native-Windows backend can be added without touching the engine (see the [adapter authoring guide](adapter-authoring-guide.md#two-axes-cli-vs-transport)).
- The OS is abstracted by a **registry of seams**, each selecting an implementation by platform (with a test-override env var) and extended by a single registration line: the terminal multiplexer (`register_multiplexer`), the process-lifecycle `ProcessHost` (`register_process_host` — `terminate`/`force_kill`/`is_alive`/`identity`), and the hook interpreter (`ProcessHost.hook_interpreter()`); `bmad-loop validate` runs a platform preflight over them. Porting to a new OS is new files + registrations, no core edits — see [Porting bmad-loop to a new OS](porting-to-a-new-os.md).
- Supported, E2E-verified: `claude` (reference), `codex` (≥ 0.139), `gemini` (≥ 0.46), `copilot` (GitHub Copilot CLI ≥ 2026-02 — the `copilot` binary, not the VS Code extension; `agentStop` turn-end, `-i` interactive launch, `--allow-all-tools`; pin a capable model — the free GPT-5 mini default is unreliable for multi-step skills).
- Experimental, probe-required: `antigravity` (Google's `agy` ≥ 1.0.16) — `-i` interactive launch, `Stop` turn-end hook (flat handler in `.agents/hooks.json`, no SessionStart/SessionEnd), `--dangerously-skip-permissions` for unattended runs; `usage_parser = "none"` until a transcript parser is written. Verify against your `agy` build with `probe-adapter antigravity`.
- Per-stage CLI/model overrides: run dev on one CLI/model, review on another (`[adapter.dev]`, `[adapter.review]`, `[adapter.triage]`).
- Add a CLI without touching Python: drop a TOML profile in `.bmad-loop/profiles/<name>.toml` (binary, prompt template, bypass flags, hook dialect, native→canonical event map).
- `bmad-loop probe-adapter` collects + sanitizes the data needed to finalize/add a profile (hook payload shape, transcript location/format, token schema): a zero-launch scan by default, opt-in `--probe` for live capture. See the [adapter authoring guide](adapter-authoring-guide.md).

### Budgeting & cost tracking

- Per-story token budget (`max_tokens_per_story`) using a cost-weighted total — cache reads counted at `cache_read_weight` (default 0.1, matching ~0.1x vendor billing); displayed totals stay raw.
- Token usage read from each CLI's local session transcript (per-profile `usage_parser`), aggregated per story (`bmad-loop status`).

### Configuration (`.bmad-loop/policy.toml`)

- Single policy file written by `init`, snapshotted at run start (applies to new runs and resumes; editable live from the TUI).
- Sections: `[gates]`, `[limits]`, `[verify]`, `[notify]`, `[review]`, `[adapter]` (+ per-stage), `[sweep]`, `[scm]` (worktree isolation + merge-back), `[cleanup]` (run-dir retention + disk reclamation), `[plugins]` (trust allowlist + per-plugin `[plugins.<name>]` config — e.g. the opt-in game-engine layer via `[plugins.unity]`, off by default), `[tui]` (`low_frame_rate` for slow/SSH links).
- Tunable limits: `max_review_cycles`, `max_dev_attempts`, `session_timeout_min`, `stop_without_result_nudges`, `dev_stall_grace_s`, `dev_stall_nudges`, `max_tokens_per_story`.

### TUI dashboard

- Read-only observer + launcher (`bmad-loop tui`): runs table, expandable sprint tree (epics → stories/retro), severity-colored deferred-work ledger, per-story phase table (phase · dev attempts · review cycles · tokens · commit/defer), tabs tailing journal / pane log / `ATTENTION`.
- Launch & manage from keys: start run/sweep (`r`/`s`), resume (`e`), resolve escalation (`R`), answer missed decisions (`d`), attach (`a`), cleanup (`c`), validate (`v`), settings editor (`g`), theme/mode toggle (`M`), quit (`q`).
- Survives TUI exit/crash: runs launched from the TUI are detached `bmad-loop` processes in a dedicated `bmad-loop-ctl` tmux session; the dashboard watches purely via run-dir artifacts, so shell-started runs appear identically.
- Comment-preserving policy editor (`g`): grouped form, sections collapsed by default with one-line descriptions (`ctrl+e` toggles all), validated with the engine's own parser, unset keys show defaults as placeholders.

### tmux session management

- Each run drives agents in a dedicated `bmad-loop-<run-id>` session; `attach` to watch live.
- Auto-teardown on finish (`cleanup_session_on_finish`, disable to inspect); `stop` always kills it; paused/interrupted runs keep the session for `resume`.
- `bmad-loop cleanup` (or `c` in the TUI) sweeps leftover sessions/windows for finished/stopped/orphaned runs **of the current project**; live runs, and anything belonging to another project, are never touched.

### Disk reclamation (`[cleanup]`)

- `bmad-loop clean` reclaims **disk** (distinct from `cleanup`, which is only tmux). It tears down git worktrees a mid-flight stop left mounted — the main accumulation source: each carries a real Unity `Library/` (incl. the MCP-server build), which `git worktree remove` cannot reach once the engine was killed before teardown. It then trims the heavy `worktrees/` tree from runs kept for history (the run still lists in the dashboard — discovery reads `state.json`, not the worktree), and archives or deletes runs past the retention window.
- Safe by construction: only **finished or stopped** runs are touched; running, unknown-host, paused and interrupted (resumable) runs are never reclaimed. `--keep <run-id>` protects a specific run (e.g. a finished one whose Editor is still live), `--dry-run` previews, `--retain N`/`--hard` tune the window and archive-vs-delete.
- Prevention is automatic: every `run`/`sweep` start reconciles worktrees leaked by a prior **finished** run (`[cleanup] auto_clean_on_finish`), and the Unity plugin's `post_run` hook removes the IvanMurzak MCP server's downloaded `/tmp/<company>/<product>/*.zip` and truncates its unbounded editor log (`[cleanup] clean_tmp`). For recurring housekeeping of stopped runs, schedule `bmad-loop clean`.

### Setup & install

- `bmad-loop init` installs the five `bmad-loop-*` skills (`.claude/skills/` and/or `.agents/skills/`), the hook relay, `.bmad-loop/policy.toml`, and a runs-dir gitignore. Flags: `--cli` (repeatable), `--no-skills`, `--force-skills`.
- `bmad-loop validate` preflights every prerequisite: BMAD config, sprint-status, git, tmux, CLI binary, hook registration.
- Non-invasive: drives the upstream `bmad-dev-auto` skill unmodified — there is no fork to keep in sync — and review is just a re-invocation of it on the `done` spec. Your standard BMAD install is never modified.

### Command reference

- `bmad-loop init` — install skills, hooks, policy, gitignore.
- `bmad-loop validate` — preflight all prerequisites.
- `bmad-loop run` — drive the dev → review → verify → commit loop.
- `bmad-loop sweep` — triage + execute open deferred-work entries.
- `bmad-loop resume <run-id>` — continue a paused/interrupted run.
- `bmad-loop resolve <run-id>` — resolve a CRITICAL escalation, then re-arm + resume.
- `bmad-loop decisions` — answer deferred-work decisions past sweeps left unanswered (`--list` to just show them).
- `bmad-loop list` (`ls`) — list every run/sweep with its short ref, type, and status.
- `bmad-loop status [<run-id>]` — run + sprint summary with per-story token totals.
- `bmad-loop diagnose [<run-id>]` (`diag`) — emit a sanitized diagnostic dump of a run/sweep to hand maintainers (histograms, counts, env, file sizes — no code/spec/prompts/paths/PII); defaults to the latest run (`--all`, `--out`, `--json`, `--max-journal-entries`).
- `bmad-loop attach [<run-id>]` — tmux-attach to a run's live agent session.
- `bmad-loop stop <run-id>` — stop a live run (engine + agent session).
- `bmad-loop delete <run-id>` — delete a run directory (`--force` stops it first if live).
- `bmad-loop archive <run-id>` — compress a run into `.bmad-loop/archive` and remove it (`--force` stops it first if live).
- `bmad-loop cleanup` — remove leftover tmux artifacts for finished/stopped runs.
- `bmad-loop clean` — reclaim disk from concluded runs per `[cleanup]`: tear down worktrees a mid-flight stop orphaned, trim heavy `worktrees/` from runs kept for history, archive/delete past the retention window (`--dry-run`, `--keep`, `--retain N`, `--hard`).
- `bmad-loop tui` — the interactive dashboard (`--low-frame-rate` for slow/SSH links).
- `bmad-loop probe-adapter <cli>` (`collect-adapter-data`) — collect + sanitize adapter-finalization data for a CLI profile; default zero-launch scan, opt-in `--probe` live capture.
- Every command takes `--project <dir>` (default: current directory). Any `<run-id>` accepts a partial — the tail after the last `-`, shortened to any unique prefix.
