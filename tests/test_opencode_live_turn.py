"""Live full-turn gate: one real OpenCode 2.x turn through ``OpencodeHttpAdapter.run()``.

RELAXED ZERO-TOKEN INVARIANT: unlike ``test_opencode_live.py`` (which never
prompts), this module DOES send a prompt — but every LLM request it causes goes
to a stdlib fake OpenAI-compatible server on 127.0.0.1 inside this process, so
it spends zero real tokens. Three layers make a real provider unreachable, and
the test asserts the outcome rather than trusting them:

- the server's env is scrubbed to PATH, LANG and a scratch HOME / XDG base, so
  no provider credential, user config or default model leaks in (a real shell
  env once leaked a hosted default model into a probe);
- the project's ``opencode.json`` declares only the ``mock`` provider, the
  session's model is ``mock/mock-1``, and a ``provider.use`` policy denies the
  hosted ``opencode`` provider — 2.x's credential-free fallback when no model
  is usable;
- the test asserts the fake received exactly one request per assistant step,
  and every step in the transcript names the ``mock`` provider.

The fake scripts the turn: its first request answers with one ``write`` tool
call that writes the task's ``result.json``, the next with a closing text
reply, each with fixed usage — so the session's token aggregate is known
exactly.

V2-only (the opencode on PATH must report 2.x) and POSIX-only, like the no-prompt
module: opencode-on-Windows is unverified for this adapter.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import opencode_runs

from bmad_loop.adapters.opencode_protocol import parse_opencode_version


def _path_opencode_is_v2() -> bool:
    """Whether the runnable `opencode` on PATH reports a 2.x version."""
    if not opencode_runs():
        return False
    binary = shutil.which("opencode")
    assert binary is not None
    probe = subprocess.run(
        [binary, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    version = parse_opencode_version(probe.stdout)
    return version is not None and version[0] == 2


HAVE_OPENCODE_V2 = _path_opencode_is_v2()
pytestmark = pytest.mark.skipif(
    not HAVE_OPENCODE_V2,
    reason="live turn needs a runnable OpenCode 2.x `opencode` on PATH (POSIX)",
)

httpx = pytest.importorskip("httpx")

from bmad_loop.adapters.base import SessionSpec  # noqa: E402
from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter  # noqa: E402
from bmad_loop.adapters.profile import get_profile  # noqa: E402
from bmad_loop.model import TokenUsage  # noqa: E402
from bmad_loop.policy import LimitsPolicy, Policy  # noqa: E402

TASK_ID = "live-turn"
RESULT = {"status": "ok", "summary": "written by the fake LLM's write call"}
# Fixed usage per scripted reply — the session aggregate is their sum.
TOOL_USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
TEXT_USAGE = {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18}


class FakeLLM:
    """A stdlib OpenAI-compatible chat-completions server on 127.0.0.1.

    Every POST is recorded (path + parsed body) and answered as a streamed
    ``chat.completion.chunk`` SSE: the first request that offers tools gets one
    ``write`` call (``{"path", "content"}`` — the 2.x tool's argument shape),
    every later request a short text reply. ``GET`` answers the model list."""

    def __init__(self, write_path: Path, write_content: str) -> None:
        self.requests: list[dict] = []
        self._tooled = False
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep pytest output clean
                pass

            def do_GET(self) -> None:
                body = json.dumps(
                    {"object": "list", "data": [{"id": "mock-1", "object": "model"}]}
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                req = json.loads(self.rfile.read(length) or b"{}")
                with fake._lock:
                    fake.requests.append({"path": self.path, "body": req})
                    tool_turn = bool(req.get("tools")) and not fake._tooled
                    fake._tooled = fake._tooled or tool_turn
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                if tool_turn:
                    call = {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps(
                                {"path": str(write_path), "content": write_content}
                            ),
                        },
                    }
                    self._chunk({"role": "assistant", "tool_calls": [call]})
                    self._chunk({}, "tool_calls", TOOL_USAGE)
                else:
                    self._chunk({"role": "assistant", "content": "Result written."})
                    self._chunk({}, "stop", TEXT_USAGE)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def _chunk(self, delta: dict, finish: str | None = None, usage=None) -> None:
                chunk: dict = {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "mock-1",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                if usage:
                    chunk["usage"] = usage
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeLLM:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def scrubbed_env(tmp_path, monkeypatch):
    """os.environ reduced to PATH, LANG and a scratch HOME / XDG base — the
    adapter spawns the server with this env, so nothing outside it (provider
    keys, cloud credentials, the operator's opencode config) can reach it."""
    home = tmp_path / "home"
    keep = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in keep.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOME", str(home))
    for var, rel in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        (home / rel).mkdir(parents=True)
        monkeypatch.setenv(var, str(home / rel))
    return home


def _mock_only_project(cwd: Path, llm_port: int) -> None:
    """The project config: the `mock` provider pointed at the fake, and the
    hosted `opencode` provider denied — the only providers a session could
    reach are the ones declared here."""
    config = {
        "providers": {
            "mock": {
                "name": "Mock",
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": f"http://127.0.0.1:{llm_port}/v1", "apiKey": "x"},
                "models": {"mock-1": {"name": "Mock 1"}},
            }
        },
        "experimental": {
            "policies": [{"action": "provider.use", "resource": "opencode", "effect": "deny"}]
        },
    }
    (cwd / "opencode.json").write_text(json.dumps(config), encoding="utf-8")


def test_v2_real_turn_through_mock_provider(tmp_path, scrubbed_env):
    cwd = tmp_path / "project"
    cwd.mkdir()
    run_dir = tmp_path / "run"
    result_path = run_dir / "tasks" / TASK_ID / "result.json"

    adapter = OpencodeHttpAdapter(
        run_dir=run_dir,
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("opencode"),
    )
    spawned = []
    real_spawn = adapter._spawn_server

    def recording_spawn(spec):
        sess = real_spawn(spec)
        spawned.append(sess)
        return sess

    adapter._spawn_server = recording_spawn  # type: ignore[method-assign]

    with FakeLLM(result_path, json.dumps(RESULT)) as llm:
        _mock_only_project(cwd, llm.port)
        spec = SessionSpec(
            task_id=TASK_ID,
            role="triage",
            prompt="Write the task result file, then reply.",
            cwd=cwd,
            model="mock/mock-1",
            timeout_s=180.0,
        )
        result = adapter.run(spec)

    # The turn completed on the skill-written result the tool call landed.
    assert result.status == "completed", result
    assert result.result_json == RESULT
    assert json.loads(result_path.read_text(encoding="utf-8")) == RESULT

    # Usage is the session aggregate of the fake's two scripted replies.
    assert adapter.read_usage(result) == TokenUsage(
        input_tokens=TOOL_USAGE["prompt_tokens"] + TEXT_USAGE["prompt_tokens"],
        output_tokens=TOOL_USAGE["completion_tokens"] + TEXT_USAGE["completion_tokens"],
    )

    # Every LLM request hit the fake: one chat-completions request per
    # assistant step, and every step ran on the mock provider.
    assert [r["path"] for r in llm.requests] == ["/v1/chat/completions"] * 2
    transcript = json.loads((run_dir / "tasks" / TASK_ID / "messages.json").read_text("utf-8"))
    steps = [m for m in transcript if m.get("type") == "assistant"]
    assert len(steps) == len(llm.requests)
    assert {(m["model"]["providerID"], m["model"]["id"]) for m in steps} == {("mock", "mock-1")}

    # Unattended config reached the model: no `question` tool to park on.
    tools = {t["function"]["name"] for t in llm.requests[0]["body"]["tools"]}
    assert "write" in tools
    assert "question" not in tools

    # The run's observability sinks exist and carry the turn.
    logs = run_dir / "logs"
    log_text = (logs / f"{TASK_ID}.log").read_text(encoding="utf-8")
    assert "Write the task result file" in log_text
    trace = [
        json.loads(line) for line in (logs / f"{TASK_ID}.sse.jsonl").read_text("utf-8").splitlines()
    ]
    assert "session.execution.succeeded" in {r["type"] for r in trace}
    assert (logs / f"{TASK_ID}.server.out").is_file()
    assert (logs / f"{TASK_ID}.provider-errors.log").read_text(encoding="utf-8") == ""

    # No orphan: the one server this run spawned is reaped and its port closed.
    assert len(spawned) == 1
    sess = spawned[0]
    assert sess.process.poll() is not None
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", sess.port), timeout=1.0)
