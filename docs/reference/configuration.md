# Configuration reference

bmad-loop reads its policy from `.bmad-loop/policy.toml` in the project root. `bmad-loop init` writes a commented template there when the file is missing (it never overwrites an existing one) and adds the file to the project's `.gitignore`, so policy is per-machine.

Every key is optional: an unset key takes the default listed below, unknown core keys are ignored, and a value of the wrong type or outside its allowed set is rejected with a policy error. The TUI settings screen edits the same file.

Each `run` and `sweep` snapshots the policy into its run state when it starts, and `resume` re-stamps that snapshot from the current file, so an edit applies to new runs and to the next resume of an existing one.

Where a key can be set in more than one place, the first of these wins:

1. An environment variable from the table below, where one exists for the key.
2. A command-line flag that overrides a single key for that invocation: `run --spec` (`[stories] source`), `sweep --max-bundles` / `--repeat` / `--no-repeat` / `--max-cycles` (`[sweep]`), `clean --retain` (`[cleanup] run_retention`).
3. `.bmad-loop/policy.toml`.
4. The built-in default.

Plugin activation (`[plugins] enabled`) and plugin settings (`[plugins.<name>]`) are not listed here; see the [plugin authoring guide](../plugin-authoring-guide.md#settings) and each plugin's own guide.

## Environment variables

Operator overrides read by the orchestrator:

| Variable                      | Value                                          | Effect                                                                                                                                                                                                                                                                                                                                           |
| ----------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `BMAD_LOOP_MUX_BACKEND`       | registered backend name (e.g. `tmux`, `psmux`) | Forces the terminal-multiplexer backend, outranking `[mux] backend` and auto-selection. A name matching no registered backend is an error. See [multiplexer backends](../multiplexer-backends.md).                                                                                                                                               |
| `BMAD_LOOP_STATE_DIR`         | absolute directory path                        | Replaces the state root that holds out-of-tree run state (`<root>/<project-hash>/<run-id>/`). A relative value is an error; an empty value reads as unset. Unset: `$XDG_STATE_HOME/bmad-loop` (when absolute), else `~/.local/state/bmad-loop`; on Windows `%LOCALAPPDATA%\bmad-loop\state`, else `%USERPROFILE%\AppData\Local\bmad-loop\state`. |
| `BMAD_LOOP_SESSION_TIMEOUT_S` | seconds (float)                                | Overrides the per-session wall-clock budget (`[limits] session_timeout_min` × 60). A value that is not a finite positive number is ignored.                                                                                                                                                                                                      |
| `BMAD_LOOP_PROCESS_HOST`      | registered host name (`posix`, `windows`)      | Forces the process-lifecycle host (a test hook). A name matching no registered host is an error. Unset: the platform's host.                                                                                                                                                                                                                     |

Other `BMAD_LOOP_*` names are not operator settings:

- Session protocol (`BMAD_LOOP_RUN_DIR`, `BMAD_LOOP_TASK_ID`, `BMAD_LOOP_EVENTS_DIR`, …) — the orchestrator sets these in each session it launches and overwrites any value you export.
- Plugin hook environment (`BMAD_LOOP_STAGE`, `BMAD_LOOP_SETTING_<KEY>`, …) — see [declarative hooks](../plugin-authoring-guide.md#declarative-hooks).
- Unity plugin (`BMAD_LOOP_UNITY_*`, `BMAD_LOOP_ENGINE_*`) — see the [Unity plugin env-var reference](../game-engine-mcp-guide.md#full-env-var-reference-unity-plugin).

## Policy settings

Generated from the core settings schema (`src/bmad_loop/data/settings/core.toml`) by `scripts/gen_policy_reference.py`; do not edit the tables by hand.

<!-- BEGIN GENERATED: policy-reference — run scripts/gen_policy_reference.py -->
<!-- prettier-ignore-start -->

### `[gates]`

Approval gates, escalation & retrospective behavior.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `mode` | string | `"per-epic"` | `none` / `per-epic` / `per-story-spec-approval` |  |
| `retrospective` | string | `"notify"` | `auto` / `never` / `notify` | at each epic boundary, and at run end for a finished last epic: never, notify (suggest /bmad-retrospective), or auto (run a headless /bmad-retrospective session on [adapter.retro] and commit its doc + board) |

### `[review]`

Separate adversarial review session toggle.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `enabled` | boolean | `true` |  | ON: adversarial review runs in a dedicated 2nd session · OFF: the dev pass runs its own review layers inline, story straight to done |
| `trigger` | string | `"recommended"` | `always` / `recommended` | recommended: run the 2nd-opinion review only when the dev pass flags it · always: run it every story (both bounded by limits.max_review_cycles) |
| `on_timeout` | string | `"retry"` | `defer` / `retry` / `salvage-if-done` | retry: burn a review cycle per timeout (default) · salvage-if-done: commit the verified dev product and refile the follow-up · defer: give up on the first timeout |
| `on_status_contradiction` | string | `"escalate"` | `escalate` / `retry` | escalate: pause naming both sides when a review writes sprint-status back off done (default) · retry: legacy — burn review cycles, then defer + roll back |

### `[stories]`

Story-queue source: classic sprint board vs. folder+id dispatch.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `source` | string | `"sprint-status"` | `sprint-status` / `stories` / `tickets` | sprint-status: walk sprint-status.yaml · stories: folder+id dispatch off a typed stories.yaml (needs spec_folder) · tickets: the BMAD v7 ticket tree of core.active_initiative, via _bmad/method/scripts/tickets.py (experimental) |
| `spec_folder` | string | `""` |  | stories mode only: project-relative path to the epic spec folder (holds stories.yaml + SPEC.md) |

### `[tickets]`

BMAD v7 ticket-tree runs (stories.source = tickets).

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `mark_done` | string | `"auto"` | `auto` / `never` | auto: bmad-loop marks a ticket done once its build passes verify and review · never: leave the plan at built for a human |
| `thorough_review` | string | `"high-risk"` | `always` / `high-risk` / `never` | when a ticket gets the thorough review: high-risk (its risk is high), always, or never |

### `[limits]`

Cycle/attempt caps, timeout & token budget.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `max_review_cycles` | integer | `3` | ≥ 1 |  |
| `max_dev_attempts` | integer | `2` | ≥ 1 |  |
| `artifact_file_max_mb` | integer | `5` | ≥ 1 | raw-byte limit for each ignored file selected for isolated artifact publication; tracked declarations ride Git and do not count |
| `artifact_payload_max_mb` | integer | `10` | ≥ 1 | aggregate raw-byte limit across ignored files selected for isolated artifact publication; checked before base64 state encoding |
| `max_followup_reviews` | integer | `1` | ≥ 0 | additional review rounds granted solely because a finalized (status: done) round still recommended a follow-up; once spent, such a round converges + refiles the recommendation instead of burning another cycle · 0 = never honor a pass's own recommendation |
| `session_timeout_min` | integer | `90` | ≥ 1 |  |
| `git_timeout_s` | integer | `120` | ≥ 1 | bound on any single git subprocess the orchestrator spawns; exceeding it degrades/pauses (never crashes the run) — raise on a loaded host or a very large worktree |
| `teardown_grace_s` | integer | `20` | ≥ 0 | verified session teardown: poll the killed window up to this long, then force-kill its pane pids and re-kill the window · 0 = single unverified best-effort kill |
| `stop_without_result_nudges` | integer | `1` | ≥ 0 |  |
| `dev_stall_grace_s` | integer | `600` | ≥ 0 | silence grace for a dev/review session, armed at launch and re-armed by transport activity or fresh Stop/idle evidence, before bounded nudges/stalled classification · also the transcript-idle notice threshold (journal session-idle/session-active, TUI idle age) · 0 = no launch timer (and no idle notice), but a result-less turn end still fails fast |
| `session_start_grace_s` | integer | `120` | ≥ 0 | seconds a session whose CLI maps SessionStart may run without one before a journal-only session-start-missing notice; 0 = off |
| `dev_stall_nudges` | integer | `2` | ≥ 0 | best-effort wake nudges for a silent dev/review session on grace expiry before it is called stalled; transport activity re-arms the grace and fresh Stop/idle evidence restores this budget · 0 = stall on grace expiry |
| `dev_stall_nudges_cap` | integer | `6` | ≥ 0 | total (never-restored) best-effort stall nudges for a dev/review session; bounds launch-time recovery and Stop/idle budget refills because an accepted nudge does not guarantee a wake · 0 = stall on first grace expiry |
| `workflow_stall_nudges_cap` | integer | `3` | ≥ 0 | total (never-restored) stall nudges for an injected plugin-workflow session before it is called stalled; bounds a session that finished its work but never wrote its completion marker · 0 = stall on first grace expiry |
| `dev_contract_nudge` | boolean | `true` |  | ON: one targeted nudge per session when a Stop finds a spec finalized to a terminal frontmatter status but missing its `## Auto Run Result` marker, asking the skill to append it and end its turn (sent exactly once, never refilled, touches no stall counters) · OFF: rely only on harness-side frontmatter synthesis |
| `max_tokens_per_story` | integer | `2000000` | ≥ 1 | advisory cap on one story's cumulative weighted spend across all its sessions, re-checked at every session boundary · crossing it raises one ATTENTION + desktop notice per story and nothing else — the story runs on · for a cap that can END a session see session budget mode below |
| `cache_read_weight` | float | `0.1` | 0.0–1.0 | weight of cache-read tokens in every token total — budgets and displays alike · cache reads bill at ~0.1x base input on all supported vendors and are 80-95% of the raw count on agentic work, so the default keeps totals proportional to spend rather than to context re-reads · 1.0 = count cache reads at full price (raw) |
| `session_budget_mode` | string | `"warn"` | `enforce` / `off` / `warn` | mid-session guard on the weighted per-session spend, sampled every ~30s · off: no sampling · warn: one ATTENTION + breadcrumb · enforce: wrap-up nudge then over_budget termination (retry→defer) |
| `max_tokens_per_session` | integer | `4000000` | ≥ 1 | weighted (cache-discounted) token cap a single session may spend before the budget guard trips; healthy sessions run ~1-2.5M weighted, so the default trips only true runaways |
| `session_budget_grace_s` | integer | `240` | ≥ 0 | enforce mode: seconds a tripped session gets to wrap up after the nudge before it is terminated over_budget · 0 = terminate at trip, no nudge |

### `[verify]`

Post-implementation verification commands.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `commands` | array of strings | `[]` |  |  |
| `command_timeout_min` | integer | `30` | ≥ 1 | per-command wall-clock bound; a command running past it is killed and counts as an ordinary (fixable) verify failure · a graceful stop waits for the running command |
| `stream_capture_kb` | integer | `256` | ≥ 0 | per-stream cap on the verifier stdout/stderr retained under the run's verify/ directory (the tail is kept; the journal records the full size and a truncation flag) · 0 = capture nothing |
| `env_fault_rc` | integer | `0` | 0–255 | exit status a verify command uses to declare an environment fault (pause, attempt not charged) instead of a code failure · 0 = disabled; 75 (EX_TEMPFAIL) is a good choice |

### `[environment]`

Environment health probes (run before verify, session launches, and charging a failed attempt).

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `probes` | array of strings | `[]` |  | health-check commands run in the project root; a failing probe (nonzero exit, timeout, unrunnable) is an environment fault: the run pauses and the attempt is not charged |
| `probe_timeout_s` | integer | `60` | ≥ 1 | per-probe wall-clock bound; a probe running past it counts as failed |

### `[notify]`

Desktop & file notifications.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `desktop` | boolean | `true` |  |  |
| `file` | boolean | `true` |  |  |

### `[adapter]`

CLI client, model & bypass flags (base for all stages).

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `name` | string | unset — CLI profile — default: claude |  |  |
| `model` | string | unset — CLI default model |  |  |
| `effort` | string | unset — provider default |  | reasoning effort, free-form (provider/model-specific, e.g. high, max); sent by opencode-http as the model variant (on 2.x it must be one of the model's variants), ignored by the tmux CLIs |
| `extra_args` | array of strings | unset |  | when set, replaces the CLI profile's permission-bypass flags (its bypass_args), so repeat them (e.g. claude: --permission-mode bypassPermissions) or unattended sessions may stall on permission prompts |
| `cleanup_session_on_finish` | boolean | `true` |  |  |
| `usage_grace_s` | float | unset — inherit from CLI profile (copilot ships 8s) | ≥ 0 | seconds to poll the transcript for token usage after a session ends; CLIs that flush totals on shutdown (copilot) need a grace |
| `stop_without_result_nudges` | integer | unset — inherit (copilot profile ships 5) | ≥ 0 | result-less Stop signals tolerated before a session is called stalled; overrides the global limits value for this CLI |

### `[adapter.dev]`

Dev-stage adapter overrides.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `name` | string | unset — inherit from [adapter] |  |  |
| `model` | string | unset — inherit / client default |  |  |
| `effort` | string | unset — inherit / provider default |  |  |
| `extra_args` | array of strings | unset |  | inherits from [adapter] only when the client matches (a stage that switches client falls back to that client's bypass flags); when set, replaces the CLI profile's permission-bypass flags (its bypass_args), so repeat them (e.g. claude: --permission-mode bypassPermissions) |
| `usage_grace_s` | float | unset — inherit from [adapter] | ≥ 0 |  |
| `stop_without_result_nudges` | integer | unset — inherit from [adapter] | ≥ 0 |  |

### `[adapter.review]`

Review-stage adapter overrides.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `name` | string | unset — inherit from [adapter] |  |  |
| `model` | string | unset — inherit / client default |  |  |
| `effort` | string | unset — inherit / provider default |  |  |
| `extra_args` | array of strings | unset |  | inherits from [adapter] only when the client matches (a stage that switches client falls back to that client's bypass flags); when set, replaces the CLI profile's permission-bypass flags (its bypass_args), so repeat them (e.g. claude: --permission-mode bypassPermissions) |
| `usage_grace_s` | float | unset — inherit from [adapter] | ≥ 0 |  |
| `stop_without_result_nudges` | integer | unset — inherit from [adapter] | ≥ 0 |  |

### `[adapter.triage]`

Triage-stage adapter overrides.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `name` | string | unset — inherit from [adapter] |  |  |
| `model` | string | unset — inherit / client default |  |  |
| `effort` | string | unset — inherit / provider default |  |  |
| `extra_args` | array of strings | unset |  | inherits from [adapter] only when the client matches (a stage that switches client falls back to that client's bypass flags); when set, replaces the CLI profile's permission-bypass flags (its bypass_args), so repeat them (e.g. claude: --permission-mode bypassPermissions) |
| `usage_grace_s` | float | unset — inherit from [adapter] | ≥ 0 |  |
| `stop_without_result_nudges` | integer | unset — inherit from [adapter] | ≥ 0 |  |

### `[adapter.retro]`

Retro-stage adapter overrides.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `name` | string | unset — inherit from [adapter] |  |  |
| `model` | string | unset — inherit / client default |  |  |
| `effort` | string | unset — inherit / provider default |  |  |
| `extra_args` | array of strings | unset |  | inherits from [adapter] only when the client matches (a stage that switches client falls back to that client's bypass flags); when set, replaces the CLI profile's permission-bypass flags (its bypass_args), so repeat them (e.g. claude: --permission-mode bypassPermissions) |
| `usage_grace_s` | float | unset — inherit from [adapter] | ≥ 0 |  |
| `stop_without_result_nudges` | integer | unset — inherit from [adapter] | ≥ 0 |  |

### `[sweep]`

Deferred-work sweep automation.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `auto` | string | `"never"` | `never` / `per-epic` / `run-end` |  |
| `max_bundles` | integer | `5` | ≥ 1 |  |
| `max_triage_attempts` | integer | `2` | ≥ 1 |  |
| `repeat` | boolean | `false` |  |  |
| `max_cycles` | integer | `5` | ≥ 1 |  |

### `[scm]`

Git isolation, branching & merge-back.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `isolation` | string | `"none"` | `none` / `worktree` | none: work in place on the checked-out branch (default) · worktree: run each story in its own git worktree, merge back to the target branch |
| `branch_per` | string | `"story"` | `run` / `story` | worktree mode: one branch per story, or one shared branch per run (run forces delete-branch off so the shared branch survives) |
| `target_branch` | string | unset — default: the branch checked out at run start |  | worktree mode: branch all units merge back into (created if missing) |
| `merge_strategy` | string | `"merge"` | `ff` / `merge` / `squash` | worktree mode: how a unit branch lands on the target — ff, merge, or squash |
| `delete_branch` | boolean | `true` |  | worktree mode: delete the unit branch after a successful merge |
| `keep_failed` | boolean | `true` |  | worktree mode: keep a failed unit's worktree + branch mounted for inspection |
| `rollback_on_failure` | boolean | `false` |  | ⚠ in-place mode (isolation=none): when ON, a failed attempt's tracked changes are auto-reverted and the untracked files this run created are deleted (its uncommitted work is lost). When OFF (default), the orchestrator never touches your tree — it pauses with manual recovery steps. Governs unattended/stopped attempts only: a resolved escalation's re-drive always auto-recovers regardless (reverts the failed source, keeps the corrected spec). Prefer isolation=worktree to keep failures off your main checkout. |
| `preserve_keep` | integer | `20` | ≥ 0 | attempt-preserve/\* recovery branches, attempt-preserve-dirty/\* rollback snapshots and merge-preflight-preserve/\* merge pre-flight snapshots kept at each run start (per family) · the newest by committer date survive and the tail is deleted (0 = never prune) |
| `seed_adapter_defaults` | boolean | `true` |  | worktree mode: copy each loaded adapter's gitignored MCP/CLI configs (.mcp.json, .claude/settings.json, .codex/config.toml …) into the worktree so isolated sessions can reach their MCP server |
| `worktree_seed` | array of strings | `[]` |  | worktree mode: additional project-relative gitignored files to copy into each worktree, one per line — on top of the adapter defaults above |
| `commit_message_template` | string | unset — blank = built-in default; {story_key} / {run_id} / {story_title} substituted |  | commit message dev sessions use for a story/bundle commit when set |
| `failed_diff_max_mb` | integer | `5` | ≥ 1 | per-file size limit for untracked files captured into a kept-failed unit's changes.patch · oversized files are skipped with a marker |
| `failed_diff_unlimited` | boolean | `false` |  | ⚠ ON: capture failed-unit diffs with NO size limit (overrides the cap above) — may produce very large patches; a warning is logged when active |

### `[cleanup]`

Disk reclamation for .bmad-loop/runs, plus each run's out-of-tree control-plane dir (terminal runs only).

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `run_retention` | integer | `10` | ≥ 0 | newest concluded runs kept whole · older ones are trimmed or archived by `bmad-loop clean` (0 = keep none by count) |
| `retention_days` | integer | `0` | ≥ 0 | 0 = disabled · else also keep runs newer than N days regardless of the count above |
| `trim_artifacts` | boolean | `true` |  | drop the heavy worktrees/ tree from concluded runs · the run still lists in the dashboard with full status/journal/logs |
| `archive_old` | boolean | `true` |  | archive (.bmad-loop/archive/&lt;id&gt;.tar.gz) rather than permanently delete runs beyond the retention window |
| `auto_clean_on_finish` | boolean | `true` |  | reconcile worktrees leaked by a mid-flight stop + apply retention each time a run finishes cleanly |
| `clean_tmp` | boolean | `true` |  | let engine plugins clean their /tmp scratch on finish (e.g. the Unity MCP server's downloaded zips) |

### `[tui]`

Dashboard rendering (slow-link / SSH tuning).

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `low_frame_rate` | boolean | `false` |  | cap to 15fps + disable animations — fixes repaint tearing/garbage over slow or SSH links · takes effect next time the TUI launches |

### `[operator]`

Stories that owe external, human-only actions.

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `enabled` | boolean | `true` |  | let a session finish + commit a story whose acceptance criteria need a human action (buy a domain, publish a DNS record) and park it as awaiting-operator instead of forcing done or blocked · what is owed is recorded in the story spec's operator_actions: frontmatter, and `bmad-loop confirm <story-key>` completes the story once you have done it |
| `on_review_demotion` | string | `"escalate"` | `escalate` / `park` | escalate: a review pass is never offered the park, and one that finalizes a done story at awaiting-operator is not accepted (default) · park: the review may demote it — the board moves done -&gt; awaiting-operator, the story commits and parks · sprint mode with parks enabled only |

### `[mux]`

Terminal-multiplexer backend (machine-specific — policy.toml is gitignored).

| Key | Type | Default | Allowed | Description |
| --- | --- | --- | --- | --- |
| `backend` | string | `""` | a registered backend name — see [multiplexer backends](../multiplexer-backends.md) | force a registered transport backend by name (see `bmad-loop mux`); leave blank to auto-select — BMAD_LOOP_MUX_BACKEND outranks it · takes effect on the next bmad-loop invocation |
| `honor_ambient_psmux_data_dir` | boolean | `false` |  | psmux only: on — an absolute PSMUX_DATA_DIR your profile exports into every shell is used as the registry instead of bmad-loop's derived per-project one · off (default) — it is overridden · leave off for a value typed into one shell · takes effect on the next bmad-loop invocation; a running TUI refuses launches until restarted |

<!-- prettier-ignore-end -->
<!-- END GENERATED: policy-reference -->
