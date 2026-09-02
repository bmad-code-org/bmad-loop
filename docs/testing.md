# Testing strategy

How bmad-loop is tested: the layer model, the doctrines behind the suite's shape, and the
policies a new test is expected to follow. [AGENTS.md](../AGENTS.md) owns the terse testing
invariants agents must not violate; this document is the strategy behind them — why each rule
exists, where a new test belongs, and which absences are decisions rather than gaps. When the
two disagree, fix this document first and re-derive the terse rule (see
[Doc-sync rules](#policy-quick-reference-and-doc-sync-rules)).

The one-line version: **deterministic tests over a real repo sandbox, zero LLM tokens anywhere,
and every negative assertion proven by ablation.**

## The suite at a glance

- Flat `tests/` — roughly 70 `test_*.py` files mirroring `src/bmad_loop` modules by name,
  ~5,100 collected tests. No package nesting, one `tests/conftest.py`, one data file
  (`tests/fixtures/stories.yaml`, the dogfooded stories-mode contract from BMAD-METHOD
  PR #2549).
- Configuration is two settings under `[tool.pytest.ini_options]` in `pyproject.toml`:
  `testpaths = ["tests"]` and `asyncio_mode = "auto"`. There is no `addopts`, no marker
  registry, no `filterwarnings` (the last is under triage — #548).
- `uv run pytest -q` runs everything runnable on the host; `-n logical` (pytest-xdist) is
  supported because every test builds its state from per-test fixtures (the template repo is
  per-worker). Prefer `logical` over `auto`: `psutil` is installed on every platform here
  (the `non-linux` extra carries no environment marker, and CI syncs `--all-extras`), so
  xdist's `auto` resolves to the _physical_ core count — half the vCPUs on the hosted
  runners. CI passes `-n logical` in both test jobs for that reason. A test that fails only
  under xdist load is a flake, which is a bug. Live/E2E modules skip themselves when their
  host requirements are absent — selection is in-file, never in config.
- Only builtin pytest marks appear: `parametrize`, `skipif`, `usefixtures`, and exactly one
  `xfail(strict=True)` (a pinned known defect, see [Ablation records](#ablation-records)).
  **No custom markers, deliberately**: a marker registry is a second selection mechanism that
  can drift from the tests it classifies. The filename suffix and the module-level `skipif`
  encode the same fact where the reader already is.

## Layers: where a test belongs

New behavior lands with a test at the **lowest layer that can catch its regression**. The
layers, from cheapest to most expensive:

| Layer                         | What it is                                                                                                                                                                                            | Machinery                                                                                                                                                                                                                                                                 |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **L1 — pure core**            | Unit tests over the pyright-strict seven: `model`, `statemachine`, `policy`, `documents`, `machine`, `checks`, `sanitize`. No process, git, or mux I/O; fixtures limited to `tmp_path`/`monkeypatch`. | Plain asserts; exhaustive `parametrize` grids                                                                                                                                                                                                                             |
| **L2 — seam**                 | Tests that drive one injection seam with a scripted double.                                                                                                                                           | `MockAdapter`, TOML adapter profiles, the `force_tmux_backend` fixture, the injectable `make_adapters` and engine-class parameters of `runsetup.compose_run` (`engine_cls`, `stories_engine_cls`), `compose_sweep` (`sweep_engine_cls`), and `compose_resume` (all three) |
| **L3 — sandbox integration**  | Engine/CLI flows over a real git repo, no multiplexer.                                                                                                                                                | The `project` fixture (a copied template repo) plus the conftest effect helpers                                                                                                                                                                                           |
| **L4 — real-mux integration** | A real tmux server drives a scripted shell script standing in for the CLI.                                                                                                                            | The `HAVE_TMUX`-gated tests in `tests/test_generic_tmux.py`                                                                                                                                                                                                               |
| **L5 — live gates**           | Real external binaries, still zero tokens.                                                                                                                                                            | `tests/test_stories_e2e.py`, `tests/test_opencode_live.py`, `tests/test_psmux_live.py`                                                                                                                                                                                    |

Placement rules:

- **Mirror-file rule.** A test for `src/bmad_loop/<module>.py` lives in `tests/test_<module>.py`;
  subpackage modules flatten into prefixed names (`tui/app.py` → `test_tui_app.py`,
  `plugins/loader.py` → `test_plugin_loader.py`), and adapter internals are covered through
  their transport suites. Cross-module flows live with the module that owns the behavior under
  assertion (engine flows in `test_engine.py`, CLI contract in `test_cli.py`). A module without
  a mirror file is a smell the gap register tracks — five top-level modules currently lack one
  (see the register's mirror-gap rows; #545 tracks the sharpest, `machine.py`).
- **Suffix convention is the selection mechanism.** `*_e2e.py` and `*_live.py` name the modules
  with host requirements; each carries a module-level
  `pytestmark = pytest.mark.skipif(...)` naming its requirement (`HAVE_TMUX`, `HAVE_OPENCODE`,
  `HAVE_PSMUX`; the opencode module adds an `importorskip` on httpx, and its `HAVE_OPENCODE` is
  probe-backed rather than `which`-backed — conftest's `opencode_runs()` runs the binary's
  `--version`, so a resolvable-but-dead shim skips instead of failing, #294). Ordinary runs collect
  them and skip them; a capable host runs them with no extra flags. Do not add a marker, an env-var opt-in, or a separate pytest invocation for
  these — the filename plus the in-file gate is the whole mechanism.
- **Prefer a lower layer over a broader one.** If a defect is expressible as a pure-core case,
  do not pin it in a sandbox flow: L3+ tests are slower, and their failures triangulate worse.
  The inverse also holds — a seam contract (say, adapter timeout instrumentation) cannot be
  proven by an L1 test and belongs at L2 with a parity twin (see
  [Duplicate tests and contract parity](#duplicate-tests-and-contract-parity)).

## Fixtures and helpers

`tests/conftest.py` holds **fixtures for lifecycle and isolation only — currently exactly
five** — and everything else is a plain function or constant a test imports by name:

- `project` — the workhorse: a disposable copy of a session-scoped template repo
  (`_project_template`), BMAD-shaped artifact dirs plus an initial commit. Never hand-roll a
  temp repo and never touch the template — a mutation there poisons every later test in the
  worker; the copy exists so per-test git init/fsync cost is paid once per xdist worker.
- `force_tmux_backend` — pins `BMAD_LOOP_MUX_BACKEND=tmux` and clears the selection cache on
  both ends, so tests asserting tmux argv through the mux seam cannot be hijacked by an
  installed external backend that happens to match the platform.
- `_project_template` — session-scoped, built once per xdist worker, reached only through
  `project`.
- `_isolate_ambient_git_ignores` — session-scoped and **autouse**: it wraps every test, not
  just sandbox ones, shadowing the developer's global gitignore sources (`GIT_CONFIG_GLOBAL`,
  `XDG_CONFIG_HOME`). Without it, a dev box that globally ignores `.claude/` makes shield
  tests measure the wrong thing while passing.
- `_isolate_state_root` — **autouse** and function-scoped: points `BMAD_LOOP_STATE_DIR` at a
  per-test temp dir. `runs.state_root()` does not merely read the user-scoped state location,
  it mkdirs into it, so without this every test that builds an adapter would litter the
  developer's (or the runner's) real state directory with one tree per run id, on a path no
  fixture cleans up.

Everything else is a **plain helper — a function or constant** (`from conftest import
write_spec, dev_effect, machine_json, ...`). The rule is deliberate: a fixture is ambient — its consumers are invisible
at the call site and to grep — so it is reserved for setup/teardown that must wrap the test.
Construction and simulation (`write_spec`, `write_sprint`, `dev_effect`, `review_effect`,
`escalated_run`, ...) are explicit imports, so each test names its dependencies. Local fixtures
inside a single test file are fine for file-scoped concerns.

Two helpers carry suite-wide contracts and are worth knowing before writing any CLI or engine
test:

- `machine_json(argv, capsys, *, rc=0, err_contains=None)` — runs a `--json` command and parses
  the **whole** of stdout. Parsing the full stream is itself the assertion that nothing but the
  document was printed (the `machine.py` purity contract); the default `err == ""` is the strict
  form, and `err_contains` is an opt-in to a different assertion, never a waiver.
- `dev_effect` / `review_effect` and their bundle twins — simulate what a real skill session
  does to disk (spec frontmatter flips, code edits) and, just as deliberately, what it does
  **not** do: `dev_effect` never writes sprint-status — the orchestrator is the single
  sprint-status writer at dev time, and a test double that helpfully did both would mask a
  regression. (`review_effect` does advance the board when it finalizes, modeling what real
  review sessions do and the orchestrator re-verifies; the bundle twins write none, because
  bundles have no sprint-status entry.)

Divergent-root tests choose a topology for the distinction they need to prove:

- **Default** (`project`) keeps `project == repo_root`. Use it for ordinary sandbox behavior,
  never for an assertion that claims to distinguish those roots.
- **Sibling** places `repo_root` beside or otherwise outside the BMAD project. Lower-level seam
  rows may hand-build this shape (`test_verify.py::_repo_root_override`); caller/config coverage
  writes it with `write_repo_root_override` and reloads it through `load_paths`. Use it to prove
  that code commands run in the configured root while artifact reads stay in the project.
  Artifact-derived excludes correctly collapse to `()` from the disjoint code tree; a non-empty
  project-relative spelling may still match nothing there, so this shape cannot grade pathspec
  selection by outcome alone.
- **Nested monorepo** (`nested_repo_root_paths`) puts the BMAD project at `<repo>/app`, writes
  and commits `_bmad/bmm/config.yaml`, and returns `load_paths(app)`. Use it when both right-
  and wrong-root pathspecs must be non-empty and separable: the correct value carries `app/`,
  while the wrong value can select a plausible outer-tree decoy. The helper accepts alias
  spellings but returns canonical paths and commits every seed/config file, so fixture residue
  cannot masquerade as session work.
- **Isolated worktree** adds a third live root. Use a real relative command against a marker
  created only in the mounted unit worktree; assert the main checkout lacks it, the command
  record passed, and classification received the same mounted cwd. Keep the marker under a
  gitignored generated-state path so it cannot merge back as proof of work.

Every divergent-root row guards its premise before its outcome: compare resolved roots, assert
the nested parent relation when nesting matters, and for cwd tests plant/probe both directions
(`plant_root_markers`, `REPO_ROOT_MARKER_CMD`, `PROJECT_MARKER_CMD`). A positive marker identifies
the intended root; the opposite-root marker rules out the tempting alternative. When a wrong
root could still name a real path, create that decoy and assert the selected value rather than
asserting only that some path is absent or a gate refused.

**`MockAdapter` is production code** — `src/bmad_loop/adapters/mock.py`, shipped in the wheel,
scripted with a list of `SessionResult`s or `callable(spec) -> SessionResult` effects. It is
not reachable from configuration (no `mock` profile exists; `runsetup.make_adapters` builds
only real CLI adapters — generic-over-mux, or the opencode HTTP pair for hookless profiles),
and it exercises **the generic adapter path only** — a caveat to
respect, not to fix: adapter-specific behavior needs an L2 profile test or an L4/L5 gate. It
also deliberately carries no `profile` attribute; the profile-less shape _is_ the None-tree
fallback path, and `attach_profile` in conftest opts in where the resolved skill name is under
test.

## Quality guards (meta-tests)

A slice of the suite tests the **repo** rather than the product. The inventory:

| Guard                      | Where                                                   | Enforces                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------- | ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Portability guard          | `tests/test_portability_guard.py`                       | One shared AST scan over every `src/bmad_loop/**/*.py` (data scripts included), carrying thirteen guards: literal `["tmux", ...]` argvs only in the two backend files (the backends' own `[self._BINARY, ...]` spelling is deliberately unmatched, so this tripwire currently flags nothing — #549); sequence-form git argvs (list or tuple, literal or named constant) only as `_run_git`'s argv argument in `verify.py`, with string-form git spawns refused everywhere, `verify.py` included; no bare `/tmp`-class POSIX paths; no `signal.SIGKILL` attribute; `os.kill(pid, 0)` probes only in `process_host.py`; any `os.kill` at all only there too (a second, distinct guard); `start_new_session` only in the detach helpers; `shell=True` only in its two sanctioned files; `BMAD_LOOP_*` env reads only through the `envvars.py` registry, a plugin's own variable family, or the session-protocol vars the two stand-alone hook relays read back; a persisted `spec_file` / `dispatched_spec_file` resolved with a bare `Path(...)` only in the four files that run inside the tree the value was recorded against; `verify_commands_outcome` called only from `verify._verify_review_commands`; its classifier half `verify_command_results_outcome` called only from `verify.verify_commands_outcome` or `Engine._verify_commands_with_results` (a separate guard, because fencing the wrapper alone still lets a gate compose run+classify by hand and pick its own root — #695); plus a scanned-file-count floor so a broken scan root cannot pass vacuously |
| Kind & refusal inventories | `tests/test_portability_guard.py`                       | Enumerate-vs-declare gates riding the same AST scan, so a new entry on an enumerable surface is a PR-time decision instead of review-pass archaeology: every literal journal kind is a declared `JOURNAL_KINDS` row (kind-only writes and the `kind=` literals or parameter defaults that reach a declared dynamic-kind position included, via a dedicated literal-kind emit; only the f-string family stays out, governed by the literalness test); every `_refuse_*`/`_reject_*` helper definition is a `REFUSAL_HELPER_DEFS` row; and the #414-family isolation-refusal call sites (`bmadconfig.worktree_isolation_conflict` plus its CLI wrapper `_reject_isolation_conflict`) match `ISOLATION_CONFLICT_CALLERS` with multiplicity, so `cmd_resolve`'s legitimate second call cannot absorb a third. All assertions are exact-set or exact-Counter in both directions — a removed or renamed entry reddens its stale row too — and each new detector emit carries must-flag and must-stay-silent probe rows through `_scan_source`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Settings-schema sync       | `tests/test_settings_schema.py`                         | `src/bmad_loop/data/settings/core.toml` stays in lockstep with `policy.py` by reflection, in both directions: every spec maps to a live dataclass field with a matching default wherever one is baked in, every policy field is reachable from exactly one spec (or listed in the explicit `HIDDEN` set), and every `*Policy` dataclass is consciously classified                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| Exit-code allocation       | `tests/test_entry_point.py`                             | `ExitCode` is pinned literally (OK=0, FAILURE=1, USAGE=2, INTERRUPTED=130) **and closed**: the enum's value set equals exactly those four, so codes 3–129/131+ cannot be allocated quietly                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Extra-less core CLI        | `tests/test_entry_point.py`                             | A fresh interpreter with `pyte`/`rich`/`textual`/`tomlkit` blocked at `find_spec` — the blocker **raises** rather than returning None, so the dev venv's installed copies cannot make it pass vacuously, and an `import pyte` floor proves it bites — imports `bmad_loop.cli` and `bmad_loop.settings_schema`, runs `list` to rc 0, and asserts `tui` degrades to the `bmad-loop[tui]` hint instead of a traceback. Every test job installs `--all-extras`, which is why #650 shipped broken for 23 releases; CI's isolated wheel `list` run is the same floor at install level                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| State-machine table        | `tests/test_statemachine.py`                            | Every `Phase` has a transition row; `TERMINAL_PHASES` (model.py) equals the table's dead ends — a cross-module parity nothing else links; an N×N `parametrize` grid drives every pair (legal pairs land, illegal pairs raise and leave the phase untouched); the awaiting-operator reachability rule is additionally stated independently, because the N² grid reads its expectation out of the table under test                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Check-id registry          | `checks.py` + `tests/test_cli.py`                       | `ValidationReport.add` asserts its id is in `VALIDATE_CHECKS` at every **executed** call site, and an end-to-end test unions the ids a real passing **and** failing `validate --json` emit and asserts them registered. Both mechanisms are exercised-path enforcement — there is no static call-site scan, so an id on a branch neither reaches can still ship unregistered and raises `AssertionError` only when that branch first executes; a new check site therefore lands together with a test that reaches it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| Skill-drift guard          | `tests/test_module_skills_sync.py`                      | The seeded forks in `.claude/skills/` and `.agents/skills/` are byte-identical to canonical `src/bmad_loop/data/skills/`. **Documented limitation: CI-inert** — both trees are gitignored and absent in CI, so every parametrization skips there; the guard bites on dev boxes only. (The canonical-existence assertion runs before the skip and is CI-live.)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Schema-version parity      | `tests/test_tui_app.py`                                 | The TUI renderer's pinned validate schema version equals `documents.VALIDATE_SCHEMA_VERSION` — deliberate duplication, because an import would auto-follow a CLI bump and silently render a v2 document as v1                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Installed-copy drift       | `tests/test_hook_script.py`, `tests/test_probe_hook.py` | The hook relays' copies match their source: `test_hook_script.py` re-runs `install_into` and text-compares the project copy against the source; `test_probe_hook.py` compares the packaged resource — which only bites in a wheel-installed run, since an editable install resolves both sides to the same file                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Version sync               | `tests/test_release.py` + CI                            | `scripts/release.py check` runs as the `version-sync` job — `sync_version.check()` in-process, plus the CHANGELOG release contract (the canonical version's section exists; `## [Unreleased]` was reopened; its `compare/v<version>...HEAD` link tracks the bump). `tests/test_release.py` covers the release helpers' pure logic **and** drives `cmd_check`/`cmd_prepare` over fixture changelogs; the version-field comparison itself is still CI-only                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |

Rules for adding or touching a guard:

- **Fix the source, not the allowlist.** A new finding means routing the call through the seam;
  widening an allowlist is the last resort and needs the same scrutiny as a production change.
- **Exemptions are scoped and acknowledged.** Most file-keyed allowlists (paths, detach,
  kill-probe, `shell=True`) require a per-line `# portability:` ack on top; whole-file passes
  exist only where the file _is_ the sanctioned spot (the tmux backends; `process_host.py` for
  `os.kill`), and `verify.py`'s git exemption is narrowed further, to the `_run_git` argv
  position. Env-read exemptions are scoped **by variable name or family, never by file** — a
  file-wide pass would let a hook read a core knob unnoticed.
- **The detector itself gets executable coverage.** Every detector added since this bar was set
  carries a probe matrix — env-read, git-argv, the two verify-composition ones, spec-anchor,
  task-artifact, session-task-id, the re-arm call, the journal field / kind / kind-literal
  family, refusal-def and isolation-call: the scan is split (`_scan_source`) so probe fixtures run
  the same code path as the real scan, with a must-flag row per claimed access form and a
  must-stay-silent row per lookalike. When a new form turns up, add the failing probe row
  first, then fix the detector. The green "no findings today" assertion cannot grade a
  detector — it is equally green when the scan silently stopped scanning, which is why the
  matrices exist. The seven older tripwires predate this bar and have no probe rows; #549
  tracks bringing them up to it.
- **The guard's boundary is `src/bmad_loop`,** enforced structurally by its scan root rather
  than by an allowlist. Tests, `scripts/`, and CI workflows deliberately spawn their own git —
  a harness must not depend on the artifact it validates (AGENTS.md states this for git; the
  same reasoning covers their env reads).

## The zero-token invariant

**No test, at any layer, ever spends an LLM token.** AGENTS.md states the hard invariant for
the live/E2E gates ("Live/E2E tests must consume zero LLM tokens"); in practice it holds
everywhere, and the mechanism differs per gate — worth knowing before touching any of them:

| Gate                                                               | Real component                                     | Zero-token mechanism                                                                                                                                                                                                                                                                                                                                                             | Runs where                                                                                                                  |
| ------------------------------------------------------------------ | -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `test_generic_tmux.py` (5 `HAVE_TMUX` test functions, 7 collected) | tmux server                                        | The spawned "CLI" is a tiny shell script written by the test                                                                                                                                                                                                                                                                                                                     | CI Linux job + any POSIX dev box with tmux                                                                                  |
| `test_stories_e2e.py`                                              | tmux + the real `bmad-loop run/resolve/resume` CLI | Scripted fake `claude` variants (bash, defined as string constants in the test module) wired in as a custom TOML profile (`fakestories`); the fake writes its own SessionStart/Stop hook events, so no `bmad-loop init` and no real CLI exists anywhere in the run                                                                                                               | CI Linux job (Linux-only gate: the fakes use GNU coreutils + `setsid`)                                                      |
| `test_opencode_live.py`                                            | A real `opencode serve` HTTP server                | **Never sends a prompt**: only the spawn/teardown paths are used — nothing that prompts — and the tests touch health/doc/session/event endpoints; the prompt endpoint is asserted against the OpenAPI schema, never called. One test asserts the session's token/cost aggregates are zero                                                                                        | Manual — POSIX box with a **runnable** `opencode` — the gate probes `--version`, so a resolvable-but-dead shim skips (#294) |
| `test_psmux_live.py`                                               | A real psmux on Windows                            | **Parked windows only**: every parked window runs `pwsh -NoProfile -Command exit 0`, and no coding CLI is ever launched. Includes `test_premise_*` probes with inverted semantics — a red probe means a workaround became droppable — and `test_adopted_*` probes with ordinary semantics — a red probe means psmux regressed a behavior the 3.3.8 floor lets the backend assume | CI Windows job (the job installs psmux) + any Windows dev box with an admitted psmux (3.3.8+) and `pwsh` on PATH            |

Never "fix" a gate to call a real CLI, and never add a completion path that trusts LLM output —
sessions complete only on hook Stop events or window death, and the fakes exercise exactly
those paths.

**Manual-gate cadence:** `test_opencode_live.py` is the one gate CI still cannot see. Run it on a
POSIX box with opencode installed before every release, and after any change to the adapter it
covers; a release cut without it is trusting stale evidence. `test_psmux_live.py` left this
category in #662 — the **test-windows** job installs psmux and runs the gate on every pull
request and on pushes to `main`/`release/*` (the workflow's own triggers), in its own serial
step, so its evidence is as fresh as the branch rather than as fresh as someone's memory.

## Ablation records

The rule (AGENTS.md): **for any test asserting "X is refused/absent", delete the gating code
and confirm the test fails before trusting it** — a negative assertion passes for every reason
a value could be absent. The suite records performed ablations in-file, in one house grammar:

- **The canonical form** is a prescriptive, undated sentence in the test's docstring (or a
  nearby comment), naming the exact mutation and the expected failure:

  > Ablation target: delete the `if raw is None: return None` early-out and this test fails
  > alone, on the TypeError `float(None)` raises — which the reader's `except ValueError`
  > deliberately does not catch.

  Accepted spellings: `Ablation:` (the most common), `Ablation target:`/`Ablation targets:`,
  `Ablation guard:`, `Ablations:`, `ablation pin:`, and lettered `ABLATION A1`, `A2`, … where
  a test carries several.

- **`INVERSE ablation:`** — used when deleting the gate would not reproduce the bug (the gate
  is an _absence_, or the bug needs the old behavior restored). The record states the inverse
  mutation: restore the old code, or add what the rule forbids.
- **Green-ablation records** — a small set records that a mutation deliberately does _not_
  redden this test, naming, where one exists, the test that does pin it. That is a durable
  warning against miscounting coverage, not a guard.

Two disciplines around the grammar:

- **Records are evidence, not contracts.** A record certifies the ablation that was run when
  the test was written or last audited. Touching the gate re-opens the question: re-run the
  ablation, against a `cp` backup of the source (never `git checkout` — it has destroyed
  in-flight fixes), and restore byte-identical. Dates, pasted assertion output, and run logs
  belong in the **commit message**, not the file — durable facts in docstrings, records in git
  history. (Three dated `ABLATION (... 2026-07-25)` comments survive in
  `test_opencode_http.py` from before this convention settled; trim them to the undated form
  on next touch.)
- **Ablate singly.** Two gates deleted together redden everything and grade neither; the
  envvars work proved its rejection gates hold disjoint row sets by ablating them one at a
  time — three gates today, four ablations counting the `raw is None` early-out.

A close cousin: a **known defect is pinned with `@pytest.mark.xfail(strict=True)`** naming the
issue (`test_env_fault_patterns.py`, #194). `strict=True` is the point — the day the defect is
fixed, the test fails and forces the debt note to be removed with it.

## Typing and lint

- **Staged pyright, deliberate.** `typeCheckingMode = "basic"` over `src/bmad_loop`, with the
  strict list covering the pure/leaf seven (`model`, `statemachine`, `policy`, `documents`,
  `machine`, `checks`, `sanitize`). Strictness lives where the code is pure; the I/O edges are
  not forced over the strict bar. `src/bmad_loop/data` is excluded — data-shipped scripts are
  standalone templates outside the import graph (with the untested-script consequence #546
  tracks).
- **`tests/` and `scripts/` are outside pyright, deliberately.** Tests get their rigor from
  ablation, parity, and the guards — annotating monkeypatch-heavy test code buys noise, not
  safety. Do not add them to the include list as a drive-by.
- **Pyright is pinned exactly** (`pyright==1.1.411` in the dev group) — it is a gate, and a
  newer pyright adds checks that fail the build on untouched code. Bump the `==` pin
  deliberately; an exact pin is not something a lock refresh can move for you.
- **Ruff rule families are pinned** (`E4`, `E7`, `E9`, `F`) to ruff 0.15.17's default set, so
  CI (trunk's pinned ruff) and a bare newer ruff share rule families — a newer ruff may still
  add rules under those prefixes. **Do not ratchet stricter than CI**
  without moving CI first — the `[tool.ruff.lint]` comment in pyproject.toml is the authority.
- Formatting is trunk's (black/isort/prettier/markdownlint); `trunk check` before every push is
  the contract, and the pre-push hook enforces it.

## CI, flakes, and deliberate absences

Six jobs (`.github/workflows/ci.yml`): **test** (ubuntu, Python 3.11–3.14, tmux installed so
L4 and `stories_e2e` run), **test-windows** (`PYTHONUTF8=1`, psmux installed so the L5
`test_psmux_live.py` gate runs; PRs run the 3.11/3.14 boundary only — Windows failures here
have been platform-shaped, not version-shaped — pushes to `main` and `release/*` run the full
spread), **version-sync**, **lint** (trunk, including actionlint + zizmor over the
workflows themselves), **typecheck** (the same pinned pyright a contributor runs), and
**build** (packaging smoke: sdist + wheel, the console script executed from the installed
wheel, and a wheel data-file inventory against `git ls-files` — every other job runs from the
source tree, so packaging breaks were invisible until this job existed).

**Zero-retry flaky policy.** There is no retry mechanism anywhere: no `--reruns`, no retry
plugin — `pytest-rerunfailures` was removed from the environment precisely because an installed
retry plugin is an invitation to paper over a real flake. **A flake is a bug**: it gets an
issue, a diagnosis, and a deterministic fix — never a rerun loop. The main defense is the
**frozen-clock pattern, mandatory for time-dependent tests**: no unit or seam test sleeps
toward a deadline (the real-process gates use short settle polls, which is different from
waiting out a production timeout). Two sanctioned shapes:

1. A scoped `_Clock` shim over a mutable dict, monkeypatched in as the module-under-test's
   `time` reference; a scripted watcher callback advances the dict past the deadline
   (`test_generic_tmux.py`, with an identically shaped `_install_clock` twin in
   `test_opencode_http.py`).
2. Production parameter injection where the seam already exists: `SignalWatcher.wait_for`
   takes `clock`/`sleep` callables, and the test's fake `sleep` credits the fake clock
   (`test_signals.py`).

Deliberate absences — decisions, not gaps:

- **No coverage gate.** A percentage threshold optimizes for the metric, and this suite's
  riskiest surfaces (guards, ablations, parity) are exactly what line coverage cannot grade.
  Coverage runs on demand when auditing a module:
  `uv run --with pytest-cov pytest --cov=bmad_loop --cov-report=term -q`.
- **No macOS runner.** POSIX portability is held by the seam quarantine plus the portability
  guard; macOS-specific traps found in the field get targeted unit tests (the psutil
  `create_time` trap being the canonical example). A macOS job would mostly re-run the Linux
  suite at 10× the queue time.
- **No opencode install in CI** — so `test_opencode_live.py` is the last manual gate (table
  above), and faking the server would test the fake. psmux is the counter-example rather than
  the precedent: it is one zip on a GitHub release, so **test-windows** installs it and runs
  that gate on every PR and every push to `main`/`release/*` (#662). Chocolatey was tried
  first and dropped — its community feed 503'd on both matrix legs, and Chocolatey document
  it as unguaranteed and rate-limited per IP, which hosted runners share.

## TUI testing

The TUI suite is split by what can regress, across four files:

| File                   | Covers                                                                                                                                                                    | Textual required?                                                                                                                             |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_tui_data.py`     | The pure filesystem observation layer — data correctness, no textual involved                                                                                             | No textual — one test _enforces_ that `tui/data.py` imports none — but the file still needs the `tui` extra (`data.py` imports `pyte`/`rich`) |
| `test_tui_launch.py`   | Exact tmux/CLI argv built by `tui.launch`, against monkeypatched subprocess (plus real-subprocess sanity checks of the captured path); module-wide `force_tmux_backend`   | No                                                                                                                                            |
| `test_tui_app.py`      | Coarse Pilot smoke over the dashboard: mount, run-table population and auto-select, pane polling, keybinding → modal → `tui.launch` wiring (monkeypatched — no real tmux) | Yes                                                                                                                                           |
| `test_tui_settings.py` | `PolicyDoc` edit-model semantics plus Pilot tests of the settings screen (minimal diff on save, invalid values blocked)                                                   | Yes                                                                                                                                           |

Doctrine:

- **Correctness lives below the widget layer.** Data assertions go in `test_tui_data.py`,
  argv assertions in `test_tui_launch.py`; `test_tui_app.py` proves _wiring_, coarsely, with
  the shared `until(pilot, condition)` poll helper. A fine-grained assertion inside a Pilot
  test is usually in the wrong file.
- **Without the `tui` extra, the TUI files fail at collection with ImportError — not skip.
  Deliberate.** The extra is a dev prerequisite (`uv sync --all-extras` in the docs and in
  every CI job that installs the project), so an environment missing it is a broken dev setup
  that should fail loudly, not a legitimate configuration that silently runs ~300 fewer tests
  (the two Pilot files, and `test_tui_data.py` via its `pyte`/`rich` imports —
  `test_cleanup.py` left that set in #650, when the run-inventory reader it
  depends on moved to `runs.py`).
- **Snapshot testing is rejected, with revisit triggers.** `pytest-textual-snapshot` is
  unverified against the textual 8 line this repo pins, and every TUI gap observed so far has
  been behavioral (data, argv, wiring, liveness gates) — exactly what snapshots do not catch,
  while snapshot churn taxes every layout tweak. Revisit if either changes: the plugin proves
  out on textual 8+, or a real regression escapes because it was purely visual
  (#547's attention-pane work is the current best candidate to test that claim).

## Duplicate tests and contract parity

An AST dedup sweep (name collisions plus normalized-body hashes over every test file, PR #540)
informs this policy:

- **A true duplicate is consolidated at the mirror file of the function it covers** — the one
  the sweep found now lives beside its function, superset assertions kept.
- **Identical names across files are otherwise deliberate contract parity.** The
  timeout-instrumentation and token-budget blocks in `test_generic_tmux.py` and
  `test_opencode_http.py` hold identically named tests over two transports: the
  `test_generic_tmux.py` headers state the contract — _a behavior change lands in both or
  records the divergence_ — and the `test_opencode_http.py` headers point back. A cross-link
  must exist on both sides, because a one-way pointer rots; the layered
  `test_envvars.py`/`test_engine.py` pair, which carries the full contract sentence in both
  files, is the model form.
- **Layered coverage is not duplication.** `test_envvars.py` and `test_engine.py` pin the same
  rejection rows at two layers — "what does the parse return" vs "does the policy default
  survive" are different claims — and each states its parity with the other.
- **A shared parametrized contract module is rejected as over-abstraction.** Hoisting the
  parity blocks into one parametrized-over-transport module would hide the transport-specific
  setup that is most of each test's meaning, and the divergence _note_ is the valuable
  artifact. Identical bodies under disjoint `parametrize` sets, and one body exercised against
  two different shipped scripts, were likewise adjudicated deliberate and left alone.

## Gaps and deviations

The honest register. Each row is either tracked or a documented deviation with its rationale.

| Item                                                                                                                                                                                                                                                         | Status                                                                                                                                    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `machine.py` has no mirror test file; `add_json_flag` uncovered; two contract edges unpinned                                                                                                                                                                 | #545                                                                                                                                      |
| `checks.py`, `documents.py`, `fences.py`, `workspace.py` also lack mirror files (`checks` and `documents` being pyright-strict modules); today they are reached only from consumer suites (`test_cli.py`, the conftest harnesses, the engine/worktree files) | Known, untracked — file an issue on next touch if a regression risk surfaces that consumer coverage cannot catch                          |
| `unity_cleanup.py` (data-shipped Unity script) has zero behavioral tests; plugin dispatch guards unpinned                                                                                                                                                    | #546 — test on next touch                                                                                                                 |
| TUI Attention pane render path (append, truncation reset, toast) untested                                                                                                                                                                                    | #547                                                                                                                                      |
| No `filterwarnings` posture; a new DeprecationWarning passes the matrix green                                                                                                                                                                                | #548 — `-W error` triage, then targeted config or a recorded reject here                                                                  |
| "Legal phase transitions live only in `statemachine.py`" is comment-enforced, not guard-enforced: ~14 direct `task.phase =` writes exist outside the state machine, each with a prose ack, inline or in the enclosing docstring                              | Deviation, accepted — the writes are deliberate resets/latches; a guard would need an ack grammar the comments already provide informally |
| Skill-drift guard is CI-inert (fork trees are gitignored, so absent in CI; the guard skips when a tree is absent)                                                                                                                                            | Documented limitation; drift is caught on dev boxes, which is where forks exist                                                           |
| Seven older portability tripwires have no probe matrices; the tmux and SIGKILL detectors flag nothing on today's tree, so nothing grades them                                                                                                                | #549                                                                                                                                      |
| Three dated ablation comments predate the undated house form                                                                                                                                                                                                 | Trim on next touch of `test_opencode_http.py`                                                                                             |

Recently closed by the audit's remediation PRs, for context: the suite's one true duplicate
test and its vestigial `engine.py` re-export (#540); the untested `envvars.py` registry, the
unenforced plugin-env-family invariant, and the TUI hard-stop/archive paths (#541, which also
removed the unused retry plugin and fixed the `inf` session-timeout override it found); CI's
unlinted workflows and untested packaging (#543).

## Policy quick-reference and doc-sync rules

The rules a reviewer actually enforces, in one place:

1. Zero LLM tokens in any test, any layer. Sessions complete on Stop events or window death
   only.
2. New behavior lands at the lowest layer that catches its regression; mirror-file rule for
   placement.
3. Negative assertion ⇒ ablation, singly, against a `cp` backup; record the undated
   `Ablation:`/`Ablation target:` sentence in-file, the run evidence in the commit message.
4. Time-dependent test ⇒ frozen clock. No sleeps toward deadlines, no retries, ever. A flake
   gets an issue.
5. In-process `--json` test expecting a document on stdout ⇒ `machine_json` (whole-stdout
   parse is the purity assertion); error paths that owe an **empty** stdout and `--json
--out` runs that validate the written file sit outside it. Process-boundary tests —
   `test_entry_point.py`, and `test_stories_e2e.py` where it runs `validate --json` —
   spawn a real subprocess and parse its whole stdout themselves; the subprocess _is_ the
   test, so never convert one to `cli.main`.
6. Sandbox test ⇒ `project` fixture; never hand-roll a repo, never touch the template.
7. Cross-transport/cross-layer twins keep identical names and two-way header cross-links.
8. Guard finding ⇒ fix the source; allowlist widening is a reviewed exception with a scoped,
   acknowledged entry — and a new detector shape gets its probe row first.
9. New policy field ⇒ `core.toml` entry (the schema-sync guard will insist). New check id ⇒
   `VALIDATE_CHECKS`. New env var ⇒ `envvars.py` or the plugin's own family.
10. Suite invariants that break silently are AGENTS.md material; everything else about testing
    belongs here.

Doc-sync: **edit this file first.** AGENTS.md's Testing section stays terse — a bullet changes
there only when the underlying rule changes, and it links here (one Testing bullet plus a
docs-index row) rather than duplicating.
CONTRIBUTING.md points contributors here from its Tests bullet. Behavior docs
([FEATURES.md](FEATURES.md)) never carry testing policy. When a gap in the register closes,
delete its row in the same PR.
