# BMAD Loop module (`bmad-loop`)

A BMAD module pairing the automation skills with the
[bmad-loop orchestrator tool](https://github.com/bmad-code-org/bmad-loop) (the
Python program that drives the loop). The skills can be installed by the BMAD
installer, or laid down by `bmad-loop init` (the orchestrator's wheel **bundles**
them); either way `bmad-loop-setup` installs the `bmad-loop` package from its
Git repository, so installing this module gives you a working system — skills
plus the orchestrator that invokes them. Standard BMAD installs are never
modified; the skills are bmad-loop-owned, standalone or bmad-loop-native (see
the table below).

| Component           | Forked from          | Role                                                                                                                                              |
| ------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bmad-loop`         | — (this repo, Git)   | the orchestrator: ralph-loop, hooks, tmux adapters, TUI. CLI `bmad-loop`. Installed by `bmad-loop-setup` from Git.                                |
| `bmad-loop-resolve` | — (bmad-loop-native) | interactive CRITICAL-escalation resolution: a human disambiguates a frozen spec so a paused story can be re-driven (`/bmad-loop-resolve <story>`) |
| `bmad-loop-sweep`   | — (bmad-loop-native) | read-only deferred-work ledger triage; owns the canonical `deferred-work-format.md`                                                               |
| `bmad-loop-setup`   | — (scaffolded)       | **installs the orchestrator tool from Git**, runs `bmad-loop init` + `validate`, refreshes `_bmad/bmad-loop/module-help.csv`                      |
| `bmod-bmad-loop`    | — (module record)    | the BMAD v7 module record (`bmod.toml`, `help/help.md`) that `bmad setup` discovers; metadata only, never invoked                                 |

The **inner dev primitive is the upstream `bmad-build-auto` skill** (BMAD-METHOD's
generic unattended dev session; `bmad-dev-auto` on pre-rename releases, resolved
from disk and invoked under whichever name is there). It is **not** owned or
bundled here — the orchestrator drives it as an external skill that must already be installed
(by the BMad installer / bmm-core). The bmad-loop orchestrator synthesizes its `result.json`
from the spec the session leaves on disk (see `bmad_loop.devcontract`). The skill
self-reviews inline (its step-04 review layers) and commits its own
work each iteration; the orchestrator's **follow-up review is just a re-invocation
of the primitive on the done spec** (BMAD-METHOD #2508 routes a `done` spec to a
fresh review pass), so there is no separate review skill.

## Install into a project

The orchestrator tool now bundles these skills, so `bmad-loop init` lays them
down for you:

```bash
uv tool install "bmad-loop[tui] @ git+https://github.com/bmad-code-org/bmad-loop.git"
bmad-loop init --project /path/to/project --cli claude   # add --cli codex/gemini as needed
claude "/bmad-loop-setup accept all defaults"            # installs the tool + wires the project
```

`bmad-loop init` installs the `bmad-loop-*` skills into `.claude/skills/`
(claude) and/or `.agents/skills/` (codex/gemini), registers hooks, writes
`.bmad-loop/policy.toml`, and gitignores the runs dir. Existing skill dirs are
left untouched (`--force-skills` to overwrite, `--no-skills` to skip).
On BMAD v7, `npx skills add bmad-code-org/bmad-loop` followed by `bmad setup` installs
the same skills as a module: `bmod-bmad-loop/bmod.toml` is the module record, and each
`bmad-loop-*` skill's `[skill]` `bmod.toml` names it.
`bmad-loop-setup` is one-shot for the bootstrap the BMAD installer cannot do: it
ensures the orchestrator tool is installed, then runs `bmad-loop init` and
`bmad-loop validate` (preflight). Module registration — `_bmad/bmad-loop/`, the
central `config.toml`, the `/bmad-help` catalog — belongs to the BMAD installer,
which regenerates it on every run; the only file the skill writes there is
`_bmad/bmad-loop/module-help.csv`.

The skills must be installed **together**: `bmad-loop-sweep` owns the canonical
`deferred-work-format.md` that the ledger normalizes to, and the upstream dev
primitive must also be present (since BMAD-METHOD 6.10.1-next.33 it records
deferred findings in its spec's frontmatter for the orchestrator to harvest into
the ledger; pre-rename copies append flat entries the sweep normalizes instead).
On BMAD v6 this requires the BMad Method (bmm) module (`_bmad/bmm/config.yaml`), and sprint
mode needs a `sprint-status.yaml` from `bmad-sprint-planning`. On BMAD v7 it requires the
`bmad` and `bmad-build-auto` skills from the method module (6.13.0 or newer), and tickets
mode needs a ticket tree from `bmad-agent-po` (Sofia, the Product Owner) with an active
initiative.

`_bmad/custom/<skill-name>.toml` customization overrides are keyed by skill
directory name.

## Maintaining the skills

- This directory (`skills/` at the repo root) is **canonical** for the skills.
  It sits where the Vercel `skills` CLI looks, so `npx skills add
bmad-code-org/bmad-loop` finds every skill in it. The wheel bundles it as
  package data (`force-include` maps it to `bmad_loop/data/skills`), so
  `bmad-loop init` can install the skills too; code reads it through
  `bmad_loop.install.skills_root()`, which also covers an editable checkout. The repo's `.claude/skills/` and `.agents/skills/` hold dev-workspace
  copies; `tests/test_module_skills_sync.py` fails if they drift. After editing
  here, re-copy the skill dirs into both trees.
- The orchestrator tool is **not** bundled in the skill dirs — the BMAD installer
  copies only the skill directories, so a sibling `tool/` would never reach an
  installed project. `bmad-loop-setup` installs the `bmad-loop` package from
  <https://github.com/bmad-code-org/bmad-loop> (`src/bmad_loop`, `pyproject.toml`
  are canonical at the repo root). (The skills, by contrast, ride along inside
  the package wheel.)
- The inner dev primitive (`bmad-build-auto`, or `bmad-dev-auto` pre-rename) is
  **not** maintained here — it is the upstream bmm-core skill, driven unmodified.
  Nothing in this directory mirrors it; the orchestrator adapts to it via
  `bmad_loop.devcontract`.
- Do **not** rename the result.json `workflow` values — they are machine
  contracts the orchestrator validates, not skill names:
  - dev → `"auto-dev"` (checked by `verify.DEV_WORKFLOW` in
    `verify_dev` / `verify_dev_bundle`; the orchestrator forges this value in
    `devcontract` when synthesizing the dev result from the spec).
  - sweep triage / migrate → `"deferred-sweep-triage"` / `"deferred-sweep-migrate"`
    (checked in `sweep.py`).
- The `[bmod] version` in `bmod-bmad-loop/bmod.toml` is stamped by
  `scripts/sync_version.py` like every other version field; never edit it by hand. The
  `required_skills` minimums are upstream versions and are edited by hand.
- An `Environment fault: <text>` line in a session's last `## Auto Run Result`
  becomes `env_fault_claim` in the synthesized dev result (DW-523). It is a claim
  contract, not a verdict: it only triggers the orchestrator's own
  `[environment] probes`, and never pauses or passes a story on its own.

Validate after changes (from the repo root):

```bash
python3 .claude/skills/bmad-module-builder/scripts/validate-module.py skills
```
