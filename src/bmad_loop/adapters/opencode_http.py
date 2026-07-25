"""OpenCode driver: sessions over the local HTTP server, no tmux, no hooks.

OpenCode (opencode.ai) is client/server: its TUI is just a client of a local
HTTP server (``opencode serve``) exposing an OpenAPI 3.1 API. This adapter
drives sessions entirely over that API (``injection="http"``,
``observation="sse"``).

API contract — every fact below was pinned live against the real 1.18.2
binary (2026-07-16; the full probe record and the archived OpenAPI spec live
in git history, commit b85d8ca / PR #167). This list wins over memory:

- Readiness target is ``GET /global/health`` (``/health`` is the web-UI SPA
  shell). With ``OPENCODE_SERVER_PASSWORD`` set *every* endpoint 401s, health
  included — basic-auth username is literally ``opencode``; Bearer is
  rejected.
- SSE ``GET /event``: flat ``data:``-only frames, first frame
  ``server.connected``, ``server.heartbeat`` ≈ every 10 s, no server-side
  filtering — clients filter by ``properties.sessionID``.
- ``session.idle`` fires even when aborting an idle session, so an idle event
  alone is never proof a turn ran — it must pair with an assistant message
  whose ``time.completed`` post-dates the last prompt sent.
- Poll fallback ``GET /session/status`` returns a ``{sessionID: status}`` map
  in which idle sessions are simply **absent** — the rule is "absent ⇒ not
  busy", never "wait for idle".
- Per-prompt ``model`` is the object form ``{providerID, modelID}``; the
  ``"provider/model"`` string form belongs only to the config-file ``model``
  key.
- ``OPENCODE_CONFIG_CONTENT`` outranks the project's ``opencode.json``
  (applied as a final local-scope merge over all config files).
- Hermetic skills need ``OPENCODE_DISABLE_EXTERNAL_SKILLS=1`` **plus**
  ``skills.paths=["<worktree>/.claude/skills"]`` inside the config content —
  by default every server also sees the operator's personal
  ``~/.claude/skills`` and ``~/.agents/skills``.
- ``opencode serve`` survives parent SIGKILL (reparents to init and keeps
  serving); SIGTERM exits it cleanly.
- All ``time.*`` fields are epoch **milliseconds**; ``POST /session/:id/abort``
  returns ``200 true`` even when nothing is running.

Transport shape (the settled design drivers):

- **One ``opencode serve`` per session.** The API has no per-session env, and
  the ``BMAD_LOOP_*`` contract must reach tool subprocesses via the server
  process env — so each session gets its own server spawned with
  ``cwd=spec.cwd`` and the session env. Ports are OS-assigned free ports,
  re-picked on a bind race.
- **Config injected via ``OPENCODE_CONFIG_CONTENT``** (outranks the project's
  ``opencode.json``): a blanket permission allow (the bypass-flags
  analogue), the hermetic-skills recipe above (project ``.claude/skills``
  only — without it every session sees the operator's personal skills), and
  the policy model when set. A per-session ``OPENCODE_SERVER_PASSWORD`` makes
  the health poll self-discriminating against a foreign server on a reused
  port and keeps other local processes from driving an allow-all server.
- **SSE ``session.idle`` ≙ the Stop hook**, filtered to this session's id —
  child/subagent sessions share the stream and emit their own idles. SSE is
  lossy upstream, so a silent or reconnecting stream degrades to an HTTP poll
  (``GET /session/status`` + message-level proof-of-work): an idle event alone
  is NOT proof a turn ran (abort emits one on an idle session),
  so the fallback demands an assistant message completed *after* the last
  prompt this adapter sent. OpenCode timestamps are epoch **milliseconds**.
- **Server death ≙ window death** (``crashed``, landed artifact honored);
  stall verdicts under a live server pin ``accept_result=False`` — the
  #48/#53 artifact-distrust invariant, unchanged.
- **Usage is read over HTTP before teardown** (server state is sqlite, not a
  readable file tree): assistant-message token sums, stashed by session id,
  raw messages dumped to ``tasks/<task_id>/messages.json`` as the transcript.
  Child-session (subagent) tokens are not counted — the API scopes messages
  per session.
- ``opencode serve`` survives parent death, so teardown is
  authoritative: kill in ``run()``'s finally plus an atexit sweep. On Windows
  the binary is an npm ``.cmd`` shim, so the kill goes straight to the
  process-tree force-kill while the wrapper is still alive to enumerate.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import gates
from ..bmadconfig import ProjectPaths
from ..journal import LOGS_DIR
from ..model import TokenUsage
from ..policy import Policy
from ..process_host import ProcessHostError, get_process_host
from .base import CodingCLIAdapter, SessionHandle, SessionResult, SessionSpec, ViewerConfig
from .generic import (
    BUDGET_NUDGE_TEXT,
    HEARTBEAT_INTERVAL_S,
    NUDGE_TEXT,
    STALL_NUDGE_TEXT,
    _DevSynthesisMixin,
    _ResultFileMixin,
)
from .profile import CLIProfile
from .viewer_launch import _is_control_flow as _viewer_control_flow
from .viewer_launch import close_viewer_window as _close_viewer
from .viewer_launch import ensure_viewer_window as _ensure_viewer

if TYPE_CHECKING:
    from ..process_host import ProcessHost

# Spawn/readiness defaults; per-instance attributes so tests shrink them.
HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_S = 0.25
SSE_READ_TIMEOUT_S = 30.0  # server heartbeats ~10s; 3 misses = stream is gone
SILENCE_THRESHOLD_S = 30.0
RECONNECT_SLEEP_S = 1.0
KILL_WAIT_S = 5.0
# Straggler-reap poll cadence. Independent of generic's KILL_POLL_S: this teardown
# budgets waits at kill_wait_s scale (seconds), not the tmux grace scale, so a
# tight fixed poll keeps the reap responsive without a per-instance knob.
REAP_POLL_S = 0.1
SPAWN_ATTEMPTS = 3
POLL_TICK_S = 5.0  # max event-queue wait per loop tick (generic's cadence)

# Fixed basic-auth username in OPENCODE_SERVER_PASSWORD mode (Bearer is rejected).
AUTH_USER = "opencode"


class OpencodeServerError(Exception):
    """An ``opencode serve`` instance could not be spawned, readied or driven."""


def _require_httpx():
    """Import httpx lazily — it ships as the ``opencode`` extra, so the
    dep-free core never pays for it (the ``_psutil()`` pattern)."""
    try:
        import httpx  # intentional lazy import — optional extra
    except ImportError as exc:
        raise OpencodeServerError(
            "the opencode-http adapter needs httpx; "
            "install it with `pip install 'bmad-loop[opencode]'`"
        ) from exc
    return httpx


def _now_ms() -> int:
    """Wall clock in epoch milliseconds — OpenCode's ``time.*`` unit.
    Comparisons against ``SessionHandle.launched_ns`` must divide by 1e6 first;
    a raw ns-vs-ms comparison is always False and silently disables the poll
    fallback."""
    return time.time_ns() // 1_000_000


def _free_port() -> int:
    """An OS-assigned free localhost port. Racy by nature (the bind is
    released before ``opencode serve`` re-binds); the spawn loop retries with a
    fresh port when the server dies during the health poll."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _parse_sse_lines(lines) -> Any:
    """Minimal SSE frame parser: accumulate ``data:`` lines until a blank line,
    then yield the JSON-decoded payload. Tolerates comments, unknown fields and
    undecodable payloads (skipped) — the stream is advisory, never trusted."""
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                try:
                    yield json.loads("\n".join(data))
                except (json.JSONDecodeError, ValueError):
                    pass
                data = []
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip())


@dataclass
class _ServerSession:
    """Everything the adapter tracks for one live ``opencode serve``."""

    process: subprocess.Popen
    port: int
    base_url: str
    password: str
    log_fh: Any
    client: Any = None  # control httpx.Client — main thread only
    session_id: str = ""
    events: queue.Queue = field(default_factory=queue.Queue)
    sse_thread: threading.Thread | None = None
    sse_stop: threading.Event = field(default_factory=threading.Event)
    sse_connected: threading.Event = field(default_factory=threading.Event)
    # Bumped by the SSE reader on any non-heartbeat frame (any session — the
    # parent is silent while a child session streams, exactly like subagent
    # output in a tmux pane). The wait loop snapshots it to re-arm the
    # dev-stall grace window, mirroring generic._log_activity_key.
    activity: int = 0
    # Monotonic timestamp of the last SSE frame of any kind (heartbeats
    # included) — a healthy-but-quiet stream keeps this fresh, so the wait
    # loop only falls back to HTTP polling when BOTH its own dequeue clock and
    # this are stale (a dead reader thread leaves it stale, preserving the
    # degraded path).
    last_frame_monotonic: float = 0.0
    # Monotonic completion floor in epoch ms: the poll fallback only
    # synthesizes an idle for an assistant message completed strictly after
    # this. Starts at prompt-send, advances on every prompt this adapter sends
    # and on every completion it consumes — otherwise one stale completed
    # message re-synthesizes idle on every probe (each fake "Stop" refills the
    # stall budget) and the session livelocks or burns its nudges.
    floor_ms: int = 0


class OpencodeHttpAdapter(_ResultFileMixin, CodingCLIAdapter):
    injection = "http"
    observation = "sse"
    state = "remote"

    def __init__(
        self,
        run_dir: Path,
        policy: Policy,
        profile: CLIProfile,
        binary: str | None = None,
        extra_args: tuple[str, ...] | None = None,
        usage_grace_s: float | None = None,
        stop_without_result_nudges: int | None = None,
    ):
        self._httpx = _require_httpx()
        self.run_dir = run_dir
        self.policy = policy
        self.profile = profile
        self.name = profile.name
        self.binary = binary or profile.binary
        # None = no extra serve args; unlike the tmux adapters there are no
        # bypass flags to default to (permissions ride OPENCODE_CONFIG_CONTENT).
        self.extra_args = extra_args
        self._usage_grace_s = usage_grace_s if usage_grace_s is not None else profile.usage_grace_s
        self._stop_nudges = (
            stop_without_result_nudges
            if stop_without_result_nudges is not None
            else (
                profile.stop_without_result_nudges
                if profile.stop_without_result_nudges is not None
                else policy.limits.stop_without_result_nudges
            )
        )
        # Same base semantics as GenericAdapter: fail fast on a result-less
        # Stop. The Phase 4 dev subclass raises these via _configure_dev_knobs.
        self._stall_grace_s = 0.0
        self._stall_nudges = 0
        # Timing knobs — instance attributes so tests shrink them per-adapter.
        self.health_timeout_s = HEALTH_TIMEOUT_S
        self.health_poll_s = HEALTH_POLL_S
        self.sse_read_timeout_s = SSE_READ_TIMEOUT_S
        self.silence_threshold_s = SILENCE_THRESHOLD_S
        self.reconnect_sleep_s = RECONNECT_SLEEP_S
        self.kill_wait_s = KILL_WAIT_S
        self.poll_tick_s = POLL_TICK_S
        self.result_grace_s: float | None = None  # None = the mixin default
        self.tasks_dir = run_dir / "tasks"
        self.logs_dir = run_dir / LOGS_DIR
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, _ServerSession] = {}
        self._usage: dict[str, TokenUsage] = {}
        # Viewer window config — set in start_session if show_attached_ui;
        # the teardown path always reads it via getattr (safe even when the
        # session crashed before start_session returned).  Persisted to
        # viewer.json so the dashboard Log tab, attach routing, and resume
        # can find it even when re-reading a stopped run.
        self._viewer_config: ViewerConfig = ViewerConfig()
        # opencode serve survives parent death: sweep whatever is
        # still registered when the interpreter exits cooperatively. A hard
        # SIGKILL of the engine still leaks — documented residual risk.
        atexit.register(self._atexit_sweep)

    # ------------------------------------------------------------- spawning

    def _serve_argv(self, resolved_binary: str, port: int) -> list[str]:
        """argv for one server. A seam: tests monkeypatch it to launch the
        FakeOpencode sidecar wrapper-free."""
        extra = self.extra_args or ()
        return [
            resolved_binary,
            "serve",
            "--port",
            str(port),
            "--hostname",
            "127.0.0.1",
            "--print-logs",
            *extra,
        ]

    def _config_content(self, spec: SessionSpec) -> str:
        """The OPENCODE_CONFIG_CONTENT JSON for this session:
        blanket permission allow (the bypass-flags analogue), the hermetic
        skills path (project skills only, paired with
        OPENCODE_DISABLE_EXTERNAL_SKILLS=1 in the env), and the model when the
        policy sets one (config-file model is the "provider/model" string
        form)."""
        config: dict[str, Any] = {
            "permission": "allow",
            "skills": {"paths": [str(Path(spec.cwd) / self.profile.skill_tree)]},
        }
        if spec.model:
            config["model"] = spec.model
        return json.dumps(config)

    def _session_env(self, spec: SessionSpec, password: str) -> dict[str, str]:
        return {
            **os.environ,
            **self.profile.env,
            **spec.env,
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_CONFIG_CONTENT": self._config_content(spec),
        }

    def _make_client(self, sess: _ServerSession):
        return self._httpx.Client(
            base_url=sess.base_url,
            auth=(AUTH_USER, sess.password),
            timeout=self._httpx.Timeout(10.0, connect=5.0),
        )

    def _spawn_server(self, spec: SessionSpec) -> _ServerSession:
        """Spawn `opencode serve` and wait for readiness, retrying with a fresh
        port when the process dies during the health poll (the free-port bind
        is released before the server re-binds, so a collision is possible)."""
        resolved = shutil.which(self.binary)
        if resolved is None:
            # shutil.which honors PATHEXT — on Windows the npm install is an
            # `opencode.cmd` shim that a bare-name Popen (which appends only
            # .exe) would never find.
            raise OpencodeServerError(
                f"opencode binary {self.binary!r} not found on PATH; " f"see `bmad-loop validate`"
            )
        password = secrets.token_urlsafe(16)
        env = self._session_env(spec, password)
        log_path = self.logs_dir / f"{spec.task_id}.log"
        log_fh = log_path.open("ab")  # append: retries share one log
        last_error = "server did not become healthy"
        try:
            for _ in range(SPAWN_ATTEMPTS):
                port = _free_port()
                process = subprocess.Popen(  # argv built from profile
                    self._serve_argv(resolved, port),
                    cwd=str(spec.cwd),
                    env=env,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
                sess = _ServerSession(
                    process=process,
                    port=port,
                    base_url=f"http://127.0.0.1:{port}",
                    password=password,
                    log_fh=log_fh,
                )
                if self._await_healthy(sess):
                    sess.client = self._make_client(sess)
                    return sess
                # Died or never readied: reap and try a fresh port. A live-but-
                # unhealthy server is killed too — never leak what we spawned.
                if process.poll() is None:
                    last_error = "server did not become healthy in time"
                    self._kill_process(sess)
                else:
                    last_error = f"server exited rc={process.returncode} during startup"
        except BaseException:
            log_fh.close()
            raise
        log_fh.close()
        raise OpencodeServerError(
            f"could not start `{self.binary} serve` after {SPAWN_ATTEMPTS} attempts "
            f"({last_error}); log: {log_path}"
        )

    def _await_healthy(self, sess: _ServerSession) -> bool:
        """Poll /global/health (authenticated) until it answers healthy. The
        process liveness is re-checked after *every* probe: a foreign server
        answering 200 on a stolen port must not mask our own corpse, and the
        auth + shape check means a foreign 200 without our password never
        reads as ready."""
        deadline = time.monotonic() + self.health_timeout_s
        with self._make_client(sess) as client:
            while time.monotonic() < deadline:
                healthy = False
                try:
                    resp = client.get("/global/health")
                    healthy = resp.status_code == 200 and resp.json().get("healthy") is True
                except Exception:  # not up yet (conn refused, junk)
                    healthy = False
                if sess.process.poll() is not None:
                    return False
                if healthy:
                    return True
                time.sleep(self.health_poll_s)
        return False

    # -------------------------------------------------------------- adapter

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        task_dir = self.tasks_dir / spec.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "prompt.txt").write_text(spec.prompt + "\n", encoding="utf-8")
        # A re-armed/resumed run reuses task_ids; drop any prior cycle's result
        # so a session that writes nothing can't be read as a stale completion.
        (task_dir / "result.json").unlink(missing_ok=True)

        launched_ns = time.time_ns()
        sess = self._spawn_server(spec)
        # Registered before the API handshake so the atexit sweep (and kill())
        # covers a crash mid-setup; run()'s finally-kill only exists once
        # start_session has returned a handle.
        self._sessions[spec.task_id] = sess
        try:
            resp = sess.client.post("/session", json={"title": spec.task_id})
            if resp.status_code != 200:
                raise OpencodeServerError(
                    f"POST /session failed: {resp.status_code} {resp.text[:200]}"
                )
            sess.session_id = resp.json()["id"]

            self._start_sse_reader(sess)
            # Wait for the stream to actually attach before prompting: a fast
            # turn can emit session.idle before the subscription exists, and a
            # lost idle degrades every completion to the (slow) poll fallback.
            if not sess.sse_connected.wait(timeout=self.health_timeout_s):
                raise OpencodeServerError("event stream did not connect")

            self._prompt(sess, self.profile.render_prompt(spec.prompt))
        except Exception:
            self._sessions.pop(spec.task_id, None)
            self._teardown(sess)
            raise

        # Viewer window: created after the session is live so the attach URL and
        # session id are available.  Best-effort — failures degrade to headless.
        if self.policy.adapter.show_attached_ui:
            self._viewer_config = self._make_viewer_config(spec.task_id, sess, spec.cwd)
        else:
            self._viewer_config = ViewerConfig()

        return SessionHandle(
            task_id=spec.task_id, native_id=sess.session_id, launched_ns=launched_ns
        )

    def _prompt(self, sess: _ServerSession, text: str) -> None:
        """prompt_async — the injection primitive for both the initial prompt
        and the nudges. Advances the completion floor: anything completed
        before this send is a previous turn's evidence. The floor moves only
        once the server ACCEPTS the prompt (204) — a rejected/failed send
        starts no new turn, and consuming the floor for it would discard
        still-valid completion evidence of the previous turn."""
        sent_ms = _now_ms()  # sampled before the POST: it precedes the new turn
        resp = sess.client.post(
            f"/session/{sess.session_id}/prompt_async",
            json={"parts": [{"type": "text", "text": text}]},
        )
        if resp.status_code != 204:
            raise OpencodeServerError(f"prompt_async failed: {resp.status_code} {resp.text[:200]}")
        sess.floor_ms = max(sess.floor_ms, sent_ms)

    def send_text(self, handle: SessionHandle, text: str) -> None:
        """Nudge the running session. Best-effort: a server that died between
        the liveness probe and the nudge is caught as `crashed` on the next
        tick, not by blowing up the completion loop."""
        sess = self._sessions.get(handle.task_id)
        if sess is None:
            return
        try:
            self._prompt(sess, text)
        except Exception:  # nosec B110 - next tick's poll() settles liveness
            pass

    def _start_sse_reader(self, sess: _ServerSession) -> None:
        thread = threading.Thread(
            target=self._sse_loop,
            args=(sess,),
            name=f"opencode-sse-{sess.port}",
            daemon=True,
        )
        sess.sse_thread = thread
        thread.start()

    def _sse_loop(self, sess: _ServerSession) -> None:
        """SSE reader: owns its own client (created and closed here — kill()
        never touches it; killing the server is what unblocks the read, with
        the read timeout as backstop). Filters idle/error to this session's id
        (child sessions share the stream), counts every other non-heartbeat
        frame as activity, and turns any disconnect into a single `gap`
        sentinel so the wait loop probes over HTTP for what the stream may
        have dropped."""
        httpx = self._httpx
        while not sess.sse_stop.is_set():
            try:
                with httpx.Client(
                    base_url=sess.base_url,
                    auth=(AUTH_USER, sess.password),
                    timeout=httpx.Timeout(5.0, read=self.sse_read_timeout_s),
                ) as client:
                    with client.stream("GET", "/event") as resp:
                        if resp.status_code != 200:
                            raise OpencodeServerError(f"/event -> {resp.status_code}")
                        # The server registers the subscriber once the response
                        # starts; events published after this are delivered.
                        sess.sse_connected.set()
                        for event in _parse_sse_lines(resp.iter_lines()):
                            if sess.sse_stop.is_set():
                                return
                            self._dispatch_sse(sess, event)
            except Exception:  # nosec B110 - reader must never die silently
                pass
            if sess.sse_stop.is_set():
                return
            sess.events.put("gap")
            sess.sse_stop.wait(self.reconnect_sleep_s)

    def _dispatch_sse(self, sess: _ServerSession, event: Any) -> None:
        if not isinstance(event, dict):
            return
        sess.last_frame_monotonic = time.monotonic()
        etype = event.get("type")
        if etype in ("server.heartbeat", "server.connected"):
            return
        # Any substantive frame — including child-session traffic — proves the
        # session tree is working (the parent is silent while a subagent
        # streams, exactly like subagent output in a tmux pane log).
        sess.activity += 1
        props = event.get("properties") or {}
        if props.get("sessionID") != sess.session_id:
            return
        if etype == "session.idle":
            sess.events.put("idle")
        elif etype == "session.error":
            sess.events.put("error")

    # ------------------------------------------------------ completion loop

    def _result_json(self, handle: SessionHandle, spec: SessionSpec, *, wait: bool) -> dict | None:
        if not wait:
            return self._read_result(handle.task_id)
        # Pass the grace explicitly: the mixin's grace_s default is bound at
        # def time, so an instance override is the only reachable knob.
        if self.result_grace_s is not None:
            return self._await_result(handle.task_id, grace_s=self.result_grace_s)
        return self._await_result(handle.task_id)

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        sess = self._sessions.get(handle.task_id)
        if sess is None:
            raise OpencodeServerError(f"no live opencode server for task {handle.task_id!r}")
        deadline = time.monotonic() + spec.timeout_s
        # Wall-clock co-bound (#157): a host suspend freezes time.monotonic(),
        # silently extending the monotonic deadline by the nap's length. The
        # wall clock keeps counting through a suspend, so it may EXPIRE the
        # deadline — never extend it; all sub-waits below stay monotonic (a
        # wall clock stepped backward must not stretch the session).
        wall_deadline = time.time() + spec.timeout_s
        session_id = sess.session_id
        nudges_left = self._stop_nudges
        # Mirrors generic.wait_for_completion: stall-grace window armed by a
        # result-less Stop (idle), re-armed by activity, spent via wake-nudges
        # bounded by the monotonic spec.stall_nudges_cap (#149).
        stall_deadline: float | None = None
        last_activity: int | None = None
        stall_nudges_left = self._stall_nudges
        stall_nudges_sent = 0
        # Loop-owned silence clock: updated on every dequeue, so a dead reader
        # thread degrades to the poll fallback instead of disabling it.
        last_seen = time.monotonic()
        # monotonic ts of the last heartbeat.json overwrite; None = not yet
        # written, so the first tick always stamps one.
        last_heartbeat: float | None = None
        # Session-budget guard (#158), mirroring generic.wait_for_completion:
        # latched on the first cap crossing; budget_deadline is the enforce-mode
        # monotonic grace expiry, checked every tick (sampling itself rides the
        # heartbeat cadence).
        budget_tripped = False
        budget_weighted: int | None = None
        budget_deadline: float | None = None
        # wall-clock co-bound for the grace (#157 pattern): a host suspend
        # freezes time.monotonic(), silently stretching the "bounded" wrap-up
        # window; the wall clock may EXPIRE the grace — never extend it.
        budget_wall_deadline: float | None = None

        while True:
            remaining = deadline - time.monotonic()
            wall_expired = time.time() >= wall_deadline
            if remaining <= 0 or wall_expired:
                if remaining <= 0 and wall_expired:
                    expired = "both"
                elif remaining <= 0:
                    expired = "monotonic"
                else:
                    # wall-only expiry with monotonic time to spare: the
                    # monotonic clock stood still — the suspend signature.
                    expired = "wall"
                self._note_lifecycle(
                    handle.task_id,
                    "timeout-fired",
                    expired_clock=expired,
                    timeout_s=spec.timeout_s,
                    mono_remaining_s=round(remaining, 3),
                )
                self._abort(sess)
                transcript = self._capture_usage(handle, sess)
                return SessionResult(
                    status="timeout",
                    session_id=session_id,
                    transcript_path=transcript,
                    timeout_fired_at=time.time(),
                    timeout_expired_clock=expired,
                    budget_weighted=budget_weighted,
                )
            now = time.monotonic()
            if last_heartbeat is None or now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                last_heartbeat = now
                self._write_heartbeat(
                    handle.task_id,
                    {
                        "ts": time.time(),
                        "remaining_s": round(remaining, 3),
                        "stall_armed": stall_deadline is not None,
                        "stall_nudges_sent": stall_nudges_sent,
                    },
                )
                # Mid-session spec-status transition sampling (#276 M2) rides the
                # same heartbeat cadence — a no-op on the plain HTTP adapter; the
                # OpencodeDevAdapter shares _DevSynthesisMixin, so the hook records
                # transitions there exactly as on the generic dev adapter.
                self._observe_tick(handle, spec)
                # Budget sampling rides the heartbeat cadence — no extra knob.
                # Usage comes over HTTP (server state is sqlite, not a file).
                if (
                    not budget_tripped
                    and spec.token_budget is not None
                    and spec.token_budget_mode in ("warn", "enforce")
                ):
                    weighted = self._sample_weighted_usage(sess, spec)
                    if weighted is not None and weighted > spec.token_budget:
                        budget_tripped = True
                        budget_weighted = weighted
                        self._note_lifecycle(
                            handle.task_id,
                            "budget-tripped",
                            weighted=weighted,
                            budget=spec.token_budget,
                            mode=spec.token_budget_mode,
                        )
                        try:
                            gates.notify(
                                self.policy,
                                self.run_dir,
                                "bmad-loop session over token budget",
                                f"{handle.task_id}: weighted spend {weighted} crossed the "
                                f"{spec.token_budget} per-session cap "
                                f"(mode={spec.token_budget_mode})",
                            )
                        except OSError:
                            # observe-degrade: an unwritable ATTENTION file is
                            # observability, never a reason to break the loop
                            # (the _write_heartbeat doctrine).
                            pass
                        # nosec below: bandit B105 pattern-matches the "token"
                        # in token_budget_mode as a hardcoded-password compare;
                        # it is a mode enum, not a credential.
                        if spec.token_budget_mode == "enforce":  # nosec B105
                            if spec.token_budget_grace_s <= 0:
                                # zero grace = terminate at trip, no nudge — but
                                # server death still wins (artifact honored via
                                # the crash path), exactly like grace expiry.
                                if sess.process.poll() is not None:
                                    transcript = self._capture_usage(handle, sess)
                                    return self._final(
                                        handle,
                                        spec,
                                        "crashed",
                                        session_id,
                                        transcript,
                                        budget_weighted=weighted,
                                    )
                                self._note_lifecycle(
                                    handle.task_id,
                                    "over-budget-fired",
                                    weighted=weighted,
                                    budget=spec.token_budget,
                                    grace_s=spec.token_budget_grace_s,
                                    zero_grace=True,
                                )
                                self._abort(sess)
                                transcript = self._capture_usage(handle, sess)
                                return SessionResult(
                                    status="over_budget",
                                    session_id=session_id,
                                    transcript_path=transcript,
                                    budget_weighted=weighted,
                                )
                            try:
                                self.send_text(handle, BUDGET_NUDGE_TEXT)
                            except Exception:  # nosec B110 - best-effort nudge
                                # a dead/hung server can't take the nudge; the
                                # grace still arms — the next tick's process
                                # poll scores a dead server crashed.
                                pass
                            budget_deadline = time.monotonic() + spec.token_budget_grace_s
                            budget_wall_deadline = time.time() + spec.token_budget_grace_s
            if budget_deadline is not None and (
                time.monotonic() >= budget_deadline
                or (budget_wall_deadline is not None and time.time() >= budget_wall_deadline)
            ):
                # Grace expired with no completion (wall co-bound included: a
                # suspend-frozen monotonic clock must not stretch the window,
                # #157). Server death ≙ window death (the crash path honors a
                # landed artifact); a live server ends over_budget WITHOUT
                # reading the result file — an artifact under a live session is
                # never trusted (#48/#53).
                if sess.process.poll() is not None:
                    transcript = self._capture_usage(handle, sess)
                    return self._final(
                        handle,
                        spec,
                        "crashed",
                        session_id,
                        transcript,
                        budget_weighted=budget_weighted,
                    )
                self._note_lifecycle(
                    handle.task_id,
                    "over-budget-fired",
                    weighted=budget_weighted,
                    budget=spec.token_budget,
                    grace_s=spec.token_budget_grace_s,
                    zero_grace=False,
                )
                self._abort(sess)
                transcript = self._capture_usage(handle, sess)
                return SessionResult(
                    status="over_budget",
                    session_id=session_id,
                    transcript_path=transcript,
                    budget_weighted=budget_weighted,
                )
            try:
                event: str | None = sess.events.get(timeout=min(remaining, self.poll_tick_s))
            except queue.Empty:
                event = None
            if event is not None:
                last_seen = time.monotonic()

            if event == "error":
                # session.error may precede a retry, not a turn-end (status
                # "retry" exists); only a PROVABLY settled session reads as a
                # result-less Stop — an errored turn may never get
                # time.completed, so waiting on proof-of-work alone would burn
                # the whole timeout. A dead server takes the crash path; an
                # unknowable status (probe failure) keeps waiting rather than
                # mis-nudging a session that may still be retrying — timeout_s
                # bounds a persistently unreadable one.
                if sess.process.poll() is not None:
                    transcript = self._capture_usage(handle, sess)
                    return self._final(
                        handle,
                        spec,
                        "crashed",
                        session_id,
                        transcript,
                        budget_weighted=budget_weighted,
                    )
                if self._session_status(sess) is not False:
                    continue
                event = "idle"

            if event in (None, "gap"):
                if sess.process.poll() is not None:
                    # Server death ≙ window death: the crash path vouches for a
                    # landed artifact (accept_result=True), same as generic.
                    transcript = self._capture_usage(handle, sess)
                    return self._final(
                        handle,
                        spec,
                        "crashed",
                        session_id,
                        transcript,
                        budget_weighted=budget_weighted,
                    )
                silent = (
                    time.monotonic() - max(last_seen, sess.last_frame_monotonic)
                    > self.silence_threshold_s
                )
                if (event == "gap" or silent) and self._probe_completion(sess):
                    event = "idle"  # fall through to the Stop path below
                    last_seen = time.monotonic()
                else:
                    if stall_deadline is not None:
                        # Re-arm on activity (any SSE traffic since arming) —
                        # a session streaming subagent work is working, not
                        # stalled; only genuine silence trips the stall below.
                        key = sess.activity
                        if last_activity is None or key != last_activity:
                            last_activity = key
                            stall_deadline = time.monotonic() + self._stall_grace_s
                            continue
                        if time.monotonic() >= stall_deadline:
                            if stall_nudges_left > 0 and (
                                spec.stall_nudges_cap is None
                                or stall_nudges_sent < spec.stall_nudges_cap
                            ):
                                if self._session_status(sess):
                                    # Provably mid-turn (a busy child/parent the
                                    # SSE missed): re-arm rather than injecting a
                                    # prompt into a working session. Unknown
                                    # (None) proceeds to the nudge — a transport
                                    # too broken to answer the probe would fail
                                    # the nudge too, and burning the bounded
                                    # budget converges to an honest stall.
                                    stall_deadline = time.monotonic() + self._stall_grace_s
                                    continue
                                stall_nudges_left -= 1
                                stall_nudges_sent += 1
                                self.send_text(handle, STALL_NUDGE_TEXT)
                                stall_deadline = time.monotonic() + self._stall_grace_s
                                last_activity = sess.activity
                                continue
                            # Re-probe liveness before finalizing: a hard death
                            # in the gap since the top-of-tick check flows
                            # through the crash path (artifact honored) instead
                            # of a stall that discards a just-flushed result.
                            if sess.process.poll() is not None:
                                transcript = self._capture_usage(handle, sess)
                                return self._final(
                                    handle,
                                    spec,
                                    "crashed",
                                    session_id,
                                    transcript,
                                    budget_weighted=budget_weighted,
                                )
                            transcript = self._capture_usage(handle, sess)
                            return self._final(
                                handle,
                                spec,
                                "stalled",
                                session_id,
                                transcript,
                                accept_result=False,
                                budget_weighted=budget_weighted,
                            )
                    continue

            if event == "idle":
                result_json = self._result_json(handle, spec, wait=True)
                if result_json is not None:
                    transcript = self._capture_usage(handle, sess)
                    return SessionResult(
                        status="completed",
                        result_json=result_json,
                        session_id=session_id,
                        transcript_path=transcript,
                        budget_weighted=budget_weighted,
                    )
                if nudges_left > 0:
                    nudges_left -= 1
                    self.send_text(handle, NUDGE_TEXT)
                    continue
                if self._stall_grace_s <= 0:
                    transcript = self._capture_usage(handle, sess)
                    return self._final(
                        handle,
                        spec,
                        "stalled",
                        session_id,
                        transcript,
                        budget_weighted=budget_weighted,
                    )
                # A result-less Stop, but the session may have ended its turn
                # awaiting a background process: open/re-arm the idle-grace
                # window; a fresh Stop lands here again and resets it.
                stall_deadline = time.monotonic() + self._stall_grace_s
                last_activity = sess.activity
                # a real turn-end proves the session responsive: restore the
                # wake-nudge budget (the monotonic cap still bounds the total).
                stall_nudges_left = self._stall_nudges
                continue

    def _session_status(self, sess: _ServerSession) -> bool | None:
        """Tri-state /session/status probe: True = busy/retrying, False =
        provably settled (absent from the map or explicit idle),
        None = unknowable (unreachable server, non-200). Unknown is not
        settled: callers must not treat a failed probe as proof a turn ended
        (the liveness-parity doctrine); the process poll settles real death."""
        try:
            resp = sess.client.get("/session/status")
            if resp.status_code != 200:
                return None
            status = resp.json().get(sess.session_id) or {}
            return status.get("type") in ("busy", "retry")
        except Exception:  # probe is advisory
            return None

    def _probe_completion(self, sess: _ServerSession) -> bool:
        """HTTP fallback for a lossy stream: the turn is finished
        only when the session is PROVABLY settled (an unknowable status is not
        settled) AND an assistant message completed strictly after the
        completion floor exists — an idle status alone is not proof a turn ran
        (abort emits idle even on an idle session). Consuming the evidence advances the floor so one
        completion can never be consumed twice."""
        if self._session_status(sess) is not False:
            return False
        try:
            resp = sess.client.get(f"/session/{sess.session_id}/message")
            if resp.status_code != 200:
                return False
            completed = 0
            for msg in resp.json():
                info = msg.get("info") or {}
                if info.get("role") != "assistant":
                    continue
                done_ms = (info.get("time") or {}).get("completed") or 0
                completed = max(completed, int(done_ms))
        except Exception:  # probe is advisory
            return False
        if completed > sess.floor_ms:
            sess.floor_ms = completed
            return True
        return False

    def _abort(self, sess: _ServerSession) -> None:
        if sess.client is None or not sess.session_id or sess.process.poll() is not None:
            return
        try:
            sess.client.post(f"/session/{sess.session_id}/abort")
        except Exception:  # nosec B110 - abort is best-effort
            pass

    # ----------------------------------------------------------------- usage

    def _sample_weighted_usage(self, sess: _ServerSession, spec: SessionSpec) -> int | None:
        """Mid-session cumulative weighted spend over HTTP, or None when the
        guard must stay inert this tick (no live session yet, non-200, a
        transport error). Never raises — sampling must not break the wait
        loop."""
        if sess.client is None or not sess.session_id:
            return None
        try:
            resp = sess.client.get(f"/session/{sess.session_id}/message")
            if resp.status_code != 200:
                return None
            usage = _sum_usage(resp.json())
        except Exception:  # sampling is advisory
            return None
        return usage.weighted_total(spec.cache_read_weight)

    def _capture_usage(self, handle: SessionHandle, sess: _ServerSession) -> str | None:
        """Read usage over HTTP before teardown (state is server-side sqlite):
        dump the raw messages as the transcript and stash the token sum by
        session id for read_usage(). Best-effort in full — the crashed path
        runs this against a dead server and the verdict must not change."""
        if sess.client is None or not sess.session_id:
            return None
        try:
            resp = sess.client.get(f"/session/{sess.session_id}/message")
            if resp.status_code != 200:
                return None
            messages = resp.json()
            path = self.tasks_dir / handle.task_id / "messages.json"
            path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
            self._usage[sess.session_id] = _sum_usage(messages)
            return str(path)
        except Exception:  # usage is metadata, never a gate
            return None

    def read_usage(self, result: SessionResult) -> TokenUsage | None:
        if not result.session_id:
            return None
        return self._usage.get(result.session_id)

    # -------------------------------------------------------------- teardown

    def kill(self, handle: SessionHandle) -> None:
        # Teardown order: viewer window → server/session → process.
        # Idempotent — all operations tolerate being called on gone targets.
        self._close_viewer()
        sess = self._sessions.pop(handle.task_id, None)
        if sess is None:
            return
        self._abort(sess)
        self._teardown(sess)

    def _make_viewer_config(self, task_id: str, sess: "_ServerSession", cwd: Path) -> ViewerConfig:
        """Create a viewer window after the server + session are live.
        Best-effort — a missing opencode binary or mux failure degrades to
        a no-op ViewerConfig (headless-only).  Persists config to disk."""
        run_id = self.run_dir.name
        try:
            vc = _ensure_viewer(
                run_id=run_id,
                server_url=sess.base_url,
                session_id=sess.session_id,
                working_directory=cwd,
                attach_binary=shutil.which("opencode") or "opencode",
                run_dir=self.run_dir,
                # The serve process 401s everything without its per-session
                # password; `opencode attach` reads this var for basic auth.
                attach_env={"OPENCODE_SERVER_PASSWORD": sess.password},
            )
        except Exception as exc:  # nosec B110 - best effort
            if _viewer_control_flow(exc):
                raise  # the engine's stop/pause landed here — never swallow it
            vc = ViewerConfig()
        self.viewer_config = vc
        # Persist so the dashboard/attach/resume can read back even from
        # a dead run dir.
        self._persist_viewer_config(vc)
        return vc

    def _persist_viewer_config(self, vc: ViewerConfig) -> None:
        """Write viewer metadata to <run_dir>/viewer.json (append-only
        keyed by adapter name, so a resumed run overwrites the previous entry)."""
        try:
            path = self.run_dir / "viewer.json"
            if path.is_file():
                try:
                    entries = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    entries = []
            else:
                entries = []
            entries = [e for e in entries if e.get("adapter") != self.name] + [
                {
                    "adapter": self.name,
                    "task_id": getattr(self, "_current_task_id", ""),
                    "viewer_window": vc.viewer_window or "",
                    "viewer_url": vc.viewer_url or "",
                    "viewer_session": vc.viewer_session or "",
                    "viewer_cwd": vc.viewer_cwd or "",
                    "viewer_command": vc.viewer_command or "",
                }
            ]
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entries, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(path)
        except Exception:  # nosec B110 - best attempt, never raise
            pass

    @classmethod
    def load_viewer_config(cls, run_dir: Path, adapter_name: str) -> ViewerConfig | None:
        """Read the most recent viewer config written by *adapter_name*.
        Returns ``None`` when there is no recorded config or the file is unreadable."""
        try:
            path = run_dir / "viewer.json"
            if not path.is_file():
                return None
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                return None
            for e in entries:
                if e.get("adapter") == adapter_name:
                    return ViewerConfig(
                        viewer_window=e.get("viewer_window") or None,
                        viewer_url=e.get("viewer_url") or "",
                        viewer_session=e.get("viewer_session") or "",
                        viewer_cwd=e.get("viewer_cwd") or "",
                        viewer_command=e.get("viewer_command") or "",
                    )
            return None
        except Exception:  # nosec B110 - best effort read
            return None

    def _close_viewer(self) -> None:
        """Teardown the viewer window. Idempotent: close_viewer_window
        tolerates a missing target."""
        wc = getattr(self, "_viewer_config", ViewerConfig())
        _close_viewer(wc.viewer_window)

    def close_viewer_window(self, target: str | None = None) -> None:
        """Adapter-seam close — delegates to the internal path."""
        self._close_viewer()

    def ensure_viewer_window(
        self, spec: SessionSpec, session_id: str, base_url: str
    ) -> ViewerConfig:
        """Adapter-seam ensure — delegates to the internal path."""
        run_id = self.run_dir.name
        # The viewer must authenticate against the per-session serve password;
        # find the live session this id belongs to (absent for a foreign id —
        # the viewer then runs unauthenticated and shows the server's 401).
        password = next(
            (s.password for s in self._sessions.values() if s.session_id == session_id),
            None,
        )
        try:
            return _ensure_viewer(
                run_id=run_id,
                server_url=base_url,
                session_id=session_id,
                working_directory=spec.cwd,
                attach_binary=shutil.which("opencode") or "opencode",
                run_dir=self.run_dir,
                attach_env=({"OPENCODE_SERVER_PASSWORD": password} if password else None),
            )
        except Exception as exc:  # nosec B110 - best effort
            if _viewer_control_flow(exc):
                raise  # the engine's stop/pause landed here — never swallow it
            return ViewerConfig()

    def _teardown(self, sess: _ServerSession) -> None:
        sess.sse_stop.set()
        self._kill_process(sess)
        if sess.sse_thread is not None:
            # Bounded: killing the server closed the stream socket, which
            # unblocks the reader; the join is a courtesy, never a gate.
            sess.sse_thread.join(timeout=2.0)
        if sess.client is not None:
            try:
                sess.client.close()
            except Exception:  # nosec B110 - closing is best-effort
                pass
        try:
            sess.log_fh.close()
        except OSError:
            pass

    def _kill_process(self, sess: _ServerSession) -> None:
        process = sess.process
        if process.poll() is not None:
            return
        try:
            host = get_process_host()
        except ProcessHostError:
            # An explicit-but-bogus BMAD_LOOP_PROCESS_HOST override raises loudly
            # (deliberate doctrine — never silently mis-signal). But the lookup
            # precedes the first signal, so the server must not be left alive
            # behind the raise: one legacy Popen root strike (no host → no tree
            # kill; the win32 kill() reaps only the .cmd wrapper — an accepted
            # degrade on a loud config error), then re-raise. Mirrors the tmux
            # adapter's kill().
            try:
                if sys.platform == "win32":
                    process.kill()
                else:
                    process.terminate()
            except OSError:
                pass
            raise
        # Harvest the server's descendant tree BEFORE the first signal, while the
        # tree is intact (#183): a tool subprocess the server detached (setsid, a
        # double-fork) outlives the root's SIGTERM and would keep writing into the
        # worktree the engine is about to merge/remove. Post-kill it reparents to
        # init and is unreachable, so snapshot it now; each member's pid-reuse
        # identity rides along from the enumeration itself, and the reap below is
        # identity-guarded via alive_and_ours, never a bare (reusable) pid. {} =
        # no descendants / psutil absent → the root ladder alone, as before.
        tree = host.descendants(process.pid)
        # The live Popen handle pins the pid (win32 handle / unreaped POSIX
        # child), so signalling it cannot hit a reused pid — the identity
        # confirmation force_kill's contract asks for.
        if sys.platform == "win32":
            # Both the npm install and the test launcher are `.cmd` wrappers:
            # Popen.pid is cmd.exe. Go straight to the tree force-kill while
            # the tree is still intact — a polite taskkill can reap cmd.exe
            # alone (orphaning the server with the port bound), and once the
            # wrapper is gone `/T` can never enumerate the child again.
            try:
                host.force_kill(process.pid)
            except Exception:  # nosec B110 - already-gone races are fine
                pass
        else:
            try:
                host.terminate(process.pid)  # SIGTERM exits opencode cleanly
            except OSError:
                pass
        try:
            process.wait(timeout=self.kill_wait_s)
        except subprocess.TimeoutExpired:
            try:
                host.force_kill(process.pid)
            except Exception:  # nosec B110 - already-gone races are fine
                pass
            try:
                process.wait(timeout=self.kill_wait_s)
            except subprocess.TimeoutExpired:
                pass
        self._reap_descendants(host, tree)

    def _reap_descendants(self, host: ProcessHost, tree: dict[int, float | None]) -> None:
        """Reap harvested straggler descendants the root signal missed — a detached
        tool subprocess that escaped the server's process group. Terminate → bounded
        wait ≤ ``kill_wait_s`` → force-kill, identity-guarded: a None identity is
        unconfirmable (a possible pid reuse), so it is never signalled or polled at
        all — even a SIGTERM to a recycled pid kills an innocent process (the tmux
        adapter's straggler doctrine, mirrored). Same already-gone swallow as the
        root ladder; best-effort, never a teardown gate."""

        def _survivors() -> list[int]:
            return [
                pid
                for pid, identity in tree.items()
                if identity is not None and host.alive_and_ours(pid, identity)
            ]

        survivors = _survivors()
        if not survivors:
            return
        for pid in survivors:
            try:
                host.terminate(pid)
            except OSError:
                pass
        deadline = time.monotonic() + self.kill_wait_s
        while True:
            survivors = _survivors()
            if not survivors or time.monotonic() >= deadline:
                break
            time.sleep(REAP_POLL_S)
        for pid in survivors:
            try:
                host.force_kill(pid)
            except Exception:  # nosec B110 - already-gone races are fine
                pass

    def _atexit_sweep(self) -> None:
        for task_id in list(self._sessions):
            sess = self._sessions.pop(task_id, None)
            if sess is not None:
                self._teardown(sess)


class OpencodeDevAdapter(_DevSynthesisMixin, OpencodeHttpAdapter):
    """Dev/review adapter for the generic ``bmad-dev-auto`` skill over HTTP.

    That skill writes NO ``result.json`` — its outcome lives in the terminal
    spec it leaves on disk, which :class:`_DevSynthesisMixin` locates and
    synthesizes into the legacy result dict via :mod:`devcontract` (the same
    machinery as GenericDevAdapter, mixin-shared rather than duplicated).
    Selected by ``policy.dev.skill == "bmad-dev-auto"`` (see
    ``cli._make_adapters``).
    """

    def __init__(self, *args, paths: ProjectPaths, **kwargs):
        super().__init__(*args, **kwargs)
        self.paths = paths
        self._configure_dev_knobs()
        # task_id -> server process, kept past kill(): kill() pops the
        # _ServerSession registry, but _post_kill_reconcile still needs to
        # settle liveness after the teardown.
        self._server_procs: dict[str, subprocess.Popen] = {}

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        handle = super().start_session(spec)
        self._server_procs[spec.task_id] = self._sessions[spec.task_id].process
        return handle

    def _probe_alive(self, handle: SessionHandle) -> bool | None:
        proc = self._server_procs.get(handle.task_id)
        if proc is None:
            return False  # never spawned: nothing we own is alive
        # Never None: the live Popen handle pins the pid, so poll() is always
        # answerable — unlike a tmux transport there is no probe that can hang.
        return proc.poll() is None


def _sum_usage(messages: Any) -> TokenUsage:
    """Sum assistant-message token counts. Reasoning tokens are
    billed as output; OpenCode's cache read/write map onto the claude-style
    cache_read/cache_creation fields. Child-session (subagent) tokens are not
    visible here — the messages endpoint is scoped per session."""
    usage = TokenUsage()
    if not isinstance(messages, list):
        return usage
    for msg in messages:
        info = (msg or {}).get("info") or {}
        if info.get("role") != "assistant":
            continue
        tokens = info.get("tokens") or {}
        cache = tokens.get("cache") or {}
        usage.add(
            TokenUsage(
                input_tokens=int(tokens.get("input") or 0),
                output_tokens=int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0),
                cache_read_tokens=int(cache.get("read") or 0),
                cache_creation_tokens=int(cache.get("write") or 0),
            )
        )
    return usage
