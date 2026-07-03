# Porting bmad-loop to a new OS

bmad-loop runs on Linux and macOS today, and on Windows **via WSL** (which _is_
Linux — every fast path works unchanged). A **native** Windows host, or any other
OS, is not yet shipped. This guide is the map for adding one.

## The promise

The OS-specific work is quarantined behind four seams. Porting to a new OS is
**new files plus one registration line per seam** — no edits to the bodies of the
core `.py` modules or their call sites. Each seam selects its implementation by
platform from a registry, with an env-var override for tests.

| #   | Seam                 | Contract / registry                                     | Override env var         |
| --- | -------------------- | ------------------------------------------------------- | ------------------------ |
| 1   | Terminal multiplexer | `TerminalMultiplexer` / `register_multiplexer`          | `BMAD_LOOP_MUX_BACKEND`  |
| 2   | Process lifecycle    | `ProcessHost` / `register_process_host`                 | `BMAD_LOOP_PROCESS_HOST` |
| 3   | Hook interpreter     | `ProcessHost.hook_interpreter()`                        | (rides on seam 2)        |
| 4   | Validate preflight   | `_platform_preflight()` (no new code — reads seams 1–2) | —                        |

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

`get_multiplexer()` returns the first backend whose `matches(sys.platform)` is
true, unless `BMAD_LOOP_MUX_BACKEND` forces one by name; tmux is the default
fallback, so POSIX behavior is unchanged. (The result is cached — see
[Testing a port](#testing-a-port).)

### Two build paths

- **Extend `BaseTmuxBackend`** (`adapters/tmux_base.py`) for a **tmux-family**
  backend. `BaseTmuxBackend` holds every argv construction and routes every spawn
  through one primitive, `_run(argv, *, check=..., env=...)`. A native-Windows
  "psmux" that speaks a tmux-like CLI sets the `_ENCODING` class attribute for
  output decoding (e.g. `"utf-8"`) and passes a per-call `env=` where needed —
  overriding `_run()` itself only to tweak the binary or timeout — plus the few
  genuinely divergent methods (e.g. the
  parked-window `sh -c` trailer in `new_parked_window`) — **without editing**
  `tmux_base.py` or its POSIX leaf `tmux_backend.py` (`TmuxMultiplexer`).
- **Implement `TerminalMultiplexer` fresh** when the host has no tmux-shaped CLI
  at all (e.g. a ConPTY-based window manager). You implement the full contract
  directly; `tmux_backend.py` is the reference for what each method must produce.

`available()` gates whether the backend is usable on the current host (e.g. its
binary is on PATH); the optional `version()` feeds the diagnostic dump and the
validate preflight (seam 4).

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
  their "already gone / not ours" handling.
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

`_platform_preflight()` (`src/bmad_loop/cli.py`, called from `cmd_validate`) asks
the selected multiplexer for its `available()` / `version()` and names the selected
process host. A new OS therefore surfaces its readiness in `bmad-loop validate`
**by registering** (seams 1–2) — not by adding a `win32` block to `validate`. The
process host is named in the output so a misselection (e.g. the Windows host picked
on Linux) is visible at a glance.

There is no new code to write for this seam — it reads seams 1 and 2.

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
because the first call memoizes the selection for the process.

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
