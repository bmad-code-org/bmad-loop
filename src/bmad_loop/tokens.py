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
                 (sessions here are fresh per task). Each subagent writes its
                 own rollout, and its usage is NOT part of the parent's
                 totals: a sibling whose first-line session_meta names the
                 parent as its `session_id` (the root thread, at any depth),
                 or whose `parent_thread_id` chains into the parent's tree,
                 adds its own last cumulative totals. Siblings are sought in
                 the parent's day dir and later ones (a session can cross
                 midnight), from the parent's filename timestamp on.
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

import datetime
import json
import re
import threading
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


def _codex_rollout_total(transcript_path: Path) -> dict[str, Any] | None:
    """The rollout's last cumulative `token_count` totals, or None when it has
    none. A subagent's rollout holds only its own turns, so this rule holds
    for children as well as for the parent. OSError/ValueError propagate."""
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
    return last


def _codex_usage(totals: dict[str, Any]) -> TokenUsage:
    cached = _int(totals.get("cached_input_tokens"))
    return TokenUsage(
        # Codex's input_tokens includes the cached portion; split it out.
        input_tokens=max(0, _int(totals.get("input_tokens")) - cached),
        output_tokens=_int(totals.get("output_tokens")),
        cache_read_tokens=cached,
    )


# A session_meta line is ~18-23 KB (it embeds base_instructions). A membership
# test reads at most this much of a sibling, never its body.
_CODEX_META_READ_CAP = 1 << 20

# (id, session_id, parent_thread_id) from a rollout's first-line session_meta,
# or None when the file does not open with one. session_meta is written once and
# never changes, so every answer, negative included, is cached by path for
# good; only a first line still being written (no newline yet) is re-read.
# Bounded oldest-first like opencode_http's USAGE_STASH_CAP. A day dir holds a
# few hundred rollouts and a tally lists the parent's day plus the next, so the
# cap sits far above one tally's candidate set: the budget guard's per-heartbeat
# tally never evicts its own siblings, and re-reads only the files' bodies.
CODEX_META_CACHE_CAP = 4096
_CodexMeta = tuple[str, str | None, str | None]
_codex_meta_cache: dict[str, _CodexMeta | None] = {}
_codex_meta_lock = threading.Lock()

_ROLLOUT_TS = re.compile(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})")


def _read_codex_first_line(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.readline(_CODEX_META_READ_CAP)


def _parse_codex_meta(line: bytes) -> _CodexMeta | None:
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "session_meta":
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None

    def text(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    thread_id = text(payload.get("id"))
    if thread_id is None:
        return None
    parent = text(payload.get("parent_thread_id"))
    if parent is None:
        # Older rollouts name the parent only in the spawn source.
        source = payload.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        parent = text(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else None
    return thread_id, text(payload.get("session_id")), parent


def _codex_meta(path: Path) -> _CodexMeta | None:
    """The rollout's session_meta facts, cached (see CODEX_META_CACHE_CAP).
    OSError propagates."""
    key = str(path)
    with _codex_meta_lock:
        if key in _codex_meta_cache:
            return _codex_meta_cache[key]
    line = _read_codex_first_line(path)
    meta = _parse_codex_meta(line)
    if line.endswith(b"\n") or len(line) >= _CODEX_META_READ_CAP:
        with _codex_meta_lock:
            if key not in _codex_meta_cache:
                while len(_codex_meta_cache) >= CODEX_META_CACHE_CAP:
                    del _codex_meta_cache[next(iter(_codex_meta_cache))]
            _codex_meta_cache[key] = meta
    return meta


def _codex_day(folder: Path) -> datetime.date | None:
    try:
        return datetime.date(
            int(folder.parent.parent.name), int(folder.parent.name), int(folder.name)
        )
    except ValueError:
        return None


def _codex_sibling_candidates(transcript_path: Path) -> list[Path]:
    """Rollouts that may be the parent's subagents: `rollout-*.jsonl` in the
    parent's day dir and each later day dir through the day after the parent's
    last write (capped at local today + 1; dirs are local dates and a child
    can start after midnight), whose filename timestamp is not earlier than
    the parent's. A parent outside a YYYY/MM/DD tree searches its own dir."""
    folder = transcript_path.parent
    first = _codex_day(folder)
    folders = [folder]
    if first is not None:
        today = datetime.date.today()
        try:
            written = datetime.date.fromtimestamp(transcript_path.stat().st_mtime)
        except (OSError, ValueError, OverflowError):
            written = today
        last = max(first, min(written, today)) + datetime.timedelta(days=1)
        sessions = folder.parent.parent.parent
        folders = []
        day = first
        while day <= last:
            folders.append(sessions / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}")
            day += datetime.timedelta(days=1)
    stamp = _ROLLOUT_TS.match(transcript_path.name)
    floor = stamp.group(1) if stamp else ""
    candidates: list[Path] = []
    for each in folders:
        for path in sorted(each.glob("rollout-*.jsonl")):
            match = _ROLLOUT_TS.match(path.name)
            if path != transcript_path and (match.group(1) if match else "") >= floor:
                candidates.append(path)
    return candidates


def tally_codex_rollout(transcript_path: Path) -> TokenUsage | None:
    """The session's usage: the parent rollout's last cumulative totals plus
    each subagent rollout's own (Codex keeps a child's usage out of the
    parent's totals).

    A parent whose first line is not a session_meta tallies alone. A child is
    a sibling (see `_codex_sibling_candidates`) whose session_meta names the
    parent as its `session_id` (the root thread, at any depth), or whose
    `parent_thread_id` chains into the parent's tree (children that predate
    `session_id`), iterated to a fixpoint. One thread written to two files
    counts once, from the later file. A child's usage makes the session
    tracked even when the parent reports none. An unreadable sibling
    (OSError/ValueError) is skipped and named in `TokenUsage.unread`, as are
    a failed sibling scan and an unreadable parent first line (both leave the
    children uncounted). Parent body errors propagate as before, and None
    still means no usage signal anywhere: a skip can only qualify a figure
    that exists. The budget guard's live samples ignore `unread`, since a
    child mid-write can tear a UTF-8 read; the engine's post-session read
    journals it."""
    parent_totals = _codex_rollout_total(transcript_path)
    unread: list[str] = []
    try:
        parent_meta = _codex_meta(transcript_path)
    except OSError as e:
        parent_meta = None
        unread.append(f"{transcript_path.name}: {type(e).__name__}")
    totals: list[dict[str, Any]] = [] if parent_totals is None else [parent_totals]
    if parent_meta is not None:
        root = parent_meta[0]
        metas: dict[str, _CodexMeta] = {}
        try:
            candidates = _codex_sibling_candidates(transcript_path)
        except OSError as e:
            candidates = []
            unread.append(f"{transcript_path.parent.name}/: {type(e).__name__}")
        for path in candidates:
            try:
                meta = _codex_meta(path)
            except OSError as e:
                unread.append(f"{path.name}: {type(e).__name__}")
                continue
            if meta is not None and meta[0] != root:
                metas[str(path)] = meta
        tree = {root}
        members = {path for path, meta in metas.items() if meta[1] == root}
        tree.update(metas[path][0] for path in members)
        grew = True
        while grew:
            grew = False
            for path, (thread_id, _, parent) in metas.items():
                if path not in members and parent in tree:
                    members.add(path)
                    tree.add(thread_id)
                    grew = True
        by_thread = {metas[path][0]: Path(path) for path in sorted(members)}
        for child in by_thread.values():
            try:
                child_totals = _codex_rollout_total(child)
            except (OSError, ValueError) as e:
                unread.append(f"{child.name}: {type(e).__name__}")
                continue
            if child_totals is not None:
                totals.append(child_totals)
    if not totals:
        return None
    usage = TokenUsage(unread=tuple(unread))
    for each in totals:
        usage.add(_codex_usage(each))
    return usage


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
