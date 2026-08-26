# Terminal multiplexer backends

The orchestrator drives every agent session — and the TUI's launch/attach keys — through
a terminal multiplexer behind a pluggable seam (`TerminalMultiplexer`). Two backends ship
in the box: **tmux** (the POSIX default) and the experimental **psmux** (the native-Windows
default). Additional backends install as separate packages and register themselves
automatically. This page is for operators: which backend is running, how to switch, and how
external backends arrive.
Contributors porting a new backend should start with
[Porting bmad-loop to a new OS](porting-to-a-new-os.md).

## Which backend is running, and how to switch

`bmad-loop mux` lists the registered backends (platform · availability · version) and
shows which one is selected and why. Selection precedence, highest first:

1. `BMAD_LOOP_MUX_BACKEND=<name>` — forces a backend for one invocation.
2. `bmad-loop mux set <name>` — persists the choice into the gitignored, machine-scoped
   `[mux] backend` key in `.bmad-loop/policy.toml` (`mux set --clear` reverts to auto).
3. The platform default — tmux on POSIX, psmux on native Windows — when registered
   and available (psmux's availability rules are below).
4. The first registered backend that matches the platform and is available.
5. If none of the above is available, a historical fallback keeps older setups working:
   the first backend matching the platform regardless of availability, else tmux. The
   selected backend then probes unavailable, and `bmad-loop validate` reports it as such.

The choice applies to the next invocation — switch between runs, not while one is live:
`attach`, `cleanup`, and the TUI all look for sessions in the currently selected backend.
After switching, `bmad-loop validate` reports the selected backend's availability and
version as part of the preflight.

The `AVAILABLE` column answers "does this backend's binary answer on this host", not
"can it be selected here". The two diverge when a foreign-platform binary shares a name
with a local one — on Windows, `tmux` is psmux's compatibility shim, so the tmux row
reads as a real tmux install. Only steps 3–5 above consult the platform, so `bmad-loop
mux` prints a `note:` naming exactly the rows that are **available, foreign to this
platform, and not the selected one** — the combination that looks like a contradiction.
A forced choice (step 1 or 2) reaches those backends anyway, which is why the column is
not gated instead; and a backend forced _into_ selection is left out of the note, since
calling it unselectable would contradict its own `*` marker.

## tmux (the default)

tmux is the reference backend: everything else in these docs — the `bmad-loop-<run-id>`
and `bmad-loop-ctl` session names, the `ctrl-b d` detach chord — describes tmux behavior.
While tmux is the selected backend it is required for launching, attaching, and driving
runs (an external backend brings its own session mechanism instead); pure TUI observation
works without any backend.

**The supported floor is tmux 3.2.** No version gate enforces it — tmux is selected on the
presence of the binary alone — so an older tmux is not refused up front. It does not follow
that an older tmux merely runs untested, because these adapters do not all speak the same
vintage: environment injection goes out as `new-window -e KEY=VALUE`, which tmux gained in
3.0, so anything older fails loudly at window creation, while other forms — the `=`
exact-match target prefix among them — are considerably older and parse fine. Do not take the
oldest form the argv happens to accept as the floor either. Between whatever an old tmux still
parses and 3.2 lies a range that may run without complaint, and that nobody tests.
psmux carries a separate version requirement of its own, for unrelated reasons — see below.
The two floors are independent and neither implies the other.

## psmux (native Windows, experimental)

On a native-Windows host the bundled **psmux** backend is the platform default. psmux is a
ConPTY tmux re-implementation that speaks the tmux CLI through its own `psmux` binary, so it
reuses tmux's session/window model — the `bmad-loop-<run-id>` session names carry over, and
the control session's does with a per-registry suffix
([below](#where-psmux-sessions-live-the-per-project-registry)). It is selected automatically
when available; `available()` requires the
`psmux` and `pwsh` (PowerShell) binaries on `PATH` and a psmux **3.3.8 or newer** (older
releases report unavailable and selection falls through: 3.3.6 and below can force-kill a
recycled PID during teardown, and the backend's verbs are written against fixes that landed
in 3.3.8 rather than routing around the defects they close). Native Windows is still
experimental — see the
[roadmap](ROADMAP.md#native-windows-multiplexer-backend) for the remaining work. WSL is
unaffected: it _is_ Linux and uses tmux — provided bmad-loop was installed with the
distro's own Python. WSL appends the Windows `PATH` to its own, so a Windows-installed
bmad-loop is reachable from the bash prompt; that process reports `win32` and takes the
psmux default no matter how Linux the shell looks. `bmad-loop validate` names the
selection reason on every host, and warns (`host.win32-on-wsl-path`) when a `win32` interpreter
is working on a `\\wsl.localhost\...` project (#332).

Two model differences matter if you port a backend or read psmux argv. psmux runs one server
per session, so window ids are minted per server and the backend session-qualifies every id it
hands out (`session:@N`). And psmux has no per-window user options — one scope exists per
server — so the window-option verbs and the `@`-prefixed columns of `list_windows` are served
by a session-scoped option whose key carries a seam-owned marker plus the window id
(`@bmad_project__blw@3` for window `@3`; no real-world naming convention collides with the
marker, so the channel's cleanup sweeps stay off hand-written user options unless one
deliberately imitates the seam). Both are properties of psmux's model, not gaps awaiting an
upstream release. Practical consequence: such a value is **not** readable via
`psmux show-options -w` by hand — read it with
`psmux show-options -qv -t <session> "@bmad_project__blw@N"` instead. Session-scoped options need
no such substitute — one server per session means that server's single map _is_ the session's —
but the value has to read back verbatim either way, so session-scoped `@` options are gated the
same way. One visible limit: a value that cannot make that round trip is refused with a stderr
warning and the option reads as unset. Ordinary Windows paths pass — spaced, UNC, apostrophed,
trailing-separator alike. What is refused is what the backend's own listing parse cannot carry
back: a value containing a double quote or a line break, or one with leading/trailing
whitespace (both reads strip it). A `-`-leading value is refused too, because psmux still
treats it as a flag. The project ownership tag no
longer meets this gate: it is stored as a hex digest of the project path, transportable by
construction ([#419](https://github.com/bmad-code-org/bmad-loop/issues/419)), so sessions stay
tagged whatever the path and the run-dir fallback remains only for genuinely untagged state.

### Where psmux sessions live: the per-project registry

psmux resolves every target through a **registry** — the directory named by `PSMUX_DATA_DIR`,
holding one `.port`/`.key`/`.sid`/`.pid` set per session — and defaults it to
`%USERPROFILE%\.psmux`. bmad-loop points it at a per-project root instead:
`<state root>/<project key>/_mux`, where `<state root>` is the location
[`BMAD_LOOP_STATE_DIR`](../README.md#environment-variables) resolves to and `<project key>` is
the same digest of the project's resolved path that tags session ownership. The export happens
once per bmad-loop process, before anything spawns psmux, so `run`, `cleanup`, `stop`, `attach`
and the TUI all agree on one registry without any of them persisting it.

**The consequence to know: a bare `psmux ls` does not show these sessions.** It reads psmux's
default registry, finds nothing there, and answers "no sessions" — not an error. The same goes
for `psmux attach -t bmad-loop-<run-id>` typed by hand. To reach them, export the same root
first:

```powershell
$env:PSMUX_DATA_DIR = 'C:\Users\you\AppData\Local\bmad-loop\state\<project key>\_mux'
psmux ls
```

`bmad-loop mux` prints that root and a paste-ready export line for the current project, which is
the reliable way to get it — do not compose the path by hand. `bmad-loop attach` needs none of
this: it runs the client itself, under the root it just exported.

Two further consequences:

- **The control session is now per project, and its name says so.** `bmad-loop-ctl` was one
  session shared by every project on the machine; separate registries mean one per project — and
  the name must change with the scope, because psmux's duplicate-server guard is a mutex keyed on
  the session name alone, across every registry in your login session (`Local\psmux-session-<name>`
  — a per-login-session object namespace, not a machine-global one), so a fixed name would let only
  one registry there hold a control session and every other project's launch would fail as a
  duplicate. On psmux the session is therefore `bmad-loop-ctl-<16-hex registry digest>`; the
  TUI's toasts name the exact session. On tmux (no registries) the shared `bmad-loop-ctl` is
  unchanged.
- **Sessions created before this change are in whichever registry the old build inherited**,
  and `bmad-loop cleanup` sweeps those too — but only for sessions that carry this project's
  ownership tag and are not still running. There are two such registries, because the old build
  simply used whatever `PSMUX_DATA_DIR` it found: psmux's **default** registry on a machine that
  never set the variable, and **your own exported root** on one that did — the same root
  bmad-loop now overrides (it remembers what it displaced, for exactly this sweep). The leftovers
  line names the registry each session is in, so the one to open is the one it names.

  Whatever the sweep leaves standing is **named on stderr** (and in `cleanup --json`, at
  `sessions.legacy_leftovers`) so a removal count never quietly stands for a partial migration.
  Three kinds stay behind by design:

  - An **untagged** `bmad-loop-<run-id>` session. In a shared registry a matching run directory
    here is not proof of ownership — run ids are only unique within one project, and `--run-id`
    is caller-supplied — so claiming one could kill a live session belonging to another project.
  - **A live session of your own.** Cleanup never kills a run whose engine is still going,
    wherever it lives.
  - The pre-upgrade **`bmad-loop-ctl` control session**. The ctl-window sweep runs against the
    current registry only.

  The list is taken by _presence_ — what the registry still holds once the sweep has run — so it
  covers a kill that silently did not land as well as one that was never attempted. (`cleanup`'s
  own `removed` count is still the pre-kill plan, the ceiling `cleanup --json`'s
  `sessions.removed` has always documented; the leftovers line is what catches the difference.)

  These are one-time artifacts of the upgrade. To see them, put the shell on the registry the
  leftovers line named. For psmux's default registry that means a shell with **no**
  `PSMUX_DATA_DIR` set; for a root of your own, export that root:

  ```powershell
  Remove-Item Env:PSMUX_DATA_DIR      # psmux's default registry
  # ...or, for the root bmad-loop displaced, the one the message named:
  $env:PSMUX_DATA_DIR = 'D:\your-own-registry'

  psmux ls
  psmux list-windows -t bmad-loop-ctl -F '#{window_index}: #{window_name}'
  ```

  > **Do not reach for `psmux kill-session -t bmad-loop-ctl`.** That session is machine-wide, and
  > `kill-session` kills every child process in every one of its windows — across every project
  > that used it. bmad-loop's own windows run the engine first and park only once it exits, so a
  > window there is a **live run** until it isn't. If `bmad-loop cleanup` reported a leftover
  > control session while any pre-upgrade run was still going, that run is what you would be
  > killing.

  **`list-windows` cannot tell you which is which.** A live window and a parked one render
  identically — `1: run-… (1 panes) [120x30]` — and so do `#{pane_pid}` and `#{pane_dead}`: the
  parked window's shell is still alive, holding the prompt. The `[bmad-loop exited …]` banner is
  pane _text_, so the one command that answers it is `capture-pane`:

  ```powershell
  psmux capture-pane -p -t bmad-loop-ctl:1     # per window index from the listing above
  ```

  A window whose last line reads `[bmad-loop exited <code> — press enter]` has finished. One that
  does not is still running — take its `run-<run-id>` / `sweep-<run-id>` name to
  `bmad-loop list --project <that project>` and stop it through bmad-loop
  (`bmad-loop stop --project <that project> <run-id>`, or run it from that project)
  rather than through psmux.

  **`stop` works there, but it does not sweep the old registry.** The stop reaches the engine
  _process_, not a session: it lodges a request in the run directory and signals the recorded pid,
  and a run directory belongs to a project and a run id, not to a registry. So a pre-upgrade run
  stops, and a still-live engine tears its own window down under the registry it was launched with.
  What `stop` does not reach is its own backstop kill — that one addresses the registry bmad-loop
  exported for this project, so an agent session an already-dead engine left behind in an older
  registry stays standing and the run is still marked stopped. It is `bmad-loop cleanup` that
  reaches those, through the legacy pass described above: a session carrying this project's tag is
  killed there, and anything the tag rule declines is named on the leftovers line with the registry
  it is in. `stop` is deliberately not widened to match — a by-name kill in a registry shared with
  other projects, without that tag proof, could take a neighbour's same-named session, since run
  ids are unique only within one project.

  Finished windows can simply be left parked — a parked window costs one idle shell. To close one,
  `psmux kill-window -t bmad-loop-ctl:<index>` kills only that window's own children, so it is
  safe once `capture-pane` has shown you the banner. Closing the last window ends the session too,
  but only while psmux's `exit-empty` is on; it is on by default, and `psmux show-options -g
exit-empty` says which you have. With it off, an empty session stays.

Setting `PSMUX_DATA_DIR` yourself does **not** move bmad-loop's registry. bmad-loop derives the
root from the project and the state root and exports it over whatever it finds, saying so once on
stderr when it replaced something. Your value is left alone for your own psmux sessions — it is
psmux's variable, not bmad-loop's, so bmad-loop overrides it rather than refusing to run.

That is deliberate, and the reason is worth having: an honoured export would make the registry a
function of the shell a command happened to start in. A TUI launched from the Start menu carries no
profile environment and would derive; a run started from a dev shell whose profile exports a root
would honour it — two registries on one machine, and a live session reading as gone in one of them.
Nor can bmad-loop tell the two apart: a value typed once in one shell and a value a profile exports
into every shell arrive identically, and they want opposite answers.

To reach these sessions from a bare psmux, point your shell at bmad-loop's root rather than the
other way round. `bmad-loop mux` prints it ready to paste:

```powershell
$env:PSMUX_DATA_DIR = '<root from bmad-loop mux>'
psmux ls
```

One registry serving both bmad-loop and your own psmux is a reasonable thing to want and is not
available today; it needs a preference you state rather than one bmad-loop guesses at.

Two consequences of deriving, both benign:

- A pane child agrees with a clean process by construction — `bmad-loop --project <other>` run from
  a pane of this project's session gets the other project's root, not this one's.
- The registry follows `BMAD_LOOP_STATE_DIR`, so a process under a different state root uses a
  different registry. It moves with the out-of-tree state the state root already holds — each run's
  control-plane directory and its hook-event channel. The run _directory_ is not among them: that
  stays in-tree at `<project>/.bmad-loop/runs` and moves with the project.

How the state root reaches the windows bmad-loop opens: coding-CLI windows (the engine's sessions
and the probe launcher's window) are **told** it through their env dict, which travels inside the
command the window runs; everything else — a session's initial shell window, the engine windows
the TUI parks — inherits it from the multiplexer server, as it always has. The told entry is
always this process's own answer: a `BMAD_LOOP_STATE_DIR` declared in a profile's `[env]` table is
overwritten with the resolved root, and when no root can be derived the entry is removed rather
than forwarded — the window then inherits and fails exactly as its parent does, instead of being
aimed at a state root (and so a registry) its own orchestrator cannot see.

One psmux mode breaks that inheritance and is **not supported**: `PSMUX_BARE_ENV=1` (psmux's
escape hatch for a Windows environment block near the 32 KB `CreateProcessW` limit) empties a pane
child's environment and rebuilds it from a 14-name allowlist that drops both
`BMAD_LOOP_STATE_DIR` and the `LOCALAPPDATA` its default falls back to — so an inherited value is
no value at all. (`TMUX` is still set in the pane, but by psmux afterwards, not by the allowlist,
which does not contain it.) A `bmad-loop` run in a window-0 shell or a parked engine window under
that switch then re-derives the state root from what survived, and lands somewhere else — reading
its own live session as gone — whenever `BMAD_LOOP_STATE_DIR` was in force or `LOCALAPPDATA` points
outside `%USERPROFILE%\AppData\Local`; `USERPROFILE` survives the clear, so a default profile
re-derives the same root. Coding-CLI windows keep working — their env rides the in-command
transport, which psmux applies after the bare-env clear (source-read at v3.3.8). bmad-loop warns
once per process when the switch is on in its environment (the server, not this process, is what
reads the switch at pane spawn, so a server already running with it on under a clean client is not
detected); the
remedy is to unset `PSMUX_BARE_ENV` for bmad-loop's sessions.

If the state root cannot be derived at all — `BMAD_LOOP_STATE_DIR` set to a relative path, say —
bmad-loop has no registry of its own to point at, says so, and leaves whatever `PSMUX_DATA_DIR` you
had in force. `cleanup` then refuses to claim an untagged session on run-directory evidence, because
that evidence only holds in a registry bmad-loop derived: a run id is unique within a project, not
across the registry you are sharing with it.

## External backends

Every backend beyond the two bundled ones is a separate package that you co-install with bmad-loop; it
registers itself through the `bmad_loop.mux_backends` entry-point group, so installation
is the entire setup — the new backend simply appears in `bmad-loop mux`, selectable and
persistable like a bundled one. With bmad-loop installed as a `uv` tool:

```bash
uv tool install "bmad-loop @ git+https://github.com/bmad-code-org/bmad-loop.git" \
  --with "<adapter package or git URL>"
bmad-loop mux            # the new backend's row appears
bmad-loop mux set <name> # persist for this machine
```

The reference external backend is
**[bmad-loop-adapter-herdr](https://github.com/pbean/bmad-loop-adapter-herdr)** —
[herdr](https://herdr.dev) is a cross-platform, agent-aware terminal workspace manager
whose agent-status sidebar is a natural fit for watching runs. What changes from your
seat on herdr (one manual detach chord, `ctrl+b q`; polled logs; a JSON state sidecar) is
documented in
[that repo's operator guide](https://github.com/pbean/bmad-loop-adapter-herdr/blob/main/docs/adapter-multiplexer-herdr.md).

Two operational notes that apply to any external backend:

- **A broken adapter package never breaks bmad-loop.** If an installed backend fails to
  import, selection proceeds without it; `bmad-loop mux` prints a
  `warning: external backend '<name>' failed to load: <reason>` line and `validate` notes
  the same. The fix is usually reinstalling or upgrading the adapter.
- **`mux set --force` covers late registrations.** A backend that only registers on some
  other machine (where the package IS installed) can still be persisted in a shared
  workflow with `bmad-loop mux set <name> --force`.
