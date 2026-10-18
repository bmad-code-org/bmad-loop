# Testing strategy

How bmad-loop is tested: the layer model, the doctrines behind the suite's shape, and the
policies a new test is expected to follow. [AGENTS.md](../AGENTS.md) owns the terse testing
invariants agents must not violate; this document is the strategy behind them: why each rule
exists, where a new test belongs, and which absences are decisions rather than gaps. When the
two disagree, fix this document first and re-derive the terse rule (see
[Doc-sync rules](#policy-quick-reference-and-doc-sync-rules)).

The one-line version: **deterministic tests over a real repo sandbox, zero LLM tokens anywhere,
and every negative assertion proven by ablation.**

## The suite at a glance

- Flat `tests/`: `test_*.py` files mirroring `src/bmad_loop` modules by name, no package
  nesting, one `tests/conftest.py`, and a small `tests/fixtures/` data dir (`stories.yaml`, the
  stories-mode contract, plus the captured claude trust-prompt pane log that
  `test_generic_tmux.py` replays). `tests/fixtures/bmad_v7/` vendors upstream BMAD-METHOD's
  `tickets.py`, `config_utils.py` and `LICENSE-BMAD-METHOD` byte for byte at the commit named in
  `PIN.toml`; `test_vendored_bmad_v7_matches_pin` checks them by sha256.
- Configuration is four settings under `[tool.pytest.ini_options]` in `pyproject.toml`:
  `testpaths = ["tests"]`, `asyncio_mode = "auto"`, `addopts = "--dist loadgroup"`, and
  `filterwarnings`.
- **`--dist loadgroup` is a scheduling declaration.** It makes the `xdist_group` marks on the
  real-tmux E2Es bite; under xdist's default `load` scheduler those marks are silently ignored.
  It lives in `addopts` so a local `-n logical` run schedules exactly as CI does. It makes
  pytest-xdist (a `dev`-group dependency) a prerequisite: a `pytest` from outside the uv
  environment aborts with `unrecognized arguments: --dist`.
- **Deprecations are errors.** `filterwarnings` carries `error::DeprecationWarning` and
  `error::PendingDeprecationWarning`, so a new deprecation fails the test that triggers it. A
  test that expects one uses `pytest.warns(DeprecationWarning)`. A dependency warning we cannot
  fix gets a targeted `ignore:<message regex>:DeprecationWarning:<module>` entry after the two
  `error::` lines, never a blanket category ignore. The filter covers only the pytest process.
- `uv run pytest -q` runs everything runnable on the host. `-n logical` (pytest-xdist) is
  supported because every test builds its state from per-test fixtures. Prefer `logical` over
  `auto`: with `psutil` installed, `auto` resolves to the physical core count, half the vCPUs
  on hosted runners. A test that fails only under xdist load is a flake, which is a bug.
  Live/E2E modules skip themselves when their host requirements are absent.
- **No custom markers, deliberately.** Only builtin marks appear (`parametrize`, `skipif`,
  `usefixtures`, `xfail(strict=True)` to pin a known defect) plus `xdist_group`, which
  pytest-xdist registers itself. A marker registry is a second selection mechanism that can
  drift from the tests it classifies; the filename suffix and the module-level `skipif` encode
  the same fact. `xdist_group` classifies scheduling, not a test category, and appears only on
  the real-tmux E2Es, all through the single `real_mux_e2e` alias in `tests/conftest.py`.

## Layers: where a test belongs

New behavior lands with a test at the **lowest layer that can catch its regression**. The
layers, from cheapest to most expensive:

| Layer                         | What it is                                                                                                                                                                                                                                                                              | Machinery                                                                                                                                                                                                                                                                                       |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **L1 — pure core**            | Unit tests over the pyright-strict modules: `model`, `statemachine`, `policy`, `documents`, `machine`, `checks`, `sanitize`, `plugins/digest`, `plugins/grants`, `worksource`, `ticketmodel`, `ticketstore`. No process, git, or mux I/O; fixtures limited to `tmp_path`/`monkeypatch`. | Plain asserts; exhaustive `parametrize` grids                                                                                                                                                                                                                                                   |
| **L2 — seam**                 | Tests that drive one injection seam with a scripted double.                                                                                                                                                                                                                             | `MockAdapter`, TOML adapter profiles, the `force_tmux_backend` fixture, the injectable `make_adapters` and engine-class parameters of `runsetup.compose_run` (`engine_cls`, `stories_engine_cls`, `tickets_engine_cls`), `compose_sweep` (`sweep_engine_cls`), and `compose_resume` (all three) |
| **L3 — sandbox integration**  | Engine/CLI flows over a real git repo, no multiplexer.                                                                                                                                                                                                                                  | The `project` fixture (a copied template repo) plus the conftest effect helpers                                                                                                                                                                                                                 |
| **L4 — real-mux integration** | A real tmux server drives a scripted shell script standing in for the CLI.                                                                                                                                                                                                              | The `HAVE_TMUX`-gated tests in `tests/test_generic_tmux.py`                                                                                                                                                                                                                                     |
| **L5 — live gates**           | Real external binaries, still zero tokens.                                                                                                                                                                                                                                              | `tests/test_stories_e2e.py`, `tests/test_tickets_e2e.py`, `tests/test_opencode_live.py`, `tests/test_opencode_live_turn.py`, `tests/test_psmux_live.py`                                                                                                                                         |

Placement rules:

- **Mirror-file rule.** A test for `src/bmad_loop/<module>.py` lives in `tests/test_<module>.py`;
  subpackage modules flatten into prefixed names (`tui/app.py` → `test_tui_app.py`,
  `plugins/loader.py` → `test_plugin_loader.py`), and adapter internals are covered through
  their transport suites. Cross-module flows live with the module that owns the behavior under
  assertion (engine flows in `test_engine.py`, CLI contract in `test_cli.py`). A module without
  a mirror file is a smell; see [Gaps and deviations](#gaps-and-deviations).
- **Suffix convention is the selection mechanism.** `*_e2e.py` and `*_live.py` name the modules
  with host requirements; each carries a module-level `pytestmark = pytest.mark.skipif(...)`
  naming its requirement (`HAVE_TMUX`, `HAVE_OPENCODE`, `HAVE_OPENCODE_V2`, `HAVE_PSMUX`).
  `HAVE_OPENCODE` runs the binary's `--version`, so a resolvable but dead shim skips instead
  of failing; `HAVE_OPENCODE_V2` also requires that version to be 2.x. Ordinary runs
  collect and skip them; a capable host runs them with no extra flags. Do not add a marker, an
  env-var opt-in, or a separate pytest invocation for these.
- **Prefer a lower layer over a broader one.** If a defect is expressible as a pure-core case,
  do not pin it in a sandbox flow: L3+ tests are slower, and their failures triangulate worse.
  The inverse also holds: a seam contract (say, adapter timeout instrumentation) cannot be
  proven by an L1 test and belongs at L2 with a parity twin (see
  [Duplicate tests and contract parity](#duplicate-tests-and-contract-parity)).

## Fixtures and helpers

`tests/conftest.py` holds **fixtures for lifecycle and isolation only**; everything else is a
plain function or constant a test imports by name. The fixtures:

- **Sandboxes.** `project` is the workhorse: a disposable copy of a session-scoped template
  repo (`_project_template`, built once per xdist worker) with BMAD-shaped artifact dirs and an
  initial commit. Never hand-roll a temp repo and never touch the template: a mutation there
  poisons every later test in the worker. `project_tree` is the same file tree with no Git,
  opted into only by tests whose code path never consults a repository. `project_v7` is the
  BMAD v7 twin: a copy of a per-worker v7 template with the vendored `tickets.py` installed and
  the ticket tree tracked in the checkout (layout T1). `project_v7_store_repo` (T3) and
  `project_v7_external_store` (T4) derive from it. Plain helpers build the rest:
  `install_bmad_v7`, `v7_layout`/`V7Layout`, `write_ticket_tree`, and `FakeTicketTree` for unit
  tests that must not spawn `tickets.py`.
- **Backend pins.** `force_tmux_backend` and `force_psmux_backend` pin the transport backend
  by name and clear the selection cache, so tests asserting backend argv cannot be hijacked by
  the host's default or an installed external backend.
- **Autouse isolation.** `_isolate_ambient_git_ignores` (session-scoped) shadows the
  developer's global gitignore sources (`GIT_CONFIG_GLOBAL`, `XDG_CONFIG_HOME`), so a dev box
  that globally ignores `.claude/` cannot make shield tests measure the wrong thing.
  `_isolate_state_root` points `BMAD_LOOP_STATE_DIR` at a per-test directory (allocated by
  `_state_root_allocator`), because `runs.state_root()` creates directories in the real state
  location. `_isolate_mux_registry` keeps the psmux registry root and the inside-a-pane marker
  out of the cross-test environment.
- **Per-test helpers that need teardown.** `emulate_windows_newlines` makes `Path.write_text`
  translate newlines as Windows does, so a "no CRLF" assertion can fail on Linux.
  `reap_leftovers` force-kills pids a failing row may have leaked.

Everything else is a **plain helper** (`from conftest import write_spec, dev_effect,
machine_json, ...`). A fixture is ambient, invisible at the call site and to grep, so it is
reserved for setup and teardown that must wrap the test. Construction and simulation helpers
are explicit imports, so each test names its dependencies. Local fixtures inside a single test
file are fine for file-scoped concerns.

Two helpers carry suite-wide contracts:

- `machine_json(argv, capsys, *, rc=0, err_contains=None)` runs a `--json` command and parses
  the **whole** of stdout. Parsing the full stream is itself the assertion that nothing but the
  document was printed (the `machine.py` purity contract); `err == ""` is the default, and
  `err_contains` opts into a different assertion, never a waiver.
- `dev_effect` / `review_effect` and their bundle twins simulate what a real skill session does
  to disk, and just as deliberately what it does **not** do: `dev_effect` never writes
  sprint-status, because the orchestrator is the single sprint-status writer at dev time.
  `review_effect` advances the board when it finalizes, as real review sessions do; the bundle
  twins write none, because bundles have no sprint-status entry.

Divergent-root tests choose a topology for the distinction they need to prove:

- **Default** (`project`) keeps `project == repo_root`. Use it for ordinary sandbox behavior,
  never for an assertion that claims to distinguish those roots.
- **Sibling** places `repo_root` outside the BMAD project (`write_repo_root_override`, reloaded
  through `load_paths`). Use it to prove that code commands run in the configured root while
  artifact reads stay in the project. It cannot grade pathspec selection by outcome alone.
- **Nested monorepo** (`nested_repo_root_paths`) puts the BMAD project at `<repo>/app`. Use it
  when both right- and wrong-root pathspecs must be non-empty and separable.
- **Isolated worktree** adds a third live root. Use a real relative command against a marker
  created only in the mounted unit worktree, and assert the main checkout lacks it.

**`MockAdapter` is production code** (`src/bmad_loop/adapters/mock.py`, shipped in the wheel),
scripted with a list of `SessionResult`s or `callable(spec) -> SessionResult` effects. No
configuration reaches it, and it exercises **the generic adapter path only**: adapter-specific
behavior needs an L2 profile test or an L4/L5 gate. It carries no `profile` attribute on
purpose; `attach_profile` in conftest opts in where the resolved skill name is under test.

## Premise before outcome

A test asserts its **premise before its outcome**: before checking the result, it proves the
setup is the situation the test claims to exercise. A negative outcome ("nothing was
published", "the gate refused") passes for every reason the value could be absent, including
a setup that never reached the code under test.

- Assert the planted state is really on disk (the entry is `done`, the cache is valid, the
  tree starts clean) before acting on it.
- For divergent-root tests, compare resolved roots, assert the nesting when it matters, and
  plant markers in both directions (`plant_root_markers`, `REPO_ROOT_MARKER_CMD`,
  `PROJECT_MARKER_CMD`): one identifies the intended root, the other rules out the tempting
  alternative.
- When a wrong answer could still name a real path, create that decoy and assert the selected
  value, rather than asserting only that some path is absent.

## Quality guards (meta-tests)

A slice of the suite tests the **repo** rather than the product. Each guard's test docstrings
carry its full rationale and blind spots.

| Guard                         | Test file                                               | What it prevents                                                                                                                                          |
| ----------------------------- | ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Portability guard             | `tests/test_portability_guard.py`                       | tmux argv, git spawns, POSIX-only calls, `BMAD_LOOP_*` env reads, spec-path anchoring and encoding-less text I/O escaping their sanctioned seams          |
| Kind & refusal inventories    | `tests/test_portability_guard.py`                       | an undeclared journal kind, `patch` carrier, splat hole, `_refuse_*`/`_reject_*` helper or isolation-refusal call site landing without a PR-time decision |
| Settings-schema sync          | `tests/test_settings_schema.py`                         | `core.toml` drifting from the `policy.py` dataclasses: a spec without a live field, a mismatched default, or an unclassified field                        |
| Exit-code allocation          | `tests/test_entry_point.py`                             | renumbering an `ExitCode` or quietly allocating one outside {0, 1, 2, 130}                                                                                |
| Extra-less core CLI           | `tests/test_entry_point.py`                             | the core CLI crashing without the `[tui]` packages; `tui` must degrade to an install hint instead of a traceback                                          |
| State-machine table           | `tests/test_statemachine.py`                            | a `Phase` without a transition row, terminal phases out of step with the table's dead ends, or an illegal move that lands                                 |
| Check-id registry             | `checks.py` + `tests/test_cli.py`                       | a `validate` check id emitted on an executed path without being in `VALIDATE_CHECKS` (exercised paths only; no static scan)                               |
| Skill-drift guard             | `tests/test_module_skills_sync.py`                      | seeded `.claude/skills` and `.agents/skills` forks drifting from the canonical skills (dev boxes only; CI has no forks)                                   |
| Schema-version parity         | `tests/test_tui_app.py`                                 | the TUI rendering a newer validate document schema as the old one after a CLI bump                                                                        |
| Installed-copy drift          | `tests/test_hook_script.py`, `tests/test_probe_hook.py` | the packaged hook relays drifting from their source (wheel-installed runs only), or `init` writing a workspace relay copy                                 |
| Version sync                  | `tests/test_release.py` + CI                            | version fields disagreeing, or a release leaving `## [Unreleased]` and its compare link unreopened                                                        |
| Real-mux xdist grouping       | `tests/test_conftest.py`                                | real-tmux E2Es scheduled onto concurrent workers: each must carry exactly the `real_mux_e2e` group, honored by `--dist loadgroup`                         |
| Tmux SessionSpec + reap walls | `tests/test_conftest.py`                                | load-sensitive literal timeouts in real-tmux waits; they must use `REAL_MUX_HANG_CEILING_S`                                                               |
| Deprecation posture           | `tests/test_conftest.py`                                | deprecation warnings passing silently, including through a later blanket `ignore` that leaves the `error::` entries inert                                 |
| Docs hygiene                  | `tests/test_docs_hygiene.py`                            | broken relative links and anchors in the docs, and a stale generated policy table in the configuration reference                                          |

Rules for adding or touching a guard:

- **Fix the source, not the allowlist.** A new finding means routing the call through the seam;
  widening an allowlist is the last resort and needs the same scrutiny as a production change.
- **Exemptions are scoped and acknowledged.** Most file-keyed allowlists (paths, detach,
  kill-probe, `shell=True`) also require a per-line `# portability:` ack. Whole-file passes
  exist only where the file _is_ the sanctioned spot (the tmux backends; `process_host.py` for
  `os.kill`), and `verify.py`'s git exemption is narrowed to the `_run_git` argv position.
  Env-read exemptions are scoped **by variable name or family, never by file**.
- **The detector itself gets executable coverage.** Source detectors are split (`_scan_source`
  and its siblings) so probe fixtures run the same code path as the real scan, with a
  must-flag row per claimed access form and a must-stay-silent row per lookalike. When a new
  form turns up, add the failing probe row first, then fix the detector. A green "no findings
  today" assertion cannot grade a detector: it is equally green when the scan silently stopped
  scanning.
- **The guard's boundary is `src/bmad_loop`,** enforced by its scan root rather than an
  allowlist. Guards whose subject is the suite's own scheduling and waits are rooted at
  `tests/` and live in `tests/test_conftest.py`, so the portability guard's scan root stays
  the boundary. Tests, `scripts/`, and CI workflows deliberately spawn their own git: a harness
  must not depend on the artifact it validates.

## The zero-token invariant

**No test, at any layer, ever spends an LLM token.** AGENTS.md states the hard invariant for
the live/E2E gates; in practice it holds everywhere, and the mechanism differs per gate:

| Gate                                 | Real component                                                | Zero-token mechanism                                                                                                                                                                                                                                                                                                                                       | Runs where                                                        |
| ------------------------------------ | ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `test_generic_tmux.py` (`HAVE_TMUX`) | tmux server                                                   | The spawned "CLI" is a tiny shell script written by the test                                                                                                                                                                                                                                                                                               | CI Linux job + any POSIX dev box with tmux                        |
| `test_stories_e2e.py`                | tmux + the real `bmad-loop run/resolve/resume` CLI            | Scripted fake `claude` variants (bash string constants) wired in as a custom TOML profile; the fake writes its own SessionStart/Stop hook events                                                                                                                                                                                                           | CI Linux job (the fakes use GNU coreutils + `setsid`)             |
| `test_tickets_e2e.py`                | tmux + the real `bmad-loop` CLI and the vendored `tickets.py` | A scripted fake `bmad-build-auto` (bash) resolves its ticket through `tickets.py find`, builds to `built` and writes its own SessionStart/Stop hook events; it never marks `done`. 14 rows cover the pauses, checkpoints, retro and closure, resolve, isolation, store repos, refusals and an isolated sweep, several parametrized over `topology` (T1–T4) | CI Linux job                                                      |
| `test_opencode_live.py`              | A real `opencode serve` HTTP server                           | **Never sends a prompt**: only spawn/teardown and the health/OpenAPI/session/event endpoints of the detected major (1.x or 2.x); the prompt endpoint is checked against the OpenAPI schema, never called                                                                                                                                                   | Manual: a POSIX box with a runnable `opencode`                    |
| `test_opencode_live_turn.py`         | A real OpenCode 2.x server running one full turn              | **Prompts reach only a local fake**: the one test that prompts sends every LLM request to an in-process stdlib OpenAI-compatible server on 127.0.0.1, under a scrubbed env (scratch HOME/XDG, no credentials), a mock-only provider config and a deny on the hosted provider, and asserts the fake saw every request                                       | Manual: a POSIX box with a runnable OpenCode 2.x `opencode`       |
| `test_psmux_live.py`                 | A real psmux on Windows                                       | **Parked windows only**: every window runs `pwsh -NoProfile -Command exit 0`. `test_premise_*` probes are inverted (red means a workaround became droppable); `test_adopted_*` probes are ordinary                                                                                                                                                         | CI Windows job + any Windows dev box with psmux 3.4.0+ and `pwsh` |

Never "fix" a gate to call a real CLI, and never add a completion path that trusts LLM output:
sessions complete only on hook Stop events or window death, and the fakes exercise exactly
those paths.

**Manual-gate cadence:** the two `test_opencode_live*.py` modules are the gates CI cannot run.
Run them on a POSIX box before every release and after any change to the adapter they cover,
once with an OpenCode 1.x and once with a 2.x `opencode` first on PATH
(`PATH=<dir>:$PATH uv run pytest tests/test_opencode_live.py tests/test_opencode_live_turn.py -q`).

## Ablation records

The rule (AGENTS.md): **for any test asserting "X is refused/absent", delete the gating code
and confirm the test fails before trusting it.** The suite records performed ablations
in-file, in one house grammar:

- **The canonical form** is a prescriptive, undated sentence in the test's docstring (or a
  nearby comment), naming the exact mutation and the expected failure:

  > Ablation target: delete the `if raw is None: return None` early-out and this test fails
  > alone, on the TypeError `float(None)` raises — which the reader's `except ValueError`
  > deliberately does not catch.

  Accepted spellings: `Ablation:` (the most common), `Ablation target:`/`Ablation targets:`,
  `Ablation guard:`, `Ablations:`, `ablation pin:`, and lettered `ABLATION A1`, `A2`, … where
  a test carries several.

- **`INVERSE ablation:`** is used when deleting the gate would not reproduce the bug (the gate
  is an _absence_, or the bug needs the old behavior restored). The record states the inverse
  mutation: restore the old code, or add what the rule forbids.
- **Green-ablation records** note that a mutation deliberately does _not_ redden this test,
  naming the test that does pin it where one exists: a warning against miscounting coverage.

Two disciplines around the grammar:

- **Records are evidence, not contracts.** A record certifies the ablation run when the test
  was written or last audited. Touching the gate re-opens the question: re-run the ablation
  against a `cp` backup of the source (never `git checkout`, which can destroy in-flight
  fixes), and restore byte-identical. Dates, pasted assertion output, and run logs belong in
  the **commit message**, not the file.
- **Ablate singly.** Two gates deleted together redden everything and grade neither.

A **known defect is pinned with `xfail(strict=True)`**, its reason naming the defect.
`strict=True` is the point: the day the defect is fixed, the test fails and forces the note to
be removed with it.

## Typing and lint

- **Staged pyright, deliberate.** `typeCheckingMode = "basic"` over `src/bmad_loop`, with the
  strict list covering the pure/leaf modules (`model`, `statemachine`, `policy`, `documents`,
  `machine`, `checks`, `sanitize`, `plugins/digest`, `plugins/grants`, `worksource`, `ticketmodel`). Strictness lives where
  the code is pure. `src/bmad_loop/data`
  is excluded: data-shipped scripts are standalone templates outside the import graph.
- **Three `os` surfaces.** CI also runs pyright with `--pythonplatform Windows` and `Darwin`,
  whose `os` stubs lack POSIX/Linux-only names (`O_DIRECTORY`, `setxattr`, `sysconf`). Code that
  needs such a name fetches it with a default-free `getattr` inside its runtime check.
- **`tests/` and `scripts/` are outside pyright, deliberately.** Tests get their rigor from
  ablation, parity, and the guards. Do not add them to the include list as a drive-by.
- **Pyright is pinned exactly** in the dev group: it is a gate, and a newer pyright adds checks
  that fail the build on untouched code. Bump the `==` pin deliberately.
- **Ruff rule families are pinned** (`E4`, `E7`, `E9`, `F`), so CI (trunk's pinned ruff) and a
  bare newer ruff share rule families. **Do not ratchet stricter than CI** without moving CI
  first; the `[tool.ruff.lint]` comment in pyproject.toml is the authority.
- Formatting is trunk's (black/isort/prettier/markdownlint); `trunk check` before every push is
  the contract, and the pre-push hook enforces it.

## CI, flakes, and deliberate absences

Eight jobs (`.github/workflows/ci.yml`):

- **test**: ubuntu, Python 3.11–3.14, tmux installed so L4, `stories_e2e` and `tickets_e2e` run.
- Three Windows jobs (`PYTHONUTF8=1`). Pull requests run the 3.11/3.14 boundary only; pushes
  to `main` and `release/*` run the full spread.
- **version-sync**: `scripts/release.py check`.
- **lint**: trunk, including actionlint and zizmor over the workflows themselves.
- **typecheck**: the same pinned pyright a contributor runs, plus the Windows and Darwin
  target checks.
- **build**: packaging smoke. Builds the sdist and wheel, runs the console script from the
  installed wheel, and checks the wheel's data files against `git ls-files`; every other job
  runs from the source tree.

The Windows jobs split one leg across machines without dropping a test:

- **test-windows-shard** runs the suite minus `test_psmux_live.py` as two shards per Python
  leg, `--ci-shard=1/2` and `2/2` (`tests/perf_report.py`). A shard receives whole units: a
  test function with every parameter variant, or a whole xdist group, so a real-mux group can
  never split. Units are assigned by a SHA-256 digest of the unit's nodeid, never Python's
  randomized `hash()` or a file list, so a new test is assigned automatically and existing
  tests do not move. The digest balances test counts, not seconds.
- **test-windows-live** installs one pinned psmux release and runs the L5 gate once per leg,
  serially, on a machine of its own. The pin, not `latest`: the gate's `test_premise_*` rows
  turn red by design when a psmux release fixes what they pin, so an upstream release must
  never reach a PR's checks. Moving the pin is its own PR, which also moves the version floor
  and retires the workarounds. The non-required **psmux canary** workflow (weekly, or on
  demand) runs the same gate against the latest release and goes red when that PR is due.
- **test-windows** keeps the check name `test (windows, py3.x)` and is the leg's verdict.
  `perf_report.py verify-shards --count 2` requires both shards present and green, their
  selections disjoint, and their union equal to the collected set, with no function family
  straddling shards. `verify-executed` requires every live-gate test to have passed; a skip
  there means the gate did not run. `verify-jobs` requires each of the leg's jobs to have
  concluded `success` in its latest execution, so a re-run that fails before uploading cannot
  pass on an earlier attempt's records.

Sharding buys latency, not work: every shard pays its own runner setup and a full collection.
It stays only while the extra aggregate job-seconds remain within 15% of the unsharded layout.

**Zero-retry flaky policy.** There is no retry mechanism anywhere: no `--reruns`, no retry
plugin, because an installed retry plugin is an invitation to paper over a real flake. **A
flake is a bug**: it gets a diagnosis and a deterministic fix, never a rerun loop. The main
defense is the **frozen-clock pattern, mandatory for time-dependent tests**: no unit or seam
test sleeps toward a deadline (the real-process gates use short settle polls, which is
different from waiting out a production timeout). Two sanctioned shapes:

1. A scoped `_Clock` shim over a mutable dict, monkeypatched in as the module-under-test's
   `time` reference; a scripted watcher callback advances the dict past the deadline
   (`test_generic_tmux.py`, with an identically shaped `_install_clock` twin in
   `test_opencode_http.py`).
2. Production parameter injection where the seam already exists: `SignalWatcher.wait_for`
   takes `clock`/`sleep` callables, and the test's fake `sleep` credits the fake clock
   (`test_signals.py`).

Deliberate absences, decisions rather than gaps:

- **No coverage gate.** A percentage threshold optimizes for the metric, and this suite's
  riskiest surfaces (guards, ablations, parity) are exactly what line coverage cannot grade.
  Coverage runs on demand when auditing a module:
  `uv run --with pytest-cov pytest --cov=bmad_loop --cov-report=term -q`.
- **No macOS runner.** POSIX portability is held by the seam quarantine plus the portability
  guard; macOS-specific traps found in the field get targeted unit tests (the psutil
  `create_time` trap is the canonical example).
- **No WSL2 runner.** Hosted runners cannot host WSL2, so run the suite there by hand, from a
  clone inside the WSL filesystem: a Windows worktree reached through `/mnt/c` carries a `C:/`
  gitdir that WSL git cannot resolve. A test that spawns an interpreter uses `sys.executable`,
  never a bare `python` (Ubuntu ships only `python3`). uv's standalone CPython has no
  `os.pidfd_open`, so the pidfd reap rows skip via `conftest.needs_pidfd`; to run them, build
  the venv on the distro Python with `uv venv -p /usr/bin/python3`.
- **No opencode install in CI**, so the `test_opencode_live*.py` modules are the last manual
  gate (see
  [The zero-token invariant](#the-zero-token-invariant)); faking the server would test the
  fake. psmux, one zip on a GitHub release, is installed by **test-windows-live** instead.

## Runtime, metrics, and performance budgets

**Default invocation is serial.** `uv run pytest -q` runs one process: correct everywhere,
slowest everywhere. Parallelism is opt-in with `-n logical`. Compare like with like: a serial
local run against a four-worker hosted run measures the worker count, not the change.

**The 2-vCPU Windows comparison** (a small VM or WinBoat guest) is two commands, in this order,
never alongside another full-suite run on the same guest:

```powershell
$env:PYTHONUTF8 = '1'
uv run pytest -q -n 2 --ignore=tests/test_psmux_live.py
uv run pytest -q tests/test_psmux_live.py
```

Keep the checkout and `TMP`/`TEMP` on the guest's own disk; a host-shared or network folder
measures the share. The live gate runs separately and serially because its tests drive real
psmux servers, a single-machine resource.

**Metrics** (`tests/perf_report.py`, inert unless asked for) answer where the time went,
including for a run that never finished:

```sh
uv run python scripts/run_test_benchmark.py --metrics-dir <fresh-dir> -- -q -n logical
uv run python tests/perf_report.py summarize <fresh-dir>
```

The wrapper owns the pytest process lifetime, preserves pytest's exit status, and with
`--timeout`/`--deadline-epoch` stops the whole tree (exit 124) while leaving every flushed
record behind. Pytest alone takes `--test-metrics-dir=PATH`, spelled with `=`. The summary
keeps wall-clock elapsed apart from _worker-seconds_ summed across xdist workers, and reports
fixture setup totals, per-file cost, the cleanup tail, and tests started but never finished.
`--test-metrics-subprocesses` adds per-test, per-command and per-call-site launch counts; it
has real overhead and stays off in CI. Recorded data carries no environment values, output, or
argv beyond a program name and git verb. Metrics directories are run artifacts: CI uploads
them (`test-metrics-*`) and they never go into Git.

**Budgets.** Each test job has a hard `timeout-minutes` cap, and its test steps stop a few
minutes short of it through the wrapper's deadline, so a wedged run ends as a red step with its
metrics uploaded rather than as a cancelled job. Change a cap and its deadline arithmetic
together (the workflow comments name both). The engineering targets for the Windows legs are
an ordinary-suite median of at most ten minutes and a completed Windows check within fifteen,
judged on at least three hosted samples per boundary Python version. These are targets, not
guarantees. A red sample is recorded as a result, never rerun into a green one.

## TUI testing

The TUI suite is split by what can regress, across four files:

| File                   | Covers                                                                                                                         | Textual required?                                                                       |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `test_tui_data.py`     | The pure filesystem observation layer: data correctness                                                                        | No (one test enforces that `tui/data.py` imports no textual), but needs the `tui` extra |
| `test_tui_launch.py`   | Exact tmux/CLI argv built by `tui.launch`, against monkeypatched subprocess; module-wide `force_tmux_backend`                  | No                                                                                      |
| `test_tui_app.py`      | Coarse Pilot smoke over the dashboard: mount, run table, pane polling, keybinding → modal → `tui.launch` wiring (no real tmux) | Yes                                                                                     |
| `test_tui_settings.py` | `PolicyDoc` edit-model semantics plus Pilot tests of the settings screen (minimal diff on save, invalid values blocked)        | Yes                                                                                     |

Doctrine:

- **Correctness lives below the widget layer.** Data assertions go in `test_tui_data.py`,
  argv assertions in `test_tui_launch.py`; `test_tui_app.py` proves _wiring_, coarsely, with
  the shared `until(pilot, condition)` poll helper. A fine-grained assertion inside a Pilot
  test is usually in the wrong file.
- **Without the `tui` extra, the TUI files fail at collection with ImportError, not skip.
  Deliberate.** The extra is a dev prerequisite (`uv sync --all-extras`), so an environment
  missing it is a broken dev setup that should fail loudly rather than silently run hundreds
  fewer tests.
- **Snapshot testing is rejected, with revisit triggers.** `pytest-textual-snapshot` is
  unverified against the textual 8 line this repo pins, and every TUI gap observed so far has
  been behavioral (data, argv, wiring, liveness gates), which snapshots do not catch, while
  snapshot churn taxes every layout tweak. Revisit if the plugin proves out on textual 8+, or
  a real regression escapes because it was purely visual.

## Duplicate tests and contract parity

- **A true duplicate is consolidated at the mirror file of the function it covers**, superset
  assertions kept.
- **Identical names across files are otherwise deliberate contract parity.** The
  timeout-instrumentation and token-budget blocks in `test_generic_tmux.py` and
  `test_opencode_http.py` hold identically named tests over two transports: the
  `test_generic_tmux.py` headers state the contract (_a behavior change lands in both or
  records the divergence_) and the `test_opencode_http.py` headers point back. A cross-link
  must exist on both sides, because a one-way pointer rots.
- **Layered coverage is not duplication.** `test_envvars.py` and `test_engine.py` pin the same
  rejection rows at two layers ("what does the parse return" vs "does the policy default
  survive" are different claims), and each states its parity with the other.
- **A shared parametrized contract module is rejected as over-abstraction.** Hoisting the
  parity blocks into one module parametrized over transport would hide the transport-specific
  setup that is most of each test's meaning, and the divergence _note_ is the valuable
  artifact.

## Gaps and deviations

The honest register. Each row is a known gap or a documented deviation with its rationale.

| Item                                                                                                                                                                                    | Status                                                                                           |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `machine.py` has no mirror test file                                                                                                                                                    | Known gap                                                                                        |
| `checks.py`, `documents.py`, `fences.py`, `workspace.py` lack mirror files and are reached only from consumer suites (`test_cli.py`, the conftest harnesses, the engine/worktree files) | Known gap; add a mirror file when a regression risk surfaces that consumer coverage cannot catch |
| `unity_cleanup.py` (data-shipped Unity script) has no behavioral tests                                                                                                                  | Known gap; test on next touch                                                                    |
| TUI Attention pane render path (append, truncation reset, toast) is untested                                                                                                            | Known gap                                                                                        |
| "Legal phase transitions live only in `statemachine.py`" is comment-enforced, not guard-enforced: direct `task.phase =` writes outside the state machine each carry a prose ack         | Deviation, accepted: the writes are deliberate resets and latches                                |
| Skill-drift guard is CI-inert (fork trees are gitignored, so absent in CI)                                                                                                              | Documented limitation; drift is caught on dev boxes, where forks exist                           |
| Three dated ablation comments in `test_opencode_http.py` predate the undated house form                                                                                                 | Trim on next touch                                                                               |

## Policy quick-reference and doc-sync rules

The rules a reviewer actually enforces, in one place:

1. Zero LLM tokens in any test, any layer. Sessions complete on Stop events or window death
   only.
2. New behavior lands at the lowest layer that catches its regression; mirror-file rule for
   placement.
3. Negative assertion ⇒ premise before outcome, and ablation, singly, against a `cp` backup;
   record the undated `Ablation:`/`Ablation target:` sentence in-file, the run evidence in the
   commit message.
4. Time-dependent test ⇒ frozen clock. No sleeps toward deadlines, no retries, ever. A flake is
   diagnosed and fixed deterministically.
5. In-process `--json` test expecting a document on stdout ⇒ `machine_json` (whole-stdout
   parse is the purity assertion). Error paths that owe an **empty** stdout, and `--json --out`
   runs that validate the written file, sit outside it. Process-boundary tests
   (`test_entry_point.py`, and `test_stories_e2e.py` where it runs `validate --json`) spawn a
   real subprocess and parse its whole stdout themselves; never convert one to `cli.main`.
6. Sandbox test ⇒ `project` fixture; never hand-roll a repo, never touch the template.
7. Cross-transport/cross-layer twins keep identical names and two-way header cross-links.
8. Guard finding ⇒ fix the source; allowlist widening is a reviewed exception with a scoped,
   acknowledged entry, and a new detector shape gets its probe row first.
9. New policy field ⇒ `core.toml` entry (the schema-sync guard will insist). New check id ⇒
   `VALIDATE_CHECKS`. New env var ⇒ `envvars.py` or the plugin's own family.
10. Suite invariants that break silently are AGENTS.md material; everything else about testing
    belongs here.

Doc-sync: **edit this file first.** AGENTS.md's Testing section stays terse: a bullet changes
there only when the underlying rule changes, and it links here rather than duplicating.
CONTRIBUTING.md points contributors here from its Tests bullet. Behavior docs
([FEATURES.md](FEATURES.md)) never carry testing policy. When a gap in the register closes,
delete its row in the same PR.
