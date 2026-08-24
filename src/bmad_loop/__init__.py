"""Deterministic orchestrator for the BMAD implementation phase.

The control loop is plain Python; LLMs only run inside disposable
coding-CLI sessions spawned per pipeline step. All durable state lives
on disk: sprint-status.yaml (the orchestrator is its sole writer while a
run is in flight — see :mod:`bmad_loop.sprintstatus`; your own BMAD skill
runs still generate and edit it outside one), spec files, and the per-run
directory under .bmad-loop/runs/.
"""

__version__ = "0.11.1"
