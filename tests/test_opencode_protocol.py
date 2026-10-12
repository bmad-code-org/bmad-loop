"""Unit tests for the OpenCode wire-protocol module: version parsing, the
support verdict both the adapter (refuse at spawn) and `validate` (warn) use,
and the per-major codecs' pure halves (config, health, frame decode)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.opencode_protocol import (
    SUPPORTED,
    V1Protocol,
    V2Protocol,
    parse_opencode_version,
    project_skill_ids,
    support_verdict,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param("1.19.0", (1, 19, 0), id="v1-bare"),
        pytest.param("opencode v2.0.27", (2, 0, 27), id="v2-prefixed"),
        pytest.param("opencode v2.0.27\n", (2, 0, 27), id="v2-trailing-newline"),
        pytest.param("v3.1.0", (3, 1, 0), id="unknown-major-still-parses"),
        pytest.param("", None, id="empty"),
        pytest.param("garbage", None, id="garbage"),
        pytest.param("1.2", None, id="two-part"),
    ],
)
def test_parse_opencode_version(text, expected):
    assert parse_opencode_version(text) == expected


def test_support_verdict_floors_and_majors():
    assert SUPPORTED == {1: (1, 18, 0), 2: (2, 0, 21)}

    for version in [(1, 18, 0), (1, 19, 0), (2, 0, 21), (2, 0, 27), (2, 3, 0)]:
        verdict, message = support_verdict(version)
        assert verdict == "ok", version
        assert f"V{version[0]} server API" in message

    verdict, message = support_verdict((1, 17, 9))
    assert verdict == "below-floor"
    assert "opencode 1.17.9" in message and "1.18.0" in message

    verdict, message = support_verdict((2, 0, 20))
    assert verdict == "below-floor"
    assert "opencode 2.0.20" in message and "2.0.21" in message

    for version in [(0, 9, 0), (3, 1, 0)]:
        verdict, message = support_verdict(version)
        assert verdict == "unsupported-major", version
        assert "1.x (>= 1.18.0)" in message and "2.x (>= 2.0.21)" in message


# ------------------------------------------------------------------- codecs


def _spec(tmp_path: Path, **kw) -> SessionSpec:
    return SessionSpec(task_id="t", role="triage", prompt="p", cwd=tmp_path, **kw)


def _skill(skill_dir: Path, dirname: str, name: str) -> None:
    (skill_dir / dirname).mkdir(parents=True)
    (skill_dir / dirname / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill {dirname}\n---\n\nBody.\n",
        encoding="utf-8",
    )


# Pinned live against 2.0.27: a skill's id is its DIRECTORY name (`dir-x`
# holding `name: name-y`), and the `skill` rules match the id.
SKILL_IDS_EXPECTED = ["dir-x", "plain-skill"]


class _Resp:
    def __init__(self, status_code: int, body, content_type: str = "application/json"):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


class _GetClient:
    def __init__(self, resp: _Resp):
        self.resp = resp
        self.paths: list[str] = []

    def get(self, path: str) -> _Resp:
        self.paths.append(path)
        return self.resp


def test_v1_config_content_unchanged(tmp_path):
    """V1's config bytes are a compatibility contract: the V2 work must leave
    them byte-identical (a skill tree on disk changes nothing on V1)."""
    skill_dir = tmp_path / ".claude" / "skills"
    _skill(skill_dir, "dir-x", "name-y")
    v1 = V1Protocol()
    assert v1.config_content(_spec(tmp_path), skill_dir) == (
        '{"permission": "allow", "skills": {"paths": [' + json.dumps(str(skill_dir)) + "]}}"
    )
    assert v1.config_content(_spec(tmp_path, model="a/b"), skill_dir) == (
        '{"permission": "allow", "skills": {"paths": ['
        + json.dumps(str(skill_dir))
        + ']}, "model": "a/b"}'
    )
    assert v1.extra_env() == {"OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"}


def test_v2_config_allows_all_and_denies_question(tmp_path):
    """V2's native keys: allow everything (the bypass-flags analogue), then deny
    `question` so an unattended session never parks on a form; the project
    skill tree as the `skills` path; the policy model as the string form."""
    skill_dir = tmp_path / ".claude" / "skills"
    config = json.loads(V2Protocol().config_content(_spec(tmp_path), skill_dir))
    assert config["permissions"][:2] == [
        {"action": "*", "resource": "*", "effect": "allow"},
        {"action": "question", "resource": "*", "effect": "deny"},
    ]
    assert config["skills"] == [str(skill_dir)]
    assert "permission" not in config and "model" not in config
    config = json.loads(V2Protocol().config_content(_spec(tmp_path, model="mock/m"), skill_dir))
    assert config["model"] == "mock/m"
    assert V2Protocol().extra_env() == {}


def test_v2_config_denies_external_skills(tmp_path):
    """V2 has no OPENCODE_DISABLE_EXTERNAL_SKILLS: the only lever is a `skill`
    deny-all followed by one allow per project skill id. Rules are last-match-
    wins, so every allow must come AFTER the deny-all — reversed, the deny
    would hide the project skills too. Directories without a SKILL.md and
    loose files are not skills."""
    skill_dir = tmp_path / ".claude" / "skills"
    _skill(skill_dir, "plain-skill", "plain-skill")
    _skill(skill_dir, "dir-x", "name-y")
    (skill_dir / "no-skill-md").mkdir()
    (skill_dir / "README.md").write_text("not a skill", encoding="utf-8")

    rules = json.loads(V2Protocol().config_content(_spec(tmp_path), skill_dir))["permissions"]

    skill_rules = [r for r in rules if r["action"] == "skill"]
    assert skill_rules[0] == {"action": "skill", "resource": "*", "effect": "deny"}
    assert skill_rules[1:] == [
        {"action": "skill", "resource": skill_id, "effect": "allow"}
        for skill_id in SKILL_IDS_EXPECTED
    ]
    assert rules.index(skill_rules[0]) < min(rules.index(r) for r in skill_rules[1:])
    assert project_skill_ids(skill_dir) == SKILL_IDS_EXPECTED


def test_v2_config_without_a_skill_tree_still_denies_every_skill(tmp_path):
    rules = json.loads(
        V2Protocol().config_content(_spec(tmp_path), tmp_path / ".claude" / "skills")
    )["permissions"]
    assert [r for r in rules if r["action"] == "skill"] == [
        {"action": "skill", "resource": "*", "effect": "deny"}
    ]


def test_v2_decode_frame_reads_data_envelope():
    """V2 frames carry the session id under `data`, not V1's `properties`."""
    v2 = V2Protocol()
    frame = {
        "id": "evt_1",
        "created": 1,
        "type": "session.execution.succeeded",
        "data": {"sessionID": "ses_1", "x": 1},
    }
    assert v2.frame_type(frame) == "session.execution.succeeded"
    assert v2.frame_payload(frame) == ({"sessionID": "ses_1", "x": 1}, "ses_1")
    # A V1-shaped frame names no session in the V2 envelope.
    v1_frame = {"type": "session.idle", "properties": {"sessionID": "ses_1"}}
    assert v2.frame_payload(v1_frame) == ({}, None)
    assert v2.frame_payload({"type": "server.connected", "data": {}}) == ({}, None)
    assert v2.event_path == "/api/event"


def test_v2_execution_events_map_to_idle_and_error():
    """V2's turn end is `session.execution.*`; `session.idle` / `session.status`
    are deprecated and never fired live, so neither may be waited on. The
    heartbeat is an SSE comment, leaving `server.connected` as the only
    liveness-only frame; no session-less frame is admitted."""
    v2 = V2Protocol()
    assert v2.idle_types == {"session.execution.succeeded", "session.execution.interrupted"}
    assert v2.error_types == {"session.execution.failed"}
    assert v2.heartbeat_types == ("server.connected",)
    assert v2.sessionless_types == frozenset()
    for deprecated in ("session.idle", "session.status", "session.error"):
        assert deprecated not in v2.idle_types | v2.error_types


def test_v2_health_rejects_html_and_v1_shape():
    """`/api/info` is healthy only as a JSON object with a string 2.x `version`:
    the web-UI HTML a V1 server answers is flagged as such, and V1's health body
    (or a 1.x version) is not V2's."""
    v2 = V2Protocol()
    good = _GetClient(_Resp(200, {"version": "2.0.27", "pid": 1, "urls": [], "paths": {}}))
    assert v2.health(good).healthy is True
    assert good.paths == ["/api/info"]
    html = v2.health(_GetClient(_Resp(200, "<!doctype html>", "text/html; charset=utf-8")))
    assert (html.healthy, html.html) == (False, True)
    for body in (
        {"healthy": True, "version": "1.19.0"},
        {"healthy": True},
        {"version": 2},
        {"version": "garbage"},
        ["2.0.27"],
    ):
        probe = v2.health(_GetClient(_Resp(200, body)))
        assert (probe.healthy, probe.html) == (False, False), body
    unauthorized = v2.health(_GetClient(_Resp(401, {"_tag": "UnauthorizedError"})))
    assert unauthorized.healthy is False


def test_v1_health_rejects_v2_info_shape():
    """The V1 health route is healthy only on `{"healthy": true}`: V2's
    `/api/info` body carries no such flag."""
    v1 = V1Protocol()
    info = {"version": "2.0.27", "pid": 1, "urls": [], "paths": {}, "capabilities": {}}
    assert v1.health(_GetClient(_Resp(200, info))).healthy is False
    html = v1.health(_GetClient(_Resp(200, "<!doctype html>", "text/html")))
    assert (html.healthy, html.html) == (False, True)
    ok = _GetClient(_Resp(200, {"healthy": True, "version": "1.19.0"}))
    assert v1.health(ok).healthy is True
    assert ok.paths == ["/global/health"]


class _PostClient:
    def __init__(self, resp: _Resp):
        self.resp = resp
        self.calls: list[tuple[str, object]] = []

    def post(self, path: str, json=None) -> _Resp:  # noqa: A002 - mirrors httpx
        self.calls.append((path, json))
        return self.resp


def test_v2_create_prompt_and_interrupt_routes(tmp_path):
    """Create carries the task title and the project as the location and reads
    `data.id`; prompt is `{"text"}` (no variant field on V2) and must answer
    200; abort is `/interrupt`. A refusal raises, like V1's non-204."""
    v2 = V2Protocol()
    create = _PostClient(_Resp(200, {"data": {"id": "ses_9"}}))
    assert v2.create_session(create, _spec(tmp_path)) == "ses_9"
    assert create.calls == [
        ("/api/session", {"title": "t", "location": {"directory": str(tmp_path)}})
    ]
    prompt = _PostClient(_Resp(200, {"data": {"id": "msg_1", "type": "user"}}))
    v2.prompt(prompt, "ses_9", "hello", "high")
    assert prompt.calls == [("/api/session/ses_9/prompt", {"text": "hello"})]
    from bmad_loop.adapters.opencode_protocol import OpencodeServerError

    with pytest.raises(OpencodeServerError, match="prompt failed: 409"):
        v2.prompt(_PostClient(_Resp(409, {"_tag": "ConflictError"})), "ses_9", "x", "")
    with pytest.raises(OpencodeServerError, match="POST /api/session failed: 500"):
        v2.create_session(_PostClient(_Resp(500, {"_tag": "x"})), _spec(tmp_path))
    interrupt = _PostClient(_Resp(200, {"interrupted": True}))
    v2.abort(interrupt, "ses_9")
    assert interrupt.calls == [("/api/session/ses_9/interrupt", None)]


def test_v2_unread_probes_answer_unknown_never_empty():
    """Until the V2 poll/usage reads land, they answer "unknown" with a fault —
    never a healthy-looking False / 0 / [] a caller would read as measured."""
    v2 = V2Protocol()
    assert v2.session_busy(None, "ses_1") is None
    assert v2.latest_completed_ms(None, "ses_1") is None
    transcript, fault = v2.transcript(None, "ses_1")
    assert transcript is None and fault
    usage, fault = v2.tally(None)
    assert usage is None and fault
