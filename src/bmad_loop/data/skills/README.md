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
| `bmad-loop-setup`   | — (scaffolded)       | registers the module in `_bmad/config.yaml` + `module-help.csv`, **installs the orchestrator tool from Git**, runs `bmad-loop init` + `validate`  |

The **inner dev primitive is the upstream `bmad-dev-auto` skill** (BMAD-METHOD's
generic unattended dev session). It is **not** owned or bundled here — the
orchestrator drives it as an external skill that must already be installed
(by the BMad installer / bmm-core). The bmad-loop orchestrator synthesizes its `result.json`
from the spec the session leaves on disk (see `bmad_loop.devcontract`). The skill
self-reviews inline (Blind + Edge-Case hunters in its step-04) and commits its own
work each iteration; the orchestrator's **follow-up review is just a re-invocation
of `bmad-dev-auto` on the done spec** (BMAD-METHOD #2508 routes a `done` spec to a
fresh review pass), so there is no separate review skill.

## Install into a project

The orchestrator tool now bundles these skills, so `bmad-loop init` lays them
down for you:

```bash
uv tool install "bmad-loop[tui] @ git+https://github.com/bmad-code-org/bmad-loop.git"
bmad-loop init --project /path/to/project --cli claude   # add --cli codex/gemini as needed
claude "/bmad-loop-setup accept all defaults"            # registers _bmad/ config + help
```

`bmad-loop init` installs the `bmad-loop-*` skills into `.claude/skills/`
(claude) and/or `.agents/skills/` (codex/gemini), registers hooks, writes
`.bmad-loop/policy.toml`, and gitignores the runs dir. Existing skill dirs are
left untouched (`--force-skills` to overwrite, `--no-skills` to skip).
`bmad-loop-setup` is one-shot for the BMAD-side wiring: it merges config + help
entries, ensures the tool is installed, then runs `bmad-loop init` and
`bmad-loop validate` (preflight).

The skills must be installed **together**: `bmad-loop-sweep` owns the canonical
`deferred-work-format.md` that the ledger normalizes to, and the upstream
`bmad-dev-auto` dev session must also be present (it appends flat deferred-work
entries the orchestrator normalizes on sweep). Requires the BMad Method (bmm)
module (`_bmad/bmm/config.yaml`) and a `sprint-status.yaml` from
`bmad-sprint-planning`.

`_bmad/custom/<skill-name>.toml` customization overrides are keyed by skill
directory name.

## Maintaining the skills

- This directory (`src/bmad_loop/data/skills/`) is **canonical** for the skills
  and is bundled into the wheel as package data, so `bmad-loop init` can install
  them. The repo's `.claude/skills/` and `.agents/skills/` hold dev-workspace
  copies; `tests/test_module_skills_sync.py` fails if they drift. After editing
  here, re-copy the skill dirs into both trees.
- The orchestrator tool is **not** bundled in the skill dirs — the BMAD installer
  copies only the skill directories, so a sibling `tool/` would never reach an
  installed project. `bmad-loop-setup` installs the `bmad-loop` package from
  <https://github.com/bmad-code-org/bmad-loop> (`src/bmad_loop`, `pyproject.toml`
  are canonical at the repo root). (The skills, by contrast, ride along inside
  the package wheel.)
- The inner dev primitive `bmad-dev-auto` is **not** maintained here — it is the
  upstream bmm-core skill, driven unmodified. Nothing in this directory mirrors
  it; the orchestrator adapts to it via `bmad_loop.devcontract`.
- Do **not** rename the result.json `workflow` values — they are machine
  contracts the orchestrator validates, not skill names:
  - dev → `"auto-dev"` (checked by `verify.DEV_WORKFLOW` in
    `verify_dev` / `verify_dev_bundle`; the orchestrator forges this value in
    `devcontract` when synthesizing the dev result from the spec).
  - sweep triage / migrate → `"deferred-sweep-triage"` / `"deferred-sweep-migrate"`
    (checked in `sweep.py`).

Validate after changes (from the repo root):

```bash
python3 .claude/skills/bmad-module-builder/scripts/validate-module.py src/bmad_loop/data/skills
```
