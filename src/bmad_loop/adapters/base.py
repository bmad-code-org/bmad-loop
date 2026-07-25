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
class SpecSnapshot:
    """Launch-state fingerprint of a review session's spec, captured by the
    engine immediately after the pre-review-launch marker strip
    (``_reset_spec_for_review``) and threaded onto its ``SessionSpec``.

    Lets the generic adapter's missing-marker fallback deterministically refuse
    to synthesize from a candidate whose bytes are byte-identical to the spec's
    launch state: such a spec is provably untouched by this session, so the
    terminal ``status:`` it carries is the PRIOR pass's ``done`` (re-opened for
    review), not proof this session finished (#276 M1).

    Process-transient: it rides the live ``SessionSpec`` only and is deliberately
    NOT persisted (no ``StoryTask.to_dict`` entry). A crash-resume that
    reconstructs the ``SessionSpec`` therefore carries no snapshot, and the
    fallback degrades to its conservative 2-observation fingerprint path."""

    path: str
    mtime_ns: int
    sha256: str
    fm_status: str


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
    # total stall wake-nudges this session may ever receive; None (the raw
    # constructor default) = unbounded. Unlike the adapter's refillable
    # per-silence budget, this cap is monotonic — a session that keeps ending
    # its turn without a result cannot re-earn nudges forever, because the
    # nudge is itself a submitted turn whose reply refills the budget (#149).
    # The engine sets it for every session it drives: workflow_stall_nudges_cap
    # for injected workflow sessions, dev_stall_nudges_cap otherwise, so a
    # missing completion artifact degrades to "stalled" instead of livelocking
    # until timeout_s.
    stall_nudges_cap: int | None = None
    # Mid-session token-budget guard (#158): weighted per-session cap the wait
    # loop samples cumulative usage against on its heartbeat cadence. None (the
    # raw constructor default) or mode "off" leaves the guard inert, so adapters
    # constructed outside the engine (tests, MockAdapter) are unaffected. The
    # engine sets these from limits.max_tokens_per_session /
    # limits.session_budget_mode / limits.session_budget_grace_s /
    # limits.cache_read_weight for every session it drives.
    token_budget: int | None = None
    token_budget_mode: str = "off"  # "off" | "warn" | "enforce"
    token_budget_grace_s: float = 240.0
    cache_read_weight: float = 0.1
    # Launch-state snapshot of a review session's spec (#276 M1): captured by the
    # engine right after the pre-review-launch marker strip and threaded here so
    # the generic adapter's missing-marker fallback can deterministically refuse
    # to synthesize from a spec still byte-identical to its launch state. None for
    # every non-review session and on a crash-resume (process-transient — see
    # SpecSnapshot). Kept LAST so positional SessionSpec constructions stay valid.
    spec_snapshot: SpecSnapshot | None = None


@dataclass(frozen=True)
class SessionHandle:
    task_id: str
    native_id: str  # tmux window id, HTTP session id, ...
    launched_ns: int = 0  # wall-clock ns just before launch; floor for hook events


@dataclass(frozen=True)
class SessionResult:
    status: str  # "completed" | "stalled" | "timeout" | "crashed" | "over_budget"
    result_json: dict[str, Any] | None = None
    session_id: str | None = None
    transcript_path: str | None = None
    # wall time.time() when wait_for_completion declared the deadline elapsed;
    # None unless this session's timeout actually fired (#157).
    timeout_fired_at: float | None = None
    # which clock(s) had expired at fire time: "monotonic" | "wall" | "both".
    # "wall" alone is the suspend signature — a frozen monotonic clock.
    timeout_expired_clock: str | None = None
    # weighted usage sampled when the session-budget guard tripped (#158); None
    # unless the guard tripped. Set on every post-trip exit — warn-mode sessions
    # that run to completion carry it too — so the engine can journal it.
    budget_weighted: int | None = None
    # transport-failure classification (#194): True when a non-completed session
    # was post-mortem-matched as an *environment fault* (the coding CLI lost its
    # API connection and idled out the session clock instead of doing real work).
    # Set by the _classify_env_fault hook; env_fault_evidence carries the matched,
    # ANSI-stripped log line. These two MUST stay the LAST fields so every
    # positional SessionResult construction in the codebase stays valid.
    env_fault: bool = False
    env_fault_evidence: str | None = None


@dataclass(frozen=True)
class ViewerConfig:
    """Metadata for a detached viewer window that runs the adapter's attached
    TUI alongside the headless server.

    Persists so the dashboard Log tab can render the viewer pane, ``bmad-loop
    attach`` and the TUI ``a`` action can prefer it, and teardown stops it
    first.  The mux backend stamps a unique window name so the multiplexer
    can kill / re-attach it idempotently.

    Fields are best-effort: ``viewer_window`` may be ``None`` when tmux is
    unavailable, and ``viewer_url``/``viewer_session`` come from whatever the
    viewer needs (for OpenCode they are the server URL and session id; other
    viewers may ignore them or use a different convention).
    """

    viewer_window: str | None = None
    viewer_url: str = ""
    viewer_session: str = ""
    viewer_cwd: str = ""
    viewer_command: str = ""


class CodingCLIAdapter(ABC):
    name: str = "abstract"
    injection: str = ""
    observation: str = ""
    state: str = ""
    # Optional viewer metadata, set by adapters that support `show_attached_ui`.
    # The engine reads this to persist metadata for attach/Log-tab/teardown.
    viewer_config: ViewerConfig = ViewerConfig()

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

    # ---------------------------------------------------------------- viewer

    def ensure_viewer_window(
        self, spec: SessionSpec, session_id: str, base_url: str
    ) -> ViewerConfig:
        """Create (or reuse) a detached tmux viewer window for this session.

        Subclasses override to wire the correct ``opencode attach``-style
        command.  Base behaviour: no-window (head-only).  Idempotent — a
        second call with the same task id returns the existing config.
        """
        return ViewerConfig()

    def close_viewer_window(self, config: ViewerConfig) -> None:
        """Teardown the viewer window; a no-op when window is None."""
        pass

    def kill(self, handle: SessionHandle) -> None:  # optional cleanup
        pass

    def read_usage(self, result: SessionResult) -> TokenUsage | None:
        return None

    def run(self, spec: SessionSpec) -> SessionResult:
        handle = self.start_session(spec)
        try:
            result = self.wait_for_completion(handle, spec)
        finally:
            self.kill(handle)
        result = self._post_kill_reconcile(handle, spec, result)
        return self._classify_env_fault(handle, spec, result)

    def _post_kill_reconcile(
        self, handle: SessionHandle, spec: SessionSpec, result: SessionResult
    ) -> SessionResult:
        """Last-chance reconcile after the session's window has been torn down.

        Runs only on the normal return path — a raising wait_for_completion
        still kills the window and propagates without reaching this hook.
        Base behavior: identity. Adapters whose completion trust keys on
        window death (see GenericDevAdapter) may re-inspect on-disk state here,
        now that the kill has settled the liveness question a live-window
        verdict had to leave open."""
        return result

    def _observe_tick(self, handle: SessionHandle, spec: SessionSpec) -> None:
        """Heartbeat-cadence hook for mid-session on-disk observation, called from
        the wait loop's heartbeat-throttled block (~every HEARTBEAT_INTERVAL_S; the
        first tick always fires). Base behavior: nothing. Adapters that drive a
        skill whose terminal on-disk state is heuristic to attribute may sample it
        here (see GenericDevAdapter's `_DevSynthesisMixin`, which records the spec's
        first non-terminal status transition to make a later terminal frontmatter
        deterministic proof this session wrote it, #276 M2). An observation seam
        only — it MUST NOT mutate session state or the spec, and any read failure is
        a sample it silently skips, never a verdict."""
        return None

    def _classify_env_fault(
        self, handle: SessionHandle, spec: SessionSpec, result: SessionResult
    ) -> SessionResult:
        """Last-chance post-mortem: label a non-completed session an environment
        fault (#194) when the CLI lost its API connection and idled out the
        session clock rather than doing real work.

        Runs LAST in ``run()`` — after ``_post_kill_reconcile`` — so a reconcile
        upgrade to ``completed`` is never re-classified, and only a genuinely
        non-completed verdict (``result_json is None``) is ever inspected. Base
        behavior: identity, like ``_post_kill_reconcile``, so adapters with no
        post-mortem signal (HTTP/mock) stay inert. Adapters that tee the pane
        (see GenericAdapter) may match profile patterns against the log tail here
        and stamp ``env_fault`` / ``env_fault_evidence`` onto the result."""
        return result
