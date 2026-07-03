# Authoring CLI adapters & profiles

bmad-loop drives any coding CLI that fits the **tmux-injection + hook-signal**
transport through one generic adapter (`adapters/generic.py`); everything
CLI-specific lives in a declarative **TOML profile** (`adapters/profile.py`). This
guide is the canonical home for the profile schema and the two ways to teach
bmad-loop a new CLI:

- **The common case — a TOML profile.** If the CLI fits tmux + hook-signal, you
  write no Python. The [Profile field reference](#profile-field-reference) is the
  complete `CLIProfile` / `HookSpec` schema; the
  [walkthrough below](#walkthrough-finalizing-a-profile) shows how `probe-adapter`
  finalizes one against a real run.
- **The advanced case — a new adapter class.** If the CLI does _not_ fit that
  transport (e.g. an HTTP/SSE service), see
  [Writing a new adapter class](#writing-a-new-adapter-class) for the
  `CodingCLIAdapter` ABC.

## Two axes: CLI vs transport

These are independent and abstracted separately:

- **CLI axis** — `CodingCLIAdapter` (`adapters/base.py`): _which_ binary to launch,
  how the prompt is rendered, the hook dialect, where the transcript lives. The
  generic adapter + a TOML profile cover this; the rest of this guide is about it.
- **Transport axis** — `TerminalMultiplexer` (`adapters/multiplexer.py`): how
  sessions, windows, and panes are created, observed, and torn down. The generic
  adapter never shells out itself — it goes through `self.mux`, obtained from
  `get_multiplexer()`. The one backend today is tmux: argv construction and the
  single spawn primitive live in `BaseTmuxBackend` (`adapters/tmux_base.py`), with
  the thin POSIX leaf `TmuxMultiplexer` (`adapters/tmux_backend.py`); together they
  are the **only** files allowed to invoke `tmux` (and the only place POSIX-shell
  trailers live). A future non-POSIX backend (e.g. a native-Windows "psmux")
  registers itself via `register_multiplexer(...)` and slots in behind
  `get_multiplexer()` with no change to the adapters. A backend author reads
  `multiplexer.py` for the contract and `tmux_backend.py` / `tmux_base.py` for the
  reference implementation. Transport is one of **four OS seams** — the others
  (process lifecycle, hook interpreter, validate preflight) are mapped in
  [Porting bmad-loop to a new OS](porting-to-a-new-os.md).

### The transport contract (for a backend author)

Every part of the codebase that touches sessions, windows, or clients now goes
through `get_multiplexer()` — not just the generic adapter but also `runs.py`
(session listing/tagging, kill, attach argv), `tui/launch.py` (the control
session and its parked orchestrator windows), `probe.py` (the throwaway probe
session), and `tui/data.py` (legacy-run liveness). A grep for `"tmux"` outside the
tmux backend (`adapters/tmux_base.py` + `adapters/tmux_backend.py`) should turn up
only `shutil.which("tmux")` presence checks, never an invocation.

To add a backend, build a `TerminalMultiplexer` (`adapters/multiplexer.py`) and
**register** it — `register_multiplexer(name, matches, factory)`, where
`matches(sys.platform)` decides automatic selection and `name` is the key the
`BMAD_LOOP_MUX_BACKEND` env var forces (for tests / overrides). `get_multiplexer()`
returns the first backend whose `matches` is true, with tmux as the default
fallback. There are two build paths: extend `BaseTmuxBackend` (`adapters/tmux_base.py`)
for a tmux-family backend — overriding only its single spawn primitive `_run()`
plus the shell-dialect hooks (`_shell_wrap`, `_join_argv`, `_parked_trailer`,
`_source_prefix`, `_window_launch` and the `_EXIT_CAPTURE`/`_ECHO`/`_PARK`
fragments) — or implement
`TerminalMultiplexer` fresh for a host with no tmux-shaped CLI. The non-transport
seams of a full OS port are in
[Porting bmad-loop to a new OS](porting-to-a-new-os.md). The contract groups into:

- **Sessions** — `has_session`, `new_session` (geometry is optional: agent
  sessions pin a fixed pane size because they are observed while detached; the
  control session omits it), `kill_session`, `list_sessions`, `session_options`
  (read a user option across all sessions), `set_session_option`.
- **Windows** — `new_window` (run a command in a fresh window), `new_parked_window`
  (run a command, then _park_ on a keypress so the exit status stays inspectable,
  then return any attached client to its origin — the POSIX `sh -c` recipe is
  composed from the base's overridable shell-dialect hooks, so a non-POSIX
  backend swaps the dialect fragments, not the method body), `list_window_ids`, `list_windows` (selected fields per window),
  `window_alive`, `kill_window`, `select_window`, `set_window_option`,
  `unset_window_option`, `show_window_option`, `pipe_pane` (tee a pane to a log),
  `send_text`.
- **Client / attach** — `attach_target_argv` (argv that reaches a target, nesting-
  aware), `current_pane_id` / `current_window_id` / `current_session`,
  `detach_client`, `switch_client` (with an optional last-client fallback),
  `available` (is this backend usable on the current host).

Operations that can race a window dying (`pipe_pane`) or a session already being
gone (`kill_session`) must tolerate it rather than raise; everything else raises a
`MultiplexerError` subclass on failure, which call sites catch at the seam (e.g.
`tui/launch.start_detached` turns it into a `LaunchError`) without importing the
backend. `window_alive` uses `list-windows` membership, not `display-message`, because
`display-message -t <dead-window>` exits 0 on tmux.

`tmux_backend.py` is the reference implementation; reading it alongside the ABC is
the fastest way to see exactly what a `new_parked_window` or `session_options` must
produce.

The hard part of a new profile isn't the TOML — it's the **facts that live in no
doc**: the CLI's exact hook payload shape (field names and casing, whether
`session_id` / `transcript_path` / `cwd` are present), where it writes its session
transcript and in what format, and the token-usage schema a `usage_parser` has to
read. Historically the only way to get these was to hand a volunteer a manual
recipe and ask them to sanitize the output by hand — error-prone and PII-risky.

**`bmad-loop probe-adapter`** (alias `collect-adapter-data`) pulls all of that and
runs it through an audited sanitizer, so a user of any coding CLI can run one
command and paste back a clean, content-free report.

```bash
bmad-loop probe-adapter <cli> --project .          # default: zero-launch scan
bmad-loop probe-adapter <cli> --probe --project .  # opt-in live capture
```

---

## Two modes

Both modes emit the **same single sanitized report** (markdown to stdout, or to a
file with `--out`; add `--json` for a machine-readable block).

### SCAN (default — no process launch)

Runs `<binary> --version` / `--help`, locates the newest **already-existing**
session transcript by convention, reads the declared hook config, and infers the
token schema from the transcript. Works whenever you've used the CLI before, with
zero execution risk. This is the right first step for any CLI that already has a
profile (claude/codex/gemini/copilot) or that you've run by hand.

### PROBE (`--probe` — opt-in live capture)

In an ephemeral `mkdtemp` workspace, `probe` registers a full-payload capture hook
for every native event in the profile, launches **one trivial content-free turn**
(`Reply with exactly: OK`) in a tmux window, captures each hook event's complete
payload, locates the transcript, then tears everything down. Use it to confirm the
**exact hook payload shape** and that the CLI actually **accepts the hook dialect**
your profile declares — facts scan can't see without running the CLI.

`--probe` needs a known profile (it uses the profile's hook dialect and event map).
If `tmux` or the binary is missing, probe degrades gracefully to a scan.

---

## PII safety model

The report is built to be **safe to paste into an issue or PR**. A single audited
sanitizer (`src/bmad_loop/sanitize.py`) is the only chokepoint:

- **numbers, booleans, and `null` pass through** — token _counts_ are not PII;
- **dict keys are kept verbatim** — field names and casing are the whole point of
  a payload probe;
- every **leaf string** is `$HOME`→`~` redacted and then kept **only if** it looks
  like a short machine identifier (e.g. `claude-opus-4-8`, `session-abc_123`);
  anything else — prose, code, paths, emails — becomes `<redacted:str>`;
- **list lengths are preserved**, contents are scrubbed element by element;
- `--help` / `--version` text and log tails have the home dir and any emails
  redacted, with a line cap.

In PROBE mode the raw capture exists **only transiently** inside the temp dir,
which is `rmtree`'d in a `finally` (even on exception or Ctrl-C). The CLI's own
transcript stays in its home dir — the command reads its _structure_, never copies
it. A hidden `--keep-temp` flag retains the raw temp dir for debugging and prints a
loud **"raw retained — do not share"** warning; never paste a `--keep-temp` run.

---

## Walkthrough: finalizing a profile

### 1. Draft a profile

Drop a TOML file in `<project>/.bmad-loop/profiles/<name>.toml` with the fields
from the [Profile field reference](#profile-field-reference) below. The minimum is
a `binary`, a `prompt_template`, bypass flags, a `[hooks]` block picking one of the
config dialects (`claude-settings-json` / `codex-hooks-json` /
`gemini-settings-json` / `copilot-settings-json`) and a native→canonical event
map, and a `usage_parser` (start with `"none"` until you've written one).

### 2. Scan

```bash
bmad-loop probe-adapter <cli> --project .
```

Read three sections of the report:

- **CLI flags** — your profile's launch/bypass flags plus the scrubbed
  `--version` / `--help`, so you can confirm the flags you chose exist.
- **Transcript** — the redacted location, format, size, line count, and modified
  date of the newest transcript the convention glob found.
- **Token usage schema** — the structural key paths (types only, never values) and
  the **token-field candidates** (int leaves whose names look token-ish). When a
  real parser is already declared, its parsed counts are shown as a self-check.

### 3. Probe (confirm the live payload + dialect)

```bash
bmad-loop probe-adapter <cli> --probe --project /tmp/scratch
```

The **Hook payload shape** section now shows, per captured event, the native→
canonical pairing, the payload keys, and the scrubbed payload — so you can confirm
`session_id` / `transcript_path` casing and that the CLI accepted the hook config
for your dialect. If the CLI rejects the config or never fires a hook, the report
says so (with a scrubbed log tail) instead of silently producing nothing.

### 4. Write the `usage_parser`

Turn the report's `token_field_candidates` into a parser in
[`src/bmad_loop/tokens.py`](../src/bmad_loop/tokens.py), following the existing
ones (`tally` for claude, `tally_codex_rollout`, `tally_gemini_chat`) and
registering it in `read_usage`. The report flags **per-call vs cumulative** as a
human call — a `token_count`-style event that carries running totals (codex) is
read differently from per-message blocks that are summed (claude/gemini). Re-run
scan after wiring the parser: the **parsed counts** self-check should now appear.

---

## Flags reference

| Flag                | Purpose                                                                          |
| ------------------- | -------------------------------------------------------------------------------- |
| `--probe`           | Opt-in live capture (default is scan). Needs a known profile.                    |
| `--transcript PATH` | Inspect this exact transcript file, bypassing convention discovery.              |
| `--session-dir DIR` | Glob this dir (`**/*.jsonl` then `*.json`, newest) — for custom/unknown CLIs.    |
| `--binary NAME`     | Binary to probe for a CLI that has no profile yet (enables a reduced report).    |
| `--model NAME`      | Model passed to the probe turn (PROBE mode).                                     |
| `--timeout SECONDS` | Probe turn timeout (default 90).                                                 |
| `--out FILE`        | Write the report to a file instead of stdout (the only file the command writes). |
| `--json`            | Append a machine-readable JSON block to the report.                              |
| `--keep-temp`       | (hidden, debug) keep the raw probe temp dir — prints a "do not share" warning.   |

Exit codes mirror `validate`: `0` whenever a report is produced (warnings are
fine), `1` only when nothing could be produced. An **unknown CLI with `--binary`**
still yields a _reduced_ report (version/help + discovery, no hook events); an
unknown CLI without `--binary` fails and lists the available profiles.

---

## Worked example: copilot

The `copilot` profile was finalized from a real probe run — a good illustration of
why `probe-adapter` exists, because the as-drafted profile was wrong in ways no doc
would reveal:

```bash
bmad-loop probe-adapter copilot --probe --project /tmp/scratch
```

On Copilot CLI 1.0.63 this surfaced three corrections:

- **Turn-end event.** The draft registered PascalCase `Stop`, which never fires —
  the turn-end hook is `agentStop` (camelCase). Without this, every session reads
  as a timeout. The profile now maps `agentStop = "Stop"` (and `sessionStart` /
  `sessionEnd`; there is no `PreCompact` equivalent).
- **Payload casing.** Keys are camelCase (`sessionId`, `transcriptPath`), not
  snake_case — so the shared relay (`bmad_loop_hook.py`) reads both casings.
- **Token schema.** The probe located `~/.copilot/session-state/*/events.jsonl` and
  inferred its token fields (`data.modelMetrics.<model>.usage.*`), which became the
  `copilot-events` parser in `tokens.py`; the profile's `usage_parser` is now wired
  to it instead of `"none"`.

Confirm the `mkdtemp` dir is gone afterward.

---

## Profile field reference

A profile is the `CLIProfile` dataclass in
[`src/bmad_loop/adapters/profile.py`](../src/bmad_loop/adapters/profile.py),
loaded from TOML. **Built-ins** ship as packaged TOML
(`bmad_loop/data/profiles/*.toml`); **project overrides** in
`<project>/.bmad-loop/profiles/*.toml` overlay them — same `name` overrides a
built-in, a new `name` extends the set. The legacy alias `claude-code-tmux`
resolves to `claude`.

### `CLIProfile`

| Field                              | Required | Default            | Meaning                                                                                                                                                                                                                                                                                                                                                 |
| ---------------------------------- | -------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                             | ✅       | —                  | Profile id, also the `--cli` value and override key.                                                                                                                                                                                                                                                                                                    |
| `binary`                           | ✅       | —                  | Executable to launch (resolved on `PATH`).                                                                                                                                                                                                                                                                                                              |
| `[hooks]`                          | ✅       | —                  | The `HookSpec` table (see below).                                                                                                                                                                                                                                                                                                                       |
| `skill_tree`                       |          | `.claude/skills`   | Project-relative tree this CLI reads skills from (`.agents/skills` for codex/gemini); `bmad-loop init` installs the `bmad-loop-*` skills here. Must be relative.                                                                                                                                                                                        |
| `prompt_template`                  |          | `{prompt}`         | How the canonical `/skill args` prompt is rendered. Placeholders: `{prompt}` (whole string), `{skill}` (leading slash-command name, no `/`), `{args}` (the remainder).                                                                                                                                                                                  |
| `launch_args`                      |          | `()`               | Extra argv passed at launch, e.g. `["-i"]` to stay interactive (gemini/copilot).                                                                                                                                                                                                                                                                        |
| `bypass_args`                      |          | `()`               | Flags that bypass permission/approval prompts for unattended runs (e.g. `--allow-all-tools`).                                                                                                                                                                                                                                                           |
| `model_flag`                       |          | `--model`          | Flag used to pass the model name when one is configured.                                                                                                                                                                                                                                                                                                |
| `env`                              |          | `{}`               | Extra environment variables for the session.                                                                                                                                                                                                                                                                                                            |
| `usage_parser`                     |          | `none`             | Which transcript token parser to use — one of `claude-jsonl`, `codex-rollout`, `gemini-chat`, `copilot-events`, `none`.                                                                                                                                                                                                                                 |
| `usage_grace_s`                    |          | `0.0`              | Seconds to keep polling the transcript for token totals after the session ends. `0` = read once. Raise it for CLIs that flush totals only on shutdown (copilot writes `modelMetrics` ~1s after the turn-end hook). Must be ≥ 0.                                                                                                                         |
| `stop_without_result_nudges`       |          | unset (use global) | Per-adapter floor for Stop-without-result nudges. Leave unset to inherit `limits.stop_without_result_nudges`. Raise it for CLIs that fire a turn-end hook _per response turn_ (copilot's `agentStop`), where the global default of 1 declares them stalled too early. Must be ≥ 0 if set.                                                               |
| `subagent_stop_without_transcript` |          | `false`            | Set `true` for CLIs that fire the turn-end hook for _subagent_ turns too, with an empty `transcriptPath` and a tool-use session id (copilot's `agentStop`). A `Stop` carrying no transcript is then treated as a subagent stop and ignored, so the main session's real turn-end drives completion. Leave `false` and every `Stop` is the main turn-end. |
| `first_run_note`                   |          | `""`               | Human note printed by `init` about a manual first-run/auth step this CLI needs.                                                                                                                                                                                                                                                                         |
| `seed_files`                       |          | `()`               | Project-relative gitignored configs (MCP/CLI settings) a `git worktree add` checkout omits; `provision_worktree` copies them into isolated dev/review worktrees. Must be relative.                                                                                                                                                                      |

### `HookSpec` (the `[hooks]` table)

| Field         | Required | Meaning                                                                                                                                                                                                                                   |
| ------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dialect`     | ✅       | The CLI's hook-config format — one of `claude-settings-json`, `codex-hooks-json`, `gemini-settings-json`, `copilot-settings-json`.                                                                                                        |
| `config_path` | ✅       | Project-relative path the hook config is written to (e.g. `.claude/settings.json`). Absolute paths are rejected.                                                                                                                          |
| `events`      | ✅       | Map of **native** event name → **canonical** event name. The canonical side must be one of `SessionStart`, `Stop`, `SessionEnd`, `PreCompact`; the native side is whatever the CLI emits (e.g. `agentStop = "Stop"`). At least one entry. |

### Worked TOML — copilot

The shipped `copilot` profile exercises the non-default tuning knobs
(`usage_grace_s`, `stop_without_result_nudges`, `subagent_stop_without_transcript`)
and a camelCase event map — all discovered by the
[copilot probe walkthrough](#worked-example-copilot) above:

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

(The shipped profile under `bmad_loop/data/profiles/copilot.toml` also carries a
`prompt_template` and `first_run_note`, trimmed here for focus — read it for the
exact shipped values.)

---

## Writing a new adapter class

Reach for this **only** when a CLI does not fit the tmux-injection + hook-signal
transport — for example an HTTP/SSE service with no terminal. A CLI that _does_
fit (a binary you launch in a pane that fires lifecycle hooks) needs no Python:
reuse `generic.py` with a [profile](#profile-field-reference) instead of
subclassing.

The contract is the `CodingCLIAdapter` ABC in
[`src/bmad_loop/adapters/base.py`](../src/bmad_loop/adapters/base.py).

### Declare the three capability axes

Set these class attributes so the engine can reason about transport quality
instead of treating every CLI as a dumb terminal:

- `injection` — how a prompt reaches the CLI: `tmux-initial-prompt` | `launch-flag` | `http`.
- `observation` — how completion is detected: `hook-signal` | `sse` | `transcript-poll`.
- `state` — where session state is readable: `local-jsonl` | `local-json-tree` | `remote`.

### The data contracts

Three frozen dataclasses cross the seam:

- **`SessionSpec`** (engine → adapter) — `task_id`, `role` (`"dev"` / `"review"` /
  `"retro"`), `prompt`, `cwd`, `env`, `model` (empty = CLI default),
  `timeout_s`.
- **`SessionHandle`** (returned by `start_session`) — `task_id`, `native_id` (tmux
  window id, HTTP session id, …), `launched_ns` (wall-clock ns just before launch;
  the floor for hook events).
- **`SessionResult`** (returned by `wait_for_completion`) — `status` (one of
  `completed`, `stalled`, `timeout`, `crashed`), `result_json`, `session_id`,
  `transcript_path`.

### Methods

Required (abstract):

- `start_session(spec) -> SessionHandle` — launch the session.
- `wait_for_completion(handle, spec) -> SessionResult` — block until the session
  ends (or stalls/times out), then report status.

The base class provides `run(spec)`, the template that chains
`start_session` → `wait_for_completion` → `kill` (the kill runs in a `finally`).
You normally don't override it.

Optional capabilities (default to "unsupported" / no-op):

- `send_text(handle, text)` — nudge a running session. Raises `NotImplementedError`
  by default (an HTTP adapter that can't inject mid-turn leaves it).
- `interactive_argv(spec)` / `interactive_env(spec)` — argv + env that launch the
  CLI **attached** to the caller's terminal, seeded with the prompt, for the
  interactive escalation-resolution flow. HTTP adapters have no terminal and leave
  `interactive_argv` raising.
- `kill(handle)` — tear down the session (no-op default).
- `read_usage(result) -> TokenUsage | None` — parse token usage from the result
  (returns `None` by default).

### References

- [`adapters/opencode_http.py`](../src/bmad_loop/adapters/opencode_http.py) — the
  worked **design stub** for a non-tmux (HTTP/SSE) transport.
- [`adapters/mock.py`](../src/bmad_loop/adapters/mock.py) — the test-only reference
  implementation.
- [`adapters/generic.py`](../src/bmad_loop/adapters/generic.py) — the tmux +
  hook-signal adapter to reuse with a profile rather than subclass.
