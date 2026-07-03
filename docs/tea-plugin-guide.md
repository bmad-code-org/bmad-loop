# The Test Architect (TEA) plugin

The bundled **`tea`** plugin wires the BMAD **Test Architect Enterprise (TEA)**
module — the "Murat" agent and its `bmad-testarch-*` workflows — into every
bmad-loop run and sweep as **advisory-by-default quality steps**. It injects
six TEA sessions across the pipeline (test-design, ATDD, automate after dev;
trace, NFR, test-review just before commit), and an operator can flip any of the
three gate steps to **blocking** so a failing quality gate escalates the unit for
human review instead of committing.

Like the [game-engine layer](game-engine-plugin-guide.md), TEA is **just a
plugin** on the general [plugin system](plugin-authoring-guide.md) — same
`plugin.toml` manifest, same [lifecycle hooks](plugin-authoring-guide.md#stage-reference),
same trust model. It ships bundled (`src/bmad_loop/data/plugins/tea/`) but is
**inert until enabled**: a project that doesn't list `tea` in `[plugins] enabled`
behaves exactly as before (the zero-plugin "byte-identical" guarantee holds).
**Read the [plugin authoring guide](plugin-authoring-guide.md) first** for the
manifest, settings, hook, and trust fundamentals — this guide covers only the
TEA-specific slice.

## Prerequisite: install TEA in your project

The plugin **orchestrates** TEA; it does not bundle it. You install TEA once, per
project, with the BMAD installer:

```bash
npx bmad-method install      # choose "Test Architect" (Enterprise)
```

That writes TEA's runtime to `_bmad/tea/config.yaml` plus its compiled per-CLI
commands (`.claude/commands/bmad/tea/*`, and the equivalent under `.agents/`,
`.gemini/`, `.agent/`). The plugin's **readiness gate** checks for
`_bmad/tea/config.yaml` at startup and **fails the run fast** with a remediation
message if it's missing (see [Readiness gate](#readiness-gate) below) — so an
operator who enables `tea` without installing TEA learns immediately instead of
watching six sessions flail.

## Enabling the plugin

In `.bmad-loop/policy.toml`:

```toml
[plugins]
enabled = ["tea"]            # the [python] readiness gate loads only when enabled

[plugins.tea]
# every step ships on + advisory; flip only what you need:
nfr_blocking = true          # escalate (don't commit) when the NFR gate fails
atdd_enabled = false         # skip the ATDD generation step entirely
```

All settings are also editable from the TUI settings screen (`g`) once the plugin
is toggled on — see the [TUI guide](tui-guide.md).

## The six steps and where they run

TEA work injects at **two** lifecycle stages that have a live worktree
(`post_dev_phase` and `pre_commit_gate`; see the
[stage reference](plugin-authoring-guide.md#stage-reference)).
Each step runs as an extra agent session through the `dev` or `review` adapter,
with `workflow-start` / `workflow-end` journal entries.

| Step       | TEA workflow                | Stage             | Adapter | What it does                                                           |
| ---------- | --------------------------- | ----------------- | ------- | ---------------------------------------------------------------------- |
| `td`       | `bmad-testarch-test-design` | `post_dev_phase`  | dev     | Risk assessment + risk-based coverage strategy for the change.         |
| `atdd`     | `bmad-testarch-atdd`        | `post_dev_phase`  | dev     | Failing acceptance tests + an implementation checklist.                |
| `automate` | `bmad-testarch-automate`    | `post_dev_phase`  | dev     | Prioritized API/E2E tests, fixtures, and a Definition-of-Done summary. |
| `trace`    | `bmad-testarch-trace`       | `pre_commit_gate` | review  | Map requirements → tests and record the quality-gate decision.         |
| `nfr`      | `bmad-testarch-nfr`         | `pre_commit_gate` | review  | Assess non-functional requirements and record the gate decision.       |
| `review`   | `bmad-testarch-test-review` | `pre_commit_gate` | review  | Quality-check the written tests against TEA's knowledge base.          |

The three `post_dev_phase` steps run **after** the dev phase, in the order above;
the three `pre_commit_gate` steps run **just before the commit**. That stage fires
on **every** path into a commit — the review-converged path, the skip path
(`review.trigger = "recommended"` with no follow-up recommended, where
`post_review_result` never fires because the review loop never runs), and the
review-budget rescue — so the gates always evaluate the exact tree about to
commit, and the `*_blocking` flags enforce even on stories that skip the review
loop. Each prompt is **CLI-agnostic** — natural-language intent plus the explicit
`bmad-testarch-*` workflow name, phrased around "the changes currently in the
working tree for `{story_key}`" so the same step works for a single story **and**
a sweep bundle.

### Runs and sweeps, no extra wiring

Sweep bundles run through the **same inherited** dev/review pipeline as a normal
story, so all six TEA steps fire for **sweep bundles too** with no sweep-specific
code. `on_pre_commit` gate enforcement likewise applies to bundles via the
inherited commit path.

## The enable / blocking settings

The per-step settings use the generic **workflow-overlay convention** (see
[Making a workflow configurable](plugin-authoring-guide.md#making-a-workflow-configurable)).
Two key families, both keyed on the step name:

- **`<step>_enabled`** (bool) — every step ships `true`. Set one `false` to drop
  just that step; no session runs. If you disable every step at a stage, the
  stage drops out entirely (zero injection overhead).
- **`<step>_blocking`** (bool) — exposed only for the three **gate** steps
  (`trace`, `nfr`, `review`), shipping `false` (advisory). Flip one `true` to make
  a failing gate escalate at commit (see below). The generation steps
  (`td`/`atdd`/`automate`) are advisory by design and expose no blocking flag.

| Setting                                               | Default | Effect                                                                                          |
| ----------------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------- |
| `require_tea`                                         | `true`  | Fail fast at startup if TEA isn't installed. `false` = run the steps advisory-only without TEA. |
| `td_enabled` / `atdd_enabled` / `automate_enabled`    | `true`  | Enable each `post_dev_phase` generation step.                                                   |
| `trace_enabled` / `nfr_enabled` / `review_enabled`    | `true`  | Enable each `pre_commit_gate` gate step.                                                        |
| `trace_blocking` / `nfr_blocking` / `review_blocking` | `false` | When `true`, a FAIL/CONCERNS verdict from that gate escalates the unit at commit.               |

## Escalate-on-gate: how blocking works

A workflow's manifest `blocking` flag only checks session **completion**, not test
pass/fail — so true quality gating lives in the plugin's in-process
`on_pre_commit` hook, the first vetoable stage **after** the `pre_commit_gate`
sessions have written their artifacts. Both fire unconditionally before every
commit, so enforcement holds even when `review.trigger = "recommended"` skips
the orchestrator review loop entirely.

When you mark a gate blocking (e.g. `nfr_blocking = true`), at commit time the
plugin:

1. Locates that gate's latest artifact under TEA's configured `test_artifacts`
   directory (read from `_bmad/tea/config.yaml`; falls back to the installer
   default `_bmad-output/test-artifacts`).
2. Parses the gate verdict — from the trace gate's JSON, or the
   `## Gate Decision` / `**Gate Status**` / `**Recommendation**` line of the NFR
   and test-review markdown reports.
3. On a **FAIL** or **CONCERNS** verdict, vetoes the commit with `pause` — the
   unit **escalates for human review** instead of landing. (`pre_commit` honors
   only a `pause` veto, which is the right "blocking gate failed" semantic.)
   `PASS` and `WAIVED` never block.

**Enforcement is fail-open by design.** A gate with no blocking flag, no artifact,
or an artifact that can't be parsed into a known verdict **never** blocks the
commit — an unknown format must never wrongly stop a commit. Only a
confidently-parsed FAIL/CONCERNS on an operator-marked-blocking gate escalates.
The verdicts the plugin did read are recorded on the shared context (`tea_gates`)
as a breadcrumb for the journal.

## Worktree seeding

TEA's runtime (`_bmad/`) and its compiled per-CLI skills are commonly gitignored
or not guaranteed in a fresh checkout, so the plugin declares `seed_globs` to
**prime them into an isolated per-worktree checkout**:

```toml
seed_globs = [
    "_bmad/**",               # TEA runtime: config.yaml + testarch workflows + knowledge base
    ".claude/skills/bmad-t*", # compiled TEA skills for claude-code
    ".agents/skills/bmad-t*", # codex / cursor compiled skill home
    ".gemini/skills/bmad-t*", # gemini compiled skill home
    ".agent/skills/bmad-t*",  # generic agent compiled skill home
]
```

Under the default in-place mode (`[scm] isolation = "none"`) seeding is a no-op —
the sessions run in the project tree TEA is already installed in. It matters once
you switch to `[scm] isolation = "worktree"`, where each unit gets its own
checkout that wouldn't otherwise carry TEA's runtime.

## Reference: the bundled plugin

The canonical source lives at `src/bmad_loop/data/plugins/tea/`:

- `plugin.toml` — the `[python]` module, the `require_tea` + per-step
  enable/blocking `[[settings]]`, the six `[workflows.*]` (CLI-agnostic prompts),
  and `seed_globs`.
- `tea_plugin.py` — the in-process brain: the readiness gate (`validate`) and the
  blocking-gate enforcement (`on_pre_commit`), including the fail-open artifact
  parsing for each gate's verdict format.

### Readiness gate

With `require_tea` on (the default), `TeaPlugin.validate` refuses to start the run
unless `_bmad/tea/config.yaml` is present, raising a `PluginError` that names the
missing file and tells the operator to either run `npx bmad-method install` →
Test Architect, or set `require_tea = false` to run the steps advisory-only. Turn
`require_tea` off when you deliberately want the prompts to degrade gracefully
(e.g. on a CLI where TEA isn't installed) rather than gate the whole run.
