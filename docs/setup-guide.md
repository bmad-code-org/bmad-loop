# Setup guide

This module is two things: the bundled `bmad-loop-*` skills and the `bmad-loop`
orchestrator tool (the Python program that actually drives the loop). The skills do
nothing on their own — the orchestrator is what spawns the fresh coding-CLI sessions
that invoke the upstream `bmad-dev-auto` skill (which implements and, re-invoked on
the done spec, runs the follow-up review) plus `bmad-loop-sweep` and
`bmad-loop-resolve`, watches their hook signals, and verifies
their artifacts. Installing the tool is part of setup, not
an optional extra.

There are two ways the skills land in a project. The orchestrator's wheel **bundles**
the three skills, so the simplest path is **pip + `bmad-loop init`**, which installs them
itself. Alternatively the **BMAD-method installer** copies them. Either way the
`/bmad-loop-setup` skill registers the `_bmad/` config, ensures the tool is installed,
picks which coding CLIs to drive, and bootstraps the project. For the one-page summary,
see the [Installing the skill module](../README.md#installing-the-skill-module) section
of the README.

## Platform prerequisites

- **Python 3.11+** and a supported coding CLI (`claude` by default).
- **tmux** — the orchestrator drives agent sessions through a terminal multiplexer, and
  tmux is the only backend shipped today. It is required for launching, attaching, and
  driving runs (pure TUI observation works without it). The multiplexer sits behind a
  pluggable seam (`TerminalMultiplexer`), so a native-Windows backend can be added later
  without changing the engine — contributors should start with
  [Porting bmad-loop to a new OS](porting-to-a-new-os.md).
- **OS** — Linux or macOS. **Windows is supported via WSL**, which _is_ Linux: tmux and
  every POSIX path work unchanged there, so no special setup is needed. **Native Windows
  is not yet shipped** — it awaits a non-tmux multiplexer backend (tracked in
  [the roadmap](ROADMAP.md#native-windows-multiplexer-backend); the port path is in
  [Porting bmad-loop to a new OS](porting-to-a-new-os.md)).

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

1. Merges the module's config into `_bmad/config.yaml` (+ personal settings into the
   gitignored `_bmad/config.user.yaml`) and registers its help entries in
   `_bmad/module-help.csv`.
2. Installs **or upgrades** the `bmad-loop` tool from Git (see
   [Installing the tool and TUI](#installing-the-tool-and-tui)). On an upgrade it runs
   `uv tool upgrade bmad-loop --reinstall`.
3. Asks **which coding CLI(s)** the orchestrator should drive, then runs `bmad-loop init`
   to install the `bmad-loop-*` skills + register hooks + write the `.bmad-loop/policy.toml`
   template + add gitignore entries (see [Choosing which CLIs to drive](#choosing-which-clis-to-drive)
   and [Initializing CLIs other than claude](#initializing-clis-other-than-claude)). On an
   upgrade it passes `--force-skills` so the per-project skill copies are refreshed.
4. Runs `bmad-loop validate` as a preflight (see [Verify](#verify)).
5. Cleans up the legacy installer package directories under `_bmad/`, leaving only config.

Run `/bmad-loop-setup` with plain prompts if you want to choose interactively — e.g.
`claude "/bmad-loop-setup cli: claude, codex"` to preselect the CLIs.

## Manual install (repo clone / dev setup)

If you are working from a clone of this repo, sync the project env and let
`bmad-loop init` lay down the skills (the canonical skills live at
`src/bmad_loop/data/skills/` and are bundled into the package):

```bash
uv sync --extra tui                                             # the orchestrator tool + TUI
uv run bmad-loop init --project /path/to/project --cli claude   # installs skills + hooks + policy
claude "/bmad-loop-setup accept all defaults"                   # register _bmad/ config + help
```

Add `--cli codex --cli gemini` to also populate `.agents/skills/`. `init` always
installs all the bundled skills together (`bmad-loop-resolve`, `bmad-loop-sweep`,
`bmad-loop-setup`); `bmad-loop-sweep` owns the canonical `deferred-work-format.md`
the orchestrator normalizes the ledger to. The dev primitive `bmad-dev-auto` is
**not** bundled: it is the upstream skill the orchestrator drives (for both
implementation and the follow-up review), installed by the BMad Method (bmm)
module. `bmad-loop validate` checks it — plus the two adversarial review hunters it
invokes inline — are present before a run starts.

## Choosing which CLIs to drive

The three supported adapters are `claude` (the default), `codex`, and `gemini`. You can
pick more than one — register every CLI you intend to use for dev, review, or sweep triage.

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
be exactly `claude`, `codex`, `gemini`, or `copilot` — `init` errors on an unknown profile and
lists the valid ones.

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

### Skill location

`claude` reads skills from `.claude/skills/`; `codex` and `gemini` read from `.agents/skills/`.
`init` installs the bundled `bmad-loop-*` skills into the right tree for each CLI you pass via
`--cli`, so selecting `codex`/`gemini` populates `.agents/skills/` automatically. It skips skill
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

Two paths overlap: every project does steps 1–5 and 7; only projects set up through the
**BMAD-method installer** (i.e. that ran `/bmad-loop-setup`) also need step 6.

### 1. Reclaim run disk first

Tear down any leftover worktrees, tmux sessions, and archived runs while the tool is still
installed — once `.bmad-loop/` is gone these are unreachable:

```bash
bmad-loop clean --hard --project <project-root>   # delete every concluded run + its worktrees
bmad-loop cleanup --project <project-root>         # kill leftover tmux sessions/windows
```

`clean --hard` permanently deletes runs instead of archiving them (we're removing the tool, so
there's nothing to keep). See the disk-reclamation coverage in
[docs/FEATURES.md](FEATURES.md) and the [command reference](../README.md#command-reference) for
what each command touches. Make sure no run is still live (Editor open, session attached) first.

### 2. Remove the orchestrator state

Delete the `.bmad-loop/` directory. This removes the hook relay script
(`.bmad-loop/bmad_loop_hook.py`), the `policy.toml` template, and all per-run state
(`runs/`, `cache/`, `archive/`) in one step:

```bash
rm -rf .bmad-loop/
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

`init` appended `.bmad-loop/runs/` and `.bmad-loop/cache/` to `.gitignore`. Remove those two
lines (skip any your project relies on for other reasons).

### 6. Unregister from `_bmad/` (BMAD-installer projects only)

If you ran `/bmad-loop-setup`, it registered the module in your BMAD config. Remove the
bmad-loop (`bmad-loop`) entries from:

- `_bmad/config.yaml` and `_bmad/config.user.yaml` — drop the bmad-loop module config block
- `_bmad/module-help.csv` — drop the bmad-loop help rows

uv + `init`-only projects never write to `_bmad/` and can skip this step.

### 7. Uninstall the tool

```bash
uv tool uninstall bmad-loop
```

Confirm removal: `bmad-loop --version` should now fail with a command-not-found error. Repeat
steps 1–6 in every project that used the tool — the tool install is machine-wide, but the
state, skills, and hooks are per-project.
