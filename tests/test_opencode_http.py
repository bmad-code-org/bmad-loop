"""OpencodeHttpAdapter: unit tests + fake-binary E2E against a FakeOpencode.

The E2E cases spawn the adapter's real code path end to end: a fake `opencode`
binary (a stdlib-only HTTP server implementing the pinned 1.18.2 surface —
see the ``opencode_http`` module docstring) is launched by the adapter itself via the
conftest ``write_script_launcher`` shim, scripted per scenario through env vars
riding ``spec.env`` (the same channel the engine's BMAD_LOOP_* contract uses).
Everything binds 127.0.0.1; no real opencode binary or network access anywhere.
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import write_script_launcher

from bmad_loop import runs
from bmad_loop.adapters import base as adapter_base
from bmad_loop.adapters import generic, opencode_http
from bmad_loop.adapters.base import (
    AdapterTaskDirectoryError,
    SessionHandle,
    SessionResult,
    SessionSpec,
)
from bmad_loop.adapters.generic import BUDGET_NUDGE_TEXT, NUDGE_TEXT, STALL_NUDGE_TEXT
from bmad_loop.adapters.opencode_http import (
    _RESET,
    _TOOL_COLOR,
    OpencodeDevAdapter,
    OpencodeHttpAdapter,
    OpencodeServerError,
    _free_port,
    _now_ms,
    _parse_sse_lines,
    _role_color,
    _ServerSession,
    _sum_args,
    _sum_usage,
)
from bmad_loop.adapters.profile import get_profile
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.journal import TASK_CYCLE_ARTIFACTS
from bmad_loop.model import TokenUsage
from bmad_loop.policy import LimitsPolicy, NotifyPolicy, Policy
from bmad_loop.process_host import ProcessHostError, get_process_host

# A pinned example timestamp from the pins file (§4): OpenCode `time.*` values
# are epoch MILLISECONDS. The proof-of-work floor must live in the same unit —
# a ns-vs-ms comparison is always False and silently disables the poll
# fallback, so these tests anchor the unit explicitly.
PINNED_EPOCH_MS = 1_784_218_739_410

# ---------------------------------------------------------------- FakeOpencode
#
# Scenario contract (FAKE_OPENCODE_SCENARIO):
#   completed          prompt -> write result.json -> SSE session.idle
#   nudge-then-complete first prompt idles result-less; the second (the nudge)
#                      writes the result and idles
#   stall              every prompt idles result-less, forever
#   busy-forever       the turn never finishes and never idles
#   busy-big-usage     busy-forever, but /session/:id/message reports an
#                      assistant message with huge token counts mid-turn
#                      (completed=0, so the poll fallback never reads it as
#                      proof-of-work) — the budget-guard runaway
#   big-usage-then-complete  same huge mid-turn usage, but the turn finishes
#                      after ~0.5s (result + idle) — the warn-mode runaway
#                      that runs to its natural end
#   die-after-result   prompt -> write result.json -> the server process exits
#   die-no-result      prompt -> the server process exits
#   sse-black-hole     the SSE stream closes right after connecting (every
#                      reconnect); the turn completes result+messages only —
#                      completion is reachable only via the HTTP poll fallback
# FAKE_OPENCODE_START_FAILURES=N makes the first N spawns exit(1) pre-bind.
# FAKE_OPENCODE_LOG_LINE: written to the server's own stdout at the top of every
# turn — i.e. into logs/<task_id>.server.out, which is the channel the env-fault
# post-mortem reads (NOT <task_id>.log, the curated conversation transcript).
# Unset = silent, so every other scenario is unaffected.
# FAKE_OPENCODE_SPEC_PATH/_SPEC_TEXT: a bmad-dev-auto-style terminal spec the
# turn writes wherever a scenario writes its result (and at the start of
# busy-forever, for the post-kill rescue). Unset = no-op, like RESULT_PATH.
#
# Recordings under FAKE_OPENCODE_DIR: sessions.jsonl (incl. the Authorization
# header), prompts.jsonl, aborts.jsonl, pid (the server's own pid).

FAKE_OPENCODE = r"""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCENARIO = os.environ.get("FAKE_OPENCODE_SCENARIO", "completed")
REC_DIR = os.environ["FAKE_OPENCODE_DIR"]
RESULT_PATH = os.environ.get("FAKE_OPENCODE_RESULT_PATH", "")
LOG_LINE = os.environ.get("FAKE_OPENCODE_LOG_LINE", "")
SPEC_PATH = os.environ.get("FAKE_OPENCODE_SPEC_PATH", "")
SPEC_TEXT = os.environ.get("FAKE_OPENCODE_SPEC_TEXT", "")
START_FAILURES = int(os.environ.get("FAKE_OPENCODE_START_FAILURES", "0"))

argv = sys.argv[1:]
assert argv and argv[0] == "serve", argv
PORT = int(argv[argv.index("--port") + 1])
HOST = argv[argv.index("--hostname") + 1]
print(f"FAKE_STDOUT_CANARY listening on {HOST}:{PORT}", flush=True)

if START_FAILURES:
    counter = os.path.join(REC_DIR, "start-count")
    n = 0
    if os.path.exists(counter):
        with open(counter, encoding="utf-8") as fh:
            n = int(fh.read().strip() or 0)
    with open(counter, "w", encoding="utf-8") as fh:
        fh.write(str(n + 1))
    if n < START_FAILURES:
        sys.exit(1)

SESSION_ID = "ses_fake0000000000000000000001"
LOCK = threading.Lock()
STATE = {"busy": False, "completed_ms": 0, "prompts": 0}
EVENTS = []


def now_ms():
    return int(time.time() * 1000)


def record(name, obj):
    with LOCK:
        with open(os.path.join(REC_DIR, name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\n")


def push(evt):
    with LOCK:
        EVENTS.append(evt)


def pop_events():
    with LOCK:
        out, EVENTS[:] = EVENTS[:], []
    return out


def idle_event():
    return {"type": "session.idle", "properties": {"sessionID": SESSION_ID}}


def write_result():
    if RESULT_PATH:
        with open(RESULT_PATH, "w", encoding="utf-8") as fh:
            json.dump({"ok": True, "workflow": "fake-triage"}, fh)


def write_spec():
    if SPEC_PATH:
        os.makedirs(os.path.dirname(SPEC_PATH), exist_ok=True)
        with open(SPEC_PATH, "w", encoding="utf-8") as fh:
            fh.write(SPEC_TEXT)


def finish_turn():
    with LOCK:
        STATE["completed_ms"] = now_ms()
        STATE["busy"] = False


def run_turn():
    with LOCK:
        STATE["busy"] = True
        n = STATE["prompts"]
    if LOG_LINE:
        print(LOG_LINE, flush=True)  # stdout is the adapter's tee'd session log
    # let the 204 flush before any scripted death tears the connection down
    time.sleep(0.15)
    if SCENARIO == "completed":
        write_result(); write_spec(); finish_turn(); push(idle_event())
    elif SCENARIO == "nudge-then-complete":
        if n >= 2:
            write_result()
        finish_turn(); push(idle_event())
    elif SCENARIO == "stall":
        finish_turn(); push(idle_event())
    elif SCENARIO in ("busy-forever", "busy-big-usage"):
        write_spec()  # visible only post-kill: the turn never ends or idles
    elif SCENARIO == "big-usage-then-complete":
        time.sleep(0.35)  # stay busy through several fast heartbeat samples
        write_result(); finish_turn(); push(idle_event())
    elif SCENARIO == "die-after-result":
        write_result(); os._exit(0)
    elif SCENARIO == "die-no-result":
        os._exit(0)
    elif SCENARIO == "sse-black-hole":
        write_result(); finish_turn()  # completion visible over HTTP only


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/global/health":
            self._json(200, {"healthy": True, "version": "1.18.2"})
        elif self.path == "/session/status":
            with LOCK:
                busy = STATE["busy"]
            self._json(200, {SESSION_ID: {"type": "busy"}} if busy else {})
        elif self.path.startswith("/session/") and self.path.endswith("/message"):
            with LOCK:
                done = STATE["completed_ms"]
            msgs = []
            if SCENARIO in ("busy-big-usage", "big-usage-then-complete"):
                msgs = [{
                    "info": {
                        "id": "msg_big1", "role": "assistant",
                        "time": {"created": now_ms() - 10, "completed": done},
                        "tokens": {"input": 4000000, "output": 1000000, "reasoning": 0,
                                   "cache": {"read": 0, "write": 0}},
                        "cost": 1.0,
                    },
                    "parts": [],
                }]
            elif done:
                msgs = [{
                    "info": {
                        "id": "msg_fake1", "role": "assistant",
                        "time": {"created": done - 10, "completed": done},
                        "tokens": {"input": 100, "output": 50, "reasoning": 5,
                                   "cache": {"read": 7, "write": 3}},
                        "cost": 0.01,
                    },
                    "parts": [],
                }]
            self._json(200, msgs)
        elif self.path == "/event":
            self._sse()
        else:
            self._json(404, {"name": "NotFoundError"})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def emit(evt):
            self.wfile.write(b"data: " + json.dumps(evt).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        try:
            emit({"type": "server.connected", "properties": {}})
            if SCENARIO == "sse-black-hole":
                return  # close the stream: events are never deliverable
            last_beat = time.time()
            while True:
                for evt in pop_events():
                    emit(evt)
                if time.time() - last_beat > 0.2:
                    emit({"type": "server.heartbeat", "properties": {}})
                    last_beat = time.time()
                time.sleep(0.02)
        except OSError:
            return  # client went away

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/session":
            record("sessions.jsonl",
                   {"body": body, "auth": self.headers.get("Authorization", "")})
            self._json(200, {"id": SESSION_ID, "title": body.get("title", ""),
                             "cost": 0,
                             "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                        "cache": {"read": 0, "write": 0}},
                             "time": {"created": now_ms(), "updated": now_ms()}})
        elif self.path.endswith("/prompt_async"):
            record("prompts.jsonl", body)
            with LOCK:
                STATE["prompts"] += 1
            threading.Thread(target=run_turn, daemon=True).start()
            self.send_response(204)
            self.end_headers()
        elif self.path.endswith("/abort"):
            record("aborts.jsonl", {"path": self.path})
            self._json(200, True)
        else:
            self._json(404, {"name": "NotFoundError"})


if sys.platform == "win32":
    # SO_REUSEADDR on Windows allows binding a port already in LISTEN — under
    # xdist two fakes could silently share one port. Bind exclusively instead.
    ThreadingHTTPServer.allow_reuse_address = False
ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer.block_on_close = False

server = ThreadingHTTPServer((HOST, PORT), Handler)
with open(os.path.join(REC_DIR, "pid"), "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
server.serve_forever(poll_interval=0.05)
"""


# -------------------------------------------------------------------- helpers


def _policy(**limits) -> Policy:
    return Policy(limits=LimitsPolicy(**limits) if limits else LimitsPolicy())


def _shrink_timing(adapter: OpencodeHttpAdapter) -> OpencodeHttpAdapter:
    """Shrink every cadence for tests; the defaults are minutes of wall clock."""
    adapter.health_timeout_s = 10.0
    adapter.health_poll_s = 0.05
    adapter.reconnect_sleep_s = 0.05
    adapter.silence_threshold_s = 2.0
    adapter.poll_tick_s = 0.05
    adapter.result_grace_s = 0.5
    return adapter


def make_adapter(tmp_path: Path, binary: str = "opencode", **kwargs) -> OpencodeHttpAdapter:
    adapter = OpencodeHttpAdapter(
        run_dir=tmp_path / "run",
        policy=kwargs.pop("policy", _policy()),
        profile=get_profile("opencode"),
        binary=binary,
        **kwargs,
    )
    return _shrink_timing(adapter)


@pytest.fixture
def fake_opencode(tmp_path):
    """The fake `opencode` launcher plus its recording dir."""
    rec = tmp_path / "recordings"
    rec.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = write_script_launcher(bin_dir, "opencode", FAKE_OPENCODE)
    return launcher, rec


def make_spec(
    tmp_path: Path,
    rec: Path,
    scenario: str,
    task_id: str = "t-1",
    timeout_s: float = 30.0,
    stall_nudges_cap: int | None = 6,
    extra_env: dict | None = None,
    **spec_kw,
) -> SessionSpec:
    env = {
        "FAKE_OPENCODE_SCENARIO": scenario,
        "FAKE_OPENCODE_DIR": str(rec),
        "FAKE_OPENCODE_RESULT_PATH": str(tmp_path / "run" / "tasks" / task_id / "result.json"),
        **(extra_env or {}),
    }
    return SessionSpec(
        task_id=task_id,
        role="triage",
        prompt="/bmad-loop-sweep run it",
        cwd=tmp_path,
        env=env,
        timeout_s=timeout_s,
        stall_nudges_cap=stall_nudges_cap,
        **spec_kw,
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def assert_server_gone(rec: Path) -> None:
    """The fake's recorded pid must be dead once the adapter is done with it —
    `opencode serve` survives parent death, so a leak here is a real leak."""
    pid_file = rec / "pid"
    if not pid_file.is_file():
        return  # never got far enough to serve
    pid = int(pid_file.read_text(encoding="utf-8"))
    host = get_process_host()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not host.is_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"fake opencode server (pid {pid}) is still alive")


def prompt_texts(rec: Path) -> list[str]:
    return [p["parts"][0]["text"] for p in read_jsonl(rec / "prompts.jsonl")]


# ------------------------------------------------------------------ unit tests


def test_free_port_is_bindable():
    import socket

    port = _free_port()
    assert 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))  # still free (racy by design, but just picked)


def test_ms_floor_unit_matches_opencode_timestamps():
    """OpenCode timestamps are epoch ms. The completion floor derives
    from time_ns // 1e6 and MUST be comparable to them: same unit, same epoch."""
    floor = time.time_ns() // 1_000_000
    assert abs(floor - _now_ms()) < 5_000
    # comparable to the pins' pinned example (a real 1.18.2 response value):
    # the floor is later than that 2026 timestamp but within the same magnitude.
    assert PINNED_EPOCH_MS < floor < PINNED_EPOCH_MS * 10


def test_config_content_shapes(tmp_path):
    adapter = make_adapter(tmp_path)
    spec = SessionSpec(task_id="t", role="triage", prompt="p", cwd=tmp_path)
    config = json.loads(adapter._config_content(spec))
    assert config["permission"] == "allow"
    # hermetic-skills recipe (adapter docstring): the project skill tree, absolute
    assert config["skills"]["paths"] == [str(tmp_path / ".claude" / "skills")]
    assert "model" not in config

    spec_model = SessionSpec(
        task_id="t", role="triage", prompt="p", cwd=tmp_path, model="anthropic/claude-x"
    )
    config = json.loads(adapter._config_content(spec_model))
    assert config["model"] == "anthropic/claude-x"


def test_session_env_carries_contract(tmp_path):
    adapter = make_adapter(tmp_path)
    spec = SessionSpec(
        task_id="t", role="triage", prompt="p", cwd=tmp_path, env={"BMAD_LOOP_TASK_ID": "t"}
    )
    env = adapter._session_env(spec, "sekrit")
    assert env["BMAD_LOOP_TASK_ID"] == "t"
    assert env["OPENCODE_SERVER_PASSWORD"] == "sekrit"
    assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "1"
    json.loads(env["OPENCODE_CONFIG_CONTENT"])  # valid JSON


def test_sse_parser_accumulates_and_tolerates_junk():
    lines = [
        "data: " + json.dumps({"type": "server.connected", "properties": {}}),
        "",
        ": a comment",
        "id: 42",
        "data: not json",
        "",
        "data: " + json.dumps({"type": "session.idle", "properties": {"sessionID": "ses_1"}}),
        "",
    ]
    events = list(_parse_sse_lines(lines))
    assert [e["type"] for e in events] == ["server.connected", "session.idle"]


def test_sse_dispatch_filters_child_sessions(tmp_path):
    """A child/subagent session's idle must not read as the parent's turn-end,
    but its frames DO count as activity (the parent is silent while a child
    streams — the tmux pane-log analogue)."""
    from bmad_loop.adapters.opencode_http import _ServerSession

    adapter = make_adapter(tmp_path)
    sess = _ServerSession(process=None, port=0, base_url="", password="", log_fh=None)
    sess.session_id = "ses_parent"
    adapter._dispatch_sse(sess, {"type": "server.heartbeat", "properties": {}})
    assert sess.activity == 0 and sess.events.empty()
    adapter._dispatch_sse(sess, {"type": "session.idle", "properties": {"sessionID": "ses_child"}})
    assert sess.activity == 1 and sess.events.empty()
    adapter._dispatch_sse(
        sess, {"type": "message.part.updated", "properties": {"sessionID": "ses_child"}}
    )
    assert sess.activity == 2 and sess.events.empty()
    adapter._dispatch_sse(sess, {"type": "session.idle", "properties": {"sessionID": "ses_parent"}})
    assert sess.events.get_nowait() == "idle"
    adapter._dispatch_sse(
        sess, {"type": "session.error", "properties": {"sessionID": "ses_parent"}}
    )
    assert sess.events.get_nowait() == "error"


# ------------------------------- readable run logs + structured event JSONL
#
# The opencode-http adapter keeps three on-disk sinks per session:
#   <task_id>.log              — the readable transcript: curated one-line human
#                                progress via _render_inline ([bmad] lines only).
#   <task_id>.server.out       — the spawned server's own stdout/stderr (INFO /
#                                diagnostic lines), kept apart so <task_id>.log
#                                reads as a clean conversation, not a jumble.
#   <task_id>.sse.jsonl        — one JSON record per acted-on frame except the
#                                per-token deltas: the trace for post-hoc replay
#                                (_emit_event), gated by the sse_trace knob.
# The two text sinks (transcript + server stdout) are written from disjoint
# sources (SSE reader thread vs the server process); the JSONL is written from
# the SSE reader thread. These are pure unit tests: canned event dicts straight
# through _dispatch_sse, no real or fake opencode binary (the zero-token
# invariant from PR #167).


def _sess_with_sinks(tmp_path: Path, task_id: str = "t-log"):
    """A _ServerSession wired to real .log (binary append) and .sse.jsonl
    text sinks under tmp_path, so dispatch behaviour is assertable file-to-file.
    Mirrors how _spawn_server wires the real sinks (O_APPEND log + append jsonl)."""
    adapter = make_adapter(tmp_path)
    log_path = tmp_path / f"{task_id}.log"
    event_path = tmp_path / f"{task_id}.sse.jsonl"
    sess = _ServerSession(
        process=None,
        port=0,
        base_url="",
        password="",
        log_fh=log_path.open("ab"),
        event_fh=event_path.open("a", encoding="utf-8"),
    )
    sess.session_id = "ses_test"
    return adapter, sess, log_path, event_path


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _log_text(path: Path) -> str:
    # The transcript log now carries CRLF line endings (the TUI renders it
    # through a pyte terminal emulator); normalize to LF so assertions check
    # logical content, not the wire encoding. ANSI color escapes (the cyan
    # [bmad] marker) are stripped too so content assertions stay color-agnostic.
    if not path.is_file():
        return ""
    return _ANSI_RE.sub("", path.read_bytes().decode("utf-8").replace("\r\n", "\n"))


def test_inline_line_returns_none_for_unhandled_types():
    """_inline_line is the curate/no-op switch: None for anything not worth a
    live line, so those frames leave the run log byte-identical to today."""
    adapter = make_adapter(Path("/tmp"))  # no session built; _inline_line is pure
    assert adapter._inline_line("server.heartbeat", {}, "assistant") is None
    assert adapter._inline_line("server.connected", {}, "assistant") is None
    assert adapter._inline_line("session.created", {"sessionID": "x"}, "assistant") is None
    assert adapter._inline_line("session.idle", {"sessionID": "x"}, "assistant") is None
    assert adapter._inline_line("catalog.updated", {}, "assistant") is None
    assert adapter._inline_line("totally.unknown", {"sessionID": "x"}, "assistant") is None
    # message.updated renders no line (role refresh lives in _render_inline)
    assert (
        adapter._inline_line("message.updated", {"info": {"role": "assistant"}}, "assistant")
        is None
    )
    # empty/whitespace text carries no live value → None
    assert (
        adapter._inline_line("message.part.updated", {"part": {"text": "  "}}, "assistant") is None
    )
    assert adapter._inline_line("message.part.updated", {"part": {}}, "assistant") is None
    # message.part.delta never renders inline — it duplicates the complete
    # text message.part.updated already carries (and is kept out of the trace
    # for the same reason)
    assert adapter._inline_line("message.part.delta", {"delta": "wor"}, "assistant") is None
    assert adapter._inline_line("message.part.delta", {"delta": ""}, "assistant") is None
    # permission.updated does not exist on this surface at all (the live ask
    # frame is permission.asked) — nothing may be keyed to the dead name.
    assert (
        adapter._inline_line(
            "permission.updated", {"permission": {"type": "edit", "pattern": "src/**"}}, "assistant"
        )
        is None
    )


def test_inline_line_renders_each_curated_type():
    """Every event type worth a live line renders deterministically. The type
    strings and which of them actually fire on 1.18.2 are pinned in the adapter's
    module docstring (live probes 2026-07-16 / 2026-07-25) — notably there is no
    tool.call / tool.response on this SSE surface at all. The message.part.*
    prefix is the role recorded for that part's message id (tracked in
    _render_inline from message.updated.info.role)."""
    adapter = make_adapter(Path("/tmp"))
    assert adapter._inline_line("message.part.updated", {"part": {"text": "hi"}}, "assistant") == (
        f"\n{_role_color('assistant')}[bmad] assistant:{_RESET}\nhi\n"
    )
    assert adapter._inline_line("message.part.updated", {"part": {"text": "hi"}}, "user") == (
        f"\n{_role_color('user')}[bmad] user:{_RESET}\nhi\n"
    )
    # roles are distinct hues — the map pins them, so assistant != user colour
    assert _role_color("assistant") != _role_color("user")
    # an unseen role still renders, falling back to the default hue (no crash)
    assert adapter._inline_line("message.part.updated", {"part": {"text": "hi"}}, "system") == (
        f"\n{_role_color('system')}[bmad] system:{_RESET}\nhi\n"
    )
    # multi-line part.text: blank line above the role-colored header, then the
    # full body (internal newlines preserved) directly beneath — reads as a
    # contiguous block, not a prefix on every line.
    assert (
        adapter._inline_line(
            "message.part.updated",
            {"part": {"text": "para one\n\npara two\npara three"}},
            "assistant",
        )
        == f"\n{_role_color('assistant')}[bmad] assistant:{_RESET}\npara one\n\npara two\npara three\n"
    )
    assert adapter._inline_line(
        "command.executed", {"name": "Read", "arguments": None}, "assistant"
    ) == (f"{_TOOL_COLOR}[bmad] cmd: Read{_RESET}\n")
    assert adapter._inline_line("file.edited", {"file": "src/app.py"}, "assistant") == (
        f"{_TOOL_COLOR}[bmad] file: src/app.py{_RESET}\n"
    )
    # Permission fields are the live 1.18.2 ones: the ask frame is
    # permission.asked (permission.updated does not exist) carrying a
    # `permission` STRING plus a `patterns` list, and the reply carries `reply`.
    assert (
        adapter._inline_line(
            "permission.asked",
            {
                "id": "per_1",
                "permission": "bash",
                "patterns": ["echo permcheck"],
                "metadata": {"command": "echo permcheck"},
                "always": ["echo *"],
            },
            "assistant",
        )
        == f'{_TOOL_COLOR}[bmad] perm ask: bash ["echo permcheck"]{_RESET}\n'
    )
    # no patterns at all still names the permission rather than printing '?'
    assert adapter._inline_line("permission.asked", {"permission": "edit"}, "assistant") == (
        f"{_TOOL_COLOR}[bmad] perm ask: edit{_RESET}\n"
    )
    assert adapter._inline_line(
        "permission.replied", {"requestID": "per_1", "reply": "once"}, "assistant"
    ) == (f"{_TOOL_COLOR}[bmad] perm reply: once{_RESET}\n")


def test_inline_line_renders_session_error():
    """session.error ends the session via the queue put in _dispatch_sse; without
    a line here the transcript would simply stop mid-turn with no stated cause.
    Role-less like the other tool-family markers, so it takes the same hue. The
    error payload has no pinned shape on this surface, so any shape summarizes
    rather than raising or printing a bare '?'."""
    adapter = make_adapter(Path("/tmp"))
    assert adapter._inline_line(
        "session.error", {"sessionID": "x", "error": {"name": "ProviderAuthError"}}, "assistant"
    ) == (f'{_TOOL_COLOR}[bmad] error: {{"name": "ProviderAuthError"}}{_RESET}\n')
    # a bare string payload
    assert adapter._inline_line("session.error", {"error": "boom"}, "assistant") == (
        f"{_TOOL_COLOR}[bmad] error: boom{_RESET}\n"
    )
    # no detail at all still marks the failure — the line is the signal
    assert adapter._inline_line("session.error", {"sessionID": "x"}, "assistant") == (
        f"{_TOOL_COLOR}[bmad] error:{_RESET}\n"
    )
    # long payloads are truncated like any other one-line summary
    long_line = adapter._inline_line("session.error", {"error": "x" * 500}, "assistant")
    assert long_line is not None and "…" in long_line


def test_dispatch_session_error_renders_line_and_still_queues_error(tmp_path):
    """The inline line is additive: the control-path 'error' put still happens."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    adapter._dispatch_sse(
        sess,
        {"type": "session.error", "properties": {"sessionID": "ses_test", "error": "boom"}},
    )
    assert "[bmad] error: boom\n" in _log_text(log_path)
    assert sess.events.get_nowait() == "error"
    assert [r["type"] for r in read_jsonl(event_path)] == ["session.error"]


def test_dispatch_writes_assistant_text_to_log_and_jsonl(tmp_path):
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    adapter._dispatch_sse(
        sess,
        {
            "type": "message.part.updated",
            "properties": {"sessionID": "ses_test", "part": {"text": "hello"}},
        },
    )
    assert "\n[bmad] assistant:\nhello\n" in _log_text(log_path)
    # raw bytes still carry the assistant-role SGR (the write path does not strip it)
    assert _role_color("assistant") in log_path.read_bytes().decode("utf-8")
    records = read_jsonl(event_path)
    assert len(records) == 1
    assert records[0]["type"] == "message.part.updated"
    assert records[0]["properties"]["part"]["text"] == "hello"


def test_message_updated_keys_role_by_message_id(tmp_path):
    """``message.updated`` carries ``info.role`` under ``info.id`` but renders no
    line of its own; it records the role keyed by opencode message id so the
    following ``message.part.updated`` line prefixes with the right speaker
    (user vs assistant)."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    # a user turn: message.updated(role=user, id=u1) then the user's prompt text
    adapter._dispatch_sse(
        sess,
        {
            "type": "message.updated",
            "properties": {"sessionID": "ses_test", "info": {"id": "msg_u1", "role": "user"}},
        },
    )
    adapter._dispatch_sse(
        sess,
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test",
                "part": {"text": "do the thing", "messageID": "msg_u1"},
            },
        },
    )
    assert sess.msg_roles["msg_u1"] == "user"
    log = _log_text(log_path)
    assert "\n[bmad] user:\ndo the thing\n" in log
    assert "assistant: do the thing" not in log
    # message.updated itself renders no inline line, but IS in the JSONL trace
    types = [r["type"] for r in read_jsonl(event_path)]
    assert types == ["message.updated", "message.part.updated"]


def test_role_lookup_survives_out_of_order_message_updated(tmp_path):
    """``message.updated`` frames re-emit out of order: a stale user re-emit can
    land mid-assistant-turn. Keying role by message id (not "last seen") keeps
    the assistant's reply labeled ``assistant:`` even when a user re-emit
    arrives between the assistant's announcement and its reply text."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    # assistant announced
    adapter._dispatch_sse(
        sess,
        {
            "type": "message.updated",
            "properties": {"sessionID": "ses_test", "info": {"id": "msg_a1", "role": "assistant"}},
        },
    )
    # stale re-emit for the earlier user message (must NOT clobber the assistant)
    adapter._dispatch_sse(
        sess,
        {
            "type": "message.updated",
            "properties": {"sessionID": "ses_test", "info": {"id": "msg_u1", "role": "user"}},
        },
    )
    # assistant's reply part arrives — must still be labeled assistant:
    adapter._dispatch_sse(
        sess,
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test",
                "part": {"text": "done", "messageID": "msg_a1"},
            },
        },
    )
    log = _log_text(log_path)
    assert "\n[bmad] assistant:\ndone\n" in log
    assert "user: done" not in log


def test_part_without_known_message_id_falls_back_to_assistant(tmp_path):
    """A part whose message id has no recorded role falls back to ``assistant``
    so the line still renders rather than dropping."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    adapter._dispatch_sse(
        sess,
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_test",
                "part": {"text": "hi", "messageID": "msg_unseen"},
            },
        },
    )
    assert "\n[bmad] assistant:\nhi\n" in _log_text(log_path)


def test_dispatch_skips_empty_text_and_drops_deltas_from_both_sinks(tmp_path):
    """Empty/whitespace part text carries no live value, so it renders nothing
    while still being traced. message.part.delta is dropped from BOTH sinks: its
    tokens concatenate byte-exactly to the text message.part.updated already
    carries complete (pinned live), so tracing them re-stores text the file
    already holds — and logs/ is never trimmed by retention, so those bytes are
    permanent. Deltas also must not consume a seq: the trace's seq numbering has
    to match the lines actually in the file."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    for evt in (
        {
            "type": "message.part.updated",
            "properties": {"sessionID": "ses_test", "part": {"text": ""}},
        },
        {
            "type": "message.part.updated",
            "properties": {"sessionID": "ses_test", "part": {"text": "   "}},
        },
        {
            "type": "message.part.delta",
            "properties": {"sessionID": "ses_test", "delta": "streaming token"},
        },
    ):
        adapter._dispatch_sse(sess, evt)
    assert _log_text(log_path) == ""
    records = read_jsonl(event_path)
    assert [r["type"] for r in records] == ["message.part.updated", "message.part.updated"]
    assert [r["seq"] for r in records] == [1, 2]
    assert sess.event_seq == 2  # the delta burned no sequence number


def test_dispatch_writes_command_file_permission_lines(tmp_path):
    """The role-less marker family, dispatched with the payload shapes 1.18.2
    actually sends: command.executed carries a sessionID, file.edited carries
    none (it rides the session-less allowlist), and the permission pair uses
    permission/patterns + reply."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    for evt in (
        {
            "type": "command.executed",
            "properties": {"sessionID": "ses_test", "name": "Edit", "arguments": {"x": 1}},
        },
        {"type": "file.edited", "properties": {"file": "a.py"}},
        {
            "type": "permission.asked",
            "properties": {"sessionID": "ses_test", "permission": "bash", "patterns": ["rm *"]},
        },
        {
            "type": "permission.replied",
            "properties": {"sessionID": "ses_test", "requestID": "per_1", "reply": "deny"},
        },
    ):
        adapter._dispatch_sse(sess, evt)
    log = _log_text(log_path)
    assert "[bmad] cmd: Edit " in log
    assert "[bmad] file: a.py\n" in log
    assert '[bmad] perm ask: bash ["rm *"]\n' in log
    assert "[bmad] perm reply: deny\n" in log
    types = [r["type"] for r in read_jsonl(event_path)]
    assert types == ["command.executed", "file.edited", "permission.asked", "permission.replied"]


# ------------------------------------------------------------ tool-part render
#
# Agent tool use has no event of its own on this SSE surface: no tool.call, no
# tool.response, and command.executed never fires for it (0 frames across live
# bash/write/read/edit). It arrives as message.part.updated with a `tool`-typed
# part — which carries NO `part.text`, so the tool branch has to sit above
# _inline_line's empty-text bail-out to be reachable at all.
#
# A tool part re-fires on every state transition with the same part id
# (pending → running → terminal), so the render is gated on a TERMINAL status.
# That gate, not the rendering, is the correctness claim here — see the ABLATION
# note above test_tool_part_renders_once_per_call.


def _tool_part(status: str, **state) -> dict:
    """A `tool`-typed message part in the given state, shaped like 1.18.2's
    ToolPart (same part id across every transition of one call)."""
    return {
        "id": "prt_tool1",
        "sessionID": "ses_test",
        "messageID": "msg_a1",
        "type": "tool",
        "callID": "toolu_1",
        "tool": "bash",
        "state": {"status": status, **state},
    }


def _dispatch_tool(adapter, sess, part: dict) -> None:
    adapter._dispatch_sse(
        sess,
        {"type": "message.part.updated", "properties": {"sessionID": "ses_test", "part": part}},
    )


def test_tool_line_renders_name_and_input_summary():
    """The terminal fire renders the tool name plus a one-line input summary —
    the "what did the agent just do" the transcript exists for. state.output is
    deliberately absent from the line (it is a whole command's stdout or a whole
    file on this surface); the complete payload stays in the SSE trace."""
    adapter = make_adapter(Path("/tmp"))  # _inline_line is pure
    line = adapter._inline_line(
        "message.part.updated",
        {"part": _tool_part("completed", input={"command": "echo hi"}, output="OUTPUT_MARKER")},
        "assistant",
    )
    assert line == f'{_TOOL_COLOR}[bmad] tool: bash {{"command": "echo hi"}}{_RESET}\n'
    assert "OUTPUT_MARKER" not in line  # output is traced, never rendered inline
    # a tool with no input still names itself rather than dropping
    assert adapter._inline_line(
        "message.part.updated", {"part": _tool_part("completed", input={}, output="")}, "assistant"
    ) == (f"{_TOOL_COLOR}[bmad] tool: bash{_RESET}\n")
    # long inputs truncate like any other one-line summary
    long_line = adapter._inline_line(
        "message.part.updated",
        {"part": _tool_part("completed", input={"command": "x" * 500})},
        "assistant",
    )
    assert long_line is not None and "…" in long_line


def test_tool_line_marks_a_failed_call():
    """`error` is the other terminal status (ToolState is a 4-variant union), and
    a tool that failed is precisely what a reader goes to the transcript for —
    so it renders, with the error appended to the same line shape."""
    adapter = make_adapter(Path("/tmp"))
    assert adapter._inline_line(
        "message.part.updated",
        {"part": _tool_part("error", input={"command": "false"}, error="exit status 1")},
        "assistant",
    ) == (
        f'{_TOOL_COLOR}[bmad] tool: bash {{"command": "false"}} -> error: exit status 1{_RESET}\n'
    )
    # an error with no detail still marks the failure — the marker is the signal
    assert adapter._inline_line(
        "message.part.updated", {"part": _tool_part("error", input={})}, "assistant"
    ) == (f"{_TOOL_COLOR}[bmad] tool: bash -> error{_RESET}\n")


def test_tool_part_renders_to_the_log(tmp_path):
    """End to end through _dispatch_sse: a completed tool call lands in the
    readable transcript (this is the half of the feature that rendered nothing
    at all before — tool parts have no part.text)."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    _dispatch_tool(adapter, sess, _tool_part("completed", input={"file": "a.py"}, output="ok"))
    assert '[bmad] tool: bash {"file": "a.py"}\n' in _log_text(log_path)
    # role-less: no "[bmad] assistant:" header is emitted for a tool part
    assert "[bmad] assistant:" not in _log_text(log_path)
    assert _TOOL_COLOR in log_path.read_bytes().decode("utf-8")


# ABLATION (both directions run 2026-07-25). "One line per tool" is a PLACEMENT
# claim, and it is also what a renderer that emits NOTHING satisfies — so the
# presence ablation alone would not test the gate:
#   (a) delete the `part.get("type") == "tool"` branch from _inline_line → all
#       five tool tests fail; this one at `assert 0 == 1` on the count.
#   (b) INVERSE: drop the `status not in _TOOL_TERMINAL_STATES` gate from
#       _tool_line so it renders on every transition → this test fails on the
#       FIRST intermediate assert (pending rendered '[bmad] tool: bash'), which
#       is the proof the mutation actually fires: the pending and running frames
#       really do reach the branch, so the count assertion is not passing
#       vacuously. Left to run to completion the count is 3 != 1, with running
#       and completed rendering byte-identical lines.
# The intermediate "nothing yet" asserts are the placement claim stated directly;
# the count is the backstop.


def test_tool_part_renders_once_per_call(tmp_path):
    """One tool call = one transcript line, even though the part re-fires on
    pending, running and the terminal status with the same part id. The trace
    keeps all three fires: the sinks split curated-vs-complete by design."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    # the live progression: pending (no input yet) → running → completed
    _dispatch_tool(adapter, sess, _tool_part("pending", input={}, raw=""))
    assert _log_text(log_path) == ""  # nothing rendered while it is queued
    _dispatch_tool(adapter, sess, _tool_part("running", input={"command": "echo hi"}))
    assert _log_text(log_path) == ""  # nor while it runs
    _dispatch_tool(
        adapter, sess, _tool_part("completed", input={"command": "echo hi"}, output="hi\n")
    )

    log = _log_text(log_path)  # ANSI-stripped, CRLF normalized
    assert log.count("[bmad] tool: bash") == 1
    assert log == '[bmad] tool: bash {"command": "echo hi"}\n'
    # all three fires are in the trace — only the transcript is deduplicated
    records = read_jsonl(event_path)
    assert [r["properties"]["part"]["state"]["status"] for r in records] == [
        "pending",
        "running",
        "completed",
    ]


def test_tool_part_never_shadows_assistant_text(tmp_path):
    """The tool branch sits inside the message.part.updated arm, above the
    empty-text bail-out — it must not swallow ordinary text parts on the way."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    for part in (
        {"type": "text", "text": "thinking about it", "messageID": "msg_a1"},
        _tool_part("completed", input={"command": "ls"}, output="a.py"),
        {"type": "text", "text": "done", "messageID": "msg_a1"},
    ):
        _dispatch_tool(adapter, sess, part)
    log = _log_text(log_path)
    assert "\n[bmad] assistant:\nthinking about it\n" in log
    assert '[bmad] tool: bash {"command": "ls"}\n' in log
    assert "\n[bmad] assistant:\ndone\n" in log
    assert log.index("thinking about it") < log.index("tool: bash") < log.index("done")


def test_jsonl_seq_is_monotonic_with_ts_and_shape(tmp_path):
    """Each traced event gets one JSONL record with a monotonic seq, the raw
    type, the raw properties, and an epoch-ms ts that never goes backwards
    across the batch."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    for n in range(4):
        adapter._dispatch_sse(
            sess,
            {"type": "session.diff", "properties": {"sessionID": "ses_test", "diff": [str(n)]}},
        )
    records = read_jsonl(event_path)
    assert [r["seq"] for r in records] == [1, 2, 3, 4]
    assert all(set(r) == {"seq", "type", "properties", "ts"} for r in records)
    ts = [r["ts"] for r in records]
    assert ts == sorted(ts)  # epoch ms, non-decreasing within the batch
    # epoch ms, not ns and not seconds — the unit every opencode time.* field
    # uses, and the unit the poll fallback's floor comparison assumes
    assert all(abs(r["ts"] - _now_ms()) < 60_000 for r in records)
    assert all(r["properties"]["diff"] == [str(i)] for i, r in enumerate(records))


def test_unhandled_session_event_recorded_in_jsonl_not_log(tmp_path):
    """A session-scoped event _render_inline does not curate (e.g. session.diff)
    still passes the sessionID filter, so it lands in the complete-trace JSONL —
    but writes no human-readable line. The two sinks split by design: curated
    human log vs complete machine trace."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    adapter._dispatch_sse(
        sess, {"type": "session.diff", "properties": {"sessionID": "ses_test", "diff": []}}
    )
    assert _log_text(log_path) == ""
    records = read_jsonl(event_path)
    assert len(records) == 1 and records[0]["type"] == "session.diff"


def test_no_session_id_event_filtered_from_both_sinks(tmp_path):
    """An event with no matching sessionID (noise like catalog.updated) is
    filtered before either sink fires — neither a log line nor a JSONL record."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    adapter._dispatch_sse(sess, {"type": "catalog.updated", "properties": {}})
    assert _log_text(log_path) == ""
    assert read_jsonl(event_path) == []
    assert sess.event_seq == 0


# ----------------------------------------------- session-less type allowlist
#
# Some frames worth logging carry no sessionID at all: file.edited is exactly
# {"file": "/abs/path"} live, and 1.18.2 pins it additionalProperties:false over
# that one key — so the sessionID filter in _dispatch_sse drops it above both
# sinks and its render branch is unreachable without an explicit exemption.
# _SESSIONLESS_TYPES is that exemption, and it is sound ONLY because bmad-loop
# spawns one opencode serve per session, which makes a server-global frame
# unambiguously this session's.
#
# It is an allowlist rather than "allow anything session-less" because the
# session-less traffic is mostly noise that says nothing about the run:
# plugin.added alone was 90 of 388 frames in the live probe.
#
# ABLATION (verified 2026-07-25): revert _dispatch_sse's filter to the plain
# `props.get("sessionID") != sess.session_id` and the two tests that assert a
# session-less frame REACHES a sink fail. The two negative tests below
# (non-allowlisted, control-queue) keep passing under that ablation — they
# assert absence, which holds however the frame was dropped, so they document
# scope rather than prove reachability. The positive test carries that claim.


def test_sessionless_allowlisted_type_renders_and_traces(tmp_path):
    """file.edited has no sessionID at all, yet reaches BOTH sinks — the
    allowlist exempts it from the filter, and the trace stays consistent with the
    transcript (it is the file you read to explain a rendered line)."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    adapter._dispatch_sse(sess, {"type": "file.edited", "properties": {"file": "/abs/src/app.py"}})
    assert "[bmad] file: /abs/src/app.py\n" in _log_text(log_path)
    records = read_jsonl(event_path)
    assert [r["type"] for r in records] == ["file.edited"]
    assert records[0]["properties"] == {"file": "/abs/src/app.py"}


def test_sessionless_non_allowlisted_type_writes_nothing(tmp_path):
    """The allowlist is not a blanket exemption. plugin.added is session-less
    too and was 90 of 388 live frames; it must still be dropped above both
    sinks, burning no seq. Same for the file watcher, which fires for any change
    under the project (not just the agent's) and double-reports file.edited."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    for etype in ("plugin.added", "file.watcher.updated", "server.instance.disposed"):
        adapter._dispatch_sse(sess, {"type": etype, "properties": {"plugin": "x", "file": "y"}})
    assert _log_text(log_path) == ""
    assert read_jsonl(event_path) == []
    assert sess.event_seq == 0
    # liveness is unaffected: these frames still counted as activity, exactly as
    # before the allowlist existed (that counter sits above the filter)
    assert sess.activity == 3


def test_sessionless_allowlist_does_not_touch_the_control_queue(tmp_path):
    """The allowlist is a logging exemption only. A session-less frame must never
    reach the idle/error puts — those stay keyed to this session's id, or a
    foreign server-global frame could end the turn."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    for etype in ("session.idle", "session.error"):
        adapter._dispatch_sse(sess, {"type": etype, "properties": {}})
    assert sess.events.empty()
    # and an allowlisted one does not either
    adapter._dispatch_sse(sess, {"type": "file.edited", "properties": {"file": "a.py"}})
    assert sess.events.empty()


def test_unhashable_event_type_never_raises_in_the_filter(tmp_path):
    """The allowlist membership test sits OUTSIDE the try/except that guards the
    two sinks, so it must not raise on a server-controlled `type`: `in` on a
    frozenset raises TypeError for an unhashable value, hence the isinstance
    narrowing ahead of it."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    for etype in ({"a": 1}, ["file.edited"], {"file.edited"}):
        adapter._dispatch_sse(sess, {"type": etype, "properties": {}})  # must not raise
    assert _log_text(log_path) == ""
    assert read_jsonl(event_path) == []
    # the control path still works after them
    adapter._dispatch_sse(sess, {"type": "session.idle", "properties": {"sessionID": "ses_test"}})
    assert sess.events.get_nowait() == "idle"


def test_render_inline_noops_when_log_fh_is_none(tmp_path):
    """A bare _ServerSession (log_fh=None, as built by the older unit tests)
    must not raise when a curated event is dispatched — inline rendering is
    best-effort, never a crash path."""
    adapter = make_adapter(tmp_path)
    sess = _ServerSession(process=None, port=0, base_url="", password="", log_fh=None)
    sess.session_id = "ses_test"
    adapter._dispatch_sse(
        sess,
        {
            "type": "message.part.updated",
            "properties": {"sessionID": "ses_test", "part": {"text": "x"}},
        },
    )
    # event_fh defaults to None too: no jsonl written, no seq bumped
    assert sess.event_seq == 0


# --------------------------------------------- logging never breaks the stream
#
# Both sinks are written from _dispatch_sse ABOVE the idle/error queue puts, and
# both walk nested .get chains into server-controlled payloads. A frame that
# ships a string (or a list, or null) where the renderer expects a dict raises
# AttributeError. Unguarded, that propagates out of _dispatch_sse into the SSE
# reader's connection-level `except Exception` in _sse_loop, which treats it as
# a dead connection: it tears the stream down, puts a `gap`, sleeps, and
# reconnects — losing whatever the server had already buffered on that
# connection. That is a logging bug degrading completion signaling, so these
# tests assert the two properties that matter: the malformed frame does not
# raise, and a following session.idle still reaches the control queue.
#
# ABLATION (verified 2026-07-25): delete the try/except around the two helper
# calls in _dispatch_sse and the five tests below that feed a wrong-shaped
# payload through the RENDER path fail with AttributeError. The sixth
# (test_unserializable_properties_...) still passes ablated — its protection is
# _emit_event's own narrow `except (OSError, TypeError, ValueError)` around
# json.dumps, not this guard. It is kept as a regression test for that inner
# catch; do not read it as evidence for the outer one.

MALFORMED_FRAMES = [
    # part is a string, not a dict → _render_inline's part.get("messageID")
    {"type": "message.part.updated", "properties": {"part": "not-a-dict"}},
    # part is a list → same chain, different wrong shape
    {"type": "message.part.updated", "properties": {"part": ["text"]}},
    # info is a string → _render_inline's info.get("id") on the role refresh
    {"type": "message.updated", "properties": {"info": "not-a-dict"}},
    # a tool part whose state is a truthy non-dict → `part.get("state") or {}`
    # keeps it, then state.get("status") blows up. (The permission frames no
    # longer offer this shape: post-correction they read `permission` /
    # `patterns` / `reply` straight off props, which is always a dict here.)
    {"type": "message.part.updated", "properties": {"part": {"type": "tool", "state": "running"}}},
]


@pytest.mark.parametrize("frame", MALFORMED_FRAMES, ids=lambda f: f["type"])
def test_malformed_frame_never_breaks_dispatch(tmp_path, frame):
    """A server-shaped payload the renderer can't walk must not escape dispatch."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    event = {**frame, "properties": {**frame["properties"], "sessionID": "ses_test"}}

    adapter._dispatch_sse(sess, event)  # must not raise

    # and the control path is untouched: the next idle still queues
    adapter._dispatch_sse(sess, {"type": "session.idle", "properties": {"sessionID": "ses_test"}})
    assert sess.events.get_nowait() == "idle"


def test_malformed_frame_does_not_lose_the_trace_record(tmp_path):
    """_emit_event runs before _render_inline inside the guard, so the frame that
    broke rendering is still in the trace — which is the file you would read to
    diagnose it."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    adapter._dispatch_sse(
        sess,
        {
            "type": "message.part.updated",
            "properties": {"sessionID": "ses_test", "part": "not-a-dict"},
        },
    )
    records = read_jsonl(event_path)
    assert [r["type"] for r in records] == ["message.part.updated"]
    assert records[0]["properties"]["part"] == "not-a-dict"
    assert _log_text(log_path) == ""  # rendering aborted, nothing half-written


def test_unserializable_properties_never_break_dispatch(tmp_path):
    """The trace's own failure mode: json.dumps raising on a payload it can't
    encode must not take the stream down either. Held by _emit_event's own
    catch rather than the outer guard (see the ABLATION note above)."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    adapter._dispatch_sse(
        sess,
        {
            "type": "message.part.updated",
            "properties": {"sessionID": "ses_test", "part": {"text": "hi"}, "blob": {1, 2}},
        },
    )
    adapter._dispatch_sse(sess, {"type": "session.idle", "properties": {"sessionID": "ses_test"}})
    assert sess.events.get_nowait() == "idle"
    # the unserializable record is dropped, but the readable line still landed
    # and the trace keeps working for the frames that follow
    assert [r["type"] for r in read_jsonl(event_path)] == ["session.idle"]
    assert "\n[bmad] assistant:\nhi\n" in _log_text(log_path)


def test_non_string_event_type_is_dropped(tmp_path):
    """`type` absent or non-string can match no branch below, so the frame is
    dropped rather than traced — it is not something the adapter acted on."""
    adapter, sess, log_path, event_path = _sess_with_sinks(tmp_path)
    adapter._dispatch_sse(sess, {"properties": {"sessionID": "ses_test"}})
    adapter._dispatch_sse(sess, {"type": 7, "properties": {"sessionID": "ses_test"}})
    assert read_jsonl(event_path) == []
    assert _log_text(log_path) == ""
    assert sess.events.empty()


def test_sum_args_truncates_and_handles_shapes():
    """_sum_args keeps a command's arguments on one log line: short passthrough,
    dict→json, long→truncated with an ellipsis, falsy→empty."""
    assert _sum_args(None) == ""
    assert _sum_args("") == ""
    assert _sum_args({"a": 1}) == '{"a": 1}'
    assert _sum_args([1, 2]) == "[1, 2]"
    long = _sum_args("x" * 500)
    assert len(long) == 120 and long.endswith("\u2026")


def test_sum_usage_maps_opencode_tokens():
    messages = [
        {"info": {"role": "user", "tokens": {"input": 999}}},  # not assistant: ignored
        {
            "info": {
                "role": "assistant",
                "tokens": {
                    "input": 100,
                    "output": 50,
                    "reasoning": 5,
                    "cache": {"read": 7, "write": 3},
                },
            }
        },
        {
            "info": {
                "role": "assistant",
                "tokens": {
                    "input": 10,
                    "output": 20,
                    "reasoning": 0,
                    "cache": {"read": 1, "write": 2},
                },
            }
        },
        {"info": {"role": "assistant"}},  # tokenless (e.g. aborted): ignored
    ]
    usage = _sum_usage(messages)
    assert usage == TokenUsage(
        input_tokens=110, output_tokens=75, cache_read_tokens=8, cache_creation_tokens=5
    )
    assert _sum_usage("garbage") == TokenUsage()


def test_missing_httpx_names_the_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", None)
    with pytest.raises(OpencodeServerError, match=r"bmad-loop\[opencode\]"):
        make_adapter(tmp_path)


def test_missing_binary_is_a_clean_error(tmp_path):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spec = SessionSpec(task_id="t", role="triage", prompt="p", cwd=tmp_path)
    with pytest.raises(OpencodeServerError, match="not found on PATH"):
        adapter.start_session(spec)


@pytest.mark.parametrize(
    "task_id_kind", ["absolute", "parent-traversal", "empty", "windows-reserved"]
)
def test_start_session_refuses_unconfined_task_id_without_side_effects(tmp_path, task_id_kind):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spawn_calls = []
    adapter._spawn_server = lambda spec: spawn_calls.append(spec)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "prompt.txt").write_text("theirs", encoding="utf-8")
    for artifact in TASK_CYCLE_ARTIFACTS:
        (outside / artifact).write_text("theirs", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in outside.iterdir()}
    task_ids = {
        "absolute": str(outside),
        "parent-traversal": str(Path("..") / ".." / "outside"),
        "empty": "",
        "windows-reserved": "CON",
    }
    task_id = task_ids[task_id_kind]
    escaped_log = adapter.logs_dir / f"{task_id}.server.out"
    spec = SessionSpec(task_id=task_id, role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(AdapterTaskDirectoryError, match="expected one clean path segment"):
        adapter.start_session(spec)

    assert {path.name: path.read_bytes() for path in outside.iterdir()} == before
    assert list(adapter.tasks_dir.iterdir()) == []
    assert list(adapter.logs_dir.iterdir()) == []
    assert not escaped_log.exists()
    assert spawn_calls == []
    assert adapter._sessions == {}


def test_start_session_refuses_symlinked_task_directory_without_side_effects(tmp_path):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spawn_calls = []
    adapter._spawn_server = lambda spec: spawn_calls.append(spec)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "prompt.txt").write_text("theirs", encoding="utf-8")
    for artifact in TASK_CYCLE_ARTIFACTS:
        (outside / artifact).write_text("theirs", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in outside.iterdir()}
    task_id = "clean-task"
    task_dir = adapter.tasks_dir / task_id
    try:
        task_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    spec = SessionSpec(task_id=task_id, role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(AdapterTaskDirectoryError, match="symlink or junction"):
        adapter.start_session(spec)

    assert task_dir.is_symlink()
    assert {path.name: path.read_bytes() for path in outside.iterdir()} == before
    assert list(adapter.logs_dir.iterdir()) == []
    assert spawn_calls == []
    assert adapter._sessions == {}


@pytest.mark.parametrize("name", generic.RESULT_FILE_ARTIFACTS)
def test_start_session_refuses_a_redirected_mixin_artifact_without_side_effects(tmp_path, name):
    """The inherited `_ResultFileMixin` writes the heartbeat and both breadcrumb
    files under the task directory, so a reused directory carrying a symlink under
    one of those names is the same hazard the generic adapter refuses: the heartbeat
    overwrite truncates the link's target, a breadcrumb append lands outside the run.
    This adapter validated `messages.json` alone — its own file — and let the three
    mixin writes through.

    Parametrized over `generic.RESULT_FILE_ARTIFACTS` so a fourth mixin write is a
    new row here, not a new gap.

    Ablation: validate `(task_dir / "messages.json",)` alone again and every row
    reddens on the raise."""
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spawn_calls = []
    adapter._spawn_server = lambda spec: spawn_calls.append(spec)
    outside = tmp_path / "outside-target"
    outside.write_text("theirs", encoding="utf-8")
    task_id = "reused-task"
    task_dir = adapter.tasks_dir / task_id
    task_dir.mkdir(parents=True)
    try:
        (task_dir / name).symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    spec = SessionSpec(task_id=task_id, role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(AdapterTaskDirectoryError, match="symlink or junction"):
        adapter.start_session(spec)

    assert outside.read_text(encoding="utf-8") == "theirs"
    assert (task_dir / name).is_symlink()
    assert not (task_dir / "prompt.txt").exists()  # refused before the first write
    assert spawn_calls == []


def test_start_session_refuses_symlinked_tasks_root_without_side_effects(tmp_path):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spawn_calls = []
    adapter._spawn_server = lambda spec: spawn_calls.append(spec)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "theirs.txt").write_text("theirs", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in outside.iterdir()}
    adapter.tasks_dir.rmdir()
    try:
        adapter.tasks_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    spec = SessionSpec(task_id="clean-task", role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(AdapterTaskDirectoryError, match="tasks directory is a symlink"):
        adapter.start_session(spec)

    assert adapter.tasks_dir.is_symlink()
    assert {path.name: path.read_bytes() for path in outside.iterdir()} == before
    assert list(adapter.logs_dir.iterdir()) == []
    assert spawn_calls == []
    assert adapter._sessions == {}


def test_start_session_refuses_junction_like_tasks_root_before_mutation(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spawn_calls = []
    adapter._spawn_server = lambda spec: spawn_calls.append(spec)
    monkeypatch.setattr(adapter_base, "is_link_like", lambda path: Path(path) == adapter.tasks_dir)
    spec = SessionSpec(task_id="clean-task", role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(AdapterTaskDirectoryError, match="tasks directory is a symlink"):
        adapter.start_session(spec)

    assert list(adapter.tasks_dir.iterdir()) == []
    assert list(adapter.logs_dir.iterdir()) == []
    assert spawn_calls == []
    assert adapter._sessions == {}


def test_start_session_replaces_prompt_symlink_without_following_it(tmp_path):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    task_id = "clean-task"
    task_dir = adapter.tasks_dir / task_id
    task_dir.mkdir()
    outside_prompt = tmp_path / "outside-prompt.txt"
    outside_prompt.write_text("theirs", encoding="utf-8")
    prompt_path = task_dir / "prompt.txt"
    try:
        prompt_path.symlink_to(outside_prompt)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    spec = SessionSpec(task_id=task_id, role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(OpencodeServerError, match="not found on PATH"):
        adapter.start_session(spec)

    assert outside_prompt.read_text(encoding="utf-8") == "theirs"
    assert not prompt_path.is_symlink()
    assert prompt_path.read_text(encoding="utf-8") == "p\n"
    assert adapter._sessions == {}


def test_start_session_replaces_prompt_hardlink_without_following_it(tmp_path):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    task_id = "clean-task"
    task_dir = adapter.tasks_dir / task_id
    task_dir.mkdir()
    outside_prompt = tmp_path / "outside-prompt.txt"
    outside_prompt.write_text("theirs", encoding="utf-8")
    prompt_path = task_dir / "prompt.txt"
    try:
        os.link(outside_prompt, prompt_path)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    spec = SessionSpec(task_id=task_id, role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(OpencodeServerError, match="not found on PATH"):
        adapter.start_session(spec)

    assert outside_prompt.read_text(encoding="utf-8") == "theirs"
    assert prompt_path.stat().st_ino != outside_prompt.stat().st_ino
    assert prompt_path.read_text(encoding="utf-8") == "p\n"
    assert adapter._sessions == {}


def test_start_session_refuses_redirected_messages_before_mutation(tmp_path):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spawn_calls = []
    adapter._spawn_server = lambda spec: spawn_calls.append(spec)
    task_dir = adapter.tasks_dir / "clean-task"
    task_dir.mkdir()
    outside = tmp_path / "outside-messages.json"
    outside.write_text("theirs", encoding="utf-8")
    try:
        (task_dir / "messages.json").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    spec = SessionSpec(task_id="clean-task", role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(AdapterTaskDirectoryError, match="artifact is a symlink"):
        adapter.start_session(spec)

    assert outside.read_text(encoding="utf-8") == "theirs"
    assert not (task_dir / "prompt.txt").exists()
    assert list(adapter.logs_dir.iterdir()) == []
    assert spawn_calls == []
    assert adapter._sessions == {}


@pytest.mark.parametrize("suffix", [".log", ".server.out", ".sse.jsonl"])
def test_start_session_refuses_redirected_log_path_before_mutation(tmp_path, suffix):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spawn_calls = []
    adapter._spawn_server = lambda spec: spawn_calls.append(spec)
    outside = tmp_path / "outside.log"
    outside.write_text("theirs", encoding="utf-8")
    try:
        (adapter.logs_dir / f"clean-task{suffix}").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    spec = SessionSpec(task_id="clean-task", role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(AdapterTaskDirectoryError, match="artifact is a symlink"):
        adapter.start_session(spec)

    assert outside.read_text(encoding="utf-8") == "theirs"
    assert not (adapter.tasks_dir / "clean-task").exists()
    assert spawn_calls == []
    assert adapter._sessions == {}


def test_start_session_refuses_redirected_logs_root_before_mutation(tmp_path):
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spawn_calls = []
    adapter._spawn_server = lambda spec: spawn_calls.append(spec)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "clean-task.server.out"
    outside_file.write_text("theirs", encoding="utf-8")
    adapter.logs_dir.rmdir()
    try:
        adapter.logs_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    spec = SessionSpec(task_id="clean-task", role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(AdapterTaskDirectoryError, match="artifact directory is a symlink"):
        adapter.start_session(spec)

    assert outside_file.read_text(encoding="utf-8") == "theirs"
    assert not (adapter.tasks_dir / "clean-task").exists()
    assert spawn_calls == []
    assert adapter._sessions == {}


def test_start_session_refuses_junction_like_task_directory_before_mutation(tmp_path, monkeypatch):
    """The adapter consumes the shared predicate's Windows-junction verdict.

    platform_util's reparse-tag tests own junction detection itself; an ordinary
    directory standing in here makes this composition arm run on every platform.
    """
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spawn_calls = []
    adapter._spawn_server = lambda spec: spawn_calls.append(spec)
    task_id = "clean-task"
    task_dir = adapter.tasks_dir / task_id
    task_dir.mkdir()
    (task_dir / "prompt.txt").write_text("theirs", encoding="utf-8")
    for artifact in TASK_CYCLE_ARTIFACTS:
        (task_dir / artifact).write_text("theirs", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in task_dir.iterdir()}
    monkeypatch.setattr(adapter_base, "is_link_like", lambda path: Path(path) == task_dir)
    spec = SessionSpec(task_id=task_id, role="triage", prompt="p", cwd=tmp_path)

    with pytest.raises(AdapterTaskDirectoryError, match="symlink or junction"):
        adapter.start_session(spec)

    assert {path.name: path.read_bytes() for path in task_dir.iterdir()} == before
    assert list(adapter.logs_dir.iterdir()) == []
    assert spawn_calls == []
    assert adapter._sessions == {}


def test_start_session_drops_every_reused_task_cycle_artifact(tmp_path):
    """Parity with GenericAdapter: both adapters own a tasks/<id>/ dir, so both must
    drop a prior cycle's artifacts before a re-armed run reusing the id lands there.
    No fake server needed: the unlinks run BEFORE _spawn_server's PATH check raises,
    so a missing binary still exercises them.

    That parity is now STRUCTURAL rather than asserted twice in prose: both adapters
    loop over `journal.TASK_CYCLE_ARTIFACTS`, this test iterates the same constant,
    and `test_portability_guard.test_task_cycle_artifacts_named_only_through_the_constant`
    refuses the bare literal that would let one adapter drift from the other. A third
    artifact added to the constant is covered here with no edit."""
    adapter = make_adapter(tmp_path, binary="definitely-not-a-real-binary-xyz")
    spec = SessionSpec(task_id="t-1", role="triage", prompt="p", cwd=tmp_path)
    task_dir = adapter.tasks_dir / "t-1"
    task_dir.mkdir(parents=True, exist_ok=True)
    assert TASK_CYCLE_ARTIFACTS, "the constant is the list under test; an empty one is vacuous"
    stale = [task_dir / name for name in TASK_CYCLE_ARTIFACTS]
    for path in stale:
        path.write_text(
            json.dumps({"escalations": [{"severity": "CRITICAL", "detail": "last cycle"}]}),
            encoding="utf-8",
        )

    with pytest.raises(OpencodeServerError, match="not found on PATH"):
        adapter.start_session(spec)
    assert [p for p in stale if p.exists()] == []

    # ...and the ordinary case — nothing left behind — reaches the same spawn error,
    # i.e. the unlinks are missing_ok and did not become the failure themselves
    with pytest.raises(OpencodeServerError, match="not found on PATH"):
        adapter.start_session(spec)


def test_kill_unknown_handle_is_a_noop(tmp_path):
    adapter = make_adapter(tmp_path)
    adapter.kill(SessionHandle(task_id="never-started", native_id="ses_x"))


@pytest.mark.skipif(sys.platform == "win32", reason="os.kill(0) reap probe is POSIX")
@pytest.mark.skipif(
    not sys.platform.startswith("linux") and importlib.util.find_spec("psutil") is None,
    reason="descendant discovery off Linux needs psutil (the non-linux extra)",
)
def test_kill_process_reaps_detached_descendant(tmp_path):
    """#183 mirror on the HTTP transport, deterministic without a real opencode
    binary: a focused _kill_process test with a real Popen server whose body
    detaches a child into its own session (``start_new_session=True`` — Python's
    portable setsid; macOS ships no setsid(1) utility, so the root SIGTERM cannot
    reach it) against the REAL process host. After _kill_process the detached child
    is reaped, proving the pre-signal descendant harvest + reap covers a straggler
    the pane/pgid kill would leak (a live opencode binary is not required, and the
    live-server harness cannot easily be made to detach a child — noted in the
    report)."""
    adapter = make_adapter(tmp_path)
    adapter.kill_wait_s = 3.0
    child_pid_file = tmp_path / "detached.pid"
    # The "server" detaches a session-leader child (records its pid), then idles so
    # it is provably alive at harvest — the server (process.pid) is the parent of
    # the detached child, so host.descendants(server) finds it before the SIGTERM.
    server_body = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],"
        " start_new_session=True)\n"
        f"open({str(child_pid_file)!r}, 'w', encoding='utf-8').write(str(p.pid))\n"
        "time.sleep(300)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", server_body])
    detached_pid = None
    try:
        deadline = time.monotonic() + 10
        while not child_pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_pid_file.is_file(), "server never recorded its detached child"
        detached_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
        # sanity: the recorded pid is the setsid'd process and is currently alive
        os.kill(detached_pid, 0)

        sess = _ServerSession(process=process, port=0, base_url="", password="", log_fh=None)
        adapter._kill_process(sess)

        assert process.poll() is not None  # root server reaped
        reap_deadline = time.monotonic() + 10
        while True:
            try:
                os.kill(detached_pid, 0)
            except ProcessLookupError:
                break  # detached child reaped by the descendant sweep
            assert time.monotonic() < reap_deadline, f"detached child {detached_pid} survived"
            time.sleep(0.05)
    finally:
        for pid in (detached_pid, process.pid):
            if pid is None:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def test_kill_process_strikes_root_before_reraising_bad_host_override(tmp_path, monkeypatch):
    """The process-host lookup precedes the first signal; an explicit-but-bogus
    BMAD_LOOP_PROCESS_HOST must still raise loudly (never silently mis-signal), but
    the server must not be left alive behind the raise — one legacy Popen root
    strike fires, then ProcessHostError propagates (the tmux adapter's
    strike-before-reraise doctrine, mirrored)."""

    class _FakePopen:
        pid = 4242

        def __init__(self):
            self.terminated = 0
            self.killed = 0

        def poll(self):
            return None  # alive → _kill_process must not early-return

        def terminate(self):
            self.terminated += 1

        def kill(self):
            self.killed += 1

    adapter = make_adapter(tmp_path)
    process = _FakePopen()
    sess = _ServerSession(process=process, port=0, base_url="", password="", log_fh=None)
    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "bogus-host-name")
    get_process_host.cache_clear()
    try:
        with pytest.raises(ProcessHostError):
            adapter._kill_process(sess)
        if sys.platform == "win32":
            assert process.killed == 1  # the win32 legacy strike is Popen.kill()
        else:
            assert process.terminated == 1  # struck once before the raise
    finally:
        get_process_host.cache_clear()


class _ReapRecordingHost:
    """Minimal host for the reap-gate unit test: nobody dies on SIGTERM, so the
    force-kill loop is what settles survivors — proving the identity gate, not
    terminate, decides who gets force-killed. identity is None for ``no_identity``."""

    def __init__(self, alive=(), no_identity=()):
        self.alive = set(alive)
        self.no_identity = set(no_identity)
        self.terminated: list[int] = []
        self.force_killed: list[int] = []

    def identity(self, pid):
        return None if pid in self.no_identity else float(pid)

    def alive_and_ours(self, pid, identity):
        if pid not in self.alive:
            return False
        return identity is None or identity == self.identity(pid)

    def terminate(self, pid):
        self.terminated.append(pid)  # deliberately does NOT kill — force_kill settles it

    def force_kill(self, pid):
        self.force_killed.append(pid)
        self.alive.discard(pid)


def test_reap_descendants_never_signals_none_identity(tmp_path):
    """_reap_descendants signals only identity-confirmed stragglers; a
    None-identity survivor (a possible pid reuse) is never signalled AT ALL — no
    terminate, no force-kill, no poll burn (even a SIGTERM to a recycled pid kills
    an innocent process) — the ProcessHost contract, mirrored on the HTTP teardown
    (opencode_http.py:_reap_descendants)."""
    adapter = make_adapter(tmp_path)
    adapter.kill_wait_s = 0.1  # bound the poll: nobody dies on terminate here
    host = _ReapRecordingHost(alive={200, 400}, no_identity={400})
    tree = {200: 200.0, 400: None}  # 200 identity-confirmed at harvest, 400 unconfirmable
    adapter._reap_descendants(host, tree)
    assert host.terminated == [200]  # the unconfirmable 400 is never asked to stop
    assert host.force_killed == [200]  # only the confirmed pid escalated
    assert 400 not in host.force_killed


def test_read_usage_returns_stash_by_session_id(tmp_path):
    from bmad_loop.adapters.base import SessionResult

    adapter = make_adapter(tmp_path)
    adapter._usage["ses_1"] = TokenUsage(input_tokens=1)
    assert adapter.read_usage(SessionResult(status="completed", session_id="ses_1")).total == 1
    assert adapter.read_usage(SessionResult(status="completed", session_id="ses_2")) is None
    assert adapter.read_usage(SessionResult(status="completed")) is None


def test_sample_weighted_usage_inert_on_http_failure(tmp_path):
    """The budget guard's mid-session sample must never break the wait loop:
    no live session yet, a transport error, or a non-200 all read as None
    (guard inert this tick)."""
    adapter = make_adapter(tmp_path)
    spec = SessionSpec(task_id="t", role="triage", prompt="p", cwd=tmp_path)
    sess = _ServerSession(process=None, port=0, base_url="", password="", log_fh=None)
    assert adapter._sample_weighted_usage(sess, spec) is None  # no session id yet

    sess.session_id = "ses_1"

    class _BoomClient:
        def get(self, path):
            raise RuntimeError("connection refused")

    sess.client = _BoomClient()
    assert adapter._sample_weighted_usage(sess, spec) is None

    class _Client500:
        def get(self, path):
            class _Resp:
                status_code = 500

            return _Resp()

    sess.client = _Client500()
    assert adapter._sample_weighted_usage(sess, spec) is None


# ------------------------------------------------------------------- E2E tests


def test_e2e_completed(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "completed")

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json == {"ok": True, "workflow": "fake-triage"}
    assert result.session_id and result.session_id.startswith("ses_")
    # transcript = the raw messages dump; usage stashed for read_usage
    assert result.transcript_path and Path(result.transcript_path).is_file()
    usage = adapter.read_usage(result)
    assert usage == TokenUsage(
        input_tokens=100, output_tokens=55, cache_read_tokens=7, cache_creation_tokens=3
    )
    # the prompt went through the profile's template
    assert prompt_texts(rec) == ["Use the bmad-loop-sweep skill now: run it"]
    # authenticated transport end to end
    sessions = read_jsonl(rec / "sessions.jsonl")
    assert sessions and sessions[0]["auth"].startswith("Basic ")
    # teardown: registry empty, server dead, log tee landed
    assert adapter._sessions == {}
    assert_server_gone(rec)
    assert (tmp_path / "run" / "logs" / "t-1.log").exists()
    # the server's own stdout is redirected to its own file, keeping the
    # readable transcript in <task>.log clean of server INFO noise
    server_log = (tmp_path / "run" / "logs" / "t-1.server.out").read_text()
    assert "FAKE_STDOUT_CANARY" in server_log
    assert "FAKE_STDOUT_CANARY" not in (tmp_path / "run" / "logs" / "t-1.log").read_text()
    # the SSE trace is on by default and named .sse.jsonl
    trace = read_jsonl(tmp_path / "run" / "logs" / "t-1.sse.jsonl")
    assert trace and "session.idle" in {r["type"] for r in trace}


def test_e2e_sse_trace_knob_off_never_opens_the_sink(tmp_path, fake_opencode):
    """sse_trace is a tier-1 instance knob, not a policy field: flipping it off
    means the file is never created at all (not created-then-empty), while the
    run itself and the readable transcript are unaffected."""
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    adapter.sse_trace = False
    spec = make_spec(tmp_path, rec, "completed")

    result = adapter.run(spec)

    assert result.status == "completed"
    assert not (tmp_path / "run" / "logs" / "t-1.sse.jsonl").exists()
    assert (tmp_path / "run" / "logs" / "t-1.log").exists()
    assert_server_gone(rec)


def test_e2e_result_less_stop_nudges_then_completes(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "nudge-then-complete")

    result = adapter.run(spec)

    assert result.status == "completed"
    texts = prompt_texts(rec)
    assert len(texts) == 2
    assert texts[1] == NUDGE_TEXT  # the wake-up carried the result-contract nudge
    assert_server_gone(rec)


def test_e2e_stall_after_nudge_budget(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "stall")

    result = adapter.run(spec)

    # default budget: 1 stop-nudge; the second result-less idle is a stall
    assert result.status == "stalled"
    assert result.result_json is None
    assert len(prompt_texts(rec)) == 2
    breadcrumbs = read_jsonl(tmp_path / "run" / "tasks" / "t-1" / "resultless-stops.jsonl")
    assert breadcrumbs and all(b["verdict"] == "no-result-json" for b in breadcrumbs)
    assert_server_gone(rec)


def test_e2e_monotonic_stall_cap(tmp_path, fake_opencode):
    """#149 on the HTTP transport: every result-less idle refills the per-silence
    wake budget, so only the monotonic spec.stall_nudges_cap bounds a session
    that keeps ending its turn without a result."""
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher), stop_without_result_nudges=0)
    adapter._stall_grace_s = 0.3
    adapter._stall_nudges = 99  # per-silence budget effectively unbounded
    spec = make_spec(tmp_path, rec, "stall", stall_nudges_cap=2, timeout_s=60.0)

    result = adapter.run(spec)

    assert result.status == "stalled"
    assert result.result_json is None
    texts = prompt_texts(rec)
    # initial prompt + exactly cap stall-nudges, then the stall verdict
    assert len(texts) == 3
    assert texts[1] == STALL_NUDGE_TEXT and texts[2] == STALL_NUDGE_TEXT
    assert_server_gone(rec)


def test_e2e_timeout_aborts(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "busy-forever", timeout_s=1.5)

    result = adapter.run(spec)

    assert result.status == "timeout"
    assert result.result_json is None
    aborts = read_jsonl(rec / "aborts.jsonl")
    assert aborts and "/abort" in aborts[0]["path"]
    assert_server_gone(rec)


# ------------------------------ mid-session token-budget guard (#158)
#
# Mirrors the generic-adapter guard on the HTTP transport: cumulative usage is
# sampled from GET /session/:id/message on the heartbeat cadence (the first
# tick always samples), and an enforce-mode trip nudges, arms the grace, then
# aborts the session and returns over_budget — the timeout path's exit shape.


def _budget_policy() -> Policy:
    return Policy(limits=LimitsPolicy(), notify=NotifyPolicy(desktop=False, file=True))


def test_e2e_budget_enforce_trips_nudges_and_aborts_over_budget(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher), policy=_budget_policy())
    spec = make_spec(
        tmp_path,
        rec,
        "busy-big-usage",
        timeout_s=30.0,
        token_budget=1_000_000,
        token_budget_mode="enforce",
        token_budget_grace_s=0.3,
    )

    result = adapter.run(spec)

    assert result.status == "over_budget"
    assert result.result_json is None
    # fake reports input 4M + output 1M, no cache: weighted = 5M
    assert result.budget_weighted == 5_000_000
    texts = prompt_texts(rec)
    assert len(texts) == 2 and texts[1] == BUDGET_NUDGE_TEXT
    aborts = read_jsonl(rec / "aborts.jsonl")
    assert aborts and "/abort" in aborts[0]["path"]
    # trip actions fired exactly once: one ATTENTION line, one breadcrumb
    attention = (tmp_path / "run" / "ATTENTION").read_text(encoding="utf-8")
    assert len(attention.splitlines()) == 1
    lifecycle = read_jsonl(tmp_path / "run" / "tasks" / "t-1" / "session-lifecycle.jsonl")
    tripped = [ln for ln in lifecycle if ln["event"] == "budget-tripped"]
    assert len(tripped) == 1
    assert tripped[0]["weighted"] == 5_000_000 and tripped[0]["mode"] == "enforce"
    # the verdict leaves a breadcrumb, like timeout-fired (#157 forensics)
    fired = [ln for ln in lifecycle if ln["event"] == "over-budget-fired"]
    assert len(fired) == 1
    assert fired[0]["weighted"] == 5_000_000 and fired[0]["zero_grace"] is False
    # usage was captured over HTTP before teardown
    assert result.transcript_path and Path(result.transcript_path).is_file()
    usage = adapter.read_usage(result)
    assert usage == TokenUsage(input_tokens=4_000_000, output_tokens=1_000_000)
    assert_server_gone(rec)


def test_e2e_budget_zero_grace_terminates_at_trip_without_nudge(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher), policy=_budget_policy())
    spec = make_spec(
        tmp_path,
        rec,
        "busy-big-usage",
        timeout_s=30.0,
        token_budget=1_000_000,
        token_budget_mode="enforce",
        token_budget_grace_s=0.0,
    )

    result = adapter.run(spec)

    assert result.status == "over_budget"
    assert result.budget_weighted == 5_000_000
    assert prompt_texts(rec) == ["Use the bmad-loop-sweep skill now: run it"]  # no nudge
    assert_server_gone(rec)


def test_e2e_budget_inert_under_cap(tmp_path, fake_opencode, monkeypatch):
    """A session whose weighted usage stays under its cap never trips: no
    ATTENTION, no breadcrumb, no budget_weighted on the completed result.
    The shrunk heartbeat guarantees samples actually observe the 5M weighted
    spend (the `completed` scenario would finish before ever reporting usage,
    leaving the comparison untested)."""
    monkeypatch.setattr(opencode_http, "HEARTBEAT_INTERVAL_S", 0.05)
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher), policy=_budget_policy())
    spec = make_spec(
        tmp_path,
        rec,
        "big-usage-then-complete",
        timeout_s=30.0,
        token_budget=10**9,
        token_budget_mode="enforce",
        token_budget_grace_s=240.0,
    )

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.budget_weighted is None
    assert not (tmp_path / "run" / "ATTENTION").exists()
    lifecycle_path = tmp_path / "run" / "tasks" / "t-1" / "session-lifecycle.jsonl"
    if lifecycle_path.exists():
        lifecycle = read_jsonl(lifecycle_path)
        assert not [ln for ln in lifecycle if ln["event"] == "budget-tripped"]
    assert_server_gone(rec)


def test_e2e_budget_warn_trips_once_and_completes(tmp_path, fake_opencode, monkeypatch):
    """Warn mode across MULTIPLE heartbeat samples (interval shrunk to 0.05s
    while the fake stays busy ~0.5s): exactly one ATTENTION line and one
    budget-tripped breadcrumb (the trip latch), NO nudge, NO abort while the
    session ran — it completes naturally with budget_weighted on the result."""
    monkeypatch.setattr(opencode_http, "HEARTBEAT_INTERVAL_S", 0.05)
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher), policy=_budget_policy())
    spec = make_spec(
        tmp_path,
        rec,
        "big-usage-then-complete",
        timeout_s=30.0,
        token_budget=1_000_000,
        token_budget_mode="warn",
        token_budget_grace_s=240.0,
    )

    handle = adapter.start_session(spec)
    try:
        result = adapter.wait_for_completion(handle, spec)
        # no abort while the session ran (kill() below aborts at teardown)
        assert not (rec / "aborts.jsonl").exists()
    finally:
        adapter.kill(handle)

    assert result.status == "completed"
    assert result.result_json == {"ok": True, "workflow": "fake-triage"}
    assert result.budget_weighted == 5_000_000
    assert prompt_texts(rec) == ["Use the bmad-loop-sweep skill now: run it"]  # no nudge
    attention = (tmp_path / "run" / "ATTENTION").read_text(encoding="utf-8")
    assert len(attention.splitlines()) == 1
    lifecycle = read_jsonl(tmp_path / "run" / "tasks" / "t-1" / "session-lifecycle.jsonl")
    tripped = [ln for ln in lifecycle if ln["event"] == "budget-tripped"]
    assert len(tripped) == 1
    assert tripped[0]["mode"] == "warn"
    assert [ln for ln in lifecycle if ln["event"] == "over-budget-fired"] == []
    assert_server_gone(rec)


# Clock-driven budget unit tests: the _timeout_driven_session machinery plus a
# fake control client whose /message answer reports runaway usage (weighted 5M).


class _BigUsageClient:
    def __init__(self):
        self.posts: list[str] = []

    def get(self, path):
        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return [
                    {
                        "info": {
                            "role": "assistant",
                            "tokens": {"input": 4_000_000, "output": 1_000_000},
                        }
                    }
                ]

        return _Resp()

    def post(self, path):
        self.posts.append(path)

        class _Resp:
            status_code = 200

        return _Resp()

    def close(self):
        pass


def _budget_unit_spec(tmp_path, grace_s: float, timeout_s: float = 30_000.0) -> SessionSpec:
    return SessionSpec(
        task_id="t-1",
        role="dev",
        prompt="p",
        cwd=tmp_path,
        timeout_s=timeout_s,
        token_budget=1_000_000,
        token_budget_mode="enforce",
        token_budget_grace_s=grace_s,
    )


def test_budget_grace_fires_on_wall_clock_when_monotonic_frozen(tmp_path, monkeypatch):
    """The #157 suspend signature on the budget grace: time.monotonic() stands
    still through a host suspend, so the monotonic grace alone would stretch
    the wrap-up window by the nap's length. The wall co-bound fires anyway."""
    adapter = make_adapter(tmp_path, policy=_budget_policy())
    clock = _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)

    def advance():
        clock["wall"] += 11.0  # suspended host: wall counts on, monotonic frozen

    sess = _timeout_driven_session(adapter, advance)
    sess.client = _BigUsageClient()
    adapter.send_text = lambda handle, text: None  # nudge delivery not under test

    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"), _budget_unit_spec(tmp_path, grace_s=50.0)
    )

    assert result.status == "over_budget"
    assert result.budget_weighted == 5_000_000
    fired = [ln for ln in _lifecycle_lines(adapter) if ln["event"] == "over-budget-fired"]
    assert len(fired) == 1 and fired[0]["zero_grace"] is False


def test_budget_nudge_send_failure_still_arms_grace(tmp_path, monkeypatch):
    """A dead/hung server can reject the wrap-up nudge (the HTTP send raises);
    the trip must survive it and the grace still arm — the session is then
    scored via the normal paths (here: grace expiry → over_budget)."""
    adapter = make_adapter(tmp_path, policy=_budget_policy())
    clock = _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)

    def advance():
        clock["mono"] += 11.0

    sess = _timeout_driven_session(adapter, advance)
    sess.client = _BigUsageClient()

    def boom(handle, text):
        raise RuntimeError("http send failed")

    adapter.send_text = boom

    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"), _budget_unit_spec(tmp_path, grace_s=50.0)
    )

    assert result.status == "over_budget"
    assert result.budget_weighted == 5_000_000


def test_budget_zero_grace_dead_server_takes_crash_path(tmp_path, monkeypatch):
    """A trip coinciding with server death must not discard a landed artifact
    just because grace is 0: the zero-grace exit checks the process first and
    routes a dead server through the crash path, which honors the artifact."""
    adapter = make_adapter(tmp_path, policy=_budget_policy())
    _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)
    (adapter.tasks_dir / "t-1" / "result.json").write_text('{"ok": true}')

    sess = _timeout_driven_session(adapter, lambda: None)
    sess.client = _BigUsageClient()

    class _DeadProc:
        def poll(self):
            return 1

    sess.process = _DeadProc()

    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"), _budget_unit_spec(tmp_path, grace_s=0.0)
    )

    assert result.status == "completed"  # crash path honored the artifact
    assert result.result_json == {"ok": True}
    assert result.budget_weighted == 5_000_000


def test_budget_notify_failure_does_not_break_trip(tmp_path, monkeypatch):
    """observe-degrade: an ATTENTION append failure (disk full, perms) degrades
    to a missing notification; the trip and the over_budget verdict proceed."""
    from bmad_loop import gates as gates_mod

    adapter = make_adapter(tmp_path, policy=_budget_policy())
    _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(gates_mod, "notify", boom)
    sess = _timeout_driven_session(adapter, lambda: None)
    sess.client = _BigUsageClient()

    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"), _budget_unit_spec(tmp_path, grace_s=0.0)
    )

    assert result.status == "over_budget"
    assert result.budget_weighted == 5_000_000


# ---------------- timeout instrumentation + wall-clock co-bound (#157)
#
# Mirrors the generic-adapter coverage on the HTTP transport: the fire moment
# stamps the result and one timeout-fired line in session-lifecycle.jsonl, and
# a wall-clock co-bound fires through a frozen time.monotonic() (the macOS-sleep
# signature) but may never EXTEND the deadline. There is no watcher here — a
# tick is one sess.events.get(), so a fake queue advances the steerable clock
# each tick, exactly as _ScriptedWatcher.on_call does for the generic adapter.


def _install_clock(monkeypatch, mono=1000.0, wall=5000.0):
    clock = {"mono": mono, "wall": wall}

    class _Clock:
        monotonic = staticmethod(lambda: clock["mono"])
        time = staticmethod(lambda: clock["wall"])
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(opencode_http, "time", _Clock)
    return clock


def _timeout_driven_session(adapter, advance, task_id="t-1"):
    """Register a live session whose event queue never yields a frame but runs
    ``advance`` each tick, so only a clock crossing its deadline can end the
    wait. client=None makes _abort/_capture_usage no-ops."""

    class _AliveProc:
        def poll(self):
            return None

    class _TickingQueue:
        def get(self, timeout=None):
            advance()
            raise queue.Empty

        def empty(self):
            return True

    sess = _ServerSession(process=_AliveProc(), port=0, base_url="", password="", log_fh=None)
    sess.session_id = "ses_1"
    sess.client = None
    sess.events = _TickingQueue()
    adapter._sessions[task_id] = sess
    # There is no real server behind the fake process, so the atexit sweep must
    # not try to signal its (absent) pid or close its (None) log handle.
    adapter._teardown = lambda _sess: None
    return sess


def _lifecycle_lines(adapter, task_id="t-1"):
    path = adapter.tasks_dir / task_id / "session-lifecycle.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _timeout_spec(tmp_path, timeout_s=30.0) -> SessionSpec:
    return SessionSpec(task_id="t-1", role="dev", prompt="p", cwd=tmp_path, timeout_s=timeout_s)


def test_timeout_monotonic_expiry_is_instrumented(tmp_path, monkeypatch):
    """A plain monotonic expiry records WHEN and BY WHICH CLOCK the deadline was
    declared elapsed — result stamps, one timeout-fired lifecycle line, and a
    heartbeat.json topped up while the loop still ran."""
    adapter = make_adapter(tmp_path)
    clock = _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)  # start_session makes it in production

    def advance():
        clock["mono"] += 11.0  # wall frozen: only the monotonic clock expires

    _timeout_driven_session(adapter, advance)
    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"), _timeout_spec(tmp_path)
    )

    assert result.status == "timeout"
    assert result.timeout_expired_clock == "monotonic"
    assert result.timeout_fired_at == 5000.0  # the fake wall clock at fire time
    (fired,) = [ln for ln in _lifecycle_lines(adapter) if ln["event"] == "timeout-fired"]
    assert fired["expired_clock"] == "monotonic"
    assert fired["timeout_s"] == 30.0
    assert fired["mono_remaining_s"] <= 0
    hb = json.loads((adapter.tasks_dir / "t-1" / "heartbeat.json").read_text(encoding="utf-8"))
    assert hb["remaining_s"] == 30.0 and hb["stall_armed"] is False


def test_timeout_fires_on_wall_clock_when_monotonic_frozen(tmp_path, monkeypatch):
    """The #157 suspend signature on the HTTP transport: time.monotonic() stands
    still through a host suspend, so the monotonic deadline alone would stretch
    the session by the nap's length. The wall-clock co-bound fires anyway, and
    the wall-only expiry (monotonic time still to spare) is stamped as the
    evidence."""
    adapter = make_adapter(tmp_path)
    clock = _install_clock(monkeypatch)

    def advance():
        clock["wall"] += 11.0  # suspended host: wall counts on, monotonic frozen

    _timeout_driven_session(adapter, advance)
    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"), _timeout_spec(tmp_path)
    )

    assert result.status == "timeout"
    assert result.timeout_expired_clock == "wall"
    (fired,) = [ln for ln in _lifecycle_lines(adapter) if ln["event"] == "timeout-fired"]
    assert fired["expired_clock"] == "wall"
    assert fired["mono_remaining_s"] == 30.0  # the frozen clock never advanced


def test_timeout_wall_clock_step_back_cannot_extend_deadline(tmp_path, monkeypatch):
    """The co-bound may only EXPIRE the deadline, never stretch it: a wall clock
    stepped backward (an NTP correction) leaves the monotonic expiry on its
    original schedule."""
    adapter = make_adapter(tmp_path)
    clock = _install_clock(monkeypatch)
    ticks = {"n": 0}

    def advance():
        ticks["n"] += 1
        clock["mono"] += 11.0
        clock["wall"] -= 3600.0  # NTP step-back: must change nothing

    _timeout_driven_session(adapter, advance)
    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"), _timeout_spec(tmp_path)
    )

    assert result.status == "timeout"
    assert result.timeout_expired_clock == "monotonic"
    assert ticks["n"] == 3  # same tick count as an untouched wall clock


# ---------------------------- in-session hard-stop poll (#319)
#
# Contract parity: tests/test_generic_tmux.py carries the identically named pair
# over the tmux transport. `bmad-loop stop` lodges a mode-aware stop-request.json
# before it signals; the wait loop reads it twice per iteration and returns the
# non-completion `aborted` verdict, cancelling the in-flight HTTP turn exactly as
# the timeout arm does. The adapter never unlinks the file — the engine consumes
# it, and must still see it to attribute the stop.


class _AbortRecordingClient:
    """Minimal opencode HTTP client stand-in: records every POST path so a test
    can prove the abort really went out, and answers the usage GET
    `_capture_usage` makes on the way out."""

    def __init__(self):
        self.posts: list[str] = []

    def get(self, path):
        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return [{"info": {"role": "assistant", "tokens": {"input": 10, "output": 5}}}]

        return _Resp()

    def post(self, path):
        self.posts.append(path)

        class _Resp:
            status_code = 200

        return _Resp()

    def close(self):
        pass


def _lodge_stop_request(adapter, mode: str) -> Path:
    """Lodge a stop request of ``mode`` on this run's control-file channel, as
    ``bmad-loop stop`` does."""
    adapter.run_dir.mkdir(parents=True, exist_ok=True)
    path = adapter.run_dir / runs.STOP_REQUEST_FILE
    path.write_text(
        json.dumps({"requested_at": "2026-08-22T00:00:00", "mode": mode}), encoding="utf-8"
    )
    return path


def test_wait_aborts_on_hard_stop_request(tmp_path, monkeypatch):
    """A hard stop pending on the channel ends the wait on its very next
    iteration with the non-completion `aborted` verdict, and takes the timeout
    arm's exit shape: `_abort` cancels the in-flight turn, then `_capture_usage`
    reads usage back before teardown. Without the abort the HTTP turn would keep
    running server-side until the session is torn down.

    The verdict fires before the loop ever polls its event queue, so the
    steerable clock never advances: the pass is deterministic, not a race.

    Ablation: delete the `_hard_stop_requested()` arm from `wait_for_completion`
    and the clock runs the session to its scripted `timeout` verdict instead —
    proven red once, then restored.
    """
    adapter = make_adapter(tmp_path)
    clock = _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)
    request = _lodge_stop_request(adapter, "hard")

    ticks = {"n": 0}

    def advance():
        ticks["n"] += 1
        clock["mono"] += 11.0  # only reached if the abort arm is gone

    sess = _timeout_driven_session(adapter, advance)
    sess.client = _AbortRecordingClient()

    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"), _timeout_spec(tmp_path)
    )

    assert result.status == "aborted"
    assert result.result_json is None  # an abort is never a completion path
    assert ticks["n"] == 0  # aborted before the first event-queue poll
    assert sess.client.posts == ["/session/ses_1/abort"]
    assert result.transcript_path == str(adapter.tasks_dir / "t-1" / "messages.json")
    fired = [ln for ln in _lifecycle_lines(adapter) if ln["event"] == "stop-abort-fired"]
    assert len(fired) == 1
    # The engine consumes the request when it raises; an adapter that unlinked it
    # would leave the engine unable to attribute the stop.
    assert request.is_file()


def test_wait_polls_the_hard_stop_channel_after_the_event_queue_too(tmp_path, monkeypatch):
    """The arm at the top of the loop is not enough by itself. Below the event-queue
    wait sit the dispatch legs — `_probe_completion`'s two GETs (not throttled: once
    a turn goes quiet past SILENCE_THRESHOLD_S they run every tick), a
    `_session_status` GET, or `_result_json(wait=True)`'s grace wait — each bounded
    only by the client's own timeouts. One iteration can outlast `stop_run`'s 10s
    grace window, and on native Windows that is the force-kill this issue exists to
    avoid. A second poll straight after the queue wait leaves at most one leg between
    two checks.

    The request is lodged *inside* the queue poll, so it is absent at the top-of-loop
    check and present immediately after — the interval the second poll covers.

    This does not make the interval unconditionally short, and the prose no longer
    claims it does: an in-flight socket read cannot be interrupted from this thread.

    Ablation: delete the second `_hard_stop_requested()` arm (below the queue wait)
    -> `_probe_completion` is called, because the loop enters the silent-turn
    dispatch leg and only notices the request on its next iteration. The verdict
    stays `aborted` either way, which is why the probe spy carries the proof and the
    status does not."""
    adapter = make_adapter(tmp_path)
    clock = _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)
    adapter.silence_threshold_s = 0.0  # the quiet-turn leg fires on every tick

    probes: list[int] = []
    monkeypatch.setattr(
        type(adapter), "_probe_completion", lambda self, sess: (probes.append(1), False)[1]
    )

    lodged: list[str] = []

    def advance():
        if not lodged:  # absent at the top-of-loop check, present right after
            lodged.append("hard")
            _lodge_stop_request(adapter, "hard")
        clock["mono"] += 11.0  # makes the turn read as silent below the wait

    sess = _timeout_driven_session(adapter, advance)
    sess.client = _AbortRecordingClient()

    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"), _timeout_spec(tmp_path)
    )

    assert lodged == ["hard"]  # the interleave really happened
    assert result.status == "aborted"
    assert sess.client.posts == ["/session/ses_1/abort"]  # took the abort exit shape
    assert probes == []  # never entered the dispatch leg below the wait


def test_wait_ignores_graceful_stop_request(tmp_path, monkeypatch):
    """Graceful means *finish the in-flight item*, so a graceful request pending
    on the same channel must not touch a running session — only `hard` aborts.
    Every pre-#319 (modeless) body reads graceful, so this pins the back-compat
    case for the HTTP transport too.

    Ablation: widen the adapter's check to any pending request (drop the
    ``== "hard"`` comparison in `_hard_stop_requested`) and this test reddens
    with an `aborted` verdict.
    """
    adapter = make_adapter(tmp_path)
    clock = _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)
    _lodge_stop_request(adapter, "graceful")

    def advance():
        clock["mono"] += 11.0

    sess = _timeout_driven_session(adapter, advance)
    sess.client = _AbortRecordingClient()

    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"), _timeout_spec(tmp_path)
    )

    # the loop ran on to its scripted verdict rather than aborting
    assert result.status == "timeout"
    events = [ln["event"] for ln in _lifecycle_lines(adapter)]
    assert "stop-abort-fired" not in events
    assert events.count("timeout-fired") == 1


# -------------------------- launch-stall transport parity (#411/#470)
#
# Two-way contract-parity link: tests/test_generic_tmux.py carries identically
# named T1/T2 tests under its matching launch-stall header. Changes to the
# shared completion-loop behavior must update both transports or record the
# deliberate divergence; T3 pins OpenCode's HTTP busy/retry safety branch.


def test_dev_stall_arms_at_launch_without_stop(tmp_path, monkeypatch):
    """A silent dev/review turn is bounded even if no idle event ever arrives.

    INVERSE ablation: restore launch initialization of ``stall_deadline`` and
    ``last_activity`` to ``None`` and this test times out with zero stall nudges.
    """
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 2
    adapter.silence_threshold_s = float("inf")
    clock = _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)

    def advance():
        clock["mono"] += 11.0

    sess = _timeout_driven_session(adapter, advance)
    assert sess.activity == 0
    monkeypatch.setattr(adapter, "_session_status", lambda _sess: False)
    sent: list[str] = []
    monkeypatch.setattr(adapter, "send_text", lambda _handle, text: sent.append(text))
    heartbeats: list[dict] = []
    monkeypatch.setattr(
        adapter, "_write_heartbeat", lambda _task_id, payload: heartbeats.append(payload)
    )

    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"),
        _timeout_spec(tmp_path, timeout_s=100.0),
    )

    assert (result.status, sent) == ("stalled", [STALL_NUDGE_TEXT] * 2)
    assert heartbeats[0]["stall_armed"] is True


def test_dev_activity_rearms_launch_stall_grace(tmp_path, monkeypatch):
    """Two productive launch ticks re-arm grace; one full silent grace stalls.

    ``test_sse_dispatch_filters_child_sessions`` proves that the manually
    incremented counter is shared parent/child SSE activity, not an invented
    test seam. Ablation target: delete the activity-change re-arm branch and
    this test stalls at the first productive grace crossing instead of tick 3.
    """
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 0
    adapter.silence_threshold_s = float("inf")
    clock = _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)
    ticks = {"n": 0}
    sess: _ServerSession

    def advance_and_stream():
        ticks["n"] += 1
        clock["mono"] += 11.0
        if ticks["n"] <= 2:
            sess.activity += 1

    sess = _timeout_driven_session(adapter, advance_and_stream)
    monkeypatch.setattr(adapter, "_session_status", lambda _sess: False)
    sent: list[str] = []
    monkeypatch.setattr(adapter, "send_text", lambda _handle, text: sent.append(text))

    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"),
        _timeout_spec(tmp_path, timeout_s=100.0),
    )

    assert result.status == "stalled"
    assert (ticks["n"], sess.activity, sent) == (3, 2, [])


def test_dev_busy_status_rearms_launch_stall_grace_without_nudging(tmp_path, monkeypatch):
    """OpenCode busy/retry proof protects work after the nudge budget is spent.

    Ablation target: move the busy-status guard back under the positive nudge-
    budget branch and this test stalls instead of reaching the timeout.
    """
    adapter, _ = make_dev_adapter(tmp_path)
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 0
    adapter.silence_threshold_s = float("inf")
    clock = _install_clock(monkeypatch)
    (adapter.tasks_dir / "t-1").mkdir(parents=True)
    ticks = {"n": 0}

    def advance():
        ticks["n"] += 1
        clock["mono"] += 11.0

    sess = _timeout_driven_session(adapter, advance)
    statuses = iter(("busy", "retry", "busy", "retry"))
    observed: list[str] = []

    class _StatusClient:
        def get(self, path):
            if path == "/session/status":
                status = next(statuses)
                observed.append(status)
                payload = {sess.session_id: {"type": status}}
            else:
                payload = []

            class _Resp:
                status_code = 200

                @staticmethod
                def json():
                    return payload

            return _Resp()

        def post(self, path):
            class _Resp:
                status_code = 200

            return _Resp()

        def close(self):
            pass

    sess.client = _StatusClient()
    sent: list[str] = []
    monkeypatch.setattr(adapter, "send_text", lambda _handle, text: sent.append(text))

    result = adapter.wait_for_completion(
        SessionHandle(task_id="t-1", native_id="ses_1"),
        _timeout_spec(tmp_path, timeout_s=35.0),
    )

    assert result.status == "timeout"
    assert (observed, sent, ticks["n"]) == (["busy", "retry", "busy", "retry"], [], 4)


def test_e2e_server_death_with_artifact_completes(tmp_path, fake_opencode):
    """Server death ≙ window death: the crash path vouches for a landed
    result.json (accept_result=True), so finished-then-died reads completed."""
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "die-after-result")

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json == {"ok": True, "workflow": "fake-triage"}
    assert_server_gone(rec)


def test_e2e_server_death_without_artifact_crashes(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "die-no-result")

    result = adapter.run(spec)

    assert result.status == "crashed"
    assert result.result_json is None
    assert_server_gone(rec)


def test_e2e_sse_loss_degrades_to_poll_fallback(tmp_path, fake_opencode):
    """The stream closes after every connect, so session.idle is never
    deliverable; completion must arrive via GET /session/status + the
    message-level proof-of-work — in epoch-ms units (the regression test for
    the ns-vs-ms floor bug, which silently disables this whole path)."""
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "sse-black-hole")

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json == {"ok": True, "workflow": "fake-triage"}
    assert_server_gone(rec)


def test_e2e_spawn_retry_survives_one_early_death(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "completed", extra_env={"FAKE_OPENCODE_START_FAILURES": "1"})

    result = adapter.run(spec)

    assert result.status == "completed"
    assert int((rec / "start-count").read_text(encoding="utf-8")) == 2
    assert_server_gone(rec)


def test_e2e_spawn_gives_up_after_attempts(tmp_path, fake_opencode):
    """The give-up error has to point at the file that actually holds the
    diagnostics. Server stdout is redirected to <task>.server.out, so naming
    <task>.log — which now carries only the SSE-rendered transcript, and is
    EMPTY when the server never got far enough to stream anything — would send
    an operator to a blank file on every failed spawn."""
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "completed", extra_env={"FAKE_OPENCODE_START_FAILURES": "99"})
    logs = tmp_path / "run" / "logs"

    with pytest.raises(OpencodeServerError, match="after 3 attempts") as excinfo:
        adapter.run(spec)

    assert adapter._sessions == {}
    message = str(excinfo.value)
    assert str(logs / "t-1.server.out") in message
    assert str(logs / "t-1.log") not in message
    # and the path it names holds the goods: every attempt appended to one
    # server log, so all 3 spawns' stdout is there to read
    server_out = (logs / "t-1.server.out").read_text(encoding="utf-8")
    assert server_out.count("FAKE_STDOUT_CANARY") == 3
    # the transcript is the file that would have been useless here
    assert (logs / "t-1.log").read_bytes() == b""


def test_spawn_open_failure_closes_the_sinks_already_open(tmp_path, fake_opencode, monkeypatch):
    """A failing sink `open()` must not strand the sinks opened before it.

    All three sinks open ABOVE the retry loop's try/except, so an `open()` that
    fails partway — ENOSPC, EMFILE, a permission race — propagates straight out
    of `_spawn_server` without ever reaching the loop's handler. Only the guard
    around the opens themselves can close what was already handed out; without
    it those handles stay open for as long as the propagating traceback keeps
    this frame alive. One sink had nothing to leak here; three do.
    """
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(tmp_path, rec, "completed")
    opened: dict = {}
    real_open = Path.open

    def flaky_open(self, *args, **kwargs):
        # the LAST of the three sinks to open — so .log and .sse.jsonl are both
        # already live when it blows up
        if self.name.endswith(".server.out"):
            raise OSError(28, "No space left on device")
        fh = real_open(self, *args, **kwargs)
        opened[self.name] = fh
        return fh

    monkeypatch.setattr(Path, "open", flaky_open)

    with pytest.raises(OSError, match="No space left"):
        adapter._spawn_server(spec)

    # both earlier sinks were opened, and both were closed on the way out
    assert set(opened) >= {"t-1.log", "t-1.sse.jsonl"}, sorted(opened)
    still_open = [name for name, fh in opened.items() if not fh.closed]
    assert still_open == [], f"leaked sink handles: {still_open}"


def test_close_spawn_sinks_survives_a_failing_close(tmp_path):
    """A close that raises must not displace the failure being reported.

    Both callers are already reporting something worse — one is mid-`raise`, the
    other is about to raise `OpencodeServerError` — so an OSError from a
    flush-on-close would replace the real diagnosis. It must also not strand the
    sinks after it in the loop.
    """
    adapter = make_adapter(tmp_path)
    closed = []

    class _Fh:
        def __init__(self, name, boom=False):
            self.name, self._boom = name, boom

        def close(self):
            closed.append(self.name)
            if self._boom:
                raise OSError(5, "Input/output error")

    # the FIRST sink fails to close: an unguarded loop would propagate here and
    # never reach the other two
    adapter._close_spawn_sinks(_Fh("log", boom=True), _Fh("server"), _Fh("event"))

    assert closed == ["log", "server", "event"]
    # and None (sse_trace off, or the sink whose open was the failure) is skipped
    adapter._close_spawn_sinks(_Fh("log2"), None, None)
    assert closed[-1] == "log2"


# --------------------------------------------- OpencodeDevAdapter (Phase 4)
#
# Dev/review sessions run the generic bmad-dev-auto skill, which writes NO
# result.json: the outcome lives in the terminal spec it leaves on disk,
# synthesized via devcontract by _DevSynthesisMixin — the same machinery
# GenericDevAdapter uses, composed over the HTTP transport.

_DONE_SPEC = (
    "---\nstatus: done\nbaseline_revision: abc123\n---\n\n"
    "## Auto Run Result\n\nStatus: done\nImplemented.\n"
)


def make_dev_adapter(
    tmp_path: Path, binary: str = "opencode", **kwargs
) -> tuple[OpencodeDevAdapter, Path]:
    impl = tmp_path / "impl"
    impl.mkdir(exist_ok=True)
    # project root == tmp_path so rebased(spec.cwd=tmp_path) is a no-op: these
    # sessions run in place, where cwd == the project root.
    paths = ProjectPaths(
        project=tmp_path,
        implementation_artifacts=impl,
        planning_artifacts=tmp_path / "plan",
    )
    adapter = OpencodeDevAdapter(
        run_dir=tmp_path / "run",
        policy=kwargs.pop("policy", _policy()),
        profile=get_profile("opencode"),
        binary=binary,
        paths=paths,
        **kwargs,
    )
    return _shrink_timing(adapter), impl


def make_dev_spec(
    tmp_path: Path,
    rec: Path,
    scenario: str,
    spec_path: Path,
    spec_text: str = _DONE_SPEC,
    story_key: str = "3-1",
    task_id: str = "3-1-dev-1",
    timeout_s: float = 30.0,
    extra_env: dict | None = None,
) -> SessionSpec:
    env = {
        "FAKE_OPENCODE_SCENARIO": scenario,
        "FAKE_OPENCODE_DIR": str(rec),
        # Deliberately NO FAKE_OPENCODE_RESULT_PATH: the dev skill writes no
        # result.json, and the dev adapter must never lean on one.
        "FAKE_OPENCODE_SPEC_PATH": str(spec_path),
        "FAKE_OPENCODE_SPEC_TEXT": spec_text,
        "BMAD_LOOP_STORY_KEY": story_key,
        **(extra_env or {}),
    }
    return SessionSpec(
        task_id=task_id,
        role="dev",
        prompt=f"/bmad-dev-auto {story_key}",
        cwd=tmp_path,
        env=env,
        timeout_s=timeout_s,
        stall_nudges_cap=6,
    )


def test_dev_knobs_configured(tmp_path):
    """_configure_dev_knobs over the HTTP knob names: no result-contract stop
    nudges (the skill writes no result.json), stall grace + wake budget from
    policy — same contract as GenericDevAdapter."""
    adapter, _ = make_dev_adapter(
        tmp_path, policy=_policy(dev_stall_grace_s=123, dev_stall_nudges=4)
    )
    assert adapter._stop_nudges == 0
    assert adapter._stall_grace_s == 123.0
    assert adapter._stall_nudges == 4


def test_dev_probe_alive_never_none(tmp_path):
    """The post-kill liveness seam: poll() on the retained Popen handle is
    always answerable (True/False), never the tri-state unknown a tmux probe
    can hit — and a task with no retained process owns nothing alive."""

    class _Proc:
        def __init__(self, rc):
            self._rc = rc

        def poll(self):
            return self._rc

    adapter, _ = make_dev_adapter(tmp_path)
    handle = SessionHandle(task_id="t", native_id="ses_x")
    assert adapter._probe_alive(handle) is False  # never spawned
    adapter._server_procs["t"] = _Proc(None)
    assert adapter._probe_alive(handle) is True  # kill silently failed: keep verdict
    adapter._server_procs["t"] = _Proc(0)
    assert adapter._probe_alive(handle) is False


def test_dev_result_json_ignores_result_file(tmp_path):
    """MRO pin: _DevSynthesisMixin's spec synthesis shadows the core adapter's
    result.json read-back — a stray result.json with no spec on disk is not a
    dev result."""
    adapter, _ = make_dev_adapter(tmp_path)
    task_dir = adapter.tasks_dir / "3-1-dev-1"
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text('{"ok": true}', encoding="utf-8")
    spec = SessionSpec(
        task_id="3-1-dev-1",
        role="dev",
        prompt="/bmad-dev-auto 3-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_STORY_KEY": "3-1"},
    )
    handle = SessionHandle(task_id="3-1-dev-1", native_id="ses_x")
    assert adapter._result_json(handle, spec, wait=False) is None


def test_e2e_dev_synthesizes_terminal_spec(tmp_path, fake_opencode):
    launcher, rec = fake_opencode
    adapter, impl = make_dev_adapter(tmp_path, binary=str(launcher))
    spec = make_dev_spec(tmp_path, rec, "completed", impl / "spec-3-1-foo.md")

    result = adapter.run(spec)

    assert result.status == "completed"
    rj = result.result_json
    assert rj["workflow"] == "auto-dev"
    assert rj["status"] == "done"
    assert rj["baseline_commit"] == "abc123"  # mapped from baseline_revision
    assert rj["story_key"] == "3-1"
    assert rj["escalations"] == []
    assert "post_kill_reconciled" not in rj  # vouched by the idle, not rescued
    # transport parity with the classic path: usage, template, teardown
    assert adapter.read_usage(result) == TokenUsage(
        input_tokens=100, output_tokens=55, cache_read_tokens=7, cache_creation_tokens=3
    )
    assert prompt_texts(rec) == ["Use the bmad-dev-auto skill now: 3-1"]
    assert adapter._sessions == {}
    assert_server_gone(rec)


def test_e2e_dev_retained_park_marker_is_not_reowned_by_unrelated_rewrite(tmp_path, fake_opencode):
    """The HTTP transport also captures marker provenance before prompting."""
    launcher, rec = fake_opencode
    adapter, impl = make_dev_adapter(tmp_path, binary=str(launcher))
    spec_path = impl / "spec-3-1-foo.md"
    parked = (
        "---\nstatus: awaiting-operator\nbaseline_revision: abc123\n"
        "operator_actions:\n  - publish the TXT record\n---\n\n# Story\n\n"
        "## Auto Run Result\n\nStatus: awaiting-operator\nParked.\n"
    )
    spec_path.write_text(parked)
    spec = make_dev_spec(
        tmp_path,
        rec,
        "completed",
        spec_path,
        spec_text=parked.replace("# Story", "# Story\n\nUnrelated session edit."),
    )

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json["status"] == "awaiting-operator"
    assert result.result_json["park_asserted"] is False
    assert_server_gone(rec)


def test_e2e_dev_stories_mode_resolves_by_id(tmp_path, fake_opencode, monkeypatch):
    """Folder+id dispatch (BMAD_LOOP_SPEC_FOLDER): the story spec is resolved
    at its deterministic id-keyed path — never via the mtime scan."""
    launcher, rec = fake_opencode
    adapter, impl = make_dev_adapter(tmp_path, binary=str(launcher))

    def boom(*a, **k):
        raise AssertionError("stories mode must not call the mtime scan")

    monkeypatch.setattr(generic.devcontract, "find_result_artifact", boom)
    spec = make_dev_spec(
        tmp_path,
        rec,
        "completed",
        tmp_path / "epic" / "stories" / "1-foo.md",
        story_key="1",
        task_id="1-dev-1",
        extra_env={"BMAD_LOOP_SPEC_FOLDER": "epic"},
    )

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["story_key"] == "1"
    assert_server_gone(rec)


def test_e2e_dev_post_kill_rescue(tmp_path, fake_opencode):
    """#61 over HTTP: the turn wrote its terminal spec but its idle was never
    seen (the fake stays busy forever), so the loop times out — a verdict
    reached under a live server, where the artifact is advisory (#48/#53).
    run()'s kill settles the liveness question; _post_kill_reconcile re-probes
    the now-dead server process and rescues the self-consistent done spec."""
    launcher, rec = fake_opencode
    adapter, impl = make_dev_adapter(tmp_path, binary=str(launcher))
    spec = make_dev_spec(tmp_path, rec, "busy-forever", impl / "spec-3-1-foo.md", timeout_s=1.5)

    result = adapter.run(spec)

    assert result.status == "completed"
    assert result.result_json["status"] == "done"
    assert result.result_json["post_kill_reconciled"] is True
    assert_server_gone(rec)


def test_e2e_dev_wait_loop_drives_observe_tick(tmp_path, fake_opencode, monkeypatch):
    """Cross-adapter parity (#276 M2): the OpenCode wait loop invokes _observe_tick
    from its heartbeat-throttled block, exactly as the generic adapter does. The
    first tick always fires (last_heartbeat is None), so any run drives the hook —
    with this session's handle and the very spec that was dispatched. OpencodeDev-
    Adapter shares _DevSynthesisMixin, so the observation seam is live over HTTP."""
    launcher, rec = fake_opencode
    adapter, impl = make_dev_adapter(tmp_path, binary=str(launcher))
    spec = make_dev_spec(tmp_path, rec, "completed", impl / "spec-3-1-foo.md")

    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        adapter, "_observe_tick", lambda handle, s: seen.append((handle.task_id, s is spec))
    )

    result = adapter.run(spec)

    assert result.status == "completed"
    assert seen and all(tid == "3-1-dev-1" and same for tid, same in seen)
    assert_server_gone(rec)


# --------------------------------------------------------------- env fault (#194)
# The HTTP adapter went without env-fault classification entirely while the tmux
# adapter had it, because the hook was implemented on GenericAdapter rather than
# shared — so a 5-hour provider quota outage read as three healthy stories timing
# out and burned their retry budgets. The classifier now lives in EnvFaultMixin;
# these tests assert this adapter actually inherits it, which is the regression
# that matters (the mechanics themselves are covered in test_generic_tmux.py).
#
# This adapter's log is the `opencode serve` process's own stdout/stderr, not a
# tmux pane capture, so the lines below keep the server's logfmt SHAPE and the
# provider's verbatim AI_APICallError text (that text is what the patterns key
# on) — but every session/run identifier is a synthetic stand-in, not the real
# one from the outage, which came from a private client project's run.
_EF_TASK = "17-1b-dev-1"

_QUOTA_LINE = (
    'timestamp=2026-07-26T13:12:53.262Z level=ERROR run=fake0001 message="stream error" '
    "providerID=zai-coding-plan modelID=glm-5.2 session.id=ses_fake0000000000000000000001 "
    'small=false agent=general mode=subagent error.error="AI_APICallError: Usage limit '
    'reached for 5 hour. Your limit will reset at 2026-07-26 22:49:46"'
)
_SOCKET_LINE = (
    'timestamp=2026-07-26T22:52:57.193Z level=ERROR run=fake0002 message="stream error" '
    'providerID=zai-coding-plan modelID=glm-5.2 error.error="AI_APICallError: Cannot '
    'connect to API: The socket connection was closed unexpectedly."'
)


def _ef_classify(adapter, status, *, result_json=None, task_id=_EF_TASK):
    handle = SessionHandle(task_id=task_id, native_id=task_id)
    spec = SessionSpec(task_id=task_id, role="dev", prompt="/x", cwd=adapter.run_dir)
    return adapter._classify_env_fault(
        handle, spec, SessionResult(status=status, result_json=result_json)
    )


def _write_ef_log(adapter, text: str, task_id: str = _EF_TASK) -> None:
    """Writes to whatever file the adapter actually scans, not a hardcoded name.

    These unit tests are deliberately blind to WHICH file that is — asserting the
    suffix here would just restate the implementation. Pinning the wiring is
    test_e2e_env_fault_classified_through_run's job, and it is the only thing that
    caught the scan pointing at the wrong file after <task_id>.log became the
    conversation transcript."""
    adapter._env_fault_log_path(task_id).write_text(text, encoding="utf-8")


@pytest.mark.parametrize("line", [_QUOTA_LINE, _SOCKET_LINE])
def test_opencode_classifies_provider_quota_as_env_fault(tmp_path, line):
    """The headline regression: a timed-out session whose server log carries the
    provider's refusal is an environment fault, not a failed story attempt."""
    adapter = make_adapter(tmp_path)
    _write_ef_log(adapter, f"listening on 127.0.0.1\n{line}\ncleanup prune=7.days\n")
    result = _ef_classify(adapter, "timeout")
    assert result.env_fault is True
    assert result.status == "timeout"  # the verdict string is unchanged
    assert "AI_APICallError" in result.env_fault_evidence


def test_opencode_env_fault_writes_lifecycle_breadcrumb(tmp_path):
    adapter = make_adapter(tmp_path)
    _write_ef_log(adapter, _QUOTA_LINE + "\n")
    _ef_classify(adapter, "timeout")
    events = [
        json.loads(line)
        for line in (adapter.tasks_dir / _EF_TASK / "session-lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [e["event"] for e in events] == ["env-fault-classified"]
    assert events[0]["status"] == "timeout"
    assert "Usage limit reached" in events[0]["evidence"]


def test_opencode_healthy_timeout_is_not_an_env_fault(tmp_path):
    """The other half: an ordinary timeout whose log holds only normal server
    chatter stays a real dev attempt. Classifying this would let a genuinely
    stuck story pause the run forever instead of retrying."""
    adapter = make_adapter(tmp_path)
    _write_ef_log(
        adapter,
        "timestamp=2026-07-26T15:45:39.732Z level=INFO message=stream "
        "providerID=zai-coding-plan modelID=glm-5.2\ncleanup prune=7.days\n",
    )
    result = _ef_classify(adapter, "timeout")
    assert result.env_fault is False
    assert result.env_fault_evidence is None


def test_opencode_env_fault_skips_completed_and_result_bearing(tmp_path):
    """`completed` never reaches the scan, and a result-bearing verdict is a
    session that did real work — a reconcile upgrade must not be re-classified."""
    adapter = make_adapter(tmp_path)
    _write_ef_log(adapter, _QUOTA_LINE + "\n")
    assert _ef_classify(adapter, "completed").env_fault is False
    assert _ef_classify(adapter, "timeout", result_json={"status": "done"}).env_fault is False
    assert not (adapter.tasks_dir / _EF_TASK / "session-lifecycle.jsonl").exists()


def test_opencode_env_fault_missing_log_degrades_silently(tmp_path):
    """Best-effort doctrine: an unreadable log leaves the verdict untouched."""
    adapter = make_adapter(tmp_path)
    result = _ef_classify(adapter, "timeout", task_id="never-ran")
    assert result.env_fault is False


def test_e2e_env_fault_classified_through_run(tmp_path, fake_opencode):
    """The wiring, not just the inheritance: every test above calls
    _classify_env_fault directly, and all of them would still pass if
    CodingCLIAdapter.run() never invoked the hook for this adapter — which is
    exactly the regression (#194 went unclassified here for that reason).

    So drive a real session end to end: the fake server logs the provider's
    refusal to its own stdout (logs/<task_id>.server.out — NOT <task_id>.log, the
    curated conversation transcript) at the top of a turn that then never
    finishes, the session idles out its clock, and the SessionResult that comes
    back out of run() must carry the classification."""
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    spec = make_spec(
        tmp_path,
        rec,
        "busy-forever",
        timeout_s=1.5,
        extra_env={"FAKE_OPENCODE_LOG_LINE": _QUOTA_LINE},
    )

    result = adapter.run(spec)

    assert result.status == "timeout"  # the verdict string is unchanged
    assert result.result_json is None
    assert result.env_fault is True
    assert "Usage limit reached" in result.env_fault_evidence
    # The signal came off the SERVER log specifically. Named literally rather than
    # via adapter._env_fault_log_path, because this assertion exists to catch the
    # scan being pointed at the wrong file — resolving the path through the code
    # under test would make it agree with any answer. <task_id>.log is the curated
    # conversation transcript and must NOT be the source here.
    logs = tmp_path / "run" / "logs"
    assert "AI_APICallError" in (logs / "t-1.server.out").read_text(encoding="utf-8")
    assert "AI_APICallError" not in (logs / "t-1.log").read_text(encoding="utf-8")
    events = read_jsonl(tmp_path / "run" / "tasks" / "t-1" / "session-lifecycle.jsonl")
    assert [e["event"] for e in events][-1:] == ["env-fault-classified"]
    assert_server_gone(rec)


def test_env_fault_log_is_dropped_at_session_start(tmp_path, fake_opencode):
    """A re-armed run reusing a task_id must not classify off the PREVIOUS cycle's
    provider error.

    This is the pause loop the classifier would otherwise create for itself: an
    env fault PAUSEs the run, the operator re-arms and resumes, and the next
    session — however healthy its own log — rescans the stale refusal and pauses
    again, forever. GenericAdapter.start_session unlinks its pane tee for exactly
    this reason; the server sink needs the same treatment.

    Drives a real session so the unlink is exercised through the actual spawn
    path, not asserted against a hand-placed file."""
    launcher, rec = fake_opencode
    adapter = make_adapter(tmp_path, binary=str(launcher))
    stale = adapter._env_fault_log_path("t-1")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(_QUOTA_LINE + "\n", encoding="utf-8")

    # This cycle times out with the provider saying NOTHING (no FAKE_OPENCODE_LOG_LINE),
    # so it is eligible for classification and the only quota line anywhere is the
    # stale one. Deliberately a non-completed verdict: `completed` short-circuits
    # the classifier before it reads a byte, so it would pass with or without the
    # unlink and prove nothing.
    spec = make_spec(tmp_path, rec, "busy-forever", timeout_s=1.5)
    result = adapter.run(spec)

    assert result.status == "timeout"
    assert result.env_fault is False, f"classified off a stale log: {result.env_fault_evidence}"
    assert "Usage limit reached" not in stale.read_text(encoding="utf-8", errors="replace")
