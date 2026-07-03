"""opencode driver stub — proves the adapter seam for a non-tmux transport.

opencode is architecturally client/server: its TUI is just a client of a local
HTTP server (`opencode serve`), which exposes an OpenAPI 3.1 API —
POST /session, POST /session/:id/prompt_async, POST /session/:id/command,
POST /session/:id/abort — plus SSE event streams at GET /event. An adapter can
therefore drive sessions entirely over HTTP (injection="http",
observation="sse") with no tmux and no hook scripts, while a human can still
attach the TUI to watch the same session.

Implementation notes for whoever picks this up:
- start_session: POST /session, then POST /session/:id/command to run the
  skill (opencode commands/skills live in .opencode/commands|skills/), or
  prompt_async with the same "/skill args" text.
- wait_for_completion: subscribe to GET /event SSE; `session.idle` is the
  turn-complete signal (no Stop-hook needed). The result.json contract is
  unchanged — the skill still writes it; read it on idle.
- state: session/message/part JSON under ~/.local/share/opencode/storage/;
  token usage is in the message JSON.
- env: pass BMAD_LOOP_* via the server process or per-session env support.
"""

from __future__ import annotations

from .base import CodingCLIAdapter, SessionHandle, SessionResult, SessionSpec


class OpencodeHttpAdapter(CodingCLIAdapter):
    name = "opencode-http"
    injection = "http"
    observation = "sse"
    state = "local-json-tree"

    def __init__(self, base_url: str = "http://127.0.0.1:4096"):
        self.base_url = base_url
        raise NotImplementedError("opencode-http adapter is a design stub — see module docstring")

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        raise NotImplementedError

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        raise NotImplementedError
