# bmad-loop roadmap

Forward-looking work for the orchestrator itself — design intent and rationale for features
we've deliberately deferred, so the "why" survives between sessions.

Status legend: **planned** (agreed, not started) · **exploring** (shape still open) · **blocked** (waiting on an external dependency).

---

## Native Windows multiplexer backend

**Status:** planned · **Foundation:** the full platform-seam series landed (multiplexer registry + `BaseTmuxBackend` + `ProcessHost` + hook interpreter + validate preflight, v0.7.6; original seam v0.7.0)

The orchestrator no longer fuses tmux into the engine. All session/window/pane operations
go through a single `TerminalMultiplexer` ABC (`src/bmad_loop/adapters/multiplexer.py`),
obtained from `get_multiplexer()`; the tmux backend — argv + spawn primitive in
`BaseTmuxBackend` (`adapters/tmux_base.py`), POSIX leaf `TmuxMultiplexer`
(`adapters/tmux_backend.py`) — is the **only** place allowed to shell out to `tmux`, and it
quarantines the POSIX `sh -c` parked-window trailer. Backends now self-**register**
(`register_multiplexer`, selected by platform with a `BMAD_LOOP_MUX_BACKEND` override) rather
than being hardcoded into `get_multiplexer()`. The process-lifecycle POSIX-isms moved behind a
matching seam, `ProcessHost` (`src/bmad_loop/process_host.py`): `terminate` / `force_kill` /
`is_alive` / `identity` (a PID-reuse guard) plus `hook_interpreter()` (so hook registration
never branches on platform), registered the same way (`register_process_host`,
`BMAD_LOOP_PROCESS_HOST`); `WindowsProcessHost` already ships. `bmad-loop validate` runs a
`_platform_preflight()` that reports the selected backend's readiness and names the process
host — so a new OS surfaces in preflight by registering, not by a `validate` edit. The Unity
plugin's `/proc`/`/tmp`/`cp -a`/symlink primitives degrade off Linux (with `psutil` from the
optional `non-linux` extra) and its pid lifecycle now delegates to `ProcessHost`; everything is
held by a CI portability guard (`tests/test_portability_guard.py`). **WSL already works today**
— it _is_ Linux, so it takes every fast path unchanged; this is purely about a future _native_
Windows host.

The remaining work is a real non-tmux backend (a "psmux"-style multiplexer) that implements
the `TerminalMultiplexer` contract on native Windows and registers itself for `win32`. The
seams are designed so this slots in as **new files + one registration line each, with no
change to the adapters, `runs.py`, `tui/launch.py`, `probe.py`, `tui/data.py`, or
`cli.py`'s `validate`** (`WindowsProcessHost` and its hook interpreter are already in place
and registered). The end-to-end port path — both build options, the test-override env vars,
and exactly what a native-Windows port costs — is documented in
[Porting bmad-loop to a new OS](porting-to-a-new-os.md); the deep transport contract is in
the [adapter authoring guide](adapter-authoring-guide.md#the-transport-contract-for-a-backend-author).

**Open questions:** what hosts the windows on native Windows (Windows Terminal panes, a
ConPTY-based manager, a headless process supervisor?); how attach/detach and the parked
exit-status window map without a POSIX shell; and the Windows-Unity cache-path correctness
left as a documented follow-up in Phase 4.

---

## Parallel unit execution (`[scm] max_parallel`)

**Status:** planned · **Foundation:** landed with worktree isolation (v0.4.0)

Worktree isolation (`[scm] isolation = "worktree"`) already gives each story/bundle its own
worktree and branch, and the `max_parallel` knob is parsed and validated in
`src/bmad_loop/policy.py` (`ScmPolicy`). But it is **clamped to `1` in `loads()`** — merge-back
is serialized, one unit at a time — because the internal fan-out scheduler isn't built yet. The
knob exists so the config surface is stable; it stays inert until this phase lands.

The goal is to drive N units concurrently (each in its own worktree, independent tmux session),
then serialize only the merge-back into the target branch. Then lifting the clamp activates the
existing knob with no config change for users.

**Open questions:** how to bound concurrent CLI sessions vs. token/cost budgets; merge-back
ordering and conflict handling when several units finish close together; how the TUI surfaces
multiple in-flight units per run.

---

## Automate epic retro action items

**Status:** planned · **Blocked-by:** retro-item detail isn't standardized yet

The parser now recognizes `epic-{N}-retro-item-{M}-{slug}` keys in `sprint-status.yaml`
(`src/bmad_loop/sprintstatus.py` → `RetroItem` / `SprintStatus.retro_items`), so the
`sprint-status-unknown-keys` warning no longer fires. They are tracked but **not driven** as work.

The goal is to run actionable (`backlog`) retro items through the dev → review → commit pipeline,
the same way deferred-work sweeps already run.

**Approach (designed, not built):** a separate `bmad-loop retro` run type that mirrors the
`SweepEngine` (`src/bmad_loop/sweep.py`) end-to-end — `RetroEngine`, a `retro` CLI command + resume
branch, a retro-item intent fed to the `bmad-dev-auto` primitive, and `verify` helpers paralleling
the bundle verifiers. Story runs stay untouched.

**Why blocked:** retro-item _detail_ is scattered — some lives in the epic retro-doc Action-Items
table (`epic-N-retro-YYYY-MM-DD.md`), some in `deferred-work.md` (DW-N) entries, some in ad-hoc
`spec-*.md` files; only one epic has an `epic-N-action-items.md`. A deterministic key→file map isn't
viable, so automation needs an LLM triage step (like sweep's) to locate/extract each item's intent
**and** classify out the non-code items (research, docs). **Prerequisite:** standardize where
retro-item detail is written at retrospective time (a future BMAD update) — that makes the triage
reliable enough to trust unattended.

---

## Integrate BMAD test-design + test-automation runs (TEA / testarch)

**Status:** exploring · **Foundation:** experimental opt-in `tea` plugin landed (v0.5.1)

The bundled, **experimental** `tea` plugin (`[plugins] enabled = ["tea"]`, see the
[TEA plugin guide](tea-plugin-guide.md)) now wires the BMAD Test Architect (TEA) suite —
`bmad-testarch-test-design`, `-automate`, `-atdd`, `-nfr`, `-trace`, `-test-review`, and the
`bmad-tea` agent — into every run and sweep as **advisory-by-default** quality steps (the three
gate steps can be flipped to blocking). It's experimental: the workflows ride the generic
`[workflows.<name>]` session-injection layer rather than being first-class orchestrated runs.

The remaining work is to drive **test design** (derive a test plan / coverage map for a feature or
backlog) and **test automation** (generate + run the actual tests) as first-class orchestrated runs —
closing the loop that retro items like `epic-5-retro-item-1-test-design-and-backfill-prior-epics`
currently call out by hand.

**Open questions:** is this a new `test` run type, or a phase wired into the existing story/review
pipeline? How does generated-test output feed verification (gate a story on its test plan / coverage)?
Which testarch skills become orchestrated vs. stay interactive?

---

## Integrate BMAD GDS game-test items

**Status:** exploring · **Foundation:** opt-in game-engine layer landed (Unity, shared + per_worktree), now riding the general plugin system

The opt-in Unity plugin (`docs/FEATURES.md` → "Game-engine projects"), enabled with
`[plugins] enabled = ["unity"]`, already lets a Unity project run its dev/sweep cycle against a
live Editor MCP in **shared** mode (agent works in place on the operator's open Editor; a readiness
gate blocks until the Editor + MCP are up) and in **per_worktree** mode (one managed Editor per
worktree, with reflink/CoW `Library` priming, setup/teardown hooks, and MCP-skill seeding via
`seed_globs`). The game-engine layer is no longer bespoke core code — it's a plugin built on the
general [plugin system](plugin-authoring-guide.md) (the legacy `[engine]` block is a deprecated
compatibility shim, folded onto `[plugins.unity]` at load time). Next steps: batchmode `verify_cmd`
and Godot/Unreal plugins on the same `plugin.toml`+scripts shape — the authoring path is documented
in [Writing a Game Engine plugin](game-engine-plugin-guide.md) and
[Writing a plugin for a specific Editor MCP](game-engine-mcp-guide.md).

The BMAD **GDS** module (game dev — Unity / Unreal / Godot) carries its own testing track via the
`gametest` workflow (`_bmad/gds/workflows/gametest`). For game projects, the testarch/TEA pipeline
above doesn't map cleanly; GDS has its own design → technical → production → gametest flow.

The goal is to let bmad-loop recognize and drive GDS game-test items the same way it drives
sprint stories and (eventually) retro items, so game projects get the same unattended
implement → test → review loop.

**Open questions:** how do GDS workflow artifacts map onto the orchestrator's sprint-status/work-item
model? Does GDS need its own run type, or can the test-design/automation integration above generalize
to cover it? Depends on the testarch integration landing first.
