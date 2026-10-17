# BMAD Loop knowledge

This document covers the `bmad-loop` module: what it gives the user, when to recommend it, and what to offer next.

## What the module is

BMAD Loop runs BMAD implementation work unattended. Its orchestrator, the `bmad-loop` command, is ordinary Python with no LLM in its control loop. It picks the next unit of work, launches a fresh coding-agent session for it inside a terminal multiplexer, verifies what the session left on disk, runs the project's own checks, and commits. Then it moves on. The skills in this module are the parts those sessions dispatch; the orchestrator itself is a Python package that `bmad-loop-setup` installs.

## What it needs

- `uv`, `git`, and a terminal multiplexer (tmux; psmux on native Windows).
- A supported coding CLI: claude, codex, gemini, copilot, antigravity or opencode.
- The upstream `bmad-build-auto` skill, which every dev and review session invokes.
- On BMAD v7, a ticket tree from `bmad-ticket` with an active initiative. On BMAD v6, a `sprint-status.yaml` from `bmad-sprint-planning`.

## The skills

| Skill               | For                                                                                                        | When to recommend                                                                |
| ------------------- | ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `bmad-loop-setup`   | Installs or upgrades the orchestrator, then runs `bmad-loop init` and `bmad-loop validate` in the project. | The user wants to install, set up, wire or upgrade bmad-loop.                    |
| `bmad-loop-resolve` | An interactive session that disambiguates a frozen plan or spec so a paused unit can be re-driven.         | A run paused on a CRITICAL escalation; the user runs `/bmad-loop-resolve <key>`. |
| `bmad-loop-sweep`   | Read-only triage of the deferred-work ledger. Only `bmad-loop sweep` invokes it.                           | Never directly; recommend `bmad-loop sweep` instead.                             |

## Start here

- "Run my tickets unattended", "automate the build loop", "drive the stories overnight" → `/bmad-loop-setup`, then `bmad-loop run`.
- "Check my setup", "why won't it start" → `bmad-loop validate`.
- "Watch the run", "show me progress" → `bmad-loop tui` or `bmad-loop status`.
- "Clear the deferred work" → `bmad-loop sweep`.
- "The run paused" → `bmad-loop status`, then `bmad-loop resolve` or `bmad-loop confirm`, as it says.

## When not to recommend it

- One change the user wants to watch and steer is `bmad-build`, not an unattended loop.
- Planning work (specs, PRDs, the ticket tree itself) belongs to the method skills; bmad-loop only builds what is already planned.
