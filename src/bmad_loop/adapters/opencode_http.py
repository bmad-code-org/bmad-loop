"""OpenCode driver: sessions over the local HTTP server, no tmux, no hooks.

OpenCode (opencode.ai) is client/server: its TUI is just a client of a local
HTTP server (``opencode serve``) exposing an OpenAPI 3.1 API. This adapter
drives sessions entirely over that API (``injection="http"``,
``observation="sse"``).

The wire contract — what each OpenCode server API answers, pinned live per
major — lives in :mod:`.opencode_protocol`'s module docstring, beside the
codecs that implement it. The major is detected from ``opencode --version``
before the server is spawned (:meth:`OpencodeHttpAdapter._protocol_for`):
1.x (``opencode-ai``, verified 1.18.2–1.19.x) and 2.x (``@opencode/cli``,
verified 2.0.21–2.0.27) are driven; any other major is refused. Everything
below is the transport every major shares.

Transport shape (the settled design drivers):

- **One ``opencode serve`` per session.** The API has no per-session env, and
  the ``BMAD_LOOP_*`` contract must reach tool subprocesses via the server
  process env — so each session gets its own server spawned with
  ``cwd=spec.cwd`` and the session env. Ports are OS-assigned free ports,
  re-picked on a bind race.
- **Config injected via ``OPENCODE_CONFIG_CONTENT``** (outranks the project's
  ``opencode.json``): a blanket permission allow (the bypass-flags
  analogue), the hermetic-skills recipe (see the protocol module; project
  ``.claude/skills`` only — without it every session sees the operator's
  personal skills), and
  the policy model when set. Reasoning effort (``SessionSpec.effort``, #643)
  deliberately does NOT ride the config: the config schema has no top-level
  ``variant``, and its only effort key (``agent.<name>.variant``) applies solely
  when that agent table also pins its own ``model`` — inert otherwise, which is
  the measured negative result in #643. On V1 it is sent instead as the
  per-call ``variant`` in every ``prompt_async`` body (``_prompt``), initial
  prompt and nudges alike, omitted entirely when empty, and passed through
  unvalidated. V2's prompt has no variant: the effort rides the session's
  model ref at create, and an effort that is not one of that model's variants
  fails the session start (see the protocol module). A per-session
  ``OPENCODE_SERVER_PASSWORD`` makes the health poll self-discriminating against
  a foreign server on a reused port and keeps other local processes from
  driving an allow-all server.
- **The SSE turn end ≙ the Stop hook** (V1 ``session.idle``, V2
  ``session.execution.*`` — the protocol's ``idle_types``), filtered to this
  session's id — child/subagent sessions share the stream and emit their own
  turn ends. SSE is lossy upstream, so a silent or reconnecting stream
  degrades to an HTTP poll (the protocol's busy probe — V1
  ``GET /session/status`` — + message-level proof-of-work): an idle event alone
  is NOT proof a turn ran (abort emits one on an idle session),
  so the fallback demands an assistant message completed *after* the last
  prompt this adapter sent. OpenCode timestamps are epoch **milliseconds**.
- **Server death ≙ window death** (``crashed``, landed artifact honored);
  stall verdicts under a live server pin ``accept_result=False`` — the
  #48/#53 artifact-distrust invariant, unchanged.
- **Usage is read over HTTP before teardown** (server state is sqlite, not a
  readable file tree): V1 sums assistant-message tokens, V2 reads the
  session's own token aggregate; stashed by session id, the raw messages
  (V2: every page, oldest first) dumped to ``tasks/<task_id>/messages.json``
  as the transcript. Child-session (subagent) tokens are not counted — the
  API scopes messages and usage per session.
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
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import childrun, gates
from ..bmadconfig import ProjectPaths
from ..journal import LOGS_DIR, TASK_CYCLE_ARTIFACTS
from ..model import TokenUsage
from ..policy import Policy
from ..process_host import ProcessHostError, get_process_host
from .base import (
    CodingCLIAdapter,
    SessionHandle,
    SessionResult,
    SessionSpec,
    reset_task_prompt,
    validate_adapter_artifact_paths,
    validated_task_directory,
)
from .env_fault import EnvFaultMixin
from .generic import (
    BUDGET_NUDGE_TEXT,
    HEARTBEAT_INTERVAL_S,
    NUDGE_TEXT,
    RESULT_FILE_ARTIFACTS,
    STALL_NUDGE_TEXT,
    _DevSynthesisMixin,
    _ResultFileMixin,
)
from .multiplexer import MultiplexerError

# The `name as name` imports are re-exports: the V1 codec helpers moved to
# `opencode_protocol` keep their historical import path for callers and tests.
from .opencode_protocol import _RESET as _RESET
from .opencode_protocol import _TOOL_COLOR as _TOOL_COLOR
from .opencode_protocol import (
    OpencodeProtocol,
    OpencodeServerError,
    V1Protocol,
    V2Protocol,
)
from .opencode_protocol import _role_color as _role_color
from .opencode_protocol import _sum_args as _sum_args
from .opencode_protocol import _sum_usage as _sum_usage
from .opencode_protocol import _tally_messages as _tally_messages
from .opencode_protocol import (
    format_version,
    parse_opencode_version,
    support_verdict,
    supported_ranges,
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
# The adapter-authored provider-error lines (`_note_provider_error`), scanned by
# the env-fault classifier beside `.server.out`. Created at spawn on every
# protocol, so a session that saw no provider error leaves an empty file — a
# clean answer — rather than a missing one, which the scan reads as a fault.
PROVIDER_ERRORS_SUFFIX = ".provider-errors.log"

# Capacity bound for the `_usage` stash (DW-117). `_usage` is written once per
# session in `_capture_usage` and never popped, so without a ceiling it grows
# O(sessions) for the adapter's lifetime. It cannot ride the `_evict_task_state`
# seam that bounds the per-task stores: it is keyed by `session_id`, not
# `task_id`, and the engine calls `read_usage(result)` AFTER `run()` returns, so
# evicting in `run()`'s `finally` would zero token accounting. Draining the entry
# in `read_usage` itself would be a tighter bound but is ruled out too: DW-117
# requires `read_usage` stay idempotent. So the bound is enforced at the write
# site instead, oldest-first.
#
# What keeps a pending read safe is the SIZING MARGIN, not the eviction order: at
# most a handful of sessions can stash between one session's `_capture_usage` and
# the engine's `read_usage(result)` — bmad-loop drives sessions essentially
# serially per run — so the pending entry is hundreds of writes away from being
# the oldest. Pick a cap far above that gap, and the store is fixed-size without
# a live read ever losing its entry.
USAGE_STASH_CAP = 256

# Fixed basic-auth username in OPENCODE_SERVER_PASSWORD mode (Bearer is rejected).
AUTH_USER = "opencode"

# The codec a caller with no session in hand reads (the name-stable delegators
# below); stateless, so one instance serves every such call.
_V1 = V1Protocol()

# The codec per supported major (`opencode_protocol.SUPPORTED`'s keys).
_PROTOCOLS: dict[int, type[OpencodeProtocol]] = {1: V1Protocol, 2: V2Protocol}

# Where every version refusal sends the operator.
_VERSION_REMEDY = "see docs/setup-guide.md#opencode-versions"

# `opencode --version` budget for the pre-spawn protocol detection — probe's
# `binary_runs` default, which bounds the same call in `bmad-loop validate`.
VERSION_TIMEOUT_S = 10.0

# The argv tokens `_serve_argv` owns: the subcommand and the three flags it
# appends. A `launch_args` token equal to one of these (or the `--flag=value`
# spelling of one) collides with what the adapter assigns — a second `serve`
# becomes a positional, a second `--port` fights the port the health poll dials,
# a `--print-logs=false` silences the server log the env-fault classifier reads.
# `extra_args` land AFTER the owned flags, so there only the flags collide (a
# later `--port` wins over the adapter's; a trailing `serve` is a positional to
# `serve`, not a second subcommand) — DW-483.
# Kept beside `_serve_argv` so the two cannot drift apart.
_SERVE_OWNED_FLAGS = frozenset({"--port", "--hostname", "--print-logs"})
SERVE_OWNED_TOKENS = _SERVE_OWNED_FLAGS | {"serve"}
_SERVE_OWNED_VALUED = ("--port=", "--hostname=", "--print-logs=")

# Binaries that need a PROGRAM before `serve`: interpreters and package runners,
# matched on the basename (split on `/` and `\`, lowercased, one of
# `.exe`/`.cmd`/`.bat`/`.com` stripped). Given no program in `launch_args`,
# `_serve_argv` hands them `serve` as the program — an interpreter reads it as a
# script path resolved against the workspace cwd (see `runsetup.config_digest`),
# a package runner fetches the package named `serve`. A closed name set, never a
# probe: `bmad-loop validate` must not execute a project-controlled binary.
_NEEDS_PROGRAM_BASENAME = re.compile(
    r"python(?:\d+(?:\.\d+)?)?|pythonw|py|pypy(?:\d+(?:\.\d+)?)?"
    r"|sh|bash|zsh|dash|node|nodejs|bun|deno|perl|ruby|env|npx|bunx|pnpx"
)
_WINDOWS_LAUNCHER_SUFFIXES = (".exe", ".cmd", ".bat", ".com")


def launch_args_unservable(binary: str, launch_args: tuple[str, ...] | list[str]) -> str | None:
    """Why ``[binary, *launch_args, "serve", …]`` cannot run an opencode server,
    or None when the shape is servable. Pure: `bmad-loop validate` reports the
    reason as ``adapter.launch-args-unservable`` (warning) for opencode-http
    profiles.

    Two shapes are unservable: a ``launch_args`` token repeating an adapter-owned
    one (:data:`SERVE_OWNED_TOKENS`, or a ``--flag=`` spelling), and an
    interpreter / package-runner ``binary`` with no program — ``launch_args``
    empty or options only (``python3 -u``, ``python3 -m``, ``node --inspect``
    still leave ``serve`` in the script/module slot). Wrappers that name a
    program (``npx -y opencode-ai``, ``node /opt/x.js``, ``python3 -m
    launcher``) are servable."""
    collision = _owned_collision("launch_args", launch_args, SERVE_OWNED_TOKENS)
    if collision is not None:
        return collision
    base = re.split(r"[\\/]", binary)[-1].lower()
    for suffix in _WINDOWS_LAUNCHER_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    no_program = all(token.startswith("-") for token in launch_args)
    if no_program and _NEEDS_PROGRAM_BASENAME.fullmatch(base):
        return (
            f"binary {binary!r} needs a program in launch_args — 'serve' would run "
            "as the program (a script from the workspace, or a package named serve)"
        )
    return None


def extra_args_unservable(extra_args: tuple[str, ...] | list[str]) -> str | None:
    """Why a role's resolved ``extra_args`` collide with the serve argv, or None.
    Pure: `bmad-loop validate` reports the reason as
    ``policy.extra-args-unservable`` (warning), per role, for opencode-http
    profiles (DW-483).

    ``_serve_argv`` appends ``extra_args`` after the owned flags, so a repeated
    ``--port`` / ``--hostname`` / ``--print-logs`` (or a ``--flag=`` spelling)
    overrides what the adapter assigns — the health poll then dials a port the
    server is not on. ``serve`` is not owned here: after the subcommand it is a
    positional, not a second subcommand."""
    return _owned_collision("extra_args", extra_args, _SERVE_OWNED_FLAGS)


def _owned_collision(
    field: str, tokens: tuple[str, ...] | list[str], owned: frozenset[str]
) -> str | None:
    """The first token of ``tokens`` repeating one of ``owned`` (or a ``--flag=``
    spelling of an owned flag), worded for ``field``; None when there is none."""
    for token in tokens:
        if token in owned or token.startswith(_SERVE_OWNED_VALUED):
            return (
                f"{field} token {token!r} collides with the `serve --port … "
                "--hostname 127.0.0.1 --print-logs` argv the adapter assigns"
            )
    return None


class OpencodeNudgeSendError(MultiplexerError):
    """A nudge :meth:`OpencodeHttpAdapter.send_text` could not deliver (DW-503).

    A :class:`MultiplexerError` so the shared nudge call sites (the
    ``_DevSynthesisMixin`` contract nudge) catch it at the seam-level type and
    crumb ``nudge-send-failed`` instead of reporting the nudge as sent."""


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


def _parse_sse_lines(
    lines,
    on_drop: Callable[[str], None] | None = None,
    on_comment: Callable[[], None] | None = None,
) -> Any:
    """Minimal SSE frame parser: accumulate ``data:`` lines until a blank line,
    then yield the JSON-decoded payload. Tolerates comments, unknown fields and
    undecodable payloads (skipped) — the stream is advisory, never trusted.
    A skipped payload is reported to ``on_drop`` as ``"<ExcType>: <message>"``
    (DW-462), so the reader can count what the stream lost. A comment line
    (``:``-prefixed — V2's ``: heartbeat``) yields nothing but is reported to
    ``on_comment``: it proves the stream alive."""
    data: list[str] = []
    for line in lines:
        if line.startswith(":"):
            if on_comment is not None:
                on_comment()
            continue
        if line == "":
            if data:
                try:
                    yield json.loads("\n".join(data))
                except (json.JSONDecodeError, ValueError) as e:
                    if on_drop is not None:
                        on_drop(f"{type(e).__name__}: {e}")
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
    # `SessionSpec.effort`, stashed once at session construction and sent as the
    # per-call `variant` on EVERY prompt_async body this session issues (initial
    # prompt and nudges — a nudge dropping back to the provider default mid-session
    # would be silent drift). "" = omit the key, so the body is byte-identical to
    # an effort-less session's. V2 ignores it: the variant rides the session's
    # model ref at create, which every later prompt inherits.
    variant: str = ""
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
    # SSE frames whose payload would not decode, running count over the session
    # (DW-462): the parser skips them, so without this a server shipping garbage
    # reads as a quiet stream. Written by the reader thread, read by the wait
    # loop's heartbeat (a plain int bump, like `activity`).
    sse_frames_dropped: int = 0
    # The reader's running streak of stream attempts that raised; the streak's
    # transitions are crumbed (`sse-stream-failed` / `sse-stream-recovered`),
    # never each failed reconnect (DW-462). Reader thread only.
    sse_failures: int = 0
    # Monotonic completion floor in epoch ms: the poll fallback only
    # synthesizes an idle for an assistant message completed strictly after
    # this. Starts at prompt-send, advances on every prompt this adapter sends
    # and on every completion it consumes — otherwise one stale completed
    # message re-synthesizes idle on every probe (each fake "Stop" refills the
    # stall budget) and the session livelocks or burns its nudges.
    floor_ms: int = 0
    # The server API codec, chosen by the major `--version` reported before the
    # spawn (`_protocol_for`). Defaults to V1 so a bare session built without a
    # spawn (unit tests) reads the V1 wire.
    protocol: OpencodeProtocol = field(default_factory=V1Protocol)
    # The `X.Y.Z` `--version` reported for this server; "" for a bare session.
    server_version: str = ""
    # Why the last health poll failed, when it is more specific than "not
    # healthy in time" (an HTML answer: the wrong server API) — read by
    # `_spawn_server` for its give-up error.
    health_fault: str = ""
    # The task this server runs (``spec.task_id``), naming the lifecycle file
    # the reader thread's crumbs land in; empty for a bare session.
    lifecycle_task: str = ""
    # `logs/<task_id>.provider-errors.log`, created at spawn and appended by the
    # reader (`_note_provider_error`); None for a bare session.
    provider_errors_path: Path | None = None
    # Whether a provider-errors append has failed this session: the first
    # failure is crumbed (`provider-errors-write-failed`), the rest only lost.
    provider_errors_write_failed: bool = False


class OpencodeHttpAdapter(_ResultFileMixin, EnvFaultMixin, CodingCLIAdapter):
    # Env-fault classification scans the SERVER's own stdout/stderr, not
    # <task_id>.log — that file is the curated `[bmad]` conversation transcript
    # written by the SSE reader, so it carries the model's own words. Two reasons
    # this must be .server.out: the provider's AI_APICallError logfmt lines only
    # ever land there, and the profile's patterns are anchored on the assumption
    # that a story quoting a provider error verbatim cannot reach the scanned
    # bytes. Point this at the transcript and both properties break at once.
    ENV_FAULT_LOG_SUFFIX = ".server.out"
    # ...plus the provider-errors log: OpenCode 2.x logs no provider retry to
    # stdout (one line only once it gives up, ~90 s in — after most session
    # clocks have run out), so the adapter writes one line per provider-error SSE frame
    # itself, from structured fields only (`format_provider_error`) — model-free
    # by construction, which is what lets the profile anchor on its prefix.
    ENV_FAULT_LOG_SUFFIXES = (".server.out", PROVIDER_ERRORS_SUFFIX)

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
        # `_protocol_for`'s cache: binary identity -> the version it reported.
        self._version_cache: dict[tuple[str, int, int], tuple[int, int, int]] = {}
        # Bounded at its write site by `_stash_usage`; see USAGE_STASH_CAP.
        self._usage: dict[str, TokenUsage] = {}
        # opencode serve survives parent death: sweep whatever is
        # still registered when the interpreter exits cooperatively. A hard
        # SIGKILL of the engine still leaks — documented residual risk.
        atexit.register(self._atexit_sweep)

    # ------------------------------------------------------------- spawning

    def _serve_argv(self, resolved_binary: str, port: int) -> list[str]:
        """argv for one server: ``[binary, *launch_args, "serve", <owned flags>,
        *extra_args]``. ``launch_args`` sits between the binary and ``serve`` so a
        wrapper profile (``binary = "npx"``, ``launch_args = ["-y",
        "opencode-ai"]``) launches ``npx -y opencode-ai serve …``. The adapter owns
        ``serve`` and the three flags (:data:`SERVE_OWNED_TOKENS`); validate warns
        on launch_args shapes that collide with them (:func:`launch_args_unservable`)
        and on a role's extra_args repeating an owned flag
        (:func:`extra_args_unservable`).
        A seam: tests monkeypatch it to launch the FakeOpencode sidecar
        wrapper-free."""
        extra = self.extra_args or ()
        return [
            resolved_binary,
            *self.profile.launch_args,
            "serve",
            "--port",
            str(port),
            "--hostname",
            "127.0.0.1",
            "--print-logs",
            *extra,
        ]

    def _config_content(self, spec: SessionSpec, protocol: OpencodeProtocol | None = None) -> str:
        """The OPENCODE_CONFIG_CONTENT JSON for this session, in ``protocol``'s
        dialect (V1 when omitted): see :meth:`V1Protocol.config_content`."""
        return (protocol or _V1).config_content(spec, Path(spec.cwd) / self.profile.skill_tree)

    def _session_env(
        self, spec: SessionSpec, password: str, protocol: OpencodeProtocol | None = None
    ) -> dict[str, str]:
        protocol = protocol or _V1
        return {
            **os.environ,
            **self.profile.env,
            **spec.env,
            **protocol.extra_env(),
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_CONFIG_CONTENT": self._config_content(spec, protocol),
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
        protocol, version = self._protocol_for(resolved)
        self._note_lifecycle(
            spec.task_id,
            "opencode-server-version",
            version=format_version(version),
            major=protocol.major,
            verdict=support_verdict(version)[0],
        )
        refused = protocol.refused_skill_ids(Path(spec.cwd) / self.profile.skill_tree)
        if refused:
            self._note_lifecycle(spec.task_id, "skill-ids-refused", ids=refused)
        password = secrets.token_urlsafe(16)
        env = self._session_env(spec, password, protocol)
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
            provider_errors_path = self.logs_dir / f"{spec.task_id}{PROVIDER_ERRORS_SUFFIX}"
            provider_errors_path.touch()  # kept, like the others, across spawn retries
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
                    protocol=protocol,
                    server_version=format_version(version),
                    lifecycle_task=spec.task_id,
                    provider_errors_path=provider_errors_path,
                )
                if self._await_healthy(sess):
                    sess.client = self._make_client(sess)
                    return sess
                # Died or never readied: reap and try a fresh port. A live-but-
                # unhealthy server is killed too — never leak what we spawned.
                if process.poll() is None:
                    last_error = sess.health_fault or "server did not become healthy in time"
                    self._kill_process(sess)
                else:
                    last_error = f"server exited rc={process.returncode} during startup"
        except BaseException:
            self._close_spawn_sinks(log_fh, server_fh, event_fh)
            raise
        self._close_spawn_sinks(log_fh, server_fh, event_fh)
        launched = " ".join((self.binary, *self.profile.launch_args))
        raise OpencodeServerError(
            f"could not start `{launched} serve` after {SPAWN_ATTEMPTS} attempts "
            f"({last_error}); server log: {server_path}"
        )

    def _protocol_for(self, resolved: str) -> tuple[OpencodeProtocol, tuple[int, int, int]]:
        """``(protocol, version)`` for the server ``resolved`` would start, read
        from ``<resolved> <launch_args> --version`` before anything is spawned —
        V1 and V2 share the binary name (the V2 installer replaces the V1
        binary), so the major is the only way to pick the wire. A seam: tests
        monkeypatch it to skip the probe.

        Cached per binary identity ``(path, st_mtime_ns, st_size)``, so an
        upgrade in the middle of a run is re-detected on the next spawn while an
        unchanged binary is asked once. Run through ``childrun.run_argv`` for
        the same never-hang reasons as ``probe.binary_runs``.

        Raises :class:`OpencodeServerError` when ``--version`` cannot run, its
        output carries no version, or the major is not one this adapter speaks.
        A supported major older than its verified floor proceeds — ``bmad-loop
        validate`` warns about it."""
        stat = os.stat(resolved)
        key = (resolved, stat.st_mtime_ns, stat.st_size)
        version = self._version_cache.get(key)
        if version is None:
            version = self._detect_version(resolved)
            self._version_cache[key] = version
        verdict, message = support_verdict(version)
        if verdict == "unsupported-major":
            raise OpencodeServerError(f"{message}; {_VERSION_REMEDY}")
        return _PROTOCOLS[version[0]](), version

    def _detect_version(self, resolved: str) -> tuple[int, int, int]:
        argv = [resolved, *self.profile.launch_args, "--version"]
        shown = " ".join((self.binary, *self.profile.launch_args, "--version"))
        try:
            run = childrun.run_argv(argv, cwd=None, timeout=VERSION_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError, ProcessHostError) as e:
            raise OpencodeServerError(
                f"`{shown}` could not be run ({type(e).__name__}: {e}); {_VERSION_REMEDY}"
            ) from e
        if run.timed_out or run.interrupted:
            raise OpencodeServerError(
                f"`{shown}` did not finish within {VERSION_TIMEOUT_S:g}s; {_VERSION_REMEDY}"
            )
        output = ((run.stdout or "") + (run.stderr or "")).strip()
        first_line = output.splitlines()[0] if output else ""
        version = parse_opencode_version(first_line) if run.returncode == 0 else None
        if version is None:
            raise OpencodeServerError(
                f"could not read an OpenCode version from `{shown}` "
                f"(exit {run.returncode}, first line {first_line[:200]!r}); "
                f"supported: {supported_ranges()}; {_VERSION_REMEDY}"
            )
        return version

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
        """Poll the protocol's health endpoint (authenticated) until it answers
        healthy. The process liveness is re-checked after *every* probe: a
        foreign server answering 200 on a stolen port must not mask our own
        corpse, and the auth + shape check means a foreign 200 without our
        password never reads as ready.

        A server still answering ``200 text/html`` at the deadline is the web-UI
        shell each major serves for the other major's routes: ``--version``
        named one API and the server speaks another. That lands in
        ``sess.health_fault`` so the give-up error says so instead of the bare
        "not healthy in time" — the opaque failure an unrecognized server API
        used to produce."""
        deadline = time.monotonic() + self.health_timeout_s
        sess.health_fault = ""
        html = False
        with self._make_client(sess) as client:
            while time.monotonic() < deadline:
                healthy = False
                try:
                    probe = sess.protocol.health(client)
                    healthy, html = probe.healthy, probe.html
                except Exception:  # not up yet (conn refused, junk)
                    healthy, html = False, False
                if sess.process.poll() is not None:
                    return False
                if healthy:
                    return True
                time.sleep(self.health_poll_s)
        if html:
            sess.health_fault = (
                f"opencode answered HTML on the {sess.protocol.major}.x health endpoint — "
                "the server speaks a different OpenCode API than `--version` reported "
                f"({sess.server_version or 'unknown'}); {_VERSION_REMEDY}"
            )
        return False

    # -------------------------------------------------------------- adapter

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        task_dir = validated_task_directory(self.tasks_dir, spec.task_id)
        # `messages.json` is this transport's own; the rest are the inherited
        # `_ResultFileMixin`'s writes, validated here for the same reason
        # GenericAdapter validates them — see `RESULT_FILE_ARTIFACTS`.
        validate_adapter_artifact_paths(
            task_dir,
            (task_dir / "messages.json", *(task_dir / name for name in RESULT_FILE_ARTIFACTS)),
        )
        log_paths = [
            self.logs_dir / f"{spec.task_id}.log",
            self.logs_dir / f"{spec.task_id}.server.out",
            self.logs_dir / f"{spec.task_id}{PROVIDER_ERRORS_SUFFIX}",
        ]
        if self.sse_trace:
            log_paths.append(self.logs_dir / f"{spec.task_id}.sse.jsonl")
        validate_adapter_artifact_paths(self.logs_dir, tuple(log_paths))
        task_dir.mkdir(parents=True, exist_ok=True)
        reset_task_prompt(task_dir, spec.prompt)
        # Task ids are supplied by the caller, so defensively reset cycle-scoped
        # outputs if one is reused. A silent session must not inherit a stale result.
        # Iterating `journal.TASK_CYCLE_ARTIFACTS` is what makes the parity with
        # GenericAdapter.start_session structural instead of a claim in a test
        # docstring: both adapters and `resolve._gather_escalations` share one list.
        for artifact in TASK_CYCLE_ARTIFACTS:
            (task_dir / artifact).unlink(missing_ok=True)
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
        # diagnostics inside the SAME session. Every scanned file, the
        # provider-errors log included — its stale line pauses just the same.
        for scanned in self._env_fault_log_paths(spec.task_id):
            scanned.unlink(missing_ok=True)

        launched_ns = time.time_ns()
        sess = self._spawn_server(spec)
        sess.variant = spec.effort
        # Registered before the API handshake so the atexit sweep (and kill())
        # covers a crash mid-setup; run()'s finally-kill only exists once
        # start_session has returned a handle.
        self._sessions[spec.task_id] = sess
        try:
            sess.session_id = sess.protocol.create_session(sess.client, spec)

            self._start_sse_reader(sess, spec.task_id)
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
        sess.protocol.prompt(sess.client, sess.session_id, text, sess.variant)
        sess.floor_ms = max(sess.floor_ms, sent_ms)

    def send_text(self, handle: SessionHandle, text: str) -> None:
        """Nudge the running session. An undelivered nudge raises
        :class:`OpencodeNudgeSendError` (chained from the ``_prompt`` fault) so
        no caller reports it as sent (DW-503); the wait loop's own nudge sites
        catch it, crumb ``nudge-send-failed`` and carry on — a server that died
        between the liveness probe and the nudge is caught as `crashed` on the
        next tick, not by blowing up the completion loop."""
        sess = self._sessions.get(handle.task_id)
        if sess is None:
            raise OpencodeNudgeSendError(f"no live opencode session for task {handle.task_id!r}")
        try:
            self._prompt(sess, text)
        except Exception as exc:
            raise OpencodeNudgeSendError(f"{type(exc).__name__}: {exc}") from exc

    def _start_sse_reader(self, sess: _ServerSession, task_id: str) -> None:
        thread = threading.Thread(
            target=self._sse_loop,
            args=(sess, task_id),
            name=f"opencode-sse-{sess.port}",
            daemon=True,
        )
        sess.sse_thread = thread
        thread.start()

    def _sse_loop(self, sess: _ServerSession, task_id: str) -> None:
        """SSE reader: owns its own client (created and closed here — kill()
        never touches it; killing the server is what unblocks the read, with
        the read timeout as backstop). Filters idle/error to this session's id
        (child sessions share the stream), counts every other non-heartbeat
        frame as activity, and turns any disconnect into a single `gap`
        sentinel so the wait loop probes over HTTP for what the stream may
        have dropped.

        The fallback to polling is unchanged, but a raising stream is not a
        silent gap (DW-462): its first failure crumbs `sse-stream-failed`
        (``error``, ``frames_dropped``) and the next successful connect crumbs
        `sse-stream-recovered` (``failures``) — once per break, never once per
        failed reconnect against a dead server. A raise after `sse_stop` is the
        teardown closing the socket, not a fault, and is not crumbed. ``task_id``
        names the lifecycle file those crumbs land in."""
        httpx = self._httpx
        while not sess.sse_stop.is_set():
            try:
                with httpx.Client(
                    base_url=sess.base_url,
                    auth=(AUTH_USER, sess.password),
                    timeout=httpx.Timeout(5.0, read=self.sse_read_timeout_s),
                ) as client:
                    event_path = sess.protocol.event_path
                    with client.stream("GET", event_path) as resp:
                        if resp.status_code != 200:
                            raise OpencodeServerError(f"{event_path} -> {resp.status_code}")
                        # The server registers the subscriber once the response
                        # starts; events published after this are delivered.
                        sess.sse_connected.set()
                        if sess.sse_failures:
                            self._note_lifecycle(
                                task_id, "sse-stream-recovered", failures=sess.sse_failures
                            )
                            sess.sse_failures = 0
                        for event in _parse_sse_lines(
                            resp.iter_lines(),
                            lambda error: self._note_sse_drop(sess, task_id, error),
                            lambda: self._note_sse_comment(sess),
                        ):
                            if sess.sse_stop.is_set():
                                return
                            self._dispatch_sse(sess, event)
            except Exception as e:  # reader must never die; a break is crumbed
                if not sess.sse_stop.is_set():
                    sess.sse_failures += 1
                    if sess.sse_failures == 1:
                        self._note_lifecycle(
                            task_id,
                            "sse-stream-failed",
                            error=f"{type(e).__name__}: {e}",
                            frames_dropped=sess.sse_frames_dropped,
                        )
            if sess.sse_stop.is_set():
                return
            sess.events.put("gap")
            sess.sse_stop.wait(self.reconnect_sleep_s)

    def _note_sse_drop(self, sess: _ServerSession, task_id: str, error: str) -> None:
        """Count an undecodable SSE frame (DW-462); heartbeat.json carries the
        running count, and the session's first drop is crumbed with its error
        (`sse-frame-dropped`) — not every drop, so a garbage stream cannot flood
        the lifecycle file."""
        sess.sse_frames_dropped += 1
        if sess.sse_frames_dropped == 1:
            self._note_lifecycle(task_id, "sse-frame-dropped", error=error)

    @staticmethod
    def _note_sse_comment(sess: _ServerSession) -> None:
        """An SSE comment line — V2's ``: heartbeat`` — is liveness, not agent
        activity: it keeps the stream fresh for the wait loop's silence check
        and never bumps ``activity`` (V1's ``server.heartbeat`` frame drop, for
        a heartbeat that is not a frame)."""
        sess.last_frame_monotonic = time.monotonic()

    def _dispatch_sse(self, sess: _ServerSession, event: Any) -> None:
        if not isinstance(event, dict):
            return
        sess.last_frame_monotonic = time.monotonic()
        protocol = sess.protocol
        etype = protocol.frame_type(event)
        if etype in protocol.heartbeat_types:
            return
        # Any substantive frame — including child-session traffic — proves the
        # session tree is working (the parent is silent while a subagent
        # streams, exactly like subagent output in a tmux pane log).
        sess.activity += 1
        props, frame_session = protocol.frame_payload(event)
        # Session-scoped frames must name this session (child/subagent sessions
        # share the stream). A short allowlist of SESSION-LESS types passes
        # anyway: some frames worth logging carry no sessionID at all, so there
        # is no id to match and this filter would drop them before either sink.
        # See the protocol's `sessionless_types` (V1: `_SESSIONLESS_TYPES`) for
        # which ones, why it is an allowlist, and what deliberately stays out.
        #
        # Both sinks sit below this gate, so an allowlisted frame is traced as
        # well as rendered. That is intended: the trace's contract is one record
        # per frame the adapter acted on, and it is the file you read to explain
        # a line in the transcript — a rendered line with no matching record
        # would make the two sinks disagree. It costs nothing here because the
        # allowlist admits one low-rate type, not the noisy session-less bulk.
        if frame_session != sess.session_id and not (
            # `in` on a frozenset raises TypeError for an unhashable value, and
            # `type` is server-controlled, so narrow before the membership test
            # (the isinstance check below this filter is deliberately kept there
            # — moving it up would change the activity counter above).
            isinstance(etype, str)
            and etype in protocol.sessionless_types
        ):
            return
        if not isinstance(etype, str):
            # Every branch below compares `type` against a string literal, so a
            # frame carrying a non-string (or absent) type can match none of
            # them. Dropping it here narrows the value for the typed helpers and
            # keeps a malformed frame out of the trace — it is not a frame the
            # adapter acted on. Liveness/activity above already counted it.
            return
        # The env-fault evidence first, and outside the logging guard below: the
        # classifier reads this file after the session settles, so the line must
        # land before the turn end it may precede is queued.
        self._note_provider_error(sess, etype, props)
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
        if etype in protocol.idle_types:
            sess.events.put("idle")
        elif etype in protocol.error_types:
            sess.events.put("error")

    def _note_provider_error(self, sess: _ServerSession, etype: str, props: Any) -> None:
        """Append this session's provider-error frame, as one adapter-authored
        line (:func:`format_provider_error` — structured fields only, never the
        provider's response body), to ``logs/<task_id>.provider-errors.log``:
        the env-fault classifier's evidence on 2.x, whose server log carries no
        line per retry. A frame of another type writes nothing.

        Never raises — the reader must not die — but a lost line is a lost
        classification, so a failure is visible: a payload that will not format
        or an append that fails crumbs ``provider-errors-write-failed``
        (``frame``, ``error``) once per session."""
        if sess.provider_errors_path is None:
            return  # bare session: no sink
        try:
            line = sess.protocol.provider_error_line(etype, props)
            if line is None:
                return
            with sess.provider_errors_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as e:  # the reader must never die; the loss is crumbed
            if not sess.provider_errors_write_failed:
                sess.provider_errors_write_failed = True
                self._note_lifecycle(
                    sess.lifecycle_task,
                    "provider-errors-write-failed",
                    frame=etype,
                    error=f"{type(e).__name__}: {e}",
                )

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

        Which frames render, and how, is the protocol's
        (:meth:`V1Protocol.render_line` — the pinned frame facts live beside
        it); ``sess.msg_roles`` is the per-session role map it keys by message
        id."""
        line = sess.protocol.render_line(sess.msg_roles, etype, props)
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
        """The V1 inline line for one frame — :meth:`V1Protocol.inline_line`."""
        return _V1.inline_line(etype, props, role)

    def _emit_event(self, sess: _ServerSession, etype: str, props: dict) -> None:
        """Append one structured JSON object to the session's
        ``<task_id>.sse.jsonl`` sink — a record for every acted-on frame except
        the per-token deltas, for post-hoc replay/debugging. No-ops when the
        sink is off (``event_fh is None``, i.e. the ``sse_trace`` knob is down).

        The protocol's ``trace_skip_types`` are the exclusion — on V1,
        ``message.part.delta``. Its text concatenates byte-exactly to the
        ``message.part.updated`` record already in the file (pinned live — see
        the protocol module docstring), so every delta is bytes spent
        re-storing text the trace already holds, and they dominate the frame
        count on any real turn. ``logs/`` is never trimmed by retention, so that
        cost is permanent. Drop the type from the set to get them back."""
        if sess.event_fh is None:
            return
        if etype in sess.protocol.trace_skip_types:
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
        # the running streak of budget usage samples that failed (a transport
        # error, a non-200, a malformed payload — DW-461); heartbeat.json carries
        # it as of the previous sample (sampling follows the heartbeat write).
        usage_failures = 0

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
                        # the running budget usage-sample failure streak (DW-461)
                        "usage_sample_failures": usage_failures,
                        # SSE frames the parser could not decode (DW-462)
                        "sse_frames_dropped": sess.sse_frames_dropped,
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
                    weighted, usage_fault = self._sample_weighted_usage(sess, spec)
                    # A failed sample is still "no sample" this tick; its streak
                    # is crumbed at the transitions only (DW-461), the generic
                    # adapter's DW-452 model — a persistent fault leaves
                    # enforce mode off, which must not look like a quiet session.
                    if usage_fault is not None:
                        usage_failures += 1
                        if usage_failures == 1:
                            self._note_lifecycle(
                                handle.task_id, "usage-sample-failed", error=usage_fault
                            )
                    else:
                        if usage_failures:
                            self._note_lifecycle(
                                handle.task_id, "usage-sample-recovered", failures=usage_failures
                            )
                        usage_failures = 0
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
                            except MultiplexerError as e:
                                # a dead/hung server can't take the nudge; the
                                # grace still arms — the next tick's process
                                # poll scores a dead server crashed.
                                self._note_nudge_send_failed(handle, "budget", e)
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
                # V1 only (V2's terminal failure queues "idle"): session.error
                # may precede a retry, not a turn-end (status "retry" exists);
                # only a PROVABLY settled session reads as a
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
                                try:
                                    self.send_text(handle, STALL_NUDGE_TEXT)
                                except MultiplexerError as e:
                                    self._note_nudge_send_failed(handle, "stall", e)
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
                    try:
                        self.send_text(handle, NUDGE_TEXT)
                    except MultiplexerError as e:
                        self._note_nudge_send_failed(handle, "stop", e)
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
        """Tri-state busy probe — the protocol's ``session_busy``: True =
        busy/retrying, False = provably settled, None = unknowable. Unknown is
        not settled: callers must not treat a failed probe as proof a turn ended
        (the liveness-parity doctrine); the process poll settles real death."""
        return sess.protocol.session_busy(sess.client, sess.session_id)

    def _probe_completion(self, sess: _ServerSession) -> bool:
        """HTTP fallback for a lossy stream: the turn is finished
        only when the session is PROVABLY settled (an unknowable status is not
        settled) AND an assistant message completed strictly after the
        completion floor exists — an idle status alone is not proof a turn ran
        (abort emits idle even on an idle session). Consuming the evidence advances the floor so one
        completion can never be consumed twice."""
        if self._session_status(sess) is not False:
            return False
        completed = sess.protocol.latest_completed_ms(sess.client, sess.session_id)
        if completed is None:  # the read failed: the probe is advisory
            return False
        if completed > sess.floor_ms:
            sess.floor_ms = completed
            return True
        return False

    def _abort(self, sess: _ServerSession) -> None:
        if sess.client is None or not sess.session_id or sess.process.poll() is not None:
            return
        try:
            sess.protocol.abort(sess.client, sess.session_id)
        except Exception:  # nosec B110 - abort is best-effort
            pass

    # ----------------------------------------------------------------- usage

    def _sample_weighted_usage(
        self, sess: _ServerSession, spec: SessionSpec
    ) -> tuple[int | None, str | None]:
        """``(weighted, fault)``: the mid-session cumulative weighted spend over
        HTTP, or None when the guard must stay inert this tick; ``fault`` names
        why the sample failed (a transport error, a non-200, a malformed
        payload), else None. Never raises — sampling must not break the wait
        loop. No live session yet, or nothing tallied yet, is a clean None: the
        fault is returned apart from it (DW-461) so the wait loop can crumb the
        streak."""
        if sess.client is None or not sess.session_id:
            return None, None
        usage, fault = sess.protocol.sample_usage(sess.client, sess.session_id)
        if usage is None:
            return None, fault
        return usage.weighted_total(spec.cache_read_weight), None

    def _capture_usage(self, handle: SessionHandle, sess: _ServerSession) -> str | None:
        """Read usage over HTTP before teardown (state is server-side sqlite):
        dump the raw messages as the transcript and stash the token sum by
        session id for read_usage(). Best-effort in full — the crashed path
        runs this against a dead server and the verdict must not change.

        A fault leaves nothing stashed, so read_usage() answers None (untracked)
        and never a zero; it is crumbed as `usage-capture-failed` with ``stage``
        (``fetch`` — a GET, its status or its body; ``payload`` — a body of the
        wrong shape: a malformed V1 message list or V2 session; ``dump`` — the
        transcript write) and
        ``error`` (DW-461). The tally is stashed before the dump, so an
        unwritable transcript no longer costs the usage; nor does a failed
        transcript read on a protocol whose usage is read apart from it (V2) —
        that usage is still read, without the transcript (a zero total then
        stays untracked), and only the dump is skipped."""
        if sess.client is None or not sess.session_id:
            return None
        messages, fault = sess.protocol.transcript(sess.client, sess.session_id)
        if fault is not None:
            self._note_usage_capture_failed(handle, "fetch", fault)
            if sess.protocol.usage_reads_transcript:
                return None
        usage, usage_fault = sess.protocol.usage(sess.client, sess.session_id, messages)
        if usage is not None:
            self._stash_usage(sess.session_id, usage)
        elif usage_fault is not None:
            self._note_usage_capture_failed(handle, usage_fault.stage, usage_fault.error)
        if fault is not None:
            return None
        try:
            path = self.tasks_dir / handle.task_id / "messages.json"
            path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # the transcript is metadata too
            self._note_usage_capture_failed(handle, "dump", f"{type(e).__name__}: {e}")
            return None
        return str(path)

    def _note_usage_capture_failed(self, handle: SessionHandle, stage: str, error: str) -> None:
        self._note_lifecycle(handle.task_id, "usage-capture-failed", stage=stage, error=error)

    def _stash_usage(self, session_id: str, usage: TokenUsage) -> None:
        """Write into the capacity-bounded `_usage` stash (see USAGE_STASH_CAP).

        A plain dict is insertion-ordered on the 3.11 floor, so the oldest key is
        `next(iter(...))`. Only a NEW key can evict, so re-stashing a live
        session never drops a peer. Eviction is oldest-first, which makes an
        entry's survival a function of how many NEW sessions stashed after it;
        that a pending `read_usage(result)` still finds its entry rests on the
        cap's sizing margin (see USAGE_STASH_CAP), not on the order itself — a
        pending entry that HAD become the oldest is exactly what would be
        dropped."""
        if session_id not in self._usage:
            while len(self._usage) >= USAGE_STASH_CAP:
                del self._usage[next(iter(self._usage))]
        self._usage[session_id] = usage

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
        # settle liveness after the teardown. Retention is session-scoped, not
        # the adapter's lifetime (DW-106): a live Popen per completed session
        # would accumulate forever, so `_evict_task_state` drops the entry from
        # `run()`'s `finally` — still provably past `_post_kill_reconcile`,
        # which base `run()` calls INSIDE the call the mixin's `try` wraps.
        self._server_procs: dict[str, subprocess.Popen] = {}

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        handle = super().start_session(spec)
        self._server_procs[spec.task_id] = self._sessions[spec.task_id].process
        return handle

    def _evict_task_state(self, task_id: str) -> None:
        # The mixin cannot reach this transport-owned store, so the eviction seam
        # is extended here rather than moving `_server_procs` onto the mixin.
        self._server_procs.pop(task_id, None)
        super()._evict_task_state(task_id)

    def _probe_alive(self, handle: SessionHandle) -> bool | None:
        proc = self._server_procs.get(handle.task_id)
        if proc is None:
            return False  # never spawned: nothing we own is alive
        # Never None: the live Popen handle pins the pid, so poll() is always
        # answerable — unlike a tmux transport there is no probe that can hang.
        return proc.poll() is None
