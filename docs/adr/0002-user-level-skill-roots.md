# ADR 0002: Resolving BMAD skills from user-level skill roots

- **Status:** Accepted (2026-11-15)
- **Date:** 2026-11-15
- **Issues:** #770 (use globally installed BMAD skills); context #768 / PR #1054 (the project-tree-only rule this record reverses behind an opt-in)
- **Seam:** `skill_locate` (`src/bmad_loop/skill_locate.py`), the profile `[user_skills]` table (`src/bmad_loop/adapters/profile.py`), the `[skills] user_roots` policy switch

**Decision in brief:**

1. Keep project-tree-only resolution as the default. Add an opt-in switch, `[skills] user_roots = false`.
2. With the switch on, resolve a skill from a user-level folder only when the coding CLI's profile declares that folder: the folders the CLI loads natively, in the CLI's own order, with the CLI's own precedence (`project`, `user` or `unordered`) and the environment variables that make it skip a folder.
3. Route every skill question through one locator: `validate`, the run preflight, `run --dry-run`, session dispatch, the render probe, review-lens reads and worktree seeding.
4. opencode-http 2.x allows only the BMAD module skills a session needs from a user root; 1.x is refused. Worktrees never copy from a user root.

## 1. Problem

`npx skills add -g` installs skills into the user's home folder (`~/.agents/skills`, `~/.claude/skills`, and others depending on the target CLI). bmad-loop resolved every skill as `<project>/<skill_tree>/<name>`, so a project relying on a global install failed `validate` with `skills.base-missing`. The only workaround, a junction from the project tree to the global folder, passed `validate` and then broke `init` and worktree seeding, which escalated mid-run.

On 2026-11-14 #768 (merged as PR #1054) settled the question as "project tree only": `validate` refused a global-only install and named the global copy in a read-only hint. The issue asked for the opposite. This record reverses that rule, behind an opt-in.

Two facts shape the design:

- **There is no single global folder.** Checked against vendor docs and source on 2026-11-15:

  | CLI         | Global folders it loads                                             | On a name clash                                 |
  | ----------- | ------------------------------------------------------------------- | ----------------------------------------------- |
  | Claude Code | `~/.claude/skills` only                                             | personal beats project                          |
  | Codex       | `~/.agents/skills` and the deprecated `~/.codex/skills`             | both appear, no order                           |
  | Gemini      | `~/.agents/skills` and `~/.gemini/skills`                           | workspace beats user; `.agents` beats `.gemini` |
  | Copilot CLI | `~/.copilot/skills` and `~/.agents/skills`                          | project beats personal                          |
  | Antigravity | `~/.gemini/config/skills`                                           | not documented                                  |
  | OpenCode    | `~/.config/opencode/skills`, `~/.claude/skills`, `~/.agents/skills` | project wins                                    |

- **A stale global copy already ran in place of the one `validate` checked.** Claude Code runs `~/.claude/skills/<name>` over the project's copy, so a project-tree-only `validate` could pass a primitive the session never loaded.

The governing constraint: `validate` must never pass when the session cannot load the skill.

## 2. Options

### A. Keep the project-tree-only rule

No change; document the per-project install. Rejected: it leaves the shadowing case above undetected, and it refuses an install method the skills CLI offers as a first-class choice.

### B. A free-form `~` path in `skill_tree`

Let a profile or policy name `skill_tree = "~/.agents/skills"`. Rejected:

- A tree is also where `init` installs and what worktree seeding copies. A home path would make `init` write into the user's home and worktrees copy from it.
- Nothing ties the path to what the CLI loads. A path the CLI never reads gives a green `validate` and a session that cannot find its skill.
- It already loaded silently and resolved to a literal `<project>/~/...`. This change refuses a leading `~` in every profile path instead.

### C. Fall back to one global folder automatically

When a skill is missing from the project, look in `~/.agents/skills`. Rejected: that folder is not what Claude Code, Antigravity or (with `COPILOT_HOME`) Copilot read, and the fallback ignores which copy a CLI prefers on a clash. `validate` would check a copy the session does not run.

### D. Declared, per-CLI user roots behind an opt-in (chosen)

Each packaged profile declares, in a `[user_skills]` table, the folders its CLI loads natively, in its own order, with its precedence and its disabling variables. A profile without the table (overlays, entry-point profiles) never resolves from a user root. The switch defaults off, so existing projects resolve exactly as before.

## 3. Decision

Option D.

- **One locator.** `skill_locate.locate(project, scope, name)` answers every skill question. A `SkillScope` carries the project trees, the declared user roots and the switch; a plain tree string is a project-only scope, which keeps every existing caller and output unchanged.
- **Precedence follows the CLI.** `project`: the project copy, else a user copy. `user` (claude): the user copy, else the project's, so `validate` checks the copy Claude Code runs. `unordered` (codex, antigravity): the project copy is checked and a warning says both load.
- **Shadowing is reported with the switch off.** `skills.user-shadow` warns whenever a user copy outranks or competes with the project's, because the CLI loads it whether or not bmad-loop resolves it.
- **Unavailable roots are skipped, not guessed.** A root whose disabling variable is set (in bmad-loop's environment plus the profile's `[env]`), that cannot be read, or that has no home folder is never resolved and is reported as `skills.user-root-unavailable`.
- **Visibility.** `validate` emits a `skills.root` line per driven skill; `run --dry-run` prints a note for each user-root skill; each session journals `skill-user-root` rows.

### opencode isolation trade-off

opencode-http keeps sessions hermetic from the operator's personal skills. On 1.x it sets `OPENCODE_DISABLE_EXTERNAL_SKILLS=1`; on 2.x it denies every skill and re-allows the project's ids.

- **2.x:** the allow-list also takes the BMAD module skill ids the session needs from a user root: the dev primitive, its review lenses, the prompt skill, and the ids a user root's `bmod-*/bmod.toml` lists. Personal skills stay denied. This opens the hermetic boundary only by ids BMAD itself declares.
- **1.x:** there is no per-id control; lifting `OPENCODE_DISABLE_EXTERNAL_SKILLS` would expose every personal skill. User roots are unsupported there: `validate` warns (`skills.user-root-unsupported`) and the session refuses to spawn, so `validate` and the run agree.

### Worktrees

A worktree is a clean checkout; it never carries the user's home folder. Seeding copies only from project trees, and the seeding gate exempts skills the locator puts in a user root (journaled as `worktree-skills-user-root`): the session loads those from the home folder directly. A skill tree linked outside the repository is refused at preflight under worktree isolation (`skills.tree-outside`), because the worktree cannot carry it and the run used to escalate mid-run.

## 4. Consequences

- Deliberate changes with the switch off: a leading `~` in a profile path is a load error; `skills.tree-outside` refuses at preflight under worktree isolation; `skills.user-shadow` warns; the `skills.base-missing` hint names only a copy in the CLI's own user roots; opencode-http also reads `.agents/skills`; copilot's prompt names the located skill folder through `{skill_dir}`, which changes its config digest once.
- The vendor folder lists drift. The profiles carry the lists as data with source comments; they must be rechecked when a CLI changes how it discovers skills.
- Disabling variables are read from bmad-loop's environment, not a long-lived multiplexer server's, which can differ.
- Which copy opencode loads when one id is in both of its project trees is not pinned; the skills CLI normally links the two.
