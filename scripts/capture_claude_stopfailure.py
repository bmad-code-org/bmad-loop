#!/usr/bin/env python3
"""Capture Claude Code's real ``StopFailure`` hook payloads and pane bytes, zero tokens.

claude.toml's ``[hooks.stop_failure_types]`` table and its ``env_fault_patterns``
must be seeded from captured output, never from a plausible string. This harness
is that capture, reproducibly: it runs the REAL ``claude`` binary in a private
tmux server against an in-process fake Anthropic API that answers every message
request with one scripted failure, and records what the CLI does with it:

* ``<scenario>.stopfailure.json`` — the ``StopFailure`` hook payload, as the
  project hook received it on stdin;
* ``<scenario>.pipe-pane.log`` — the raw ``tmux pipe-pane`` bytes of the session,
  which is exactly what bmad-loop's env-fault tail scan reads;
* ``manifest.json`` — claude + tmux versions, capture date, and per scenario the
  response the fake server sent and every request it received.

Safety — no real API is ever contacted, and no token is spent:

* the session runs with a fake API key, ``ANTHROPIC_BASE_URL`` pointing at the
  loopback fake server, a fresh temporary ``CLAUDE_CONFIG_DIR`` and
  ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1``; every inherited ``ANTHROPIC_*``
  / ``CLAUDE*`` variable, the Bedrock/Vertex switches and the proxy variables
  are dropped from its environment first;
* the session also gets claude.toml's own ``[env]`` (the classic inline
  renderer, as a bmad-loop session runs), so the pane bytes are what the
  orchestrator's tail scan would read;
* a scenario's files are written only when the fake server recorded at least one
  message request and nothing else — the run refuses otherwise.

``CLAUDE_CODE_MAX_RETRIES=0`` makes the FIRST failure surface; a real session
retries first, so its pane can hold retry lines before the final error.

Redaction — the pane logs are the captured bytes verbatim, and the payload
files are the hook payload re-serialized as JSON (values and key order
preserved), each with exactly two substitutions: the temporary capture root
(its path, Claude Code's ``-``-slug of it, and the ``/…/`` elided form the
welcome banner prints) becomes ``<capture-root>``, and the fake server's port
after ``127.0.0.1:`` becomes ``<port>``. A scenario whose output still carries
either is refused rather than written.

POSIX only (tmux). Usage::

    uv run python scripts/capture_claude_stopfailure.py --out tests/fixtures/claude-stopfailure
    uv run python scripts/capture_claude_stopfailure.py --out /tmp/x --scenario auth-401
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
CLAUDE_PROFILE = REPO / "src" / "bmad_loop" / "data" / "profiles" / "claude.toml"

# A syntactically plausible key that authenticates nowhere. Claude Code asks
# before using an external key; `.claude.json` pre-approves its last 20 chars.
FAKE_KEY = "sk-ant-api03-" + "fakekey" * 6 + "-AAAAAAAA"
PROMPT = "say hi"
ROOT_PLACEHOLDER = "<capture-root>"
PORT_PLACEHOLDER = "<port>"
PANE_WIDTH, PANE_HEIGHT = 120, 40

# Environment the session must not inherit: anything that could route the CLI
# to a real endpoint, authenticate it, or mark it as nested in another session.
_DROP_PREFIXES = ("ANTHROPIC_", "CLAUDE", "AWS_BEARER_TOKEN_BEDROCK", "TMUX")
_DROP_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    }
)

_HOOK_SCRIPT = """\
import json
import sys

raw = sys.stdin.read()
try:
    line = json.dumps(json.loads(raw), ensure_ascii=False)
except ValueError:
    line = json.dumps({"unparsed_stdin": raw}, ensure_ascii=False)
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(line + "\\n")
"""


class CaptureError(RuntimeError):
    """A scenario that cannot be captured faithfully; nothing is written for it."""


@dataclass(frozen=True)
class Scenario:
    name: str
    response: str  # what the fake server sends, recorded in the manifest
    status: int = 0  # an error JSON response's HTTP status; 0 for a stream scenario
    error_type: str = ""
    error_message: str = ""
    stream: str = ""  # "cut" | "truncated" for the two 200-stream scenarios


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario(
            "mid-response-cut",
            "200 text/event-stream: message_start, text block 0 complete, block 1 "
            "started with one delta, then the TCP connection reset (SO_LINGER 0)",
            stream="cut",
        ),
        Scenario(
            "truncated-stream",
            "200 for every request, closed cleanly before any content block "
            "completes: a streaming request gets message_start, block 0 started "
            "with one delta, then EOF; a non-streaming request gets an empty body",
            stream="truncated",
        ),
        Scenario(
            "rate-limit-429",
            "429 application/json rate_limit_error",
            429,
            "rate_limit_error",
            "Number of request tokens has exceeded your per-minute rate limit",
        ),
        Scenario(
            "overloaded-529",
            "529 application/json overloaded_error",
            529,
            "overloaded_error",
            "Overloaded",
        ),
        Scenario(
            "server-error-500",
            "500 application/json api_error",
            500,
            "api_error",
            "Internal server error",
        ),
        Scenario(
            "auth-401",
            "401 application/json authentication_error",
            401,
            "authentication_error",
            "invalid x-api-key",
        ),
        Scenario(
            "billing-402",
            "402 application/json billing_error",
            402,
            "billing_error",
            "Your credit balance is too low to access the Anthropic API.",
        ),
    )
}


# ------------------------------------------------------------- fake Anthropic API


def _sse(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


_MESSAGE_START = {
    "type": "message_start",
    "message": {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": "claude-fake",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    },
}


def _block_start(index: int) -> bytes:
    return _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        },
    )


def _block_delta(index: int, text: str) -> bytes:
    return _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    )


class FakeApi:
    """A loopback ``ThreadingHTTPServer`` that fails every message request the
    scenario's way and records every request it receives."""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeApi:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(entry)

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def _empty(self, status: int) -> None:
                self.send_response(status)
                self.send_header("content-length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                api._record({"method": self.command, "path": self.path, "served": False})
                self._empty(404)

            do_HEAD = do_GET  # noqa: N815

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length)
                is_message = (
                    self.path.startswith("/v1/messages") and "count_tokens" not in self.path
                )
                try:
                    stream = bool(json.loads(body).get("stream")) if body else False
                except (ValueError, AttributeError):
                    stream = False
                api._record(
                    {"method": "POST", "path": self.path, "stream": stream, "served": is_message}
                )
                if not is_message:
                    self._empty(404)
                    return
                scenario = api.scenario
                if scenario.stream == "cut":
                    self._cut()
                elif scenario.stream == "truncated":
                    self._truncated(stream)
                else:
                    self._error(scenario)

            def _stream_head(self) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()

            def _cut(self) -> None:
                self._stream_head()
                self.wfile.write(_sse("message_start", _MESSAGE_START))
                self.wfile.write(_block_start(0))
                self.wfile.write(_block_delta(0, "Partial answer before the cut"))
                self.wfile.write(
                    _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                )
                self.wfile.write(_block_start(1))
                self.wfile.write(_block_delta(1, "more"))
                self.wfile.flush()
                time.sleep(0.3)
                # SO_LINGER 0: close() sends RST, not FIN — a transport drop.
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                self.connection.close()
                self.close_connection = True

            def _truncated(self, stream: bool) -> None:
                if stream:
                    self._stream_head()
                    self.wfile.write(_sse("message_start", _MESSAGE_START))
                    self.wfile.write(_block_start(0))
                    self.wfile.write(_block_delta(0, "Truncated"))
                    self.wfile.flush()
                else:
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", "0")
                    self.end_headers()
                self.close_connection = True

            def _error(self, scenario: Scenario) -> None:
                body = json.dumps(
                    {
                        "type": "error",
                        "error": {"type": scenario.error_type, "message": scenario.error_message},
                        "request_id": "req_fake",
                    }
                ).encode()
                self.send_response(scenario.status)
                self.send_header("content-type", "application/json")
                self.send_header("request-id", "req_fake")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


# ------------------------------------------------------------------ the session


def _tmux(sock: str, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["tmux", "-u", "-L", sock, "-f", "/dev/null", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
        check=False,
    )
    if check and proc.returncode != 0:
        raise CaptureError(f"tmux {args[0]} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def _session_env(root: Path, port: int) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _DROP_NAMES
        and not k.startswith(_DROP_PREFIXES)
        and "BEDROCK" not in k
        and "VERTEX" not in k
    }
    with CLAUDE_PROFILE.open("rb") as fh:
        env.update({str(k): str(v) for k, v in tomllib.load(fh).get("env", {}).items()})
    env.update(
        {
            "CLAUDE_CONFIG_DIR": str(root / "cfg"),
            "ANTHROPIC_API_KEY": FAKE_KEY,
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "DISABLE_AUTOUPDATER": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return env


def _prepare(root: Path) -> Path:
    """The project (with its hook config) and the fresh config dir; returns the
    project dir."""
    proj = root / "proj"
    cfg = root / "cfg"
    (proj / ".claude").mkdir(parents=True)
    cfg.mkdir()
    hook = root / "hook.py"
    hook.write_text(_HOOK_SCRIPT, encoding="utf-8")
    log = root / "hooks.jsonl"
    command = " ".join(shlex.quote(p) for p in (sys.executable, str(hook), str(log)))
    entry = [{"hooks": [{"type": "command", "command": command}]}]
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"StopFailure": entry, "Stop": entry}}, indent=2) + "\n",
        encoding="utf-8",
    )
    # Pre-seeded so the session reaches its prompt with no dialog: onboarding
    # done, the external key approved, the workspace trusted.
    (cfg / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "customApiKeyResponses": {"approved": [FAKE_KEY[-20:]], "rejected": []},
                "projects": {
                    str(proj): {
                        "hasTrustDialogAccepted": True,
                        "hasCompletedProjectOnboarding": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return proj


def _hook_payloads(root: Path) -> list[dict[str, Any]]:
    log = root / "hooks.jsonl"
    if not log.is_file():
        return []
    payloads: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            payloads.append(json.loads(line))
        except ValueError:
            continue  # a line the hook is still writing
    return payloads


def _wait(predicate: Any, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise CaptureError(f"timed out after {timeout_s:.0f}s waiting for {what}")


def _redact(data: bytes, root: Path, port: int) -> bytes:
    placeholder = ROOT_PLACEHOLDER.encode()
    for path in sorted({str(root), os.path.realpath(root)}, key=len, reverse=True):
        data = data.replace(path.encode(), placeholder)
        data = data.replace(re.sub(r"[^A-Za-z0-9]", "-", path).encode(), placeholder)
    # The welcome banner elides a long cwd as `/…/<tail>`.
    data = re.sub(rb"/\xe2\x80\xa6/\S*?" + re.escape(root.name.encode()), placeholder, data)
    hosted = [host + b":" + str(port).encode() for host in (b"127.0.0.1", b"localhost")]
    for hp in hosted:
        data = data.replace(hp, hp.rsplit(b":", 1)[0] + b":" + PORT_PLACEHOLDER.encode())
    for leftover, what in ((root.name.encode(), "capture root"), *((hp, "port") for hp in hosted)):
        if leftover in data:
            raise CaptureError(f"redaction left the {what} ({leftover.decode()}) in the output")
    return data


@dataclass
class Capture:
    payload: dict[str, Any]
    pane: bytes
    requests: list[dict[str, Any]]


def capture(scenario: Scenario, claude: str, timeout_s: float, keep: bool) -> Capture:
    root = Path(tempfile.mkdtemp(prefix="ccsf-")).resolve()
    sock = f"bmad-loop-stopfailure-{os.getpid()}-{scenario.name}"
    try:
        proj = _prepare(root)
        pane_log = root / "pane.log"
        with FakeApi(scenario) as api:
            env = _session_env(root, api.port)
            try:
                _tmux(
                    sock,
                    "new-session",
                    "-d",
                    "-x",
                    str(PANE_WIDTH),
                    "-y",
                    str(PANE_HEIGHT),
                    "-c",
                    str(proj),
                    claude,
                    env=env,
                )
                _tmux(sock, "pipe-pane", "-o", f"cat >> {shlex.quote(str(pane_log))}", env=env)
                _wait(
                    lambda: "❯" in _tmux(sock, "capture-pane", "-p", env=env, check=False),
                    30,
                    "the prompt",
                )
                time.sleep(1.0)
                _tmux(sock, "send-keys", "-l", PROMPT, env=env)
                time.sleep(0.5)
                _tmux(sock, "send-keys", "Enter", env=env)
                _wait(
                    lambda: any(
                        p.get("hook_event_name") == "StopFailure" for p in _hook_payloads(root)
                    ),
                    timeout_s,
                    "the StopFailure hook",
                )
                time.sleep(3.0)  # let the pane finish rendering the error
            finally:
                _tmux(sock, "kill-server", env=env, check=False)
                time.sleep(0.5)  # pipe-pane's `cat` drains on EOF
            requests = list(api.requests)
        if not any(r["served"] for r in requests):
            raise CaptureError("the fake server recorded no message request")
        unserved = [r for r in requests if not r["served"]]
        if unserved:
            raise CaptureError(f"the fake server recorded requests it did not serve: {unserved}")
        failures = [p for p in _hook_payloads(root) if p.get("hook_event_name") == "StopFailure"]
        if len(failures) != 1:
            raise CaptureError(f"expected one StopFailure payload, got {len(failures)}")
        payload_bytes = json.dumps(failures[0], ensure_ascii=False).encode()
        payload = json.loads(_redact(payload_bytes, root, api.port))
        pane = _redact(pane_log.read_bytes() if pane_log.is_file() else b"", root, api.port)
        if not pane:
            raise CaptureError("pipe-pane captured nothing")
        return Capture(payload=payload, pane=pane, requests=requests)
    finally:
        if keep:
            print(f"  kept {root}", file=sys.stderr)
        else:
            shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------------- main


def _version(argv: list[str]) -> str:
    proc = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", timeout=60, check=True
    )
    return proc.stdout.strip()


def _write_manifest(out: Path, entries: dict[str, dict[str, Any]]) -> None:
    path = out / "manifest.json"
    manifest: dict[str, Any] = {}
    if path.is_file():
        manifest = json.loads(path.read_text(encoding="utf-8"))
    scenarios = dict(manifest.get("scenarios") or {})
    scenarios.update(entries)
    manifest = {
        "harness": "scripts/capture_claude_stopfailure.py",
        "redactions": {
            ROOT_PLACEHOLDER: "the temporary capture root: its path, its `-` slug, its `/…/` elided form",
            PORT_PLACEHOLDER: "the fake server's port after 127.0.0.1:",
        },
        "scenarios": {name: scenarios[name] for name in sorted(scenarios)},
    }
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="directory to write into")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(SCENARIOS),
        help="capture only this scenario (repeatable; default: all)",
    )
    parser.add_argument("--claude", default="claude", help="claude binary (default: on PATH)")
    parser.add_argument(
        "--timeout", type=float, default=90.0, help="seconds to wait for StopFailure"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep each scenario's temporary capture root"
    )
    args = parser.parse_args(argv)

    if sys.platform == "win32" or shutil.which("tmux") is None:
        print("capture_claude_stopfailure: needs tmux (POSIX)", file=sys.stderr)
        return 2
    claude = shutil.which(args.claude)
    if claude is None:
        print(f"capture_claude_stopfailure: {args.claude!r} not found", file=sys.stderr)
        return 2
    claude_version = _version([claude, "--version"]).split()[0]
    tmux_version = _version(["tmux", "-V"])
    captured = datetime.date.today().isoformat()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    entries: dict[str, dict[str, Any]] = {}
    failed = 0
    for name in args.scenario or list(SCENARIOS):
        scenario = SCENARIOS[name]
        print(f"{name}: capturing", file=sys.stderr)
        try:
            result = capture(scenario, claude, args.timeout, args.keep)
        except (CaptureError, subprocess.SubprocessError, OSError, ValueError) as exc:
            print(f"{name}: REFUSED — {exc}", file=sys.stderr)
            failed += 1
            continue
        (out / f"{name}.stopfailure.json").write_text(
            json.dumps(result.payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (out / f"{name}.pipe-pane.log").write_bytes(result.pane)
        entries[name] = {
            "claude_version": claude_version,
            "tmux_version": tmux_version,
            "captured": captured,
            "response": scenario.response,
            "requests": result.requests,
            "stop_failure_error": result.payload.get("error"),
        }
        print(f"{name}: error={result.payload.get('error')!r}", file=sys.stderr)
    if entries:
        _write_manifest(out, entries)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
