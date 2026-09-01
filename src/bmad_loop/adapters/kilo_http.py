"""Kilo HTTP adapter — drives the Kilo CLI (``kilocode``, an OpenCode fork)
over its local HTTP/SSE server, the same transport as :mod:`.opencode_http`.

The Kilo fork renames the OpenCode contract in exactly two ways that matter to
the HTTP adapter:

* It reads ``KILO_*`` env vars, NOT the ``OPENCODE_*`` vars the bundled
  ``opencode-http`` adapter injects. With ``OPENCODE_SERVER_PASSWORD`` set,
  `kilo serve` still warns "KILO_SERVER_PASSWORD is not set; server is
  unsecured" and starts unauthenticated; ``OPENCODE_CONFIG_CONTENT`` is
  likewise ignored. So the OpenCode adapter's auth + config + hermetic-skills
  injection would silently no-op against a kilo server.
* Its server basic-auth username is ``kilo``, not ``opencode`` (verified:
  ``-u kilo:<pw>`` authenticates, ``-u opencode:<pw>`` 401s).

Everything else — the HTTP/SSE API surface (``GET /global/health``,
``POST /session``, ``POST /session/:id/prompt_async``, ``GET /event``,
``GET /session/status``, ``POST /session/:id/abort``), the SSE event frame
shapes, and the message/usage schemas — is byte-identical to OpenCode, because
Kilo is a rename of the same codebase. Verified live: health returns
``{"healthy": true, "version": "7.5.5"}`` and ``POST /session`` returns the
same ``{id, slug, ...}`` shape the base adapter consumes.

So this module subclasses the OpenCode adapters and overrides only the two
facts the rename changed: the env-var prefix and the auth username. It is
selected by the bundled ``kilo`` profile (``data/profiles/kilo.toml``) via the
``kilo-http`` adapter kind registered in :mod:`.registry`.
"""

from __future__ import annotations

import os
from typing import Any

from ..bmadconfig import ProjectPaths
from .base import SessionHandle, SessionSpec
from .generic import _DevSynthesisMixin
from .opencode_http import (
    OpencodeHttpAdapter,
    OpencodeServerError,
    _parse_sse_lines,
    _ServerSession,
)

# Kilo's basic-auth username (the base module's AUTH_USER is "opencode"; kilo
# rejects that and accepts the bare "kilo" username with the shared password).
KILO_AUTH_USER = "kilo"

# Env-var names kilo reads, mirroring opencode's OPENCODE_* contract.
KILO_DISABLE_EXTERNAL_SKILLS = "KILO_DISABLE_EXTERNAL_SKILLS"
KILO_SERVER_PASSWORD = "KILO_SERVER_PASSWORD"
KILO_CONFIG_CONTENT = "KILO_CONFIG_CONTENT"


class KiloHttpAdapter(OpencodeHttpAdapter):
    """opencode-http adapter tuned for the Kilo CLI.

    The base ``OpencodeHttpAdapter`` injects ``OPENCODE_*`` env vars and
    authenticates with the literal ``AUTH_USER`` ("opencode"); both are wrong
    for kilo. The module-level ``AUTH_USER`` is not a class attribute, so we
    override ``_session_env`` (KILO_* names) and the two places the base builds
    an authenticated httpx client (``_make_client`` and the SSE reader) to use
    our own ``kilo`` username.
    """

    def _session_env(self, spec: SessionSpec, password: str) -> dict[str, str]:
        return {
            **os.environ,
            **self.profile.env,
            **spec.env,
            KILO_DISABLE_EXTERNAL_SKILLS: "1",
            KILO_SERVER_PASSWORD: password,
            KILO_CONFIG_CONTENT: self._config_content(spec),
        }

    def _make_client(self, sess: _ServerSession):
        return self._httpx.Client(
            base_url=sess.base_url,
            auth=(KILO_AUTH_USER, sess.password),
            timeout=self._httpx.Timeout(10.0, connect=5.0),
        )

    def _sse_loop(self, sess: _ServerSession) -> None:
        httpx = self._httpx
        while not sess.sse_stop.is_set():
            try:
                with httpx.Client(
                    base_url=sess.base_url,
                    auth=(KILO_AUTH_USER, sess.password),
                    timeout=httpx.Timeout(5.0, read=self.sse_read_timeout_s),
                ) as client:
                    with client.stream("GET", "/event") as resp:
                        if resp.status_code != 200:
                            raise OpencodeServerError(f"/event -> {resp.status_code}")
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


class KiloDevAdapter(_DevSynthesisMixin, KiloHttpAdapter):
    """Dev/review adapter for the bundled ``bmad-build-auto`` skill over kilo.

    Mirrors ``OpencodeDevAdapter`` (the dev primitive writes no ``result.json``;
    :class:`_DevSynthesisMixin` synthesizes it from the spec on disk) but
    composes :class:`KiloHttpAdapter` so the kilo env/auth deltas carry through.
    """

    def __init__(self, *args, paths: ProjectPaths, **kwargs):
        super().__init__(*args, **kwargs)
        self.paths = paths
        self._configure_dev_knobs()
        self._server_procs: dict[str, Any] = {}

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        handle = super().start_session(spec)
        self._server_procs[spec.task_id] = self._sessions[spec.task_id].process
        return handle

    def _probe_alive(self, handle: SessionHandle) -> bool | None:
        proc = self._server_procs.get(handle.task_id)
        if proc is None:
            return False
        return proc.poll() is None
