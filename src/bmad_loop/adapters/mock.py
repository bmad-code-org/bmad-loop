"""Scripted adapter for engine tests — no tmux, no LLM.

The script is a list of entries consumed in order; each entry is either a
SessionResult or a callable(spec) -> SessionResult that can perform the
side effects a real skill session would (write spec files, bump
sprint-status, edit code) before returning.
"""

from __future__ import annotations

from typing import Callable

from ..model import TokenUsage
from .base import CodingCLIAdapter, SessionHandle, SessionResult, SessionSpec

ScriptEntry = SessionResult | Callable[[SessionSpec], SessionResult]


class ScriptExhausted(Exception):
    pass


class MockAdapter(CodingCLIAdapter):
    name = "mock"
    injection = "launch-flag"
    observation = "hook-signal"
    state = "local-jsonl"

    def __init__(self, script: list[ScriptEntry], usage_per_session: TokenUsage | None = None):
        self.script = list(script)
        self.sessions: list[SessionSpec] = []
        self.nudges: list[tuple[str, str]] = []
        self.usage_per_session = usage_per_session

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        self.sessions.append(spec)
        return SessionHandle(task_id=spec.task_id, native_id=f"mock-{len(self.sessions)}")

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        if not self.script:
            raise ScriptExhausted(f"no scripted result for session {spec.task_id}")
        entry = self.script.pop(0)
        return entry(spec) if callable(entry) else entry

    def send_text(self, handle: SessionHandle, text: str) -> None:
        self.nudges.append((handle.task_id, text))

    def read_usage(self, result: SessionResult) -> TokenUsage | None:
        return self.usage_per_session
