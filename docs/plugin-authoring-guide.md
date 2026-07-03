# Writing a bmad-auto plugin

A **plugin** extends the bmad-auto orchestrator **without touching its core loop**.
It can be as simple as a settings-only data file or as complex as the bundled
Unity game-engine layer. A plugin can:

- **observe, veto, or mutate** the run at every lifecycle stage (a _hook bus_);
- contribute **settings** that render in the settings TUI and persist to policy;
- inject its own **workflow** sessions at defined points in the dev/review cycle.

Plugins are **folder-drop**: a directory with a `plugin.toml` manifest (plus any
helper scripts) dropped under `.automator/plugins/<name>/`. No registration, no
install step. A plugin that ships **in-process Python** is loaded only when you
**trust** it by name — dropping a folder in never runs code.

> Already wrote a [CLI adapter profile](../README.md#other-coding-clis) or the old
> `[engine]` block? Same idea — declarative TOML + optional scripts, discovered and
> overlaid. The plugin system generalizes both.

---

## Quick start: the smallest plugin

A data-only plugin carries one setting and zero behavior. Create
`.automator/plugins/hello/plugin.toml`:

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
help = "Shown in the settings UI once this plugin is enabled."
```

That's a complete, loadable plugin. It has no `[hooks]` and no `[python]`, so it
runs no code and is byte-identical to "no plugin" at run time — it exists only to
contribute a setting. This is the shape the bundled
[`example`](../src/automator/data/plugins/example/plugin.toml) plugin ships in.

A setting-only section appears in the TUI only once the plugin is **enabled**:

```toml
# .automator/policy.toml
[plugins]
enabled = ["hello"]
```

---

## Distribution & discovery

Plugins are discovered from three sources, overlaid in precedence order (a later
same-name plugin **overrides** an earlier one; a new name **extends** the set):

| Source        | Path                                              | Precedence             |
| ------------- | ------------------------------------------------- | ---------------------- |
| Builtin       | `automator/data/plugins/<name>/plugin.toml`       | base                   |
| Entry point   | `bmad_auto.plugins` group                         | _reserved (see below)_ |
| Project-local | `<project>/.automator/plugins/<name>/plugin.toml` | **highest**            |

Each plugin lives in its **own directory** — that directory is the plugin's
`{scripts}` root (see [`{scripts}` substitution](#scripts-substitution)), so its
manifest and helper scripts sit together.

**Entry points are a documented future seam.** `discover()` already yields an
`entry_point` source between builtin and project, but `_discover_entry_points()`
returns nothing today — folder-drop is the only live distribution path. When
pip-installable plugins land, they slot in here with **no change to authors or to
discovery order** (`importlib.metadata` selectable entry points, group
`bmad_auto.plugins`). See `src/automator/plugins/loader.py`.

---

## The manifest (`plugin.toml`)

Every plugin is one `plugin.toml`. Only `[plugin] name` + `api_version` are
required; every section below is optional, so a plugin opts into exactly what it
needs. The manifest parses into the immutable `PluginManifest`
(`src/automator/plugins/model.py`).

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
class = "MyPlugin"         # subclass of automator.plugins.Plugin (default "Plugin")
```

Declaring `[python]` makes the **whole plugin trust-gated**: the module is never
imported unless the plugin is in `[plugins] enabled`. See [Trust](#trust--safety).

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

There are **two trust tiers**, by design:

1. **Declarative tier (always runs).** A data-only or declarative plugin —
   settings + `[hooks.<stage>]` shell commands — loads and runs as soon as it is
   discovered. This is the same risk surface as the old `engine.toml *_cmd` hooks
   or a project's verify commands: operator-authored shell, trusted by virtue of
   living in the repo.

2. **In-process tier (trust-gated).** A plugin that declares a `[python]` module
   is **never imported or executed** unless its `name` is in:

   ```toml
   [plugins]
   enabled = ["my-plugin"]
   ```

   **Dropping a `[python]` plugin folder in never runs its code.** The module
   (and anything it provides that depends on the module — its `on_<stage>`
   handlers, its `validate`, and its provided **workflows**) stays inert until you
   list it. An un-enabled `[python]` plugin is recorded `plugin-untrusted` in the
   run journal.

**Failure isolation.** Every hook — subprocess or Python — is wrapped. A Python
handler that raises is caught (`except Exception` only — `RunStopped`/SIGTERM as
`BaseException` always propagate), journalled `plugin-error`, and the offending
instance is **disabled for the rest of the run**. The run survives. A blocking
declarative hook fails **open** by default (an error lets the run continue; only a
clean non-zero exit vetoes); set `fail_closed = true` to make any failure defer
the unit. An in-process handler can opt into the same by setting `fail_closed =
True` on its class.

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

**Rendering.** Once a plugin is enabled, its settings appear as their own section
in the settings TUI (generated from the schema — see
`src/automator/settings_schema.py`). They persist to a `[plugins.<name>]` table in
`policy.toml`:

```toml
[plugins]
enabled = ["my-plugin"]

[plugins.my-plugin]
strict = true
mode = "b"
```

**Reading a setting.**

- In an **in-process** plugin: `self.settings["strict"]` — the manifest defaults
  overlaid by the operator's `[plugins.<name>]` table.
- In a **declarative** hook: each setting is exported as an environment variable
  `BMAD_AUTO_SETTING_<KEY>` (uppercased), already resolved.
- Anywhere with a `Policy`: `policy.plugin_setting("my-plugin", "strict", default)`.

Settings are **data**, not code — a plugin can carry `[plugins.<name>]` settings
without being in `enabled` (the settings UI just won't surface a disabled plugin's
section). Only the in-process `[python]` module is trust-gated.

---

## Hooks

A hook binds a [stage](#stage-reference) to behavior. The **hook bus**
(`src/automator/plugins/bus.py`) fans each stage out to every bound plugin, in
registry order (`priority`, then load order). A run with no plugin bound to a
stage does no work for it (an O(1) fast-path) — zero-plugin runs stay
byte-identical.

### Declarative hooks

A `[hooks.<stage>]` shell command. The bus runs it with:

- **cwd** = the unit's worktree (or repo root);
- a `BMAD_AUTO_*` environment describing the run:

  | Var                                                                               | Meaning                                       |
  | --------------------------------------------------------------------------------- | --------------------------------------------- |
  | `BMAD_AUTO_STAGE`                                                                 | the stage firing                              |
  | `BMAD_AUTO_RUN_ID` / `BMAD_AUTO_RUN_DIR`                                          | run identity                                  |
  | `BMAD_AUTO_REPO_ROOT` / `BMAD_AUTO_WORKTREE`                                      | git roots                                     |
  | `BMAD_AUTO_STORY_KEY` / `BMAD_AUTO_ROLE` / `BMAD_AUTO_PHASE` / `BMAD_AUTO_BRANCH` | unit context                                  |
  | `BMAD_AUTO_AGENTS`                                                                | comma-separated CLI agent ids in the worktree |
  | `BMAD_AUTO_PLUGIN`                                                                | your plugin's name                            |
  | `BMAD_AUTO_SETTING_<KEY>`                                                         | each resolved setting                         |

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

Subclass `automator.plugins.Plugin` and define `on_<stage>(self, ctx)` methods.
The bus calls the handler for each stage you implement; you only mark the stages
you handle, so the fast path holds for the rest.

```python
from automator.plugins import Plugin

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

Every hook receives one `HookContext` (`src/automator/plugins/context.py`) for the
stage. It carries:

- **Read-only facts** (properties, no setter): `run_id`, `story_key`, `epic`,
  `phase`, `attempt`, `role`, `worktree`, `branch`, `repo_root`, `run_dir`,
  `agents`, `result_json` (a copy), `session_status`, `verify_reason`,
  `decision_action`, `settings`. Observe these; you can never rewrite history.
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

### Story / unit

| Stage                                              | When                                  | Mutable surface                                    |
| -------------------------------------------------- | ------------------------------------- | -------------------------------------------------- |
| `pre_story` / `post_story`                         | around one story                      | veto (`pre_`)                                      |
| `pre_worktree_setup` / `post_worktree_setup`       | around isolated-worktree provisioning | —                                                  |
| `pre_ready_gate` / `post_ready_gate`               | around the engine-ready gate          | veto (`pre_`)                                      |
| `pre_worktree_teardown` / `post_worktree_teardown` | around teardown (in a `finally`)      | **observe-only** — a veto here cannot un-tear-down |
| `pre_integrate`                                    | before integrating a finished unit    | —                                                  |
| `pre_merge` / `post_merge`                         | around the local branch merge         | —                                                  |

### Dev

| Stage                              | When                        | Mutable surface                                                                      |
| ---------------------------------- | --------------------------- | ------------------------------------------------------------------------------------ |
| `pre_dev_phase` / `post_dev_phase` | around the dev attempt loop | veto (`pre_`); `post_dev_phase` is a [workflow injection point](#workflows-provides) |
| `pre_dev_session`                  | before each dev session     | `proposed_prompt`, `proposed_env`, veto                                              |
| `post_dev_verify`                  | after dev verification      | —                                                                                    |

### Review

| Stage                 | When                           | Mutable surface                                   |
| --------------------- | ------------------------------ | ------------------------------------------------- |
| `pre_review_phase`    | before the review loop         | veto                                              |
| `pre_review_session`  | before each review session     | `proposed_prompt`, `proposed_env`, veto           |
| `post_review_session` | after each review session      | —                                                 |
| `post_review_result`  | after a review verdict         | a [workflow injection point](#workflows-provides) |
| `pre_fix_session`     | before a verify-repair session | `proposed_prompt`, `proposed_env`, veto           |

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
- The injected session is a **first-class session**: it fires `pre_workflow_session`
  → `pre_session` → `post_session`, is recorded on the task, and counts toward the
  token budget. Its journal entries are `workflow-start` / `workflow-end`.
- **`blocking`**: a blocking workflow whose session doesn't complete **defers the
  unit** (through the existing defer primitive). A non-blocking one is advisory.
- A workflow from a `[python]` plugin is **trust-gated** along with the module: it
  fires only when the plugin is enabled. A workflow from a pure-declarative plugin
  fires whenever the plugin is discovered.

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
.automator/plugins/guardrails/
  plugin.toml
  guardrails.py
```

**2. Write the manifest** (`plugin.toml`): metadata, a `[python]` module, two
`[[settings]]` (`trailer`, `forbid_epic`), and one `[workflows.doc-sync]` bound to
`post_dev_phase`. Copy it from the example.

**3. Write the module** (`guardrails.py`):

```python
from automator.plugins import Plugin

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

**4. Enable + configure** in `.automator/policy.toml`:

```toml
[plugins]
enabled = ["guardrails"]

[plugins.guardrails]
trailer = "Automated-by: bmad-auto"
forbid_epic = 0           # set to an epic number to park it
```

**5. Run.** On the next `bmad-auto run`:

- the settings TUI shows a **guardrails** section with `trailer` + `forbid_epic`;
- each story increments `stories_seen` in the run's `plugin_shared`;
- a story in the parked epic is **skipped** before its dev session;
- after dev lands, the **doc-sync** workflow runs an extra review-role session;
- each commit message gets the **trailer** appended.

Drop `[python]` from the manifest and `guardrails` from `enabled`, and the plugin
goes completely inert — proof of the trust gate.

---

## Platform portability

bmad-auto's core is portable Python, and the OS-specific work is quarantined behind
seams (the tmux transport, the `ProcessHost` lifecycle, the hook interpreter — see
[Porting bmad-auto to a new OS](porting-to-a-new-os.md)). Plugin **helper scripts**
are spawned under the orchestrator's own interpreter (`sys.executable`), **not** a
PATH-resolved `python3` — so a bundled script may `import automator` and reach those
seams instead of re-implementing OS primitives. Follow this discipline:

- **Use the `ProcessHost` seam for pid lifecycle.** For terminate / force-kill /
  liveness, call `get_process_host()` rather than guarding `os.kill` /
  `signal.SIGKILL` / `taskkill` behind your own `sys.platform` branch:

  ```python
  from automator.process_host import get_process_host

  host = get_process_host()
  host.terminate(pid)
  if host.is_alive(pid):
      host.force_kill(pid)
  ```

  The seam already carries the POSIX and Windows behavior, so the script gains a
  new OS for free when a host registers.

- **Stay dependency-free.** Beyond `automator` itself, import only the stdlib so the
  script runs anywhere the core does. If you genuinely need a third-party package on
  one platform, make it an **optional extra** in `pyproject.toml` and **import it
  lazily** with a clear error if missing — never at module top level. The bundled
  Unity plugin does exactly this for `psutil`: a `non-linux` extra it imports only on
  non-Linux process _discovery_, so the dep-free Linux/WSL path never pulls it in.

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

- Model + base class: `src/automator/plugins/model.py`
- Manifest parser: `src/automator/plugins/manifest.py`
- Discovery + overlay + api-check: `src/automator/plugins/loader.py`
- Trust gate: `src/automator/plugins/trust.py`
- Registry (the inter-pillar contract): `src/automator/plugins/registry.py`
- Hook bus + dispatch: `src/automator/plugins/bus.py`
- Hook context + veto: `src/automator/plugins/context.py`
- Settings schema: `src/automator/settings_schema.py`
- The bundled Unity engine plugin (a real, complex example):
  `src/automator/data/plugins/unity/`
- Game-engine specifics: [Game Engine plugin guide](game-engine-plugin-guide.md)
