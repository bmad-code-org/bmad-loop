# Porting bmad-loop to a new OS

bmad-loop runs on Linux and macOS today, and on Windows **via WSL** (which _is_
Linux — every fast path works unchanged). A **native** Windows host, or any other
OS, is not yet shipped. This guide is the map for adding one.

## The promise

The OS-specific work is quarantined behind four seams. Porting to a new OS is
**new files plus one registration line per seam** — no edits to the bodies of the
core `.py` modules or their call sites. Each seam selects its implementation by
platform from a registry, with an env-var override for tests.

| #   | Seam                 | Contract / registry                                            | Override env var                                        |
| --- | -------------------- | -------------------------------------------------------------- | ------------------------------------------------------- |
| 1   | Terminal multiplexer | `TerminalMultiplexer` / `register_multiplexer`                 | `BMAD_LOOP_MUX_BACKEND` (or `bmad-loop mux set <name>`) |
| 2   | Process lifecycle    | `ProcessHost` / `register_process_host`                        | `BMAD_LOOP_PROCESS_HOST`                                |
| 3   | Hook interpreter     | `ProcessHost.hook_interpreter()`                               | (rides on seam 2)                                       |
| 4   | Validate preflight   | `_platform_preflight(project)` (no new code — reads seams 1–2) | —                                                       |

The **one** bundled caveat: a backend you ship _in this repo_ needs its import
added to the relevant `_load_builtin_*` loader so it self-registers (one line). An
out-of-tree backend skips even that — it registers at its own import time.

This guide covers porting **the OS axis** (transport + process lifecycle). The
orthogonal **CLI axis** — teaching bmad-loop a new coding CLI — lives in the
[adapter authoring guide](adapter-authoring-guide.md). The deep transport contract
(every method a multiplexer must implement) also lives there; this guide links to
it rather than duplicating it.

---

## Seam 1 — terminal multiplexer (transport)

**Contract:** `TerminalMultiplexer` (`src/bmad_loop/adapters/multiplexer.py`) — how
sessions, windows, and panes are created, observed, and torn down. Everything that
touches a session goes through `get_multiplexer()`; nothing else shells out to a
multiplexer directly.

**Register** at import time:

```python
from bmad_loop.adapters.multiplexer import register_multiplexer

register_multiplexer("psmux", lambda platform: platform == "win32", PsmuxMultiplexer)
```

`register_multiplexer(name, matches, factory)`:

- `name` — the key the `BMAD_LOOP_MUX_BACKEND` override selects by.
- `matches(sys.platform) -> bool` — decides automatic selection.
- `factory() -> TerminalMultiplexer` — builds the backend.

`get_multiplexer()` resolves by precedence:

1. `BMAD_LOOP_MUX_BACKEND` — forces a backend by name (per-invocation).
2. `[mux] backend` in `.bmad-loop/policy.toml` — the persisted, machine-scoped
   choice (`bmad-loop init` gitignores policy.toml, so it never reaches
   teammates). Set it with `bmad-loop mux set <name>`.
3. The **platform default** — win32: `psmux`, everywhere else: `tmux` — when
   that backend is registered _and_ `available()`.
4. The first registered backend whose `matches(sys.platform)` is true _and_
   whose `available()` reports usable (registration order breaks ties).
5. The historical fallback: first platform match regardless of availability,
   bottoming out at tmux — so a POSIX host without tmux still selects
   `TmuxMultiplexer` and `validate` reports it unavailable.

A forced name (1–2) bypasses both the platform predicate and `available()` — an
explicit choice is trusted — and fails loudly when it matches no registered
backend. Launch preflights and TUI observers share one forced-aware gate
(`mux_usable()`), which warns once on stderr when a forced backend probes
unavailable instead of silently proceeding or refusing. `bmad-loop mux` lists every registered backend with its availability,
version, and the current selection. (The result is cached — see
[Testing a port](#testing-a-port).)

### Shipping out-of-tree: the `bmad_loop.mux_backends` entry point

How does the registration snippet above ever _run_ when the backend lives in its
own package? Advertise the module in the package's `pyproject.toml`:

```toml
[project.entry-points."bmad_loop.mux_backends"]
psmux = "my_package.backend"
```

Before every selection, core scans that entry-point group and imports each
advertised module (builtins first, so tmux keeps first registration and the
precedence above is unchanged); the module's top-level `register_multiplexer(...)`
call does the rest. Installing the package into bmad-loop's environment — e.g.
`uv tool install bmad-loop --with <your-adapter>` — is the entire setup; no core
edit, no config step. The entry-point _value_ is a bare module path (core only
imports it; the name is just a diagnostic label).

A package that fails to import can never break selection: the failure is
recorded and reported by `bmad-loop mux` (a `warning:` line under the table) and
the `validate` preflight, and selection proceeds without it. The reference
out-of-tree adapter is
[bmad-loop-adapter-herdr](https://github.com/pbean/bmad-loop-adapter-herdr).

### Two build paths

- **Extend `BaseTmuxBackend`** (`adapters/tmux_base.py`) for a **tmux-family**
  backend. `BaseTmuxBackend` holds every argv construction and routes every spawn
  through one primitive, `_run(argv, *, check=..., env=...)`. A native-Windows
  "psmux" that speaks a tmux-like CLI sets the `_BINARY` class attribute to the
  binary it drives (every spawn, PATH probe, and in-source client verb follows
  it) and the `_ENCODING` class attribute for output decoding (e.g. `"utf-8"`),
  and passes a per-call `env=` where needed — overriding `_run()` itself only
  to tweak the timeout — plus the
  shell-dialect hooks that `new_window` / `new_parked_window` compose from
  (`_shell_wrap`, `_join_argv`, `_parked_trailer`, `_source_prefix`,
  `_window_launch` and the `_EXIT_CAPTURE`/`_ECHO`/`_PARK` fragments) —
  **without editing** `tmux_base.py` or its POSIX leaf `tmux_backend.py`
  (`TmuxMultiplexer`). The one method-body override left is `pipe_pane`, whose
  POSIX `cat >>` redirection is not behind a hook.
- **Implement `TerminalMultiplexer` fresh** when the host has no tmux-shaped CLI
  at all (e.g. a ConPTY-based window manager). You implement the full contract
  directly; `tmux_backend.py` is the reference for what each method must produce.
  The reference worked example is the external **herdr adapter**
  ([pbean/bmad-loop-adapter-herdr](https://github.com/pbean/bmad-loop-adapter-herdr),
  `src/bmad_loop_adapter_herdr/backend.py`) — a cross-platform, agent-aware
  workspace manager whose object model (workspace/tab/pane) and CLI are a
  different binary family entirely, so it subclasses nothing and maps the whole
  contract onto herdr verbs: a bmad-loop session is a herdr **workspace** (label
  == session name), a window is a **tab** (its `root_pane.pane_id` is the native
  window id), and the launched command runs via a typed `exec <argv>` so
  process-exit stays tmux-identical window death. Where herdr has no analogue
  for a contract method — options, `pipe_pane`, the parked-window return hop,
  detach — it emulates or degrades honestly (a JSON sidecar for options, a
  polling tee for `pipe_pane`, a per-window return file for the parked trailer,
  a no-op detach); that **degradation ledger** is the module docstring, and it
  is the template for what "implement fresh" costs in practice. (The
  operator-facing view of those degradations — what a herdr _user_ notices and
  does — is
  [the adapter's operator guide](https://github.com/pbean/bmad-loop-adapter-herdr/blob/main/docs/adapter-multiplexer-herdr.md).)

`available()` gates whether the backend is usable on the current host (e.g. its
binary is on PATH); the optional `version()` feeds `bmad-loop mux`, the diagnostic
dump, and the validate preflight (seam 4).

**`version()` returns one bounded line.** Every one of those consumers renders it
inline — a table row whose width sets every other row's, a finding message, a
scalar `--json` field — so a binary whose `--version` prints several lines (psmux
prints a `tmux X.Y.Z` compatibility line plus its own) folds them in the backend,
and a very long single line breaks the same surfaces a newline does. Use
`fold_version()` from `adapters/multiplexer.py`: it joins the non-blank lines with
`"; "` **in order**, caps the result at `VERSION_MAX_CHARS`, and returns `None` —
the "no version" sentinel, never `""` — for an all-blank probe. Order is
load-bearing wherever something parses the string: psmux's version gate anchors at
the first segment, and the cap only ever cuts the tail. Core applies the same fold
defensively at each consumer, so breaking the promise cannot split a `mux` row —
but fold at the source, since only the backend knows which line identifies it.

### Availability discriminators (same-platform backends)

Selection consults `available()`, so it must be a **cheap, side-effect-free
probe** — PATH lookups, plus at most one bounded version query when usability
genuinely depends on the installed version — and `factory()` must be a plain
constructor: `detect_multiplexers()` instantiates every registered backend just
to list it. When two backends claim the same platform, their `available()`
probes should be **pairwise discriminating** — otherwise both report usable and
only the platform default / registration order separates them in listings and
selection. The bundled psmux backend discriminates by construction: it drives
psmux's distinctly-named binary (`_BINARY = "psmux"`), so it never claims some
other tmux-family install that owns the `tmux` name. Its probe also
version-gates — psmux 3.3.8 or newer is required
([why](multiplexer-backends.md#psmux-native-windows-experimental)), so an old or unidentifiable
version reads as unavailable (`psmux -V` keeps the `tmux X.Y.Z` output format
deliberately):

```python
class PsmuxMultiplexer(BaseTmuxBackend):
    _BINARY = "psmux"

    def available(self) -> bool:
        if not all(shutil.which(exe) for exe in ("psmux", "pwsh")):
            return False
        reported = re.match(r"tmux (\d+)\.(\d+)(?:\.(\d+))?", self.version() or "")
        return bool(reported) and tuple(int(part or 0) for part in reported.groups()) > (3, 3, 7)

# a sibling that owns the `tmux` name (e.g. a tmux-windows port) discriminates
# against psmux explicitly:
class WindowsTmuxMultiplexer(BaseTmuxBackend):
    def available(self) -> bool:
        return shutil.which("tmux") is not None and shutil.which("psmux") is None
```

Do **not** inherit `BaseTmuxBackend.available()` (a bare `which` on `_BINARY`)
for a same-platform sibling that shares a binary name: selection would still
break the tie via the platform default, but `bmad-loop mux` and the validate
preflight would list both as available when only one actually drives the
installed binary. A host with an ambiguous install resolves it explicitly:
`bmad-loop mux set <name>`.

A backend from a **different binary family** sidesteps this problem entirely.
The external herdr adapter probes `shutil.which("herdr")` — a distinct binary
that no tmux-family backend claims — so it is **pairwise-discriminating by
construction**: it can never report available on a host where only tmux is
installed, and vice versa, without any explicit tie-break. That `available()`
must stay a pure PATH lookup (it is called by `detect_multiplexers()` on every
listing) — the herdr adapter in particular **never** probes or starts its
background server from `available()`, `version()`, or the constructor; server
autostart is lazy, confined to the mutating operations that actually need it.

**Deep contract →** [adapter authoring guide: the transport contract for a backend
author](adapter-authoring-guide.md#the-transport-contract-for-a-backend-author).

---

## Seam 2 — process lifecycle (`ProcessHost`)

**Contract:** `ProcessHost` (`src/bmad_loop/process_host.py`) — the four pid
operations the orchestrator needs (`runs.stop_run`, the TUI liveness column), plus
the hook interpreter (seam 3). On POSIX these are `os.kill` calls; on Windows
`taskkill` / psutil. `WindowsProcessHost` already ships (unexercised until a
Windows backend lands).

**Register** like the multiplexer:

```python
from bmad_loop.process_host import register_process_host

register_process_host("windows", lambda platform: platform == "win32", WindowsProcessHost)
```

`get_process_host()` selects by the same rule as `get_multiplexer()`;
`BMAD_LOOP_PROCESS_HOST` forces one by name; POSIX is the default fallback.

**Implement:**

- `terminate(pid)` — politely stop it (POSIX `SIGTERM` / Windows `taskkill`). Raise
  the `OSError` family (`ProcessLookupError` / `PermissionError`) so callers keep
  their "already gone / not ours" handling. This is the polite fast path, not the
  stop guarantee: `bmad-loop stop` also lodges a hard `stop-request.json` the engine
  reads itself, so a port whose `terminate` cannot actually be delivered still stops
  runs (#319).
- `force_kill(pid)` — escalation when `terminate` is ignored (POSIX `SIGKILL` /
  Windows `taskkill /F /T`). Only ever called once identity is confirmed.
- `is_alive(pid)` — read-only liveness probe, no signal sent.
- `identity(pid) -> float | None` — the **PID-reuse guard**: a value that stays
  constant for the life of `pid` but changes if the pid is reused (Linux reads
  `/proc/<pid>/stat` start-time; elsewhere psutil's `create_time()`). Return
  `None` where the platform can't provide one — callers then **refuse to
  force-kill** rather than risk an unrelated process that inherited the pid.
- `hook_interpreter()` — seam 3, below.

---

## Seam 3 — hook interpreter

`ProcessHost.hook_interpreter()` is the command prefix that `install` / `probe`
interpolate into the hook registrations they write (the script path and canonical
event are appended by the caller). It exists so hook registration never branches
on `sys.platform` at the call site:

- POSIX returns `"python3"` (the interpreter on PATH).
- `WindowsProcessHost` returns `"uv run --no-project python"` — Windows ships no
  `python3` launcher, and `--no-project` resolves an interpreter without activating
  a project venv (hooks fire detached).

A new OS overrides this on its `ProcessHost`; nothing else changes.

---

## Seam 4 — validate preflight

`_platform_preflight(project)` (`src/bmad_loop/cli.py`, called from `cmd_validate`)
asks the selected multiplexer for its `available()` / `version()` and names the
selected process host. A new OS therefore surfaces its readiness in `bmad-loop
validate` **by registering** (seams 1–2) — not by adding a `win32` block to
`validate`. The process host is named in the output so a misselection (e.g. the
Windows host picked on Linux) is visible at a glance.

There is no new code to write for this seam — it reads seams 1 and 2.

The one `sys.platform` branch that does live here is not a port seam and is not a
precedent for one: the `host.win32-on-wsl-path` check (#332) reports that the _interpreter
itself_ is the wrong build for the shell that launched it — a native-Windows
`bmad-loop` reached from a WSL prompt. No registration can express that, because
every seam is correctly selected for the interpreter that is running; what is wrong
is which interpreter the operator got. Readiness questions still register.

---

## Helper scripts (plugins)

Plugin **helper scripts** (e.g. the bundled Unity plugin's `unity_setup.py` /
`unity_teardown.py`) are spawned under the orchestrator's own interpreter via
`sys.executable`, **not** a PATH-resolved `python3`. The practical consequence: a
bundled helper script may `import bmad_loop` — so for pid lifecycle it should
**use the seam** rather than re-implement kill/liveness behind its own
`sys.platform` guards:

```python
from bmad_loop.process_host import get_process_host

host = get_process_host()
host.terminate(pid)
if host.is_alive(pid):
    host.force_kill(pid)
```

**Worked example:** `data/plugins/unity/unity_teardown.py` now _delegates_ its
SIGTERM→SIGKILL sweep of leaked Editor / MCP-server processes to
`get_process_host()` instead of calling `os.kill` / `signal.SIGKILL` itself — so it
gains Windows behavior for free when a Windows host registers. (It still does its
own worktree-bound process _discovery_ via `/proc` with a psutil fallback, because
discovery has no seam — see the next section.)

> Out-of-tree plugin scripts distributed outside this repo can't assume `bmad_loop`
> is importable in every install; the import path is reliable for **bundled**
> scripts spawned under `sys.executable`.

---

## The portability guard

`tests/test_portability_guard.py` AST-scans `src/bmad_loop` and fails CI if a new
hard POSIX dependency creeps in outside an allowlist — a `["tmux", …]` argv outside
the tmux backend, a bare `os.kill(pid, 0)` outside the liveness helpers, an
unguarded `signal.SIGKILL`, a hardcoded `/tmp` / `/proc` / `/dev/null`,
`start_new_session=True`, or `shell=True`. When you add a seam, route the OS call
**through** it rather than widening an allowlist; the few sanctioned exceptions
(the quarantine files, the platform-guarded discovery helpers) carry a
`# portability:` ack on the line.

Things **without** a seam still need a hand-guarded fallback behind a
`sys.platform` branch with that ack: `cp --reflink` / CoW copies, symlinks,
`/proc` scanning, `/tmp`, and `start_new_session`. Keep the Linux fast path
byte-identical; the new-OS branch can be best-effort until exercised.

---

## Testing a port

Both `get_multiplexer()` and `get_process_host()` are `lru_cache`d, and selection
keys off `sys.platform`. To exercise a not-yet-default backend on your dev box:

| To force…      | Set                              | Then clear the cache             |
| -------------- | -------------------------------- | -------------------------------- |
| a multiplexer  | `BMAD_LOOP_MUX_BACKEND=psmux`    | `get_multiplexer.cache_clear()`  |
| a process host | `BMAD_LOOP_PROCESS_HOST=windows` | `get_process_host.cache_clear()` |

The env var picks the registered backend by `name`; `cache_clear()` is required
because the first call memoizes the selection for the process. To make a
multiplexer choice stick across invocations instead, persist it with
`bmad-loop mux set <name>` (writes `[mux] backend` into the machine-local
policy.toml; the env var still outranks it, and `configure_multiplexer` /
`register_multiplexer` clear the cache themselves).

---

## What a native-Windows port costs, end to end

Concretely, a native-Windows port is:

1. `PsmuxMultiplexer(BaseTmuxBackend)` (or a fresh `TerminalMultiplexer`) +
   its `register_multiplexer("psmux", …)`.
2. `WindowsProcessHost` — **already shipped** — needs only its registration, which
   is **already present** in `_load_builtin_hosts`. Its `hook_interpreter()`
   (`uv run --no-project python`) is in place too.
3. A CI runner on Windows to exercise the above.

No edits to the adapters, `runs.py`, `tui/launch.py`, `probe.py`, `tui/data.py`,
`cli.py`'s `validate`, or the POSIX seam bodies. The remaining design questions
(what hosts the windows, how attach/detach and the parked exit-status window map
without a POSIX shell, the Windows-Unity cache-path follow-up) are tracked in
[the roadmap](ROADMAP.md#native-windows-multiplexer-backend).
