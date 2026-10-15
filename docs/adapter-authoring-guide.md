# Authoring CLI adapters & profiles

bmad-loop drives any coding CLI that fits the **multiplexer-injection + hook-signal**
transport through one generic adapter (`adapters/generic.py`). Everything
CLI-specific lives in a declarative **TOML profile** (`adapters/profile.py`). This
guide covers the profile schema and the two ways to teach bmad-loop a new CLI:

- **The common case: a TOML profile.** If the CLI runs in a terminal pane and fires
  lifecycle hooks, you write no Python. The [Profile field reference](#profile-field-reference)
  is the complete schema; the [walkthrough](#walkthrough-finalizing-a-profile) shows how
  `probe-adapter` finalizes a profile against a real run.
- **The advanced case: a new adapter class.** If the CLI does _not_ fit that transport
  (an HTTP/SSE service, say), see [Writing a new adapter class](#writing-a-new-adapter-class)
  and [Shipping a new adapter class out-of-tree](#shipping-a-new-adapter-class-out-of-tree).

## Two axes: CLI vs transport

The two axes are independent:

- **CLI axis:** `CodingCLIAdapter` (`adapters/base.py`) decides which binary to launch, how
  the prompt is rendered, the hook dialect, and where the transcript lives. The generic
  adapter plus a TOML profile covers the common case. A CLI that needs its own class
  registers one with `register_adapter(...)` (`adapters/registry.py`), selected by the
  profile's `adapter` field. Most of this guide is about this axis.
- **Transport axis:** `TerminalMultiplexer` (`adapters/multiplexer.py`) creates, observes
  and tears down sessions, windows and panes. Adapters never shell out themselves; they go
  through `self.mux`, obtained from `get_multiplexer()`. Only `adapters/tmux_base.py`
  (`BaseTmuxBackend`) and its POSIX leaf `adapters/tmux_backend.py` (`TmuxMultiplexer`)
  invoke `tmux`. The native-Windows leaf `adapters/psmux_backend.py` drives the `psmux`
  binary through the same base. Transport is one of four OS seams; registration, the two
  build paths and the other seams are in [Porting bmad-loop to a new OS](porting-to-a-new-os.md).
  Operators choosing a backend read [Terminal multiplexer backends](multiplexer-backends.md).

### The transport contract for a backend author

A backend subclasses `TerminalMultiplexer` and registers with
`register_multiplexer(name, matches, factory)`. How to register, ship out-of-tree, and
choose between extending `BaseTmuxBackend` and implementing fresh is in the
[porting guide](porting-to-a-new-os.md#shipping-out-of-tree-the-bmad_loopmux_backends-entry-point);
same-platform backends need
[discriminating `available()` probes](porting-to-a-new-os.md#availability-discriminators-same-platform-backends).
`tmux_base.py` is the reference implementation.

Rules that span methods:

- **Failures.** Raise a `MultiplexerError` subclass, so call sites catch the seam type
  without importing the backend. Each method below states whether it raises, tolerates, or
  answers a sentinel.
- **Targets.** Target-taking methods receive either the seam token `=session[:window]`,
  built by `target()`, or a native id your backend minted. `parse_target()` decodes a token
  to `(session, window)` and answers `None` for a native id, which you use as-is. Core never
  builds the grammar by hand.
- **Qualified ids.** If a minting method returns anything other than a bare native id,
  every method whose output core compares that id against must emit the identical form, and
  every verb the id is replayed through must accept it. The pairings: `new_window` ↔
  `list_window_ids`; `new_parked_window` ↔ the `list_windows` `window_id` column; that column
  ↔ `list_window_ids` ↔ `current_window_id`. A verb that cannot take the form translates
  inside itself.
- **Client verbs report effect, not dispatch.** `detach_client` and `switch_client` answer
  what actually happened to the client. A vacuous `True` is never allowed.

**Sessions**

- `has_session(name)`: true iff the session exists. A failed lookup answers `False`; a transport failure raises.
- `new_session(name, cwd, cols=None, lines=None)`: create a detached session with one shell window; pin the geometry when given.
- `kill_session(name)`: tolerate the session already being gone.
- `list_sessions()`: names of all live sessions. On a failed listing, raise, or answer `[]` and override `list_sessions_reporting`.
- `list_sessions_reporting(*, on_fault=None)` (default: `list_sessions()`): a backend that folds a failed listing to `[]` hands a one-line fault to `on_fault`.
- `session_name_key(name)` (default: identity): the comparison key. Override it if the transport folds names, for example case-insensitively.
- `session_options(option)`: map of session name to that option's value.
- `set_session_option(name, option, value)`: raises on a transport failure. A value the transport cannot carry verbatim may be refused with a warning.

**Windows**

- `new_window(session, name, cwd, env, command)`: run `command` with `env` layered on; return the native window id.
- `new_parked_window(session, name, cwd, argv, return_opt)`: run `argv`, print `PARKED_BANNER`, park on a key, then return any attached client via `return_opt`.
- `list_window_ids(session)`: same id form as `new_window`. Answer `[]` only for an empty listing or a proved-gone session; otherwise raise.
- `list_windows(session, fields)`: one tuple per window in field order; best-effort `[]` on failure.
- `window_alive(session, window_id)`: `True` while the window is listed AND its pane is alive; a window the multiplexer keeps listed after its pane exited (`remain-on-exit on`) is not alive. Membership alone is `list_window_ids`. Raise when liveness cannot be known; never answer `False` for that.
- `kill_window(target)`: tolerate the window already being gone; no-op on a transport failure.
- `select_window(target)`: make the window current; best-effort.
- `set_window_option(target, option, value)` / `unset_window_option(target, option)`: best-effort. An unset option must read back as unset, not empty.
- `show_window_option(target, option)`: the value, or `''` when unset or on failure.
- `pipe_pane(window_id, log_file)`: tee the pane to `log_file`; tolerate a dead window.
- `send_text(window_id, text)`: send `text` literally, then Enter.
- `capture_pane(window_id)` (default: raises `MultiplexerError`): the visible screen as text. Override it to enable `parked_prompt_patterns`.

**Client and attach**

- `attach_target_argv(target)`: argv that attaches the caller's terminal to a target, nesting-aware.
- `current_pane_id()` / `current_window_id()` / `current_session()`: this process's pane, window and session, or `None` outside the multiplexer.
- `current_return_target()` (default: `current_pane_id()`): what an attach records for the parked-window return hop. Override when native ids do not resolve from another session.
- `detach_client()`: `True` only when a client was actually detached. A failure, or a backend with no real detach, answers `False`.
- `switch_client(target, last_fallback=False)`: `True` = switched; `False` = nothing switched and the client is still here; `None` = cannot vouch (timeout, unreadable effect, no client).

**Probes**

- `available()`: can this backend run on this host. Keep it cheap and side-effect-free; a raise reads as unavailable.
- `version()` (default: `None`): one bounded line built with `fold_version()`, identifying version first.
- `version_error()` (default: `None`): why the last `version()` answered `None` although the binary exists. Must not raise.

**Registry namespace** (all default to "no namespace"; rules in the
[porting guide](porting-to-a-new-os.md#registry-namespaces-only-if-your-transport-has-one))

- `has_registry_namespace()` (default: `False`): `True` on every instance if the transport namespaces sessions by registry.
- `registry_root()` (default: `None`): the root the verbs currently resolve through. Must not raise.
- `legacy_registries()` (default: `[]`): independent instances bound to older registries, for the cleanup sweep.

**Other defaults**

- `target(session, window=None)` (default: `=session[:window]`): override only to emit another stable by-name reference, never an eagerly resolved live id.
- `inherited_env(session, name, *, on_fault=None)` (default: `None`): the value a new pane would inherit, `UNSET` when confirmed absent, `None` when unknown. Must not raise.
- `window_pane_pids(target)` (default: `[]`): best-effort pids of the pane's root processes. Must not raise.

The external herdr backend ([pbean/bmad-loop-adapter-herdr](https://github.com/pbean/bmad-loop-adapter-herdr))
is a worked example of a native-id backend: its `_parse_target` resolves the seam token
lazily at use time, and its module docstring lists every place it degrades.

## Collecting CLI facts with `probe-adapter`

The hard part of a new profile is the facts no doc records: the CLI's hook payload shape
(field names and casing; whether `session_id`, `transcript_path` and `cwd` are present),
where it writes its transcript and in what format, and the token-usage schema a
`usage_parser` has to read. `session_id` matters: it is how the generic adapter tells the
launched session's events from a nested CLI's (see
[Session identity and nested CLIs](#session-identity-and-nested-clis)).

`bmad-loop probe-adapter` (alias `collect-adapter-data`) gathers these and runs them
through an audited sanitizer, so anyone can run one command and paste back a clean,
content-free report.

```bash
bmad-loop probe-adapter <cli> --project .          # default: zero-launch scan
bmad-loop probe-adapter <cli> --probe --project .  # opt-in live capture
```

### Two modes

Both modes emit the same sanitized finding: markdown on stdout, or a file with `--out`;
`--json` emits a machine-readable JSON document instead.

#### SCAN (default, no process launch)

Runs `<binary> --version` and `--help`, finds the newest existing session transcript by
convention, reads the declared hook config, and infers the token schema from the
transcript. Use it first for any CLI that already has a profile or that you have run by
hand.

#### PROBE (`--probe`, opt-in live capture)

In an ephemeral `mkdtemp` workspace, `probe` registers a full-payload capture hook for every
native event in the profile, launches **one trivial content-free turn**
(`Reply with exactly: OK`) in a window of the selected multiplexer, captures each hook
event's payload, finds the transcript, and tears everything down. Use it to confirm the
exact hook payload shape and that the CLI accepts the hook dialect your profile declares.

`--probe` needs a known profile. If the multiplexer backend is unavailable or the binary is
missing, it falls back to a scan with a warning.

### PII safety model

The report is built to be safe to paste into an issue or PR. One audited sanitizer
(`src/bmad_loop/sanitize.py`) is the only chokepoint:

- **Hook payloads ship as a schema, never as values.** Per event you get the field names
  and dotted key paths with leaf types (`tool_input.command:str`). A dynamic or
  credential-shaped key collapses to `<key>`.
- **Numbers, booleans and `null` pass through** elsewhere; token counts are not PII.
- **Transcript locations are redacted per component.** Home becomes `~`; anything that is
  not a plain machine identifier, or that embeds your username, becomes `<redacted>`; your
  project directory name becomes a salted alias (`project-3f2a9c…`), as in `diagnose`.
- **`--help` / `--version` text and log tails** have the home dir and emails redacted, with
  a line cap.
- **The rendered report re-scans itself before emitting** (the same `sanitize.guard` egress
  check as `diagnose`). A stray project name is repaired and disclosed. An email, secret,
  home path or username in the final bytes makes the command refuse to emit: a message on
  stderr, empty stdout, nonzero exit, no `--out` file.

In PROBE mode the raw capture exists only inside the temp dir, which is removed in a
`finally` (even on an exception or Ctrl-C). The CLI's own transcript stays in its home dir;
the command reads its structure and never copies it. The hidden `--keep-temp` flag keeps
the raw temp dir for debugging and adds a `RAW probe data retained at … — DO NOT SHARE` warning to the report. Never
paste a `--keep-temp` run.

### Flags reference

| Flag                | Purpose                                                                                                     |
| ------------------- | ----------------------------------------------------------------------------------------------------------- |
| `--probe`           | Opt-in live capture (default is scan). Needs a known profile.                                               |
| `--transcript PATH` | Inspect this exact transcript file, bypassing convention discovery.                                         |
| `--session-dir DIR` | Glob this dir (`**/*.jsonl`, then `**/*.json`; newest wins) — for custom/unknown CLIs.                      |
| `--binary NAME`     | Binary to probe for a CLI that has no profile yet (enables a reduced report).                               |
| `--model NAME`      | Model passed to the probe turn (PROBE mode).                                                                |
| `--workspace PATH`  | Also report (read-only) whether PATH is in the profile's `[workspace_trust]` list; exit `1` unless trusted. |
| `--timeout SECONDS` | Probe turn timeout (default 90).                                                                            |
| `--out FILE`        | Write the output to a file instead of stdout (the only file the command writes).                            |
| `--json`            | Emit a machine-readable JSON document _instead of_ the report (honours `--out`).                            |
| `--keep-temp`       | (hidden, debug) keep the raw probe temp dir — prints a "do not share" warning.                              |

Exit codes mirror `validate`: `0` whenever a report is produced (warnings are fine), `1`
only when nothing could be produced. An unknown CLI with `--binary` still yields a reduced
report (version/help and discovery, no hook events); an unknown CLI without `--binary`
fails and lists the available profiles.

The two trust verdicts are exceptions, because their exit code carries the answer:

- A Codex `hook_trust` other than `trusted` exits `1`.
- `--workspace PATH` exits `0` only when PATH is listed (`workspace trust: trusted`), and
  `1` when it is `untrusted` (file, key or entry missing) or `unverifiable` (malformed
  settings file), each with a warning and a next step. A profile without
  `[workspace_trust]` fails with `FAIL:` on stderr. The path is never rendered. `--json`
  carries the verdict as the `workspace_trust` key (`null` without `--workspace`).

## Walkthrough: finalizing a profile

### 1. Draft a profile

Drop a TOML file in `<project>/.bmad-loop/profiles/<name>.toml` with the fields from the
[Profile field reference](#profile-field-reference). The minimum is a `binary`, a
`prompt_template`, bypass flags, a `[hooks]` block with a dialect and a native→canonical
event map, and a `usage_parser` (start with `"none"`).

### 2. Scan

```bash
bmad-loop probe-adapter <cli> --project .
```

Read three sections of the report:

- **CLI flags:** the scrubbed `--version` / `--help` output, so you can confirm the flags
  your profile declares exist.
- **Transcript:** the redacted location, format, size, line count and modified date of the
  newest transcript the convention glob found.
- **Token usage schema:** structural key paths (types only) and the **token-field
  candidates** (int leaves whose names look token-ish). When a real parser is declared, its
  parsed counts appear as a self-check.

### 3. Probe (confirm the live payload and dialect)

```bash
bmad-loop probe-adapter <cli> --probe --project /tmp/scratch
```

The **Hook payload shape** section shows, per captured event, the native→canonical
pairing, the payload keys and the payload schema, so you can confirm `session_id` /
`transcript_path` casing and that the CLI accepted the hook config. If the CLI rejects the
config or never fires a hook, the report says so, with a scrubbed log tail.

### 4. Write the `usage_parser`

Turn the report's `token_field_candidates` into a parser in
[`src/bmad_loop/tokens.py`](../src/bmad_loop/tokens.py), following the existing ones
(`tally` for claude, `tally_codex_rollout`, `tally_gemini_chat`), and register it in
`read_usage`. Decide **per-call vs cumulative** yourself: a `token_count`-style event that
carries running totals (codex) is read differently from per-message blocks that are summed
(claude, gemini). Re-run the scan; the **parsed counts** self-check should now appear.
At run time the parser reads the file named by the hook payload's `transcript_path`. For
`codex-rollout`, a missing or unusable hook path is resolved from the session id under
`$CODEX_HOME/sessions`.

**`none` is a legitimate final answer.** If the report has no `token_field_candidates`, the
CLI may not expose usage anywhere a parser can reach. `antigravity` is the example: it
writes token counts only into an internal SQLite blob, never into the transcript. Leave
`usage_parser = "none"` and record why in the profile. Do not scrape a vendor's internal
store outside the `(transcript_path) -> TokenUsage | None` contract.

**Trust the payload over the docs.** A CLI that reports its transcript path in the hook
payload is telling the truth; a path in its documentation may be illustrative. `--probe`
prefers the captured path; the convention glob is the fallback.

## Worked example: copilot

The `copilot` profile was finalized from a real probe run:

```bash
bmad-loop probe-adapter copilot --probe --project /tmp/scratch
```

It surfaced three corrections to the draft:

- **Turn-end event.** PascalCase `Stop` never fires; the turn-end hook is `agentStop`.
  Without the mapping every session reads as a timeout. The profile maps
  `agentStop = "Stop"`, plus `sessionStart` and `sessionEnd` (there is no `PreCompact`
  equivalent).
- **Payload casing.** Keys are camelCase (`sessionId`, `transcriptPath`), so the shared
  relay (`bmad_loop_hook.py`) reads both casings.
- **Token schema.** The probe found `~/.copilot/session-state/*/events.jsonl` and its token
  fields (`data.modelMetrics.<model>.usage.*`), which became the `copilot-events` parser in
  `tokens.py`.

Confirm the `mkdtemp` dir is gone afterward.

## Profile field reference

A profile is the `CLIProfile` dataclass in
[`src/bmad_loop/adapters/profile.py`](../src/bmad_loop/adapters/profile.py), loaded from
TOML. Built-ins ship as packaged TOML (`bmad_loop/data/profiles/*.toml`). Project overrides
in `<project>/.bmad-loop/profiles/*.toml` overlay them: the same `name` replaces a built-in,
a new `name` extends the set. The aliases `claude-code-tmux` and `opencode` resolve to
`claude` and `opencode-http`. Unknown top-level keys are ignored.

### `CLIProfile`

| Field                              | Required | Default                                                                        | Meaning                                                                                                                                                                                                           |
| ---------------------------------- | -------- | ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                             | ✅       | —                                                                              | Profile id: the `--cli` value, the policy `[adapter] name`, and the override key.                                                                                                                                 |
| `binary`                           | ✅       | —                                                                              | Executable resolved on `PATH`. `validate` runs `--version` only on binary names a packaged profile declares (`adapter.binary-unrunnable`).                                                                        |
| `[hooks]`                          | ✅       | —                                                                              | The [`HookSpec`](#hookspec-the-hooks-table) table.                                                                                                                                                                |
| `adapter`                          |          | `generic` (`opencode-http` if `dialect=none`)                                  | Adapter kind, resolved against the live [registry](#shipping-a-new-adapter-class-out-of-tree) at run start and by `validate` (`adapter.kind`). `generic` with `dialect = "none"` is refused.                      |
| `skill_tree`                       |          | `.claude/skills`                                                               | Project-relative tree the CLI reads skills from; `init` installs the `bmad-loop-*` skills here. Must stay inside the project.                                                                                     |
| `prompt_template`                  |          | `{prompt}`                                                                     | Renders the canonical `/skill args` prompt. Placeholders: `{prompt}` (whole string), `{skill}` (command name, no `/`), `{args}` (the rest).                                                                       |
| `launch_args`                      |          | `[]`                                                                           | Argv after `binary` (`["-i"]` keeps gemini/copilot interactive). Under `opencode-http` it precedes `serve`; `validate` warns on unservable shapes.                                                                |
| `bypass_args`                      |          | `[]`                                                                           | Permission-bypass flags for unattended runs. A policy `extra_args` replaces them; `validate` warns (`policy.bypass-dropped`) when one is dropped.                                                                 |
| `model_flag`                       |          | `--model`                                                                      | Flag passed with the model name when one is configured.                                                                                                                                                           |
| `env`                              |          | `{}`                                                                           | `[env]` table of extra session environment variables. It cannot override the run's state-root pin.                                                                                                                |
| `usage_parser`                     |          | `none`                                                                         | Transcript token parser: `claude-jsonl`, `codex-rollout`, `gemini-chat`, `copilot-events` or `none`.                                                                                                              |
| `usage_grace_s`                    |          | `0.0`                                                                          | Seconds to keep polling the transcript for token totals after the session ends; `0` reads once. Must be ≥ 0.                                                                                                      |
| `stop_without_result_nudges`       |          | unset                                                                          | Per-profile default for Stop-without-result nudges. An `[adapter]` / `[adapter.<stage>]` value overrides it; unset inherits `[limits] stop_without_result_nudges`. Must be ≥ 0.                                   |
| `subagent_stop_without_transcript` |          | `false`                                                                        | When `true`, a `Stop` with no transcript path is a subagent turn-end and is ignored.                                                                                                                              |
| `first_run_note`                   |          | `""`                                                                           | Note `init` prints about a manual first-run or auth step.                                                                                                                                                         |
| `seed_files`                       |          | `[]`                                                                           | Project-relative gitignored files copied into each provisioned worktree. Must stay inside the project.                                                                                                            |
| `env_fault_patterns`               |          | `[]`                                                                           | Regexes that classify a non-completed session as an environment fault. See [Environment-fault patterns](#environment-fault-patterns).                                                                             |
| `parked_prompt_patterns`           |          | `[]`                                                                           | Regexes matched against the visible pane at each stall-grace expiry. See [Parked-session signals](#parked-session-signals).                                                                                       |
| `[workspace_trust]`                |          | unset                                                                          | The [`WorkspaceTrustSpec`](#workspacetrustspec-the-workspace_trust-table) table. Unset: nothing under `~` is read or written.                                                                                     |
| `session_id_flag`                  |          | `""`                                                                           | Launch flag for a caller-chosen session id (claude: `--session-id`). The generic adapter appends `[flag, uuid4]` and pins attribution to that id.                                                                 |
| `subagent_transcript_dir`          |          | derived from `usage_parser` (`{stem}/subagents` for `claude-jsonl`, else `""`) | Subagent transcript tree beside the transcript, walked recursively and stat-only; its writes count as idle-notice activity. Must contain `{stem}` (the transcript's filename stem) and stay relative; `""` = off. |

`session_id_flag` must be one `-`-prefixed token with no whitespace or `=`, and is refused
on hookless profiles. Set it only when the CLI's `SessionStart` payload reports the id you
passed.

An overlay that changes `usage_parser` away from `claude-jsonl` must set
`subagent_transcript_dir` itself; it is refused on hookless profiles.

### `HookSpec` (the `[hooks]` table)

| Field                | Required                     | Default | Meaning                                                                                                                                                          |
| -------------------- | ---------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dialect`            | ✅                           | —       | Hook-config format: `claude-settings-json`, `codex-hooks-json`, `gemini-settings-json`, `copilot-settings-json`, `antigravity-hooks-json`, or `none` (hookless). |
| `config_path`        | ✅ unless `dialect = "none"` | `""`    | Project-relative path the hook config is written to. Must stay inside the project; empty when hookless.                                                          |
| `events`             | ✅ unless `dialect = "none"` | `{}`    | Native event → canonical event (`SessionStart`, `Stop`, `SessionEnd`, `PreCompact`, `Notification`, or a parked kind). Empty when hookless.                      |
| `notification_types` |                              | `{}`    | `[hooks.notification_types]`: notification subtype → parked kind. Unmapped subtypes are ignored. Needs an event mapped to `Notification`.                        |

### Session identity and nested CLIs

The relay stamps every hook event with the session's task id from the inherited
environment, so a coding CLI launched from _inside_ a session writes its events into the
parent's stream. The generic adapter tells them apart by `session_id`
(`SessionAttribution` in `signals.py`):

- **`SessionStart`'s `session_id` binds the session.** Without `session_id_flag`, the first
  `SessionStart` is the launched session's; with it, the id is pinned at launch. A later one with a new id marks that id foreign and drops its
  events, unless the payload's `source` is `clear` or `compact`, which rebinds. Map
  `SessionStart` whenever the CLI has one, and check that its payload carries the id.
- **`Stop` and `SessionEnd` should carry the same id.** Attribution fails toward
  acceptance: an id-less event, or an id that never announced a `SessionStart`, is
  admitted. Two exceptions: under `session_id_flag` an identified `SessionStart` or
  `SessionEnd` from any other id is foreign, and once relay lineage is calibrated trusted a `mismatch`-tagged
  event is dropped, id-less or not.
- **Subagent turn-ends stay with `subagent_stop_without_transcript`.** Subagent ids never
  announce a `SessionStart`, so attribution admits them and the transcript filter decides.
- **Stop-only profiles get no nested-CLI protection.** With no `SessionStart` mapped,
  every event is admitted (`antigravity`).

`probe-adapter --probe` shows the payload's field names; confirm the `session_id` and
`source` casing before relying on them.

### `WorkspaceTrustSpec` (the `[workspace_trust]` table)

Some CLIs refuse to start in a workspace that is not listed in a home-level settings file,
and block on a trust dialog a spawned session cannot answer. Declaring the file lets
provisioning append each unit worktree to that list before any session launches.

| Field           | Required | Meaning                                                                                                    |
| --------------- | -------- | ---------------------------------------------------------------------------------------------------------- |
| `settings_path` | ✅       | `~/`-prefixed path of a JSON settings file inside the home directory. No `..` and no Windows device names. |
| `key`           | ✅       | One **top-level** key of that file holding a list of path strings.                                         |

Any other key in the table is refused. The seeder (`bmad_loop/workspace_trust.py`) adds a
worktree **only when the main checkout root is already in the list**. It touches only the
declared key, writes atomically, and keeps the file's permission bits. Outcomes are
journaled as `worktree-trust-seeded` or `worktree-trust-unseeded`; a malformed file or a
write fault escalates the unit. Entries are never removed on teardown. The shipped
`antigravity` profile declares:

```toml
[workspace_trust]
settings_path = "~/.gemini/antigravity-cli/settings.json"
key = "trustedWorkspaces"
```

### Environment-fault patterns

`env_fault_patterns` are matched line by line against the ANSI-stripped log tail of a
session that ended `timeout`, `stalled` or `crashed`. A match stamps `env_fault` /
`env_fault_evidence` on the `SessionResult`, unless the session tracked nonzero token
usage, which records the match in `env_fault_overruled` instead. What the engine does next
is in [Environment faults](escalations-guide.md#environment-faults).

- **Which log** is per adapter, named by `EnvFaultMixin.ENV_FAULT_LOG_SUFFIX` (one file) or
  `ENV_FAULT_LOG_SUFFIXES` (several, scanned in order; the last match across them wins,
  and a missing file is a read fault). The tmux adapters scan the pane capture
  `logs/<task-id>.log`; `opencode-http` scans `logs/<task-id>.server.out` plus
  `logs/<task-id>.provider-errors.log`, the one adapter-written line per provider-error
  event.
- **A pane capture carries the model's own output**, so a pattern must reproduce the whole
  first sentence of one of the CLI's error messages, taken from captured output. An
  error-shaped token plus a cause is not enough; that is what a story writing about the
  error prints.
- **A pattern must not match its own line in the profile file.** The shipped patterns use
  single-character classes (`t[o]`) for this. `test_shipped_patterns_do_not_match_their_own_profile_line`
  and `test_pane_capture_patterns_do_not_match_this_repo` enforce it across every tracked
  file.
- Patterns are a list of strings, compiled at load (an invalid regex is a profile error),
  and matched with the `regex` module under a per-pattern timeout.
- Seeded for `claude` (connection loss and two captured provider 5xx errors) and
  `opencode` (provider quota/rate-limit and connection on 1.x; `provider.rate-limit`,
  `provider.quota` and `provider.transport` events on 2.x). Empty means inert.

### Parked-session signals

A CLI parked on a permission, idle or quota prompt looks like a stalled one, and the stall
wake nudge's trailing Enter can answer that prompt. Two opt-in profile signals let the
generic adapter tell them apart:

- **Hook-reported.** Map a native event directly to a parked kind (`PermissionPrompt`,
  `IdlePrompt`, `QuotaPrompt`), or map the CLI's notification event to `Notification` and
  its subtypes in `[hooks.notification_types]`. The shipped `claude` profile parks
  `permission_prompt`, `idle_prompt`, `quota_auto_resume_stale`,
  `quota_auto_resume_disabled`, `elicitation_dialog` and `elicitation_url_dialog`; every
  other subtype is ignored. The adapter latches the signal, and a `Stop` clears it.
- **Pane-matched.** `parked_prompt_patterns` are matched against the visible pane
  (`mux.capture_pane`) at every stall-grace expiry with no latch set, including the final
  one. A backend that cannot capture, a capture failure, or a regex timeout reads as no
  match. Seed only lines captured off the CLI's own screen, with single-character classes
  as for env-fault patterns.

Neither signal acts on arrival. At the stall-grace expiry, a parked signal suppresses the
nudge and ends the session `stalled` with `SessionResult.parked` / `parked_evidence` set (a
window already dead still ends `crashed`). The engine and sweep then pause for a human; an
`env_fault` outranks it. Re-arm restores the attempt without charging it.

Claude sends `idle_prompt` about 60 s after every finished turn, so a session that ends a
turn without a result and sits idle pauses as parked instead of being nudged. A project
overlay (`.bmad-loop/profiles/claude.toml`) that drops `idle_prompt` from
`[hooks.notification_types]` restores the wake nudge.

### Worked TOML — copilot

The shipped `copilot` profile uses the non-default tuning knobs and a camelCase event map,
all found by the [copilot probe walkthrough](#worked-example-copilot):

```toml
name = "copilot"
binary = "copilot"
skill_tree = ".agents/skills"
launch_args = ["-i"]
bypass_args = ["--allow-all-tools", "--allow-all-paths"]
usage_parser = "copilot-events"
usage_grace_s = 8.0        # token totals land ~1s after agentStop, on session.shutdown
stop_without_result_nudges = 5   # agentStop fires per response turn
subagent_stop_without_transcript = true  # ignore subagent agentStops (empty transcriptPath)
seed_files = [".github/copilot/settings.json"]

[hooks]
dialect = "copilot-settings-json"
config_path = ".github/copilot/settings.json"
events = { agentStop = "Stop", sessionStart = "SessionStart", sessionEnd = "SessionEnd" }
```

The shipped file (`bmad_loop/data/profiles/copilot.toml`) also sets `prompt_template`,
`model_flag` and `first_run_note`.

## Writing a new adapter class

Reach for this only when a CLI does not fit the pane-injection + hook-signal transport, for
example an HTTP/SSE service with no terminal. A CLI that does fit needs no Python: reuse
`generic.py` with a [profile](#profile-field-reference).

The contract is the `CodingCLIAdapter` ABC in
[`src/bmad_loop/adapters/base.py`](../src/bmad_loop/adapters/base.py).

### Declare the three capability axes

Set these class attributes so the engine can reason about transport quality:

- `injection`: how a prompt reaches the CLI: `tmux-initial-prompt` | `launch-flag` | `http`.
- `observation`: how completion is detected: `hook-signal` | `sse` | `transcript-poll`.
- `state`: where session state is readable: `local-jsonl` | `local-json-tree` | `remote`.

Also set `name`. The engine attaches `journal`; gate every adapter-side journal write on it
being non-`None`.

### The data contracts

Frozen dataclasses cross the seam; read their docstrings in `base.py` for every field:

- **`SessionSpec`** (engine → adapter): `task_id`, `role` (`"dev"` / `"review"` /
  `"retro"`), `prompt`, `cwd`, `env`, `model` (empty = CLI default), `timeout_s`, the
  stall-nudge and token-budget settings, the `spec_snapshot` / `expected_spec` pair, and
  `effort` (empty = provider default). Only `opencode-http` uses `effort`; `validate` warns
  when a stage on the generic family sets it.
- **`SessionHandle`** (returned by `start_session`): `task_id`, `native_id` (window id,
  HTTP session id, …), `launched_ns` (wall-clock ns just before launch, the floor for hook
  events), and `pinned_session_id`.
- **`SessionResult`** (returned by `wait_for_completion`): `status` (`completed`,
  `stalled`, `timeout`, `crashed`, `over_budget` or `aborted`), `result_json`,
  `session_id`, `transcript_path`, and post-mortem fields including `env_fault` /
  `env_fault_evidence` / `env_fault_overruled` and `parked` / `parked_evidence`.

### Methods

Required (abstract):

- `start_session(spec) -> SessionHandle` — launch the session. An adapter that
  persists the standard `tasks/<id>/` directory must reset its shared cycle
  artifacts when an id is reused: after creating the task directory and before
  launching the session, remove every file named by
  `journal.TASK_CYCLE_ARTIFACTS` (shared artifacts: [`result.json`,
  `escalation.json`]).
  Use missing-safe deletion; a missing artifact is a normal no-op and must not
  make startup fail. This tuple covers only artifacts shared across adapters and
  readers. Adapter-private breadcrumbs such as `heartbeat.json`,
  `resultless-stops.jsonl`, `session-lifecycle.jsonl`, and `messages.json` remain
  outside the shared cleanup contract and are managed by their owning adapter.
- `wait_for_completion(handle, spec) -> SessionResult` — block until the session ends,
  stalls or times out, then report status. On both sides of the loop's own blocking wait,
  check for a hard stop (`_ResultFileMixin._hard_stop_requested()` reads this run's and the
  owning run's `stop-request.json`) and return `SessionResult(status="aborted")` when one
  is lodged. Never raise and never unlink the request; the engine consumes it. Keep the
  wait short: the bundled adapters cap theirs at 5 s. An adapter without the poll is
  stopped at the next item boundary instead.

The base class provides `run(spec)`, the template you normally leave alone: `start_session`
→ `wait_for_completion` → `kill` (in a `finally`), then three post-processing hooks over
the result:

- `_post_kill_reconcile(handle, spec, result)`: identity by default. A session that
  finished but lost its final `Stop` can be rescued to `completed` here, once window death
  has settled liveness (see `GenericDevAdapter`).
- `_classify_env_fault(handle, spec, result)`: identity by default; runs only for a
  non-`completed` result with `result_json is None`. Mix in `EnvFaultMixin` for the
  profile-driven implementation, and set its `ENV_FAULT_LOG_SUFFIX` (or
  `ENV_FAULT_LOG_SUFFIXES`) if your diagnostics do not land in `logs/<task-id>.log`.
- `_classify_zero_token_timeout(result)`: stamps `env_fault` on a `timeout` whose tracked
  usage is zero, unless an env fault or `env_fault_overruled` is already set.

Optional capabilities (each has a default):

- `send_text(handle, text)`: nudge a running session. Raises `NotImplementedError` by
  default.
- `interactive_argv(spec)` / `interactive_env(spec)`: argv and env that launch the CLI
  attached to the caller's terminal, seeded with the prompt, for interactive escalation
  resolution. `interactive_argv` raises by default; `interactive_env` returns `spec.env`.
- `kill(handle)`: tear down the session (no-op default). Must be safe in `run()`'s
  `finally`.
- `read_usage(result) -> TokenUsage | None`: token usage (default `None`, untracked).
- `_observe_tick(handle, spec)`: heartbeat-cadence observation hook (no-op default). It
  must not mutate session state.

### Worked example: the opencode adapter

[`adapters/opencode_http.py`](../src/bmad_loop/adapters/opencode_http.py) is the shipped
non-tmux adapter. It drives [OpenCode](https://opencode.ai) over `opencode serve`'s HTTP API
and SSE stream (`injection = "http"`, `observation = "sse"`, `state = "remote"`). Patterns
worth reusing:

- **Split the wire codec from the transport.** OpenCode 1.x and 2.x serve different HTTP
  APIs from the same binary name. The transport (server process, port, password, teardown,
  the stall/budget/timeout loop) is shared; each major's routes, frame envelope, turn-end
  events, usage read and transcript renderer live in a protocol class in
  [`adapters/opencode_protocol.py`](../src/bmad_loop/adapters/opencode_protocol.py), whose
  module docstring records each API contract as pinned live. The adapter picks one from
  `opencode --version` before spawning, refuses a major it has no codec for, and
  cross-checks the choice at health time: each major answers the other's routes with the
  web UI's HTML.
- **One server per session.** Each session gets its own `opencode serve`, spawned with
  `cwd = spec.cwd` and `spec.env`, so the engine's `BMAD_LOOP_*` contract reaches tool
  subprocesses. The argv is
  `[binary, *launch_args, "serve", "--port", <free port>, "--hostname", "127.0.0.1", "--print-logs", *extra_args]`.
  Permissions, the model and a hermetic skills path go in `OPENCODE_CONFIG_CONTENT`, with a
  per-server `OPENCODE_SERVER_PASSWORD`. Reasoning effort is the model `variant`: in each
  1.x `prompt_async` body, and on the 2.x session's model ref at create.
- **Map the transport onto hook-signal semantics.** The SSE turn end (1.x `session.idle`,
  2.x `session.execution.*`) ≙ the Stop hook; server-process death ≙ window death
  (`crashed`); on SSE loss, a poll of the busy map (1.x `GET /session/status`, 2.x
  `GET /api/session/active`) plus proof of a completed assistant message takes over. An idle
  event without proof of new work never completes a session.
- **Log sinks, one per audience.** `<task-id>.log` is a readable transcript curated off
  the SSE stream; `<task-id>.server.out` takes the server's own stdout;
  `<task-id>.provider-errors.log` gets one adapter-written line per provider-error event
  (2.x logs no per-retry error to stdout). Env-fault classification scans those last two,
  never the model-written transcript. `<task-id>.sse.jsonl` keeps raw frames behind the
  `sse_trace` knob.
- **Hookless profile.** `[hooks] dialect = "none"`: no hook registration and no hook-config
  merge into worktrees. Skills still install; opencode discovers `.claude/skills/<name>/SKILL.md`.
- **Reuse the synthesis mixins.** `_ResultFileMixin` (`result.json` read-back) and
  `_DevSynthesisMixin` (dev/review result synthesis) live in
  [`adapters/generic.py`](../src/bmad_loop/adapters/generic.py).
  `OpencodeDevAdapter(_DevSynthesisMixin, OpencodeHttpAdapter)` overrides
  `_probe_alive(handle) -> bool | None` (post-kill liveness; `None` = unknown, the verdict
  stands) and `_evict_task_state(task_id)`, and calls `_configure_dev_knobs()` from
  `__init__`.
- **Usage before teardown, kill in `finally`.** Usage is read over HTTP before every return
  path. `kill()` is idempotent, an `atexit` sweep backs it up, and on Windows the process
  tree is force-killed first.

[`tests/test_opencode_http.py`](../tests/test_opencode_http.py) runs the adapter against a
scripted stdlib fake server with a mode per major;
[`tests/test_opencode_live.py`](../tests/test_opencode_live.py) smoke-checks the detected
major's HTTP contract against a real local binary without ever prompting, and
[`tests/test_opencode_live_turn.py`](../tests/test_opencode_live_turn.py) runs one full 2.x
turn whose every LLM request goes to an in-process fake provider. Both skip when no binary
is on PATH and spend zero tokens.

### Shipping a new adapter class out-of-tree

Which class drives a CLI is data: the profile's `adapter` field, resolved against the
adapter registry ([`adapters/registry.py`](../src/bmad_loop/adapters/registry.py)). A
co-installed package registers its own kind with no core edit, as a transport backend does
([the transport contract](#the-transport-contract-for-a-backend-author)).

Advertise **two entry points** in the package's `pyproject.toml`:

- **`bmad_loop.adapters`** → a module whose import registers the kind. Core imports each
  advertised module before it resolves any adapter. A bundled kind name always wins, because
  the builtins are seeded first and the first registration wins:

  ```python
  # acme_adapter/__init__.py
  from bmad_loop.adapters.registry import AdapterBuilder, register_adapter

  def _load():                       # lazy: imported only when a run builds this kind,
      from .acme import AcmeAdapter, AcmeDevAdapter   # so an optional dep stays unpaid
      return AdapterBuilder(
          plain=AcmeAdapter,          # the plain class
          dev=AcmeDevAdapter,         # the _DevSynthesisMixin-composed dev/review class
          construct_error=(),         # exception type(s) __init__ may raise; () = none
      )

  register_adapter("acme", needs_mux=True, load=_load)   # needs_mux: does it drive a multiplexer?
  ```

  ```toml
  # pyproject.toml
  [project.entry-points."bmad_loop.adapters"]
  acme = "acme_adapter"
  ```

- **`bmad_loop.profiles`** → a callable returning `CLIProfile`s (or an iterable of them),
  so the profile that selects your kind ships with it. Precedence is packaged < entry-point
  < project, so a project TOML can still override it:

  ```python
  # acme_adapter/__init__.py  (same package)
  from bmad_loop.adapters.profile import CLIProfile, HookSpec

  def profiles():
      return [CLIProfile(name="acme", binary="acme", adapter="acme",
                         hooks=HookSpec("none", "", {}))]
  ```

  ```toml
  [project.entry-points."bmad_loop.profiles"]
  acme = "acme_adapter:profiles"
  ```

The adapter group's value is a bare module path (registration is the import's side
effect); the profile group's value names a callable. Installing the package into
bmad-loop's environment is the whole setup, for example
`uv tool install bmad-loop --with <your-adapter>`.

Once installed, `bmad-loop adapters` lists the kind and the profiles that select it,
`bmad-loop validate` checks the reference (`adapter.kind`, against the live registry), and
a run whose policy `[adapter] name` points at a profile with `adapter = "acme"` selects it.
Policy's `[adapter] name` picks the **profile**; the profile's `adapter` field picks the
**kind**.

A broken package never breaks selection. `bmad-loop adapters` prints
`warning: external adapter '<name>' failed to load: <reason>` (and the same for a failed
profile provider), `validate` reports it, and selection proceeds without it. An
entry-point profile is validated like a TOML profile on arrival, so an invalid one is
reported rather than half-installed.

Seam facts for the class itself:

- **`needs_mux`** decides whether the run bootstrap resolves and usability-checks the
  terminal multiplexer for your kind. A pane/hook family sets it `True`; a self-hosted
  HTTP/SSE family sets it `False` and is never handed a `mux`.
- **`dev` vs `plain`.** When a dev or review session runs the dev skill (policy
  `dev.skill = "bmad-dev-auto"`), which writes no `result.json`, the bootstrap builds the `dev` variant and passes it `paths`
  so it can synthesize the result from the spec on disk. Every other role builds `plain`,
  without `paths`.
- **Accept `**kwargs`in both variants.**`runsetup.make_adapters`builds every class with`cls(\*\*build_kwargs)`: `run_dir`, `policy`, `profile`, `extra_args`, `usage_grace_s`,
`stop_without_result_nudges`and`events_dir`, plus `mux`for a`needs_mux=True`kind and`paths`for the`dev`variant. Core adds keys as the run gains things worth describing.
A class that rejects a keyword aborts the run with`error: profile '<name>': adapter kind '<kind>' rejected this run's adapter keywords: …`.
Accept what you do not use: `opencode_http`takes`events_dir` and discards it.

### References

- [`adapters/registry.py`](../src/bmad_loop/adapters/registry.py) — the adapter-kind
  registry and the two entry-point scans.
- [`adapters/opencode_http.py`](../src/bmad_loop/adapters/opencode_http.py) — a non-tmux
  (HTTP/SSE) adapter.
- [`adapters/mock.py`](../src/bmad_loop/adapters/mock.py) — the test-only reference
  implementation.
- [`adapters/generic.py`](../src/bmad_loop/adapters/generic.py) — the pane + hook-signal
  adapter to reuse with a profile; home of `_ResultFileMixin` and `_DevSynthesisMixin`.
