"""Live-contract smoke against a REAL local `opencode` binary (skipif-gated).

ZERO-TOKEN INVARIANT: this module never sends a prompt — V1's `prompt_async`,
`/session/{id}/message` POST and `/session/{id}/command`, and V2's
`/api/session/{id}/prompt`, are never called, and `adapter.start_session`
(which prompts) is never used. Sessions are created, inspected and deleted
freely, which spends nothing (verified during Phase 0 pinning). Servers are
spawned through the adapter's own `_spawn_server`, so the version detection
and the spawn/auth/health/teardown code paths run for real. (The full-turn
gate, which does prompt — a local fake LLM only — is
`test_opencode_live_turn.py`.)

Each assert pins a fact from the API-contract docstring of the detected
major's codec (``opencode_protocol``: V1 verified live against 1.18.2, V2
against 2.0.27). Each major's checks skip on the other (``test_v2_*`` are the
2.x ones), so one run covers whichever `opencode` is on PATH. A newer opencode that
breaks the contract surfaces here as a loud — but cleanly skippable — failure
instead of a silent adapter break.

Every server runs under a scratch HOME / XDG base: the operator's own
opencode state is never read or written. That is load-bearing, not tidiness —
both majors keep their sqlite store at the same
``$XDG_DATA_HOME/opencode/opencode.db``, and a 1.x server refuses to start on
one a 2.x server created ("Database is not empty and has no session table").

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
from bmad_loop.adapters.opencode_protocol import parse_opencode_version  # noqa: E402
from bmad_loop.adapters.profile import get_profile  # noqa: E402
from bmad_loop.policy import LimitsPolicy, Policy  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def scratch_home(tmp_path_factory):
    """A throwaway HOME + XDG base dirs for every server this module spawns
    (module docstring: the two majors share one data-dir database)."""
    home = tmp_path_factory.mktemp("opencode-home")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        for var, rel in (
            ("XDG_CONFIG_HOME", ".config"),
            ("XDG_DATA_HOME", ".local/share"),
            ("XDG_STATE_HOME", ".local/state"),
            ("XDG_CACHE_HOME", ".cache"),
        ):
            mp.setenv(var, str(home / rel))
        yield home


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
def live(scratch_home, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("opencode-live")
    adapter, sess = _spawn(tmp)
    try:
        yield adapter, sess
    finally:
        adapter._teardown(sess)
        if sess.process.poll() is None:  # never leak a live server past the suite
            sess.process.kill()
            sess.process.wait(timeout=10)


def _only_on(sess, major: int) -> None:
    """Skip a check pinned for one server API major when PATH's opencode
    speaks the other — the adapter detected it from `--version`."""
    if sess.protocol.major != major:
        pytest.skip(f"pins the {major}.x server API; PATH opencode is {sess.server_version}")


def test_version_output_matches_the_serving_api(live):
    """Pins the detection seam: the version `--version` reported (what picked
    the codec) is the one the running server reports on its own major's health
    endpoint — a mismatch would mean the adapter speaks the wrong API."""
    _, sess = live
    health = "/global/health" if sess.protocol.major == 1 else "/api/info"
    resp = sess.client.get(health)
    assert resp.status_code == 200
    served = parse_opencode_version(resp.json()["version"])
    assert served is not None
    assert served == parse_opencode_version(sess.server_version)
    assert served[0] == sess.protocol.major


def test_health_shape_and_auth(live):
    """Pins §1 + §2: /global/health shape; password mode 401s everything —
    including the readiness probe itself — and our client authenticates."""
    _, sess = live
    _only_on(sess, 1)
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
    _only_on(sess, 1)
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
    _only_on(sess, 1)
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
    _only_on(sess, 1)
    with sess.client.stream("GET", "/event") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[len("data: ") :])
                assert event["type"] == "server.connected"
                break
            assert not line.startswith("event:"), "framing changed: event: fields appeared"


def test_v2_api_info_shape_and_auth(live):
    """Pins V2 health + auth: `GET /api/info` answers JSON with a string
    `version`; password mode 401s an anonymous request with the
    `UnauthorizedError` tag, and our client authenticates."""
    _, sess = live
    _only_on(sess, 2)
    resp = sess.client.get("/api/info")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert isinstance(body["version"], str) and body["version"]

    with httpx.Client(base_url=sess.base_url, timeout=5.0) as anon:
        refused = anon.get("/api/info")
        assert refused.status_code == 401
        assert refused.json()["_tag"] == "UnauthorizedError"


def test_v2_openapi_pins_load_bearing_paths(live):
    """Pins V2: every route V2Protocol drives is in `/openapi.json` with the
    method it uses, and the prompt still answers 200 to a `text` body."""
    _, sess = live
    _only_on(sess, 2)
    resp = sess.client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for path, method in (
        ("/api/info", "get"),
        ("/api/session", "post"),
        ("/api/session/{sessionID}", "get"),
        ("/api/session/{sessionID}/prompt", "post"),
        ("/api/session/{sessionID}/message", "get"),
        ("/api/session/{sessionID}/interrupt", "post"),
        ("/api/session/active", "get"),
        ("/api/event", "get"),
        ("/api/model", "get"),
        ("/api/model/default", "get"),
    ):
        assert method in paths.get(
            path, {}
        ), f"pinned route missing from /openapi.json: {method} {path}"

    prompt = paths["/api/session/{sessionID}/prompt"]["post"]
    assert "200" in prompt["responses"]
    schema = prompt["requestBody"]["content"]["application/json"]["schema"]
    assert "text" in schema["properties"]


def test_v2_session_create_get_delete_zero_token(live, tmp_path):
    """Pins V2 session shapes without ever prompting: create → `data.id` with
    zeroed `tokens` (the aggregate usage reads), absent from the active map,
    empty first message page with null cursors, interrupt on an idle session
    → false, delete → 204, then a 404 `SessionNotFoundError`."""
    _, sess = live
    _only_on(sess, 2)
    client = sess.client
    resp = client.post(
        "/api/session", json={"title": "live-smoke", "location": {"directory": str(tmp_path)}}
    )
    assert resp.status_code == 200
    info = resp.json()["data"]
    sid = info["id"]
    assert re.match(r"^ses", sid)
    assert info["title"] == "live-smoke"
    tokens = info["tokens"]
    assert (tokens["input"], tokens["output"], tokens["reasoning"]) == (0, 0, 0)
    assert tokens["cache"] == {"read": 0, "write": 0}
    assert info["cost"] == 0

    got = client.get(f"/api/session/{sid}")
    assert got.status_code == 200
    assert got.json()["data"]["tokens"] == tokens

    active = client.get("/api/session/active")
    assert active.status_code == 200
    assert sid not in active.json()["data"]

    msgs = client.get(f"/api/session/{sid}/message")
    assert msgs.status_code == 200
    assert msgs.json() == {"data": [], "cursor": {"previous": None, "next": None}}

    assert client.post(f"/api/session/{sid}/interrupt").json() == {"interrupted": False}

    assert client.delete(f"/api/session/{sid}").status_code == 204
    gone = client.get(f"/api/session/{sid}")
    assert gone.status_code == 404
    assert gone.json()["_tag"] == "SessionNotFoundError"


def test_v2_event_stream_first_frame_is_server_connected(live):
    """Pins V2 `/api/event`: `data:`-only frames in the `{id, type, data}`
    envelope (no `properties`), greeting with server.connected."""
    _, sess = live
    _only_on(sess, 2)
    with sess.client.stream("GET", "/api/event") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[len("data: ") :])
                assert event["type"] == "server.connected"
                assert isinstance(event["data"], dict)
                assert "properties" not in event
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
