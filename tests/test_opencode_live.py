"""Live-contract smoke against a REAL local `opencode` binary (skipif-gated).

ZERO-TOKEN INVARIANT: this module never sends a prompt — `prompt_async`,
`/session/{id}/message` POST and `/session/{id}/command` are never called, and
`adapter.start_session` (which prompts) is never used. Sessions are created,
inspected and deleted freely, which spends nothing (verified during Phase 0
pinning). Servers are spawned through the adapter's own `_spawn_server`, so
the spawn/auth/health/teardown code paths run for real.

Each assert pins a fact from the adapter's API-contract docstring
(``opencode_http``, verified live against 1.18.2). A newer opencode that
breaks the contract surfaces here as a loud —
but cleanly skippable — failure instead of a silent adapter break.

Windows is excluded: opencode-on-Windows is unverified for this adapter
(README adapter table); the suite must skip cleanly there.
"""

from __future__ import annotations

import json
import re
import socket

import pytest
from conftest import opencode_runs

HAVE_OPENCODE = opencode_runs()
pytestmark = pytest.mark.skipif(
    not HAVE_OPENCODE,
    reason="live smoke needs a runnable `opencode` binary (POSIX): not on PATH, or on "
    "PATH but failing `--version` — e.g. a dead WSL/npm shim (#294)",
)

httpx = pytest.importorskip("httpx")

from bmad_loop.adapters.base import SessionSpec  # noqa: E402
from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter  # noqa: E402
from bmad_loop.adapters.profile import get_profile  # noqa: E402
from bmad_loop.policy import LimitsPolicy, Policy  # noqa: E402


def _spawn(tmp_path):
    """One real `opencode serve` via the adapter's own spawn path (port
    pre-bind, per-session password, hermetic-skills config, health poll)."""
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)
    adapter = OpencodeHttpAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("opencode"),
    )
    spec = SessionSpec(task_id="live-smoke", role="triage", prompt="never sent", cwd=cwd)
    return adapter, adapter._spawn_server(spec)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("opencode-live")
    adapter, sess = _spawn(tmp)
    try:
        yield adapter, sess
    finally:
        adapter._teardown(sess)
        if sess.process.poll() is None:  # never leak a live server past the suite
            sess.process.kill()
            sess.process.wait(timeout=10)


def test_health_shape_and_auth(live):
    """Pins §1 + §2: /global/health shape; password mode 401s everything —
    including the readiness probe itself — and our client authenticates."""
    _, sess = live
    resp = sess.client.get("/global/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is True
    assert isinstance(body["version"], str) and body["version"]

    with httpx.Client(base_url=sess.base_url, timeout=5.0) as anon:
        assert anon.get("/global/health").status_code == 401


def test_openapi_pins_load_bearing_paths(live):
    """Pins §§2,5,6,8: every endpoint the adapter drives still exists, and
    prompt_async still accepts `parts` with a 204."""
    _, sess = live
    resp = sess.client.get("/doc")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for path in (
        "/session",
        "/session/{sessionID}/prompt_async",
        "/session/{sessionID}/abort",
        "/session/{sessionID}/message",
        "/session/status",
        "/event",
        "/global/health",
    ):
        assert path in paths, f"pinned endpoint missing from /doc: {path}"

    prompt_async = paths["/session/{sessionID}/prompt_async"]["post"]
    assert "204" in prompt_async["responses"]
    schema = prompt_async["requestBody"]["content"]["application/json"]["schema"]
    assert schema.get("required") == ["parts"]


def test_session_lifecycle_zero_token(live):
    """Pins §4 + §6 + §7: create/inspect/delete a session without ever
    prompting — id shape, zeroed usage aggregates, absent-from-status-map
    poll rule, empty message list, delete → true → 404."""
    _, sess = live
    client = sess.client

    resp = client.post("/session", json={"title": "live-smoke"})
    assert resp.status_code == 200
    body = resp.json()
    sid = body["id"]
    assert re.match(r"^ses", sid)
    assert body["title"] == "live-smoke"
    tokens = body["tokens"]
    assert (tokens["input"], tokens["output"], tokens["reasoning"]) == (0, 0, 0)
    assert tokens["cache"] == {"read": 0, "write": 0}
    assert body["cost"] == 0

    # Poll fallback (§6): an idle session is ABSENT from the status map (or,
    # tolerated for forward-compat, present as explicit idle) — never busy.
    status = client.get("/session/status")
    assert status.status_code == 200
    entry = status.json().get(sid)
    assert entry is None or entry.get("type") == "idle"

    # No turn ever ran: the message list is empty and stays empty (§6).
    msgs = client.get(f"/session/{sid}/message")
    assert msgs.status_code == 200
    assert msgs.json() == []

    assert client.delete(f"/session/{sid}").json() is True
    assert client.get(f"/session/{sid}").status_code == 404


def test_event_stream_first_frame_is_server_connected(live):
    """Pins §2: /event streams flat `data:`-only frames and greets with
    server.connected — the barrier start_session waits on before prompting."""
    _, sess = live
    with sess.client.stream("GET", "/event") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[len("data: ") :])
                assert event["type"] == "server.connected"
                break
            assert not line.startswith("event:"), "framing changed: event: fields appeared"


def test_teardown_leaves_no_orphan(tmp_path):
    """Pins §11: SIGTERM exits opencode cleanly; after `_teardown` the process
    is reaped and the port refuses connections. Own server: the check must not
    depend on test ordering against the shared module fixture."""
    adapter, sess = _spawn(tmp_path)
    port = sess.port
    adapter._teardown(sess)
    assert sess.process.poll() is not None
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1.0)
