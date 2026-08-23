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

``/event`` frame types the run-log renderer reads — pinned by a second live
probe of 1.18.2 (2026-07-25, 388 frames over 5 turns across two servers; the
full evidence table is posted on PR #279). Cross-checked against the server's
own OpenAPI at ``GET /doc``, whose ``Event`` union has 89 variants and is the
static contract — worth re-dumping on any version bump:

- ``message.updated`` — turn metadata only, no text. Carries ``info.id`` +
  ``info.role``; **re-emits out of order** (a stale re-emit for the user
  message lands mid-assistant-turn), so role must be keyed by message id, never
  tracked as "last role seen".
- ``message.part.updated`` — **complete-once, NOT cumulative.** A part fires
  exactly twice: once at creation with ``text: ""``, then once carrying the
  full final text. Held at 4 / 711 / 5689 chars (2 fires each, 1 non-empty).
  Rendering on this frame therefore yields one clean line per statement, with
  no de-duplication needed. **Tool execution also surfaces here** — and only
  here — as ``part.type == "tool"`` with ``part.tool`` and a ``state`` whose
  ``status`` steps pending → running → terminal (``state.input`` throughout,
  ``state.output`` on ``completed``, ``state.error`` on ``error``). Tool parts
  carry no ``part.text``, so the tool branch must sit ABOVE the empty-text
  bail-out in ``_inline_line`` to be reachable at all. ``ToolState`` is a
  4-variant union (pending / running / completed / error), so rendering on the
  terminal pair is exhaustively once-per-tool-call; ``error`` is included from
  the static contract (no tool failed during the live probe) so a failed call
  cannot vanish from the transcript.
- ``message.part.delta`` — the separate per-token stream. At 5689 chars its 50
  deltas concatenate **byte-exactly** to the single ``part.updated`` text, so
  it is pure redundancy: never rendered inline, and excluded from the SSE trace
  (``logs/`` is never trimmed by retention).
- ``command.executed`` — the **slash-command** surface, kept as that and nothing
  more. It does **not** fire for agent tool use: 0 frames across live ``bash`` /
  ``write`` / ``read`` / ``edit`` calls. Payload is ``name`` / ``arguments`` /
  ``messageID`` / ``sessionID`` (all required, so it needs no allowlist).
- ``file.edited`` — payload is exactly ``{"file": "/abs/path"}``, schema-pinned
  ``additionalProperties: false`` over that one key, so it carries **no
  ``sessionID``** and the ``properties.sessionID`` filter would drop it before
  either sink. It is reachable only through the explicit ``_SESSIONLESS_TYPES``
  allowlist, sound here only because bmad-loop runs one server per session.
  ``file.watcher.updated`` is session-less too and deliberately NOT allowlisted
  (see that constant).
- ``permission.asked`` — the ask frame. There is **no ``permission.updated``** in
  1.18.2. Payload is a ``permission`` **string** plus ``patterns`` /
  ``metadata`` / ``always`` / ``tool`` / ``id`` / ``sessionID`` — not a nested
  object with ``type`` / ``pattern``.
- ``permission.replied`` — carries **``reply``**, not ``response`` (plus
  ``sessionID`` / ``requestID``).
- ``permission.v2.asked`` / ``permission.v2.replied`` exist in the union and are
  **deliberately not consumed**: neither was seen live, and the v2 ask is a
  different payload (``action`` / ``resources`` / ``save`` / ``metadata`` /
  ``source``), so a branch for it would be written from the schema alone —
  exactly the guess that made the three corrections above necessary. The v2
  reply is shape-identical to the v1 one, but consuming it without the ask would
  log a decision with no request. Add both together once a live sighting pins
  what ``action`` / ``resources`` actually hold.
- There is **no ``tool.call`` / ``tool.response``** on this surface —
  confirmed both statically in the 89-variant union and live.

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
from .base import CodingCLIAdapter, SessionHandle, SessionResult, SessionSpec
from .env_fault import EnvFaultMixin
from .generic import (
    BUDGET_NUDGE_TEXT,
    HEARTBEAT_INTERVAL_S,
    NUDGE_TEXT,
    STALL_NUDGE_TEXT,
    _DevSynthesisMixin,
    _ResultFileMixin,
)
from .profile import CLIProfile

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
# Structured SSE trace (``logs/<task_id>.sse.jsonl``). Tier-1 knob deliberately:
# a module default plus the ``sse_trace`` instance attribute, NOT a policy field
# in core.toml. It changes nothing about how a run behaves — it only decides
# whether a debugging artifact is written — so it belongs with the timing knobs
# an operator flips in a REPL or a subclass, not in the run contract every
# settings file has to carry. Off means the sink is never opened.
SSE_TRACE = True

# ``/event`` frame types that carry NO ``properties.sessionID`` but are still
# attributed to this session, exempting them from the sessionID filter in
# `_dispatch_sse`. Sound ONLY because bmad-loop spawns one `opencode serve` per
# session (see the transport notes in the module docstring): the server is
# single-tenant, so a server-global frame is unambiguously this session's. Any
# design that ever shares a server across sessions must empty this set.
#
# Deliberately an allowlist and not "allow every session-less frame": the live
# probe's session-less traffic was overwhelmingly noise that says nothing about
# the run — `plugin.added` alone was 90 of 388 frames, plus `server.heartbeat`,
# `catalog.updated`, `server.connected`, `reference.updated` and
# `integration.updated`. `file.watcher.updated` is excluded on purpose too: it
# is the editor-integration watcher firing for any change under the project
# (our own git operations, a build, an editor save), so it is not evidence the
# agent did anything, and for the changes the agent DID make it just
# double-reports what `file.edited` already carries.
_SESSIONLESS_TYPES = frozenset({"file.edited"})

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


def _sum_args(arguments: Any) -> str:
    """Compact one-line summary of an arbitrary event payload value (a tool
    part's ``state.input``, a ``permission.asked`` ``patterns``, a
    ``command.executed`` ``arguments``, a ``session.error`` ``error``) for the
    inline run log. Truncates long dicts/strings so a single line never explodes
    the tail view, and accepts any shape — these payloads are server-controlled
    and not all of them have a pinned schema."""
    if not arguments:
        return ""
    try:
        if isinstance(arguments, (dict, list)):
            text = json.dumps(arguments, ensure_ascii=False)
        else:
            text = str(arguments)
    except (TypeError, ValueError):
        text = str(arguments)
    if len(text) > 120:
        text = text[:119] + "\u2026"
    return text


# ANSI colors for inline-log marker lines, keyed by message role. The bmad-loop
# TUI renders <task>.log through a pyte terminal emulator (tui/data.py LogView),
# which dispatches SGR to per-Char styles and maps pyte named colors via
# _rich_color straight through to Rich — so every [bmad] marker shows colored in
# the LogView. A plain `cat`/editor shows the escape bytes; the structured trace
# stays uncoloured in <task>.sse.jsonl. Keys are opencode message.info.role
# values; an unseen role falls back to _ROLE_COLOR_DEFAULT so it still renders
# distinctly — add it to the map to pin its hue.
_ROLE_COLORS = {
    "user": "\x1b[33m",  # SGR 33 = yellow
    "assistant": "\x1b[36m",  # SGR 36 = cyan
}
_ROLE_COLOR_DEFAULT = "\x1b[35m"  # SGR 35 = magenta — unseen roles
_TOOL_COLOR = "\x1b[32m"  # SGR 32 = green — tool/cmd/file/permission (role-less)
_RESET = "\x1b[0m"

# Tool-part statuses that end a tool call. `ToolState` is a 4-variant union in
# the 1.18.2 OpenAPI (`pending`, `running`, `completed`, `error`), so these two
# are exhaustively the terminal ones — a tool call reaches exactly one of them,
# which is what makes "render on terminal" equal to "one line per tool". The
# live probe only ever saw pending → running → completed (nothing failed during
# it); `error` comes from the static contract, and including it is what keeps a
# FAILED tool call from vanishing from the transcript entirely.
_TOOL_TERMINAL_STATES = frozenset({"completed", "error"})


def _role_color(role: str) -> str:
    """ANSI color for a message role; unseen roles fall back to magenta."""
    return _ROLE_COLORS.get(role, _ROLE_COLOR_DEFAULT)


@dataclass
class _ServerSession:
    """Everything the adapter tracks for one live ``opencode serve``."""

    process: subprocess.Popen
    port: int
    base_url: str
    password: str
    log_fh: Any
    # The spawned server's own stdout/stderr sink (``<task_id>.server.out``),
    # kept separate from ``log_fh`` so the readable transcript stays clean. The
    # server's INFO/diagnostic lines land here; ``log_fh`` carries only the
    # curated ``[bmad]`` lines written from the SSE reader thread.
    server_fh: Any = None
    # Structured SSE-trace JSONL sink (``<task_id>.sse.jsonl``); None when the
    # ``sse_trace`` knob is off. Written from the SSE reader thread only.
    event_fh: Any = None
    # Monotonic line number stamped into the trace. Per SESSION, while the file
    # is per TASK and opened in append mode — so a retried task restarts the seq
    # at 1 partway down the file. Read a run of seq back to 1 as "new session",
    # not as corruption; the ``ts`` field orders records across the whole file.
    event_seq: int = 0
    # Role keyed by opencode message id (``msg_*``), refreshed from
    # ``message.updated.info.{id,role}``. The per-part / delta events carry a
    # ``messageID`` but no role of their own, and ``message.updated`` frames
    # arrive out of order (re-emits for an earlier message land mid-turn), so a
    # "last role seen" would mislabel the assistant's reply as ``user:``. Keying
    # by message id makes the lookup ordering-independent.
    msg_roles: dict = field(default_factory=dict)
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


class OpencodeHttpAdapter(_ResultFileMixin, EnvFaultMixin, CodingCLIAdapter):
    # Env-fault classification scans the SERVER's own stdout/stderr, not
    # <task_id>.log — that file is the curated `[bmad]` conversation transcript
    # written by the SSE reader, so it carries the model's own words. Two reasons
    # this must be .server.out: the provider's AI_APICallError logfmt lines only
    # ever land there, and the profile's patterns are anchored on the assumption
    # that a story quoting a provider error verbatim cannot reach the scanned
    # bytes. Point this at the transcript and both properties break at once.
    ENV_FAULT_LOG_SUFFIX = ".server.out"

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
        events_dir: Path | None = None,
    ):
        # `events_dir` is accepted and unused: this family observes over SSE and
        # fires no hooks, so it has no event channel to point at. It is part of
        # the run description `runsetup.make_adapters` hands every family (#494),
        # and refusing the kwarg here would make the bootstrap branch per family
        # on a value that costs nothing to carry.
        del events_dir
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
        self.sse_trace = SSE_TRACE  # False = never open the .sse.jsonl sink
        self.result_grace_s: float | None = None  # None = the mixin default
        self.tasks_dir = run_dir / "tasks"
        self.logs_dir = run_dir / LOGS_DIR
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, _ServerSession] = {}
        self._usage: dict[str, TokenUsage] = {}
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
        event_fh = None
        server_fh = None
        # The remaining sinks open under their own guard: each `open()` can fail
        # on its own (ENOSPC, EMFILE, a permission race) with the earlier ones
        # already open, and that happens ABOVE the retry loop's try/except — so
        # without this the handles opened so far never reach `_close_spawn_sinks`
        # and stay open for as long as the propagating traceback keeps this frame
        # alive. One sink had nothing to leak here; three do.
        try:
            # Structured SSE trace, off entirely when the knob is down. Append
            # like the other two sinks: the file is per TASK and every retry of
            # that task writes into it, while `event_seq` is per SESSION and
            # restarts at 1 for each one — so a seq dropping back to 1 mid-file
            # marks a new session, not a truncation. `ts` (epoch ms) is the
            # cross-session ordering key.
            if self.sse_trace:
                event_path = self.logs_dir / f"{spec.task_id}.sse.jsonl"
                event_fh = event_path.open("a", encoding="utf-8")  # one record per line
            # The server's own stdout/stderr (INFO/diagnostic lines) is kept in a
            # separate file so the readable transcript in <task_id>.log stays
            # clean. This is also where a failed spawn's diagnostics land — the
            # give-up error below names this path, not log_path.
            server_path = self.logs_dir / f"{spec.task_id}.server.out"
            server_fh = server_path.open("ab")  # append: retries share one server log
        except BaseException:
            self._close_spawn_sinks(log_fh, server_fh, event_fh)
            raise
        last_error = "server did not become healthy"
        try:
            for _ in range(SPAWN_ATTEMPTS):
                port = _free_port()
                process = subprocess.Popen(  # argv built from profile
                    self._serve_argv(resolved, port),
                    cwd=str(spec.cwd),
                    env=env,
                    stdout=server_fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
                sess = _ServerSession(
                    process=process,
                    port=port,
                    base_url=f"http://127.0.0.1:{port}",
                    password=password,
                    log_fh=log_fh,
                    server_fh=server_fh,
                    event_fh=event_fh,
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
            self._close_spawn_sinks(log_fh, server_fh, event_fh)
            raise
        self._close_spawn_sinks(log_fh, server_fh, event_fh)
        raise OpencodeServerError(
            f"could not start `{self.binary} serve` after {SPAWN_ATTEMPTS} attempts "
            f"({last_error}); server log: {server_path}"
        )

    @staticmethod
    def _close_spawn_sinks(log_fh: Any, server_fh: Any, event_fh: Any) -> None:
        """Close whichever of the three per-task sinks are open, on the spawn
        failure paths. Any of them can be None: `event_fh` when the sse_trace
        knob is off, and either of the last two when the failure WAS the open
        that would have created it.

        Every close is guarded, matching `_teardown`. Both callers are already
        reporting a failure — one is mid-`raise`, the other is about to raise
        `OpencodeServerError` — so an OSError from a flush-on-close would
        replace the failure actually worth reporting, and would strand the sinks
        after it in this loop."""
        for fh in (log_fh, server_fh, event_fh):
            if fh is None:
                continue
            try:
                fh.close()
            except OSError:
                pass

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
        # Same hazard, same reason, for the file the #194 tail scan reads (mirrors
        # GenericAdapter.start_session, which unlinks its pane tee here). This one
        # bites hardest on the path the classifier exists to serve: an env fault
        # PAUSEs the run, the operator re-arms and resumes, and the next session
        # reusing this task_id would scan the PREVIOUS cycle's provider error and
        # pause again — however healthy the new session's own log. A pause loop
        # that survives every re-arm, off one stale line.
        #
        # Unlinked HERE and not in _spawn_server, which deliberately opens the file
        # "ab" so a spawn retry (free-port collision) keeps its predecessor's
        # diagnostics inside the SAME session.
        self._env_fault_log_path(spec.task_id).unlink(missing_ok=True)

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
        # Session-scoped frames must name this session (child/subagent sessions
        # share the stream). A short allowlist of SESSION-LESS types passes
        # anyway: some frames worth logging carry no sessionID at all, so there
        # is no id to match and this filter would drop them before either sink.
        # See `_SESSIONLESS_TYPES` for which ones, why it is an allowlist, and
        # what deliberately stays out.
        #
        # Both sinks sit below this gate, so an allowlisted frame is traced as
        # well as rendered. That is intended: the trace's contract is one record
        # per frame the adapter acted on, and it is the file you read to explain
        # a line in the transcript — a rendered line with no matching record
        # would make the two sinks disagree. It costs nothing here because the
        # allowlist admits one low-rate type, not the noisy session-less bulk.
        if props.get("sessionID") != sess.session_id and not (
            # `in` on a frozenset raises TypeError for an unhashable value, and
            # `type` is server-controlled, so narrow before the membership test
            # (the isinstance check below this filter is deliberately kept there
            # — moving it up would change the activity counter above).
            isinstance(etype, str)
            and etype in _SESSIONLESS_TYPES
        ):
            return
        if not isinstance(etype, str):
            # Every branch below compares `type` against a string literal, so a
            # frame carrying a non-string (or absent) type can match none of
            # them. Dropping it here narrows the value for the typed helpers and
            # keeps a malformed frame out of the trace — it is not a frame the
            # adapter acted on. Liveness/activity above already counted it.
            return
        # Before the control queue: emit a structured trace record and render
        # one human-readable line into the run log. Both helpers no-op on
        # unknown types, so unhandled frames leave both sinks byte-identical to
        # today, and idle/error still queue below — control flow is unchanged.
        #
        # Guarded as a block because these two sinks sit UPSTREAM of the
        # idle/error queue puts on a frame-shaped payload we do not control:
        # both helpers walk nested `.get` chains (props["part"], ["info"],
        # ["permission"]), so a server that ships a string where a dict is
        # expected raises AttributeError here. Unguarded that unwinds all the
        # way to the reader's connection-level catch in `_sse_loop`, which tears
        # the stream down, reconnects, and drops whatever the server had already
        # buffered — a logging bug silently degrading completion signaling. Same
        # never-die doctrine as the reader itself: logging is advisory, the
        # queue puts below are not. `_emit_event` runs first (it is the simpler
        # of the two) so a render bug still leaves the offending frame recorded
        # in the trace that would be used to diagnose it.
        try:
            self._emit_event(sess, etype, props)
            self._render_inline(sess, etype, props)
        except Exception:  # nosec B110 - logging must never disturb the queue puts below
            pass
        if etype == "session.idle":
            sess.events.put("idle")
        elif etype == "session.error":
            sess.events.put("error")

    def _render_inline(self, sess: _ServerSession, etype: str, props: dict) -> None:
        """Append one human-readable progress line to the run log for the event
        types worth watching live. No-ops on anything else, so an unhandled
        frame writes nothing.

        ``log_fh`` is the readable transcript sink (``<task_id>.log``): it
        carries ONLY these curated ``[bmad]`` lines. The server's own INFO /
        diagnostic stdout is kept apart in ``<task_id>.server.out`` (see
        ``server_fh``) so the transcript stays a clean, line-wrapped
        conversation rather than a jumble of server logging. Only this SSE
        reader thread writes ``log_fh``, and each line is a single ``write()``
        plus ``flush()`` — so it always lands as a whole line at EOF.

        Every event-type string below, and the payload shape each branch reads,
        is pinned in the module docstring's ``/event`` section against a live
        1.18.2 probe — read that before adding or changing a branch here. That
        is deliberately the only copy of those facts: three of this renderer's
        original branches named frames the server does not send, and a second
        copy is a second thing to get wrong on the next version bump.

        ``message.updated`` carries only turn metadata (notably ``info.role``
        keyed by ``info.id``) and renders no inline line of its own — it just
        records the role under the message id so the following
        ``message.part.*`` lines resolve the right speaker. Keying by message
        id (not "last role seen") is required because ``message.updated``
        frames arrive out of order: a re-emit for the user message lands mid-
        assistant-turn and would otherwise flip the prefix to ``user:`` on the
        assistant's own reply text. That also avoids a flood of role-only
        announce lines like ``[bmad] assistant: assistant``."""
        if etype == "message.updated":
            info = props.get("info") or {}
            mid = info.get("id")
            role = info.get("role")
            if mid and role:
                sess.msg_roles[mid] = role
            return
        role = "assistant"
        if etype == "message.part.updated":
            mid = (props.get("part") or {}).get("messageID")
            if mid and mid in sess.msg_roles:
                role = sess.msg_roles[mid]
        line = self._inline_line(etype, props, role)
        if line is None:
            return
        if sess.log_fh is None:  # bare-sess unit tests / disabled inline log
            return
        try:
            # CRLF line endings: the bmad-loop TUI renders this log through a
            # pyte terminal emulator, where a bare LF moves the cursor down
            # without returning to column 0 (a real PTY's ONLCR does that, but
            # pyte is a pure VT100 emulator). LF-only lines staircase right,
            # so emit CRLF like real terminal output (the claude/tmux captures
            # carry it via cursor-addressing; our plain text must carry it raw).
            sess.log_fh.write(line.replace("\n", "\r\n").encode("utf-8"))
            sess.log_fh.flush()
        except OSError:
            pass

    def _inline_line(self, etype: str, props: dict, role: str) -> str | None:
        # ``message.updated`` is consumed in ``_render_inline`` (role refresh
        # only); it renders no line here.
        # assistant / user assembled text. Per-token ``message.part.delta``
        # frames are NOT rendered inline: they concatenate to exactly the text
        # ``message.part.updated`` already carries complete, so emitting both
        # duplicates every statement as a one-word-per-line flood. For the same
        # reason they are excluded from the SSE trace too (see ``_emit_event``);
        # nothing consumes deltas anywhere in this adapter.
        if etype == "message.part.updated":
            part = props.get("part") or {}
            # Tool execution rides this same frame as a `tool`-typed part —
            # there is no tool.call/tool.response event on this surface, and
            # `command.executed` never fires for it (both pinned live). Tool
            # parts carry no `text`, so this branch MUST sit above the
            # empty-text `return None` below or it can never be reached: that
            # exact ordering is the difference between rendering the agent's
            # actions and rendering nothing but its prose.
            if part.get("type") == "tool":
                return self._tool_line(part)
            text = part.get("text") or ""
            body = text.strip()  # drop surrounding blanks; keep internal structure
            if not body:  # empty/non-text part — no value live
                return None
            role = role or "assistant"
            # A blank line separates each turn from the previous one (the
            # separator sits ABOVE the header, body follows directly beneath),
            # and the role-colored marker line anchors the turn boundary.
            # Internal newlines in the body are preserved so multi-paragraph
            # reasoning reads as prose under the one header.
            return f"\n{_role_color(role)}[bmad] {role}:{_RESET}\n{body}\n"
        # The SLASH-COMMAND surface — NOT the tool surface. Agent tool use never
        # reaches here (0 `command.executed` frames across live bash/write/read/
        # edit calls); it renders from the `tool`-typed part above. Kept because
        # a slash command invoked through this server is still worth a line and
        # its payload is pinned (`name`/`arguments`/`messageID`, all required),
        # but do not "fix" tool rendering back into this branch.
        if etype == "command.executed":
            args = _sum_args(props.get("arguments"))
            return f"{_TOOL_COLOR}[bmad] cmd: {props.get('name') or '?'}{(' ' + args) if args else ''}{_RESET}\n"
        if etype == "file.edited":
            # Session-less: reachable only via `_SESSIONLESS_TYPES`.
            return f"{_TOOL_COLOR}[bmad] file: {props.get('file') or '?'}{_RESET}\n"
        # Permission surface. Field names are the live 1.18.2 ones: the ask frame
        # is `permission.asked` (there is no `permission.updated`) carrying a
        # `permission` STRING plus a `patterns` list — not a nested object with
        # `type`/`pattern` — and the reply carries `reply`, not `response`.
        # `metadata` (the concrete command) and `always` are left to the trace;
        # `patterns` is what the permission actually matched on.
        if etype == "permission.asked":
            pats = _sum_args(props.get("patterns"))
            return (
                f"{_TOOL_COLOR}[bmad] perm ask: {props.get('permission') or '?'}"
                f"{(' ' + pats) if pats else ''}{_RESET}\n"
            )
        if etype == "permission.replied":
            return f"{_TOOL_COLOR}[bmad] perm reply: {props.get('reply') or '?'}{_RESET}\n"
        # A failing turn is the one thing a reader most wants in the transcript:
        # without this the log just stops mid-turn while the queue put below
        # ends the session. Same green/tool hue as the other role-less markers —
        # the color family is "not a speaker", not "severity". `error` has no
        # pinned shape on this surface, so summarize whatever it carries rather
        # than reaching into fields that may not exist.
        if etype == "session.error":
            detail = _sum_args(props.get("error"))
            return f"{_TOOL_COLOR}[bmad] error:{(' ' + detail) if detail else ''}{_RESET}\n"
        return None

    @staticmethod
    def _tool_line(part: dict) -> str | None:
        """One line for a finished tool call, or None while it is still running.

        A `tool`-typed ``message.part.updated`` part fires once per state
        transition — ``pending`` → ``running`` → terminal — carrying the SAME
        part id each time. Rendering every fire would print each tool call three
        times, so the line is emitted only on a terminal status
        (``_TOOL_TERMINAL_STATES``): a tool call reaches exactly one terminal
        state, which makes that gate exactly "once per tool call". The gate is
        the whole correctness argument here — a test that only asserts a tool
        renders at all passes with it removed.

        Same green/tool hue as the other role-less markers: a tool call is an
        action, not a speaker. ``state.input`` is summarized inline (that is the
        "what did the agent just do" the reader wants); ``state.output`` is
        deliberately NOT — it is a full command's stdout or a whole file's text
        on this surface, and the complete payload is in the SSE trace for anyone
        who needs it. A failure appends the error, because a tool that failed
        silently is exactly what a reader would go looking for."""
        state = part.get("state") or {}
        status = state.get("status")
        if status not in _TOOL_TERMINAL_STATES:
            return None
        args = _sum_args(state.get("input"))
        suffix = ""
        if status == "error":
            detail = _sum_args(state.get("error"))
            suffix = f" -> error{(': ' + detail) if detail else ''}"
        return (
            f"{_TOOL_COLOR}[bmad] tool: {part.get('tool') or '?'}"
            f"{(' ' + args) if args else ''}{suffix}{_RESET}\n"
        )

    def _emit_event(self, sess: _ServerSession, etype: str, props: dict) -> None:
        """Append one structured JSON object to the session's
        ``<task_id>.sse.jsonl`` sink — a record for every acted-on frame except
        the per-token deltas, for post-hoc replay/debugging. No-ops when the
        sink is off (``event_fh is None``, i.e. the ``sse_trace`` knob is down).

        ``message.part.delta`` is the one exclusion. Its text concatenates
        byte-exactly to the ``message.part.updated`` record already in the file
        (pinned live — see the module docstring), so every delta is bytes spent
        re-storing text the trace already holds, and they dominate the frame
        count on any real turn. ``logs/`` is never trimmed by retention, so that
        cost is permanent. Drop the branch to get them back."""
        if sess.event_fh is None:
            return
        if etype == "message.part.delta":
            return
        sess.event_seq += 1
        record = {
            "seq": sess.event_seq,
            "type": etype,
            "properties": props,
            "ts": _now_ms(),  # epoch ms — the unit every opencode time.* uses
        }
        try:
            sess.event_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            sess.event_fh.flush()
        except (OSError, TypeError, ValueError):
            pass

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
        # Mirrors generic.wait_for_completion: stall grace starts at launch,
        # re-arms on activity or a result-less Stop (idle), and is spent via
        # wake-nudges bounded by the monotonic spec.stall_nudges_cap (#149).
        stall_deadline = time.monotonic() + self._stall_grace_s if self._stall_grace_s > 0 else None
        last_activity = sess.activity
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
            # Hard-stop poll (#319), per-iteration and deliberately NOT inside the
            # heartbeat throttle below: the loop blocks up to `POLL_TICK_S` (5s) per
            # tick, so *detection* normally lands well inside `stop_run`'s 10s
            # grace window — the common case, not a bound: the dispatch legs below
            # the wait are bounded only by the client's own timeouts, and the
            # generic adapter is no better off (its `_await_result` waits
            # RESULT_GRACE_S on a healthy box). Beyond detection, this arm then
            # makes two
            # HTTP round-trips against a server that may itself be wedged, and the
            # client's 10s per-phase timeout applies to each. So the arm is NOT
            # bounded by the grace window, by design: it gives the engine its best
            # chance to tear itself down cleanly, and when the server will not answer
            # it degrades to `stop_run`'s force-kill backstop — the same outcome
            # every native-Windows stop had before #319, never a worse one. Don't
            # "fix" this by trimming the timeouts: the same two calls serve the
            # timeout arm, where the transcript is the whole diagnostic payload.
            #
            # Mirror the timeout arm exactly — without `_abort` the in-flight HTTP
            # turn keeps running until teardown. Return the verdict; never raise
            # `RunStopped` here, and never unlink the request file: the engine
            # consumes it and attributes the stop.
            if self._hard_stop_requested():
                self._note_lifecycle(handle.task_id, "stop-abort-fired")
                self._abort(sess)
                transcript = self._capture_usage(handle, sess)
                return SessionResult(
                    status="aborted",
                    session_id=session_id,
                    transcript_path=transcript,
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

            # Second poll (#319) — see the arm at the top of the loop. What follows
            # here is the dispatch: `_probe_completion`'s two GETs, which are NOT
            # throttled (once a turn goes quiet past SILENCE_THRESHOLD_S they run on
            # every tick), a `_session_status` GET, or `_result_json(wait=True)`'s
            # RESULT_GRACE_S wait. Each is bounded only by the client's own timeouts,
            # so a single iteration can outlast `stop_run`'s 10s grace. Polling here
            # keeps at most one leg between two checks. It cannot bound an in-flight
            # socket read, so when one does outlast the window the stop degrades to
            # the force-kill backstop exactly as it did before #319.
            if self._hard_stop_requested():
                self._note_lifecycle(handle.task_id, "stop-abort-fired")
                self._abort(sess)
                transcript = self._capture_usage(handle, sess)
                return SessionResult(
                    status="aborted",
                    session_id=session_id,
                    transcript_path=transcript,
                    budget_weighted=budget_weighted,
                )
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
                            if self._session_status(sess):
                                # Provably mid-turn (a busy child/parent the
                                # SSE missed): re-arm rather than injecting a
                                # prompt into a working session or declaring
                                # it stalled after the nudge budget is spent.
                                stall_deadline = time.monotonic() + self._stall_grace_s
                                continue
                            if stall_nudges_left > 0 and (
                                spec.stall_nudges_cap is None
                                or stall_nudges_sent < spec.stall_nudges_cap
                            ):
                                # Unknown status (None) proceeds to the nudge —
                                # a transport too broken to answer the probe
                                # would fail the nudge too, and burning the
                                # bounded budget converges to an honest stall.
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
        sess = self._sessions.pop(handle.task_id, None)
        if sess is None:
            return
        self._abort(sess)
        self._teardown(sess)

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
        if sess.server_fh is not None:
            try:
                sess.server_fh.close()
            except OSError:
                pass
        if sess.event_fh is not None:
            try:
                sess.event_fh.close()
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
    """Dev/review adapter for the generic ``bmad-build-auto`` skill over HTTP.

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
