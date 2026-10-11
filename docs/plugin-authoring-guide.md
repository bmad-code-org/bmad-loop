# Writing a bmad-loop plugin

A **plugin** extends the bmad-loop orchestrator **without touching its core loop**.
It can be as simple as a settings-only data file or as complex as the bundled
Unity game-engine layer. A plugin can:

- **observe, veto, or mutate** the run at every lifecycle stage (a _hook bus_);
- contribute **settings** that render in the settings TUI and persist to policy;
- inject its own **workflow** sessions at defined points in the dev/review cycle.

Plugins are **folder-drop**: a directory with a `plugin.toml` manifest (plus any
helper scripts) dropped under `.bmad-loop/plugins/<name>/`. No registration, no
install step. Dropping a folder in **never runs code**: a project plugin with
anything executable — shell hooks, workflows, seeds, or in-process Python — runs
only after you **grant** it with `bmad-loop plugins enable <name>`, which records
a sha256 of the whole plugin directory outside the repo. See
[Trust & safety](#trust--safety).

> Already wrote a [CLI adapter profile](../README.md#other-coding-clis) or the old
> `[engine]` block? Same idea — declarative TOML + optional scripts, discovered and
> overlaid. The plugin system generalizes both.

---

## Quick start: the smallest plugin

A data-only plugin carries one setting and zero behavior. Create
`.bmad-loop/plugins/hello/plugin.toml`:

```toml
[plugin]
name = "hello"
version = "1.0.0"
api_version = 1
description = "Smallest possible plugin."
author = "you"

[[settings]]
key = "greeting"
type = "str"
default = "hi"
label = "Greeting"
help = "Shown in the settings UI."
```

That's a complete, loadable plugin. It has no `[hooks]` and no `[python]`, so it
runs no code and is byte-identical to "no plugin" at run time — it exists only to
contribute a setting. This is the shape the bundled
[`example`](../src/bmad_loop/data/plugins/example/plugin.toml) plugin ships in.
A settings-only plugin needs no grant: it loads as soon as it is discovered, and
its section always shows in the settings TUI.

Now give it behavior. Append a hook:

```toml
[hooks.pre_story]
cmd = 'python3 "{scripts}/hello.py"'
```

and add `.bmad-loop/plugins/hello/hello.py` beside the manifest. The plugin now
has an executable surface, so it is **inert until you grant it**. The next
`bmad-loop run` prints a `note:` that `hello` is not granted, and runs without
it. Review the folder, then grant it:

```console
$ bmad-loop plugins enable hello
plugin "hello" granted: sha256:3f9a1c0b7d2e (2 files) in ~/.local/state/bmad-loop/<project-tag>/plugin-grants.json
```

The grant pins the bytes of every file in the folder. Edit any of them and the
next launch stops with `plugin "hello" changed since it was granted`. Review the
change, then re-pin it with `bmad-loop plugins accept hello`. `bmad-loop plugins`
lists every discovered plugin with its grant status.

---

## Distribution & discovery

Plugins are discovered from three sources, overlaid in precedence order (a later
same-name plugin **overrides** an earlier one; a new name **extends** the set):

| Source        | Path                                              | Precedence             |
| ------------- | ------------------------------------------------- | ---------------------- |
| Builtin       | `bmad_loop/data/plugins/<name>/plugin.toml`       | base                   |
| Entry point   | `bmad_loop.plugins` group                         | _reserved (see below)_ |
| Project-local | `<project>/.bmad-loop/plugins/<name>/plugin.toml` | **highest**            |

Each plugin lives in its **own directory** — that directory is the plugin's
`{scripts}` root (see [`{scripts}` substitution](#scripts-substitution)), so its
manifest and helper scripts sit together.

**Origin decides trust.** Builtin plugins (and, once they exist, entry-point
plugins) are trusted by provenance: they ship with the installed package. A
project-local plugin lives in the agent-writable repo, so it runs only under an
operator grant (see [Trust & safety](#trust--safety)). A project plugin that
overrides a builtin name gets no trust from the builtin: the override needs its
own grant, and a refused override does **not** fall back to the builtin.

**Entry points are a documented future seam.** `discover()` already yields an
`entry_point` source between builtin and project, but `_discover_entry_points()`
returns nothing today — folder-drop is the only live distribution path. When
pip-installable plugins land, they slot in here with **no change to authors or to
discovery order** (`importlib.metadata` selectable entry points, group
`bmad_loop.plugins`). See `src/bmad_loop/plugins/loader.py`.

---

## The manifest (`plugin.toml`)

Every plugin is one `plugin.toml`. Only `[plugin] name` + `api_version` are
required; every section below is optional, so a plugin opts into exactly what it
needs. The manifest parses into the immutable `PluginManifest`
(`src/bmad_loop/plugins/model.py`).

### `[plugin]` — metadata

| Field         | Type   | Default | Purpose                                                                         |
| ------------- | ------ | ------- | ------------------------------------------------------------------------------- |
| `name`        | string | —       | **Required.** Plugin id; the directory name; the `[plugins.<name>]` key.        |
| `api_version` | int    | —       | **Required.** Plugin-API version this manifest targets (currently `1`).         |
| `version`     | string | `0.0.0` | Your plugin's own version.                                                      |
| `description` | string | `""`    | One line; shown in tooling.                                                     |
| `author`      | string | `""`    | Attribution.                                                                    |
| `priority`    | int    | `0`     | Cross-plugin ordering at a shared stage; **lower runs first**, then load order. |
| `seed_files`  | list   | `[]`    | Project-relative gitignored files to copy into each isolated worktree.          |
| `seed_globs`  | list   | `[]`    | Project-relative glob patterns to expand + copy into each worktree.             |

`seed_files` / `seed_globs` must be **project-relative** (an absolute path is
rejected at load). They let a plugin prime an isolated checkout with gitignored
paths it needs — e.g. the Unity plugin seeds an MCP-generated skill tree.

### `[[settings]]` — settings schema

Each entry contributes one setting. See [Settings](#settings) for the full
reference.

```toml
[[settings]]
key = "strict"        # required; unique within the plugin
type = "bool"         # bool | int | float | str | select
default = false
label = "Strict mode" # TUI label (falls back to the key)
help = "..."          # TUI help text
# select-only:
# options = ["a", "b"]
# numeric hints (int/float):
# min = 0
# max = 10
```

### `[hooks.<stage>]` — declarative (out-of-process) hooks

A shell command bound to a [lifecycle stage](#stage-reference). See
[Hooks](#hooks).

```toml
[hooks.pre_session]
cmd = 'python3 "{scripts}/probe.py"'
timeout_sec = 120          # default 120; must be >= 1
blocking = true            # non-zero exit vetoes (defers) the unit
fail_closed = false        # default: a hook *error* (timeout/launch) fails open
```

### `[python]` — in-process module (trust-gated)

```toml
[python]
module = "hooks.py"        # plugin-relative file
class = "MyPlugin"         # subclass of bmad_loop.plugins.Plugin (default "Plugin")
```

The module is never imported until the plugin is trusted: a **builtin** by naming
it in `[plugins] enabled`, a **project** plugin by granting it with
`bmad-loop plugins enable <name>`. An un-enabled builtin `[python]` plugin is
wholly inert (its hooks, workflows and seeds too). See [Trust](#trust--safety).

### `[workflows.<name>]` — provided workflows

An extra agent session injected at a stage. See [Workflows](#workflows-provides).

```toml
[workflows.lint-sweep]
stage = "post_dev_phase"   # post_dev_phase | post_review_result | pre_commit_gate
role = "dev"               # dev | review
prompt = "/lint-sweep {story_key}"
blocking = false           # true: a failed session defers the unit
```

Declare `<name>_enabled` / `<name>_blocking` settings to let operators disable a
step or flip its gate per run — see
[Making a workflow configurable](#making-a-workflow-configurable).

### `{scripts}` substitution

In any hook `cmd` or workflow `prompt`, `{scripts}` expands to the plugin's own
directory — so a plugin references its bundled scripts without hardcoding a path
that breaks across machines or between a builtin and a project-local copy.

---

## Trust & safety

Trust is decided by **origin** and pinned by **content** (#496, #497). A name is
not trusted on its own: a folder can be swapped under a name that was approved
before.

1. **Builtin and entry-point plugins — trusted by provenance.** They ship inside
   the installed package. Their manifests, settings and declarative hooks load
   when discovered. A builtin that declares an in-process `[python]` module runs
   only when its name is in:

   ```toml
   [plugins]
   enabled = ["unity"]
   ```

   An un-enabled builtin `[python]` plugin is **wholly inert**: its module, its
   `[hooks.*]`, its workflows and its seeds all stay off, and the run's
   `plugins-active` journal line leaves it out.

2. **Project plugins (`.bmad-loop/plugins/<name>/`) — trusted by content grant.**
   A project plugin with an **executable surface** runs only after the operator
   grants it. An executable surface is any of: `[hooks.*]`, `[workflows.*]`,
   `[python]`, `seed_files` or `seed_globs`. A settings-only project plugin needs
   no grant.

   ```console
   $ bmad-loop plugins                   # list plugins, origin, surface, grant status
   $ bmad-loop plugins enable <name>     # first grant
   $ bmad-loop plugins accept <name>     # re-pin after reviewing a change
   $ bmad-loop plugins revoke <name>     # drop the grant
   ```

   Toggling a project plugin on in the [settings TUI](tui-guide.md) grants it too.

   **What a grant is.** A sha256 over **every file in the plugin directory** (the
   manifest, scripts, modules and dotfiles; `__pycache__` excluded), plus the
   directory's path relative to the repo. It is stored **outside the repo**, at
   `<state root>/<project-tag>/plugin-grants.json`, because the repo and its
   `policy.toml` are agent-writable and a grant kept there could be forged. For a
   project plugin the grant replaces `[plugins] enabled`: `bmad-loop plugins`
   never writes `policy.toml`. Grants are per machine and keyed by the project
   path.

   **What a launch does.** `bmad-loop run`, `sweep` and `resume` each read the
   grants **once**, judge every plugin before their first side effect,
   and hand the same frozen grants to the engine and to any auto-sweep child.
   Nothing later re-reads the store. The outcome depends on what the operator
   intended:

   | Plugin state                                                     | Outcome                                                                                            |
   | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
   | never granted, not in `[plugins] enabled`                        | **inert**: a `note:` at launch, a `plugin-refused` journal row, the run goes on without it         |
   | named in `[plugins] enabled` but never granted                   | **startup fails**; the error points to `bmad-loop plugins enable <name>`                           |
   | granted, then a file added, removed or changed, or the dir moved | **startup fails** (`changed since it was granted`); review, then `bmad-loop plugins accept <name>` |
   | granted dir unreadable (a link inside it, over a cap)            | **startup fails**; fix the dir, then `bmad-loop plugins accept <name>`                             |
   | granted and unchanged                                            | runs from a verified snapshot                                                                      |

   A fatal refusal is raised before **any** plugin code runs, builtins included.
   In an auto-sweep child it lands as `sweep-auto-not-started` plus a
   notification. Nothing on a run, sweep or child path ever prompts: a refusal
   never turns into a stall waiting for an answer.

   **Snapshot execution.** A granted plugin does not run from its live folder. At
   launch its verified files are copied to a content-addressed snapshot under
   `<state root>/<project-tag>/_plugin-snapshots/<digest>/`, and that copy
   becomes its `{scripts}` root. A `[python]` module is compiled from the exact
   bytes the grant hashed; no `.pyc` is read or written. A session that edits
   the live plugin folder mid-run therefore changes nothing until the next launch,
   which refuses the edit. Hooks run with `PYTHONDONTWRITEBYTECODE=1`.

   **Limits.** The plugin directory itself may be a symlink, but links (and
   Windows junctions) inside it are refused. Caps: 2048 files, 16 MiB per file,
   64 MiB total.

   `bmad-loop validate` reports every refusal under the `plugins.trust` check.

**What a grant does not cover.** These residuals are known and deliberate:

- **Same-user tampering outside the repo.** The store and the snapshots live in
  the user's state directory. A process running as the same user that sets out to
  rewrite them can. Out-of-tree moves the trust record out of reach of a session
  editing the repo; it is not a sandbox (the same boundary as the #571 config
  digest).
- **Moving the project loses its grants** (#572). They are keyed by the project
  path, so a moved or re-cloned project starts ungranted. This fails closed:
  re-grant after a move.
- **Files a hook names outside `{scripts}`.** A hook `cmd` that runs a script
  elsewhere in the repo runs that script unpinned. Keep everything a hook
  executes inside the plugin folder.
- **Settings steer a granted plugin.** `[plugins.<name>]` settings live in
  `policy.toml` and are not part of the grant; a granted plugin reads whatever
  values are there at launch.
- **Builtin provenance follows the install.** In an editable install (a source
  checkout set up with `uv sync`), the builtin plugins are files in a working
  tree, and they are still trusted as builtins.
- **Snapshot leftovers.** An interrupted snapshot build can leave
  `<digest>.tmp-*` or `<digest>.stale-*` directories under `_plugin-snapshots/`.
  They are never executed unverified; deleting them is safe when no run is live.
- **A merged story that edits a tracked plugin folder** makes the next launch
  fail with a mismatch. That is the gate working: review the change, then
  `bmad-loop plugins accept <name>`.

**Failure isolation.** Every hook — subprocess or Python — is wrapped. A Python
handler that raises is caught (`except Exception` only — `RunStopped`/SIGTERM as
`BaseException` always propagate), journalled `plugin-error`, and the offending
instance is **disabled for the rest of the run**. The run survives. A blocking
declarative hook fails **open** by default (an error lets the run continue; only a
clean non-zero exit vetoes); set `fail_closed = true` to make any failure defer
the unit. An in-process handler can opt into the same by setting `fail_closed =
True` on its class.

**Hard stops.** Declarative hooks (and the verify commands) run through a
stop-aware runner. A hard `bmad-loop stop` that lands while a hook is running
kills the hook's process tree, and the run stops: the bus journals
`plugin-hook-interrupted` (`plugin`, `stage`) instead of `plugin-hook-error`, and
`fail_closed` never turns that into a veto, because an interrupted hook has not
failed. If the interrupted hook is on `post_run`, the run still finishes. A hook
timeout kills the tree the same way. The runner re-scans the tree under the
hook's root once a second while that root runs, and again every 50 ms while the
root winds down after its signal; at kill time it also takes in each surviving
known process's current children. Only processes still alive with a matching
recorded identity are signalled. A background job whose parent shell exited is
killed if its parent chain was still under the live root at one of those scans,
and a process forked in the root's TERM handler is killed if the root lived one
50 ms re-scan after forking. These cannot be reached: a process that starts and
is reparented away within one scan interval, a TERM handler that forks and exits
at once, a descendant already reparented away from a surviving job once the root
has exited, and a process that starts while the survivors are being reaped. On
macOS without `psutil`, only the root is killed. Hooks that run as part of the
stop's own unwind — `pre_worktree_teardown`/`post_worktree_teardown`,
`pre_rollback`/`post_rollback` — are not interrupted and still run to completion.

**Versioning.** Every manifest declares `api_version`. The framework supports a
set of versions (`SUPPORTED_API`). A **builtin** with an unsupported version is a
hard error (a packaging bug we shipped); a **third-party** one is **skipped with a
warning** (`plugin-skipped`) so a stale drop-in can never take a run down.

---

## Settings

A `[[settings]]` entry is presentation + validation metadata. The vocabulary
matches the core settings fields exactly:

| Field       | Applies to    | Meaning                                          |
| ----------- | ------------- | ------------------------------------------------ |
| `key`       | all           | Unique within the plugin; the policy + env key.  |
| `type`      | all           | `bool` \| `int` \| `float` \| `str` \| `select`. |
| `default`   | all           | Value when the operator hasn't set one.          |
| `label`     | all           | TUI label (falls back to `key`).                 |
| `help`      | all           | TUI help text.                                   |
| `options`   | `select`      | Non-empty list of allowed string values.         |
| `min`/`max` | `int`/`float` | Numeric bounds (widget hints).                   |

**Rendering.** A plugin's settings appear as their own section in the settings
TUI (generated from the schema — see `src/bmad_loop/settings_schema.py`): always
for a settings-only plugin, and once it is enabled or granted for one with
executable surface. They persist to a `[plugins.<name>]` table in `policy.toml`:

```toml
[plugins.my-plugin]
strict = true
mode = "b"
```

**Reading a setting.**

- In an **in-process** plugin: `self.settings["strict"]` — the manifest defaults
  overlaid by the operator's `[plugins.<name>]` table.
- In a **declarative** hook: each setting is exported as an environment variable
  `BMAD_LOOP_SETTING_<KEY>` (uppercased), already resolved.
- Anywhere with a `Policy`: `policy.plugin_setting("my-plugin", "strict", default)`.

Settings are **data**, not code — a plugin can carry `[plugins.<name>]` settings
without being enabled or granted (the settings UI just won't surface an inactive
plugin's section). The trust gate is on code, so settings are not part of a
grant: a granted plugin reads whatever `[plugins.<name>]` values are present at
launch.

---

## Hooks

A hook binds a [stage](#stage-reference) to behavior. The **hook bus**
(`src/bmad_loop/plugins/bus.py`) fans each stage out to every bound plugin, in
registry order (`priority`, then load order). A run with no plugin bound to a
stage does no work for it (an O(1) fast-path) — zero-plugin runs stay
byte-identical.

### Declarative hooks

A `[hooks.<stage>]` shell command. The bus runs it with:

- **cwd** = the unit's worktree (or repo root);
- a `BMAD_LOOP_*` environment describing the run:

  | Var                                                                               | Meaning                                                 |
  | --------------------------------------------------------------------------------- | ------------------------------------------------------- |
  | `BMAD_LOOP_STAGE`                                                                 | the stage firing                                        |
  | `BMAD_LOOP_RUN_ID` / `BMAD_LOOP_RUN_DIR`                                          | run identity                                            |
  | `BMAD_LOOP_REPO_ROOT` / `BMAD_LOOP_WORKTREE`                                      | git roots                                               |
  | `BMAD_LOOP_STORY_KEY` / `BMAD_LOOP_ROLE` / `BMAD_LOOP_PHASE` / `BMAD_LOOP_BRANCH` | unit context                                            |
  | `BMAD_LOOP_AGENTS`                                                                | comma-separated CLI agent ids in the worktree           |
  | `BMAD_LOOP_DELIVERY_ID`                                                           | dedup key of an at-least-once stage                     |
  | `BMAD_LOOP_ROLLBACK_OUTCOME`                                                      | `post_rollback` only: `completed` / `paused` / `failed` |
  | `BMAD_LOOP_PLUGIN`                                                                | your plugin's name                                      |
  | `BMAD_LOOP_SETTING_<KEY>`                                                         | each resolved setting                                   |

A **blocking** hook's non-zero exit **vetoes** (defers) the unit. A non-blocking
hook is advisory (logged `plugin-hook`).

**Mutating from a declarative hook.** Emit a single JSON object on the **last
non-empty stdout line**:

```json
{
  "shared": { "scanned": 42 },
  "mutate": { "proposed_commit_message": "rewritten by my-plugin" },
  "veto": { "action": "defer", "reason": "lint failed" }
}
```

- `shared` — merged into the cross-stage `shared` dict.
- `mutate` — only [whitelisted fields](#the-hookcontext) for the current stage.
- `veto` — `action` ∈ `skip` \| `defer` \| `pause`. Supplying a `veto` replaces
  the implicit "non-zero exit = defer".

Any non-JSON output is treated as advisory log text.

### In-process hooks

Subclass `bmad_loop.plugins.Plugin` and define `on_<stage>(self, ctx)` methods.
The bus calls the handler for each stage you implement; you only mark the stages
you handle, so the fast path holds for the rest.

```python
from bmad_loop.plugins import Plugin

class MyPlugin(Plugin):
    fail_closed = False   # a raised handler is isolated; True also defers the unit

    def on_pre_commit(self, ctx):
        ctx.proposed_commit_message = f"{ctx.proposed_commit_message}\n\nShipped-by: me"
```

Optionally override `validate(self, policy)` to **reject an incompatible config at
startup** (raise `PluginError`) — e.g. a coupling between a plugin setting and a
core policy field. This runs once before any stage; a raise fails the run fast
(it is a deliberate config rejection, not isolated like a hook bug).

> **Don't do expensive or side-effecting work in `__init__`.** Construction
> happens at registry-build time, and a raised exception there disables the
> instance.

### The `HookContext`

Every hook receives one `HookContext` (`src/bmad_loop/plugins/context.py`) for the
stage. It carries:

- **Read-only facts** (properties, no setter): `run_id`, `story_key`, `epic`,
  `phase`, `attempt`, `role`, `worktree`, `branch`, `repo_root`, `run_dir`,
  `agents`, `result_json` (a copy), `session_status`, `verify_reason`,
  `decision_action`, `delivery_id`, `rollback_outcome`, `settings`. Observe
  these; you can never rewrite history.
- **A mutable whitelist** — assign only these, and only where the stage allows:
  `proposed_prompt`, `proposed_env`, `proposed_feedback`,
  `proposed_commit_message`, `proposed_decision`.
- **`ctx.shared`** — a free-form, JSON-serializable dict that **persists across
  stages** (the engine backs it with `RunState.plugin_shared`, so it survives
  pause/resume). Use it to carry state between your own hooks.

**Veto** is `ctx.veto(action, reason)`, with `action`:

| Action  | Routes onto the engine's existing… | Effect                                   |
| ------- | ---------------------------------- | ---------------------------------------- |
| `skip`  | quiet retire                       | unit dropped (DEFERRED), no notification |
| `defer` | defer primitive                    | unit deferred + operator notified        |
| `pause` | escalation                         | run pauses (raises `RunPaused`)          |

There is **no new abort path** — a veto maps onto control flow the engine already
has. Multiple plugins can object; the bus collects every veto without
short-circuit and resolves the **most-conservative** one (`pause` > `defer` >
`skip`), so load order can never hide a severer objection. A `post_*`-stage veto
is **clamp-conservative only**: a plugin can escalate the engine's own decision,
never silence it.

---

## Stage reference

Stages fire in `pre_`/`post_` pairs around each unit of work. `post_*` stages see
the mutations earlier `pre_*` stages made. The **mutable surface** column lists
what a hook may assign at that stage; everything else on the context is read-only
there.

### Run / loop (no unit — a `pause` veto pauses the run)

| Stage                                      | When                            |
| ------------------------------------------ | ------------------------------- |
| `pre_run` / `post_run`                     | around the whole run            |
| `pre_pick_next` / `post_pick_next`         | around selecting the next story |
| `pre_epic_boundary` / `post_epic_boundary` | at an epic transition           |

Under `[gates] retrospective = "auto"`, the epic transition also runs one headless
retrospective session between those two stages. That session fires
`pre_retro_session`, then the generic `pre_session` / `post_session`, with
`role = "retro"` and the story key `epic-<N>-retrospective`. That key is not a
story in the run's state. A veto on either pre-stage fails the retrospective
(journaled `retro-auto-failed`); it does not pause the run.

### Story / unit

| Stage                                              | When                                                                                                                                         | Mutable surface                                       |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| `pre_story` / `post_story`                         | around one story                                                                                                                             | veto (`pre_`)                                         |
| `pre_worktree_setup` / `post_worktree_setup`       | around isolated-worktree provisioning                                                                                                        | —                                                     |
| `pre_ready_gate` / `post_ready_gate`               | around the engine-ready gate                                                                                                                 | veto (`pre_`)                                         |
| `pre_worktree_teardown` / `post_worktree_teardown` | around teardown (in a `finally`)                                                                                                             | **observe-only** — a veto here cannot un-tear-down    |
| `pre_rollback` / `post_rollback`                   | around a failed attempt's `git reset --hard` (only when a rollback actually runs); `post_` pairs every `pre_`, even a paused/failed rollback | **observe-only** — a veto here cannot block the reset |
| `pre_integrate`                                    | before integrating a finished unit                                                                                                           | —                                                     |
| `pre_merge` / `post_merge`                         | around the local branch merge                                                                                                                | —                                                     |

`post_story` fires after an isolated unit's worktree teardown. If the worktree was
removed, a declarative `post_story` hook runs from the repo root and
`BMAD_LOOP_WORKTREE` still names the removed worktree; if it was kept (a deferred
unit under `keep_failed`), the hook runs from that worktree.

`post_rollback` fires exactly once for every emitted `pre_rollback`, including when
the rollback stops part-way — it pauses the run for manual recovery (an
attempt-owned spec it cannot safely restore, work it could not park, a refused
reset) or an unexpected error escapes it. `ctx.rollback_outcome`
(`BMAD_LOOP_ROLLBACK_OUTCOME` for a declarative hook) says which: `completed`,
`paused`, or `failed`; after the latter two the tree may be only partially reset.
It is `None` on every other stage, `pre_rollback` included. A plugin that
quiesces in `pre_rollback` can therefore always rely on the matching release;
one that does post-reset work (re-installing dependencies, re-applying state)
should gate it on `completed`, since `post_rollback` no longer implies the reset
finished.
Rollback paths that never emit `pre_rollback` (a clean tree, a policy pause with
`[scm] rollback_on_failure` off, an owned-spec pause raised before the rollback
starts) emit neither stage.

### Dev

| Stage                              | When                             | Mutable surface                                                                      |
| ---------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------ |
| `pre_dev_phase` / `post_dev_phase` | around the dev attempt loop      | veto (`pre_`); `post_dev_phase` is a [workflow injection point](#workflows-provides) |
| `pre_dev_session`                  | before each dev session          | `proposed_prompt`, `proposed_env`, veto                                              |
| `post_dev_verify`                  | after dev or repair verification | —                                                                                    |

`post_dev_verify` fires on **both** legs of the dev phase — once after the dev
session's verification, and again after each repair session's — so a handler sees
it **more than once per story**, not once. The two legs share one `attempt`
counter bounded by `[limits] max_dev_attempts`, which is also the bound on how
many times the stage can fire for one story. Write handlers to be idempotent and
to key on the correlation fields below rather than on the story alone. It also
fires on the way to a pause: an attempt whose session reported a CRITICAL
escalation emits before the run stops, on either leg — and so does a dev or repair
leg whose `[environment]` probe preflight failed, with no command results. One
emission falls outside that bound: a `bmad-loop resolve --reverify` replay fires
the stage once more with **no session** — `ctx.session_status` is `None`,
`ctx.result_json` is the latest completed dev session's result, and `attempt` does
not advance — so a handler that assumes a session behind every emission must
check `session_status` first.

`post_dev_verify` exposes `ctx.command_results`: an immutable tuple of the
per-command `CommandResult` records core just executed. Each has `command`,
`returncode`, the existing merged bounded `output_tail`, separate `stdout`
and `stderr` strings, and `spawn_error` — normally `None`, and set when the child
could not be started at all. The typical cause is the directory it was to run in
(missing, not a directory, or unsearchable), which the message names, but any
spawn-time `OSError` lands here — a missing shell, EMFILE, ENOMEM — so read the
wrapped exception rather than assuming the directory. Such a result carries no
real exit status (`verify.SPAWN_FAULT_RC`, deliberately outside the range a
signal-killed child reports and distinct from the timeout leg's `-1`) and
classifies as an environment fault, which pauses the run. The two stream strings
are intended to be the streams essentially whole
— they are not cut to `[verify] stream_capture_kb`, which bounds only what is
written to disk — but they are not unbounded either: a hard 32 MiB per-stream
ceiling applies, so a pathologically chatty command cannot grow the orchestrator's
peak memory with the number of configured verify commands. When that ceiling cuts
a stream the **tail** is what a plugin receives, and the matching journal record's
`stdout_bytes` / `stderr_bytes` still report what the command emitted, so the cut
is always detectable rather than silent. Ordinary suites never reach it. This is
observation data only: a plugin cannot change the verifier's outcome or the commit
decision. The run's `journal.jsonl` also records
one `verify-command-result` entry per command with run/story/attempt/stage and
verification-sequence correlation — note that `attempt` is the dev/repair counter, so every review
cycle of one attempt shares a single value and only `verification_sequence` tells successive review
passes apart — `output_tail`, `spawn_error`, byte counts, and run-relative `stdout_path` /
`stderr_path` pointers under the run's `verify/` directory; full streams are not
embedded in the journal. `spawn_error` rides the record because the record's
readers are out-of-process and `returncode` alone cannot separate a child that
never started from one that ran. A pass cut short by a hard `bmad-loop stop` (DW-353)
records `interrupted: true` with `returncode` −1001 (`INTERRUPTED_RC`): the command
the stop killed has `output_tail` `interrupted by a hard stop request`, and every later
command, which was never started, still gets a record, with `output_tail`
`not started: an earlier command was interrupted by a hard stop request`. The run
stops right after journalling that pass, so no hook observes it and no decision is
taken on it. The field is absent from every pass that ran through. That store is deliberately separate from `logs/`, which
holds coding-CLI pane captures named after session task ids and is read as such
by the TUI.

Two more context fields say **which** verification the results came from, because
nothing else on the context can: both the dev leg and the repair leg emit
`post_dev_verify` from `Phase.DEV_VERIFY`, and `ctx.attempt` is one per-story
counter the repair leg continues rather than restarts, so its value orders the
two but names neither.

| Field                       | Value                                                                                  |
| --------------------------- | -------------------------------------------------------------------------------------- |
| `ctx.verification_stage`    | `"dev"` for the initial dev verification, `"fix"` for a repair one, `None` if none ran |
| `ctx.verification_sequence` | the story's 1-based ordinal for that pass, or `None` if it recorded nothing            |

The review gate publishes the same pair on its own stage, [`post_review_verify`](#review),
always as `"review"` (or `None`).

Together they are the join key: the `verify-command-result` entries carrying this
`story_key` + `verification_stage` + `verification_sequence` are exactly this
context's results, one per record, ordered by `command_index`. The sequence is
monotonic per story **across a pause/resume** — unlike `attempt`, which a human
re-arm reuses — so it is safe to persist as a correlation id.

Read `ctx.command_results == ()` together with `verification_stage`; on its own it
is ambiguous:

- **`verification_stage is None`** — no verify pass ran. Several causes land here
  and the empty tuple names none of them: the session did not complete
  (`ctx.session_status`), an earlier gate already failed the attempt — the
  dev-artifact check or the deferral harvest (`ctx.verify_reason`) — an
  `[environment]` probe failed the preflight, so no command was started
  (`ctx.verify_reason` names the probe) — or the engine
  variant suppressed the pass for this leg (stories mode skips it on a plan-halt
  leg, which has no implementation to build).
- **stage set, `verification_sequence is None`** — the pass ran and executed
  nothing, because `[verify] commands` is empty. No journal record exists either.
- **stage set, sequence an int** — those commands ran, and each has a matching
  journal record.

What lands on disk is bounded by `[verify] stream_capture_kb` (default 256 KiB per
stream): the **tail** is retained, and the record stays explicit about the cut —
`stdout_bytes` / `stderr_bytes` are what the command emitted, `stdout_captured_bytes` /
`stderr_captured_bytes` how much of that reached disk, and `stdout_truncated` /
`stderr_truncated` their inequality. Both counts are UTF-8 lengths of the decoded
stream, **not** file sizes: the files are written in text mode, so Windows newline
translation makes the file larger there. Set the knob to `0` to retain nothing at
all — no files are written and the pointers are null, but the record still lands
with the full byte counts, because "nothing was retained" and "the command was
silent" are different facts. Retaining is observation and never fails a run: if the
write raises (ENOSPC, a read-only run dir, or a `verify/` directory whose
confinement cannot be established — the store refuses rather than write through
a symlink a session planted), the pointer is null and `capture_error`
carries the reason. A plugin reading these pointers must therefore treat both
`None` and a missing file as normal, and consult `*_truncated` before assuming a
file holds a command's whole output. Treat verifier output as potentially sensitive and store, upload, sign,
or act on it only from an explicitly configured plugin.

**`post_dev_verify` covers the dev phase only.** `[verify] commands` also run at
the _review_ gate — `verify_review` / `verify_review_stories` /
`verify_review_bundle` end on the same core classifier — and those runs are
journalled (`verification_stage: "review"`, sharing the story's one
`verification_sequence` counter with the dev and fix passes) and published on
their own stage, [`post_review_verify`](#review). They run in `repo_root`, the same root
the dev phase uses (#695); only the gates' own artifact reads — the spec, the
sprint board, the deferred-work ledger — stay project-rooted. Five engine gates reach them: the
converged review pass, the review-budget-exhaustion rescue, the review-timeout
salvage, and both passes inside the skip-review commit path (which runs the gate
again after a repair). The records do not name which of the five ran — the
neighbouring `review-result` / `review-skipped*` / `review-timeout-salvage*`
entries and the sequence ordering say that. `bmad-loop confirm --reverify` runs
the commands too, out of band by construction — the run that parked the story is
finished, so there is no journal to write to and no hook bus to emit on.

Two consequences a handler has to be written for:

- **`verify-command-result` entries are a complete census of a RUN's verifier
  command invocations, but not of every gate visit or of a project's.** Every
  command an in-run dev, fix or review pass executed lands a record. A pass cut
  short by a hard stop also records each command it never started, marked
  `interrupted: true` with a `not started:` `output_tail`, so the record count of
  such a pass is its configured command count, not what actually ran. A pass
  with no `[verify] commands` configured executes nothing and therefore records
  nothing; `bmad-loop confirm --reverify` stays outside because it runs after the
  run that parked the story is over. Count distinct `verification_sequence`
  values to derive recorded passes, while preserving that zero-command caveat.
- **One command record is not a pass verdict**, and an earlier red pass is not
  evidence the commit was blocked: a failed pass can be followed by a green
  review-gate pass and a commit. Group records by `verification_sequence`, then
  correlate that group with its surrounding decision event instead of inferring
  the decision from one command or from an older pass. The dev and fix legs use
  `dev-decision` / `fix-decision`; review passes use the applicable neighbouring
  `review-result`, `review-skipped*`, `review-timeout-salvage*`,
  `review-budget-committed`, or `review-followup-damped` event described above.

### Review

| Stage                 | When                           | Mutable surface                                   |
| --------------------- | ------------------------------ | ------------------------------------------------- |
| `pre_review_phase`    | before the review loop         | veto                                              |
| `pre_review_session`  | before each review session     | `proposed_prompt`, `proposed_env`, veto           |
| `post_review_session` | after each review session      | —                                                 |
| `post_review_result`  | after a review verdict         | a [workflow injection point](#workflows-provides) |
| `post_review_verify`  | after each review verify gate  | —                                                 |
| `pre_fix_session`     | before a verify-repair session | `proposed_prompt`, `proposed_env`, veto           |

`post_review_verify` is the review leg's counterpart of `post_dev_verify`: its own
stage, not a second meaning for that one. It fires once per review verify gate
visit — the converged review pass, the review-budget-exhaustion rescue, the
review-timeout salvage, and each of the (up to two) passes inside the skip-review
commit path — in every run mode (sprint, stories, sweep). A gate visit ended by
an exception (a hard stop, a repair pause, an engine error) is never published. A
story can therefore see it **several times** (once per review cycle that reached
the gate, plus a re-check after a verify-repair), so write handlers to be
idempotent and key on `verification_sequence` when it is set (it is `None` when no
commands ran). It fires after the gate itself has finished — including its own
acceptance bookkeeping (binding the review artifact source, a sweep's ledger
reclose) — and before the engine routes on the outcome (journals
`review-verify-failed`, escalates, repairs, or commits). It is observation only: a
handler cannot change the gate's outcome, the routing, or the journal.

The context carries the same payload as `post_dev_verify`:

| Field                       | Value                                                                                                                                                                                                                                                                                                                                                           |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ctx.session_status`        | the status of the review session whose product was gated — the timeout-class status on the review-timeout salvage visit; `None` on the skip-review path, where no review session ran                                                                                                                                                                            |
| `ctx.result_json`           | that review session's result (a copy) — often `None` on the timeout-salvage visit; `None` on the skip-review path                                                                                                                                                                                                                                               |
| `ctx.verify_reason`         | the gate's outcome reason (the failure reason on a red gate)                                                                                                                                                                                                                                                                                                    |
| `ctx.command_results`       | the `CommandResult` records the gate's command pass ran, in order                                                                                                                                                                                                                                                                                               |
| `ctx.verification_stage`    | `"review"` whenever the gate reached its command pass (including zero commands); `None` when one of the gate's artifact checks refused first — the spec, the operator-action list, the sprint board, or a sweep's ledger (stories mode checks the spec only) — or a failed `[environment]` probe stopped the gate before its commands; `verify_reason` names it |
| `ctx.verification_sequence` | the story's ordinal for that pass (joins the `verify-command-result` entries), or `None` if it recorded nothing (no `[verify] commands`, or no pass)                                                                                                                                                                                                            |

Read `command_results == ()` with `verification_stage` exactly as for
`post_dev_verify`. A review pass cut short by a hard `bmad-loop stop` stops the run
before the gate is classified, so it is never published.

### Commit

| Stage             | When                                        | Mutable surface                                                                          |
| ----------------- | ------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `pre_commit_gate` | just before every commit, on every path     | a [workflow injection point](#workflows-provides); defer-safe (the unit may still defer) |
| `pre_commit`      | before committing (after `pre_commit_gate`) | **`proposed_commit_message`**; only a `pause` veto is honored (the unit is mid-commit)   |
| `post_commit`     | after committing                            | —                                                                                        |

### Generic session boundary

| Stage          | When                                                                                         | Mutable surface                         |
| -------------- | -------------------------------------------------------------------------------------------- | --------------------------------------- |
| `pre_session`  | before **every** session (after the role-specific `pre_*_session`, so it sees its mutations) | `proposed_prompt`, `proposed_env`, veto |
| `post_session` | after every session                                                                          | —                                       |

### Sweep (deferred-work cycle — bundles inherit all per-story stages)

| Stage                                                  | When                             |
| ------------------------------------------------------ | -------------------------------- |
| `pre_sweep_cycle` / `post_sweep_cycle`                 | around a sweep cycle             |
| `pre_triage_session` / `post_triage`                   | around triage                    |
| `pre_migrate_session` / `post_migrate`                 | around a legacy-ledger migration |
| `pre_close_resolved` / `post_close_resolved`           | around closing resolved entries  |
| `pre_decision` / `post_decision`                       | around a human-decision item     |
| `pre_bundle` / `post_bundle`                           | around a deferred-work bundle    |
| `pre_materialize_bundles` / `post_materialize_bundles` | around materializing bundles     |

`post_migrate` gets **at least one delivery attempt**. The engine records the
completed migration before firing it and, if the process or host dies before the emit
returns, fires it again on resume. A returned emit counts as delivered even when a
handler failed: a Python raise, a declarative error, timeout or non-zero exit is not
retried, and a veto on `post_migrate` is ignored. Every delivery of one completion
carries the same `ctx.delivery_id` (`BMAD_LOOP_DELIVERY_ID` for a declarative hook),
and a later re-migration gets a new one. A handler with non-idempotent side effects
should deduplicate on that id. Stages without an id leave `ctx.delivery_id` as `None`
and do not set the variable. A migration whose ledger holds no legacy entries completes
without a session and fires neither `pre_migrate_session` nor `post_migrate`.

---

## Workflows (`[provides]`)

A **workflow** is the conservative form of custom orchestration: an **extra agent
session** injected at a lifecycle stage, run through the engine's normal session
machinery — **no new pipeline stage**. It is the right tool when you want an
additional pass (a doc sync, a lint sweep, an extra reviewer) without rewriting
the loop.

```toml
[workflows.doc-sync]
stage = "post_dev_phase"   # injection point (see below)
role = "review"            # which adapter runs it: dev | review
prompt = "Update CHANGELOG.md for story {story_key} if it introduced user-facing changes."
blocking = false           # true: a non-completed session defers the unit
```

- **Injection stages** are deliberately limited to where the unit's worktree is
  live and the dev/review work is on disk: **`post_dev_phase`** (right after dev
  lands), **`post_review_result`** (after a review verdict — fires only when the
  orchestrator review loop actually runs; `review.trigger = "recommended"` skips
  it on stories whose dev session recommends no follow-up), and
  **`pre_commit_gate`** (unconditionally just before every commit — review, skip,
  and budget-rescue paths alike — so a session there evaluates the exact tree
  about to commit). `pre_commit_gate` is **defer-safe**: it fires before the unit
  enters COMMITTING, so a _blocking_ workflow whose session doesn't complete
  still defers cleanly. Other stages lack a worktree or run after teardown.
- **`prompt`** expands `{story_key}`, `{run_id}`, and `{scripts}`.
- **The orchestrator appends two sections to what you write**, and appends them
  _after_ the session gates fire, so a `pre_workflow_session` / `pre_session`
  prompt rewrite cannot strip them: a **`## Sprint board`** prohibition — the
  same one dev and review prompts carry, because a workflow session runs while
  the orchestrator's own `sprint-status.yaml` advance is still uncommitted and
  must not be "fixed" or reverted (#437) — and the **`## Completion signal`**
  contract naming the marker file the session must write before ending its turn.
  Write `prompt` as a self-contained instruction; do not restate either. (Stories
  mode has no board, so the first section is absent there.)
- The injected session is a **first-class session**: it fires `pre_workflow_session`
  → `pre_session` → `post_session`, is recorded on the task, and counts toward the
  token budget. Its journal entries are `workflow-start` / `workflow-end`.
- **`blocking`**: a blocking workflow whose session doesn't complete **defers the
  unit** (through the existing defer primitive). A non-blocking one is advisory.
- A workflow is **trust-gated** with the rest of its plugin. A project plugin's
  workflow fires only once the plugin is granted, and its `{scripts}` resolves to
  the verified snapshot. A builtin `[python]` plugin's workflow fires only when the
  plugin is in `[plugins] enabled`. See [Trust & safety](#trust--safety).

`registry.provided_workflows()` lists declared workflow names for introspection.

### Making a workflow configurable

The `blocking` and (in effect) on/off state a workflow declares in its manifest
are **defaults**. A plugin can let an operator tune them per run — disable a step
or flip its gate — by declaring settings that follow a naming convention. No
Python is required; the registry reads the resolved settings when it injects.

The convention, keyed on the workflow's `name`:

| Setting key       | Type | Effect                                                              |
| ----------------- | ---- | ------------------------------------------------------------------- |
| `<name>_enabled`  | bool | When explicitly `false`, the step is **dropped** — no session runs. |
| `<name>_blocking` | bool | **Overrides** the manifest's `blocking` flag for that workflow.     |

**Default semantics — absent settings change nothing.** The overlay only acts on
a setting that is present and (for `_enabled`) explicitly `false`; `_blocking`
falls back to the manifest value when unset. A plugin that declares none of these
settings is **byte-identical** to one written before the feature existed.

**Declare the matching `[[settings]]`** so the keys are first-class (typed,
documented, surfaced in the settings UI) and operators can flip them from
`[plugins.<plugin>]` in `policy.toml`:

```toml
# A gate step: generated advisory by default, an operator can make it block.
[workflows.nfr]
stage = "pre_commit_gate"
role = "review"
prompt = "Run the NFR assessment for the changes in {story_key}."
blocking = false              # manifest default: advisory

[[settings]]
key = "nfr_enabled"           # <name>_enabled  -> drop the step when false
type = "bool"
default = true
help = "Run the NFR workflow after review."

[[settings]]
key = "nfr_blocking"          # <name>_blocking -> override the blocking flag
type = "bool"
default = false
help = "Escalate the unit when the NFR gate is not satisfied."
```

An operator then opts in from policy:

```toml
[plugins.tea]
nfr_blocking = true           # flip the advisory gate to blocking
td_enabled   = false          # turn the test-design step off entirely
```

**Interaction with the blocking / defer path.** `<name>_blocking` feeds the same
`WorkflowSpec.blocking` the engine already honors: a blocking workflow whose
session does not **complete** defers the unit through the existing defer
primitive (see [Workflows](#workflows-provides) above). The overlay only changes
which value that flag holds at injection time — it adds no new control flow. (For
quality gating on a workflow's _output_ rather than its completion, do that in an
in-process `on_pre_commit` hook; the manifest `blocking` flag only checks session
completion.)

**Disabling every step at a stage is free.** When a setting turns off the last
remaining workflow at a stage, that stage drops out of `registry.workflow_stages()`
too, so the engine's O(1) per-stage injection guard skips it entirely — the same
as if no workflow had ever been declared there.

---

## Worked walkthrough: the `guardrails` plugin

The repo ships a complete example under
[`examples/plugins/guardrails/`](../examples/plugins/guardrails/) that exercises a
setting, an observe hook, a veto gate, a commit-message mutation, and a provided
workflow — in ~40 lines of Python plus a manifest. Build it yourself:

**1. Make the folder.** In your project:

```text
.bmad-loop/plugins/guardrails/
  plugin.toml
  guardrails.py
```

**2. Write the manifest** (`plugin.toml`): metadata, a `[python]` module, two
`[[settings]]` (`trailer`, `forbid_epic`), and one `[workflows.doc-sync]` bound to
`post_dev_phase`. Copy it from the example.

**3. Write the module** (`guardrails.py`):

```python
from bmad_loop.plugins import Plugin

class GuardrailsPlugin(Plugin):
    fail_closed = False

    def on_pre_story(self, ctx):
        # observe: count stories in the cross-stage shared dict
        ctx.shared["stories_seen"] = ctx.shared.get("stories_seen", 0) + 1

    def on_pre_dev_phase(self, ctx):
        # gate: skip a "parked" epic
        parked = int(self.settings.get("forbid_epic") or 0)
        if parked and ctx.epic == parked:
            ctx.veto("skip", f"epic {parked} is parked")

    def on_pre_commit(self, ctx):
        # mutate: append a trailer to the commit message
        trailer = str(self.settings.get("trailer") or "").strip()
        if trailer and trailer not in (ctx.proposed_commit_message or ""):
            base = (ctx.proposed_commit_message or "").rstrip()
            ctx.proposed_commit_message = f"{base}\n\n{trailer}" if base else trailer
```

**4. Configure** in `.bmad-loop/policy.toml`:

```toml
[plugins.guardrails]
trailer = "Automated-by: bmad-loop"
forbid_epic = 0           # set to an epic number to park it
```

**5. Grant it.** Review the folder, then record its content grant:

```console
$ bmad-loop plugins enable guardrails
plugin "guardrails" granted: sha256:<12 hex> (2 files) in <state root>/<project-tag>/plugin-grants.json
```

Do **not** add `guardrails` to `[plugins] enabled`: that list trusts builtins, and
a project plugin named there without a grant fails startup. The settings live in
`policy.toml`, outside the plugin folder, so changing them never invalidates the
grant. Editing `plugin.toml` or `guardrails.py` does: the next launch stops until
you run `bmad-loop plugins accept guardrails`.

**6. Run.** On the next `bmad-loop run`:

- the settings TUI shows a **guardrails** section with `trailer` + `forbid_epic`;
- each story increments `stories_seen` in the run's `plugin_shared`;
- a story in the parked epic is **skipped** before its dev session;
- after dev lands, the **doc-sync** workflow runs an extra review-role session;
- each commit message gets the **trailer** appended.

Run `bmad-loop plugins revoke guardrails`, and the plugin goes completely inert:
the next launch prints a `note:` and journals `plugin-refused` — proof of the
trust gate.

---

## Platform portability

bmad-loop's core is portable Python, and the OS-specific work is quarantined behind
seams (the tmux transport, the `ProcessHost` lifecycle, the hook interpreter — see
[Porting bmad-loop to a new OS](porting-to-a-new-os.md)). Plugin **helper scripts**
are spawned under the orchestrator's own interpreter (`sys.executable`), **not** a
PATH-resolved `python3` — so a bundled script may `import bmad_loop` and reach those
seams instead of re-implementing OS primitives. Follow this discipline:

- **Use the `ProcessHost` seam for pid lifecycle.** For terminate / force-kill /
  liveness, call `get_process_host()` rather than guarding `os.kill` /
  `signal.SIGKILL` / `taskkill` behind your own `sys.platform` branch:

  ```python
  from bmad_loop.process_host import get_process_host

  host = get_process_host()
  host.terminate(pid)
  if host.is_alive(pid):
      host.force_kill(pid)
  ```

  The seam already carries the POSIX and Windows behavior, so the script gains a
  new OS for free when a host registers.

- **Stay dependency-free.** Beyond `bmad_loop` itself, import only the stdlib so the
  script runs anywhere the core does. If you genuinely need a third-party package on
  one platform, make it an **optional extra** in `pyproject.toml` and **import it
  lazily** with a clear error if missing — never at module top level. The bundled
  Unity plugin does exactly this for `psutil`: imported only on non-Linux process
  _discovery_ (a core dependency on Windows, the `non-linux` extra on macOS), so the
  dep-free Linux/WSL path never pulls it in.

- **Guard the primitives that have no seam.** Anything absent or differently-shaped
  on Windows that isn't behind a seam — `cp`/`--reflink`, symlinks, `/proc` scanning,
  `/tmp`, `start_new_session` — still needs a fallback behind a `sys.platform`
  branch, with a `# portability: <reason>` comment so the intent (and the CI
  portability guard, `tests/test_portability_guard.py`) stays honest. Keep the Linux
  fast path byte-identical; the Windows branch can be best-effort and is not
  exercised until a native-Windows backend ships. WSL **is** Linux, so it takes the
  fast path unchanged.

The Unity plugin's `unity_teardown.py` is the worked example: it **delegates** its
SIGTERM→SIGKILL sweep of leaked Editor / MCP-server processes to
`get_process_host()` (so it inherits Windows behavior), while still doing its own
worktree-bound process _discovery_ via the guarded `/proc`-with-psutil-fallback
scan. See it alongside `unity_setup.py` / `unity_cleanup.py` (and the
[Game Engine plugin guide](game-engine-plugin-guide.md)) for both shapes.

## Reference

- Model + base class: `src/bmad_loop/plugins/model.py`
- Manifest parser: `src/bmad_loop/plugins/manifest.py`
- Discovery + overlay + api-check: `src/bmad_loop/plugins/loader.py`
- Trust gate: `src/bmad_loop/plugins/trust.py` (refusals),
  `src/bmad_loop/plugins/grants.py` (the verdict),
  `src/bmad_loop/plugins/digest.py` (directory hashing),
  `src/bmad_loop/plugins/store.py` (the out-of-tree grant store),
  `src/bmad_loop/plugins/snapshot.py` (verified snapshots)
- Registry (the inter-pillar contract): `src/bmad_loop/plugins/registry.py`
- Hook bus + dispatch: `src/bmad_loop/plugins/bus.py`
- Hook context + veto: `src/bmad_loop/plugins/context.py`
- Settings schema: `src/bmad_loop/settings_schema.py`
- The bundled Unity engine plugin (a real, complex example):
  `src/bmad_loop/data/plugins/unity/`
- Game-engine specifics: [Game Engine plugin guide](game-engine-plugin-guide.md)
