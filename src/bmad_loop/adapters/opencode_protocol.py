"""OpenCode wire protocol: the per-major server API codecs the opencode-http
adapter drives, and the ``--version`` detection that picks one.

Wire codecs only — no process work, no sinks. Every method takes the control
``httpx.Client`` (or a decoded frame) from the adapter, so the transport in
:mod:`.opencode_http` (one ``opencode serve`` per session, its argv, port,
password, teardown and the stall/budget/timeout state machine) is shared by
every major and only what crosses the wire differs.

Version detection. ``opencode --version`` prints the bare ``1.19.0`` on V1 and
``opencode v2.0.27`` on V2; :func:`parse_opencode_version` reads the first
``X.Y.Z`` either way. :data:`SUPPORTED` maps each major the adapter speaks to
the earliest version probed live, and :func:`support_verdict` is the single
judgement both the runtime (refuse at spawn) and ``bmad-loop validate`` (warn)
derive from. Verified ranges: V1 1.18.2–1.19.x, V2 2.0.21–2.0.27 — the live
gates (``tests/test_opencode_live*.py``) passed against 1.19.0 and 2.0.27 on
2026-10-12.

V1 (``opencode-ai`` 1.x, verified 1.18.2–1.19.x) — :class:`V1Protocol`.

API contract — every fact below was pinned live against the real 1.18.2
binary (2026-07-16; the full probe record and the archived OpenAPI spec live
in git history, commit b85d8ca / PR #167). This list wins over memory:

- Readiness target is ``GET /global/health`` (``/health`` is the web-UI SPA
  shell). With ``OPENCODE_SERVER_PASSWORD`` set *every* endpoint 401s, health
  included — basic-auth username is literally ``opencode``; Bearer is
  rejected.
- SSE ``GET /event``: flat ``data:``-only frames, first frame
  ``server.connected``, ``server.heartbeat`` ≈ every 10 s, no server-side
  filtering — clients filter by ``properties.sessionID``.
- ``session.idle`` fires even when aborting an idle session, so an idle event
  alone is never proof a turn ran — it must pair with an assistant message
  whose ``time.completed`` post-dates the last prompt sent.
- Poll fallback ``GET /session/status`` returns a ``{sessionID: status}`` map
  in which idle sessions are simply **absent** — the rule is "absent ⇒ not
  busy", never "wait for idle".
- Per-prompt ``model`` is the object form ``{providerID, modelID}``; the
  ``"provider/model"`` string form belongs only to the config-file ``model``
  key.
- ``OPENCODE_CONFIG_CONTENT`` outranks the project's ``opencode.json``
  (applied as a final local-scope merge over all config files).
- Hermetic skills need ``OPENCODE_DISABLE_EXTERNAL_SKILLS=1`` **plus**
  ``skills.paths=["<worktree>/.claude/skills"]`` inside the config content —
  by default every server also sees the operator's personal
  ``~/.claude/skills`` and ``~/.agents/skills``.
- ``opencode serve`` survives parent SIGKILL (reparents to init and keeps
  serving); SIGTERM exits it cleanly.
- All ``time.*`` fields are epoch **milliseconds**; ``POST /session/:id/abort``
  returns ``200 true`` even when nothing is running.

``/event`` frame types the run-log renderer reads — pinned by a second live
probe of 1.18.2 (2026-07-25, 388 frames over 5 turns across two servers; the
full evidence table is posted on PR #279). Cross-checked against the server's
own OpenAPI at ``GET /doc``, whose ``Event`` union has 89 variants and is the
static contract — worth re-dumping on any version bump:

- ``message.updated`` — turn metadata only, no text. Carries ``info.id`` +
  ``info.role``; **re-emits out of order** (a stale re-emit for the user
  message lands mid-assistant-turn), so role must be keyed by message id, never
  tracked as "last role seen".
- ``message.part.updated`` — **complete-once, NOT cumulative.** A part fires
  exactly twice: once at creation with ``text: ""``, then once carrying the
  full final text. Held at 4 / 711 / 5689 chars (2 fires each, 1 non-empty).
  Rendering on this frame therefore yields one clean line per statement, with
  no de-duplication needed. **Tool execution also surfaces here** — and only
  here — as ``part.type == "tool"`` with ``part.tool`` and a ``state`` whose
  ``status`` steps pending → running → terminal (``state.input`` throughout,
  ``state.output`` on ``completed``, ``state.error`` on ``error``). Tool parts
  carry no ``part.text``, so the tool branch must sit ABOVE the empty-text
  bail-out in ``_inline_line`` to be reachable at all. ``ToolState`` is a
  4-variant union (pending / running / completed / error), so rendering on the
  terminal pair is exhaustively once-per-tool-call; ``error`` is included from
  the static contract (no tool failed during the live probe) so a failed call
  cannot vanish from the transcript.
- ``message.part.delta`` — the separate per-token stream. At 5689 chars its 50
  deltas concatenate **byte-exactly** to the single ``part.updated`` text, so
  it is pure redundancy: never rendered inline, and excluded from the SSE trace
  (``logs/`` is never trimmed by retention).
- ``command.executed`` — the **slash-command** surface, kept as that and nothing
  more. It does **not** fire for agent tool use: 0 frames across live ``bash`` /
  ``write`` / ``read`` / ``edit`` calls. Payload is ``name`` / ``arguments`` /
  ``messageID`` / ``sessionID`` (all required, so it needs no allowlist).
- ``file.edited`` — payload is exactly ``{"file": "/abs/path"}``, schema-pinned
  ``additionalProperties: false`` over that one key, so it carries **no
  ``sessionID``** and the ``properties.sessionID`` filter would drop it before
  either sink. It is reachable only through the explicit ``_SESSIONLESS_TYPES``
  allowlist, sound here only because bmad-loop runs one server per session.
  ``file.watcher.updated`` is session-less too and deliberately NOT allowlisted
  (see that constant).
- ``permission.asked`` — the ask frame. There is **no ``permission.updated``** in
  1.18.2. Payload is a ``permission`` **string** plus ``patterns`` /
  ``metadata`` / ``always`` / ``tool`` / ``id`` / ``sessionID`` — not a nested
  object with ``type`` / ``pattern``.
- ``permission.replied`` — carries **``reply``**, not ``response`` (plus
  ``sessionID`` / ``requestID``).
- ``permission.v2.asked`` / ``permission.v2.replied`` exist in the union and are
  **deliberately not consumed**: neither was seen live, and the v2 ask is a
  different payload (``action`` / ``resources`` / ``save`` / ``metadata`` /
  ``source``), so a branch for it would be written from the schema alone —
  exactly the guess that made the three corrections above necessary. The v2
  reply is shape-identical to the v1 one, but consuming it without the ask would
  log a decision with no request. Add both together once a live sighting pins
  what ``action`` / ``resources`` actually hold.
- There is **no ``tool.call`` / ``tool.response``** on this surface —
  confirmed both statically in the 89-variant union and live.

V2 (``@opencode/cli`` 2.x, verified 2.0.21–2.0.27) — :class:`V2Protocol`.

A separate npm package whose installer replaces the V1 binary (``opencode``,
plus ``opencode2``). API contract — pinned live against 2.0.27 (2.0.21
identical on every route below), zero tokens against a local mock provider
(2026-10-12):

- ``opencode serve --port N --hostname 127.0.0.1 --print-logs`` is unchanged;
  basic auth (username ``opencode``) still guards every ``/api/*`` route
  (``401 {"_tag":"UnauthorizedError",…}``). Each major answers the OTHER
  major's routes with the web-UI HTML shell (``200 text/html``), so each health
  endpoint returns JSON only on its own major.
- Health is ``GET /api/info`` → ``{"version":"2.0.27","pid",…}`` (a string
  version). The OpenAPI is ``GET /openapi.json``.
- ``OPENCODE_CONFIG_CONTENT`` is honored. Native keys: ``permissions`` (an
  ordered rule list ``{action, resource, effect}``, **last match wins**),
  ``skills`` (a list of paths), ``model`` (``"provider/model"``). V1 keys are
  normalized in memory, but the adapter writes V2's own.
- **No ``OPENCODE_DISABLE_EXTERNAL_SKILLS``**: the binary does not contain the
  string, and config discovery always scans ``~/.claude`` and ``~/.agents``.
  The only hermetic lever is the ``skill`` rules: deny ``*``, then allow each
  project skill by **id = the skill's directory name** (``dir-x`` holding
  ``name: name-y`` lists as ``{"id":"dir-x","name":"name-y",…}``; a rule naming
  ``name-y`` re-allows nothing). With those rules the skills advertised to the
  model are exactly the project's, and loading a personal or built-in
  (``opencode``, ``report``) skill fails ``permission.rejected``. **``GET
  /api/skill`` is NOT filtered by the rules** — it lists every discovered skill
  either way (it loads lazily per ``location``: the first call can be ``[]``),
  so hermeticity is checked on what the model is offered, never on that route.
  An id allowed by name resolves to the PROJECT copy: the registry keeps one
  skill per id, and the configured ``skills`` paths are applied after the
  ``~/.claude`` / ``~/.agents`` / ``~/.config/opencode`` roots (each scanned
  sequentially, sorted). A same-id personal copy in all three roots lost in
  every start, with either side's scan slowed by 2000 extra skills.
- A ``question`` deny removes the ``question`` tool from what the model is
  offered, so an unattended session cannot park on a form. Reads are not
  gated: under the allow-all config a ``read`` of a path outside the project
  (``/etc/hostname``) and of ``.env`` both succeed with no ``permission.asked``
  frame (``external_directory`` is a real action — an ``ask`` rule for it does
  park the outside read — but nothing gates it by default).
- Session create: ``POST /api/session {"title","location":{"directory"}}`` →
  ``200 {"data": Session.Info}``, the id at ``data.id``.
- Prompt: ``POST /api/session/{id}/prompt {"text"}`` → ``200 {"data": <user
  inbox item>}``; the body has no model or variant field. An unknown session
  is ``404 {"_tag":"SessionNotFoundError",…}``; ``409 ConflictError`` exists.
- Interrupt: ``POST /api/session/{id}/interrupt`` → ``{"interrupted": bool}``
  (``false`` on an idle session).
- SSE ``GET /api/event``: frames are ``{"id","created","type","data":{…}}`` —
  the session id is ``data.sessionID``. The first frame is
  ``server.connected`` (``data: {}``). The heartbeat is an SSE **comment line**
  ``: heartbeat``, once right after connect and then every 15 s — not a data
  frame. The turn end is ``session.execution.{succeeded, failed,
  interrupted}``; ``session.idle`` (deprecated in the schema) and
  ``session.status`` never fired. No session-less frame (no ``file.edited``)
  was seen.

Poll fallback, usage, transcript and effort — pinned live against 2.0.27
(2026-10-12), same zero-token mock setup:

- ``GET /api/session/active`` → ``{"data": {<sid>: {"type": "running"}}}``;
  ``running`` is the schema's only type. A session is listed from the prompt
  until its turn ends — **including every ``session.retry.scheduled``
  backoff** (a provider on a closed port retried ~10 times over ~86 s, listed
  throughout) — and is absent before its first prompt and after any turn end
  (no idle type). A retry run that gives up ends ``session.step.failed`` →
  ``session.execution.failed {error}``.
- ``GET /api/session/{id}/message`` → ``{"data": [...], "cursor": {"previous",
  "next"}}``, newest-first, 50 per page by default. ``limit`` is 1..200 and
  out-of-range is a 400 (never clamped); ``order`` is ``asc|desc`` only;
  ``?cursor=<next>`` walks on (cursors encode their order — combining
  ``cursor`` with ``order`` is a 400 ``InvalidCursorError``). A full last page
  still carries a ``next``: the walk ends on an EMPTY page whose cursors are
  both null. Message types seen: ``user`` (``text`` top-level), ``assistant``
  (``time{created,streamed,completed}``, ``finish``, ``tokens``, ``error``?)
  and ``idle`` (``outcome``). An unknown session is ``404
  SessionNotFoundError`` here and on ``GET /api/session/{id}``.
- ``Session.Info.tokens`` is the cumulative session aggregate (the sum over
  steps), all zero until the first step ends — an in-flight first step is not
  even listed as a message until its first chunk streams. The step an
  interrupt cancels is stamped ``time.completed`` with ``finish: "error"``,
  ``error: {"type": "aborted"}`` and no ``tokens``; a provider failure that
  exhausts its retries is stamped the same way with the provider's error type
  (e.g. ``provider.transport``). Interrupt itself adds no tokens. It counts
  the session's OWN steps only: a ``subagent`` tool call runs a child session
  (``Session.Info.parentID`` = the parent; absent on a root) with its own
  aggregate. No children route exists — ``GET /api/session?parentID=<id>``
  lists the direct children, paged like the messages (the cursor carries the
  ``parentID``), and nesting is configurable (``experimental.subagent_depth``).
  A ``background: true`` child can still be on ``/api/session/active`` after
  its parent's turn ends (pinned 2.0.32).
- Effort: the prompt body has no variant; the session's model ref
  ``{"id", "providerID", "variant"}`` carries it, set at create. Create
  accepts ANY variant (and any model) with a 200 and echoes it in
  ``data.model.variant``; an unknown one fails only at prompt time, before
  any provider request, as ``session.execution.failed`` with ``error.type
  provider.no-route`` ("Variant unavailable for <provider>/<model>: <name>").
  A known one changes the provider request (``high`` on the
  openai-compatible package adds ``reasoning_effort: "high"``). Each model
  lists its ``variants`` (``[{"id", "settings"?, "body"?}]``; the
  openai-compatible package supplies ``low|medium|high`` when the model
  declares none, and a declared list replaces them) on ``GET /api/model`` and
  ``GET /api/model/default`` — both take the deepObject query
  ``location[directory]=<abs>`` (a bare ``directory=`` is silently ignored)
  and answer ``{"location", "data"}``, ``data`` being null on the default
  route when no model is usable. The config's ``"provider/model#variant"``
  form parses but is NOT applied to a session created without a model.
- With no model configured, V2's default is the hosted ``opencode`` provider
  (credential-free), not a failure — the operator's own config decides the
  model, exactly as on V1.

Inline transcript and provider faults — pinned live against 2.0.27
(2026-10-12), same zero-token mock setup, the mock answering a 429 rate limit
(with and without ``retry-after``), a 429 and a 402 ``insufficient_quota``,
and a closed port:

- Every session frame's payload is under ``data`` with its ``sessionID``
  (``permission.*`` included). The user prompt is
  ``session.inbox.enqueued {item: {type: "user", payload: {text}}}``. Text
  and reasoning stream as ``session.{text,reasoning}.{started,delta,ended}``;
  ``.ended`` carries the complete ``text`` once (``ordinal`` numbers the
  parts). Reasoning fires for both the ``reasoning_content`` and the
  ``reasoning`` stream keys.
- A tool call is ``session.tool.input.started {id, name}`` →
  ``.input.ended {id, text}`` (the raw argument JSON) → ``session.tool.called
  {id, input}`` → ``session.tool.progress`` → exactly one of
  ``session.tool.success {id, content, metadata}`` / ``session.tool.failed
  {id, error {type, message}}``, joined by the call ``id``. A shell command
  exiting non-zero is a SUCCESS with ``metadata.exit`` set; ``.failed`` was
  seen only for a rejected permission (``error.type permission.rejected``,
  the reply's message as ``error.message``), and that turn still ended
  ``succeeded``.
- An ``ask`` rule parks the call on ``permission.asked {id, sessionID,
  action, resources, save, source {type: "tool", messageID, id}}`` (e.g.
  ``action: "shell"``, ``resources: ["echo hi"]``); the reply is ``POST
  /api/session/{id}/permission/{requestID}/reply {"decision":
  "once"|"always"|"reject", "message"?}`` → 204, then ``permission.replied
  {sessionID, requestID, reply}``. No ``form.*`` frame fired (the adapter's
  ``question`` deny keeps forms away).
- Forms — pinned live against 2.0.32 (2026-10-28) by lifting that deny in a
  probe and having the mock call the ``question`` tool. The call parks on
  ``form.created {form: {id, sessionID, title, metadata {kind: "question",
  tool {messageID, id}}, fields}}``, the one frame whose session id is
  nested (``data.form.sessionID``, no top-level ``sessionID``). The tool maps
  each question to a field ``{key: "q<n>", title: <header>, description:
  <question>, type: "string"|"multiselect", options [{value, label,
  description}], custom}``. The reply is ``POST /api/session/{id}/form/
  {formID}/reply {"answer": {<key>: value}}`` → 204, then ``form.replied {id,
  sessionID, answer}`` and the tool's ``session.tool.success``. ``DELETE
  /api/session/{id}/form/{formID}`` → 204 and an interrupt both end it
  ``form.cancelled {id, sessionID}``, then ``session.tool.failed`` with
  ``error.type aborted`` ("The user dismissed this question" / "Tool
  execution interrupted"). The adapter renders all three and never replies.
- A provider failure surfaces ONLY as frames, each with a structured
  ``error {type, message, status?, response? {body}}``: ``session.retry.
  scheduled {attempt, at, error}`` per backoff (attempts 2..11, ~2/4/8 s then
  ~10 s, ``retry-after`` ignored), then ``session.step.failed {error}`` and
  ``session.execution.failed {error}``. Observed types: a 429 rate limit is
  ``provider.rate-limit`` (``status 429``, retried 10 times, ~80–88 s); a
  quota refusal is ``provider.quota`` (``status`` 429 or 402, NOT retried —
  fails in ~0.3 s); a refused connection is ``provider.transport`` (no
  ``status``, no ``response``; retried 10 times, ~87 s). On 2.0.32 a 5xx is
  ``provider.internal`` (retried 10 times), a 401/403 ``provider.auth``, a
  400 ``provider.invalid-request``, a session model with no route
  ``provider.no-route`` (no provider request; only ``session.execution.
  failed``) and a malformed stream ``provider.invalid-output`` — none
  retried but a stream cut before its ``finish_reason``. A hung provider
  fires nothing unless a provider ``timeout``/``headerTimeout``/
  ``chunkTimeout`` setting is set, then reads ``provider.transport``
  ("Timed out waiting for response headers"/"… data"). ``error.message`` is
  the provider's own message verbatim and ``response.body`` its raw body —
  provider-written text, so the adapter copies only the bounded message, last
  on its line (:func:`format_provider_error`), never the body. The server log
  carries no line per retry; only the final give-up logs one ``level=ERROR
  message="Failed to drain Session"`` line, too late for a run killed
  mid-retry, so it is not what the classifier reads — and written for every
  failed execution, whatever its cause, with no error type.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..model import TokenUsage
from .base import SessionSpec

# Earliest version probed live per supported major — `validate` warns below it.
# A major missing here is refused at spawn: its server API is unknown to the
# adapter, and talking V1 routes at it is what produced #847's opaque timeout.
SUPPORTED: dict[int, tuple[int, int, int]] = {1: (1, 18, 0), 2: (2, 0, 21)}

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class OpencodeServerError(Exception):
    """An ``opencode serve`` instance could not be spawned, readied or driven."""


def parse_opencode_version(text: str) -> tuple[int, int, int] | None:
    """The first ``X.Y.Z`` in ``opencode --version`` output, or None when there
    is none. V1 prints ``1.19.0``, V2 ``opencode v2.0.27``."""
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    major, minor, patch = (int(group) for group in match.groups())
    return major, minor, patch


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def supported_ranges() -> str:
    """Human summary of :data:`SUPPORTED`, e.g. ``1.x (>= 1.18.0), 2.x (>= 2.0.21)``."""
    return ", ".join(
        f"{major}.x (>= {format_version(floor)})" for major, floor in sorted(SUPPORTED.items())
    )


def support_verdict(version: tuple[int, int, int]) -> tuple[str, str]:
    """``(verdict, message)`` for a parsed version: ``"ok"``, ``"below-floor"``
    (a supported major older than the earliest version probed — it may work, so
    the runtime proceeds and validate warns) or ``"unsupported-major"`` (a server
    API the adapter does not speak — the runtime refuses it). The message names
    the version found and the supported ranges."""
    found = format_version(version)
    floor = SUPPORTED.get(version[0])
    if floor is None:
        return (
            "unsupported-major",
            f"opencode {found} speaks a server API this bmad-loop does not support "
            f"(supported: {supported_ranges()})",
        )
    if version < floor:
        return (
            "below-floor",
            f"opencode {found} is older than {format_version(floor)}, the earliest "
            f"{version[0]}.x version verified (supported: {supported_ranges()})",
        )
    return "ok", f"opencode {found} → V{version[0]} server API"


@dataclass(frozen=True)
class HealthProbe:
    """One health-endpoint answer. ``html`` marks a ``200 text/html`` reply: the
    web-UI SPA shell, which each major serves for the OTHER major's routes — so
    an HTML answer on our own health route means the server speaks a different
    API than ``--version`` reported."""

    healthy: bool
    html: bool = False


def _is_html(resp: Any) -> bool:
    return resp.status_code == 200 and str(resp.headers.get("content-type", "")).lower().startswith(
        "text/html"
    )


def _sum_args(arguments: Any) -> str:
    """Compact one-line summary of an arbitrary event payload value (a tool
    part's ``state.input``, a ``permission.asked`` ``patterns``, a
    ``command.executed`` ``arguments``, a ``session.error`` ``error``) for the
    inline run log. Truncates long dicts/strings so a single line never explodes
    the tail view, and accepts any shape — these payloads are server-controlled
    and not all of them have a pinned schema."""
    if not arguments:
        return ""
    try:
        if isinstance(arguments, (dict, list)):
            text = json.dumps(arguments, ensure_ascii=False)
        else:
            text = str(arguments)
    except (TypeError, ValueError):
        text = str(arguments)
    if len(text) > 120:
        text = text[:119] + "…"
    return text


# ANSI colors for inline-log marker lines, keyed by message role. The bmad-loop
# TUI renders <task>.log through a pyte terminal emulator (tui/data.py LogView),
# which dispatches SGR to per-Char styles and maps pyte named colors via
# _rich_color straight through to Rich — so every [bmad] marker shows colored in
# the LogView. A plain `cat`/editor shows the escape bytes; the structured trace
# stays uncoloured in <task>.sse.jsonl. Keys are opencode message.info.role
# values; an unseen role falls back to _ROLE_COLOR_DEFAULT so it still renders
# distinctly — add it to the map to pin its hue.
_ROLE_COLORS = {
    "user": "\x1b[33m",  # SGR 33 = yellow
    "assistant": "\x1b[36m",  # SGR 36 = cyan
}
_ROLE_COLOR_DEFAULT = "\x1b[35m"  # SGR 35 = magenta — unseen roles
_TOOL_COLOR = "\x1b[32m"  # SGR 32 = green — tool/cmd/file/permission (role-less)
_RESET = "\x1b[0m"

# Tool-part statuses that end a tool call. `ToolState` is a 4-variant union in
# the 1.18.2 OpenAPI (`pending`, `running`, `completed`, `error`), so these two
# are exhaustively the terminal ones — a tool call reaches exactly one of them,
# which is what makes "render on terminal" equal to "one line per tool". The
# live probe only ever saw pending → running → completed (nothing failed during
# it); `error` comes from the static contract, and including it is what keeps a
# FAILED tool call from vanishing from the transcript entirely.
_TOOL_TERMINAL_STATES = frozenset({"completed", "error"})


def _role_color(role: str) -> str:
    """ANSI color for a message role; unseen roles fall back to magenta."""
    return _ROLE_COLORS.get(role, _ROLE_COLOR_DEFAULT)


# The provider-errors log (`logs/<task_id>.provider-errors.log`): one line per
# provider-error frame, authored by the adapter from the frame's STRUCTURED
# fields — what the env-fault classifier scans on V2, whose server log logs no
# retry at all (see the module docstring). Its patterns may anchor on the line's own prefix
# only because every byte before `message=` is adapter-chosen: the event name
# comes from the protocol's allowlist and each field is reduced to
# `_FIELD_SAFE`. The message is the one provider-sourced text, so it goes last,
# quoted, bounded, and escaped so it can never end the line (nor carry a CR the
# scanner would split on, nor an ANSI escape it would strip) — a forged
# `provider-error …` inside it stays mid-line, behind the anchor. The error's
# `response.body` is never copied.
PROVIDER_ERROR_MESSAGE_MAX = 400
_PROVIDER_ERROR_FIELD_MAX = 80
_FIELD_SAFE = re.compile(r"[^A-Za-z0-9._:/-]")


def _provider_error_field(value: Any) -> str:
    """One unquoted ``key=value`` value: an int or a string reduced to
    ``_FIELD_SAFE`` characters, ``-`` when absent or of another type."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return "-"
    text = _FIELD_SAFE.sub("_", str(value))[:_PROVIDER_ERROR_FIELD_MAX]
    return text or "-"


def _escape_message(text: str) -> str:
    """Backslash-escape ``text`` for a double-quoted single-line value: the
    quote, the backslash, and every control character (C0, DEL, C1 and the
    Unicode line/paragraph separators)."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20 or 0x7F <= code <= 0x9F or ch in "  ":
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


def format_provider_error(etype: str, props: Any) -> str:
    """``provider-error event=<etype> type=<error.type> status=<status>
    attempt=<n> message="<message>"`` for one provider-error frame, ``-`` for
    each absent field; the message is cut to ``PROVIDER_ERROR_MESSAGE_MAX``
    characters before escaping. Raises on a payload that is not an object."""
    error = props.get("error")
    if not isinstance(error, dict):
        error = {}
    message = error.get("message")
    message = message if isinstance(message, str) else ""
    if len(message) > PROVIDER_ERROR_MESSAGE_MAX:
        message = message[: PROVIDER_ERROR_MESSAGE_MAX - 1] + "…"
    return (
        f"provider-error event={_provider_error_field(etype)}"
        f" type={_provider_error_field(error.get('type'))}"
        f" status={_provider_error_field(error.get('status'))}"
        f" attempt={_provider_error_field(props.get('attempt'))}"
        f' message="{_escape_message(message)}"'
    )


class OpencodeProtocol:
    """One OpenCode server API: what the adapter sends and how it reads what
    comes back. One instance per server session, chosen by the major
    ``--version`` reported (:meth:`OpencodeHttpAdapter._protocol_for`).

    Network methods raise on a transport fault unless their docstring says
    otherwise; the adapter owns the guard, the crumb and the degrade, exactly as
    it did before the codec was split out."""

    major: int = 0
    # Frame types that prove only that the stream is alive: never activity.
    # Tuples, not frozensets — the frame `type` is server-controlled and may be
    # unhashable, and a tuple membership test compares rather than hashes.
    heartbeat_types: tuple[str, ...] = ()
    # Frame types with no session id that still belong to this session (see
    # the V1 `_SESSIONLESS_TYPES` note). Tested only once `type` is a str.
    sessionless_types: frozenset[str] = frozenset()
    idle_types: frozenset[str] = frozenset()  # frame types that queue "idle"
    error_types: frozenset[str] = frozenset()  # frame types that queue "error"
    trace_skip_types: frozenset[str] = frozenset()  # never written to the SSE trace
    # Frame types whose ``error`` the adapter copies into
    # ``logs/<task_id>.provider-errors.log`` (:meth:`provider_error_line`).
    provider_error_types: frozenset[str] = frozenset()
    event_path: str = ""
    # Whether :meth:`usage` is computed FROM the transcript (V1's token sum) or
    # read apart from it (V2's session aggregate): only the former loses the
    # usage when the transcript read fails.
    usage_reads_transcript: bool = True

    def config_content(self, spec: SessionSpec, skill_dir: Path) -> str:
        raise NotImplementedError

    def refused_skill_ids(self, skill_dir: Path) -> list[str]:
        """Project skill ids :meth:`config_content` left out of this
        session's hermetic skill set — the adapter crumbs them at spawn."""
        return []

    def extra_env(self) -> dict[str, str]:
        raise NotImplementedError

    def health(self, client: Any) -> HealthProbe:
        raise NotImplementedError

    def create_session(self, client: Any, spec: SessionSpec) -> str:
        raise NotImplementedError

    def prompt(self, client: Any, session_id: str, text: str, variant: str) -> None:
        raise NotImplementedError

    def frame_type(self, event: dict[str, Any]) -> Any:
        raise NotImplementedError

    def frame_payload(self, event: dict[str, Any]) -> tuple[Any, Any]:
        """``(props, session_id)`` of a frame; may raise on a malformed one."""
        raise NotImplementedError

    def render_line(self, msg_roles: dict[Any, Any], etype: str, props: Any) -> str | None:
        raise NotImplementedError

    def provider_error_line(self, etype: str, props: Any) -> str | None:
        """The ``provider-errors.log`` line for one of this session's frames, or
        None for a frame that carries no provider error. Raises on a payload
        that is not an object."""
        if etype not in self.provider_error_types:
            return None
        return format_provider_error(etype, props)

    def session_busy(self, client: Any, session_id: str) -> bool | None:
        raise NotImplementedError

    def latest_completed_ms(self, client: Any, session_id: str) -> int | None:
        raise NotImplementedError

    def abort(self, client: Any, session_id: str) -> None:
        raise NotImplementedError

    def transcript(self, client: Any, session_id: str) -> tuple[Any, str | None]:
        """``(messages, fault)``: the session's whole message history (dumped as
        ``messages.json``), or None with ``fault`` naming why the read failed.
        Never raises."""
        raise NotImplementedError

    def usage(
        self, client: Any, session_id: str, transcript: Any
    ) -> tuple[TokenUsage | None, UsageFault | None]:
        """The session's token usage at teardown, given the :meth:`transcript`
        already read. None is "untracked" when ``fault`` is None and "unknown"
        when it is not — never a zero standing in for a failed read (DW-461).
        Never raises."""
        raise NotImplementedError

    def sample_usage(self, client: Any, session_id: str) -> tuple[TokenUsage | None, str | None]:
        """``(usage, fault)``: the mid-session cumulative usage the budget guard
        samples on the heartbeat cadence; ``(None, None)`` when nothing is
        tallied yet, ``fault`` when the sample failed. Never raises."""
        raise NotImplementedError

    def active_descendants(
        self, client: Any, session_id: str
    ) -> tuple[list[str] | None, str | None]:
        """``(ids, fault)``: the child sessions :meth:`usage` counted that are
        still running at capture, so their landed tokens are a lower bound —
        or None with ``fault`` naming why that could not be read. A protocol
        that counts no child sessions has none. Never raises."""
        return [], None


@dataclass(frozen=True)
class UsageFault:
    """Why a usage read failed, as the ``usage-capture-failed`` crumb reports it
    (DW-461): ``stage`` is ``fetch`` (the request, its status or an undecodable
    body), ``payload`` (a body of the wrong shape) or ``children`` (V2's
    descendant-session walk, or a child's malformed ``Session.Info``)."""

    stage: str
    error: str


# ``/event`` frame types that carry NO ``properties.sessionID`` but are still
# attributed to this session, exempting them from the sessionID filter in
# `_dispatch_sse`. Sound ONLY because bmad-loop spawns one `opencode serve` per
# session (see the transport notes in opencode_http's module docstring): the
# server is single-tenant, so a server-global frame is unambiguously this
# session's. Any design that ever shares a server across sessions must empty
# this set.
#
# Deliberately an allowlist and not "allow every session-less frame": the live
# probe's session-less traffic was overwhelmingly noise that says nothing about
# the run — `plugin.added` alone was 90 of 388 frames, plus `server.heartbeat`,
# `catalog.updated`, `server.connected`, `reference.updated` and
# `integration.updated`. `file.watcher.updated` is excluded on purpose too: it
# is the editor-integration watcher firing for any change under the project
# (our own git operations, a build, an editor save), so it is not evidence the
# agent did anything, and for the changes the agent DID make it just
# double-reports what `file.edited` already carries.
_SESSIONLESS_TYPES = frozenset({"file.edited"})


class V1Protocol(OpencodeProtocol):
    """The ``opencode-ai`` 1.x server API — the contract in this module's
    docstring, pinned against 1.18.2."""

    major = 1
    heartbeat_types = ("server.heartbeat", "server.connected")
    sessionless_types = _SESSIONLESS_TYPES
    idle_types = frozenset({"session.idle"})
    error_types = frozenset({"session.error"})
    trace_skip_types = frozenset({"message.part.delta"})
    event_path = "/event"

    def config_content(self, spec: SessionSpec, skill_dir: Path) -> str:
        """The OPENCODE_CONFIG_CONTENT JSON for this session:
        blanket permission allow (the bypass-flags analogue), the hermetic
        skills path (project skills only, paired with
        OPENCODE_DISABLE_EXTERNAL_SKILLS=1 in the env), and the model when the
        policy sets one (config-file model is the "provider/model" string
        form)."""
        config: dict[str, Any] = {
            "permission": "allow",
            "skills": {"paths": [str(skill_dir)]},
        }
        if spec.model:
            config["model"] = spec.model
        return json.dumps(config)

    def extra_env(self) -> dict[str, str]:
        return {"OPENCODE_DISABLE_EXTERNAL_SKILLS": "1"}

    def health(self, client: Any) -> HealthProbe:
        resp = client.get("/global/health")
        if _is_html(resp):
            return HealthProbe(healthy=False, html=True)
        return HealthProbe(healthy=resp.status_code == 200 and resp.json().get("healthy") is True)

    def create_session(self, client: Any, spec: SessionSpec) -> str:
        resp = client.post("/session", json={"title": spec.task_id})
        if resp.status_code != 200:
            raise OpencodeServerError(f"POST /session failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()["id"]

    def prompt(self, client: Any, session_id: str, text: str, variant: str) -> None:
        """prompt_async; raises unless the server accepts it (204)."""
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        # Reasoning effort is a per-call PromptInput key (#643); the key is
        # omitted, not sent empty, so an effort-less session's body is unchanged.
        if variant:
            body["variant"] = variant
        resp = client.post(f"/session/{session_id}/prompt_async", json=body)
        if resp.status_code != 204:
            raise OpencodeServerError(f"prompt_async failed: {resp.status_code} {resp.text[:200]}")

    def frame_type(self, event: dict[str, Any]) -> Any:
        return event.get("type")

    def frame_payload(self, event: dict[str, Any]) -> tuple[Any, Any]:
        props = event.get("properties") or {}
        return props, props.get("sessionID")

    def render_line(self, msg_roles: dict[Any, Any], etype: str, props: Any) -> str | None:
        """The inline-log line for one frame, or None for a frame that renders
        nothing.

        Every event-type string below, and the payload shape each branch reads,
        is pinned in this module docstring's ``/event`` section against a live
        1.18.2 probe — read that before adding or changing a branch here. That
        is deliberately the only copy of those facts: three of this renderer's
        original branches named frames the server does not send, and a second
        copy is a second thing to get wrong on the next version bump.

        ``message.updated`` carries only turn metadata (notably ``info.role``
        keyed by ``info.id``) and renders no inline line of its own — it just
        records the role under the message id so the following
        ``message.part.*`` lines resolve the right speaker. Keying by message
        id (not "last role seen") is required because ``message.updated``
        frames arrive out of order: a re-emit for the user message lands mid-
        assistant-turn and would otherwise flip the prefix to ``user:`` on the
        assistant's own reply text. That also avoids a flood of role-only
        announce lines like ``[bmad] assistant: assistant``."""
        if etype == "message.updated":
            info = props.get("info") or {}
            mid = info.get("id")
            role = info.get("role")
            if mid and role:
                msg_roles[mid] = role
            return None
        role = "assistant"
        if etype == "message.part.updated":
            mid = (props.get("part") or {}).get("messageID")
            if mid and mid in msg_roles:
                role = msg_roles[mid]
        return self.inline_line(etype, props, role)

    def inline_line(self, etype: str, props: dict[str, Any], role: str) -> str | None:
        # ``message.updated`` is consumed in ``render_line`` (role refresh
        # only); it renders no line here.
        # assistant / user assembled text. Per-token ``message.part.delta``
        # frames are NOT rendered inline: they concatenate to exactly the text
        # ``message.part.updated`` already carries complete, so emitting both
        # duplicates every statement as a one-word-per-line flood. For the same
        # reason they are excluded from the SSE trace too (``trace_skip_types``);
        # nothing consumes deltas anywhere in this adapter.
        if etype == "message.part.updated":
            part = props.get("part") or {}
            # Tool execution rides this same frame as a `tool`-typed part —
            # there is no tool.call/tool.response event on this surface, and
            # `command.executed` never fires for it (both pinned live). Tool
            # parts carry no `text`, so this branch MUST sit above the
            # empty-text `return None` below or it can never be reached: that
            # exact ordering is the difference between rendering the agent's
            # actions and rendering nothing but its prose.
            if part.get("type") == "tool":
                return self.tool_line(part)
            text = part.get("text") or ""
            body = text.strip()  # drop surrounding blanks; keep internal structure
            if not body:  # empty/non-text part — no value live
                return None
            role = role or "assistant"
            # A blank line separates each turn from the previous one (the
            # separator sits ABOVE the header, body follows directly beneath),
            # and the role-colored marker line anchors the turn boundary.
            # Internal newlines in the body are preserved so multi-paragraph
            # reasoning reads as prose under the one header.
            return f"\n{_role_color(role)}[bmad] {role}:{_RESET}\n{body}\n"
        # The SLASH-COMMAND surface — NOT the tool surface. Agent tool use never
        # reaches here (0 `command.executed` frames across live bash/write/read/
        # edit calls); it renders from the `tool`-typed part above. Kept because
        # a slash command invoked through this server is still worth a line and
        # its payload is pinned (`name`/`arguments`/`messageID`, all required),
        # but do not "fix" tool rendering back into this branch.
        if etype == "command.executed":
            args = _sum_args(props.get("arguments"))
            return f"{_TOOL_COLOR}[bmad] cmd: {props.get('name') or '?'}{(' ' + args) if args else ''}{_RESET}\n"
        if etype == "file.edited":
            # Session-less: reachable only via `_SESSIONLESS_TYPES`.
            return f"{_TOOL_COLOR}[bmad] file: {props.get('file') or '?'}{_RESET}\n"
        # Permission surface. Field names are the live 1.18.2 ones: the ask frame
        # is `permission.asked` (there is no `permission.updated`) carrying a
        # `permission` STRING plus a `patterns` list — not a nested object with
        # `type`/`pattern` — and the reply carries `reply`, not `response`.
        # `metadata` (the concrete command) and `always` are left to the trace;
        # `patterns` is what the permission actually matched on.
        if etype == "permission.asked":
            pats = _sum_args(props.get("patterns"))
            return (
                f"{_TOOL_COLOR}[bmad] perm ask: {props.get('permission') or '?'}"
                f"{(' ' + pats) if pats else ''}{_RESET}\n"
            )
        if etype == "permission.replied":
            return f"{_TOOL_COLOR}[bmad] perm reply: {props.get('reply') or '?'}{_RESET}\n"
        # A failing turn is the one thing a reader most wants in the transcript:
        # without this the log just stops mid-turn while the queue put below
        # ends the session. Same green/tool hue as the other role-less markers —
        # the color family is "not a speaker", not "severity". `error` has no
        # pinned shape on this surface, so summarize whatever it carries rather
        # than reaching into fields that may not exist.
        if etype == "session.error":
            detail = _sum_args(props.get("error"))
            return f"{_TOOL_COLOR}[bmad] error:{(' ' + detail) if detail else ''}{_RESET}\n"
        return None

    @staticmethod
    def tool_line(part: dict[str, Any]) -> str | None:
        """One line for a finished tool call, or None while it is still running.

        A `tool`-typed ``message.part.updated`` part fires once per state
        transition — ``pending`` → ``running`` → terminal — carrying the SAME
        part id each time. Rendering every fire would print each tool call three
        times, so the line is emitted only on a terminal status
        (``_TOOL_TERMINAL_STATES``): a tool call reaches exactly one terminal
        state, which makes that gate exactly "once per tool call". The gate is
        the whole correctness argument here — a test that only asserts a tool
        renders at all passes with it removed.

        Same green/tool hue as the other role-less markers: a tool call is an
        action, not a speaker. ``state.input`` is summarized inline (that is the
        "what did the agent just do" the reader wants); ``state.output`` is
        deliberately NOT — it is a full command's stdout or a whole file's text
        on this surface, and the complete payload is in the SSE trace for anyone
        who needs it. A failure appends the error, because a tool that failed
        silently is exactly what a reader would go looking for."""
        state = part.get("state") or {}
        status = state.get("status")
        if status not in _TOOL_TERMINAL_STATES:
            return None
        args = _sum_args(state.get("input"))
        suffix = ""
        if status == "error":
            detail = _sum_args(state.get("error"))
            suffix = f" -> error{(': ' + detail) if detail else ''}"
        return (
            f"{_TOOL_COLOR}[bmad] tool: {part.get('tool') or '?'}"
            f"{(' ' + args) if args else ''}{suffix}{_RESET}\n"
        )

    def session_busy(self, client: Any, session_id: str) -> bool | None:
        """Tri-state /session/status probe: True = busy/retrying, False =
        provably settled (absent from the map or explicit idle),
        None = unknowable (unreachable server, non-200). Unknown is not
        settled: callers must not treat a failed probe as proof a turn ended
        (the liveness-parity doctrine); the process poll settles real death."""
        try:
            resp = client.get("/session/status")
            if resp.status_code != 200:
                return None
            status = resp.json().get(session_id) or {}
            return status.get("type") in ("busy", "retry")
        except Exception:  # probe is advisory
            return None

    def latest_completed_ms(self, client: Any, session_id: str) -> int | None:
        """The newest assistant message's ``time.completed`` (epoch ms; 0 when
        none has completed), or None when the read failed — advisory, never
        raises."""
        try:
            resp = client.get(f"/session/{session_id}/message")
            if resp.status_code != 200:
                return None
            completed = 0
            for msg in resp.json():
                info = msg.get("info") or {}
                if info.get("role") != "assistant":
                    continue
                done_ms = (info.get("time") or {}).get("completed") or 0
                completed = max(completed, int(done_ms))
        except Exception:  # probe is advisory
            return None
        return completed

    def abort(self, client: Any, session_id: str) -> None:
        client.post(f"/session/{session_id}/abort")

    def transcript(self, client: Any, session_id: str) -> tuple[Any, str | None]:
        """``(messages, fault)``: the raw message list, or None with ``fault``
        naming why the read failed (``HTTP <code>``, or ``<Exc>: <message>`` for
        a transport error or an undecodable body). Never raises."""
        try:
            resp = client.get(f"/session/{session_id}/message")
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}"
            return resp.json(), None
        except Exception as e:  # usage is metadata, never a gate
            return None, f"{type(e).__name__}: {e}"

    def usage(
        self, client: Any, session_id: str, transcript: Any
    ) -> tuple[TokenUsage | None, UsageFault | None]:
        """The assistant-message token sum over the transcript (:func:`_sum_usage`)."""
        usage, fault = _tally_messages(transcript)
        return usage, None if fault is None else UsageFault("payload", fault)

    def sample_usage(self, client: Any, session_id: str) -> tuple[TokenUsage | None, str | None]:
        transcript, fault = self.transcript(client, session_id)
        if fault is not None:
            return None, fault
        try:
            return _tally_messages(transcript)
        except Exception as e:  # sampling is advisory
            return None, f"{type(e).__name__}: {e}"


# The V2 tool-call frames the renderer joins by call `id` (see
# `V2Protocol.render_line`); `session.tool.progress` and `.input.ended` carry
# nothing the line shows.
_V2_TOOL_TYPES = frozenset(
    {
        "session.tool.input.started",
        "session.tool.called",
        "session.tool.success",
        "session.tool.failed",
    }
)


def _speaker_block(role: str, text: Any) -> str | None:
    """V1's speaker block (:meth:`V1Protocol.inline_line`): a blank line, the
    role-colored ``[bmad] <role>:`` header, then the text; None for no text."""
    body = text.strip() if isinstance(text, str) else ""
    if not body:
        return None
    return f"\n{_role_color(role)}[bmad] {role}:{_RESET}\n{body}\n"


def _error_summary(error: Any) -> str:
    """``<type> [<status>]: <message>`` for a V2 structured error (never its
    ``response.body``), bounded like every inline summary."""
    if not isinstance(error, dict):
        return _sum_args(error)
    head = " ".join(str(error[key]) for key in ("type", "status") if error.get(key) is not None)
    message = error.get("message")
    text = f"{head}: {message}" if head and message else head or str(message or "")
    return _sum_args(text)


def _form_asks(fields: Any) -> list[str]:
    """``<title>: <description>`` per field of a V2 ``form.created`` form (the
    ``question`` tool maps each question's ``header`` to ``title`` and its text
    to ``description``) — what the form asks, without its options."""
    if not isinstance(fields, list):
        return []
    asks: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        label = field.get("title") or field.get("key") or "?"
        description = field.get("description")
        asks.append(f"{label}: {description}" if description else str(label))
    return asks


# `GET /api/session/{id}/message` page size for the transcript walk: the
# server's maximum — `limit=201` is a 400, never clamped (pinned 2.0.27).
_V2_PAGE_LIMIT = 200
# Bound on that walk, so a cursor that never runs dry is a fault, not a hang —
# and on the descendant walk's `GET /api/session?parentID=` pages in total.
_V2_MAX_PAGES = 1000


class V2Protocol(OpencodeProtocol):
    """The ``@opencode/cli`` 2.x server API — the V2 section of this module's
    docstring, pinned against 2.0.21 and 2.0.27.

    Drives the session lifecycle (config, health, create, prompt, the
    ``/api/event`` envelope and the ``session.execution.*`` turn end,
    interrupt), the poll fallback (``/api/session/active`` + assistant-message
    proof-of-work), usage (the ``Session.Info`` aggregates of the session and
    its descendants) and the paginated
    transcript, the inline renderer and the provider-errors log."""

    major = 2
    # The heartbeat is an SSE comment line (``: heartbeat``), not a data frame:
    # the reader's ``on_comment`` keeps the stream fresh without counting it as
    # activity. ``server.connected`` is the one data frame that is liveness only.
    heartbeat_types = ("server.connected",)
    # No session-less frame was seen live (no ``file.edited`` on V2).
    sessionless_types = frozenset()
    # The turn end. ``session.idle`` / ``session.status`` are deprecated in the
    # schema and never fired live — waiting on them would wait forever.
    # ``session.execution.failed`` is one of them: it is published only once
    # the provider retries are spent (each retry is ``session.retry.scheduled``),
    # so it never precedes a retry the way V1's ``session.error`` can — and the
    # server publishes it BEFORE dropping the session from
    # ``/api/session/active``, so V1's settled-status gate on an error frame
    # could read it busy and discard the sole turn end.
    idle_types = frozenset(
        {
            "session.execution.succeeded",
            "session.execution.interrupted",
            "session.execution.failed",
        }
    )
    # The provider-error frames — V2's server log carries no line per retry, so
    # these are the env-fault classifier's evidence on 2.x.
    provider_error_types = frozenset(
        {"session.retry.scheduled", "session.step.failed", "session.execution.failed"}
    )
    # The per-token streams: each part's complete text arrives once on its
    # `*.ended` frame (V1's `message.part.delta` rule — never traced, never
    # rendered).
    trace_skip_types = frozenset(
        {
            "session.text.delta",
            "session.reasoning.delta",
            "session.tool.input.delta",
            "session.compaction.delta",
        }
    )
    event_path = "/api/event"
    usage_reads_transcript = False
    # How long a model missing from ``/api/model`` (or an absent default) is
    # re-read before the effort check refuses it: the snapshot "may precede
    # initial plugin settlement" (the 2.0.27 route contract), and every session
    # queries it right after its fresh server turns healthy.
    model_settle_s = 5.0
    model_poll_s = 0.25

    def config_content(self, spec: SessionSpec, skill_dir: Path) -> str:
        """The OPENCODE_CONFIG_CONTENT JSON for this session, in V2's native
        keys. ``permissions`` is an ordered rule list where the LAST match
        wins: allow everything (the bypass-flags analogue), deny ``question``
        (an unattended session must never park on a form), then the hermetic
        skills recipe — deny every skill, and re-allow each project skill by
        id. V2 has no ``OPENCODE_DISABLE_EXTERNAL_SKILLS``, so these rules are
        the only thing hiding the operator's personal and the built-in skills.
        ``skills`` points discovery at the project tree; ``model`` is the
        ``"provider/model"`` string when the policy sets one. An id the rule
        matcher would read as a pattern is never re-allowed
        (:meth:`refused_skill_ids`)."""
        permissions: list[dict[str, str]] = [
            {"action": "*", "resource": "*", "effect": "allow"},
            {"action": "question", "resource": "*", "effect": "deny"},
            {"action": "skill", "resource": "*", "effect": "deny"},
        ]
        refused = self.refused_skill_ids(skill_dir)
        permissions.extend(
            {"action": "skill", "resource": skill_id, "effect": "allow"}
            for skill_id in project_skill_ids(skill_dir)
            if skill_id not in refused
        )
        config: dict[str, Any] = {"permissions": permissions, "skills": [str(skill_dir)]}
        if spec.model:
            config["model"] = spec.model
        return json.dumps(config)

    def refused_skill_ids(self, skill_dir: Path) -> list[str]:
        """Project skill ids holding a rule-pattern metacharacter. V2 matches
        a rule's ``resource`` as a wildcard (``*`` any run, ``?`` one char,
        no escape — pinned in the 2.0.27 bundle), so an allow for a directory
        named ``*`` would come last and re-allow every personal and built-in
        skill. Such a skill stays denied, fail-closed."""
        return [
            skill_id
            for skill_id in project_skill_ids(skill_dir)
            if not _V2_RULE_WILDCARDS.isdisjoint(skill_id)
        ]

    def extra_env(self) -> dict[str, str]:
        return {}

    def health(self, client: Any) -> HealthProbe:
        """``GET /api/info``: healthy only on a JSON object whose string
        ``version`` is a 2.x — a V1 server answers this route with its web-UI
        HTML, and a V1-shaped body is not V2's."""
        resp = client.get("/api/info")
        if _is_html(resp):
            return HealthProbe(healthy=False, html=True)
        if resp.status_code != 200:
            return HealthProbe(healthy=False)
        body = resp.json()
        version = body.get("version") if isinstance(body, dict) else None
        parsed = parse_opencode_version(version) if isinstance(version, str) else None
        return HealthProbe(healthy=parsed is not None and parsed[0] == self.major)

    def create_session(self, client: Any, spec: SessionSpec) -> str:
        """``POST /api/session`` with the project as its location. A reasoning
        effort rides the session's ``model`` ref as its ``variant`` — the prompt
        body has no variant field, and the config's ``model#variant`` form is
        parsed but never applied to a session (both pinned). The session
        keeps that model for every prompt, nudges included."""
        body: dict[str, Any] = {"title": spec.task_id, "location": {"directory": str(spec.cwd)}}
        if spec.effort:
            body["model"] = self._effort_model_ref(client, spec)
        resp = client.post("/api/session", json=body)
        if resp.status_code != 200:
            raise OpencodeServerError(
                f"POST /api/session failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()["data"]["id"]

    def _effort_model_ref(self, client: Any, spec: SessionSpec) -> dict[str, str]:
        """The ``{"id", "providerID", "variant"}`` ref that carries the effort:
        the policy model (``"provider/model"``, split on the first ``/``) or,
        without one, the server's default model for the project. The effort
        must be one of that model's ``variants``: V2 accepts any variant at
        create and only fails the turn at prompt time (``provider.no-route``
        "Variant unavailable"), so an unknown name is refused here, before any
        session exists, rather than surfacing as a result-less turn end. A
        model (or default) not yet listed is re-read for ``model_settle_s``
        before that refusal — plugin-supplied models may land after the server
        turns healthy."""
        deadline = time.monotonic() + self.model_settle_s
        while True:
            info = self._effort_model_info(client, spec)
            if info is not None or time.monotonic() >= deadline:
                break
            time.sleep(self.model_poll_s)
        if info is None and spec.model:
            raise OpencodeServerError(
                f"effort {spec.effort!r} needs the model's variants, but "
                f"{spec.model!r} is not among the opencode server's models"
            )
        if info is None:
            raise OpencodeServerError(
                f"effort {spec.effort!r} is set but the opencode server has no "
                "default model to apply it to — set the adapter's model"
            )
        variants = [v.get("id") for v in info.get("variants") or []]
        if spec.effort not in variants:
            name = f"{info.get('providerID')}/{info.get('id')}"
            raise OpencodeServerError(
                f"effort {spec.effort!r} is not a variant of {name} "
                f"(available: {', '.join(str(v) for v in variants) or 'none'})"
            )
        return {"id": info["id"], "providerID": info["providerID"], "variant": spec.effort}

    def _effort_model_info(self, client: Any, spec: SessionSpec) -> dict[str, Any] | None:
        """One read of the effort's ``Model.Info``: the policy model off
        ``/api/model``, else ``/api/model/default``; None when it is not
        (yet) listed. A non-200 raises — that is no settling snapshot."""
        location = {"location[directory]": str(spec.cwd)}
        if spec.model:
            provider_id, _, model_id = spec.model.partition("/")
            resp = client.get("/api/model", params=location)
            if resp.status_code != 200:
                raise OpencodeServerError(
                    f"GET /api/model failed: {resp.status_code} {resp.text[:200]}"
                )
            return next(
                (
                    m
                    for m in resp.json()["data"]
                    if m.get("providerID") == provider_id and m.get("id") == model_id
                ),
                None,
            )
        resp = client.get("/api/model/default", params=location)
        if resp.status_code != 200:
            raise OpencodeServerError(
                f"GET /api/model/default failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()["data"]

    def prompt(self, client: Any, session_id: str, text: str, variant: str) -> None:
        """``POST /api/session/{id}/prompt``; raises unless the server accepts
        it (200 — a 409 ``ConflictError`` or anything else is a refusal). The
        body has no model or variant field: the effort rode the session's model
        ref at create (:meth:`create_session`), so ``variant`` is not sent."""
        resp = client.post(f"/api/session/{session_id}/prompt", json={"text": text})
        if resp.status_code != 200:
            raise OpencodeServerError(f"prompt failed: {resp.status_code} {resp.text[:200]}")

    def frame_type(self, event: dict[str, Any]) -> Any:
        return event.get("type")

    def frame_payload(self, event: dict[str, Any]) -> tuple[Any, Any]:
        data = event.get("data") or {}
        session_id = data.get("sessionID")
        if session_id is None and event.get("type") == "form.created":
            # The one frame that nests its session id: `form.created` carries
            # the whole `Form.Info` under `data.form` (pinned 2.0.32).
            form = data.get("form")
            session_id = form.get("sessionID") if isinstance(form, dict) else None
        return data, session_id

    def render_line(self, msg_roles: dict[Any, Any], etype: str, props: Any) -> str | None:
        """The inline-log line for one V2 frame, or None for a frame that
        renders nothing — the same lines V1 writes (:meth:`V1Protocol.inline_line`
        / :meth:`V1Protocol.tool_line`), read off the V2 frames pinned in this
        module's "Inline transcript" section. Never a ``*.delta``: each text's
        complete form arrives once on its ``*.ended`` frame.

        ``msg_roles`` is the per-session render state; V2 frames name their
        speaker by type, so it keys each tool call by its ``id`` instead: the
        name from ``session.tool.input.started``, the input from
        ``session.tool.called``, and the line rendered once, on the terminal
        ``session.tool.success`` / ``session.tool.failed``."""
        if etype == "session.inbox.enqueued":
            item = props.get("item") or {}
            return _speaker_block("user", (item.get("payload") or {}).get("text"))
        if etype in ("session.text.ended", "session.reasoning.ended"):
            # Reasoning reads as prose under the assistant header, as V1
            # renders a reasoning part.
            return _speaker_block("assistant", props.get("text"))
        if etype in _V2_TOOL_TYPES:
            return self._tool_line(msg_roles, etype, props)
        if etype == "permission.asked":
            resources = _sum_args(props.get("resources"))
            return (
                f"{_TOOL_COLOR}[bmad] perm ask: {props.get('action') or '?'}"
                f"{(' ' + resources) if resources else ''}{_RESET}\n"
            )
        if etype == "permission.replied":
            return f"{_TOOL_COLOR}[bmad] perm reply: {props.get('reply') or '?'}{_RESET}\n"
        if etype == "form.created":
            # A form parks the turn until it is replied or cancelled, so this
            # line is what explains the stall. Any other `form.*` type renders
            # nothing (the fall-through below).
            form = props.get("form")
            form = form if isinstance(form, dict) else {}
            asks = _sum_args(_form_asks(form.get("fields")))
            return (
                f"{_TOOL_COLOR}[bmad] form ask: {form.get('title') or '?'}"
                f"{(' ' + asks) if asks else ''}{_RESET}\n"
            )
        if etype == "form.replied":
            answer = _sum_args(props.get("answer"))
            return f"{_TOOL_COLOR}[bmad] form reply:{(' ' + answer) if answer else ''}{_RESET}\n"
        if etype == "form.cancelled":
            # The payload is only `id` / `sessionID`; the reason is on the
            # parked tool's `session.tool.failed` line that follows.
            return f"{_TOOL_COLOR}[bmad] form cancelled{_RESET}\n"
        if etype == "session.retry.scheduled":
            detail = _error_summary(props.get("error"))
            return (
                f"{_TOOL_COLOR}[bmad] retry {props.get('attempt') or '?'}:"
                f"{(' ' + detail) if detail else ''}{_RESET}\n"
            )
        if etype in ("session.step.failed", "session.execution.failed"):
            label = "step failed" if etype == "session.step.failed" else "error"
            detail = _error_summary(props.get("error"))
            return f"{_TOOL_COLOR}[bmad] {label}:{(' ' + detail) if detail else ''}{_RESET}\n"
        return None

    @staticmethod
    def _tool_line(calls: dict[Any, Any], etype: str, props: Any) -> str | None:
        call_id = props.get("id")
        if not isinstance(call_id, str):
            return None
        call = calls.setdefault(("tool", call_id), {})
        if etype == "session.tool.input.started":
            call["name"] = props.get("name")
            return None
        if etype == "session.tool.called":
            call["input"] = props.get("input")
            return None
        if call.get("done"):
            return None  # terminal-once: a call renders exactly one line
        call["done"] = True
        args = _sum_args(call.get("input"))
        suffix = ""
        if etype == "session.tool.failed":
            detail = _error_summary(props.get("error"))
            suffix = f" -> error{(': ' + detail) if detail else ''}"
        else:
            # A non-zero exit is a SUCCESS frame on V2 (pinned): the exit code
            # is the only trace of the failure, so it goes on the line.
            exit_code = (props.get("metadata") or {}).get("exit")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code:
                suffix = f" -> exit {exit_code}"
        return (
            f"{_TOOL_COLOR}[bmad] tool: {call.get('name') or '?'}"
            f"{(' ' + args) if args else ''}{suffix}{_RESET}\n"
        )

    def session_busy(self, client: Any, session_id: str) -> bool | None:
        """Tri-state ``GET /api/session/active`` probe: True = listed (the map's
        only type is ``running``, which covers a ``session.retry.scheduled``
        backoff too — the V1 ``retry``-is-busy rule), False = absent (settled),
        None = unknowable (unreachable server, non-200, a body without the
        ``data`` map). Unknown is not settled — see :meth:`V1Protocol.session_busy`."""
        try:
            resp = client.get("/api/session/active")
            if resp.status_code != 200:
                return None
            active = resp.json().get("data")
            if not isinstance(active, dict):
                return None
            return session_id in active
        except Exception:  # probe is advisory
            return None

    def latest_completed_ms(self, client: Any, session_id: str) -> int | None:
        """The newest assistant message's ``time.completed`` (epoch ms; 0 when
        none has completed) over the first page of messages, which is
        newest-first — or None when the read failed. Advisory, never raises."""
        try:
            resp = client.get(f"/api/session/{session_id}/message")
            if resp.status_code != 200:
                return None
            completed = 0
            for msg in resp.json()["data"]:
                if msg.get("type") != "assistant":
                    continue
                done_ms = (msg.get("time") or {}).get("completed") or 0
                completed = max(completed, int(done_ms))
        except Exception:  # probe is advisory
            return None
        return completed

    def abort(self, client: Any, session_id: str) -> None:
        client.post(f"/api/session/{session_id}/interrupt")

    def transcript(self, client: Any, session_id: str) -> tuple[Any, str | None]:
        """Every message, oldest first: ``order=asc`` at the server's maximum
        page size, then ``cursor.next`` until a page comes back empty (a full
        last page still carries a ``next``). A failed page, a body without a
        ``data`` list, or a walk past ``_V2_MAX_PAGES`` is a fault — never a
        partial history. Never raises."""
        messages: list[Any] = []
        params: dict[str, Any] = {"order": "asc", "limit": _V2_PAGE_LIMIT}
        try:
            for _ in range(_V2_MAX_PAGES):
                resp = client.get(f"/api/session/{session_id}/message", params=params)
                if resp.status_code != 200:
                    return None, f"HTTP {resp.status_code}"
                body = resp.json()
                page = body.get("data")
                if not isinstance(page, list):
                    return None, f"message page is {type(page).__name__}, not a list"
                messages.extend(page)
                next_cursor = (body.get("cursor") or {}).get("next")
                if not page or not next_cursor:
                    return messages, None
                # The cursor carries the order; repeating `order` is a 400.
                params = {"cursor": next_cursor, "limit": _V2_PAGE_LIMIT}
        except Exception as e:  # usage is metadata, never a gate
            return None, f"{type(e).__name__}: {e}"
        return None, f"message history did not end within {_V2_MAX_PAGES} pages"

    def usage(
        self, client: Any, session_id: str, transcript: Any
    ) -> tuple[TokenUsage | None, UsageFault | None]:
        """``GET /api/session/{id}`` → ``data.tokens``, the cumulative session
        aggregate (never also summed per step), plus every descendant
        (subagent) session's aggregate (:meth:`descendants`): a 2.x session
        counts only its own steps. Reasoning bills as output; the cache
        read/write map as in V1. A child still running is counted as far as it
        has landed (:meth:`active_descendants` names it). A failed walk or a
        malformed child is a ``children`` fault, never a parent-only figure.
        Untracked (None) when the summed total is zero and the transcript
        holds no completed, un-interrupted assistant message — the V1 rule
        (:func:`_sum_usage`): an in-flight first step reports zeros, and the
        step an interrupt cancelled is stamped completed with
        ``error.type == "aborted"`` and no tokens. (V1 still counts the parent
        alone.) Never raises."""
        try:
            resp = client.get(f"/api/session/{session_id}")
            if resp.status_code != 200:
                return None, UsageFault("fetch", f"HTTP {resp.status_code}")
            body = resp.json()
        except Exception as e:  # usage is metadata, never a gate
            return None, UsageFault("fetch", f"{type(e).__name__}: {e}")
        try:
            usage = _session_tokens(body)
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            return None, UsageFault("payload", f"malformed session: {type(e).__name__}: {e}")
        children, fault = self._children_tokens(client, session_id)
        if children is None:
            return None, UsageFault("children", fault or "")
        usage.add(children)
        if usage.total == 0 and not _any_completed_assistant(transcript):
            return None, None
        return usage, None

    def sample_usage(self, client: Any, session_id: str) -> tuple[TokenUsage | None, str | None]:
        """The budget guard's sample: the same parent-plus-descendants sum as
        :meth:`usage`, read each tick (never the transcript walk). A zero total
        is inert, not a sample — nothing has been spent yet."""
        try:
            resp = client.get(f"/api/session/{session_id}")
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}"
            usage = _session_tokens(resp.json())
        except Exception as e:  # sampling is advisory
            return None, f"{type(e).__name__}: {e}"
        children, fault = self._children_tokens(client, session_id)
        if children is None:
            return None, fault
        usage.add(children)
        return (usage if usage.total else None), None

    def descendants(self, client: Any, session_id: str) -> tuple[list[Any] | None, str | None]:
        """``(infos, fault)``: the ``Session.Info`` of every session below
        ``session_id``. There is no children route: ``GET /api/session?parentID=``
        lists one level, so the walk recurses breadth-first (subagent nesting
        is configurable), each listing paged like :meth:`transcript` — the
        cursor carries the ``parentID``. A listed session whose ``parentID`` is
        not the one asked for is not a child; an id already seen is skipped,
        so a cycle ends. A failed or malformed page, a child without an id, or
        more than ``_V2_MAX_PAGES`` pages in all is a fault — never a partial
        tree. Never raises."""
        found: list[Any] = []
        seen = {session_id}
        queue = [session_id]
        pages = 0
        try:
            while queue:
                parent = queue.pop(0)
                params: dict[str, Any] = {"parentID": parent, "limit": _V2_PAGE_LIMIT}
                while True:
                    if pages == _V2_MAX_PAGES:
                        return None, f"child sessions did not end within {_V2_MAX_PAGES} pages"
                    pages += 1
                    resp = client.get("/api/session", params=params)
                    if resp.status_code != 200:
                        return None, f"child list of {parent}: HTTP {resp.status_code}"
                    body = resp.json()
                    page = body.get("data")
                    if not isinstance(page, list):
                        return None, (
                            f"child list of {parent} is {type(page).__name__}, not a list"
                        )
                    for info in page:
                        child = info.get("id")
                        if not isinstance(child, str) or not child:
                            return None, f"a child of {parent} has no session id"
                        if info.get("parentID") != parent or child in seen:
                            continue
                        seen.add(child)
                        found.append(info)
                        queue.append(child)
                    next_cursor = (body.get("cursor") or {}).get("next")
                    if not page or not next_cursor:
                        break
                    params = {"cursor": next_cursor, "limit": _V2_PAGE_LIMIT}
        except Exception as e:  # usage is metadata, never a gate
            return None, f"{type(e).__name__}: {e}"
        return found, None

    def _children_tokens(
        self, client: Any, session_id: str
    ) -> tuple[TokenUsage | None, str | None]:
        """``(usage, fault)``: the summed aggregate of every :meth:`descendants`
        session (a zero usage when there are none), or None with ``fault``
        naming the failed walk or the malformed child. Never raises."""
        infos, fault = self.descendants(client, session_id)
        if infos is None:
            return None, fault
        total = TokenUsage()
        for info in infos:
            try:
                total.add(_info_tokens(info))
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                return None, f"malformed child session {info.get('id')}: {type(e).__name__}: {e}"
        return total, None

    def active_descendants(
        self, client: Any, session_id: str
    ) -> tuple[list[str] | None, str | None]:
        """The descendants still listed by ``GET /api/session/active`` — a
        ``background: true`` subagent can outlive its parent's turn. The walk
        runs only when a session other than the parent is listed. A non-200,
        a body without the ``data`` map or a failed walk is a fault, never
        "none active"."""
        try:
            resp = client.get("/api/session/active")
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}"
            active = resp.json().get("data")
            if not isinstance(active, dict):
                return None, f"active map is {type(active).__name__}, not an object"
            others = [sid for sid in active if sid != session_id]
        except Exception as e:  # observation only
            return None, f"{type(e).__name__}: {e}"
        if not others:
            return [], None
        infos, fault = self.descendants(client, session_id)
        if infos is None:
            return None, fault
        mine = {info.get("id") for info in infos}
        return sorted(sid for sid in others if sid in mine), None


def _session_tokens(body: Any) -> TokenUsage:
    """The ``{"data": Session.Info}`` body's ``tokens`` aggregate; raises on a
    body of the wrong shape (no ``data`` / ``tokens`` object, a non-numeric
    count) — a fault, never a zero."""
    return _info_tokens(body["data"])


def _info_tokens(info: Any) -> TokenUsage:
    """One ``Session.Info``'s ``tokens`` aggregate; raises on a wrong shape."""
    tokens = info["tokens"]
    if not isinstance(tokens, dict):
        raise TypeError(f"tokens is {type(tokens).__name__}, not an object")
    cache = tokens.get("cache") or {}
    return TokenUsage(
        input_tokens=int(tokens.get("input") or 0),
        output_tokens=int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0),
        cache_read_tokens=int(cache.get("read") or 0),
        cache_creation_tokens=int(cache.get("write") or 0),
    )


def _any_completed_assistant(messages: Any) -> bool:
    """Whether a V2 transcript holds an assistant message that finished on its
    own: ``time.completed`` set and not an interrupt's ``aborted`` error. A
    provider failure counts (it completed, with ``finish: "error"``) — exactly
    the measured zero the env-fault classifier is for."""
    for msg in messages or []:
        if msg.get("type") != "assistant":
            continue
        error = msg.get("error")
        aborted = isinstance(error, dict) and error.get("type") == "aborted"
        if (msg.get("time") or {}).get("completed") and not aborted:
            return True
    return False


# The characters V2's permission-rule matcher reads as wildcards.
_V2_RULE_WILDCARDS = frozenset("*?")


def project_skill_ids(skill_dir: Path) -> list[str]:
    """The ids of the skills under ``skill_dir`` (one ``<dir>/SKILL.md`` each),
    sorted — what V2's ``skill`` permission rules match on. The id is the
    DIRECTORY name, not the frontmatter ``name`` (pinned live: a rule naming
    the frontmatter name re-allows nothing)."""
    if not skill_dir.is_dir():
        return []
    return sorted(entry.name for entry in skill_dir.iterdir() if (entry / "SKILL.md").is_file())


def _sum_usage(messages: Any) -> TokenUsage | None:
    """Sum assistant-message token counts. Reasoning tokens are
    billed as output; OpenCode's cache read/write map onto the claude-style
    cache_read/cache_creation fields. Child-session (subagent) tokens are not
    visible here — the messages endpoint is scoped per session.

    None (untracked) when the payload is not a message list or no assistant
    message carries a non-empty ``tokens`` block — the same untracked-is-not-zero
    rule as ``tokens.tally`` (DW-364), which skips an empty block too. Also None
    when the sum is zero and no assistant message has a truthy
    ``info.time.completed``: opencode creates the assistant message with an
    all-zero ``tokens`` block before its first step finishes, so a timeout
    during an in-flight FIRST step (slow stream, hung first tool call) has no
    usage signal yet. An aborted message does not count as completed: the
    timeout path aborts before it captures, and opencode's cleanup stamps
    ``time.completed`` on the step it cancelled, flagged by a
    ``MessageAbortedError`` ``info.error``. A nonzero sum keeps every message's
    tokens, completed or not. A zero here is load-bearing: the base adapter classifies a timeout
    with a TRACKED zero tally as an environment fault, so a malformed, empty or
    still-in-flight response must not read as a measured zero.

    A malformed payload (not a list, or an entry the walk cannot read) is None
    too; :func:`_tally_messages` says why, for the callers that crumb it."""
    return _tally_messages(messages)[0]


def _tally_messages(messages: Any) -> tuple[TokenUsage | None, str | None]:
    """``(usage, fault)`` — :func:`_sum_usage`'s answer, with a malformed payload
    kept apart from an untracked one (DW-461): ``fault`` describes a body that is
    not a list, or whose entries, ``info``/``time``/``cache`` blocks or counts
    have the wrong shape, and the usage is then None ("unknown"), never a
    partial sum. Otherwise ``fault`` is None. (A non-dict ``tokens`` block stays
    what it always was, a message with no usage — skipped, not a fault.)"""
    if not isinstance(messages, list):
        return None, f"payload is {type(messages).__name__}, not a message list"
    try:
        return _sum_message_list(messages), None
    except (AttributeError, TypeError, ValueError) as e:
        return None, f"malformed message list: {type(e).__name__}: {e}"


def _sum_message_list(messages: list[Any]) -> TokenUsage | None:
    """The :func:`_sum_usage` walk over a list; raises on a wrong-shaped entry."""
    usage: TokenUsage | None = None
    any_completed = False
    for msg in messages:
        info = (msg or {}).get("info") or {}
        if info.get("role") != "assistant":
            continue
        error = info.get("error")
        aborted = isinstance(error, dict) and error.get("name") == "MessageAbortedError"
        if (info.get("time") or {}).get("completed") and not aborted:
            any_completed = True
        tokens = info.get("tokens")
        if not isinstance(tokens, dict) or not tokens:
            continue
        if usage is None:
            usage = TokenUsage()
        cache = tokens.get("cache") or {}
        usage.add(
            TokenUsage(
                input_tokens=int(tokens.get("input") or 0),
                output_tokens=int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0),
                cache_read_tokens=int(cache.get("read") or 0),
                cache_creation_tokens=int(cache.get("write") or 0),
            )
        )
    if usage is not None and usage.total == 0 and not any_completed:
        return None
    return usage
