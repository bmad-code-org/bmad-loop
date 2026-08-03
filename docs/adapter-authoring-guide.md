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
  `get_multiplexer()`. The bundled family is tmux-shaped: argv construction and
  the single spawn primitive live in `BaseTmuxBackend` (`adapters/tmux_base.py`),
  with the thin POSIX leaf `TmuxMultiplexer` (`adapters/tmux_backend.py`) and the
  native-Windows leaf `PsmuxMultiplexer` (`adapters/psmux_backend.py`), which
  points the same spawn seam at psmux's own binary via the `_BINARY` class
  attribute; the base and its POSIX leaf are the **only** files allowed to invoke
  `tmux` (and the only place POSIX-shell trailers live). Any other backend
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
`matches(sys.platform)` decides automatic selection and `name` is the key both
the `BMAD_LOOP_MUX_BACKEND` env var and the persisted `[mux] backend` policy key
force. `get_multiplexer()` resolves by precedence: env var → `[mux] backend`
(set with `bmad-loop mux set <name>`, machine-scoped — policy.toml is
gitignored) → the platform default when registered and `available()` → the
first available platform match → the historical tmux fallback. `bmad-loop mux`
lists every registered backend and the selection; same-platform backends need
discriminating `available()` probes (see the
[porting guide](porting-to-a-new-os.md#availability-discriminators-same-platform-backends)).
An out-of-tree backend package makes its registration run by advertising the
module under the `bmad_loop.mux_backends` entry-point group — core imports it
before every selection, so co-installing the package is the whole setup (see
[the porting guide](porting-to-a-new-os.md#shipping-out-of-tree-the-bmad_loopmux_backends-entry-point);
a broken package degrades to a `bmad-loop mux` warning, never a selection failure). There are two build paths: extend `BaseTmuxBackend` (`adapters/tmux_base.py`)
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
  backend swaps the dialect fragments, not the method body), `list_window_ids`
  (which MUST emit the same id form your `new_window` returns — `window_alive` is
  a membership test over it, so qualifying one side and not the other reads every
  live window as dead), `list_windows` (selected fields per window),
  `window_alive`, `kill_window`, `select_window`, `set_window_option`,
  `unset_window_option`, `show_window_option`, `pipe_pane` (tee a pane to a log),
  `send_text`.
- **Client / attach** — `attach_target_argv` (argv that reaches a target, nesting-
  aware), `current_pane_id` / `current_window_id` / `current_session`,
  `current_return_target` (the value an interactive attach records for the
  parked-window return hop, replayed opaquely by `switch_client` and the parked
  trailer; concrete default = the native pane id, so most backends inherit it —
  override only when your ids do not resolve from another session's context,
  as psmux does to emit `=session:%N`), `detach_client` / `switch_client` (with
  an optional last-client fallback — both answer a bool the parked-window
  return path trusts: report **effect**, not that the command was dispatched,
  so a backend with no real detach returns `False`. If your CLI's exit code
  already means "a client moved" you are done (tmux: `detach-client` fails with
  _no current client_). If it does not, **measure** — psmux is the worked
  example: every arm of its `detach-client` / `switch-client` exits 0 whether or
  not a client moved, so the backend counts the session's attached clients
  across the call and answers on the drop. Match the measure to what the verb is
  meant to change, and record what it cannot see: that count reaches a detach,
  and reaches the switch the return path normally performs — the recorded target
  is a pane in the session the client came _from_, so a real switch leaves this
  session — but a switch _within_ this session moves the client between windows
  without moving the count, and so reads as no effect. Failing that way is
  conformant: the caller keeps prompting, which is the safe direction. psmux
  carries exactly that blind spot, in its `Residue:` note. Where even a measure
  is unavailable, answer `False` and record the gap in your degradation ledger;
  a vacuous `True` is the one answer that strands a human),
  `available` (is this backend usable on the current host).

  The caller's two failures are not symmetric, which is what makes the rule
  worth the round-trip. A failed `switch_client` is positive evidence: the
  client is still in the window it was already in, so someone may well be
  watching. A failed `detach_client` is not evidence of anything — `False` there
  means only "no verified hand-back", and covers all three of nothing was
  attached (tmux says so outright), the effect could not be observed, and the
  backend has no detach verb at all (herdr, whose client only a manual chord
  releases). `tui.launch.return_attached_client` reports the two as `ATTENDED`
  and `UNREACHABLE`, and an attended sweep keeps prompting on the first but goes
  unattended on the second. `UNREACHABLE` is therefore the caller's _policy_ for
  an unverified detach rather than a claim the window is empty: assuming a human
  is still there costs a `--repeat` cycle blocked forever on `input()` in a
  window nobody may be viewing, which is the worse way to be wrong. What no
  backend may do is answer `True` when nothing happened — that collapses both
  failures into "the human has their terminal back", which is #227.

**Window targets.** The target-taking methods (`kill_window`, `select_window`,
the window-option trio, `attach_target_argv`, `switch_client`) receive one of two
families: the **seam-canonical target token** `=session[:window]` — formatted by
the concrete `TerminalMultiplexer.target(session, window=None)`, decoded by the
module-level `parse_target()` — or the backend's own **native id** (whatever your
`new_window` returned). Core never hand-assembles the grammar; it calls
`target()`. (Two values are composed by the backend itself rather than by
`target()`, precisely so the seam grammar never has to carry a pane or window
id: the parked-window return target — `current_return_target`, above — and the
native window id, which psmux qualifies to `session:@N` because its ids are
per-server (falling back to the bare id where that grammar would not survive —
the backend owns those conditions, and applies them uniformly, so the
`new_window`/`list_window_ids` symmetry rule holds under the fallback too).
Both are replayed opaquely; neither is parsed by core. psmux applies the same
qualification to `new_parked_window`, the `window_id` columns of `list_windows`
and `current_window_id`; the latter two must agree, since the ctl-window prune
compares them to skip its own window.) tmux consumes the token natively (it coincides with tmux exact-match
syntax), so `BaseTmuxBackend` passes it straight through. A native-id backend
calls `parse_target()` first — `None` means "already a native id, use as-is",
otherwise resolve `(session, window)` yourself; the herdr adapter's
`_parse_target`
([backend.py](https://github.com/pbean/bmad-loop-adapter-herdr/blob/main/src/bmad_loop_adapter_herdr/backend.py))
is the worked example (workspace-by-label → tab-by-name → root pane, resolved
lazily at use time). You MAY override `target()` to emit native ids, but the token must
stay a stable _by-name_ reference: core formats targets ahead of use (a parked
window's return target, for one), so eager resolution to a live id goes stale —
inheriting the default and resolving lazily is almost always right.

Operations that can race a window dying (`pipe_pane`) or a session already being
gone (`kill_session`) must tolerate it rather than raise; everything else raises a
`MultiplexerError` subclass on failure, which call sites catch at the seam (e.g.
`tui/launch.start_detached` turns it into a `LaunchError`) without importing the
backend. `window_alive` uses `list-windows` membership, not `display-message`, because
`display-message -t <dead-window>` exits 0 on tmux.

`tmux_backend.py` is the reference implementation; reading it alongside the ABC is
the fastest way to see exactly what a `new_parked_window` or `session_options` must
produce.

For the **implement-fresh** path, the external herdr adapter
([pbean/bmad-loop-adapter-herdr](https://github.com/pbean/bmad-loop-adapter-herdr),
`src/bmad_loop_adapter_herdr/backend.py`) is the reference worked example — a
backend over [herdr](https://herdr.dev), a cross-platform, agent-aware workspace
manager whose CLI is a different binary family from tmux. Its
mapping: a bmad-loop **session** is a herdr **workspace** (label == session name), a
**window** is a **tab** (one shell pane, whose `root_pane.pane_id` is the native
window id handed back), and the launched command runs via a typed `exec <argv>`
(`pane run` = type + Enter) so process-exit == pane-close == tab-close ==
tmux-identical window death. Where herdr has no analogue, the backend degrades
honestly rather than faking it: session/window **options** (which herdr lacks
entirely) live in a cross-process JSON **sidecar** (atomic swaps for readers, an OS
advisory lock around each read-modify-write so concurrent writers never lose
updates), and `pipe_pane` — herdr has no
tee — runs a per-window **poller** thread that snapshots `pane read` into the log
whenever the content changes, which is exactly enough to drive the two log consumers
a tmux tee would (`generic._log_activity_key`'s stall re-arm and `probe`'s marker
discovery). Its module docstring is a **degradation ledger** of every such
divergence (sidecar options, poller `pipe_pane`, the no-op `detach_client` —
which the widened seam now requires to answer `False`, not `None`, so the
parked-return path reads it as `UNREACHABLE` and an attended sweep stops
prompting into a window whose client only a manual chord can release — the attach
argv, the advisory geometry, the protocol-version policy) — the reference for what
"implement fresh" costs when the host has no tmux-shaped CLI. The operator-facing
view — what a herdr _user_ notices and does — is
[the adapter's operator guide](https://github.com/pbean/bmad-loop-adapter-herdr/blob/main/docs/adapter-multiplexer-herdr.md).

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

Both modes emit the **same single sanitized finding** (markdown to stdout, or to a
file with `--out`; `--json` emits it as a machine-readable JSON document instead).

### SCAN (default — no process launch)

Runs `<binary> --version` / `--help`, locates the newest **already-existing**
session transcript by convention, reads the declared hook config, and infers the
token schema from the transcript. Works whenever you've used the CLI before, with
zero execution risk. This is the right first step for any CLI that already has a
profile (claude/codex/gemini/copilot/antigravity) or that you've run by hand.

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

- **captured hook payloads ship as a schema, never as values** — per event you
  get the field names and the dotted key paths with leaf _types_
  (`tool_input.command:str`); no payload value of any kind reaches the report,
  and a dynamic or credential-shaped key collapses to `<key>`;
- **numbers, booleans, and `null` pass through** elsewhere — token _counts_ are
  not PII;
- **transcript locations are redacted per component** — home → `~`, anything
  that isn't a plain machine identifier (or that embeds your username) →
  `<redacted>`, and your project directory name → a salted alias
  (`project-3f2a9c…`), the same pseudonymization `diagnose` uses;
- `--help` / `--version` text and log tails have the home dir and any emails
  redacted, with a line cap;
- **the rendered report re-scans itself before emitting** — the same
  `sanitize.guard` egress backstop as `diagnose`. A stray occurrence of the
  aliased project name is repaired and disclosed; an email / secret / home path /
  username in the final bytes makes the command **refuse to emit** (message on
  stderr, empty stdout, exit ≠ 0, no `--out` file) rather than ship it.

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
`gemini-settings-json` / `copilot-settings-json` / `antigravity-hooks-json` /
`vibe-hooks-toml`) and
a native→canonical event map, and a `usage_parser` (start with `"none"` until
you've written one).

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
canonical pairing, the payload keys, and the payload **schema** (key paths + leaf
types, never values) — so you can confirm `session_id` / `transcript_path` casing
and that the CLI accepted the hook config for your dialect. If the CLI rejects the config or never fires a hook, the report
says so (with a scrubbed log tail) instead of silently producing nothing.

### 4. Write the `usage_parser`

Turn the report's `token_field_candidates` into a parser in
[`src/bmad_loop/tokens.py`](../src/bmad_loop/tokens.py), following the existing
ones (`tally` for claude, `tally_codex_rollout`, `tally_gemini_chat`) and
registering it in `read_usage`. The report flags **per-call vs cumulative** as a
human call — a `token_count`-style event that carries running totals (codex) is
read differently from per-message blocks that are summed (claude/gemini). Re-run
scan after wiring the parser: the **parsed counts** self-check should now appear.

**`none` is a legitimate final answer, not only a placeholder.** If the report
comes back with no `token_field_candidates`, the CLI may simply not expose usage
anywhere a parser can reach — `antigravity` is the worked example: it counts
tokens (its TUI displays them) but writes them only into an internal SQLite
protobuf blob, never into the transcript. Leave `usage_parser = "none"`, and
record _why_ in the profile so nobody re-litigates it. Do not reach outside the
`(transcript_path) -> TokenUsage | None` contract to scrape a vendor's internal
store: it is undocumented, unversioned, and will break.

**Trust the payload over the docs.** A CLI that reports its own transcript path
in the hook payload is telling you the truth; a path in its documentation may be
illustrative. agy's `hooks.md` shows a workspace-relative
`<workspace>/.gemini/antigravity/transcript.jsonl`, but the live payload's
`transcriptPath` is home-rooted under `brain/<conversationId>/` — and named
`transcript_full.jsonl`. `--probe` prefers the captured path for exactly this
reason; the convention glob is the fallback.

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
| `--out FILE`        | Write the output to a file instead of stdout (the only file the command writes). |
| `--json`            | Emit a machine-readable JSON document _instead of_ the report (honours `--out`). |
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

| Field                              | Required | Default            | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------------------- | -------- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                             | ✅       | —                  | Profile id, also the `--cli` value and override key.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `binary`                           | ✅       | —                  | Executable to launch (resolved on `PATH`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `[hooks]`                          | ✅       | —                  | The `HookSpec` table (see below).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `skill_tree`                       |          | `.claude/skills`   | Project-relative tree this CLI reads skills from (`.agents/skills` for codex/gemini); `bmad-loop init` installs the `bmad-loop-*` skills here. Must be relative.                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `prompt_template`                  |          | `{prompt}`         | How the canonical `/skill args` prompt is rendered. Placeholders: `{prompt}` (whole string), `{skill}` (leading slash-command name, no `/`), `{args}` (the remainder).                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `launch_args`                      |          | `()`               | Extra argv passed at launch, e.g. `["-i"]` to stay interactive (gemini/copilot).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `bypass_args`                      |          | `()`               | Flags that bypass permission/approval prompts for unattended runs (e.g. `--allow-all-tools`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `model_flag`                       |          | `--model`          | Flag used to pass the model name when one is configured.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `env`                              |          | `{}`               | Extra environment variables for the session.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `usage_parser`                     |          | `none`             | Which transcript token parser to use — one of `claude-jsonl`, `codex-rollout`, `gemini-chat`, `copilot-events`, `none`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `usage_grace_s`                    |          | `0.0`              | Seconds to keep polling the transcript for token totals after the session ends. `0` = read once. Raise it for CLIs that flush totals only on shutdown (copilot writes `modelMetrics` ~1s after the turn-end hook). Must be ≥ 0.                                                                                                                                                                                                                                                                                                                                                          |
| `stop_without_result_nudges`       |          | unset (use global) | Per-adapter floor for Stop-without-result nudges. Leave unset to inherit `limits.stop_without_result_nudges`. Raise it for CLIs that fire a turn-end hook _per response turn_ (copilot's `agentStop`), where the global default of 1 declares them stalled too early. Must be ≥ 0 if set.                                                                                                                                                                                                                                                                                                |
| `subagent_stop_without_transcript` |          | `false`            | Set `true` for CLIs that fire the turn-end hook for _subagent_ turns too, with an empty `transcriptPath` and a tool-use session id (copilot's `agentStop`). A `Stop` carrying no transcript is then treated as a subagent stop and ignored, so the main session's real turn-end drives completion. Leave `false` and every `Stop` is the main turn-end.                                                                                                                                                                                                                                  |
| `first_run_note`                   |          | `""`               | Human note printed by `init` about a manual first-run/auth step this CLI needs.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `seed_files`                       |          | `()`               | Project-relative gitignored configs (MCP/CLI settings) a `git worktree add` checkout omits; `provision_worktree` copies them into isolated dev/review worktrees. Must be relative.                                                                                                                                                                                                                                                                                                                                                                                                       |
| `env_fault_patterns`               |          | `()`               | Regex patterns matched line-by-line against the ANSI-stripped tail of a **non-completed** (`timeout`/`stalled`/`crashed`) session's pane log to classify a transport/API **environment fault** (#194); a match stamps `env_fault` / `env_fault_evidence` on the `SessionResult`. Must be a **list of strings**; compiled and validated at parse time (an invalid regex is a profile error). Matched with the `regex` module under a per-pattern timeout (`ENV_FAULT_MATCH_TIMEOUT_S`), so a pathological pattern can't hang a session teardown. Seeded only for `claude`; empty = inert. |

### `HookSpec` (the `[hooks]` table)

| Field         | Required | Meaning                                                                                                                                                                                                                                                                                                                                                                            |
| ------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dialect`     | ✅       | The CLI's hook-config format — one of `claude-settings-json`, `codex-hooks-json`, `gemini-settings-json`, `copilot-settings-json`, `antigravity-hooks-json`, `vibe-hooks-toml`. Every dialect is JSON except `vibe-hooks-toml`, whose `.vibe/hooks.toml` is a flat `hooks = [...]` array of tables — entries name their own event in a `type` field rather than being keyed by it. |
| `config_path` | ✅       | Project-relative path the hook config is written to (e.g. `.claude/settings.json`). Absolute paths are rejected.                                                                                                                                                                                                                                                                   |
| `events`      | ✅       | Map of **native** event name → **canonical** event name. The canonical side must be one of `SessionStart`, `Stop`, `SessionEnd`, `PreCompact`; the native side is whatever the CLI emits (e.g. `agentStop = "Stop"`). At least one entry.                                                                                                                                          |

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
  `completed`, `stalled`, `timeout`, `crashed`, `over_budget`), `result_json`,
  `session_id`, `transcript_path`, and the optional post-mortem forensics
  `env_fault` / `env_fault_evidence` (set by `_classify_env_fault` when a
  non-completed session is matched as a transport/API **environment fault** —
  see the `env_fault_patterns` profile key).

### Methods

Required (abstract):

- `start_session(spec) -> SessionHandle` — launch the session.
- `wait_for_completion(handle, spec) -> SessionResult` — block until the session
  ends (or stalls/times out), then report status.

The base class provides `run(spec)`, the template that chains
`start_session` → `wait_for_completion` → `kill` (the kill runs in a `finally`).
You normally don't override it.

`run(spec)` then chains two post-processing template hooks over the returned
`SessionResult` (both identity no-ops on the base class, so an adapter that needs
neither leaves them alone):

- `_post_kill_reconcile(handle, spec, result)` — runs right after the
  `finally`-kill. A session that finished but lost its final `Stop` can be
  rescued to `completed` here, once window death has settled the liveness the
  live-window verdict had to leave open (see `GenericDevAdapter`).
- `_classify_env_fault(handle, spec, result)` — runs **last**, and only for a
  non-`completed` result with `result_json is None`. An adapter may re-inspect
  its post-mortem log tail and stamp `env_fault` / `env_fault_evidence` on the
  result (see the `env_fault_patterns` profile key). Because it runs after the
  reconcile, a rescue-to-`completed` is never re-classified.

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

### Worked example: the opencode adapter

[`adapters/opencode_http.py`](../src/bmad_loop/adapters/opencode_http.py) is the
shipped non-tmux adapter: it drives [OpenCode](https://opencode.ai) entirely over
`opencode serve`'s HTTP API + SSE event stream (`injection = "http"`,
`observation = "sse"`, `state = "remote"`). Every API fact it relies on was
pinned live against a real 1.18.2 binary and is recorded in the module's
API-contract docstring — start there when the upstream API drifts. The design
decisions worth stealing:

- **One server per session.** The API has no per-session env, but the engine's
  `BMAD_LOOP_*` contract must reach tool subprocesses — so each session gets its
  own `opencode serve` spawned with `cwd = spec.cwd` and `env ⊇ spec.env`.
  Permissions, the model, and a hermetic skills path are injected via the
  `OPENCODE_CONFIG_CONTENT` env var (zero worktree pollution), and each server
  gets its own `OPENCODE_SERVER_PASSWORD` so a foreign process on a recycled
  port can never impersonate it.
- **Map the transport onto the hook-signal semantics** instead of inventing new
  ones: the SSE `session.idle` event ≙ the Stop hook, server-process death ≙
  window death (`crashed`, landed artifact honored), and a poll fallback
  (`GET /session/status` + message `time.completed` proof-of-work) covers SSE
  loss. Completion evidence is gated by a forward-advancing floor so an idle
  event without proof of new work never completes a session — the same
  artifact-distrust invariant the tmux adapters enforce.
- **A transport with no pane still owes the operator a transcript.** The tmux
  adapters get `logs/<task-id>.log` for free by replaying the pane; over HTTP that
  file would hold nothing but `opencode serve`'s own INFO stdout, leaving finished
  runs unreadable. So the transcript is _curated_ off the SSE stream the adapter
  already consumes for control — a second consumer on the same
  `sessionID`-filtered dispatch, costing one `write()` per interesting frame.
  Three sinks, one per audience: `<task-id>.log` is the readable transcript
  (role-prefixed prose plus `[bmad]`-marked `tool:` / `cmd:` / `file:` /
  `perm ask:` / `perm reply:` / `error:` lines), `<task-id>.server.out` takes the
  server's own stdout so it cannot drown that transcript, and
  `<task-id>.sse.jsonl` keeps the raw frames for post-hoc debugging (behind the
  `sse_trace` module knob, per-token deltas excluded). The catch of curating:
  what you render is only as good as the event names you pinned — check every
  branch against the running binary, not against the names that read plausibly.
- **Hookless profile.** The profile sets `[hooks] dialect = "none"`: no hook
  registration, no hook-config merge into worktrees; `init`, `validate` and
  worktree provisioning all understand `profile.hookless`. Skills still install
  and copy normally — opencode discovers `.claude/skills/<name>/SKILL.md`
  natively.
- **Reuse the synthesis mixins, never fork them.** `_ResultFileMixin`
  (`result.json` read-back) and `_DevSynthesisMixin` (the whole bmad-dev-auto
  dev/review synthesis machinery) live in
  [`adapters/generic.py`](../src/bmad_loop/adapters/generic.py).
  `OpencodeDevAdapter(_DevSynthesisMixin, OpencodeHttpAdapter)` plugs into two
  seams: `_probe_alive(handle) -> bool | None` (post-kill liveness; `None` =
  unknown ⇒ the verdict stands) and `_configure_dev_knobs()` (stall-nudge
  budgets). A new adapter class that should run dev/review sessions composes
  the same way.
- **Usage before teardown, kill in `finally`.** Token usage only exists inside
  the server (`state = "remote"`), so it is captured over HTTP before every
  return path; `kill()` is idempotent, sweeps via `atexit`, and force-kills the
  process tree first on Windows (the npm shim is a `.cmd` wrapper — a polite
  kill orphans the real server).

The test story mirrors the transport split:
[`tests/test_opencode_http.py`](../tests/test_opencode_http.py) runs the full
adapter against a scripted stdlib FakeOpencode (no binary, no network beyond
127.0.0.1), and [`tests/test_opencode_live.py`](../tests/test_opencode_live.py)
smoke-checks the pinned HTTP contract against a real local binary — skipped
when absent, zero tokens spent.

### References

- [`adapters/opencode_http.py`](../src/bmad_loop/adapters/opencode_http.py) — the
  worked example above: a real non-tmux (HTTP/SSE) transport.
- [`adapters/mock.py`](../src/bmad_loop/adapters/mock.py) — the test-only reference
  implementation.
- [`adapters/generic.py`](../src/bmad_loop/adapters/generic.py) — the tmux +
  hook-signal adapter to reuse with a profile rather than subclass; also home of
  the `_ResultFileMixin` / `_DevSynthesisMixin` seams.
