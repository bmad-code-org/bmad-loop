# CLI reference

Every `bmad-loop` subcommand, its options, its `--json` document, the process exit codes, and the `validate` check ids. `bmad-loop <command> --help` prints the same option list from the installed version. For where run state lives on disk, see [run state](run-state.md); for policy keys, see [configuration](configuration.md).

## Global options

- The top-level parser documents only `-h` and `--version`. A subcommand is required; a missing or unknown one exits 2.
- **`--project PROJECT`** — every subcommand except `relay` accepts it. It names the target project root; the default is the current directory. Before dispatch, these subcommands apply the project's `[mux] backend` choice; a broken `policy.toml` falls back to auto-selection there. Commands that need the policy load it themselves and fail with `error: …`, exit 1.
- **`--json`** — accepted by `validate`, `plugins`, `probe-adapter`, `decisions`, `confirm`, `list`, `status`, `diagnose`, `cleanup` and `clean`. See [Machine-readable output](#machine-readable-output---json).

### Run references

`resume`, `resolve`, `status`, `attach`, `stop`, `delete`, `archive` and `diagnose` take a `run_id` that may be partial. A run's short ref is the segment after the last `-` of its id (`bmad-loop list` prints it).

| Ref                 | Resolves to                                                                               |
| ------------------- | ----------------------------------------------------------------------------------------- |
| exact run id        | that run                                                                                  |
| partial             | the one run whose short ref starts with it, or whose full id ends with it                 |
| empty               | refused (`empty run ref`)                                                                 |
| no match            | `no such run: <ref>`, exit 1                                                              |
| several matches     | `ambiguous run ref '<ref>' matches N runs:` plus each id, exit 1                          |
| omitted             | `status`, `attach`, `diagnose` only: the newest run; `no runs found` and exit 1 when none |
| unreadable run list | refused, naming the fault, when the unreadable entries could change the answer            |

`clean --keep RUN_ID` is the exception: it compares full run ids exactly.

## Subcommands

Listed in `bmad-loop --help` order. `--project` is omitted from the option tables below.

### `init`

Installs the hook relay, the bundled `bmad-loop-*` skills, the policy template, and `.gitignore` entries into the target project.

```text
bmad-loop init [--project PROJECT] [--cli PROFILE] [--no-skills] [--force-skills]
```

| Option           | Description                                                                                                                                                                                                                                   |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--cli PROFILE`  | Profile to register hooks for; repeatable. A packaged profile (`claude`, `codex`, `gemini`, `copilot`, `antigravity`, `opencode-http`) or an overlay profile name. Default: the profiles `policy.toml` names across all roles, else `claude`. |
| `--no-skills`    | Skip the skill copy; write only hooks, policy and `.gitignore`.                                                                                                                                                                               |
| `--force-skills` | Replace `bmad-loop-*` skill directories that already exist (default: keep them and print a hint).                                                                                                                                             |

- For each profile, merges a `bmad-loop relay <Event>` registration (absolute path of this installation's console script) into the profile's hook config file. Hookless profiles print `no hooks needed`.
- Copies `bmad-loop-resolve`, `bmad-loop-sweep` and `bmad-loop-setup` into each profile's skill tree. Writes `.bmad-loop/policy.toml` only when it is missing. Appends any missing `.gitignore` lines from `.bmad-loop/runs/`, `.bmad-loop/cache/`, `.bmad-loop/policy.toml`, `_bmad/render/`.
- Exits 1 with `FAIL: …` on an unknown profile, a target path that resolves outside the project, or a hook config file that is not valid JSON.

### `validate`

Runs the preflight checks.

```text
bmad-loop validate [--project PROJECT] [--spec FOLDER] [--render-probe] [--probes] [--json]
```

| Option           | Description                                                                                                                                                                                            |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--spec FOLDER`  | Validate stories mode against `FOLDER/stories.yaml`. Overrides `[stories] source` and skips the sprint-status gate.                                                                                    |
| `--render-probe` | Also run each dev skill's render command in a throwaway copy of `_bmad/` (needs network access).                                                                                                       |
| `--probes`       | Also run every `[environment]` probe on this host. All probes run, each bounded by `probe_timeout_s`; any failure is a problem. In tickets mode, also run the project's `tickets.py` capability probe. |
| `--json`         | Print the findings as one JSON document.                                                                                                                                                               |

- Exit 0 when no finding has severity `problem`, else 1. Warnings do not change the exit code.
- Runs no project-controlled commands unless `--render-probe` or `--probes` is given.
- Checks are chained: when an earlier gate fails (for example the policy does not load), the checks that depend on it emit nothing. A missing check id means the check did not run, not that it passed. Every id is listed under [validate checks](#validate-checks).

JSON: [`validate --json`](#validate---json).

### `mux`

Lists the registered terminal-multiplexer backends and the selected one, or saves a backend choice.

```text
bmad-loop mux [--project PROJECT] [--clear] [--force] [{set}] [name]
```

| Option / action | Description                                                                                                      |
| --------------- | ---------------------------------------------------------------------------------------------------------------- |
| _(none)_        | Print a `NAME PLATFORM AVAILABLE VERSION SELECTED` table with the selection reason.                              |
| `set <name>`    | Save `name` as `[mux] backend` in `.bmad-loop/policy.toml` (created from the template if missing).               |
| `set --clear`   | Comment the key back out so auto-selection applies.                                                              |
| `--force`       | With `set`: save a name that is not registered in this process (for example a backend installed only elsewhere). |

- Built-in backends: `tmux` (non-Windows) and `psmux` (Windows). Selection precedence is described in [multiplexer backends](../multiplexer-backends.md).
- If the forced backend (`BMAD_LOOP_MUX_BACKEND` or `[mux] backend`) cannot be resolved, the table still prints, then `FAIL: …` and exit 1. Never prompts.

### `plugins`

Lists discovered plugins and their trust grants, or manages a project plugin's grant.

```text
bmad-loop plugins [--project PROJECT] [--json] [{list,enable,accept,revoke}] [name]
```

| Action          | Effect                                                                                                                   |
| --------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `list`          | Default. Print `NAME ORIGIN SURFACE GRANT ENABLED PATH`. Exits 0 whatever the trust state (`validate` owns the verdict). |
| `enable <name>` | Hash the plugin directory without running it and record the grant. Refuses a grant that already exists and changed.      |
| `accept <name>` | Re-pin a granted plugin that changed, after printing the file changes.                                                   |
| `revoke <name>` | Remove the grant (works after the plugin directory is gone).                                                             |

- GRANT is one of `granted`, `ungranted`, `mismatch`, `stale`, `unreadable`, `missing` (grant without a plugin) or `n/a` (no grant needed). The grant store lives outside the project tree, under the [state root](run-state.md#state-root).
- `--json` works only with `list`; with another action it exits 2. A name with `list` exits 1.
- Refuses (exit 1) built-in and entry-point plugins (trusted by provenance; enable those with `[plugins] enabled`), plugins with no executable surface, and an unreadable grant store. Never writes `policy.toml`.

JSON: [`plugins --json`](#plugins---json).

### `adapters`

Lists the registered coding-CLI adapter kinds and which profiles select each.

```text
bmad-loop adapters [--project PROJECT]
```

- Prints `NAME ORIGIN NEEDS MUX PROFILES`. ORIGIN is `builtin` or `external`. A profile's `adapter` field selects its kind (default `generic`).
- Warns on stderr about profiles naming an unregistered kind and about external packages that failed to load; exits 0. A malformed project profile overlay exits 1.

### `probe-adapter`

Collects and sanitizes the data needed to finalize a coding-CLI profile. Alias: `collect-adapter-data`. See the [adapter authoring guide](../adapter-authoring-guide.md).

```text
bmad-loop probe-adapter [--project PROJECT] [--probe] [--transcript TRANSCRIPT] [--session-dir SESSION_DIR]
                        [--binary BINARY] [--model MODEL] [--workspace PATH] [--timeout TIMEOUT] [--out OUT] [--json] cli
```

| Option                      | Description                                                                                                                               |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `cli`                       | Profile name. Hookless (HTTP) profiles are refused.                                                                                       |
| `--probe`                   | Live capture: one trivial turn in a temp workspace, recording only the shape of each hook payload. Default: a scan that launches nothing. |
| `--transcript TRANSCRIPT`   | Transcript file to inspect; skips discovery.                                                                                              |
| `--session-dir SESSION_DIR` | Directory searched for the newest transcript.                                                                                             |
| `--binary BINARY`           | Binary for a CLI with no profile; produces a reduced report.                                                                              |
| `--model MODEL`             | Model for the probe turn (`--probe` only).                                                                                                |
| `--workspace PATH`          | Also report whether PATH is in the profile's `[workspace_trust]` list; exit 1 unless trusted or when the profile declares no such list.   |
| `--timeout TIMEOUT`         | Probe turn timeout in seconds. Default: 90.                                                                                               |
| `--out OUT`                 | Write the report (or JSON document) to this file instead of stdout.                                                                       |
| `--json`                    | Emit a JSON document instead of the Markdown report.                                                                                      |

- Exits 1 when the profile is unknown (without `--binary`), the leak self-check fires (nothing is written), or hook or workspace trust is not `trusted` (the report is still written).

JSON: [`probe-adapter --json`](#probe-adapter---json).

### `run`

Runs the story loop (dev → review → verify → commit) over actionable stories from `sprint-status.yaml`, or from `stories.yaml` in stories mode.

```text
bmad-loop run [--project PROJECT] [--spec FOLDER] [--epic EPIC] [--story STORY] [--max-stories MAX_STORIES] [--dry-run]
```

| Option                      | Description                                                                                 |
| --------------------------- | ------------------------------------------------------------------------------------------- |
| `--spec FOLDER`             | Stories mode: dispatch from `FOLDER/stories.yaml`. Overrides `[stories] source`.            |
| `--epic EPIC`               | Only stories of this epic (integer). Sprint mode only; ignored with a note in stories mode. |
| `--story STORY`             | Sprint mode: `E-S` / `E.S`, a slug fragment, or a full key. Stories mode: a story id.       |
| `--max-stories MAX_STORIES` | Stop after N stories.                                                                       |
| `--dry-run`                 | Print the per-story plan and spawn nothing.                                                 |

- Sprint mode selects stories at `backlog` or `ready-for-dev`. A `--story` that matches nothing, matches several stories, or is not actionable exits 1.
- Refused with exit 1 before anything is created on: git below the supported floor, a dirty worktree, a worktree-isolation conflict, missing base skills, or a failing plugin-trust verdict.
- `[stories] source = "tickets"` is refused with exit 1, `--dry-run` included: tickets runs are not available yet. `validate` checks a tickets project.
- Prints `run <id> starting (attach: bmad-loop attach)`, then the run summary; exits 0 once the engine has run.

### `sweep`

Triages open entries in the deferred-work ledger and executes them as bundles through the normal pipeline. With `--archive`, moves closed entries to an archive file instead.

```text
bmad-loop sweep [--project PROJECT] [--no-prompt] [--decisions-only] [--max-bundles MAX_BUNDLES]
                [--only DW-ID,... | --min-severity {low,medium,high,critical}] [--repeat | --no-repeat]
                [--max-cycles MAX_CYCLES] [--dry-run] [--archive] [--before DATE]
```

| Option                      | Description                                                                                     |
| --------------------------- | ----------------------------------------------------------------------------------------------- |
| `--no-prompt`               | Skip decision prompts; run only decision-free bundles.                                          |
| `--decisions-only`          | Triage and record decisions; run no bundles.                                                    |
| `--max-bundles MAX_BUNDLES` | Override `[sweep] max_bundles`.                                                                 |
| `--only DW-ID,...`          | Triage only these open entries (comma-separated). Exclusive with `--min-severity`.              |
| `--min-severity {low,…}`    | Triage entries at this severity or higher. Exclusive with `--only`.                             |
| `--repeat`, `--no-repeat`   | Override `[sweep] repeat` (re-triage newly deferred work after each cycle).                     |
| `--max-cycles MAX_CYCLES`   | Override `[sweep] max_cycles`.                                                                  |
| `--dry-run`                 | List open entries (or, with `--archive`, the entries that would move) and write nothing.        |
| `--archive`                 | Move closed entries to `deferred-work-archive.md`, leaving an id-preserving stub in the ledger. |
| `--before DATE`             | With `--archive` only: archive entries closed before this ISO date.                             |

- `--archive` starts no sessions and combines only with `--dry-run` and `--before`. It is refused while any run of the project may be live.
- A normal sweep has the same launch refusals as `run`, plus a missing `bmad-loop-sweep` skill.
- Prints `sweep <id> starting (attach: bmad-loop attach)`, then the summary. Exits 0, except 1 when the engine crashed on an `--only` sweep.

### `resume`

Continues a paused or interrupted run or sweep.

```text
bmad-loop resume [--project PROJECT] [--accept-baseline] run_id
```

| Option              | Description                                                                                                                                          |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--accept-baseline` | For this resume only, adopt the current HEAD as the baseline of every restarted in-place story instead of preserving commits above the old baseline. |

- Refused with exit 1 for a live engine, a finished run, a sweep whose ledger cannot be read, and the same git-floor, isolation, skill and plugin-trust gates as `run`. Does not require a clean worktree.
- Refused with exit 1, before any side effect, for a story run whose work source this version does not know (the run was started by a newer bmad-loop) or cannot run yet. Do not downgrade bmad-loop while a run is in progress.
- Discards any pending stop request and re-reads the policy and config before continuing.

### `resolve`

Resolves an escalated story, then re-arms it and resumes the run. Without a mode flag it launches an interactive resolve agent for a CRITICAL escalation. Procedures: [escalation recovery](../escalations-guide.md#resolve-reverify).

```text
bmad-loop resolve [--project PROJECT] [--story STORY] [--no-interactive]
                  [--restore-patch PATH | --adopt-branch | --reverify | --abandon] [--resume | --no-resume] [--force] run_id
```

| Option                     | Description                                                                                                                                  |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `--story STORY`            | Story to resolve. Default: the run's paused story.                                                                                           |
| `--no-interactive`         | Skip the resolve agent (the spec was fixed by hand), then re-arm and resume. With `--abandon`: skip the confirm prompt.                      |
| `--restore-patch PATH`     | Re-arm the spec to `in-review` and re-apply this saved patch, so review resumes on the attempted change.                                     |
| `--adopt-branch`           | Commit and merge the kept worktree branch as-is and mark the story done. Worktree isolation only; not for sweeps.                            |
| `--reverify`               | Keep a DEFERRED (or environment-fault ESCALATED) story's attempt, replay `[verify]`, then review and commit. No dev session; not for sweeps. |
| `--abandon`                | Close an escalated replay run unmerged: park dirty work on a preserve ref, remove the worktree, keep the branch.                             |
| `--resume` / `--no-resume` | Resume without prompting / only re-arm. Default: prompt, then resume. Not with `--abandon` (exit 2).                                         |
| `--force`                  | Proceed when engine liveness cannot be verified. A provably live engine still blocks.                                                        |

- `--restore-patch`, `--adopt-branch`, `--reverify` and `--abandon` are mutually exclusive.
- `--reverify --story KEY` on a finished sprint run creates a new replay run for a kept DEFERRED worktree unit; the finished run is not modified.
- Refuses with exit 1, leaving the run unchanged, when the run is not paused at an escalation (`--reverify --story` accepts any pause), the engine is live, there is no escalated story, or a DEFERRED story is targeted without `--reverify`. Declining the confirm prompt exits 0 and leaves the run paused.

### `decisions`

Answers deferred-work decisions that earlier sweeps left unanswered.

```text
bmad-loop decisions [--project PROJECT] [--list] [--json]
```

| Option   | Description                                                        |
| -------- | ------------------------------------------------------------------ |
| `--list` | List pending decisions with their options and the recommended one. |
| `--json` | Emit the pending decisions as one JSON document. Implies `--list`. |

- Prompts once per decision. `close` marks the ledger entry done immediately; `build` and `keep-open` are saved to `.bmad-loop/decisions.json` for the next sweep.
- Exits 1 if an answer fails to record. Unreadable ledger or run directories produce a `note:` on stderr and exit 0.

JSON: [`decisions --json`](#decisions---json).

### `confirm`

Completes a story parked at `awaiting-operator` once the external actions it lists are done. Nothing is re-driven.

```text
bmad-loop confirm [--project PROJECT] [--list] [--yes] [--reverify] [--json] [story_key]
```

| Option       | Description                                                                                                                                                                |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `story_key`  | Parked story to confirm.                                                                                                                                                   |
| `--list`     | List parked stories and their owed actions.                                                                                                                                |
| `--yes`      | Skip the per-action acknowledgment prompt.                                                                                                                                 |
| `--reverify` | After the acknowledgments, run the environment probes, then the `[verify]` commands; any failure blocks (exit 1). Nothing runs when no `[verify] commands` are configured. |
| `--json`     | Emit the parked set as one JSON document. Implies `--list`.                                                                                                                |

- After each action is acknowledged, appends an `## Operator Confirmation` section to the spec, sets the spec and board to `done`, removes the park record, and commits the result (best-effort). Declining any action changes nothing and exits 0.
- Exits 2 without a `story_key`, `--list` or `--json`. Exits 1 when the story is not parked in this checkout or its park record disagrees with the committed state.

JSON: [`confirm --json`](#confirm---json).

### `list`

Lists every run and sweep. Alias: `ls`.

```text
bmad-loop list [--project PROJECT] [--json]
```

- Prints `REF TYPE STATUS RUN ID`, oldest first. Status is `running`, `paused`, `finished`, `stopped`, `crashed`, `interrupted` or `unknown` (see [derived status](run-state.md#derived-run-status)).
- Prints `no runs found` and exits 0 when there are none. A run whose `state.json` cannot be read is listed with TYPE `?` and status `unknown`. When the run listing itself is incomplete, stderr gets `warning: run listing incomplete: …`.

JSON: [`list --json`](#list---json).

### `status`

Shows a run's state: status line, token totals, one row per story (phase, dev and review counts, tokens, commit or defer reason, preserve ref), the sprint backlog remaining (stories mode: the stories done count), and the count of pending decisions.

```text
bmad-loop status [--project PROJECT] [--json] [run_id]
```

- The status line (`status: …`) is `finished`, `PAUSED (<stage>) — <reason>`, `in progress — graceful stop pending (will stop after the current item)`, or `in progress (or interrupted)`.
- Exits 1 with `no runs found`, or on a bad or ambiguous ref (stdout stays empty under `--json`).

JSON: [`status --json`](#status---json).

### `diagnose`

Writes a sanitized diagnostic dump of a run or sweep to hand to maintainers. Alias: `diag`.

```text
bmad-loop diagnose [--project PROJECT] [--all] [--out OUT] [--json] [--max-journal-entries N] [run_id]
```

| Option                    | Description                                                                   |
| ------------------------- | ----------------------------------------------------------------------------- |
| `--all`                   | Dump every run in the project (`run_id` is ignored).                          |
| `--out OUT`               | Write the report or JSON document to this file instead of stdout.             |
| `--json`                  | Emit JSON instead of the Markdown report.                                     |
| `--max-journal-entries N` | Maximum scrubbed journal entries per run; `0` = histogram only. Default: 200. |

- Identifiers are pseudonymized and the output is re-scanned before it is written. If the leak self-check fires, it prints `FAIL: refusing to emit …` on stderr, writes nothing, and exits 1.
- With `--out`, the `ok: …` confirmation goes to stdout in text mode and to stderr under `--json`.

JSON: [`diagnose --json`](#diagnose---json).

### `attach`

Attaches the terminal to a run through the selected multiplexer backend.

```text
bmad-loop attach [--project PROJECT] [run_id]
```

- Attaches to the run's control window when a sweep is waiting on a decision or no agent session is live; otherwise to the live agent session.
- Exits 1 with `nothing to attach for run <id>` when there is no target; otherwise exits with the multiplexer client's exit code.

### `stop`

Stops a run. The default is a hard stop. See [stopping a run](run-state.md#stopping-a-run).

```text
bmad-loop stop [--project PROJECT] [--graceful | --cancel-graceful] run_id
```

| Option              | Description                                                                                                   |
| ------------------- | ------------------------------------------------------------------------------------------------------------- |
| `--graceful`        | Let the in-flight story or bundle finish through commit, then stop resumably. Suppresses pending auto-sweeps. |
| `--cancel-graceful` | Withdraw a pending stop request of either mode.                                                               |

- Hard stop: exits 0 once the engine is stopped, or marks a non-finished run with a dead engine `stopped`. A finished run prints `already finished` and exits 1. Exits 1 if the engine's process identity cannot be verified.
- `--graceful` exits 0 when the request is lodged or one is already pending; exits 1 when the run is finished or its engine is provably dead (use `resume`).
- `--cancel-graceful` exits 1 when nothing was pending.

### `delete`

Permanently removes a run directory and its out-of-tree control-plane directory.

```text
bmad-loop delete [--project PROJECT] [--force] run_id
```

| Option    | Description                                                                                                                        |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `--force` | Hard-stop a live engine first (this ends its agent session), and skip the live-agent-session guard (skipping it kills no session). |

- Without `--force`, refuses (exit 1) when the engine is provably alive or an agent session that may belong to the project is live. Unverifiable liveness only warns.
- Also prunes git worktree entries for worktrees the run held.

### `archive`

Compresses a run into `.bmad-loop/archive/<run-id>.tar.gz`, then removes the run directory and its control-plane directory. To archive the deferred-work ledger, use `sweep --archive`.

```text
bmad-loop archive [--project PROJECT] [--force] run_id
```

- `--force` and the refusals are the same as `delete`. The tarball holds the in-tree run directory only.

### `cleanup`

Removes leftover multiplexer sessions and parked control windows for this project's runs. Scope: [cleanup vs clean](run-state.md#reclaiming-sessions-and-disk).

```text
bmad-loop cleanup [--project PROJECT] [--dry-run] [--json]
```

| Option      | Description                               |
| ----------- | ----------------------------------------- |
| `--dry-run` | List what would be removed; kill nothing. |
| `--json`    | Emit one JSON document instead of text.   |

- Always exits 0. Scan failures and kills that could not be verified are reported on stderr (or in the JSON document).

JSON: [`cleanup --json`](#cleanup---json).

### `clean`

Reclaims disk from concluded runs according to `[cleanup]`. Scope: [cleanup vs clean](run-state.md#reclaiming-sessions-and-disk).

```text
bmad-loop clean [--project PROJECT] [--dry-run] [--keep RUN_ID] [--retain N] [--hard] [--json]
```

| Option          | Description                                                                                                                                                                 |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--dry-run`     | List what would be reclaimed; remove nothing.                                                                                                                               |
| `--keep RUN_ID` | Never touch this run (full id, exact match). Repeatable.                                                                                                                    |
| `--retain N`    | Keep the newest N concluded runs out of archive/delete (still trimmed when `[cleanup] trim_artifacts` is on). Overrides `[cleanup] run_retention`. `0` keeps none by count. |
| `--hard`        | Delete runs past retention instead of archiving them.                                                                                                                       |
| `--json`        | Emit one JSON document instead of text.                                                                                                                                     |

- Exits 0 once the policy loads; per-run faults are warnings on stderr or fields in the JSON document.

JSON: [`clean --json`](#clean---json).

### `tui`

Opens the interactive dashboard. Needs the `tui` extra (`uv tool install 'bmad-loop[tui]'`); without it, prints an install hint and exits 1. See the [TUI guide](../tui-guide.md).

```text
bmad-loop tui [--project PROJECT] [--low-frame-rate]
```

| Option             | Description                                                                                                                                                              |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--low-frame-rate` | Cap rendering at 15 fps and turn off animations (for slow or SSH links). `[tui] low_frame_rate` does the same. Existing `TEXTUAL_FPS` / `TEXTUAL_ANIMATIONS` values win. |

### `relay`

Writes one session event file from a coding-CLI hook payload on stdin. A hook target that `init` registers, not a command to run by hand.

```text
bmad-loop relay [event]
```

- `event` is the canonical event name (`Stop`, `SessionStart`, …); default `Unknown`.
- Does nothing unless `BMAD_LOOP_RUN_DIR` and `BMAD_LOOP_TASK_ID` are set (the session was launched by bmad-loop). Writes the event to `$BMAD_LOOP_EVENTS_DIR` (default `$BMAD_LOOP_RUN_DIR/events`).
- Always exits 0 and never writes to stdout. Takes no `--project` and never reads policy.

## Machine-readable output (`--json`)

- **stdout** carries exactly one JSON object and nothing else.
- **`schema_version`** is a top-level integer in every document, versioned per command. Changes are additive: a new field may appear without a bump; removing or renaming a field, or changing a field's type or meaning, bumps that command's version.
- **Errors** produce no document: the message goes to stderr, stdout stays empty, and the exit code is nonzero.
- **Verdict commands** emit the full document and also exit nonzero to carry their answer: `validate` (read `ok`) and `probe-adapter` (read `hook_trust` / `workspace_trust`). Parse non-empty stdout whatever the exit code.
- **stderr** carries notes, warnings and `ok:` confirmations. Warnings that text mode prints become document fields in JSON mode where the schema has one.
- **`--out FILE`** (`diagnose`, `probe-adapter`) writes the same bytes `--json > FILE` would; stdout is then empty, and the exit code still carries any verdict.
- `diagnose` and `probe-adapter` documents have sorted keys and pass the leak guard; an optional top-level `backstop_repairs` appears when the guard pseudonymized a stray value.

| Command         | `schema_version` |
| --------------- | ---------------- |
| `validate`      | 1                |
| `plugins`       | 1                |
| `probe-adapter` | 2                |
| `decisions`     | 1                |
| `confirm`       | 1                |
| `list`          | 1                |
| `status`        | 1                |
| `diagnose`      | 4                |
| `cleanup`       | 2                |
| `clean`         | 1                |

### `validate --json`

| Field         | Type   | Meaning                                                                                        |
| ------------- | ------ | ---------------------------------------------------------------------------------------------- |
| `ok`          | bool   | no finding has severity `problem`; matches the exit code                                       |
| `mode`        | str    | the work source: `sprint`, `stories` or `tickets` — an open enum                               |
| `spec_folder` | str    | stories spec folder; `""` in the other modes                                                   |
| `initiative`  | str    | the BMAD v7 active initiative tickets mode drives; `""` in the other modes                     |
| `era`         | str    | the BMAD config shape the project resolved as: `v6` or `v7`; `""` when the config did not load |
| `counts`      | object | `{ok, warning, problem}` counts                                                                |
| `findings`    | array  | one entry per finding, in the order the checks ran                                             |

`mode` is an open enum: a new work source adds a value without a `schema_version` bump, and an existing value never changes meaning. Treat an unknown value as "another mode", never as sprint.

Each finding has `check` (the stable [check id](#validate-checks) — the only field safe to match on), `severity` (`ok` / `warning` / `problem`), `message` (free text), and `detail` (check-specific object, or null).

### `plugins --json`

| Field         | Type        | Meaning                                                                    |
| ------------- | ----------- | -------------------------------------------------------------------------- |
| `plugins`     | array       | one row per discovered plugin, plus one per grant whose plugin is gone     |
| `store_fault` | str \| null | why the grant store could not be read (every row is then judged ungranted) |

Each row: `name`, `origin`, `path` (POSIX path from the code root), `surface` (`{hooks, workflows, python, seeds}` bools), `grant_required`, `status` (the GRANT values above), `enabled` (listed in `[plugins] enabled`), `active` (a launch would run it), `pinned_digest`, `current_digest`, `changed` (list of paths).

### `probe-adapter --json`

| Field                           | Type           | Meaning                                                         |
| ------------------------------- | -------------- | --------------------------------------------------------------- |
| `cli`, `binary`                 | str            | probed CLI and binary                                           |
| `mode`                          | str            | `scan` or `probe`                                               |
| `known_profile`, `binary_found` | bool           | a packaged profile exists; the binary resolved                  |
| `dialect`                       | str \| null    | hook dialect                                                    |
| `usage_parser`                  | str            | usage parser (`none` without a known profile)                   |
| `hooks_registered`              | bool \| null   | relay hooks are present                                         |
| `hook_trust`, `workspace_trust` | str \| null    | `trusted`, `untrusted` or `unverifiable`                        |
| `declared_events`               | object         | native event → canonical event, as the profile declares         |
| `version`, `help`               | str \| null    | the CLI's own `--version` / `--help` output                     |
| `captured_events`               | array          | `{native_event, canonical_event, payload_keys, payload_schema}` |
| `transcript`                    | object \| null | location, format and size of the discovered transcript          |
| `tokens`                        | object \| null | token-schema inference                                          |
| `warnings`, `next_steps`        | list[str]      | advisory text                                                   |

### `decisions --json`

`decisions`: pending decisions in ledger order. Each has `id`, `question`, `context`, `recommendation`, and `options[]`; each option has `key`, `label`, `effect`, `intent`, `resolution`, `bundle_name`, and `recommended` (true when `key == recommendation`).

### `confirm --json`

`parked`: stories at `awaiting-operator`, in story-key order. Each has `story_key`, `actions` (list of str), `spec_file`, `spec_status`, `board_status`, `commit` (`""` until a commit carries the park record), `run_id`, `parked_at`, `confirmable`, `confirmation_recorded`, `resumable` (an interrupted confirmation `confirm` will finish), and `drift` (human-readable).

### `list --json`

| Field           | Type        | Meaning                                                                  |
| --------------- | ----------- | ------------------------------------------------------------------------ |
| `runs`          | array       | one entry per run, oldest first                                          |
| `listing_fault` | str \| null | what could not be read; when set, an empty `runs` means "could not list" |

Each run: `ref`, `run_id`, `run_type` (`?` when `state.json` is unreadable), `started_at`, `status`, `paused_stage` (`""` unless paused).

### `status --json`

Built from the run's `state.json`.

| Field                                               | Type           | Meaning                                                                |
| --------------------------------------------------- | -------------- | ---------------------------------------------------------------------- |
| `run_id`, `run_type`, `source`, `started_at`        | str            | run identity                                                           |
| `status`                                            | str            | `finished`, `paused`, `crashed`, `stopped` or `in-progress`            |
| `finished`, `stopped`, `crashed`                    | bool           | raw flags                                                              |
| `graceful_stop_pending`                             | bool           | a graceful stop request is pending and the engine is not dead          |
| `crash_error`                                       | str \| null    | crash message                                                          |
| `paused_stage`, `paused_reason`, `paused_story_key` | str \| null    | pause context                                                          |
| `cache_read_weight`                                 | number         | cache-read token weight from the run's policy snapshot                 |
| `tokens`                                            | object         | `{raw, weighted}` run totals                                           |
| `adapters`                                          | object \| null | `{dev, review, triage, retro: {name, model}}` from the policy snapshot |
| `sweeps_refused`                                    | object         | auto-sweep trigger → refusal reason; `{}` when none                    |
| `tasks`                                             | array          | one entry per story                                                    |

Each task: `story_key`, `epic`, `phase`, `attempt`, `review_cycle`, `tokens` (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `raw`, `weighted`), `commit_sha`, `defer_reason`, `preserve_ref`, `adapters_used` (`{role: {name, model}}`).

### `diagnose --json`

| Field          | Type   | Meaning                                                                                                  |
| -------------- | ------ | -------------------------------------------------------------------------------------------------------- |
| `generated_at` | str    | timestamp                                                                                                |
| `tool_version` | str    | bmad-loop version                                                                                        |
| `env`          | object | OS, Python, package, multiplexer, tmux and git versions                                                  |
| `runs`         | array  | per run: identity, flags, policy, phase histogram, token totals, `tasks`, `journal`, `files`, `warnings` |

### `cleanup --json`

The `--dry-run` plan and the outcome share this shape.

| Field         | Type   | Meaning                        |
| ------------- | ------ | ------------------------------ |
| `dry_run`     | bool   | plan (true) or outcome (false) |
| `sessions`    | object | agent sessions (below)         |
| `ctl_windows` | object | control windows (below)        |

`sessions`: `removed` (run ids whose session kill was attempted), `live` (left alone), `unverifiable_pid` (subset of `removed` whose engine liveness could not be proven), `legacy_leftovers`, `legacy_kill_commands` (a pre-upgrade control session in this project's registry mapped to the command that ends it, `null` when the backend is not psmux; a session a run may own is left out), `legacy_unverified`, `superseded_ctl_unverified` (a check on this project's own registry for such a session, or one that withholds its command, could not be made), and `scan_error` (present only when the scan failed).

`ctl_windows`: `removed` (verified gone, or would close under `--dry-run`), `survived`, `unverifiable` (kill outcome could not be checked), `undetermined` (kept because the window's state could not be read), `scan_error` (str or null; when set, the other lists mean "no answer").

### `clean --json`

The `--dry-run` plan and the outcome share this shape.

| Field                                         | Type        | Meaning                                                                    |
| --------------------------------------------- | ----------- | -------------------------------------------------------------------------- |
| `dry_run`                                     | bool        | plan (true) or outcome (false)                                             |
| `policy`                                      | object      | effective `retain`, plus `retention_days`, `archive_old`, `trim_artifacts` |
| `freed_bytes`                                 | int         | estimated bytes reclaimed                                                  |
| `worktrees`                                   | list[str]   | worktree paths torn down                                                   |
| `trimmed`, `archived`, `deleted`, `protected` | list[str]   | run ids                                                                    |
| `unverifiable_pid`                            | list[str]   | touched runs whose engine liveness could not be proven                     |
| `state_dirs_swept`                            | int         | orphaned out-of-tree state directories reclaimed                           |
| `listing_fault`, `state_dir_fault`            | str \| null | what could not be read                                                     |
| `worktree_faults`                             | list[str]   | `"<run id>: <fault>"` per run whose worktree teardown was incomplete       |

## Exit codes

| Code | Name          | Meaning                                                                                                                                                                                                                |
| ---- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | `OK`          | Success. On POSIX, Ctrl+C during a run's engine loop ends the run as a resumable `stopped` run and also exits 0.                                                                                                       |
| 1    | `FAILURE`     | A command failed or refused, an unexpected error was caught (`error: …` on stderr), or a verdict command (`validate`, `probe-adapter`) reported a failing verdict.                                                     |
| 2    | `USAGE`       | The invocation was malformed: rejected by argument parsing, or a flag combination a command refuses (`plugins <action> --json`, `resolve --abandon --resume`, `confirm` with no story key and no `--list` / `--json`). |
| 130  | `INTERRUPTED` | Ctrl+C outside a run's engine loop (config load, setup, a non-run command). Prints `interrupted` on stderr.                                                                                                            |

Codes 3–129 and 131 and above are unallocated.

## validate checks

Every finding's `check` is one of these ids. Severity is `ok`, `warning` or `problem`; only `problem` fails `validate`.

| Check id                               | Severity               | What it checks                                                                                                                    |
| -------------------------------------- | ---------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `bmad-config`                          | ok / problem           | BMAD config loads and artifact paths resolve; on BMAD v7 the ok line names the era and the active initiative                      |
| `bmad-config.initiative-unset`         | warning                | BMAD v7 with no `[core] active_initiative`: artifacts resolve to the output folder                                                |
| `bmad-config.v6-keys-ignored`          | warning                | a mixed install resolved as BMAD v7 for tickets mode, ignoring leftover v6 artifact keys                                          |
| `policy`                               | ok / problem           | `.bmad-loop/policy.toml` loads                                                                                                    |
| `policy.isolation-repo-root`           | problem                | `isolation = "worktree"` with the project outside the code root                                                                   |
| `policy.isolation-shared-artifact-dir` | warning                | under worktree isolation, an artifact dir shared with the main checkout                                                           |
| `policy.model-qualified`               | warning                | an opencode-http role model that is not `provider/model`                                                                          |
| `policy.extra-args-unservable`         | warning                | an opencode-http role's `extra_args` repeats an adapter-owned serve flag                                                          |
| `policy.effort-unsupported`            | warning                | `effort` set on a profile whose adapter ignores it                                                                                |
| `policy.bypass-dropped`                | warning                | an explicit `extra_args` drops the profile's `bypass_args`                                                                        |
| `adapter.profile`                      | problem                | a role's adapter name does not resolve to a loadable profile                                                                      |
| `adapter.binary`                       | ok / problem           | the profile binary is on `PATH`                                                                                                   |
| `adapter.binary-unrunnable`            | warning                | a packaged profile's binary is on `PATH` but `--version` fails                                                                    |
| `adapter.httpx`                        | ok / problem           | `httpx` is importable for an opencode-http profile                                                                                |
| `adapter.launch-args-unservable`       | warning                | an opencode-http profile's `launch_args` cannot form a valid serve command                                                        |
| `adapter.opencode-version`             | ok / warning           | the OpenCode server API an opencode-http binary's `--version` reports is supported                                                |
| `adapter.hookless`                     | ok                     | the profile is hookless, so hook checks are skipped                                                                               |
| `adapter.kind`                         | ok / problem           | the profile's `adapter` kind is registered                                                                                        |
| `adapter.external`                     | warning                | a third-party adapter entry point failed to load                                                                                  |
| `adapter.external-profile`             | warning                | a third-party profile entry point failed to load                                                                                  |
| `hooks.config-parse`                   | problem                | the hook config file is not valid JSON or has malformed handlers                                                                  |
| `hooks.registered`                     | ok / problem           | relay hooks are registered for the profile's events                                                                               |
| `hooks.trust`                          | ok / problem           | Codex project hook trust is current                                                                                               |
| `hooks.relay-path-unsafe`              | warning                | a registered relay path has shell metacharacters the host's quoting leaves unsafe                                                 |
| `hooks.relay-present`                  | ok / problem           | each registered relay executable exists and is usable                                                                             |
| `hooks.relay-stale`                    | warning                | a registered relay path differs from this installation's relay                                                                    |
| `queue.sprint-status`                  | ok / problem           | sprint mode: `sprint-status.yaml` loads; counts actionable stories                                                                |
| `queue.sprint-status-unknown-keys`     | warning                | sprint mode: unknown keys in `sprint-status.yaml` were ignored                                                                    |
| `queue.nested-specs`                   | warning                | sprint mode: spec-like files in artifact subdirectories, or unreadable subdirectories                                             |
| `queue.stories-manifest`               | ok / problem           | stories mode: `stories.yaml` and `SPEC.md` are valid                                                                              |
| `git.worktree-clean`                   | ok / problem           | the code root's worktree is clean                                                                                                 |
| `git.probe`                            | problem                | the git check itself failed (spawn, timeout, error)                                                                               |
| `git.version`                          | ok / problem           | host git meets the supported floor                                                                                                |
| `git.render-tracked`                   | warning                | the rendered-skill output dir is tracked by git                                                                                   |
| `deferred.ledger-unreadable`           | problem                | the deferred-work ledger cannot be read                                                                                           |
| `deferred.ledger-untracked`            | warning                | the ledger is neither committed nor gitignored                                                                                    |
| `deferred.ledger-ignored-isolated`     | warning                | the ledger is gitignored under worktree isolation, so session writes are lost                                                     |
| `deferred.hard-gate`                   | ok / problem           | an actionable story is gated (`gate:`) by a ledger entry not marked `done`                                                        |
| `deferred.hard-gate-unstructured`      | warning                | a not-done entry declares a gate `validate` cannot enforce (malformed, empty or misspelled `gate:`, or a prose-only `HARD GATE:`) |
| `deferred.closes-malformed`            | warning                | a spec's `closes_deferred` has an unreadable shape                                                                                |
| `deferred.closes-entry-unreadable`     | warning                | `closes_deferred` names entries that are neither open nor done                                                                    |
| `deferred.closes-unknown`              | warning                | `closes_deferred` names ids not in the ledger                                                                                     |
| `operator.actions-malformed`           | warning                | sprint mode: a parked story's `operator_actions` declares nothing readable                                                        |
| `operator.confirm-interrupted`         | warning                | sprint mode: a confirmation signed the spec but never advanced the board                                                          |
| `operator.registry-stale`              | warning                | sprint mode: a park record disagrees with the spec or board, so `confirm` will refuse it                                          |
| `operator.park-record-missing`         | warning                | sprint mode: the board parks a story that no park record claims                                                                   |
| `plugins.manifests`                    | ok / warning / problem | every `plugin.toml` parses (problem); unsupported manifests are skipped (warning)                                                 |
| `plugins.trust`                        | ok / problem           | project plugins are granted and unchanged, and the grant store is readable                                                        |
| `mux.backend`                          | ok / problem           | the selected multiplexer backend is available                                                                                     |
| `mux.preflight`                        | problem                | multiplexer selection or readiness raised an error                                                                                |
| `mux.backends-detected`                | ok / warning           | inventory of detected backends when more than one is registered; detection failure (warning)                                      |
| `mux.backend-probe`                    | warning                | a registered backend's platform or availability probe raised an error                                                             |
| `mux.selection`                        | ok / warning           | why the backend was selected; `fallback` is a warning                                                                             |
| `mux.external-backend`                 | warning                | a third-party multiplexer backend entry point failed to load                                                                      |
| `host.process`                         | ok / problem           | the process host resolves; an unknown `BMAD_LOOP_PROCESS_HOST` is a problem                                                       |
| `host.win32-on-wsl-path`               | warning                | a Windows interpreter is working on a WSL UNC project path                                                                        |
| `notify.desktop-unavailable`           | warning                | `[notify] desktop` is on but the platform has no desktop notifier                                                                 |
| `environment.probes-not-run`           | ok                     | probes are configured but `--probes` was not passed                                                                               |
| `environment.probes-none`              | ok                     | `--probes` was passed with no probes configured                                                                                   |
| `environment.probe`                    | ok / problem           | one per probe under `--probes`; `detail.status` is `pass`, `fail`, `timeout` or `interrupted`                                     |
| `skills.base`                          | ok                     | the dev primitive and review layers are present                                                                                   |
| `skills.base-missing`                  | problem                | the dev primitive or a review-layer skill is not installed                                                                        |
| `skills.base-incomplete`               | problem                | the dev primitive is missing marker files                                                                                         |
| `skills.base-shim`                     | problem                | the legacy dev primitive is unusable and the new one is absent                                                                    |
| `skills.dev-renderer`                  | problem                | a renderer-stub primitive lacks its renderer script                                                                               |
| `skills.dev-renderer-sources`          | problem                | a renderer stub's render sources are unresolved                                                                                   |
| `skills.dev-renderer-config`           | problem                | a renderer stub's central config layer is missing                                                                                 |
| `skills.dev-render-probe`              | ok / problem           | `--render-probe` only: the stub's render command succeeds in a temp copy                                                          |
| `skills.customize-legacy`              | warning                | customize overrides named for the legacy dev primitive no longer apply                                                            |
| `skills.customize-unreadable`          | warning                | a customize override is not valid TOML                                                                                            |
| `skills.review-layers-empty`           | problem                | the dev primitive has no enabled review layer                                                                                     |
| `skills.review-layer-missing`          | problem                | a referenced review-layer skill is not installed                                                                                  |
| `skills.review-layer-unresolved`       | warning                | a conditionally referenced review-layer skill is not installed                                                                    |
| `skills.stories-dispatch`              | ok                     | stories mode: the dev primitive supports folder+id dispatch                                                                       |
| `skills.stories-dispatch-missing`      | problem                | stories mode: the dispatch probe file is missing                                                                                  |
| `skills.stories-dispatch-stale`        | problem                | stories mode: the dev primitive lacks folder+id dispatch                                                                          |
| `skills.tickets-dispatch`              | ok                     | tickets mode: the dev primitive resolves a ticket from the ticket tree                                                            |
| `skills.tickets-dispatch-missing`      | problem                | tickets mode: the dispatch probe file is missing                                                                                  |
| `skills.tickets-dispatch-stale`        | problem                | tickets mode: the dev primitive predates the ticket tree                                                                          |
| `skills.sweep`                         | ok                     | the `bmad-loop-sweep` skill is present in the triage skill trees                                                                  |
| `skills.sweep-missing`                 | problem                | `bmad-loop-sweep` is not installed in a triage skill tree                                                                         |
| `skills.sweep-incomplete`              | problem                | `bmad-loop-sweep` is missing files                                                                                                |
| `tickets.era`                          | ok                     | tickets mode: names the ticket-tree script, the method module version and the active initiative                                   |
| `tickets.script-missing`               | problem                | tickets mode: `_bmad/method/scripts/tickets.py` is not installed                                                                  |
| `tickets.config-utils-missing`         | problem                | tickets mode: `_bmad/scripts/config_utils.py`, which `tickets.py` reads the config through, is missing                            |
| `tickets.uv-missing`                   | problem                | tickets mode: `uv` is not on PATH (`bmad-build-auto` runs `tickets.py` with `uv run`)                                             |
| `tickets.initiative-unset`             | problem                | tickets mode: no `[core] active_initiative` is set                                                                                |
| `tickets.initiative-missing`           | problem                | tickets mode: the active initiative's folder does not exist                                                                       |
| `tickets.store-not-repo`               | problem                | tickets mode: the ticket store is a tracker, or its config cannot be read; bmad-loop drives repo stores only                      |
| `tickets.capability`                   | ok / problem           | `--probes` in tickets mode: `tickets.py --help` offers every command and `mark` status bmad-loop needs                            |
| `tickets.capability-not-run`           | ok                     | tickets mode without `--probes`: the capability probe did not run                                                                 |

The set is closed (`checks.VALIDATE_CHECKS`); plugins cannot add check ids.
