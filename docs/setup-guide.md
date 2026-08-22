# Setup guide

This module is two things: the bundled `bmad-loop-*` skills and the `bmad-loop`
orchestrator tool (the Python program that actually drives the loop). The skills do
nothing on their own — the orchestrator is what spawns the fresh coding-CLI sessions
that invoke the upstream `bmad-build-auto` skill (which implements and, re-invoked on
the done spec, runs the follow-up review) plus `bmad-loop-sweep` and
`bmad-loop-resolve`, watches their hook signals, and verifies
their artifacts. Installing the tool is part of setup, not
an optional extra.

There are two ways the skills land in a project. The orchestrator's wheel **bundles**
the three skills, so the simplest path is **pip + `bmad-loop init`**, which installs them
itself. Alternatively the **BMAD-method installer** copies them. Either way the
`/bmad-loop-setup` skill installs the orchestrator tool, picks which coding CLIs to
drive, and bootstraps the project. For the one-page summary,
see the [Installing the skill module](../README.md#installing-the-skill-module) section
of the README.

## Platform prerequisites

- **Python 3.11+** and a supported coding CLI (`claude` by default).
- **git 2.34 or newer** — every unit of work moves through git, so this one is enforced
  rather than advised: `bmad-loop run`, `sweep` and `resume` refuse to start below it, and
  `bmad-loop validate` reports it as a problem (`git.version`, exit 1). It is a **support**
  floor rather than a capability one — no git command bmad-loop issues needs 2.34 — set so
  the project stops carrying accommodations for releases it does not test against. 2.34 is
  Ubuntu 22.04 LTS's stock git; Ubuntu 20.04 (2.25) and Debian 11 (2.30) are below it.
  `git --version` reports what is on your `PATH`.
- **A terminal multiplexer** — the orchestrator drives agent sessions through a terminal
  multiplexer: **tmux** (POSIX) and the experimental **psmux** (native Windows) ship
  bundled, and further backends install as separate packages that register themselves
  (e.g. the [herdr adapter](https://github.com/pbean/bmad-loop-adapter-herdr), then
  `bmad-loop mux set herdr` — see
  [Terminal multiplexer backends](multiplexer-backends.md)). A backend is required for
  launching, attaching, and driving runs (pure TUI observation works without one). The
  multiplexer sits behind a pluggable seam (`TerminalMultiplexer`), so a new backend slots
  in without changing the engine — contributors should start with
  [Porting bmad-loop to a new OS](porting-to-a-new-os.md). `bmad-loop mux` lists the
  registered backends and shows which is selected (and why); `bmad-loop mux set <name>` —
  or the `[mux] backend` policy key, or the `BMAD_LOOP_MUX_BACKEND` env var — forces the
  choice per machine.
- **OS** — Linux or macOS. **Windows is supported via WSL**, which _is_ Linux: tmux and
  every POSIX path work unchanged there, so no special setup is needed. **Native Windows is
  experimental** — the bundled `psmux` backend (a ConPTY tmux re-implementation) drives runs
  there and is selected automatically as the win32 default when its prerequisites are present:
  the `psmux` and `pwsh` (PowerShell) binaries on `PATH`, with `psmux` 3.3.8 or newer (older
  releases read as unavailable — see
  [multiplexer backends](multiplexer-backends.md#psmux-native-windows-experimental) for why). It is
  not yet at the Linux/macOS/WSL support tier — the remaining native-Windows work (window
  hosting, attach/detach, Unity cache paths) is tracked in
  [the roadmap](ROADMAP.md#native-windows-multiplexer-backend); the port path is in
  [Porting bmad-loop to a new OS](porting-to-a-new-os.md). Inside WSL, install with the
  **Linux** interpreter — a Windows-installed bmad-loop is reachable from the bash prompt
  and silently behaves as Windows
  ([why](multiplexer-backends.md#psmux-native-windows-experimental)). To check:
  `bmad-loop mux` should read `platform default for linux`; if it names `win32` you are
  running the Windows build, and `bmad-loop validate` says so too. (A forced choice —
  `BMAD_LOOP_MUX_BACKEND` or `[mux] backend` — is reported as the reason _instead_ of the
  platform, so clear it before reading that line as a platform check.)

## Installed via the BMAD-method installer? (recommended)

The BMAD-method installer copies the three `bmad-loop-*` skill directories
(`bmad-loop-setup`, `bmad-loop-sweep`, `bmad-loop-resolve`) into your project. It does **not** carry the orchestrator tool — the installer copies only skill
directories, not their sibling files, so the tool can't ride along in the skill folder.
It is installed separately from Git by the setup skill. The canonical source is
<https://github.com/bmad-code-org/bmad-loop>. (Going the other way, the tool's wheel
bundles the skills, so `bmad-loop init` can install them without the BMAD installer —
when the installer already placed them, `init` simply skips the existing copies.)

After the installer runs, complete setup with one command:

```bash
claude "/bmad-loop-setup accept all defaults"
```

`/bmad-loop-setup` handles both first-time setup and later upgrades — re-run it any time. It:

1. Installs **or upgrades** the `bmad-loop` tool from Git (see
   [Installing the tool and TUI](#installing-the-tool-and-tui)). On an upgrade it runs
   `uv tool upgrade bmad-loop --reinstall`.
2. Asks **which coding CLI(s)** the orchestrator should drive, then runs `bmad-loop init`
   to install the `bmad-loop-*` skills + register hooks + write the `.bmad-loop/policy.toml`
   template + add gitignore entries (including policy.toml itself — policy is per-machine; repos initialized before this run `git rm --cached .bmad-loop/policy.toml` once if theirs is already committed) (see [Choosing which CLIs to drive](#choosing-which-clis-to-drive)
   and [Initializing CLIs other than claude](#initializing-clis-other-than-claude)). On an
   upgrade it passes `--force-skills` so the per-project skill copies are refreshed.
3. Runs `bmad-loop validate` as a preflight (see [Verify](#verify)).
4. Refreshes `_bmad/bmad-loop/module-help.csv`, the module's help entries. That is the
   only file it writes under `_bmad/` — module registration, the central `config.toml`,
   and the `/bmad-help` catalog are owned by the BMAD installer, which regenerates them
   on every run.

Run `/bmad-loop-setup` with plain prompts if you want to choose interactively — e.g.
`claude "/bmad-loop-setup cli: claude, codex"` to preselect the CLIs.

## Manual install (repo clone / dev setup)

If you are working from a clone of this repo, sync the project env and let
`bmad-loop init` lay down the skills (the canonical skills live at
`src/bmad_loop/data/skills/` and are bundled into the package):

```bash
uv sync --extra tui                                             # the orchestrator tool + TUI
uv run bmad-loop init --project /path/to/project --cli claude   # installs skills + hooks + policy
claude "/bmad-loop-setup accept all defaults"                   # install the tool + wire the project
```

Add `--cli codex --cli gemini` to also populate `.agents/skills/`. `init` always
installs all the bundled skills together (`bmad-loop-resolve`, `bmad-loop-sweep`,
`bmad-loop-setup`); `bmad-loop-sweep` owns the canonical `deferred-work-format.md`
the orchestrator normalizes the ledger to. The dev primitive `bmad-build-auto`
(spelled `bmad-dev-auto` before the 6.10.1 rename — the orchestrator resolves
whichever name is on disk and invokes that one) is
**not** bundled: it is the upstream skill the orchestrator drives (for both
implementation and the follow-up review), installed by the BMad Method (bmm)
module. `bmad-loop validate` checks it — plus any review skills its layers
invoke — are present before a run starts.

**Minimum BMAD-METHOD: v6.10.0** for sprint mode. Beyond the primitive itself (with
its `customize.toml`), `bmad-loop validate` holds you to no fixed list of review
skills: it reads the primitive you actually have installed and requires the
reviewers that copy will actually invoke — which on current sources is none at all.

- **Layer-driven (post-6.10.0)** — `customize.toml` defines
  `[[workflow.review_layers]]`, and a layer's `instruction` may name a skill to hand
  off to. Current (6.11) sources define four layers — `blind-hunter`,
  `edge-case-hunter`, `verification-gap` and `intent-alignment` — and none of them
  invokes a skill: each is a self-contained prompt — `edge-case-hunter` and
  `verification-gap` read the primitive's own `review-prompts/*.md`, `blind-hunter`
  and `intent-alignment` carry their prompt inline. Such a tree requires no review
  skill at all. An intermediate release whose layers invoke the merged `bmad-review`
  with one lens each needs `bmad-review`; an earlier one naming the standalone hunters
  needs exactly those instead.
- **v6.10.0** — no `review_layers` section at all; `step-04-review.md` names
  `bmad-review-adversarial-general` and `bmad-review-edge-case-hunter` inline, so those
  two are what is required.

A layer disabled with an empty `instruction`, or replaced in
`_bmad/custom/bmad-build-auto.toml` by a recipe that runs something else, requires
nothing. If the shape cannot be read at all, `validate` falls back to requiring the two
v6.10.0 hunters — which a tree carrying `bmad-review` satisfies.

`bmad-review-verification-gap` is no longer required unconditionally (#260): no tagged
release ships it as a standalone skill, and on current sources it is only a thin
forwarder to `bmad-review`. It is required exactly when your installed layers name it.

### Customizing the review layers

Your project overrides in `_bmad/custom/bmad-build-auto.toml` (team) and
`_bmad/custom/bmad-build-auto.user.toml` (personal, gitignored by the bmm installer) are
applied before the requirement is computed. (Overrides are keyed by skill directory
name, so a pre-6.10.1 install spells both files `bmad-dev-auto*.toml`; the deprecated
shim offers to rename them when you first invoke the old name.) The merge follows the
same rules BMAD's own resolver uses: an array of tables merges by key only when
_every_ entry — default and
override alike — carries the same `code` or `id`; otherwise the override appends. So
adding a layer with a new `id` adds a requirement, and replacing one by `id` moves it.

Two things `validate` reports but never blocks on, because neither can be decided
without running the review:

- a layer gated by a `when` condition, which the model evaluates against the diff;
- a layer naming a skill in a phrasing that is not recognizably a handoff (the
  convention is ``Invoke the `skill-name` skill``).

Both come back as `skills.review-layer-unresolved` warnings, so a customized layer can
never block a run the way the old fixed catalog did. What _is_ a problem: every layer
disabled at once (`skills.review-layers-empty`), which makes the primitive HALT
blocked with `no active review layers`.

Under `isolation = "worktree"` the skills your layers name are copied into each unit's
worktree along with `_bmad/custom/`, so an isolated run resolves the same layers
`validate` checked — including from the gitignored `*.user.toml`, which a fresh checkout
would not carry.

## Choosing which CLIs to drive

The supported adapters are `claude` (the default), `codex`, `gemini`, `copilot`,
`antigravity` (Google's `agy`, experimental — `isolation = "none"` only), and `opencode`
(OpenCode ≥ 1.18 over HTTP/SSE, profile `opencode-http` — no tmux window; needs the
`bmad-loop[opencode]` extra and `model` set as `provider/model`). You can pick more
than one — register every CLI you intend to use for dev, review, or sweep triage.

There are **two layers** here, and confusing them is the usual stumbling block:

- `bmad-loop init --cli <name>` registers the orchestrator's **hooks** for that CLI. Without
  registered hooks, a CLI can't signal the engine.
- `.bmad-loop/policy.toml` `[adapter]` selects which CLI actually **runs** each stage.

So a mixed setup — say `claude` for dev and `codex` for review — needs _both_: the hooks
registered for each CLI (`--cli claude --cli codex`) **and** the role pointed at that CLI in
`policy.toml`:

```toml
[adapter]
name = "claude"        # default for every stage

[adapter.review]
name = "codex"         # the review pass runs on codex instead
```

Any CLI named in `policy.toml` must also have been registered with `--cli`. To add one later,
re-run `bmad-loop init --cli <name>`. If you only use a single CLI, leave `policy.toml`
untouched — the default is correct.

## Installing the tool and TUI

The `[tui]` extra pulls in the Textual dashboard (`textual` + `tomlkit` + `pyte`) so
`bmad-loop tui` works. The core tool needs only `pyyaml`.

**Together (recommended):**

```bash
uv tool install "bmad-loop[tui] @ git+https://github.com/bmad-code-org/bmad-loop.git"
```

**Tool first, TUI later (separately):** install the core without the extra, then add the
dashboard whenever you want it by re-running the same command **with** `[tui]`:

```bash
# core tool only
uv tool install "bmad-loop @ git+https://github.com/bmad-code-org/bmad-loop.git"

# add the TUI later — re-run with the extra (uv upgrades the install in place)
uv tool install --upgrade "bmad-loop[tui] @ git+https://github.com/bmad-code-org/bmad-loop.git"
```

Until the extra is present, `bmad-loop tui` prints a clear error
(`the TUI requires optional dependencies — uv tool install 'bmad-loop[tui]'`) rather than
failing obscurely.

`uv tool install` drops `bmad-loop` into uv's own managed tool environment, so there's no
PEP 668 externally-managed conflict and no need for a virtualenv, `--user`, or `--break-system-packages`.

To upgrade later, the simplest path is to re-run `/bmad-loop-setup` (or `/bmad-loop-setup
upgrade`) — it detects the existing install, upgrades the tool with `uv tool upgrade
bmad-loop --reinstall` (the `--reinstall` is **required** for a git source — a plain
`uv tool upgrade` reuses the cached commit and won't pull new code), and re-lays the
per-project skills with `bmad-loop init --force-skills`. To do it by hand, run those two
commands yourself (see the [Upgrading](../README.md#upgrading) section of the README).

Confirm with `bmad-loop --version`.

## Initializing CLIs other than claude

`bmad-loop init` registers hooks and installs the bundled `bmad-loop-*` skills per CLI. The
`--cli` flag is repeatable — pass it once per CLI you want to drive:

```bash
# claude only (default)
bmad-loop init --project <project-root> --cli claude

# multiple, e.g. claude + codex + gemini
bmad-loop init --project <project-root> --cli claude --cli codex --cli gemini
```

Run with no `--cli` and `init` registers hooks for every CLI the `policy.toml` references,
so a dual-client setup that's already configured in policy needs no extra flags. Names must
be exactly `claude`, `codex`, `gemini`, `copilot`, `antigravity`, or `opencode-http` (alias
`opencode`) — `init` errors on an unknown profile and lists the valid ones. A hookless
profile like `opencode-http` installs its skills but registers no hooks (it signals over
HTTP/SSE).

### First-run notes

Each CLI needs a one-time interactive setup before the first `bmad-loop run`, because
spawned sessions can't answer first-run dialogs. `init` prints the relevant notes; relay
them to whoever owns the machine:

- **claude** — run `claude` once in the project and accept the workspace-trust + hooks-approval
  dialogs.
- **codex** — run `codex` once in the project and accept **both** prompts: workspace trust,
  then "Hooks need review → Trust all and continue" (untrusted hooks silently never fire).
  Requires Codex ≥ 0.139.
- **gemini** — authenticate once (browser OAuth or `GEMINI_API_KEY`). Requires Gemini CLI
  ≥ 0.46.
- **copilot** — run `copilot` once in the project and authenticate (`gh` / a Copilot
  subscription). Requires the Copilot **CLI** GA (≥ 2026-02) — _not_ the VS Code extension.
  **Pin a capable model**: the free default (GPT-5 mini) silently skips steps in the
  multi-step dev/review skills; set `[adapter] model = "claude-sonnet-4-6"` (→ `--model`).
- **antigravity** — run `agy` once in the project, authenticate, and answer
  **"Yes, I trust this folder"** before `bmad-loop run`; spawned sessions can't answer that
  dialog, and a pending one reads as a session timeout. Requires Antigravity CLI
  (`agy` ≥ 1.1.3). Two limitations to know, both verified against 1.1.3:
  - **Trust is exact-path.** `settings.json` `trustedWorkspaces` matches whole paths —
    trusting a parent does _not_ cover its subdirectories, and
    `--dangerously-skip-permissions` does not bypass the dialog (it covers tool
    permissions only). So antigravity needs `isolation = "none"` (the default);
    **`isolation = "worktree"` will hang**, because each worktree is a fresh untrusted
    path under `.bmad-loop/runs/`. See [#169](https://github.com/bmad-code-org/bmad-loop/issues/169).
  - **Token usage is not recorded** (`usage_parser = "none"`) — and this is permanent,
    not a gap: agy's transcript carries no usage data at all (it counts tokens only in
    an internal SQLite/protobuf store). Runs work; the token columns stay empty.
- **opencode** — install the HTTP client extra (`pip install 'bmad-loop[opencode]'`) and
  authenticate once, **globally**, with `opencode auth login` (not per-project — there is no
  workspace-trust dialog to answer). Requires OpenCode ≥ 1.18. Set the model as
  `provider/model` (e.g. `[adapter] model = "anthropic/claude-haiku-4-5"`). No hooks are
  registered — the adapter drives a headless `opencode serve` over HTTP/SSE, so there is no
  tmux window to attach to; watch a session via its `logs/<task-id>.log` — a curated
  transcript of the agent's prose, tool calls, file edits and permission decisions — or the
  TUI Log tab. The server's own stdout lands beside it in `<task-id>.server.out`, and a
  structured trace of the raw SSE frames in `<task-id>.sse.jsonl`.

### Skill location

`claude` reads skills from `.claude/skills/`; `codex`, `gemini`, `copilot`, and `antigravity`
read from `.agents/skills/`. `init` installs the bundled `bmad-loop-*` skills into the right tree
for each CLI you pass via `--cli`, so selecting any of the `.agents/skills/` CLIs populates it automatically. It skips skill
dirs that already exist — pass `--force-skills` to overwrite a stale copy, or `--no-skills` to
manage them yourself.

## Verify

Preflight the project — config, sprint-status, git, tmux, and the coding CLI:

```bash
bmad-loop validate --project <project-root>
```

`validate` exits non-zero when the project isn't fully ready (e.g. no `sprint-status.yaml`
yet, or `bmad-sprint-planning` hasn't run). On a fresh project that is **expected** — read its
output as a readiness checklist, not an install failure.

For the dashboard itself, see [docs/tui-guide.md](tui-guide.md). For the full policy
reference, see the [Policy section](../README.md#policy-bmad-looppolicytoml) of the README.

## Uninstalling

Removing bmad-loop is the inverse of [`bmad-loop init`](#initializing-clis-other-than-claude):
undo what it laid down (the `.bmad-loop/` state, the per-CLI hooks and skills, the gitignore
lines), then uninstall the tool. There is no `bmad-loop uninstall` command — the steps below
are the documented manual procedure. Work **inside the project root**, and reclaim disk
**before** deleting state so no worktrees or archives are orphaned.

Two paths overlap: every project does steps 1–5 and 7; only projects with a `_bmad/` tree
also need step 6.

### 1. Reclaim run disk first

Tear down any leftover worktrees, tmux sessions, and archived runs while the tool is still
installed — once `.bmad-loop/` is gone these are unreachable:

```bash
bmad-loop clean --hard --project <project-root>   # delete every concluded run + its worktrees
bmad-loop cleanup --project <project-root>         # kill leftover tmux sessions/windows
```

`clean --hard` permanently deletes runs instead of archiving them (we're removing the tool, so
there's nothing to keep). Running it **first** also matters for the out-of-tree half: each run's
control-plane directory lives under the user-scoped state root, and `clean` is what collects it
(step 2 covers what is left over). See the disk-reclamation coverage in
[docs/FEATURES.md](FEATURES.md) and the [command reference](../README.md#command-reference) for
what each command touches. Make sure no run is still live (Editor open, session attached) first.

### 2. Remove the orchestrator state

Delete the `.bmad-loop/` directory. This removes the hook relay script
(`.bmad-loop/bmad_loop_hook.py`), the `policy.toml` template, and all per-run state
(`runs/`, `cache/`, `archive/`) in one step:

```bash
rm -rf .bmad-loop/
```

One piece is **not** under `.bmad-loop/`: each run's hook-event channel lives in a user-scoped
state root outside the project (#494) — `$XDG_STATE_HOME/bmad-loop`, else
`~/.local/state/bmad-loop`; `%LOCALAPPDATA%\bmad-loop\state` on Windows; or whatever
`BMAD_LOOP_STATE_DIR` names. Step 1 collected this project's runs from it, leaving an empty
per-project directory whose name is a digest of the project path — not something to pick out by
hand. That root is shared by **every** project on this machine, so delete the whole thing only if
you are removing bmad-loop everywhere:

```bash
# all projects on this machine — same cascade the orchestrator resolves
rm -rf "${BMAD_LOOP_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/bmad-loop}"
```

### 3. Remove the bundled skills

`init` installed the three bundled `bmad-loop-*` skill directories — delete only those. **Leave
the standard BMAD skills alone**; install never touched them.

```bash
# claude reads from .claude/skills/ ; codex/gemini read from .agents/skills/
rm -rf .claude/skills/bmad-loop-{dev,review,resolve,sweep,setup}
rm -rf .agents/skills/bmad-loop-{dev,review,resolve,sweep,setup}
```

(Run only the line for the skill tree your CLIs use — drop the other. The `dev` and `review`
names are retired — current `init` never lays them down — but listing them is a harmless no-op
that also cleans up a pre-0.7.0 install.)

### 4. Deregister the hooks

`init` **merged** its Stop-hook registration into each CLI's existing hook config, so these
files must be **edited, not deleted** (they hold your own settings too). In each config below,
remove the hook entry whose `command` contains `bmad_loop_hook.py`:

- **claude** — `.claude/settings.json`
- **codex** — `.codex/hooks.json`
- **gemini** — `.gemini/settings.json`

Edit only the registered CLIs. The `bmad_loop_hook.py` substring uniquely identifies the
entries to strip; leave every other hook in place.

### 5. Drop the gitignore lines

`init` appended `.bmad-loop/runs/`, `.bmad-loop/cache/`, and `.bmad-loop/policy.toml` to
`.gitignore`. Remove those three lines (skip any your project relies on for other reasons). If
you had run `git rm --cached .bmad-loop/policy.toml` to stop sharing the per-machine policy, the
file is untracked — re-add it (`git add .bmad-loop/policy.toml`) only if you want it back in version control.

### 6. Remove the BMAD module (BMAD-installer projects only)

There is nothing to hand-unregister: bmad-loop writes no BMAD config. If you installed the
module through the BMAD installer, remove it there — the installer owns `_bmad/bmad-loop/`,
the central `config.toml`, and the `/bmad-help` catalog, and regenerates all three on its
next run.

If you only ever ran `/bmad-loop-setup`, delete `_bmad/bmad-loop/module-help.csv` (the one
file it refreshes) — or the whole `_bmad/bmad-loop/` directory if the installer never
created it. uv + `init`-only projects that have no `_bmad/` can skip this step.

### 7. Uninstall the tool

```bash
uv tool uninstall bmad-loop
```

Confirm removal: `bmad-loop --version` should now fail with a command-not-found error. Repeat
steps 1–6 in every project that used the tool — the tool install is machine-wide, but the
state, skills, and hooks are per-project.
