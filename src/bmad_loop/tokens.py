"""Token accounting from coding-CLI transcript files.

One parser per CLI family, selected by the profile's `usage_parser` key. All
parsers are tolerant — unknown lines and shapes are skipped, never fatal; a
transcript that yields nothing reads as None (untracked), not zero.

- claude-jsonl:  ~/.claude/projects/<munged-cwd>/<session-id>.jsonl;
                 assistant entries carry an API `usage` block, summed.
- codex-rollout: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl; `token_count`
                 event payloads carry CUMULATIVE totals, so the last one is
                 the session usage (sessions here are fresh per task).
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


def tally(transcript_path: Path) -> TokenUsage:
    total = TokenUsage()
    for entry in _jsonl_entries(transcript_path):
        usage = _usage_block(entry)
        if not usage:
            continue
        total.add(
            TokenUsage(
                input_tokens=_int(usage.get("input_tokens")),
                output_tokens=_int(usage.get("output_tokens")),
                cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
                cache_creation_tokens=_int(usage.get("cache_creation_input_tokens")),
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
