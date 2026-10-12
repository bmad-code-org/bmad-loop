# Porting bmad-loop to a new OS

bmad-loop runs on Linux, macOS, WSL (which _is_ Linux), and native Windows through the
bundled, experimental psmux backend. This guide maps the seams a new OS or transport plugs
into. Operators choosing or running a backend read
[Terminal multiplexer backends](multiplexer-backends.md); the orthogonal **CLI axis**
(teaching bmad-loop a new coding CLI) is in the
[adapter authoring guide](adapter-authoring-guide.md).

## The seams

The OS-specific work sits behind four seams. Each selects its implementation by platform
from a registry, with an env-var override for tests.

| #   | Seam                 | Contract / registry                                      | Override env var                                        |
| --- | -------------------- | -------------------------------------------------------- | ------------------------------------------------------- |
| 1   | Terminal multiplexer | `TerminalMultiplexer` / `register_multiplexer`           | `BMAD_LOOP_MUX_BACKEND` (or `bmad-loop mux set <name>`) |
| 2   | Process lifecycle    | `ProcessHost` / `register_process_host`                  | `BMAD_LOOP_PROCESS_HOST`                                |
| 3   | Hook interpreter     | `ProcessHost.hook_interpreter()`                         | (rides on seam 2)                                       |
| 4   | Validate preflight   | `runsetup.platform_preflight(project)` (reads seams 1–2) | —                                                       |

A new backend or host is new files plus a registration. A bundled one adds its import and
its `register_*` call to the matching `_load_builtin_*` loader; an out-of-tree one calls
`register_*` at import time. Transport features beyond the seam contract, such as a
[registry namespace](#registry-namespaces-only-if-your-transport-has-one), also need core
support in `runs` and `cli`.

---

## Seam 1 — terminal multiplexer (transport)

**Contract:** `TerminalMultiplexer` (`src/bmad_loop/adapters/multiplexer.py`) creates,
observes and tears down sessions, windows and panes. Everything that touches a session goes
through `get_multiplexer()`. The per-method obligations are in
[the transport contract](adapter-authoring-guide.md#the-transport-contract-for-a-backend-author).

**Register:**

```python
from bmad_loop.adapters.multiplexer import register_multiplexer

register_multiplexer("mymux", lambda platform: platform == "win32", MyMultiplexer)
```

`register_multiplexer(name, matches, factory)`:

- `name`: the key `BMAD_LOOP_MUX_BACKEND` and `[mux] backend` select by. The first
  registration under a name wins, and the builtins register first.
- `matches(sys.platform) -> bool`: decides automatic selection.
- `factory() -> TerminalMultiplexer`: builds the backend. Keep it a plain constructor;
  `bmad-loop mux` instantiates every registered backend to list it.

Selection precedence is in
[Which backend is running](multiplexer-backends.md#which-backend-is-running-and-how-to-switch).
A forced name bypasses both `matches` and `available()`, and fails loudly when it matches
no registered backend. Launch preflights and TUI observers share one forced-aware gate,
`mux_usable()`, which warns once on stderr when a forced backend probes unavailable.

### Shipping out-of-tree: the `bmad_loop.mux_backends` entry point

Advertise the module that calls `register_multiplexer(...)` in the package's
`pyproject.toml`:

```toml
[project.entry-points."bmad_loop.mux_backends"]
mymux = "my_package.backend"
```

On first selection or detection, core imports each advertised module once per process,
after the builtins. Installing the package into bmad-loop's environment (for example
`uv tool install bmad-loop --with <your-adapter>`) is the whole setup. The entry-point
value is a bare module path; the name is a diagnostic label. A package that fails to import
never breaks selection: the failure is recorded and
[reported](multiplexer-backends.md#external-backends). The reference out-of-tree backend is
[bmad-loop-adapter-herdr](https://github.com/pbean/bmad-loop-adapter-herdr).

### Two build paths

- **Extend `BaseTmuxBackend`** (`adapters/tmux_base.py`) for a **tmux-family** backend.
  The base holds every argv construction and routes every spawn through `_run(argv, *,
check=..., env=...)`. A leaf sets class attributes rather than overriding methods:
  `_BINARY` (the binary every spawn, `PATH` probe and client verb uses), `_CLIENT_FLAGS`,
  `_ENCODING`, `_ERRORS`, and the session/window-gone stderr markers. It overrides `_run`
  only to wrap the spawn (psmux binds its registry root there). A non-POSIX leaf also
  overrides the shell-dialect hooks that `new_window` and `new_parked_window` compose from
  (`_shell_wrap`, `_join_argv`, `_parked_trailer`, `_source_prefix`, `_window_launch`, and
  the `_EXIT_CAPTURE` / `_ECHO` / `_PARK` fragments), plus `pipe_pane`, whose POSIX
  `cat >>` sink has no hook. Never edit `tmux_base.py` or its POSIX leaf `tmux_backend.py`
  for a new leaf.

  The base `_window_launch` is dialect-neutral (`-e` flags plus the raw command). The POSIX
  `TmuxMultiplexer` leaf adds a `/bin/sh -c` prelude that exports its pid as
  `BMAD_LOOP_LAUNCH_PID` for the relays' hook lineage, then execs `$SHELL -c <command>`. A
  leaf that does not set `BMAD_LOOP_LAUNCH_PID` (psmux does not) gets `unknown` lineage,
  which attribution ignores.

- **Implement `TerminalMultiplexer` fresh** when the host has no tmux-shaped CLI. You
  implement the full contract; `tmux_base.py` shows what each method must produce. The
  worked example is the herdr adapter
  ([pbean/bmad-loop-adapter-herdr](https://github.com/pbean/bmad-loop-adapter-herdr),
  `src/bmad_loop_adapter_herdr/backend.py`): a bmad-loop session is a herdr **workspace**,
  a window is a **tab** (its root pane id is the native window id), and the command runs via
  `exec <argv>` so process exit closes the tab. Where herdr has no analogue (options,
  `pipe_pane`, the parked-window return hop, detach) it emulates or degrades, and its
  module docstring lists each case.

`available()` gates whether the backend is usable on this host; `version()` feeds
`bmad-loop mux`, the diagnostic dump and the validate preflight.

**`version()` returns one bounded line.** Each consumer renders it inline (a table row, a
finding message, a scalar `--json` field). Fold multi-line output with `fold_version()` from
`adapters/multiplexer.py`: it joins non-blank lines with `"; "` in order, caps the result at
`VERSION_MAX_CHARS`, and returns `None` (never `""`) for a blank probe. Put the identifying
version in the first segment; a version gate parses from the front, and the cap cuts the
tail.

### Availability discriminators (same-platform backends)

Selection calls `available()`, so it must be a **cheap, side-effect-free probe**: `PATH`
lookups plus at most one bounded version query when usability depends on the installed
version. psmux is the example: it requires `psmux` and `pwsh` on `PATH` and a minimum psmux
version ([requirements](multiplexer-backends.md#psmux-native-windows-experimental)), and
caches the verdict per instance.

When two backends claim the same platform, their `available()` probes should be **pairwise
discriminating**; otherwise both report usable and only the platform default or
registration order separates them. psmux discriminates by construction: it drives its own
`psmux` binary, so it never claims another install that owns the `tmux` name. A
hypothetical sibling that owns `tmux` on Windows would discriminate explicitly:

```python
class WindowsTmuxMultiplexer(BaseTmuxBackend):
    def available(self) -> bool:
        return shutil.which("tmux") is not None and shutil.which("psmux") is None
```

Do not inherit `BaseTmuxBackend.available()` (a bare `which` on `_BINARY`) for a
same-platform sibling that shares a binary name: `bmad-loop mux` and the preflight would
list both as available. An operator with an ambiguous install resolves it with
`bmad-loop mux set <name>`.

A backend from a different binary family, such as herdr, discriminates by construction. Its
`available()` must stay a pure `PATH` lookup; never start a server from `available()`,
`version()` or the constructor.

### Registry namespaces (only if your transport has one)

Some multiplexers address sessions through a **registry**: a directory of per-session
addressing files every verb resolves a target through. psmux is one (`PSMUX_DATA_DIR`, one
`.port`/`.key`/`.sid`/`.pid` set per session). tmux is not: a server is a socket.

A registry is a **namespace, not a filter**. A session in registry A is unaddressable from
registry B, and the transport usually reports that as an ordinary "no such session". Two
processes that disagree about the root disagree about which sessions exist. If your
transport namespaces, four rules:

- **Derive the root from the project**, never from the run, the launching shell, or anything
  a driven session can write (`policy.toml` and the project tree are both session-writable).
  bmad-loop's derivation is `runs.mux_registry_root`, keyed on `runs.project_tag`; it belongs
  in `runs`, beside the state root, not in the backend.
- **Bind it once, ahead of every spawn.** `cli._configure_mux` exports it before dispatch.
  Derive by default and override an ambient value, reporting that you did; honouring an
  ambient value is an explicit operator opt-in
  ([`honor_ambient_psmux_data_dir`](multiplexer-backends.md#using-your-own-psmux_data_dir)).
- **Carry `new_window`'s env inside the command you launch**, not in the environment the
  pane inherits. A multiplexer may clear a pane's inherited environment (psmux's
  `PSMUX_BARE_ENV`). `_window_launch` is the dialect hook that owns this.
- **Answer `has_registry_namespace()`, `registry_root()` and `legacy_registries()`.** The
  first tells cleanup the transport namespaces at all, so a registry with no root in force
  reads as the shared default rather than as proof of ownership. The second lets
  `bmad-loop mux` disclose the root. The third hands cleanup instances bound to older roots;
  each must be an independent instance, never a global environment swap, because the sweep
  runs on a TUI worker thread beside other verbs.

All three default to "no namespace", so a transport without one writes no code.

---

## Seam 2 — process lifecycle (`ProcessHost`)

**Contract:** `ProcessHost` (`src/bmad_loop/process_host.py`): the pid operations behind
`runs.stop_run` and the TUI liveness column, plus the hook interpreter (seam 3).
`PosixProcessHost` uses `os.kill`; `WindowsProcessHost` uses `taskkill` and psutil. Both are
registered in `_load_builtin_hosts`.

**Register** like the multiplexer:

```python
from bmad_loop.process_host import register_process_host

register_process_host("myhost", lambda platform: platform == "myos", MyProcessHost)
```

`get_process_host()` picks the host named by `BMAD_LOOP_PROCESS_HOST`, otherwise the first
registered host whose `matches(sys.platform)` is true, otherwise POSIX. A forced name that
matches nothing raises.

**Implement:**

- `terminate(pid)`: polite stop (POSIX `SIGTERM`, Windows `taskkill`). The contract raises
  the `OSError` family (`ProcessLookupError` / `PermissionError`) so callers keep their
  "already gone / not ours" handling. `bmad-loop stop` also writes a `stop-request.json` the
  engine reads, so an undeliverable `terminate` still stops runs.
- `force_kill(pid)`: escalation when `terminate` is ignored (POSIX `SIGKILL`, Windows
  `taskkill /F /T`). Called only once identity is confirmed.
- `is_alive(pid)`: read-only liveness probe, no signal sent.
- `identity(pid) -> float | None`: the **PID-reuse guard**, a value constant for the life of
  `pid` that changes if the pid is reused (Linux reads `/proc/<pid>/stat` start time;
  elsewhere psutil's `create_time()`). Return `None` where the platform cannot provide one;
  callers then refuse to force-kill.
- `hook_interpreter()`: seam 3, below.

Defaults you may override: `descendants(pid)` (pid → identity snapshot of the process tree,
never raises; psutil by default), `shell_quote(arg)` (default `shlex.quote`), and
`unsafe_shell_chars(path)` (the shell metacharacters `shell_quote()` leaves exposed;
`init` and `validate` warn on them). Override `unsafe_shell_chars` alongside `shell_quote`
when quoting is conditional (Windows' `list2cmdline` quotes only on whitespace).

---

## Seam 3 — hook interpreter

`ProcessHost.hook_interpreter()` supplies an absolute, host-quoted `sys.executable` path for
temporary Python hook scripts, such as the live probe's capture hook. `init` and worktree
provisioning register the installed `bmad-loop relay <Event>` console script by absolute
path instead of invoking a Python script in the workspace. A new OS overrides the quoting
on its `ProcessHost` as needed.

---

## Seam 4 — validate preflight

`runsetup.platform_preflight(project)`, called from `cmd_validate`, asks the selected
multiplexer for `available()` / `version()`, reports the selection reason and any external
load or probe faults, and names the selected process host. A new OS surfaces its readiness
in `bmad-loop validate` **by registering** (seams 1–2), not by adding a platform block to
`validate`. There is no new code to write for this seam.

The one `sys.platform` branch here, `host.win32-on-wsl-path`, is not a port seam: it warns
when a native-Windows interpreter is working on a WSL distro path, which no registration
can express.

---

## Helper scripts (plugins)

Plugin helper scripts (for example the bundled Unity plugin's `unity_setup.py` /
`unity_teardown.py`) run under the orchestrator's own interpreter via `sys.executable`, not a
`PATH`-resolved `python3`. A bundled helper may therefore `import bmad_loop`, and for pid
lifecycle it should use the seam rather than its own `sys.platform` guards:

```python
from bmad_loop.process_host import get_process_host

host = get_process_host()
ident = host.identity(pid)
host.terminate(pid)
if host.is_alive(pid) and ident is not None and host.identity(pid) == ident:
    host.force_kill(pid)
```

`data/plugins/unity/unity_teardown.py` routes its terminate/force-kill sweep through
`get_process_host()`. Process discovery has no seam, so it scans `/proc` on Linux and falls
back to psutil elsewhere.

> Out-of-tree plugin scripts can't assume `bmad_loop` is importable in every install; the
> import is reliable only for bundled scripts spawned under `sys.executable`.

---

## The portability guard

`tests/test_portability_guard.py` AST-scans `src/bmad_loop` and fails CI when a hard POSIX
dependency appears outside an allowlist: a `["tmux", …]` argv outside the tmux backend, a
bare `os.kill(pid, 0)` outside the liveness helpers, any `os.kill(` outside
`process_host.py`, an unguarded `signal.SIGKILL`, a hardcoded `/tmp` / `/proc` /
`/dev/null`, `start_new_session=True`, or `shell=True`. When you add a seam, route the OS
call through it rather than widening an allowlist. Allowlisted lines carry a
`# portability:` ack; the tmux quarantine files (`tmux_base.py`, `tmux_backend.py`) are
exempt from the tmux-argv rule only; every other rule still applies to them.

Things **without** a seam need a hand-guarded fallback behind a `sys.platform` branch with
that ack: `cp --reflink` / CoW copies, symlinks, `/proc` scanning, `/tmp`, and
`start_new_session`. Keep the Linux fast path byte-identical; the new-OS branch may be
best-effort. Hook-event lineage is one such `/proc` reader: both relays (`events.py`,
`data/bmad_loop_hook.py`) walk the parent chain through `/proc`, so on an OS without it
every event is tagged `unknown` and attribution ignores lineage.

---

## Path predicates — the guard family a port must not relax

`src/bmad_loop/platform_util.py` carries four predicates that every "this config value must
be a path inside the project" guard is built from. They are **platform-independent by
construction**: a config file must not mean different things depending on where the run
happens.

- `is_absolute_path(value)`: rooted or drive-qualified in _either_ flavour: `/etc/passwd`,
  `C:\x`, `\\server\share`, and the drive-relative `C:foo`.
- `has_parent_ref(value)`: a `..` segment in either flavour.
- `names_tree_root(value)`: names the tree itself: `""`, `"."`, and the
  all-periods-and-spaces spellings Win32 trims to nothing (`". "`, `"..."`, `"   "`).
- `names_win32_alias(value)`: a value that stays inside the tree but names a **different**
  path on Windows: a reserved device basename (`NUL`, `aux.json`, `CON .txt`), a component
  whose trailing periods and spaces Win32 strips (`.claude/skills.`), or a
  periods-and-spaces component beside a real one (`sub/...`).

Rules for a porter:

- **Do not make any of them consult `sys.platform`.** A value refused here is refused
  everywhere: `skill_tree = "NUL"` is rejected on Linux for the same reason `C:\secrets` is.
- **They refuse disjoint spelling classes.** Each predicate carves out the others' cases, so
  each can be ablated on its own. Do not merge them.
- **`_is_reserved_basename` is a _segment_ predicate**, blind to `sub/NUL`. Apply it per
  component, after splitting on both separators.
- **`safe_segment` is not one of them.** It maps `/` to `_` to sanitize a single name (a run
  id, a sweep bundle name); never apply it to a multi-segment config path.
- **Their Win32 half is cited, not measured.** The docstrings carry their sources so a
  disagreement measured on a real Windows host can be settled.
- `src/bmad_loop/data/plugins/unity/unity_seed_assets.py` re-implements all four by hand
  (it is deployed into a consumer project, stdlib-only, and excluded from pyright). Its only
  drift guard is the parity suite in `tests/test_unity_scene_guard.py`; mirror any change
  there.

---

## Testing a port

`get_multiplexer()` and `get_process_host()` are `lru_cache`d, and selection keys off
`sys.platform`. To force a registered backend or host on a dev box:

| To force…      | Set                             | Then clear the cache             |
| -------------- | ------------------------------- | -------------------------------- |
| a multiplexer  | `BMAD_LOOP_MUX_BACKEND=<name>`  | `get_multiplexer.cache_clear()`  |
| a process host | `BMAD_LOOP_PROCESS_HOST=<name>` | `get_process_host.cache_clear()` |

`cache_clear()` is required because the first call memoizes the selection for the process.
`register_multiplexer`, `register_process_host` and `configure_multiplexer` clear the cache
themselves. To make a multiplexer choice persist, use
[`bmad-loop mux set <name>`](multiplexer-backends.md#which-backend-is-running-and-how-to-switch).

---

## Reference port: native Windows

The native-Windows port is `PsmuxMultiplexer` (`adapters/psmux_backend.py`) on
`BaseTmuxBackend`, plus `WindowsProcessHost` (`process_host.py`), both registered in their
`_load_builtin_*` loaders. Its registry namespace adds `runs`/`cli` support for the
per-project registry root. The Windows CI legs and the psmux live gate
(`tests/test_psmux_live.py`) exercise it. Remaining work is on
[the roadmap](ROADMAP.md#native-windows-multiplexer-backend).
