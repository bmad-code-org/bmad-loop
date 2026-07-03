# bmad-loop documentation

Start with the [project README](../README.md) for the overview and quick start. The
guides below go deeper, roughly in the order you'll need them.

## Using bmad-loop

- **[Setup guide](setup-guide.md)** — install the tools, pick a CLI, initialize a project, pass preflight, and uninstall.
- **[TUI guide](tui-guide.md)** — the dashboard: layout, key bindings, the settings editor, and troubleshooting.
- **[Features & functionality](FEATURES.md)** — the full capability matrix and policy reference.

## Extending bmad-loop

- **[Finalizing a CLI adapter profile](adapter-authoring-guide.md)** — using `bmad-loop probe-adapter` to collect + sanitize the hook payload shape, transcript location, and token schema a new CLI profile needs.
- **[Writing a bmad-loop plugin](plugin-authoring-guide.md)** — the plugin system: `plugin.toml` manifest, hooks, lifecycle stages, settings, the trust model, and workflow injection, with a worked walkthrough.
- **[Writing a Game Engine plugin](game-engine-plugin-guide.md)** — the game-engine layer (built on the plugin system): driving a live engine Editor, the `editor_mode` ↔ `[scm] isolation` coupling, a minimal Godot example.
- **[Writing a plugin for a specific Editor MCP](game-engine-mcp-guide.md)** — Editor-MCP specifics for the bundled Unity plugin: IvanMurzak vs CoplayDev, readiness probes, `per_worktree` isolation, and the full `BMAD_LOOP_*` env-var reference.
- **[Porting bmad-loop to a new OS](porting-to-a-new-os.md)** — the four OS seams (terminal multiplexer, process lifecycle, hook interpreter, validate preflight), their registries and override env vars, and what a native-Windows port costs end to end.
- **[The Test Architect (TEA) plugin](tea-plugin-guide.md)** — the bundled `tea` plugin: installing TEA, the six advisory test-architecture steps it injects across runs and sweeps, the enable/blocking settings, and the escalate-on-gate behavior.

## Project direction

- **[Roadmap](ROADMAP.md)** — planned and intentionally-deferred work.

For released changes, see the [CHANGELOG](../CHANGELOG.md).

## Contributing & community

- **[Contributing guide](../CONTRIBUTING.md)** — dev setup (uv + trunk), PR guidelines, and conventional commits.
- **[Code of Conduct](../.github/CODE_OF_CONDUCT.md)** — the Contributor Covenant we follow.
- **[Security policy](../SECURITY.md)** — how to report a vulnerability and what's in scope.
- **[Trademark guidelines](../TRADEMARK.md)** — proper use of the BMad name and brand.
