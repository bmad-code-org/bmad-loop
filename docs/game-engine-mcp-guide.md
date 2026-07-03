# Writing a Game Engine plugin for a specific Editor MCP

This is the companion to the [Game Engine plugin guide](game-engine-plugin-guide.md).
That guide covers the plugin shape (TOML schema, lifecycle hooks, the
`editor_mode` ↔ `[scm] isolation` coupling). This one covers the **Editor MCP**
specifics: how a plugin's scripts talk to a particular MCP server, how to write a
sound readiness probe, how `per_worktree` isolation works, and the **full env-var
reference** for tuning the bundled Unity plugin without forking it.

## The `mcp` policy key

`[plugins.unity] mcp` is passed through to the plugin's scripts as
`BMAD_LOOP_ENGINE_MCP`. The bundled Unity scripts **branch** on it to support two
different server implementations from one plugin:

```toml
[plugins]
enabled = ["unity"]

[plugins.unity]
mcp = "ivanmurzak"        # ivanmurzak | coplaydev
```

A plugin you write is free to ignore this key (if it targets one server) or to use
the same branch-on-`BMAD_LOOP_ENGINE_MCP` pattern to support several.

## IvanMurzak vs CoplayDev (the two wired Unity MCPs)

| Aspect                 | `ivanmurzak` ([IvanMurzak/Unity-MCP](https://github.com/IvanMurzak/Unity-MCP)) | `coplaydev` ([CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp)) |
| ---------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| `shared` mode          | ✅ supported                                                                   | ✅ supported                                                                |
| `per_worktree` mode    | ✅ fully wired (managed Editor per worktree)                                   | ⚠️ not wired — bring your own setup/teardown                                |
| Server model           | one server **per project path**, port auto-derived from the path               | one shared server (`:8080`) multiplexing Editors by instance id             |
| Readiness probe        | CLI `wait-for-ready` (Editor hosts its own server)                             | connectivity check against the HTTP server                                  |
| Per-worktree isolation | automatic (distinct path → distinct port)                                      | must be solved by your own scripts                                          |

**`shared` mode works with either** — the readiness gate just confirms the
operator's already-open Editor + MCP are up. **`per_worktree` is IvanMurzak-only in
the bundled plugin**, because its per-path port derivation gives each worktree's
Editor its own server with no manual wiring. For CoplayDev's single-shared-server
model, point `worktree_setup_cmd` / `worktree_teardown_cmd` at your own scripts
(override the plugin under `.bmad-loop/plugins/unity/`), or use `shared` mode.

## Writing an MCP-agnostic readiness probe

The contract is simple: **`ready_cmd` exits `0` when a session can safely start,
non-zero otherwise** (which defers the unit). Within that, a few things matter:

- **Respect the budget.** Poll until `BMAD_LOOP_ENGINE_READY_TIMEOUT` seconds elapse,
  then fail. The CLI's own default timeout is often far shorter (IvanMurzak's
  `wait-for-ready` defaults to 120s), so pass an explicit `--timeout`.
- **Honor the grace.** Sleep `BMAD_LOOP_ENGINE_READY_GRACE` seconds before the first
  probe. A cold `per_worktree` Editor isn't listening yet, and a fast
  connection-refused would otherwise abort the gate early. `-1` means _auto_ — the
  Unity gate picks **120s for `per_worktree`** (cold launch) and **0s for `shared`**
  (warm, already-open Editor); the grace counts against the overall timeout.
- **Probe something real.** A bare TCP connect proves a port is open, not that the
  Editor↔server bridge can answer. The Unity gate uses `wait-for-ready` (sound for
  IvanMurzak because the Editor hosts its own server, so readiness is observable
  _before_ any client connects) and optionally a read-only `run-tool` round-trip
  (`BMAD_LOOP_UNITY_READY_TOOL`) for a stricter check — off by default because tool
  names are version-specific.

> CLI subcommand names and MCP endpoints move between releases. Keep the
> version-specific bits in the plugin (and document the version you verified
> against), so an operator can override `ready_cmd` under `.bmad-loop/plugins/<name>/`
> when their installed version differs.

## `per_worktree` isolation (IvanMurzak)

For `per_worktree`, the Unity setup hook (`unity_setup.py`) makes each fresh
worktree a usable, self-isolated Unity project:

1. **Prime the `Library`.** A fresh worktree has no `Library` (it's gitignored, so
   never checked out), and opening Unity on an empty `Library` forces a _cold full
   reimport_ that, on a real project, crashes the import workers (Burst `SIGFPE`
   writing `VirtualArtifacts`). Setup reflink/CoW-copies the warm main `Library` in,
   making it an _incremental_ import — near-free on btrfs/xfs. It falls back to a
   deep copy, then to a symlinked empty cache, when CoW or a warm source is absent.
2. **Write `.mcp.json` + pin Custom mode.** `setup-mcp` writes the worktree's MCP
   client config; `bootstrap-local` pins the project to local ("Custom") connection
   mode. The IvanMurzak CLI **derives the MCP port from the project path**, so a
   worktree at a different path automatically gets its own port and self-isolates
   from the operator's main Editor — no manual port wiring.
3. **Launch an Editor that hosts its own server** (`open --start-server true`). Because
   the Editor (not the client) owns the server, the bridge — and thus
   `wait-for-ready` — comes up before any agent connects, so the readiness gate can
   observe it.

Teardown (`unity_teardown.py`) quits the Editor (`close`, then `--force`), hard-kills
any leaked Editor **or its child `gamedev-mcp-server`** whose argv references the
worktree (a leaked server holds its port and poisons later runs), and drops a
symlinked `Library` if setup used the fallback (a real primed `Library` is left for
the worktree's own deletion).

## Skill-tree seeding

An MCP server typically generates a **skill/tool tree** the coding CLI reads to call
Editor tools — and that tree is usually **gitignored**, so a fresh `git worktree`
checkout (tracked files only) doesn't have it. The plugin closes that gap with seeds:

- `seed_globs` — patterns expanded **relative to the main repo** and copied into each
  worktree. The Unity plugin uses `seed_globs = [".claude/skills/*"]` to copy the
  MCP-generated skill tree in so the agent's CLI can reach the Editor's tools.
- `seed_files` — literal **project-relative** files copied in (use for a single known
  config rather than a tree).

These compose with the `[scm]` worktree seeds (`seed_adapter_defaults`,
`worktree_seed`) that already copy adapter MCP/CLI configs like `.mcp.json` and
`.claude/settings.json`. (Sources: `src/bmad_loop/install.py`, `engine.py`.)

## Full env-var reference (Unity plugin)

The five `[plugins.unity]` keys are the operator-facing settings (editable in the TUI
under the Unity plugin's section). Everything below is a **script-level knob** with a
built-in default. The plugin builds the helper scripts' environment from `os.environ`
(then overlays the identity + settings vars below), so **override a knob by exporting it
in the environment that launches `bmad-loop`** — e.g. in your shell profile or run
wrapper:

```sh
export UNITY_MCP_CLI="unity-mcp-cli"
export BMAD_LOOP_UNITY_LIBRARY_SEED_MODE="copy"   # e.g. force a deep copy off-CoW
bmad-loop run …
```

**Always injected by the plugin** (identity from the run context + the five settings; do
not set by hand): `BMAD_LOOP_REPO_ROOT`, `BMAD_LOOP_WORKTREE`, `BMAD_LOOP_RUN_DIR`,
`BMAD_LOOP_STORY_KEY`, `BMAD_LOOP_ENGINE_MCP`, `BMAD_LOOP_ENGINE_EDITOR_MODE`,
`BMAD_LOOP_ENGINE_READY_TIMEOUT`, `BMAD_LOOP_ENGINE_READY_GRACE`, `BMAD_LOOP_UNITY_PATH`,
and `BMAD_LOOP_ENGINE_AGENTS` (the dev + review CLI ids, for per-worktree MCP routing).

### Readiness gate (`unity_ready.py`)

| Variable                     | Default                 | Effect                                                                   |
| ---------------------------- | ----------------------- | ------------------------------------------------------------------------ |
| `BMAD_LOOP_UNITY_READY_TOOL` | `""` (off)              | Opt-in read-only `run-tool` name for a stricter round-trip confirmation. |
| `UNITY_MCP_CLI`              | `unity-mcp-cli`         | IvanMurzak CLI binary.                                                   |
| `UNITY_MCP_URL`              | `http://localhost:8080` | CoplayDev MCP server URL for the connectivity check.                     |

### `per_worktree` setup (`unity_setup.py`)

| Variable                             | Default                 | Effect                                                                            |
| ------------------------------------ | ----------------------- | --------------------------------------------------------------------------------- |
| `BMAD_LOOP_ENGINE_AGENT`             | `claude-code`           | Agent id passed to `setup-mcp`.                                                   |
| `BMAD_LOOP_UNITY_LIBRARY_CACHE`      | (derived)               | Override the symlink-fallback `Library` cache root.                               |
| `BMAD_LOOP_UNITY_LIBRARY_SEED`       | `<repo>/Library`        | Warm `Library` to prime from; empty string disables priming → symlink fallback.   |
| `BMAD_LOOP_UNITY_LIBRARY_SEED_MODE`  | `reflink`               | `reflink` \| `copy` \| `symlink` \| `off`.                                        |
| `BMAD_LOOP_UNITY_MCP_LOCAL`          | `1`                     | `1`/true pins Custom/local mode; `0`/false reverts to a bare cloud-config `open`. |
| `BMAD_LOOP_UNITY_MCP_URL`            | (read from `.mcp.json`) | Local server URL.                                                                 |
| `BMAD_LOOP_UNITY_MCP_TOKEN`          | `""`                    | Bearer token (empty → auth none).                                                 |
| `BMAD_LOOP_UNITY_MCP_TRANSPORT`      | `streamableHttp`        | `streamableHttp` \| `stdio`.                                                      |
| `BMAD_LOOP_UNITY_MCP_AUTH`           | `none`                  | `none` \| `required`.                                                             |
| `BMAD_LOOP_UNITY_MCP_START_SERVER`   | `true`                  | `true` \| `false` — Editor hosts its own server.                                  |
| `BMAD_LOOP_UNITY_MCP_KEEP_CONNECTED` | `true`                  | `true` \| `false`.                                                                |
| `UNITY_MCP_CLI`                      | `unity-mcp-cli`         | IvanMurzak CLI binary.                                                            |

### `per_worktree` teardown (`unity_teardown.py`)

| Variable                        | Default         | Effect                                              |
| ------------------------------- | --------------- | --------------------------------------------------- |
| `BMAD_LOOP_UNITY_CLOSE_TIMEOUT` | `30`            | Polite-quit seconds before escalating to `--force`. |
| `UNITY_MCP_CLI`                 | `unity-mcp-cli` | IvanMurzak CLI binary.                              |

> These defaults are verified against `unity-mcp-cli` v0.81.1. The exact flags and
> subcommands move between releases — each script's module docstring is the
> authoritative, version-stamped source, and any of the above can be overridden in a
> project-local plugin's `[env]` block when yours differ.

### Post-run scratch cleanup (`unity_cleanup.py`)

`unity-mcp-cli` downloads the Editor MCP server into Unity's per-project temp dir
(`/tmp/<companyName>/<productName>/unity-mcp-server-*.zip` on Linux) and never removes
it, so a fresh ~42 MB zip lands every time the pinned server version changes; the Editor
also writes an unbounded `Temp/mcp-server/ai-editor-logs.txt`. On a clean finish the
plugin's `post_run` hook runs `unity_cleanup.py`, which removes this project's server
zips and truncates the log once it exceeds the cap. It runs once per run in **both**
editor modes, after the loop, so it never races an in-flight `setup-mcp` download.
Gated by `[cleanup] clean_tmp` (the engine maps it onto `BMAD_LOOP_CLEAN_TMP`); only the
IvanMurzak MCP downloads per-project, so CoplayDev is skipped.

| Variable                     | Default | Effect                                              |
| ---------------------------- | ------- | --------------------------------------------------- |
| `BMAD_LOOP_CLEAN_TMP`        | `1`     | `0` disables the post-run /tmp + log cleanup.       |
| `BMAD_LOOP_UNITY_LOG_CAP_MB` | `5`     | Truncate `ai-editor-logs.txt` once it exceeds this. |

## Dev-control HTTP bridge (upstream, dev-only — not wired)

Unity-MCP **0.81.1** added an optional **dev-control HTTP bridge** — a
`127.0.0.1`-only HTTP server the Unity plugin exposes for driving and inspecting its
"AI Game Developer" Editor window from outside the process. It is **off by default in
shipped builds**, and **bmad-loop does not use it** — the bundled plugin's readiness
and per_worktree lifecycle run entirely through the `unity-mcp-cli` subcommands above.
It is documented here only so operators know it exists.

Enable it on the Editor side with `UNITY_MCP_DEV_CONTROL=1` (resolution order: process
env > project `.env` > default off); the listen port is `UNITY_MCP_DEV_CONTROL_PORT`
(default **9922**). Endpoints:

| Method + path                                                         | Use                                                    |
| --------------------------------------------------------------------- | ------------------------------------------------------ |
| `GET /health`, `GET /state`                                           | Read live window / server / connection status.         |
| `POST /inject/connection-status`, `/inject/server-status`             | Inject fake states (testing).                          |
| `POST /control/server-url`, `/control/select-agent`, `/control/click` | Drive the window (set URL, pick agent, Connect/Start). |

Why an operator might reach for it **manually**: `GET /state` is a more authoritative
readiness/diagnostic signal than `wait-for-ready` (it reports what the Editor window
actually shows), and the `/control/*` routes can drive the window if a CLI subcommand
drifts in a future release. **Caveats:** it is dev-only and experimental (the surface
may change without notice), and its default port **9922 is fixed** — so concurrent
`per_worktree` Editors would collide on it, and anyone wiring it must assign a distinct
`UNITY_MCP_DEV_CONTROL_PORT` per worktree.
