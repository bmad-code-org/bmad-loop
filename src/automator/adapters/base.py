"""Coding-CLI adapter seam.

Adapters differ along three orthogonal capability axes, declared as class
attributes so the engine can reason about transport quality instead of
treating every CLI as a dumb terminal:

- injection:   how a prompt reaches the CLI
               "tmux-initial-prompt" | "launch-flag" | "http"
- observation: how turn/session completion is detected
               "hook-signal" | "sse" | "transcript-poll"
- state:       where session state is readable
               "local-jsonl" | "local-json-tree" | "remote"
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model import TokenUsage


@dataclass(frozen=True)
class SessionSpec:
    task_id: str
    role: str  # "dev" | "review" | "retro"
    prompt: str
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    model: str = ""  # empty = CLI default
    # fallback only; real dev/review/retro sessions get limits.session_timeout_min * 60
    timeout_s: float = 90 * 60
    # total stall wake-nudges this session may ever receive; None = unbounded
    # (today's behavior). Unlike the adapter's refillable per-silence budget,
    # this cap is monotonic — a session that keeps ending its turn without a
    # result cannot re-earn nudges forever. Set for injected workflow sessions
    # so a forgotten completion marker degrades to "stalled" instead of
    # livelocking until timeout_s.
    stall_nudges_cap: int | None = None


@dataclass(frozen=True)
class SessionHandle:
    task_id: str
    native_id: str  # tmux window id, HTTP session id, ...
    launched_ns: int = 0  # wall-clock ns just before launch; floor for hook events


@dataclass(frozen=True)
class SessionResult:
    status: str  # "completed" | "stalled" | "timeout" | "crashed"
    result_json: dict[str, Any] | None = None
    session_id: str | None = None
    transcript_path: str | None = None


class CodingCLIAdapter(ABC):
    name: str = "abstract"
    injection: str = ""
    observation: str = ""
    state: str = ""

    @abstractmethod
    def start_session(self, spec: SessionSpec) -> SessionHandle: ...

    @abstractmethod
    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult: ...

    def send_text(self, handle: SessionHandle, text: str) -> None:
        """Nudge a running session. Optional capability."""
        raise NotImplementedError(f"{self.name} cannot inject into a running session")

    def interactive_argv(self, spec: SessionSpec) -> list[str]:
        """argv that launches the CLI agent attached to the caller's terminal,
        seeded with spec.prompt. Used by the interactive escalation-resolution
        flow; optional capability (e.g. HTTP adapters have no terminal)."""
        raise NotImplementedError(f"{self.name} has no interactive (attached) session mode")

    def interactive_env(self, spec: SessionSpec) -> dict[str, str]:
        """Env vars to layer onto the caller's environment for interactive_argv."""
        return dict(spec.env)

    def kill(self, handle: SessionHandle) -> None:  # noqa: B027 - optional cleanup
        pass

    def read_usage(self, result: SessionResult) -> TokenUsage | None:
        return None

    def run(self, spec: SessionSpec) -> SessionResult:
        handle = self.start_session(spec)
        try:
            return self.wait_for_completion(handle, spec)
        finally:
            self.kill(handle)
