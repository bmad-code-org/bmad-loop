"""bmad-loop TUI (optional `bmad-loop[tui]` extra).

`data` is the pure-stdlib observation layer and must stay importable without
the extra; every other submodule may import textual and is loaded lazily by
the `tui` CLI command.
"""
