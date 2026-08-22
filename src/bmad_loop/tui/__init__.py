"""bmad-loop TUI (optional `bmad-loop[tui]` extra).

Only `launch` is importable on a core-only install: `data` needs pyte and rich
(it never imports textual), and every other submodule may import textual. The
core-safe run inventory lives in `bmad_loop.runs` — `data` re-exports it (#650).

It is the submodule the core CLI paths reach for (attach/park/sweep), and being
stdlib-only is what keeps those paths working extra-less. Every other
submodule is loaded lazily by the `tui` command, behind the guard that turns a
missing extra into an install hint.
"""
