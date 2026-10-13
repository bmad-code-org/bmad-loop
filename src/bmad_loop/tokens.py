"""Token accounting from coding-CLI transcript files.

One parser per CLI family, selected by the profile's `usage_parser` key. All
parsers are tolerant — unknown lines and shapes are skipped, never fatal; a
transcript that yields nothing reads as None (untracked), not zero.

- claude-jsonl:  ~/.claude/projects/<munged-cwd>/<session-id>.jsonl plus
                 its <session-id>/subagents/**/*.jsonl tree (read
                 recursively: workflow agents nest under workflows/wf_*/);
                 assistant entries carry an API `usage` block. Claude Code
                 writes one entry per content block, each repeating the
                 response's usage, so blocks are deduped by message.id with a
                 per-field max (output_tokens grows while the response
                 streams; max is order-independent across files). The dedupe
                 is global because fork subagents copy their parent's message
                 ids. Entries without an id each count.
- codex-rollout: $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
                 (CODEX_HOME defaults to ~/.codex; day dirs are LOCAL dates
                 while session_meta timestamps are UTC). The filename UUID is
                 the hook `session_id`, so when the hook's transcript_path is
                 missing or unusable the rollout is resolved by id
                 (`resolve_transcript`). `token_count` event payloads carry
                 CUMULATIVE totals, so the last one is the session usage
                 (sessions here are fresh per task).
- gemini-chat:   ~/.gemini/tmp/<project>/chats/session-*.jsonl — a JSONL
                 patch stream: bare message entries (and messages inside
                 "$set" patches) carry a per-API-call `tokens` block
                 {input, output, cached, thoughts, tool, total}; the same
                 message id is re-emitted as it accretes content, so the
                 last occurrence per id wins, then unique messages are
                 summed. `input` includes the cached portion.
- copilot-events: ~/.copilot/session-state/<session>/events.jsonl; some
                 entries carry `data.modelMetrics.<model>.usage`
                 {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens,
                 reasoningTokens} that is CUMULATIVE per model, so the last
                 entry bearing modelMetrics holds the session totals (summed
                 across models). reasoningTokens fold into output.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .model import TokenUsage


def read_usage(parser: str, transcript_path: Path) -> TokenUsage | None:
    if parser == "claude-jsonl":
        return tally(transcript_path)
    if parser == "codex-rollout":
        return tally_codex_rollout(transcript_path)
    if parser == "gemini-chat":
        return tally_gemini_chat(transcript_path)
    if parser == "copilot-events":
        return tally_copilot_events(transcript_path)
    return None


def codex_home(env: Mapping[str, str]) -> Path | None:
    """Codex's state root: ``CODEX_HOME`` when set and non-empty, else
    ``~/.codex``; None when no home directory can be determined."""
    configured = env.get("CODEX_HOME")
    if configured:
        return Path(configured)
    try:
        return Path.home() / ".codex"
    except RuntimeError:
        return None


def find_codex_rollout(session_id: str | None, home: Path | None) -> Path | None:
    """The rollout file Codex wrote for ``session_id`` under ``home``, or None.

    The id is canonicalised as a UUID before it reaches the glob, so a hostile
    or garbled id (``*``, ``../x``) matches nothing. Day directories are local
    dates, so every one is searched; on several matches the newest wins."""
    if home is None or session_id is None:
        return None
    try:
        canonical = str(uuid.UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        return None
    try:
        matches = list((home / "sessions").glob(f"*/*/*/rollout-*-{canonical}.jsonl"))
        if not matches:
            return None
        return max(matches, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def resolve_transcript(parser: str, session_id: str | None, env: Mapping[str, str]) -> Path | None:
    """The transcript ``parser`` reads for ``session_id``, found without the
    hook's transcript_path. Only codex-rollout files are resolvable by id."""
    if parser != "codex-rollout":
        return None
    return find_codex_rollout(session_id, codex_home(env))


def _jsonl_entries(transcript_path: Path):
    if not transcript_path.is_file():
        return
    with transcript_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                yield entry


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------- claude-jsonl


def _usage_block(entry: dict[str, Any]) -> dict[str, Any] | None:
    message = entry.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        return message["usage"]
    if isinstance(entry.get("usage"), dict):
        return entry["usage"]
    return None


_CLAUDE_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def tally(transcript_path: Path) -> TokenUsage | None:
    """Sum the session's assistant `usage` blocks, or None when it holds none.

    Reads the transcript plus every `*.jsonl` under the sibling
    `<session-id>/subagents/` tree, recursively (workflow agents live in
    `subagents/workflows/wf_<id>/`). Claude Code writes one entry per content
    block and each repeats the response's usage, so a block whose
    `message.id` is a string merges into one global per-id record by per-field
    max (#836): only output_tokens differs between duplicates, growing as the
    response streams, and max — unlike last-wins — does not depend on file
    order. The record is global, not per-file, because a fork subagent copies
    its parent's history, message ids included. Blocks without an id (or the
    top-level `usage` shape) each count, as before.

    None (a missing file, or no usage-bearing line in any file) is "no usage
    signal" and must never read as free (DW-364): a zeroed TokenUsage is
    reserved for a session that did report usage — an all-zero block still
    counts as seen — so a caller can tell tracked zero spend from untracked.
    OSError/ValueError propagate to the caller."""
    paths = [transcript_path]
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    if subagents_dir.is_dir():
        paths.extend(sorted(subagents_dir.rglob("*.jsonl")))
    by_id: dict[str, dict[str, int]] = {}
    anon: list[dict[str, int]] = []
    seen = False
    for path in paths:
        for entry in _jsonl_entries(path):
            usage = _usage_block(entry)
            # Absent, empty `{}`, or no recognized token key: no usage signal here.
            if not usage or not any(key in usage for key in _CLAUDE_USAGE_KEYS):
                continue
            seen = True
            counts = {key: _int(usage.get(key)) for key in _CLAUDE_USAGE_KEYS}
            message = entry.get("message")
            # Only a message-level usage block is keyed by its message's id; a
            # top-level `usage` counts on its own even beside a `message.id`.
            message_id = (
                message.get("id")
                if isinstance(message, dict) and message.get("usage") is usage
                else None
            )
            if not isinstance(message_id, str):
                anon.append(counts)
                continue
            merged = by_id.setdefault(message_id, dict.fromkeys(_CLAUDE_USAGE_KEYS, 0))
            for key, value in counts.items():
                merged[key] = max(merged[key], value)
    if not seen:
        return None
    total = TokenUsage()
    for counts in [*by_id.values(), *anon]:
        total.add(
            TokenUsage(
                input_tokens=counts["input_tokens"],
                output_tokens=counts["output_tokens"],
                cache_read_tokens=counts["cache_read_input_tokens"],
                cache_creation_tokens=counts["cache_creation_input_tokens"],
            )
        )
    return total


# ----------------------------------------------------------- codex-rollout


def tally_codex_rollout(transcript_path: Path) -> TokenUsage | None:
    last: dict[str, Any] | None = None
    for entry in _jsonl_entries(transcript_path):
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        totals = info.get("total_token_usage")
        last = totals if isinstance(totals, dict) else info
    if last is None:
        return None
    cached = _int(last.get("cached_input_tokens"))
    return TokenUsage(
        # Codex's input_tokens includes the cached portion; split it out.
        input_tokens=max(0, _int(last.get("input_tokens")) - cached),
        output_tokens=_int(last.get("output_tokens")),
        cache_read_tokens=cached,
    )


# ------------------------------------------------------------- gemini-chat


def _gemini_messages(entry: dict[str, Any]):
    if "id" in entry:
        yield entry
    set_patch = entry.get("$set")
    if isinstance(set_patch, dict):
        messages = set_patch.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict):
                    yield message


def tally_gemini_chat(transcript_path: Path) -> TokenUsage | None:
    by_id: dict[str, dict[str, Any]] = {}
    for entry in _jsonl_entries(transcript_path):
        for message in _gemini_messages(entry):
            tokens = message.get("tokens")
            message_id = message.get("id")
            if isinstance(tokens, dict) and isinstance(message_id, str):
                by_id[message_id] = tokens
    if not by_id:
        return None
    total = TokenUsage()
    for tokens in by_id.values():
        cached = _int(tokens.get("cached"))
        total.add(
            TokenUsage(
                input_tokens=max(0, _int(tokens.get("input")) - cached),
                output_tokens=_int(tokens.get("output")) + _int(tokens.get("thoughts")),
                cache_read_tokens=cached,
            )
        )
    return total


# --------------------------------------------------------- copilot-events


def tally_copilot_events(transcript_path: Path) -> TokenUsage | None:
    # data.modelMetrics.<model>.usage is cumulative per model, so the LAST entry
    # carrying modelMetrics holds the session totals; sum across its models.
    # (Schema verified against a single Copilot CLI 1.0.63 events.jsonl — revisit
    # the cumulative/multi-model assumption if a newer build disagrees.)
    last: dict[str, Any] | None = None
    for entry in _jsonl_entries(transcript_path):
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        metrics = data.get("modelMetrics")
        if isinstance(metrics, dict) and metrics:
            last = metrics
    if last is None:
        return None
    total = TokenUsage()
    for model_metrics in last.values():
        if not isinstance(model_metrics, dict):
            continue
        usage = model_metrics.get("usage")
        if not isinstance(usage, dict):
            continue
        total.add(
            TokenUsage(
                input_tokens=_int(usage.get("inputTokens")),
                output_tokens=_int(usage.get("outputTokens")) + _int(usage.get("reasoningTokens")),
                cache_read_tokens=_int(usage.get("cacheReadTokens")),
                cache_creation_tokens=_int(usage.get("cacheWriteTokens")),
            )
        )
    return total
