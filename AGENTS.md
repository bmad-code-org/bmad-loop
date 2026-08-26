# AGENTS.md — bmad-loop

bmad-loop is a deterministic Python orchestrator that drives unattended BMAD-method dev loops by spawning coding-CLI sessions (claude, codex, gemini, copilot, antigravity, opencode) inside terminal multiplexers (tmux; psmux on Windows). **The control loop contains no LLM calls — hard rule.** Orchestration is deterministic Python; LLMs run only inside disposable coding-CLI sessions. Never move orchestration into an LLM. User-facing overview: [README.md](README.md); behavior reference: [docs/FEATURES.md](docs/FEATURES.md).

## Dev environment

```bash
uv sync --all-extras          # setup (extras: tui, non-linux, opencode)
uv run pytest -q              # test suite (-n logical to parallelize)
uv run pytest tests/test_engine.py -q   # single file
uv run pyright                # typecheck — same pinned version CI runs
trunk fmt                     # format changed files
trunk check                   # lint changed files, as CI does — run before every push (pre-push hook enforces it)
trunk check --all             # whole-repo lint; the only way to catch untouched files (e.g. after a linter bump)
```

- Never `pip install`; uv owns the environment. Dependency changes: edit pyproject.toml, run `uv lock` (CI uses `uv sync --locked` and fails on a stale lock).
- Typecheck: `uv run pyright`. The version is pinned exactly in the `dev` group (pyproject.toml) and CI runs that same command, so there is one pin and no drift — bump it deliberately, never with `uv lock --upgrade`.
- Windows local runs require `PYTHONUTF8=1` (tests/conftest.py raises UsageError otherwise). Python floor: 3.11.

## Hard invariants

Things that break silently. Never violate; when in doubt, read the named module's docstring.

- No LLM calls in the orchestrator control loop.
- Sessions complete only on hook Stop events or window death — never on LLM prose. Never add another completion path. Post-session state is re-verified deterministically (`verify.py`).
- `sprintstatus.advance()` is the orchestrator's sole write path to sprint-status.yaml (internally serialized cross-process since #469; the dev skill flips only spec frontmatter; the engine mirrors it onto the board pre-verify). Legal phase transitions live only in `statemachine.py`.
- All git subprocess calls in `src/bmad_loop` go through the `_run_git` chokepoint in `verify.py` (timeouts, `LC_ALL=C`) — no bare subprocess git; `tests/test_portability_guard.py` enforces it. Tests, `scripts/`, and CI workflows deliberately spawn their own git: a harness must not depend on the artifact it validates.
- Every new policy field needs an entry in `src/bmad_loop/data/settings/core.toml` (a sync test enforces defaults/options match `policy.py`). New core env vars register in `envvars.py`; plugin-owned env-var families stay with their plugin.
- Version strings are stamped only by `scripts/sync_version.py` from `src/bmad_loop/__init__.py` — never hand-edit pyproject.toml, module.yaml, marketplace.json, or uv.lock versions.
- Canonical module skills live in `src/bmad_loop/data/skills/`; their copies in the gitignored `.claude/skills/` and `.agents/skills/` are seeded forks (`scripts/seed_skills.py`) — editing a fork loses work on reseed (drift-tested locally; the test skips in CI). Other skills in those dirs are BMAD-installed, not seeded. (The `.agents/` directory is unrelated to this file.)
- `bmad-loop <cmd> --json` output is a schema-versioned contract (`machine.py`): one JSON object on stdout, nothing else.
- `ExitCode` allocation is closed (`cli.py`): OK=0, FAILURE=1, USAGE=2, INTERRUPTED=130; 3–129 and 131+ stay unallocated.
- tmux/POSIX argv construction is quarantined in `adapters/tmux_base.py` + `tmux_backend.py`; `tests/test_portability_guard.py` enforces it.
- Live/E2E tests must consume zero LLM tokens.

## Architecture

Two orthogonal seams: **which CLI** (adapter axis: `adapters/base.py` `CodingCLIAdapter`, TOML profiles in `src/bmad_loop/data/profiles/`, user overlay `.bmad-loop/profiles/*.toml` — a new coding CLI is a TOML profile plus `bmad-loop probe-adapter`, not Python; a CLI needing its own adapter **class** registers one in `adapters/registry.py` (`register_adapter`, `bmad_loop.adapters` entry point), selected by the profile's `adapter` field) and **which transport** (mux axis: `adapters/multiplexer.py` `TerminalMultiplexer` registry; selection: `BMAD_LOOP_MUX_BACKEND` env > policy `[mux] backend` > platform default > first available match > fallback — full 5-step precedence in [docs/multiplexer-backends.md](docs/multiplexer-backends.md)).

| Module                                     | Role                                                                                        |
| ------------------------------------------ | ------------------------------------------------------------------------------------------- |
| `cli.py`                                   | argparse CLI, all `cmd_*` handlers; injectable `engine_cls`/`make_adapters` seams           |
| `engine.py`                                | `Engine` — the deterministic control loop                                                   |
| `worktree_flow.py`, `recovery_flow.py`     | engine collaborators: worktree isolation/merge; rollback + recovery-ref preservation        |
| `sweep.py`, `stories_engine.py`            | deferred-work sweep run type; stories.yaml mode                                             |
| `statemachine.py`, `model.py`, `policy.py` | pure typed core (pyright-strict): transitions, data model, policy.toml → frozen dataclasses |
| `verify.py`                                | deterministic post-session verification; `_run_git` chokepoint                              |
| `machine.py`, `documents.py`               | `--json` contract; read-model projections                                                   |
| `adapters/`                                | coding-CLI adapters + mux backends (see seams above)                                        |
| `tui/`                                     | Textual dashboard (`tui` extra); observer/launcher only — never runs engines in-process     |
| `plugins/`                                 | manifest-driven extension layer (`plugin.toml`, trust tiers, hook bus)                      |

28 further leaf modules — read the module docstring before assuming. Deeper maps: [docs/FEATURES.md](docs/FEATURES.md), [docs/adapter-authoring-guide.md](docs/adapter-authoring-guide.md), [docs/multiplexer-backends.md](docs/multiplexer-backends.md).

## Testing

- Flat `tests/` mirrors src modules by name; `asyncio_mode=auto`; no custom markers.
- Use the conftest sandbox fixtures (`project` copies a session-scoped template repo per test — never hand-roll temp repos or touch the template). Mock adapter drives engine tests without tmux or an LLM; caveat: it exercises the generic adapter path only.
- Pin the backend with the `force_tmux_backend` fixture when asserting tmux argv through the mux seam.
- E2E gates: `tests/test_stories_e2e.py` (real tmux on Linux + a scripted fake-claude profile, zero LLM tokens), `tests/test_opencode_live.py` (zero-token invariant — never sends a prompt), and `tests/test_psmux_live.py` (real psmux on Windows, parked windows only, zero tokens). Never "fix" these to call real CLIs.
- Ablation rule: for any test asserting "X is refused/absent", delete the gating code and confirm the test FAILS before trusting it — negative assertions pass for every reason a value could be absent.
- New behavior lands with a test at the lowest layer that can catch its regression: pure-core unit > seam > sandbox E2E.
- Full strategy — layer taxonomy, fixture/ablation doctrine, guard inventory, flake policy: [docs/testing.md](docs/testing.md).

## Repo hygiene

- CHANGELOG entries: terse, scannable, imperative, under the `## [Unreleased]` heading. That heading is the contract, not a staging area:
  - Every entry lands under `## [Unreleased]`, and only under the six Keep a Changelog subsections — `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`.
  - A release **promotes** that section: `## [Unreleased]` is renamed to `## [X.Y.Z] — <ISO date>` and a fresh empty `## [Unreleased]` opens above it. The release never authors a new version section from the git log, and never leaves a populated `Unreleased` behind — `scripts/release.py prepare` refuses both, and `release.py check` (CI job `version-sync`) holds the reopened heading and its `compare/v<version>...HEAD` link.
- Never commit session notes, probe records, or run artifacts. Durable facts belong in docstrings; records in git history.
- Review non-convergence is evidence about the approach, not just a defect queue — escalate rather than grind.

## Engineering doctrine

These rules apply to code you are already touching — do not initiate refactors of untouched code to satisfy them.

- Preserve behavior first: released CLI syntax, `--json` output, exit codes, config formats, and persisted data are compatibility contracts — characterize before restructuring.
- Split by responsibility, never by line count: a cohesive state machine stays whole (`statemachine.py`); size thresholds are review heuristics, not lint gates.
- Strict typing lives in the pure core, not the I/O edges (the staged pyright config in pyproject.toml is deliberate). Do NOT ratchet ruff stricter than CI — the `[tool.ruff.lint]` comment documents why.
- Fail loud at boundaries: typed escalation over bare except; observation may degrade, repair writes must raise.

## Docs index

| Doc                                                                | Read when                                               |
| ------------------------------------------------------------------ | ------------------------------------------------------- |
| [docs/setup-guide.md](docs/setup-guide.md)                         | installing/initializing a target project                |
| [docs/FEATURES.md](docs/FEATURES.md)                               | any behavior or policy question                         |
| [docs/tui-guide.md](docs/tui-guide.md)                             | TUI work                                                |
| [docs/adapter-authoring-guide.md](docs/adapter-authoring-guide.md) | adding/finalizing a coding-CLI profile or adapter class |
| [docs/multiplexer-backends.md](docs/multiplexer-backends.md)       | mux backend selection/porting                           |
| [docs/plugin-authoring-guide.md](docs/plugin-authoring-guide.md)   | plugin work (incl. game-engine + TEA guides)            |
| [docs/porting-to-a-new-os.md](docs/porting-to-a-new-os.md)         | OS seams                                                |
| [docs/testing.md](docs/testing.md)                                 | writing/placing tests, guards, flake policy             |
| [docs/ROADMAP.md](docs/ROADMAP.md)                                 | planned vs deliberately-deferred work                   |

Full list: [docs/README.md](docs/README.md).
