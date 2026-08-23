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
    # The spec path this session is REQUIRED to write, when the orchestrator
    # already knows it (#261): `StoryTask.spec_file`, recorded by verify_dev /
    # verify_dev_bundle on dev success and handed to the review session in its own
    # prompt. Set for every leg with a recorded spec — always a review, and a dev
    # retry — and None on a dev attempt 1, whose spec does not exist yet.
    #
    # When set, the generic adapter reads back from THIS path instead of scanning
    # the implementation-artifacts dir for the newest qualifying `*.md`. That scan
    # is shared with every concurrent run: a foreign story's spec landing there
    # after launch (a merge-back into the main checkout, a human edit, a sweep)
    # wins on mtime and is adopted as this session's result, so a review that
    # produced nothing is scored `completed:done` and unreviewed code merges.
    #
    # Deliberately independent of `spec_snapshot`, which degrades to None on a torn
    # read: the identity constraint must not silently disappear with it.
    #
    # Unlike SpecSnapshot this SURVIVES a crash-resume. The field itself is not
    # persisted, but its source is: `StoryTask.spec_file` round-trips through
    # state.json (stored relative to the worktree, re-absolutized by
    # WorktreeFlow on resume), and the engine re-derives this on every launch. So a
    # resumed run is protected too — always an absolute path by the time it lands
    # here. Kept LAST alongside spec_snapshot so positional constructions stay valid.
    expected_spec: str | None = None


@dataclass(frozen=True)
class SessionHandle:
    task_id: str
    native_id: str  # tmux window id, HTTP session id, ...
    launched_ns: int = 0  # wall-clock ns just before launch; floor for hook events


@dataclass(frozen=True)
class SessionResult:
    # "aborted" is the in-session hard-stop verdict (#319): the wait loop saw a
    # `mode: "hard"` stop-request.json and tore the session down. It is an abort,
    # NEVER a completion — sessions complete only on hook Stop events or window
    # death (AGENTS.md) — and it never escapes `Engine._run_session`, which
    # unwinds it as a RunStopped before any SessionRecord is written.
    status: str  # "completed" | "stalled" | "timeout" | "crashed" | "over_budget" | "aborted"
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
    # ANSI-stripped log line. New fields are APPENDED below these, never inserted
    # among them, so every positional SessionResult construction stays valid.
    env_fault: bool = False
    env_fault_evidence: str | None = None
    # Whether a `Stop` hook event arrived during this session — the hook half of the
    # #261 proof-of-work gate (see `_ResultFileMixin._produced_work`). Deliberately
    # NOT `session_id is not None`: SessionStart and SessionEnd populate that too,
    # and both fire on a CLI that launched and wedged without doing anything. Stop
    # is the only canonical event that means a turn actually ended.
    stop_seen: bool = False
    # Set on a `crashed` verdict when the mux no longer reports the SESSION, not
    # just its window (#489) — see `GenericAdapter._session_vanished` for why the
    # two are otherwise indistinguishable. Diagnostic label only: it changes the
    # reason text, never the routing. Deliberately NOT carried by
    # `_post_kill_reconcile`'s hand-built result — that path gates on
    # stalled/timeout/over_budget, which this flag can never accompany; add it
    # there if `crashed` ever joins that rescue set.
    session_vanished: bool = False


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
        fault (#194) when the CLI never got usable work out of the provider —
        connection lost, or quota/rate limit refused — and idled out the session
        clock rather than doing real work.

        Runs LAST in ``run()`` — after ``_post_kill_reconcile`` — so a reconcile
        upgrade to ``completed`` is never re-classified, and only a genuinely
        non-completed verdict (``result_json is None``) is ever inspected. Base
        behavior: identity, like ``_post_kill_reconcile``, so an adapter with no
        session log at all (mock) stays inert.

        Any adapter that writes a per-task diagnostic log should mix in
        ``EnvFaultMixin``, which matches profile patterns against the tail of the
        file its ``ENV_FAULT_LOG_SUFFIX`` names and stamps ``env_fault`` /
        ``env_fault_evidence`` onto the result. That covers the tmux adapters
        (pane capture, ``<task_id>.log``) and the opencode HTTP adapter (the
        serve process's stdout/stderr, ``<task_id>.server.out``, NOT its
        conversation transcript) alike — the signal is the log, not the
        transport. This docstring used to say HTTP adapters had no
        post-mortem signal; that stopped being true once opencode_http began
        teeing its server log, and the stale premise is why a provider quota
        outage went unclassified and burned three stories' retry budgets."""
        return result
