# Setup guide

This module is two things: the bundled `bmad-loop-*` skills and the `bmad-loop`
orchestrator tool (the Python program that actually drives the loop). The skills do
nothing on their own — the orchestrator is what spawns the fresh coding-CLI sessions
that invoke the upstream `bmad-build-auto` skill (which implements and, re-invoked on
the done spec, runs the follow-up review) plus `bmad-loop-sweep` and
`bmad-loop-resolve`, watches their hook signals, and verifies
their artifacts. Installing the tool is part of setup, not
an optional extra.

There are three ways the skills land in a project. The orchestrator's wheel **bundles**
the skills, so the simplest path is **`uv tool install` + `bmad-loop init`**, which installs them
itself. On BMAD v7, **`npx skills add` + `bmad setup`** installs them as a module; on
BMAD v6, the **BMAD-method installer** copies them. Either way the
`/bmad-loop-setup` skill installs the orchestrator tool, picks which coding CLIs to
drive, and bootstraps the project. For the one-page summary,
see [Installing and upgrading](../README.md#installing-and-upgrading) in the README.

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
  every POSIX path work unchanged there, so no special setup is needed.
  - **Native Windows is experimental.** The bundled `psmux` backend (a ConPTY tmux
    re-implementation) drives runs there and is selected automatically as the win32 default
    when its prerequisites are present: the `psmux` and `pwsh` (PowerShell) binaries on
    `PATH`, with `psmux` 3.4.0 or newer (older releases read as unavailable — see
    [multiplexer backends](multiplexer-backends.md#psmux-native-windows-experimental) for why).
  - It is below the Linux/macOS/WSL support tier; open native-Windows work is in
    [the roadmap](ROADMAP.md#native-windows-multiplexer-backend), and the port path is in
    [Porting bmad-loop to a new OS](porting-to-a-new-os.md). `bmad-loop stop` works there too:
    it lodges its request in a control file the engine reads at item boundaries and
    mid-session, with no signal delivery involved.
  - **Inside WSL, install with the Linux interpreter.** A Windows-installed bmad-loop is
    reachable from the bash prompt and silently behaves as Windows
    ([why](multiplexer-backends.md#psmux-native-windows-experimental)). To check:
    `bmad-loop mux` should read `platform default for linux`; if it names `win32` you are
    running the Windows build, and `bmad-loop validate` says so too. (A forced choice —
    `BMAD_LOOP_MUX_BACKEND` or `[mux] backend` — is reported as the reason _instead_ of the
    platform, so clear it before reading that line as a platform check.)

## Installing on BMAD v7 (`bmad setup`)

BMAD v7 installs modules from `bmod.toml` records. bmad-loop ships one: the
`bmod-bmad-loop` skill folder is the module record (metadata, never invoked), and each
`bmad-loop-*` skill carries a `bmod.toml` naming it. Add the record and its skills
(`npx skills add` asks which skills to install; pick all four), then let `bmad setup`
register the module:

```bash
npx skills add bmad-code-org/bmad-loop
claude "/bmad setup bmad-loop"
claude "/bmad-loop-setup accept all defaults"
```

`bmad setup` reads the record's requirements: the `bmad` skill and `bmad-build-auto`
from the method module at 6.13.0 or newer (a `6.13.0-next` development install counts).
It recommends `bmad-ticket` and `bmad-retrospective`, which tickets mode uses. Its update
check compares the record's version with the one on bmad-loop's `main` branch.
`bmad setup` installs skills only, so `/bmad-loop-setup` still installs the orchestrator
tool, as in the v6 route below.

To drive the initiative's ticket tree, set `[stories] source = "tickets"` and follow the
[ticket-tree mode guide](tickets-guide.md#prerequisites).

## Installed via the BMAD-method installer? (recommended on BMAD v6)

The BMAD-method installer copies the three `bmad-loop-*` skill directories
(`bmad-loop-setup`, `bmad-loop-sweep`, `bmad-loop-resolve`) into your project; `bmad-loop
init` adds the `bmod-bmad-loop` record folder, which v6 ignores. It does **not** carry the orchestrator tool — the installer copies only skill
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
   template + add gitignore entries, including policy.toml itself, which is per-machine (see [Choosing which CLIs to drive](#choosing-which-clis-to-drive)
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
installs all the bundled skills together: `bmad-loop-resolve` (the interactive
`/bmad-loop-resolve <story>` escalation workflow), `bmad-loop-sweep` (automation-only
ledger triage), `bmad-loop-setup` (installs the tool, then runs `init` and
`validate`) and `bmod-bmad-loop` (the BMAD v7 module record, never invoked);
`bmad-loop-sweep` owns the canonical `deferred-work-format.md`
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

`bmad-review-verification-gap` is required only when your installed layers name it: no
tagged release ships it as a standalone skill, and on current sources it is a thin
forwarder to `bmad-review`.

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
never block a run. What _is_ a problem: every layer
disabled at once (`skills.review-layers-empty`), which makes the primitive HALT
blocked with `no active review layers`.

Under `isolation = "worktree"` the skills your layers name are copied into each unit's
worktree along with `_bmad/custom/`, so an isolated run resolves the same layers
`validate` checked — including from the gitignored `*.user.toml`, which a fresh checkout
would not carry.

### Pointing retrospectives at run journals

`bmad-retrospective` reads session logs as evidence for its process lessons: why a
session stalled, what it tried and dropped. It doesn't know where bmad-loop keeps them,
so on an epic bmad-loop drove it finds none and skips that analysis. bmad-loop ships no
override for this. To point the skill at the run records, create
`_bmad/custom/bmad-retrospective.toml` yourself, or `bmad-retrospective.user.toml` for a
personal copy:

```toml
[workflow]
persistent_facts = [
  "Session logs for bmad-loop-driven stories are the run directories .bmad-loop/runs/<run-id>/ under {project-root}. Find this epic's runs by grepping their journal.jsonl files for its story keys.",
  "journal.jsonl holds one JSON event per line: a session-start (task_id, role, story_key, adapter, model) and a session-end (task_id, tokens, and a status of completed, stalled, timeout, crashed, over_budget or aborted) per coding-CLI session, joined on task_id since session-end carries no story_key, plus review results, retries, rollbacks, pauses and escalations.",
  "logs/<task-id>.log is that session's raw terminal capture (escape sequences and repaints included; a readable transcript for the hookless opencode profile). The journal's log_task / log_pos fields name the log file and byte offset each event lines up with. state.json records the run's stories and their outcome.",
  "Runs archived by bmad-loop archive or bmad-loop clean are .bmad-loop/archive/<run-id>.tar.gz and still hold journal.jsonl, state.json and logs/: search inside them too. Record a session-log gap only for runs that were deleted.",
]
```

`persistent_facts` appends to the skill's defaults, so this fact adds to any others you
already set. Don't use a `file:` glob such as `file:.bmad-loop/runs/*/journal.jsonl`
instead: it would load every run's full journal into the retro's context.

The records last only as long as the run directory or its archive. `bmad-loop clean`
compresses finished runs outside the retention window to `.bmad-loop/archive/` (with
`--hard`, or with `[cleanup] archive_old = false`, it deletes them), and `bmad-loop archive`
does the same for a single run. Both the retro and the fact above can still read an
archived run. `delete`, and a `clean` that deletes, remove a run for good.
`clean --keep <run-id>` protects a run for that one invocation only, so pass it every
time or retro the epic before its runs are deleted.

## Choosing which CLIs to drive

The supported adapters are `claude` (the default), `codex`, `gemini`, `copilot`,
`antigravity` (Google's `agy`, experimental — worktree isolation needs the project root trusted; see [First-run notes](#first-run-notes)), and `opencode`
(OpenCode 1.x ≥ 1.18 or 2.x ≥ 2.0.21 over HTTP/SSE, profile `opencode-http` — no tmux
window; needs the `bmad-loop[opencode]` extra and `model` set as `provider/model`). You can pick more
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
`bmad-loop tui` works. The core tool needs only `pyyaml` and `regex` (plus `psutil` on Windows).

**Together (recommended):**

```bash
uv tool install "bmad-loop[tui] @ git+https://github.com/bmad-code-org/bmad-loop.git"
```

**A pinned release tag** (reproducible; recommended for day-to-day use): append the tag to the URL,
as in `...bmad-loop.git@v<version>` (see the [releases](https://github.com/bmad-code-org/bmad-loop/releases) for tags).
Without a tag the install tracks `main`.

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

Confirm with `bmad-loop --version`.

### Upgrading

Upgrading is two steps: the tool, and the per-project skill copies that `init` laid down
(a tool upgrade does not touch them). The simplest path is to re-run the setup skill, which
detects the existing install and runs both steps:

```bash
claude "/bmad-loop-setup upgrade"
```

By hand (non-Claude CLIs, CI, scripts):

```bash
# 1. upgrade the tool; --reinstall is required for a git source, or uv reuses the cached commit
uv tool upgrade bmad-loop --reinstall
#    to move to a newer tag instead, re-install with the new ref:
#    uv tool install --force "bmad-loop[tui] @ git+https://github.com/bmad-code-org/bmad-loop.git@v<version>"

# 2. re-lay the refreshed skills into each project that uses bmad-loop; without --cli this
#    refreshes only the CLIs the policy selects (claude by default), so name every CLI
#    you initialized the project for, e.g.:
bmad-loop init --project /path/to/project --force-skills --cli claude --cli codex
```

`.bmad-loop/policy.toml` is left untouched: new keys are optional and take their defaults.
`init` gitignores policy.toml because it is per-machine; if yours is already committed, run
`git rm --cached .bmad-loop/policy.toml` once to stop sharing it (your local copy is kept).
Read the [CHANGELOG](../CHANGELOG.md) or the
[releases](https://github.com/bmad-code-org/bmad-loop/releases) for what changed between tags.

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
    permissions only). `isolation = "none"` (the default) works once the project root
    is trusted. **`isolation = "worktree"` works too**, as long as the project root is
    trusted: after provisioning each unit worktree, bmad-loop appends its path to
    `~/.gemini/antigravity-cli/settings.json` `trustedWorkspaces` before any session
    launches (journaled `worktree-trust-seeded`). It only ever extends a grant you
    already made — with the root untrusted nothing is written
    (`worktree-trust-unseeded`) and the session will hang on the dialog. Check a path
    with `bmad-loop probe-adapter antigravity --workspace <path>` (exit `0` = trusted).
    **Known limitation:** entries are never removed when a worktree is torn down, so
    `trustedWorkspaces` accumulates one entry per unit — prune stale
    `.bmad-loop/runs/` paths by hand if the list grows.
  - **Token usage is not recorded** (`usage_parser = "none"`) — and this is permanent,
    not a gap: agy's transcript carries no usage data at all (it counts tokens only in
    an internal SQLite/protobuf store). Runs work; the token columns stay empty.
- **opencode** — install the HTTP client extra (`uv tool install "bmad-loop[tui,opencode] @ git+https://github.com/bmad-code-org/bmad-loop.git"`) and
  authenticate once, **globally**, with `opencode auth login` (not per-project — there is no
  workspace-trust dialog to answer). Supports OpenCode 1.x ≥ 1.18 and 2.x ≥ 2.0.21; see
  [OpenCode versions](#opencode-versions). Set the model as
  `provider/model` (e.g. `[adapter] model = "anthropic/claude-haiku-4-5"`). A reasoning
  `effort` (e.g. `[adapter.review] effort = "max"`) is sent as the model variant and is
  opencode-only — the tmux CLIs ignore it. On 2.x the effort must be one of the model's
  variants, or the session refuses to start and names the ones available. No hooks are
  registered — the adapter drives a headless `opencode serve` over HTTP/SSE, so there is no
  tmux window to attach to; watch a session via its `logs/<task-id>.log` — a curated
  transcript of the agent's prose, tool calls, file edits and permission decisions — or the
  TUI Log tab. The server's own stdout lands beside it in `<task-id>.server.out`, and a
  structured trace of the raw SSE frames in `<task-id>.sse.jsonl`.

#### OpenCode versions

OpenCode ships two server APIs from the same `opencode` binary name, and the adapter speaks
both:

- **1.x** is the `opencode-ai` npm package (verified 1.18.2–1.19.x).
- **2.x** is a separate package, `@opencode/cli` (verified 2.0.21–2.0.27). Installing it
  replaces the 1.x `opencode` binary on PATH.

Nothing to configure: before each session the adapter runs `opencode --version`, picks the
matching API, and confirms the choice against the running server's health endpoint. A
major it has no codec for (or a `--version` it cannot parse) fails the session start with
an error naming the version found and the supported ranges; `bmad-loop validate` reports
the same verdict as `adapter.opencode-version` and warns below a major's earliest verified
release.

What differs on 2.x:

- **Instructions:** 2.x reads only `AGENTS.md` — there is no `CLAUDE.md` fallback, so put
  project instructions the agent must see in `AGENTS.md`.
- **Skills:** 2.x has no switch to turn off the operator's personal skills
  (`~/.claude/skills`, `~/.agents/skills`) and its built-in ones. The adapter keeps sessions
  hermetic with permission rules instead: every skill is denied except the project's own,
  so the model is offered only those. (`GET /api/skill` still lists every skill it found;
  the rules govern what a session can load.) A project skill whose directory name contains
  `*` or `?` stays denied, because 2.x reads those characters as rule wildcards; the spawn
  records a `skill-ids-refused` event in the session lifecycle log.
- **Logs:** a provider failure (rate limit, quota, refused connection) never reaches the
  server's stdout on 2.x, so the adapter records each one in
  `<task-id>.provider-errors.log`, which the environment-fault classifier scans beside
  `<task-id>.server.out`.
- **Going back to 1.x:** both majors keep their state in the same
  `~/.local/share/opencode/opencode.db`. A 1.x server refuses to start on a database a 2.x
  server created ("Database is not empty and has no session table"), so move that file
  aside after downgrading.

#### Codex hook trust under worktrees

Codex trusts hooks per directory, and an untrusted hook never fires, so a session would never report back. Check trust before the first run:

1. In the project root, run `codex` and accept both prompts (workspace trust, then "Hooks need review → Trust all and continue").
2. `bmad-loop validate`: the `hooks.trust` check must be ok. `bmad-loop probe-adapter codex` reports Codex's verdict for the project checkout as `hook_trust` and exits 1 unless it is `trusted`. It does not account for stage `extra_args` or worktree isolation, so always re-check with `validate`.
3. If the verdict is not `trusted`:
   - `untrusted`, trust is stale: open Codex in that checkout and accept the hook prompt again.
   - `untrusted`, the relay is not usable or Codex omitted it: re-register it with `bmad-loop init --cli codex`.
   - `unverifiable`: fix the cause it names, for example stage `extra_args` other than `--dangerously-bypass-approvals-and-sandbox`, profile `launch_args`, Codex missing from `PATH`, or an unreadable `.codex/hooks.json`. Then check again.

Under `[scm] isolation = "worktree"`, every unit runs in a new worktree that Codex has not trusted yet. `validate` therefore always fails `hooks.trust` under worktree isolation; that failure is expected, and `probe-adapter codex` still reports the project checkout. The run checks trust itself before each Codex session in a worktree. When a check fails, the unit escalates and the run pauses. For each such pause:

1. Read the escalation. It names the role, the worktree path, the status and the reason. For `unverifiable`, fix the cause first.
2. `cd` into the named worktree (it stays mounted under `.bmad-loop/runs/<run-id>/worktrees/`), run `codex`, and accept the hook trust prompt.
3. `bmad-loop resolve <run-id> --no-interactive` and answer yes: it re-arms the story and resumes the run. (With `--no-resume`, run `bmad-loop resume <run-id>` afterwards.)

Expect one pause per unit: each worktree path needs its own grant. The re-armed story restarts from its baseline. bmad-loop never writes Codex's trust state itself.

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
reference, see the [configuration reference](reference/configuration.md).

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
bmad-loop clean --hard --retain 0 --project <project-root>   # delete every concluded run + its worktrees
bmad-loop cleanup --project <project-root>         # kill leftover tmux sessions/windows
```

`clean --hard` permanently deletes runs instead of archiving them (we're removing the tool, so
there's nothing to keep), and `--retain 0` puts every concluded run past the retention window
(a `[cleanup] retention_days` you set still keeps newer runs; unset it first). Running it **first** also matters for the out-of-tree half: each run's
control-plane directory lives under the user-scoped state root, and `clean` is what collects it
(step 2 covers what is left over). See
[Reclaiming sessions and disk](reference/run-state.md#reclaiming-sessions-and-disk) and the
[CLI reference](reference/cli.md#subcommands) for what each command touches. Make sure no run is still live (Editor open, session attached) first.

`clean` never collects paused, crashed or interrupted runs. Run `bmad-loop list --project <project-root>`
afterwards; for each run still listed, save any work you want from its worktree under
`.bmad-loop/runs/<run-id>/worktrees/`, then `bmad-loop delete <run-id> --project <project-root>`
(or `archive` to keep a tarball). Deleting `.bmad-loop/` by hand instead skips their worktree teardown.

### 2. Remove the orchestrator state

Delete the `.bmad-loop/` directory. This removes the `policy.toml` template,
legacy relay copies from older installs, and all per-run state
(`runs/`, `cache/`, `archive/`) in one step:

```bash
rm -rf .bmad-loop/
```

One piece is **not** under `.bmad-loop/`: each run's hook-event channel lives in a user-scoped
state root outside the project — `$XDG_STATE_HOME/bmad-loop`, else
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
remove the hook entry whose `command` ends in `bmad-loop relay <Event>` (or, on
older installs, is a Python or `uv run --no-project python` command whose script
path ends in `.bmad-loop/bmad_loop_hook.py` and whose event argument matches
that hook event):

- **claude** — `.claude/settings.json`
- **codex** — `.codex/hooks.json`
- **gemini** — `.gemini/settings.json`
- **copilot** — `.github/copilot/settings.json`
- **antigravity** — `.agents/hooks.json` (the `bmad-loop` hook group)

Edit only the registered CLIs. Match the full relay command and event; leave every
other hook in place. The installed `bmad-loop` executable is shared with other projects,
so remove it only when uninstalling the package itself.

### 5. Drop the gitignore lines

`init` appended `.bmad-loop/runs/`, `.bmad-loop/cache/`, `.bmad-loop/policy.toml` and `_bmad/render/` to
`.gitignore`. Remove those four lines (skip any your project relies on for other reasons). If
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
