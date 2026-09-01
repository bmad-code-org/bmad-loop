"""KiloHttpAdapter: unit tests for the two kilo-specific deltas from the OpenCode
HTTP family — the ``KILO_*`` session env prefix and the ``kilo`` basic-auth
username — plus the bundled ``kilo`` profile and the ``kilo-http`` adapter kind.

Kilo is an OpenCode fork whose HTTP/SSE surface (``/global/health``,
``/session``, ``/event``, …) is byte-identical to OpenCode's, so the full
transport behavior is already exercised by ``test_opencode_http.py``; this module
pins only what the rename changed. No real kilo binary is needed — these tests
exercise the class methods directly.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from bmad_loop.adapters import kilo_http
from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.kilo_http import (
    KILO_AUTH_USER,
    KILO_CONFIG_CONTENT,
    KILO_DISABLE_EXTERNAL_SKILLS,
    KILO_SERVER_PASSWORD,
    KiloDevAdapter,
    KiloHttpAdapter,
)
from bmad_loop.adapters.profile import get_profile
from bmad_loop.adapters.registry import get_adapter_kind, known_adapter_kinds
from bmad_loop.policy import LimitsPolicy, Policy


def _policy(**limits) -> Policy:
    return Policy(limits=LimitsPolicy(**limits) if limits else LimitsPolicy())


def make_adapter(tmp_path: Path, binary: str = "kilo", **kwargs) -> KiloHttpAdapter:
    adapter = KiloHttpAdapter(
        run_dir=tmp_path / "run",
        policy=kwargs.pop("policy", _policy()),
        profile=get_profile("kilo"),
        binary=binary,
        **kwargs,
    )
    adapter.health_timeout_s = 10.0
    adapter.health_poll_s = 0.05
    adapter.reconnect_sleep_s = 0.05
    return adapter


# --------------------------------------------------------------- the profile


def test_kilo_profile_selects_kilo_http(tmp_path):
    """The bundled `kilo` profile drives the kilo binary over the kilo-http kind,
    hookless, with the same hermetic-skills tree as opencode."""
    profile = get_profile("kilo")
    assert profile.name == "kilo"
    assert profile.binary == "kilo"
    assert profile.adapter == "kilo-http"
    assert profile.skill_tree == ".claude/skills"
    assert profile.hooks.dialect == "none"


def test_kilo_is_a_bundled_hookless_adapter_kind(tmp_path):
    """kilo-http registers as a builtin hookless kind (no multiplexer), and its
    lazy thunk resolves to the kilo classes."""
    assert "kilo-http" in known_adapter_kinds()
    kind = get_adapter_kind("kilo-http")
    assert kind.needs_mux is False
    builder = kind.load()
    assert builder.plain is KiloHttpAdapter
    assert builder.dev is KiloDevAdapter
    assert builder.construct_error == (kilo_http.OpencodeServerError,)


# --------------------------------------------------------- the kilo deltas


def test_kilo_session_env_uses_kilo_prefix(tmp_path):
    """The kilo fork reads KILO_* env vars, not OPENCODE_*. The adapter injects
    the KILO_* names — and crucially NOT the OPENCODE_* names kilo ignores."""
    adapter = make_adapter(tmp_path)
    spec = SessionSpec(
        task_id="t", role="triage", prompt="p", cwd=tmp_path, env={"BMAD_LOOP_TASK_ID": "t"}
    )
    env = adapter._session_env(spec, "sekrit")
    assert env["BMAD_LOOP_TASK_ID"] == "t"
    assert env[KILO_SERVER_PASSWORD] == "sekrit"
    assert env[KILO_DISABLE_EXTERNAL_SKILLS] == "1"
    json.loads(env[KILO_CONFIG_CONTENT])  # valid JSON
    # the OPENCODE_* pair kilo ignores must NOT be present
    assert "OPENCODE_SERVER_PASSWORD" not in env
    assert "OPENCODE_CONFIG_CONTENT" not in env


def test_kilo_config_content_hermetic_skills(tmp_path):
    """The injected KILO_CONFIG_CONTENT carries the blanket permission allow, the
    hermetic project skill tree, and the model when the policy sets one."""
    adapter = make_adapter(tmp_path)
    spec = SessionSpec(task_id="t", role="triage", prompt="p", cwd=tmp_path)
    config = json.loads(adapter._config_content(spec))
    assert config["permission"] == "allow"
    assert config["skills"]["paths"] == [str(tmp_path / ".claude" / "skills")]
    assert "model" not in config

    spec_model = SessionSpec(
        task_id="t", role="triage", prompt="p", cwd=tmp_path, model="anthropic/claude-x"
    )
    config = json.loads(adapter._config_content(spec_model))
    assert config["model"] == "anthropic/claude-x"


def test_kilo_make_client_authenticates_as_kilo(tmp_path):
    """Kilo's server basic-auth accepts the username `kilo`, not `opencode`. The
    adapter's client (used for health + POST requests) must send `kilo`."""
    adapter = make_adapter(tmp_path)
    sess = kilo_http._ServerSession.__new__(kilo_http._ServerSession)
    sess.base_url = "http://127.0.0.1:9999"
    sess.password = "sekrit"
    client = adapter._make_client(sess)
    auth = client._auth
    assert auth is not None
    req = client.build_request("GET", "http://127.0.0.1:9999/global/health")
    authed = list(auth.sync_auth_flow(req))[0]
    expected = "Basic " + base64.b64encode(b"kilo:sekrit").decode()
    assert authed.headers["Authorization"] == expected


def test_kilo_auth_user_is_not_opencode():
    assert KILO_AUTH_USER == "kilo"
    assert KILO_AUTH_USER != "opencode"
