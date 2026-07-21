"""Interactive Hermes Agent adapter for attachable BMAD Loop tmux sessions."""

from __future__ import annotations

import time
from typing import Any
from ..bmadconfig import ProjectPaths
from .base import SessionHandle, SessionSpec
from .generic import GenericAdapter, _DevSynthesisMixin


class HermesStartupError(RuntimeError):
    """Hermes exited before the adapter could deliver the initial prompt."""


class HermesAdapter(GenericAdapter):
    """Launch normal Hermes CLI sessions and inject prompts after startup."""

    STARTUP_GRACE_S = 2.0

    def interactive_argv(self, spec: SessionSpec) -> list[str]:
        extra = self.extra_args
        if extra is None:
            extra = self.profile.bypass_args
        argv = [self.binary, *self.profile.launch_args, *extra]
        if spec.model:
            argv += [self.profile.model_flag, spec.model]
        return argv

    def interactive_env(self, spec: SessionSpec) -> dict[str, str]:
        env = super().interactive_env(spec)
        # Hermes discovers project-local skills through this explicit directory.
        # Respect a caller override for custom shared skill layouts.
        env.setdefault("HERMES_PROJECT_SKILLS", str((spec.cwd / self.profile.skill_tree).resolve()))
        return env

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        self._prepare_session(spec)
        handle = self._launch_session(spec, self.build_command(spec))
        time.sleep(self.STARTUP_GRACE_S)
        if not self._window_alive(handle):
            raise HermesStartupError(f"{spec.task_id}: pane exited during Hermes startup")
        self.send_text(handle, self.profile.render_prompt(spec.prompt))
        return handle


class HermesDevAdapter(_DevSynthesisMixin, HermesAdapter):
    """Hermes dev/review adapter that synthesizes bmad-dev-auto results from specs."""

    def __init__(self, *args: Any, paths: ProjectPaths, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.paths = paths
        self._configure_dev_knobs()
