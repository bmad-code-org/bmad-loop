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
  `CodingCLIAdapter` ABC — and
  [Shipping a new adapter class out-of-tree](#shipping-a-new-adapter-class-out-of-tree)
  to register it (and the profile that selects it) from a co-installed package
  with **zero core edits**, the same way a transport backend ships out-of-tree.

## Two axes: CLI vs transport

These are independent and abstracted separately:

- **CLI axis** — `CodingCLIAdapter` (`adapters/base.py`): _which_ binary to launch,
  how the prompt is rendered, the hook dialect, where the transcript lives. The
  generic adapter + a TOML profile cover the common case; a CLI needing its own
  adapter class registers one via `register_adapter(...)` (`adapters/registry.py`),
  selected by the profile's `adapter` field and shippable out-of-tree just like a
  transport backend. Most of this guide is about this axis.
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

- **Sessions** — `has_session`, `new_session` (geometry is
  optional: agent sessions pin a fixed pane size because they are observed
  while detached; the control session omits it),
  `kill_session`, `list_sessions`, `session_options` (read a user option
  across all sessions), `set_session_option`.

  One session method carries a default the seam cannot verify for your
  transport: `session_name_key(name)`, the canonical comparison key — two
  names denote the same live session exactly when their keys are equal. The
  default is identity (exact comparison — correct for tmux, whose session
  names are case-sensitive). **If your transport resolves session names
  case-insensitively — or folds them any other way — you MUST override**
  (psmux does: its NTFS port-file store opens names case-insensitively).
  With the inherited identity key, a case-variant of the control session's
  name is not discounted by the removal guard, so the documented recovery
  `bmad-loop delete ctl` wedges behind a false live-session refusal on a
  persisted `CTL` run.

- **Windows** — `new_window` (run a command in a fresh window), `new_parked_window`
  (run a command, then _park_ on a keypress so the exit
  status stays inspectable, then return any attached client to its origin — the
  POSIX `sh -c` recipe is composed from the base's overridable shell-dialect
  hooks, so a non-POSIX backend swaps the dialect fragments, not the method
  body), `list_window_ids`
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
  an optional last-client fallback — see the rule below),
  `available` (is this backend usable on the current host), `version` (the
  binary's version string or `None` — **one bounded line**, folded with
  `fold_version()`; see [the porting guide](porting-to-a-new-os.md)).
- **Registry namespace** (optional; all three default to "no namespace", so a
  transport without one writes nothing) — `has_registry_namespace` (a property
  of the transport: does it namespace sessions by registry at all? Answer
  `True` on every instance if it does, even when no root is currently in
  force: cleanup uses it to tell "no namespace exists" from "running on the
  transport's shared default registry", and only the first lets an untagged
  session be claimed on run-directory evidence — a namespaced backend that
  leaves this at the inherited `False` keeps the pre-namespace historical
  reach, which on a shared registry can kill another project's session),
  `registry_root` (the root your verbs
  currently resolve targets through, for `bmad-loop mux` to disclose; `None`
  from a namespaced backend means "no root in force" — the shared default —
  not "no namespace") and
  `legacy_registries` (instances bound to roots your sessions may predate, for
  the cleanup sweep). Only for a transport that addresses sessions through a
  directory of per-session files, as psmux does through `PSMUX_DATA_DIR`; the
  rules are in [the porting
  guide](porting-to-a-new-os.md#registry-namespaces-only-if-your-transport-has-one).

  **Both client verbs report effect, not dispatch.** They answer what the
  parked-window return path trusts — a bool from `detach_client`, a tri-state
  from `switch_client` (see below) — so a backend with no real detach (herdr —
  only a manual chord releases its client) answers `False`. If your CLI's exit
  code already means "a client moved" you are done (tmux: `detach-client` fails
  with _no current client_); if it does not, **measure** what the verb is meant
  to change — psmux counts the session's attached clients across `detach_client`
  and the last-client fallback, and answers on the drop — and record what the
  measure cannot see in your degradation ledger. Where no measure is available,
  answer `False` from `detach_client` and `None` from `switch_client`, and
  record that gap in the ledger too — per _call_, though, never as the wiring: a
  `switch_client` hard-coded to `None` pins the return path to `UNREACHABLE`, and
  an attended sweep then stops prompting for good. Use the exit code wherever it
  carries the effect. Check the ledger against every verb before you
  trust one measure for all of them: psmux's drop is blind
  to a `switch_client` whose target sits in the same session, which is the common
  case for the return path, so that verb gates on the client count read _before_
  the call and takes the exit code as the verdict instead (#659). A fallback verb
  belongs to the same audit — hang it on the primary verb's failure, never on
  "the primary could not be vouched for", or a hand-back that worked gets undone
  by its own fallback and the undo is read as the success.
  `tui.launch.return_attached_client` reads a `False` from `switch_client` as
  `ATTENDED` — the client never left this window, so an attended sweep keeps
  prompting — and a `False` from `detach_client` as `UNREACHABLE`, which is evidence
  of nothing, so the sweep goes unattended and defers this cycle's decisions to
  `bmad-loop decisions`. That makes `switch_client`'s `False` a **joint claim**:
  no switch happened _and_ the client is still here. A move you cannot _vouch_
  for is only its first half — a timed-out verb, an unreadable count, nothing
  attached to move — so answer `None`, which routes to `UNREACHABLE`: the return
  option survives and the sweep still stops prompting a window the client may
  already have left. Do not reach for that surviving option as the recovery. The
  parked trailer sits behind a blocking read, so it never runs while a `--repeat`
  cycle is stuck on `input()` in the same window — which is exactly what an
  `ATTENDED` the client has walked away from produces. A vacuous `True` remains
  the one answer no backend may give: it announces a hand-back that never
  happened and sends the sweep unattended with the human still sitting there
  (#227).

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
per-server (falling back to the bare id where that grammar would not survive).
Both are replayed opaquely; neither is parsed by core. psmux applies the same
qualification to `new_parked_window`, the `window_id` columns of `list_windows`
and `current_window_id`. To preserve unambiguous lookup, `new_parked_window` must
agree with the `list_windows` column; a backend that qualifies one side only
remains usable but falls back to resolving parked windows by name, which is
ambiguous whenever several kinds share a run id (#482). tmux consumes the token
natively (it coincides with tmux exact-match syntax), so `BaseTmuxBackend` passes
it straight through. A native-id backend calls `parse_target()` first — `None`
means "already a native id, use as-is", otherwise resolve `(session, window)`
yourself; the herdr adapter's
`_parse_target`
([backend.py](https://github.com/pbean/bmad-loop-adapter-herdr/blob/main/src/bmad_loop_adapter_herdr/backend.py))
is the worked example (workspace-by-label → tab-by-name → root pane, resolved
lazily at use time). You MAY override `target()` to emit native ids, but the token must
stay a stable _by-name_ reference: core formats targets ahead of use (a parked
window's return target, for one), so eager resolution to a live id goes stale —
inheriting the default and resolving lazily is almost always right.

**The qualified-id obligation.** The psmux qualifications above are instances of
one rule, and it is the rule — not the instances — a native-id backend needs:
**if an id-minting seam returns anything other than a bare native id, every seam
whose output core compares that id against must emit the identical form, and
every verb the id is replayed through must accept it.** Where a verb cannot take
that form — or cannot take a window scope at all — the backend translates inside
that verb rather than exempting the id from qualification: psmux has no
per-window _user_ options to write to (#310), so its option trio runs the
qualified target through `_option_scope` and writes a session-scoped key carrying
the window's id, refusing a bare id rather than guessing a server (the built-ins
pass straight through to the base). It carried a second such translation on
`select_window` until its 3.3.8 floor made the server resolve a scoped id itself
(psmux/psmux#497). Where the composed grammar cannot carry the value at all, the
backend degrades uniformly across every seam that mints or lists the id, so the
pairings still line up under the fallback — but the fallback is lossy, not a free
escape hatch: a bare psmux id routes by the _caller's_ server, so the degrade
condition must stay the narrow one the grammar forces (#221), never a
convenience. Those pairings are not a closed set: a new one appears whenever a
caller compares two id-producing seams to each other, so treat the rule as the
contract and each documented pairing as an instance of it. Every way of getting
the _form_ wrong is quiet — these are id-shape faults, never transport faults,
which `list_window_ids` must still raise: a mint/list split reads every live
window as instantly dead; a `list_windows`/`current_window_id` split makes the
ctl-window prune kill the window it is running in (#291); a
`list_windows`/`list_window_ids` split reports every kill candidate as verifiably
gone, survivors included (#435). And a verb handed a form it rejects fails or
silently no-ops — the seam is best-effort or its caller swallows the raise, so no
error reaches core either way.

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
which #317's widening requires to answer `False`, not `None`, so the
parked-return path reads it as `UNREACHABLE` and an attended sweep stops
prompting into a window whose client only a manual chord can release; that is a
different widening from `switch_client`'s tri-state above, and `detach_client`
is the verb that stays a bool — the attach
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
`gemini-settings-json` / `copilot-settings-json` / `antigravity-hooks-json`) and
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

| Field                              | Required | Default            | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ---------------------------------- | -------- | ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                             | ✅       | —                  | Profile id, also the `--cli` value and override key.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `binary`                           | ✅       | —                  | Executable to launch (resolved on `PATH`). `bmad-loop validate` also probes it — a name that resolves but fails `--version` (typically a dead WSL/npm shim) is reported as `adapter.binary-unrunnable` at warning severity, #294. Only a **packaged** profile's binary is probed. A project overlay (or an entry-point package's) profile is resolved and reported found but never launched, whatever its `binary` is called — profile fields arrive with a clone, so a project's own config cannot choose which binary this diagnostic launches. The boundary is provenance rather than the spelling of `binary`, because a bare name still resolves into the checkout whenever a checkout-local directory is on `PATH`. Note what it bounds: **which name** is probed, not what that name resolves to. Resolution runs through your `PATH`, so validate launches whatever `PATH` says the CLI is — the same file the session launch itself would run.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `[hooks]`                          | ✅       | —                  | The `HookSpec` table (see below).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `adapter`                          |          | `generic`          | Which adapter **class** drives this CLI — a key resolved against the [adapter registry](#shipping-a-new-adapter-class-out-of-tree), not a fixed enum. `generic` = the bundled tmux + hook-signal adapter; `opencode-http` and `kilo-http` = the bundled HTTP/SSE adapters (OpenCode and the Kilo fork, respectively); an out-of-tree package registers its own. Membership is checked against the **live** registry (at run start, and by `bmad-loop validate`'s `adapter.kind`), never at parse time — so an unknown kind is a clear config error rather than a schema change. Orthogonal to `hooks.dialect = "none"` — hooklessness is about the transport, this is about the driving class — with two qualifications. A back-compat carve-out for files written before this field existed: when the key is **absent** and the dialect is `none`, the kind is `opencode-http` (what hooklessness used to select), not `generic`. And one refused pairing: an explicit `generic` beside `dialect = "none"` is rejected at load — that adapter completes on a `Stop` hook a hookless profile never registers, so the session could only wait out `session_timeout_min`. Hookless on any **other** kind stays legal, which is the decoupling this field exists for; a provider shipping a hookless profile must therefore set `adapter` rather than leave it at the default.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `skill_tree`                       |          | `.claude/skills`   | Project-relative tree this CLI reads skills from (`.agents/skills` for codex/gemini); `bmad-loop init` installs the `bmad-loop-*` skills here. Must be relative.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `prompt_template`                  |          | `{prompt}`         | How the canonical `/skill args` prompt is rendered. Placeholders: `{prompt}` (whole string), `{skill}` (leading slash-command name, no `/`), `{args}` (the remainder).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `launch_args`                      |          | `()`               | Extra argv passed at launch, e.g. `["-i"]` to stay interactive (gemini/copilot).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `bypass_args`                      |          | `()`               | Flags that bypass permission/approval prompts for unattended runs (e.g. `--allow-all-tools`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `model_flag`                       |          | `--model`          | Flag used to pass the model name when one is configured.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `env`                              |          | `{}`               | Extra environment variables for the session.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `usage_parser`                     |          | `none`             | Which transcript token parser to use — one of `claude-jsonl`, `codex-rollout`, `gemini-chat`, `copilot-events`, `none`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `usage_grace_s`                    |          | `0.0`              | Seconds to keep polling the transcript for token totals after the session ends. `0` = read once. Raise it for CLIs that flush totals only on shutdown (copilot writes `modelMetrics` ~1s after the turn-end hook). Must be ≥ 0.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `stop_without_result_nudges`       |          | unset (use global) | Per-adapter floor for Stop-without-result nudges. Leave unset to inherit `limits.stop_without_result_nudges`. Raise it for CLIs that fire a turn-end hook _per response turn_ (copilot's `agentStop`), where the global default of 1 declares them stalled too early. Must be ≥ 0 if set.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `subagent_stop_without_transcript` |          | `false`            | Set `true` for CLIs that fire the turn-end hook for _subagent_ turns too, with an empty `transcriptPath` and a tool-use session id (copilot's `agentStop`). A `Stop` carrying no transcript is then treated as a subagent stop and ignored, so the main session's real turn-end drives completion. Leave `false` and every `Stop` is the main turn-end.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `first_run_note`                   |          | `""`               | Human note printed by `init` about a manual first-run/auth step this CLI needs.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `seed_files`                       |          | `()`               | Project-relative gitignored configs (MCP/CLI settings) a `git worktree add` checkout omits; `provision_worktree` copies them into isolated dev/review worktrees. Must be relative.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `env_fault_patterns`               |          | `()`               | Regex patterns matched line-by-line against the ANSI-stripped tail of a **non-completed** (`timeout`/`stalled`/`crashed`) session's log to classify a transport/API **environment fault** (#194); a match stamps `env_fault` / `env_fault_evidence` on the `SessionResult`. **Which log is per-adapter**, named by `EnvFaultMixin.ENV_FAULT_LOG_SUFFIX`: the tmux adapters scan the pane capture `logs/<task-id>.log`, `opencode-http` scans `logs/<task-id>.server.out` (the `opencode serve` process's own stdout) and never its `.log` conversation transcript. A pattern is only **sound** if the model cannot write to the log it is matched against — a pane capture carries the model's own output, so a story that merely _implements_ rate limiting prints `429`/`quota` in ordinary healthy work. So for a pane-capture profile a pattern must reproduce one of the CLI's **complete error sentences**, taken from captured output. An error-shaped token **plus** a cause on the same line is _not_ sufficient — that is exactly the shape a story writing about the error produces, and it is why the previous doctrine had to be withdrawn (#507). A shipped pattern must also not match **its own line in the profile file**, or a session that prints or diffs its profile reads its own configuration as an outage; the seeded patterns carry single-character classes (`t[o]`, `respons[e]`) for that, changing nothing about what they match and everything about what their source line matches. `test_shipped_patterns_do_not_match_their_own_profile_line` pins the self-match rule and `test_pane_capture_patterns_do_not_match_this_repo` extends the same scan to every tracked file, so a doc bullet or profile comment written in the forbidden shape fails the suite. No quota patterns are seeded for the pane-capture profiles: no captured line exists, and that vocabulary is what a rate-limiting story prints all day. Must be a **list of strings**; compiled and validated at parse time (an invalid regex is a profile error). Matched with the `regex` module under a per-pattern timeout (`ENV_FAULT_MATCH_TIMEOUT_S`), so a pathological pattern can't hang a session teardown. Seeded for `claude` (three patterns over its captured error sentences — connection loss and the two captured provider 5xx refusals, whose statuses are enumerated rather than ranged so an uncaptured `503` stays prose, #507) and `opencode` (provider quota/rate-limit + connection, #323); empty = inert. |

### `HookSpec` (the `[hooks]` table)

| Field         | Required | Meaning                                                                                                                                                                                                                                   |
| ------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dialect`     | ✅       | The CLI's hook-config format — one of `claude-settings-json`, `codex-hooks-json`, `gemini-settings-json`, `copilot-settings-json`, `antigravity-hooks-json`.                                                                              |
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
  `completed`, `stalled`, `timeout`, `crashed`, `over_budget`, `aborted`), `result_json`,
  `session_id`, `transcript_path`, and the optional post-mortem forensics
  `env_fault` / `env_fault_evidence` (set by `_classify_env_fault` when a
  non-completed session is matched as a transport/API **environment fault** —
  see the `env_fault_patterns` profile key).

### Methods

Required (abstract):

- `start_session(spec) -> SessionHandle` — launch the session.
- `wait_for_completion(handle, spec) -> SessionResult` — block until the session
  ends (or stalls/times out), then report status. Poll
  `runs.read_stop_request_mode(run_dir) == "hard"` on both sides of the loop's
  own blocking wait and return `SessionResult(status="aborted")` when it is true
  (#319): that is what makes `bmad-loop stop` land mid-session where a signal to
  the engine cannot be delivered. Return the verdict — never raise, never unlink
  the file (the engine consumes it and attributes the stop). Keep that wait
  short — both bundled adapters cap theirs at 5s and inherit the poll from
  `_ResultFileMixin` — but do not read it as a bound on the stop: a dispatch leg
  that waits out an artifact grace or blocks on a transport call outlasts
  `stop_run`'s 10s grace window on its own, and the force-kill backstop is what
  catches that. Skipping it is
  not fatal: the engine still honors the request at the next item boundary, which
  is where an adapter without the poll leaves the operator waiting.

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
  reconcile, a rescue-to-`completed` is never re-classified. Mixing in
  `EnvFaultMixin` supplies the profile-driven implementation for free; set its
  `ENV_FAULT_LOG_SUFFIX` if your adapter's diagnostics do not land in
  `logs/<task-id>.log`, since the file scanned is part of the patterns' safety
  contract.

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
  `sse_trace` module knob, per-token deltas excluded). That split is also a
  safety boundary: env-fault classification points `ENV_FAULT_LOG_SUFFIX` at
  `<task-id>.server.out` and never at the curated transcript, whose lines are the
  model's own words. The catch of curating:
  what you render is only as good as the event names you pinned — check every
  branch against the running binary, not against the names that read plausibly.
- **Hookless profile.** The profile sets `[hooks] dialect = "none"`: no hook
  registration, no hook-config merge into worktrees; `init`, `validate` and
  worktree provisioning all understand `profile.hookless`. Skills still install
  and copy normally — opencode discovers `.claude/skills/<name>/SKILL.md`
  natively.
- **Reuse the synthesis mixins, never fork them.** `_ResultFileMixin`
  (`result.json` read-back) and `_DevSynthesisMixin` (the whole bmad-build-auto
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

### Shipping a new adapter class out-of-tree

How does a new adapter class ever get _selected_ when it lives in its own package?
Which class drives a CLI is data — the profile's `adapter` field, resolved against
the adapter registry
([`adapters/registry.py`](../src/bmad_loop/adapters/registry.py)) — so a
co-installed package registers its own kind with no edit to any core `.py`, exactly
as a transport backend ships via `bmad_loop.mux_backends`
([the transport contract](#the-transport-contract-for-a-backend-author)).

Advertise **two entry points** in the package's `pyproject.toml`:

- **`bmad_loop.adapters`** → a module whose import registers the kind. Core scans
  the group and imports each advertised module (after the builtins, so a bundled
  name always keeps first registration) before it resolves any adapter:

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

- **`bmad_loop.profiles`** → a callable returning `CLIProfile`s (or an iterable of
  them), so the profile that _selects_ your kind ships with it — no project TOML
  required. Precedence is packaged < entry-point < project, so a project TOML can
  still override it:

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

The two entry-point **values** differ on purpose: the adapter group's is a bare
module path (core only imports it — registration is the import's side effect, and
the entry-point name is just a diagnostic label), while the profile group's names a
provider object core actually calls. Installing the package into bmad-loop's
environment is the entire setup — e.g.
`uv tool install bmad-loop --with <your-adapter>` — with no core edit and no config
step.

Once co-installed, `bmad-loop adapters` lists the kind and the profiles that select
it, `bmad-loop validate` checks the reference (an `adapter.kind` finding, resolved
against the live registry — never a hardcoded set), and a run whose policy
`[adapter] name` points at a profile carrying `adapter = "acme"` selects it. Note
those are two different keys: policy's `[adapter] name` picks the **profile**; the
profile's own `adapter` field picks the **kind**.

A broken package can never break selection: the failure is recorded and reported by
`bmad-loop adapters` (a `warning: external adapter '<name>' failed to load: <reason>`
line, and the same for a failed profile provider) and by the `validate` preflight,
and selection proceeds without it. There is no out-of-tree adapter-**class** package
to copy yet; the packaging pattern is identical to the transport axis's
[bmad-loop-adapter-herdr](https://github.com/pbean/bmad-loop-adapter-herdr), which
registers a `TerminalMultiplexer` rather than an adapter class.

Two seam facts worth internalizing:

- **`needs_mux`** gates whether the run bootstrap resolves and usability-checks the
  shared terminal multiplexer for your family. A tmux/hook family sets it `True`; a
  self-hosted HTTP/SSE family (like opencode) sets it `False` and is never handed a
  `mux`.
- **The `dev` / `plain` split is a pipeline concept, not a per-family branch.** When
  a dev/review session runs the dev primitive (which writes no `result.json`), the
  bootstrap builds the `dev` variant — the `_DevSynthesisMixin`-composed class — and
  threads the project `paths` into it so it can synthesize the result from the spec
  on disk; every other role builds `plain`. Both variants of a family share the
  `(*args, paths, **kwargs)` dev `__init__` contract, so honoring it is all an
  out-of-tree class must do to slot into that machinery.
- **The bootstrap's keyword set grows.** Accept `**kwargs` in both variants.
  `runsetup.make_adapters` builds every class with `cls(**build_kwargs)`, and that
  dict is a _description of the run_ — `run_dir`, `policy`, `profile`, the usage and
  nudge settings, `events_dir` (the run's hook-event channel), plus `mux` for a
  `needs_mux=True` kind and `paths` for the `dev` variant. Core adds to it as the run
  gains things worth describing; `events_dir` arrived that way (#494). A class with a
  closed signature raises `TypeError` on the first keyword it has not heard of,
  before the session starts.

  The bundled families carry closed signatures instead, which is not a second
  pattern to copy: they are edited in the same commit that adds the keyword, and an
  out-of-tree class cannot be. What they do model is the other half of the
  obligation — accept what you have no use for rather than refusing it.
  `opencode_http` takes `events_dir` and immediately `del`s it, because that family
  observes over SSE and fires no hooks, so there is no channel for it to point at.

An entry-point profile is held to the same invariants a TOML profile is (hook
dialect, path containment, `env_fault_patterns` compilation, …): it is validated on
arrival, so a provider that ships an invalid profile is reported rather than
half-installed.

### References

- [`adapters/registry.py`](../src/bmad_loop/adapters/registry.py) — the adapter-kind
  registry and the two entry-point scans described above.
- [`adapters/opencode_http.py`](../src/bmad_loop/adapters/opencode_http.py) — the
  worked example above: a real non-tmux (HTTP/SSE) transport.
- [`adapters/mock.py`](../src/bmad_loop/adapters/mock.py) — the test-only reference
  implementation.
- [`adapters/generic.py`](../src/bmad_loop/adapters/generic.py) — the tmux +
  hook-signal adapter to reuse with a profile rather than subclass; also home of
  the `_ResultFileMixin` / `_DevSynthesisMixin` seams.
