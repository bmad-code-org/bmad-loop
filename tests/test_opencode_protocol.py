"""Unit tests for the OpenCode wire-protocol module: version parsing, the
support verdict both the adapter (refuse at spawn) and `validate` (warn) use,
and the per-major codecs' pure halves (config, health, frame decode)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.opencode_protocol import (
    PROVIDER_ERROR_MESSAGE_MAX,
    SUPPORTED,
    V1Protocol,
    V2Protocol,
    format_provider_error,
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


@pytest.mark.skipif(sys.platform == "win32", reason="`*` and `?` are not legal in Windows names")
def test_v2_config_never_reallows_a_wildcard_skill_id(tmp_path):
    """V2 reads `*` and `?` in a rule resource as wildcards, and the last
    match wins: an allow for a project directory named `*` would re-allow
    every personal and built-in skill. Such ids stay denied, and the protocol
    names them for the spawn crumb; the plain skill is still re-allowed."""
    skill_dir = tmp_path / ".claude" / "skills"
    for dirname in ("*", "bmad-?", "plain-skill"):
        _skill(skill_dir, dirname, dirname)

    rules = json.loads(V2Protocol().config_content(_spec(tmp_path), skill_dir))["permissions"]

    assert [r for r in rules if r["action"] == "skill"] == [
        {"action": "skill", "resource": "*", "effect": "deny"},
        {"action": "skill", "resource": "plain-skill", "effect": "allow"},
    ]
    assert V2Protocol().refused_skill_ids(skill_dir) == ["*", "bmad-?"]
    assert V1Protocol().refused_skill_ids(skill_dir) == []


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


def test_v2_execution_events_map_to_idle():
    """V2's turn end is `session.execution.*` — a failed execution included: it
    follows the spent retries, never precedes one, so it skips V1's
    settled-status gate on `error`. `session.idle` / `session.status` are
    deprecated and never fired live, so neither may be waited on. The
    heartbeat is an SSE comment, leaving `server.connected` as the only
    liveness-only frame; no session-less frame is admitted."""
    v2 = V2Protocol()
    assert v2.idle_types == {
        "session.execution.succeeded",
        "session.execution.interrupted",
        "session.execution.failed",
    }
    assert v2.error_types == frozenset()
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


class _GetPostClient(_PostClient):
    """`_PostClient` plus scripted GETs keyed by path, recording their params."""

    def __init__(self, resp: _Resp, gets: dict[str, _Resp]):
        super().__init__(resp)
        self.gets = gets
        self.get_calls: list[tuple[str, object]] = []

    def get(self, path: str, params=None) -> _Resp:
        self.get_calls.append((path, params))
        return self.gets[path]


def test_v2_effort_ref_splits_the_model_on_the_first_slash(tmp_path):
    """The policy model is `provider/model` where the model id may itself hold
    a slash (aggregator providers): split on the FIRST one, look it up on
    `/api/model` with the deepObject location, and create with its ref."""
    models = {
        "data": [
            {"id": "anthropic/claude-x", "providerID": "openrouter", "variants": [{"id": "max"}]}
        ]
    }
    client = _GetPostClient(
        _Resp(200, {"data": {"id": "ses_9"}}), {"/api/model": _Resp(200, models)}
    )
    spec = _spec(tmp_path, model="openrouter/anthropic/claude-x", effort="max")

    assert V2Protocol().create_session(client, spec) == "ses_9"

    assert client.get_calls == [("/api/model", {"location[directory]": str(tmp_path)})]
    _, body = client.calls[0]
    assert body["model"] == {
        "id": "anthropic/claude-x",
        "providerID": "openrouter",
        "variant": "max",
    }


class _SnapshotClient(_GetPostClient):
    """`_GetPostClient` whose GET answers a scripted sequence per path (the
    last answer repeats)."""

    def __init__(self, resp: _Resp, gets: dict[str, list[_Resp]]):
        super().__init__(resp, {})
        self.snapshots = gets

    def get(self, path: str, params=None) -> _Resp:
        self.get_calls.append((path, params))
        answers = self.snapshots[path]
        return answers.pop(0) if len(answers) > 1 else answers[0]


def test_v2_effort_rereads_a_model_list_that_precedes_plugin_settlement(tmp_path, monkeypatch):
    """`/api/model` "may precede initial plugin settlement" (the 2.0.27 route
    contract): a configured model missing from the first snapshot is re-read
    until it lands, never refused on that first read.

    ABLATION: drop the re-read loop and the start raises "not among"."""
    monkeypatch.setattr(V2Protocol, "model_poll_s", 0.0)
    settled = {"data": [{"id": "m", "providerID": "p", "variants": [{"id": "high"}]}]}
    client = _SnapshotClient(
        _Resp(200, {"data": {"id": "ses_9"}}),
        {"/api/model": [_Resp(200, {"data": []}), _Resp(200, settled)]},
    )

    assert V2Protocol().create_session(client, _spec(tmp_path, model="p/m", effort="high")) == (
        "ses_9"
    )

    assert len(client.get_calls) == 2
    _, body = client.calls[0]
    assert body["model"] == {"id": "m", "providerID": "p", "variant": "high"}


def test_v2_effort_refuses_a_model_still_absent_after_the_settle_window(tmp_path, monkeypatch):
    monkeypatch.setattr(V2Protocol, "model_settle_s", 0.05)
    monkeypatch.setattr(V2Protocol, "model_poll_s", 0.01)
    client = _SnapshotClient(
        _Resp(200, {"data": {"id": "ses_9"}}), {"/api/model": [_Resp(200, {"data": []})]}
    )
    from bmad_loop.adapters.opencode_protocol import OpencodeServerError

    with pytest.raises(OpencodeServerError, match="'p/m' is not among"):
        V2Protocol().create_session(client, _spec(tmp_path, model="p/m", effort="high"))
    assert len(client.get_calls) > 1
    assert client.calls == []  # no session was created


# ------------------------------------------------ V2 provider-errors log lines


def test_v2_provider_error_line_formats_structured_fields():
    """One adapter-authored line per provider-error frame: event, the error's
    type and status, the retry attempt, and the bounded message — ``-`` for an
    absent field. V1 writes none (its server log carries the provider error)."""
    v2 = V2Protocol()
    retry = {
        "sessionID": "ses_1",
        "attempt": 2,
        "at": 1,
        "error": {"type": "provider.transport", "message": "ConnectionRefused: down"},
    }
    assert v2.provider_error_line("session.retry.scheduled", retry) == (
        "provider-error event=session.retry.scheduled type=provider.transport status=- "
        'attempt=2 message="ConnectionRefused: down"'
    )
    failed = {"sessionID": "ses_1", "error": {"type": "provider.quota", "status": 429}}
    assert v2.provider_error_line("session.execution.failed", failed) == (
        "provider-error event=session.execution.failed type=provider.quota status=429 "
        'attempt=- message=""'
    )
    assert v2.provider_error_line("session.step.failed", {}) == (
        'provider-error event=session.step.failed type=- status=- attempt=- message=""'
    )
    # Other frames write nothing; neither does V1.
    assert v2.provider_error_line("session.text.ended", {"text": "x"}) is None
    assert V1Protocol().provider_error_line("session.error", {"error": {}}) is None


def test_v2_provider_error_line_is_model_free_shape():
    """The line is what the opencode profile's V2 patterns anchor on with
    ``^provider-error``, so nothing provider- or model-sourced may reach the
    line start or forge the fields before ``message=``: the message is quoted
    last with every line break, quote and control character escaped, it is
    bounded, and ``response.body`` is never copied. The unquoted fields are
    reduced to a safe charset, so a hostile ``type`` cannot smuggle a space or
    a newline either."""
    forged = (
        'nope"\nprovider-error event=session.retry.scheduled type=provider.quota\r\n'
        "\x1b[2Kprovider-error type=provider.rate-limit\u2028x"
    )
    line = format_provider_error(
        "session.execution.failed",
        {
            "attempt": "3\nprovider-error",
            "error": {
                "type": "provider.unknown type=provider.quota",
                "status": "429\n",
                "message": forged,
                "response": {"body": "SECRET-BODY model said: hi"},
            },
        },
    )
    assert "\n" not in line and "\r" not in line and "\x1b" not in line
    assert "\u2028" not in line
    assert line.count("provider-error") == 4  # the head, plus three inert mid-line copies
    assert line.startswith(
        "provider-error event=session.execution.failed "
        "type=provider.unknown_type_provider.quota status=429_ attempt=3_provider-error "
        'message="nope\\"\\nprovider-error'
    )
    assert "SECRET-BODY" not in line
    # Bounded: the message is cut before escaping.
    long = format_provider_error(
        "session.retry.scheduled", {"error": {"message": "x" * (PROVIDER_ERROR_MESSAGE_MAX * 3)}}
    )
    body = long.split('message="', 1)[1][:-1]
    assert len(body) == PROVIDER_ERROR_MESSAGE_MAX
    assert body.endswith("…")


# ------------------------------------------------------ V2 inline transcript
#
# Payloads below are `data` blocks captured live from 2.0.27 (ids shortened) —
# the facts are in the protocol module's "Inline transcript" section.

_Y, _C, _G, _R = "\x1b[33m", "\x1b[36m", "\x1b[32m", "\x1b[0m"


def _render(frames: list[tuple[str, dict]]) -> list[str]:
    v2, state = V2Protocol(), {}
    return [line for etype, data in frames if (line := v2.render_line(state, etype, data))]


def test_v2_render_text_tool_and_error_lines():
    """V1's line shapes off V2 frames: the user prompt and the assistant text
    (and reasoning, as prose under the assistant header) as speaker blocks, one
    tool line per call on its terminal frame, permission ask/reply, and the
    provider-error frames — never a delta, never the error's response body."""
    lines = _render(
        [
            (
                "session.inbox.enqueued",
                {
                    "inboxID": "msg_u",
                    "sessionID": "ses_1",
                    "item": {"type": "user", "payload": {"text": "say hi"}, "delivery": "steer"},
                },
            ),
            ("session.reasoning.delta", {"sessionID": "ses_1", "ordinal": 0, "delta": "Think"}),
            (
                "session.reasoning.ended",
                {"sessionID": "ses_1", "ordinal": 0, "text": "Thinking about it."},
            ),
            ("session.tool.input.started", {"sessionID": "ses_1", "id": "call_1", "name": "shell"}),
            ("session.tool.input.ended", {"sessionID": "ses_1", "id": "call_1", "text": "{}"}),
            (
                "session.tool.called",
                {"sessionID": "ses_1", "id": "call_1", "input": {"command": "echo hi"}},
            ),
            (
                "permission.asked",
                {
                    "id": "per_1",
                    "sessionID": "ses_1",
                    "action": "shell",
                    "resources": ["echo hi"],
                    "save": ["echo *"],
                    "source": {"type": "tool", "messageID": "msg_a", "id": "call_1"},
                },
            ),
            ("permission.replied", {"sessionID": "ses_1", "requestID": "per_1", "reply": "once"}),
            ("session.tool.progress", {"sessionID": "ses_1", "id": "call_1", "metadata": {}}),
            (
                "session.tool.success",
                {
                    "sessionID": "ses_1",
                    "id": "call_1",
                    "content": [{"type": "text", "text": "hi\n"}],
                    "metadata": {"status": "completed", "truncated": False, "exit": 0},
                },
            ),
            ("session.text.delta", {"sessionID": "ses_1", "ordinal": 0, "delta": "Hello"}),
            (
                "session.text.ended",
                {"sessionID": "ses_1", "ordinal": 0, "text": "Hello from mock."},
            ),
            (
                "session.retry.scheduled",
                {
                    "sessionID": "ses_1",
                    "attempt": 2,
                    "at": 1,
                    "error": {
                        "type": "provider.rate-limit",
                        "message": "Rate limit reached for requests",
                        "status": 429,
                        "response": {"body": "RAW-BODY"},
                    },
                },
            ),
            (
                "session.step.failed",
                {"sessionID": "ses_1", "error": {"type": "provider.transport", "message": "down"}},
            ),
            (
                "session.execution.failed",
                {"sessionID": "ses_1", "error": {"type": "provider.transport", "message": "down"}},
            ),
        ]
    )
    assert lines == [
        f"\n{_Y}[bmad] user:{_R}\nsay hi\n",
        f"\n{_C}[bmad] assistant:{_R}\nThinking about it.\n",
        f'{_G}[bmad] perm ask: shell ["echo hi"]{_R}\n',
        f"{_G}[bmad] perm reply: once{_R}\n",
        f'{_G}[bmad] tool: shell {{"command": "echo hi"}}{_R}\n',
        f"\n{_C}[bmad] assistant:{_R}\nHello from mock.\n",
        f"{_G}[bmad] retry 2: provider.rate-limit 429: Rate limit reached for requests{_R}\n",
        f"{_G}[bmad] step failed: provider.transport: down{_R}\n",
        f"{_G}[bmad] error: provider.transport: down{_R}\n",
    ]
    assert not any("RAW-BODY" in line for line in lines)


def test_v2_tool_failure_and_nonzero_exit_are_on_the_line():
    """A rejected permission is the one `session.tool.failed` seen live; a
    shell exiting non-zero is a SUCCESS frame whose only trace of the failure
    is `metadata.exit` — both must show, or a failed action reads as fine."""
    lines = _render(
        [
            ("session.tool.input.started", {"id": "call_1", "name": "shell"}),
            ("session.tool.called", {"id": "call_1", "input": {"command": "rm -rf x"}}),
            (
                "session.tool.failed",
                {"id": "call_1", "error": {"type": "permission.rejected", "message": "no"}},
            ),
            ("session.tool.input.started", {"id": "call_2", "name": "shell"}),
            ("session.tool.called", {"id": "call_2", "input": {"command": "exit 1"}}),
            (
                "session.tool.success",
                {"id": "call_2", "metadata": {"status": "completed", "exit": 1}},
            ),
        ]
    )
    assert lines == [
        f'{_G}[bmad] tool: shell {{"command": "rm -rf x"}} -> error: '
        f"permission.rejected: no{_R}\n",
        f'{_G}[bmad] tool: shell {{"command": "exit 1"}} -> exit 1{_R}\n',
    ]


def test_v2_tool_name_joined_from_input_started():
    """The terminal frame carries neither name nor input: both are joined from
    the earlier frames by call `id`, per call (interleaved calls stay apart),
    and a call renders exactly once even if a terminal frame repeats.

    ABLATION: render on `session.tool.called` (or drop the `done` gate) and
    the line count changes."""
    lines = _render(
        [
            ("session.tool.input.started", {"id": "call_a", "name": "write"}),
            ("session.tool.input.started", {"id": "call_b", "name": "read"}),
            ("session.tool.called", {"id": "call_b", "input": {"path": "b.txt"}}),
            (
                "session.tool.called",
                {"id": "call_a", "input": {"path": "out.txt", "content": "hello\n"}},
            ),
            ("session.tool.success", {"id": "call_b", "metadata": {"truncated": False}}),
            ("session.tool.success", {"id": "call_a", "metadata": {"truncated": False}}),
            ("session.tool.success", {"id": "call_a", "metadata": {"truncated": False}}),
            ("session.tool.success", {"id": 7, "metadata": {}}),  # unkeyable id: nothing
        ]
    )
    assert lines == [
        f'{_G}[bmad] tool: read {{"path": "b.txt"}}{_R}\n',
        f'{_G}[bmad] tool: write {{"path": "out.txt", "content": "hello\\n"}}{_R}\n',
    ]


def test_v2_trace_skips_deltas(tmp_path):
    """V2 trace records keep V1's `{seq, type, properties, ts}` shape with the
    frame's `data` as `properties`, and skip the per-token streams (the text
    each `*.ended` frame already carries whole). ABLATION: empty V2's
    `trace_skip_types` and the delta records appear."""
    from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter, _ServerSession

    sess = _ServerSession(
        process=None, port=0, base_url="", password="", log_fh=None, protocol=V2Protocol()
    )
    sess.session_id = "ses_1"
    sess.event_fh = io.StringIO()
    adapter = OpencodeHttpAdapter.__new__(OpencodeHttpAdapter)  # _dispatch_sse reads only sess
    for etype in (
        "session.text.started",
        "session.text.delta",
        "session.reasoning.delta",
        "session.tool.input.delta",
        "session.compaction.delta",
        "session.text.ended",
    ):
        frame = {"id": "evt_1", "created": 1, "type": etype, "data": {"sessionID": "ses_1", "n": 1}}
        adapter._dispatch_sse(sess, frame)
    records = [json.loads(line) for line in sess.event_fh.getvalue().splitlines()]
    assert [r["type"] for r in records] == ["session.text.started", "session.text.ended"]
    assert [r["seq"] for r in records] == [1, 2]
    assert records[0]["properties"] == {"sessionID": "ses_1", "n": 1}
    assert set(records[0]) == {"seq", "type", "properties", "ts"}
