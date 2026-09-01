---
title: 'DW-56 through DW-63: harden divergent-root fixtures and cwd seams'
type: 'chore'
created: '2026-09-01'
baseline_revision: '928270f6569e3cf51149dc7e4f79426326f498db'
baseline_commit: '928270f6569e3cf51149dc7e4f79426326f498db'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - 'docs/testing.md'
warnings: ['multiple-goals', 'oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** The divergent-root test family does not yet model its supported sibling, nested-monorepo, and isolated-worktree shapes end to end. The nested helper is not config-loadable or robust to symlinked temp roots, carries an incomplete init ignore shape, and leaves restore-patch and isolated verify-command cwd seams under-specified; seven older mocks still accept any cwd, while production and testing prose overgeneralize the sibling topology.

**Approach:** Make the nested builder create and load a canonical project configuration, commit the complete init-like seed, and add non-blind seam and fixture-contract coverage. Pin verify execution and classification to an isolated unit worktree, make all existing command mocks assert their expected root, update topology-aware production prose, and document when each divergent-root shape is appropriate.

## Boundaries & Constraints

**Always:** Preserve production behavior; resolve and return canonical fixture paths; commit every nested seed file that could otherwise count as proof of work; use real relative commands for the isolated-worktree cwd row; pin both command execution and result classification; retain existing sibling and nested coverage; assert the specific refusal or path selected rather than absence alone; use portable helpers and resolved-path comparisons; add tests at the lowest relevant seam.

**Never:** Do not edit `_bmad-output/implementation-artifacts/deferred-work.md` or any other deferred-work ledger. Do not add a new production completion path, change the supported `repo_root`/worktree-isolation policy, call a real coding CLI, replace the sibling shape, or let test residue satisfy proof-of-work. Do not change the behavior of `verify_dev_exclude_relpaths`, `_stories_relpaths`, or `Engine._verify_commands_with_results` unless a test exposes an actual defect rather than a coverage gap.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Config-loaded nested monorepo | Outer git root with BMAD project under `app/` | `load_paths(app)` returns canonical paths with outer `repo_root` and nested artifact roots | Fixture assertions fail before a misleading test runs |
| Symlink/non-canonical temp root | Nested builder receives an alias spelling | Every returned path is canonical and nested pathspecs remain non-empty | No lexical/physical mismatch is hidden as `()` |
| Nested restore patch | Relative `restore.patch` exists at both outer and nested roots | Exclusion rooted at `repo_root` selects the intended outer candidate, not the nested decoy | Wrong-root anchoring fails the value assertion |
| Isolated verify commands | Worktree isolation and a relative marker created only in the unit worktree | Execution and classification use the mounted unit worktree; dev verification succeeds | Main-checkout execution fails specifically at the command seam |
| Existing scripted verify mocks | Default workspace with mocked command results | Each mock asserts cwd resolves to `project.repo_root` before returning its scripted result | Any caller-root regression fails at the mock boundary |

</intent-contract>

## Code Map

- `src/bmad_loop/verify.py` -- `verify_dev_exclude_relpaths` and `_stories_relpaths` behavior is correct; update their sibling-only docstrings to describe both disjoint sibling and nested-monorepo outcomes. Keep implementations unchanged unless new coverage proves otherwise.
- `src/bmad_loop/engine.py` -- `_verify_commands_with_results` passes `self.workspace.root` to both `run_verify_commands` and `verify_command_results_outcome`; read-only expected behavior for the new isolation test.
- `src/bmad_loop/worktree_flow.py` -- `WorktreeFlow.run_isolated` swaps `engine.workspace` to the unit worktree before driving a story, establishing the third-root contract; read-only.
- `src/bmad_loop/bmadconfig.py` -- `load_paths` canonicalizes config-derived paths and `worktree_isolation_conflict` refuses divergent roots only with worktree isolation; reuse in fixture contract tests.
- `src/bmad_loop/install.py` -- `install_into` owns the canonical four ignore entries: `.bmad-loop/runs/`, `.bmad-loop/cache/`, `.bmad-loop/policy.toml`, and `_bmad/render/`; use as the observable reference without coupling tests to private implementation unnecessarily.
- `tests/conftest.py` -- `_file_exists_cmd`, `write_repo_root_override`, `plant_root_markers`, and `nested_repo_root_paths` are the shared seams. Canonicalize the outer root, write nested config, seed the complete ignore file, commit all seed/config files, return `load_paths(app)`, and add a cwd-asserting scripted-command helper.
- `tests/test_conftest.py` -- add exact config-load/canonicalization, non-canonical alias, ignore-shape, and isolation-conflict contracts for the nested builder.
- `tests/test_verify.py` -- preserve sibling rows and existing monorepo value/outcome rows; add nested `restore_patch` coverage that distinguishes the intended outer file from a real nested decoy.
- `tests/test_engine_worktree.py` -- reuse `wt_policy`, `commit_sprint`, `wt_dev_effect`, and `make_engine` for a full isolated run with a real relative marker command; spy only on the classifier while delegating to it.
- `tests/test_engine.py` -- replace the six `lambda policy, cwd:` command mocks with the shared cwd-asserting helper while preserving each result iterator and scenario; update stale nearby prose.
- `tests/test_hook_bus.py` -- replace its one cwd-blind command mock with the same helper.
- `docs/testing.md` -- extend the fixture/helper doctrine with sibling versus nested selection, config-loaded overrides, two-direction marker probes, and required premise guards.

## Tasks & Acceptance

**Execution:**
- [x] `tests/conftest.py`, `tests/test_conftest.py` -- make `nested_repo_root_paths` canonical and loadable through `_bmad/bmm/config.yaml`, seed and commit the full init ignore/config shape, return `load_paths`, and pin these contracts including an intentionally non-canonical input spelling -- prevents platform aliases and fixture residue from hiding failures.
- [x] `src/bmad_loop/verify.py`, `tests/test_verify.py` -- make docstrings topology-aware and exercise the nested `restore_patch` exclusion against an on-disk wrong-root decoy -- aligns prose and pins the restore candidate to the caller-supplied git root.
- [x] `tests/conftest.py`, `tests/test_engine.py`, `tests/test_hook_bus.py` -- add a reusable scripted verify runner that checks canonical cwd, then replace all seven cwd-discarding mocks without changing their scripted results -- prevents future callers from silently regressing to `project`.
- [x] `tests/test_engine_worktree.py` -- add a real-command full-run row proving the dev verifier executes and classifies in the mounted unit worktree rather than the main checkout -- covers the supported third-root shape.
- [x] `docs/testing.md` -- document selection and assertion rules for default, sibling, nested, and isolated-worktree roots -- makes future tests choose a shape capable of separating the behavior they claim.
- [x] Run targeted tests, perform required negative-test ablations with recoverable file copies, then run formatting, lint, typecheck, and the full suite; confirm the ledger is unchanged.

**Acceptance Criteria:**
- Given a plain sandbox project, when `nested_repo_root_paths` builds `app/`, then `bmadconfig.load_paths(paths.project)` reproduces its canonical `ProjectPaths`, the config is committed, and isolation conflict is absent for `none` but present for `worktree`.
- Given a non-canonical alias to an existing sandbox root, when the nested builder returns, then every path is resolved, `project.parent == repo_root`, and monorepo exclude pathspecs remain non-empty.
- Given the nested builder, when its `.gitignore` is inspected, then it contains exactly the four ignore entries written by init and representative run/cache/policy/render paths are ignored.
- Given outer and nested `restore.patch` files, when `verify_dev_exclude_relpaths` is rooted on `repo_root`, then its restore entry resolves to the outer file and not the nested decoy.
- Given worktree isolation and a marker created only in the mounted unit worktree, when the dev verify command runs, then the story completes, its dev command record has return code 0, the main checkout has no marker, and both command execution and classification cwd equal the session worktree.
- Given any of the seven pre-existing scripted command mocks, when its caller invokes `run_verify_commands`, then the mock refuses a cwd whose resolved path differs from the expected `project.repo_root` while preserving the scenario's original result sequence.
- Given sibling and nested divergent-root shapes, when contributors read production and testing documentation, then the sibling-only `()` behavior and nested non-empty/separable behavior are distinguished and the required premise guards are stated.
- Given each new refusal/absence test, when its named gating or root-selection behavior is ablated, then that test fails for the asserted reason and passes again after restoration.
- Given the completed change, when targeted tests, `uv run pytest -q`, `uv run pyright`, `trunk fmt`, and `trunk check` run, then all pass and the deferred-work ledger has no diff.

## Spec Change Log

## Review Triage Log

### 2026-09-01 — Review pass
- verdicts: 9 findings — high 0, medium 1, low 2, false 6, maybe-false 0
- findings:
  - `[low]` `[patch]` `docs/testing.md` implied every sibling-root shape is config-loaded, while `_repo_root_override` is a hand-built lower-level fixture — corrected the guide to distinguish hand-built seam rows from callers that use `write_repo_root_override` and `load_paths`.
  - `[low]` `[patch]` `nested_repo_root_paths` missed a pre-existing dangling `app` symlink and leaked an opaque `FileExistsError` from setup — extended the precondition to reject symlinks and added a no-write contract row.
  - `[false]` `[reject]` The seven scripted mocks still accept `paths.project` — in every cited scenario `project` and `repo_root` resolve to the same directory, so no bad outcome follows; the separate nested and isolated rows pin behavior where the roots differ.
  - `[false]` `[reject]` The nested shape lacks a new `cli.main` row — the named gap is config loadability, which the helper now exercises by writing configuration and returning `load_paths(app)`; the reviewer demonstrated no CLI-specific bad outcome.
  - `[false]` `[reject]` The fixture duplicates init's four ignore entries instead of invoking `install_into` — the intent requires seeding the complete init shape, and existing init tests plus exact fixture and `git check-ignore` assertions cover the two surfaces without a demonstrated divergence.
  - `[medium]` `[patch]` The canonicalization matrix depended on a directory-symlink test that could skip on hosts without symlink capability — replaced it with a portable, non-skipping `..` alias row that still fails against unresolved returned paths and retains non-empty pathspec assertions.
  - `[false]` `[reject]` No new sibling behavior was added — the bundle's sibling work is accurate selection guidance and preservation of existing coverage; no ledger entry requires another sibling implementation row.
  - `[false]` `[reject]` Worktree isolation is tested only through the dev stage — both dev and fix use the same exercised `_verify_commands_with_results` method, and the existing fix-stage divergent-root row already pins both hops; no distinct isolated fix failure was demonstrated.
  - `[false]` `[reject]` Restore-patch anchoring is covered at the helper rather than a complete engine restoration flow — the ledger names `verify_dev_exclude_relpaths`, and the real outer candidate plus nested decoy directly discriminate its root contract without an untested production hop.

## Design Notes

The three relevant cwd shapes are intentionally distinct: default/sibling tests distinguish project from configured code root; nested tests make wrong pathspecs plausible and non-empty; isolated-worktree tests introduce a third live checkout and must assert against the mounted workspace, not either original root. A single fixture cannot faithfully grade all three contracts.

## Verification

**Commands:**
- `uv run pytest tests/test_conftest.py tests/test_verify.py tests/test_engine.py tests/test_engine_worktree.py tests/test_hook_bus.py -q` -- expected: affected contracts pass.
- `rg -n 'lambda policy, cwd:|lambda .*cwd:' tests/test_engine.py tests/test_hook_bus.py` -- expected: no cwd-discarding verify-command mock remains.
- `uv run pytest -q` -- expected: full suite passes with zero live LLM usage.
- `uv run pyright` -- expected: zero errors.
- `trunk fmt` and `trunk check` -- expected: clean.
- `git diff --check` -- expected: no whitespace errors.
- `git diff -- _bmad-output/implementation-artifacts/deferred-work.md` -- expected: empty.

## Auto Run Result

Status: done

Summary: Completed the divergent-root fixture family across sibling, nested-monorepo, and isolated-worktree shapes. The nested builder now round-trips through canonical project configuration, commits the complete init-like ignore/config seed, and supports non-blind pathspec tests; verify-command cwd coverage now includes the isolated unit worktree and every previously blind scripted mock.

Files changed:
- `src/bmad_loop/verify.py` — make divergent-root docstrings topology-aware.
- `tests/conftest.py` — add cwd-asserting scripted verification and harden the canonical, config-loaded nested builder.
- `tests/test_conftest.py` — cover config round-trip, canonical aliases, ignore behavior, helper guards, and isolation conflict.
- `tests/test_verify.py` — pin nested restore-patch selection against a real wrong-root decoy.
- `tests/test_engine.py` — migrate six cwd-blind mocks to the shared asserting helper.
- `tests/test_engine_worktree.py` — prove real verify execution and classification use the mounted unit worktree.
- `tests/test_hook_bus.py` — migrate the remaining cwd-blind hook-bus mock.
- `docs/testing.md` — document divergent-root topology selection and assertion rules.
- `_bmad-output/implementation-artifacts/spec-dw-56-63-root-divergence-fixture-hardening.md` — record the implementation and review result.

Review findings breakdown: 3 patches applied (high 0, medium 1, low 2), 0 items deferred, and 6 findings rejected. Rejections were: collapsed default roots have no distinct project-root failure; config loadability does not require a new CLI row; reproducing the pinned init shape has no demonstrated divergence; no new sibling behavior was required; the shared dev/fix verifier plus existing fix coverage leaves no demonstrated isolated fix gap; and direct restore-exclusion coverage observes the ledger's named surface.

Follow-up review recommendation: false — this pass patched no high finding and only one medium finding.

Verification performed:
- Affected suite after review patches: `1202 passed, 23 skipped`.
- Matrix audit: all six covering tests passed without skips.
- Full suite after review patches: `7826 passed, 51 skipped`.
- `uv run pyright`: 0 errors, 0 warnings, 0 informations.
- `trunk fmt`, `trunk check`, and `git diff --check`: clean.
- Search for cwd-discarding mocks: no matches in `tests/test_engine.py` or `tests/test_hook_bus.py`.
- Deferred-work ledger diff: empty.

Residual risks: The dangling-symlink guard row skips only where the host cannot create directory symlinks; the non-skipping `..` alias row independently covers canonical path returns on every platform. Existing platform-specific skipped tests remain outside this bundle. No executable production behavior changed.
