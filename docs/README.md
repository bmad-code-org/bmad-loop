# bmad-loop documentation

Start with the [project README](../README.md) for the overview and quick start. The
guides below go deeper, roughly in the order you'll need them.

## Using bmad-loop

- **[Setup guide](setup-guide.md)** — install the tools, pick a CLI, initialize a project, pass preflight, and uninstall.
- **[TUI guide](tui-guide.md)** — the dashboard: layout, key bindings, the settings editor, and troubleshooting.
- **[Ticket-tree mode](tickets-guide.md)** — driving a BMAD v7 initiative ticket tree: prerequisites, store layouts, pick order, every pause and how to clear it, marking done, checkpoints, the epic retrospective and closure, parking and resolve, and the v7 ledger and sweep.
- **[Deferred-work sweeps](sweep-guide.md)** — running a sweep, answering decisions, closing entries from stories, hard gates, and recovering from stale answers, publication refusals and an unreadable ledger.
- **[Recovering paused runs](escalations-guide.md)** — escalations and `resolve` (re-arm, `--adopt-branch`, `--reverify`, `--abandon`), preserved attempts, plateau-defer, `confirm` for human-only actions, environment faults, and worktree merge-back failures.
- **[Features](FEATURES.md)** — what the tool does: the feature catalog, with each feature's stability, guarantees, tests and docs.
- **[Terminal multiplexer backends](multiplexer-backends.md)** — which backend drives your agent sessions (tmux bundled; externals like the [herdr adapter](https://github.com/pbean/bmad-loop-adapter-herdr) install as packages), how selection works, and how external backends arrive.

## Reference

- **[Configuration](reference/configuration.md)** — `.bmad-loop/policy.toml` location and precedence, the `BMAD_LOOP_*` environment variables, and every policy setting with its type, default and allowed values (generated from the settings schema).
- **[CLI](reference/cli.md)** — every `bmad-loop` subcommand and its options, the `--json` document shapes, exit codes, and the `validate` check ids.
- **[Run state](reference/run-state.md)** — where run state lives, the run and task directory layout, session naming and teardown, stopping a run, and what `cleanup`, `clean`, `delete` and `archive` remove.

## Extending bmad-loop

- **[Authoring CLI adapters & profiles](adapter-authoring-guide.md)** — using `bmad-loop probe-adapter` to collect + sanitize the hook payload shape, transcript location, and token schema a new CLI profile needs, plus the `CodingCLIAdapter` ABC and how an adapter class (and the profile selecting it) ships out-of-tree.
- **[Writing a bmad-loop plugin](plugin-authoring-guide.md)** — the plugin system: `plugin.toml` manifest, hooks, lifecycle stages, settings, the trust model, and workflow injection, with a worked walkthrough.
- **[Writing a Game Engine plugin](game-engine-plugin-guide.md)** — the game-engine layer (built on the plugin system): driving a live engine Editor, the `editor_mode` ↔ `[scm] isolation` coupling, a minimal Godot example.
- **[Writing a plugin for a specific Editor MCP](game-engine-mcp-guide.md)** — Editor-MCP specifics for the bundled Unity plugin: IvanMurzak vs CoplayDev, readiness probes, `per_worktree` isolation, and the full `BMAD_LOOP_*` env-var reference.
- **[Porting bmad-loop to a new OS](porting-to-a-new-os.md)** — the four OS seams (terminal multiplexer, process lifecycle, hook interpreter, validate preflight), their registries and override env vars, and native Windows as the reference port.
- **[The Test Architect (TEA) plugin](tea-plugin-guide.md)** — the bundled `tea` plugin: installing TEA, the six advisory test-architecture steps it injects across runs and sweeps, the enable/blocking settings, and the escalate-on-gate behavior.

## Project direction

- **[Roadmap](ROADMAP.md)** — planned and intentionally-deferred work.
- **[Architecture decision records](adr/README.md)** — design decisions with the options weighed and the reasons, starting with the multiplexer env transport.

For released changes, see the [CHANGELOG](../CHANGELOG.md).

## Contributing & community

- **[Contributing guide](../CONTRIBUTING.md)** — dev setup (uv + trunk), PR guidelines, and conventional commits.
- **[Testing strategy](testing.md)** — the layer model, fixture and ablation doctrines, quality guards, the zero-token invariant, and the CI/flake policy behind the suite.
- **[Code of Conduct](../.github/CODE_OF_CONDUCT.md)** — the Contributor Covenant we follow.
- **[Security policy](../SECURITY.md)** — how to report a vulnerability and what's in scope.
- **[Trademark guidelines](../TRADEMARK.md)** — proper use of the BMad name and brand.
