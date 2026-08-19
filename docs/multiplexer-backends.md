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
reuses tmux's session/window model — the `bmad-loop-<run-id>` and `bmad-loop-ctl` session
names carry over. It is selected automatically when available; `available()` requires the
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
