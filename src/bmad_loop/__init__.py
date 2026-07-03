"""Deterministic orchestrator for the BMAD implementation phase.

The control loop is plain Python; LLMs only run inside disposable
coding-CLI sessions spawned per pipeline step. All durable state lives
on disk: sprint-status.yaml (owned by the skills, read-only here),
spec files, and the per-run directory under .bmad-loop/runs/.
"""

__version__ = "0.8.0"
