# Terminal multiplexer backends

The orchestrator drives every agent session, and the TUI's launch and attach keys, through
a terminal multiplexer behind a pluggable seam (`TerminalMultiplexer`). Two backends ship:
**tmux** (the POSIX default) and the experimental **psmux** (the native-Windows default).
Other backends install as separate packages and register themselves. This page is for
operators: which backend is running, how to switch, and what each one needs. Authors of a
new backend start with [Porting bmad-loop to a new OS](porting-to-a-new-os.md) and
[the transport contract](adapter-authoring-guide.md#the-transport-contract-for-a-backend-author).

## Which backend is running, and how to switch

`bmad-loop mux` lists the registered backends (platform · availability · version) and
shows which one is selected and why. Selection precedence, highest first:

1. `BMAD_LOOP_MUX_BACKEND=<name>` forces a backend for one invocation.
2. `bmad-loop mux set <name>` persists the choice in the `[mux] backend` key of the
   gitignored, machine-scoped `.bmad-loop/policy.toml`. `mux set --clear` reverts to auto.
3. The platform default (tmux on POSIX, psmux on native Windows), when registered and
   available.
4. The first registered backend that matches the platform and is available.
5. Fallback: the first backend matching the platform regardless of availability, else
   tmux. The selected backend then probes unavailable, and `bmad-loop validate` reports it.

A forced name (1 or 2) skips the platform and availability checks; a forced backend that
probes unavailable is still used, with a one-time warning. A name that matches no
registered backend fails.

Switch between runs, not while one is live: `attach`, `cleanup` and the TUI look for
sessions in the currently selected backend. `bmad-loop validate` reports the selected
backend's availability and version.

The `AVAILABLE` column answers "does this backend's binary answer on this host", not "can it
be selected here". On Windows, `tmux` can be psmux's compatibility shim, so the tmux row
reads as available. `bmad-loop mux` prints a `note:` naming each row that is available,
foreign to this platform, and not selected; such a backend is reachable only by forcing it.

## tmux (the default)

tmux is the reference backend: the `bmad-loop-<run-id>` and `bmad-loop-ctl` session names
and the `ctrl-b d` detach chord elsewhere in these docs describe tmux. While tmux is
selected it is required for launching, attaching and driving runs; pure TUI observation
works without any backend.

The supported floor is **tmux 3.2**. Nothing enforces it: tmux counts as available when the
binary is on `PATH`. Environment injection uses `new-window -e KEY=VALUE`, which a much
older tmux rejects at window creation.

### A server started under another state root

A tmux server is long-lived, and its panes inherit the environment the _server_ started
with. A server started under one state root (`BMAD_LOOP_STATE_DIR`, `XDG_STATE_HOME` or
`HOME`) and reused by a bmad-loop under another hands every new shell the first root.

- Engine windows the TUI parks are not affected: the TUI passes each one its state root on
  the command line. A TUI that cannot derive a state root refuses the launch.
- A shell is affected: window 0 of the control session, or any shell opened there, inherits
  the server's root, so a `bmad-loop` command typed there uses it. When the TUI launches into
  its control session it compares the root a new window would inherit with its own and
  notes once, naming both, when they differ. A shell that must agree can
  `export BMAD_LOOP_STATE_DIR=<root>`.

psmux (3.4.0 or newer) answers the same query, so the note fires on psmux too. psmux runs one
server per session, so the inherited environment is that session's own server's. Its
registry is keyed on the state root, so a launcher normally reaches only servers started
under its own root; the note covers the cases where it does not.

## psmux (native Windows, experimental)

On a native-Windows host the bundled **psmux** backend is the platform default. psmux is a
ConPTY tmux re-implementation driven through its own `psmux` binary, so it reuses tmux's
session and window model and the `bmad-loop-<run-id>` session names.

Requirements:

- **psmux 3.4.0 or newer** and **`pwsh`** on `PATH`. `available()` requires both binaries
  and reads psmux's reported version; an older or unreadable version reports unavailable
  and selection falls through.
- **PowerShell 7.3 or newer** as `pwsh`. The backend resolves `pwsh` to one absolute path,
  reads its version once per process, and refuses to create a session or open a window
  under an older, unreadable or unresolvable one, naming what it found. A forced psmux is
  refused the same way. Every window and the pipe-pane log sink run with that same `pwsh`.
- A new session's initial window runs psmux's own default shell. Window commands use
  PowerShell's Standard argument passing, so a `.cmd` or `.bat` launcher (an npm CLI shim)
  receives empty or quoted arguments intact.

Native Windows is experimental; the remaining work is on the
[roadmap](ROADMAP.md#native-windows-multiplexer-backend).

**WSL** is Linux and uses tmux, provided bmad-loop was installed with the distro's Python.
WSL appends the Windows `PATH`, so a Windows-installed bmad-loop is reachable from the bash
prompt; that process reports `win32` and selects psmux. `bmad-loop validate` names the
selection reason and warns (`host.win32-on-wsl-path`) when a `win32` interpreter works on a
`\\wsl.localhost\...` project.

**Window options.** psmux has one option scope per server, so bmad-loop stores a window
option as a session option whose key carries the window id: `@bmad_project__blw@3` for
window `@3`. `psmux show-options -w` does not show these values; read one with
`psmux show-options -qv -t <session> "@bmad_project__blw@N"`. Window ids are
session-qualified (`session:@N`).

**Refused option values.** An `@` option value must read back verbatim. A value that is
empty, contains a double quote or a line break, or has leading or trailing whitespace is
refused with a stderr warning, and the option reads as unset. So are three `-`-leading
shapes that psmux's client rewrites before parsing: `-X=value` (one
letter, `=`, then at least one character), any value starting with `-t`, and `--`. Other
`-`-leading values (`-u`, `-1=x`, `--x=y`) pass. Ordinary
Windows paths (spaced, UNC, apostrophed, trailing separator) pass. The project ownership
tag is a hex digest of the project path, so it always passes.

### Where psmux sessions live: the per-project registry

psmux resolves every target through a **registry**: the directory named by
`PSMUX_DATA_DIR`, holding one `.port`/`.key`/`.sid`/`.pid` set per session. psmux defaults it
to `%USERPROFILE%\.psmux`. bmad-loop uses a per-project root instead:
`<state root>/<project key>/_mux`, where `<state root>` is what
[`BMAD_LOOP_STATE_DIR`](reference/configuration.md#environment-variables) resolves to and
`<project key>` is the 16-hex digest of the project path that also tags session ownership.
Every bmad-loop process exports this root before it spawns psmux, so `run`, `cleanup`,
`stop`, `attach` and the TUI agree on one registry.

**A bare `psmux ls` does not show these sessions.** It reads psmux's default registry and
answers "no sessions". The same goes for a hand-typed `psmux attach -t bmad-loop-<run-id>`.
`bmad-loop mux` prints the root and a paste-ready export line; use it rather than composing
the path:

```powershell
$env:PSMUX_DATA_DIR = '<root from bmad-loop mux>'
psmux ls
```

`bmad-loop attach` needs none of this; it runs the client under the root it exported.

**The control session is `bmad-loop-ctl`**, one per project registry on psmux. A session
named `bmad-loop-ctl-<16 hex>` is also a control session to bmad-loop, so `cleanup` never
prunes it. Nothing launches into it, though, so the TUI's attach and stop keys and the
parked-window sweep do not reach its windows. To end one, let its runs finish
(`bmad-loop stop` reaches a live engine wherever it runs) and check that every window shows
the exit banner (see `capture-pane` below). Then, unless the name may be a run's own session
(below), run `psmux kill-session -t bmad-loop-ctl-<16 hex>`.

`cleanup` names the one an earlier release started for this project in its registry
(matched by that exact name, since a run id `ctl-<16 hex>` gives an agent session the same
shape) on stderr, in a TUI cleanup toast and in `sessions.superseded_ctl_sessions`, never
with the `not migrated` leftovers; the command is ready to paste
(`sessions.legacy_kill_commands` in `--json`). `attach` names it when it finds no run or nothing to
attach, with `switch-client` instead of `attach` when run inside a psmux pane. A name that
may be a run's own session (a run of this project has that id, or the session carries a run's
project tag, as a neighbour's run can in a shared registry) is named with no command; do not
end it by hand until you know which it is.

A name `cleanup` could not confirm (its derivation failed, or the listed name no longer
matches it) is also named with no command, beside the fault that withheld it.

**If the state root cannot be derived** (for example, `BMAD_LOOP_STATE_DIR` is a relative
path), bmad-loop says so and leaves whatever `PSMUX_DATA_DIR` was in force. `cleanup` then
claims only sessions carrying this project's ownership tag.

### Using your own `PSMUX_DATA_DIR`

By default an ambient `PSMUX_DATA_DIR` does **not** move bmad-loop's registry: bmad-loop
exports its derived root over it and notes once on stderr that it replaced a value. Your
value still applies to your own psmux sessions.

If your profile exports `PSMUX_DATA_DIR` into **every** shell and you want one registry for
both, turn on the switch in the project's `policy.toml`:

```toml
[mux]
honor_ambient_psmux_data_dir = true
```

bmad-loop then uses your value as the registry, and `bmad-loop mux` reports it as your own
`$PSMUX_DATA_DIR`, honoured, alongside the derived root it would otherwise use. Engine
windows the TUI parks are handed the registry on their command line. Leave the switch off
for a value typed into one shell: a process started without it would use the derived root,
and the session would read as gone there.

- It is a yes/no switch; policy can never set a path.
- An empty or relative `PSMUX_DATA_DIR`, or none, changes nothing: the derived root is used.
- A value shaped like a derived root (`<16-hex project key>\_mux`) is never treated as
  yours; a pane child re-derives.
- The control session gets its own name in your registry.
- A running TUI keeps the registry it started with and refuses to launch runs after the
  switch flips, until you restart it (`bmad-loop tui`).
- Sessions left in the derived root are still reached by `bmad-loop cleanup`, which claims a
  session in your registry only by its ownership tag.

The registry follows `BMAD_LOOP_STATE_DIR`, so a process under a different state root uses a
different registry. Coding-CLI windows receive the state root in their env dict, which
travels inside the command the window runs; a `BMAD_LOOP_STATE_DIR` in a profile's `[env]`
table is overwritten with the resolved root.

### `PSMUX_BARE_ENV` is not supported

`PSMUX_BARE_ENV=1` makes psmux rebuild a pane child's environment from a short allowlist
that drops `BMAD_LOOP_STATE_DIR` and `LOCALAPPDATA`. The windows bmad-loop launches keep
their roots (parked engine windows get them on the command line; coding-CLI windows carry
their env inside the command). What breaks:

- A `bmad-loop` command typed into a window-0 shell re-derives the state root from what
  survived and can read a live session as gone, whenever `BMAD_LOOP_STATE_DIR` was set or
  `LOCALAPPDATA` points outside `%USERPROFILE%\AppData\Local`.
- Every variable a window's env dict does not name is dropped. A coding CLI can miss
  credentials or configuration, and without `APPDATA` git stops reading global excludes in
  `%APPDATA%\Git\ignore`.

bmad-loop warns once per process when the switch is set in its environment, and the TUI
repeats the warning. A server already running with it on is not detected. Unset
`PSMUX_BARE_ENV` for bmad-loop's sessions.

### Sessions in another registry

`bmad-loop cleanup` also sweeps the registries this project's sessions may sit in from
before the per-project root: psmux's default registry, and any `PSMUX_DATA_DIR` root
bmad-loop displaced. It kills only sessions that carry this project's ownership tag and
whose run is not still going. Whatever it leaves is named on stderr as
`left in <registry> (<label>): …` and in `cleanup --json` at
`sessions.legacy_leftovers`; a registry that could not be asked at all is named as
`legacy registry not checked …` (`sessions.legacy_unverified`, a toast in the TUI).
The label is `not migrated`, except for a registry derived from another project path,
which gets its own label (below).

Three kinds stay behind by design:

- An **untagged** `bmad-loop-<run-id>` session. Run ids are unique only within a project, so
  a matching run directory is not proof of ownership in a shared registry.
- **A live session of your own.** Cleanup never kills a run whose engine is still going.
- The shared **`bmad-loop-ctl` control session** in that registry.

**Sessions in another registry.** A displaced root shaped like one bmad-loop derived
(`<project tag>/_mux`) under a tag that is not this project's is reported in full like any
other, but its line reads `left in <registry> (another project's derived registry, or this
project's from before a rename): …` instead of `(not migrated)`. That is the root
`bmad-loop mux` printed for another project, or for this one before it was renamed or moved,
still exported in the shell. Its control session and untagged sessions most likely belong to
that project: do not remove them from here. A session tagged as this project's is yours, but
it may still be running (cleanup never kills a live run's session), so check it before removing
it. A
derived-shaped root whose tag cannot be compared gets the same wording. The
`note: using bmad-loop's own psmux registry …` line that names the displaced root says it is
such a registry, and `cleanup --json` is unchanged.

To inspect one, put the shell on the registry the message named:

```powershell
Remove-Item Env:PSMUX_DATA_DIR      # psmux's default registry
# ...or the root the message named:
$env:PSMUX_DATA_DIR = 'D:\your-own-registry'

psmux ls
psmux list-windows -t bmad-loop-ctl -F '#{window_index}: #{window_name}'
```

> **Do not run `psmux kill-session -t bmad-loop-ctl`.** That session can be shared by every
> project that used the registry, and `kill-session` kills every child process in every
> window. A window there runs its engine first and parks only once the engine exits, so it
> is a **live run** until it shows the exit banner.

`list-windows` cannot tell a live window from a parked one. Read the pane text:

```powershell
psmux capture-pane -p -t bmad-loop-ctl:1     # per window index from the listing above
```

A window whose last line reads `[bmad-loop exited <code> — press enter]` has finished. One
that does not is still running: find its `run-<run-id>` / `sweep-<run-id>` name with
`bmad-loop list --project <that project>` and stop it with
`bmad-loop stop --project <that project> <run-id>`, not through psmux. `stop` reaches the
engine process wherever its session lives, but an agent session a dead engine left in an
older registry is reached only by `bmad-loop cleanup`.

A finished window can stay parked; it costs one idle shell. To close one,
`psmux kill-window -t bmad-loop-ctl:<index>` kills only that window's children, so it is
safe once `capture-pane` has shown the banner.

## Recovering a stranded session

`bmad-loop delete` and `bmad-loop archive` refuse to remove a run while its agent session `bmad-loop-<run-id>` is still live, unless the session is tagged to another project: `run <run-id>: refusing to delete its directory while its agent session is still live — … (or pass --force)` (`archive` for archive). To clear the refusal:

1. Confirm the session is this project's: `bmad-loop attach <run-id>`.
2. `bmad-loop cleanup`, then run `delete` or `archive` again.
3. Or pass `--force`. It stops a live engine first, then removes the run without checking for a session, and leaves any orphaned session running.

A removal can still strand a session when the session listing misses it, because the multiplexer binary is not on the removing process's `PATH`. To clear a stranded session:

1. Put the multiplexer back on `PATH`.
2. Run `bmad-loop cleanup`. It prunes a session tagged to this project even when its run directory is gone.
3. For an untagged session, inspect it by hand. `bmad-loop attach` cannot reach it, because it needs the run directory. On psmux, paste the `$env:PSMUX_DATA_DIR = …` line `bmad-loop mux` prints first ([why](#where-psmux-sessions-live-the-per-project-registry)). Then attach with `psmux attach -t bmad-loop-<run-id>` (`tmux attach -t …` on tmux).
4. Once you have confirmed from its contents that it is yours (the name does not carry the project), kill it: `psmux kill-session -t bmad-loop-<run-id>` (or `tmux kill-session -t …`).

Never kill a `bmad-loop-ctl…` control session this way: its windows can be live runs.

## External backends

Every backend beyond the two bundled ones is a separate package co-installed with
bmad-loop. It registers through the `bmad_loop.mux_backends` entry-point group, so
installing it is the whole setup: the backend appears in `bmad-loop mux`, selectable and
persistable like a bundled one. With bmad-loop installed as a `uv` tool:

```bash
uv tool install "bmad-loop @ git+https://github.com/bmad-code-org/bmad-loop.git" \
  --with "<adapter package or git URL>"
bmad-loop mux            # the new backend's row appears
bmad-loop mux set <name> # persist for this machine
```

The reference external backend is
**[bmad-loop-adapter-herdr](https://github.com/pbean/bmad-loop-adapter-herdr)**.
[herdr](https://herdr.dev) is a cross-platform, agent-aware terminal workspace manager. What
changes on herdr (one manual detach chord, `ctrl+b q`; polled logs; a JSON state sidecar) is
in [that repo's operator guide](https://github.com/pbean/bmad-loop-adapter-herdr/blob/main/docs/adapter-multiplexer-herdr.md).

Operational notes for any external backend:

- **A broken package never breaks bmad-loop.** If a backend fails to import, selection
  proceeds without it; `bmad-loop mux` prints
  `warning: external backend '<name>' failed to load: <reason>` and `validate` reports a
  `mux.external-backend` warning. Reinstalling or upgrading the package usually fixes it.
- **A raising probe reads as unavailable, and says so.** A raising `available()` is skipped
  by selection with a one-time stderr warning. `bmad-loop mux` prints
  `warning: <name> backend probe failed: <error>` (or `version probe failed` for a raising
  `version()`), and `validate` adds a `mux.backend-probe` warning.
- **`mux set --force` persists a backend that is not registered here**, for a shared
  workflow where the package is installed on other machines.
