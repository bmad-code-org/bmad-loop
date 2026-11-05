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
                 ids. Entries without an id each count. Per-model split: a
                 deduped response bills to the first model its blocks name.
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
                 Per-model split: the deltas between consecutive token_count
                 readings bill to the latest `turn_context` model.
- gemini-chat:   ~/.gemini/tmp/<project>/chats/session-*.jsonl — a JSONL
                 patch stream: bare message entries (and messages inside
                 "$set" patches) carry a per-API-call `tokens` block
                 {input, output, cached, thoughts, tool, total}; the same
                 message id is re-emitted as it accretes content, so the
                 last occurrence per id wins, then unique messages are
                 summed. `input` includes the cached portion. Per-model
                 split: an id's winning `tokens` bill to the last model any
                 `type == "gemini"` occurrence of that id named.
- copilot-events: ~/.copilot/session-state/<session>/events.jsonl
                 (Copilot CLI 1.0.105); each turn's `session.usage_checkpoint`
                 (under `data.accountingSnapshot`) and the `session.shutdown`
                 line carry `modelMetrics.<model-id>.usage`
                 {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens,
                 reasoningTokens} that is CUMULATIVE per model, so the last
                 snapshot holds the session totals (summed across models; the
                 split keeps them per model). inputTokens includes the cache
                 reads and writes, which are subtracted; reasoningTokens are
                 part of outputTokens. Only the shutdown line is final
                 (`read_usage_settled`).
- antigravity-transcript: ~/.gemini/antigravity-cli/brain/<conversation-id>/
                 .system_generated/logs/transcript_full.jsonl (agy 1.4.9);
                 each PLANNER_RESPONSE step (one API call) carries top-level
                 input_tokens (exclusive of cache reads), cache_read_tokens and
                 output_tokens, summed per step. A subagent is its own
                 conversation: the parent's .system_generated/subagents/
                 <child-id>.json spawn markers name the children, whose own
                 transcripts are folded in recursively. No model id is
                 written (checked through agy 1.5.0), so models (and the
                 per-model split) stay untracked (None); agy names the model
                 only on its Stop hook payload (`modelName`), which feeds
                 the `session-model` journal row, not `models`.

A tracked parser's `TokenUsage.by_model` (DW-618) sums field-wise to its
totals; usage it cannot pin on a model lands under `UNATTRIBUTED_MODEL`.
"""

from __future__ import annotations

import datetime
import fnmatch
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .model import TokenUsage, add_to_bucket, settle_by_model


def read_usage(parser: str, transcript_path: Path) -> TokenUsage | None:
    if parser == "claude-jsonl":
        return tally(transcript_path)
    if parser == "codex-rollout":
        return tally_codex_rollout(transcript_path)
    if parser == "gemini-chat":
        return tally_gemini_chat(transcript_path)
    if parser == "copilot-events":
        return tally_copilot_events(transcript_path)
    if parser == "antigravity-transcript":
        return tally_antigravity_transcript(transcript_path)
    return None


def read_usage_settled(parser: str, transcript_path: Path) -> tuple[TokenUsage | None, bool]:
    """``(usage, settled)``: `read_usage`, plus whether the transcript holds its
    final totals. A post-session read polls until ``settled`` (or its grace
    runs out), because a read that is not final can already be non-None.

    Only copilot-events reads early: each turn's checkpoint lands before the
    next turn runs, so a read is settled only once `session.shutdown` is
    written (on /exit and when the window is killed). Every other parser
    reads settled."""
    if parser == "copilot-events":
        return _copilot_tally(transcript_path)
    return read_usage(parser, transcript_path), True


def codex_home(env: Mapping[str, str]) -> Path | None:
    """Codex's state root: ``CODEX_HOME`` when set and non-empty, else
    ``.codex`` under the home ``env`` names (``USERPROFILE`` on Windows,
    ``HOME`` elsewhere, as ``Path.home()`` reads them), else under this
    process's home; None when no home directory can be determined."""
    configured = env.get("CODEX_HOME")
    if configured:
        return Path(configured)
    home = env.get("USERPROFILE" if os.name == "nt" else "HOME")
    if home:
        return Path(home) / ".codex"
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


# Claude Code's zero-usage placeholder model on a locally synthesized
# (API-error) assistant entry: not a model the session ran.
_CLAUDE_SYNTHETIC_MODEL = "<synthetic>"


def _note_claude_model(entry: dict[str, Any], models: dict[str, None]) -> str | None:
    """Record the entry's `message.model` in `models` (insertion-ordered set)
    and return it, or None when the entry names no usable model.

    Independent of the usage dedupe: every block of a response repeats the
    same model, and the set keeps only distinct ids. Skips a non-str or empty
    value and the `<synthetic>` placeholder."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    model = message.get("model")
    if isinstance(model, str) and model and model != _CLAUDE_SYNTHETIC_MODEL:
        models.setdefault(model, None)
        return model
    return None


def _claude_usage(counts: dict[str, int]) -> TokenUsage:
    return TokenUsage(
        input_tokens=counts["input_tokens"],
        output_tokens=counts["output_tokens"],
        cache_read_tokens=counts["cache_read_input_tokens"],
        cache_creation_tokens=counts["cache_creation_input_tokens"],
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

    `models` holds the distinct assistant `message.model` ids in walk order
    (main transcript first, then subagents), `()` when none was found.
    `by_model` bills each deduped response to the first usable model any of
    its blocks named, and each id-less entry to its own; the rest (no model,
    or only `<synthetic>`) is `UNATTRIBUTED_MODEL`.

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
    id_model: dict[str, str] = {}  # message id -> first usable model it named
    anon: list[tuple[dict[str, int], str | None]] = []
    models: dict[str, None] = {}  # distinct, first-seen order
    seen = False
    for path in paths:
        for entry in _jsonl_entries(path):
            model = _note_claude_model(entry, models)
            message = entry.get("message")
            # Any block of a response may name its model, usage-bearing or
            # not: associate it before the usage guard below.
            if model is not None and isinstance(message, dict):
                block_id = message.get("id")
                if isinstance(block_id, str):
                    id_model.setdefault(block_id, model)
            usage = _usage_block(entry)
            # Absent, empty `{}`, or no recognized token key: no usage signal here.
            if not usage or not any(key in usage for key in _CLAUDE_USAGE_KEYS):
                continue
            seen = True
            counts = {key: _int(usage.get(key)) for key in _CLAUDE_USAGE_KEYS}
            # Only a message-level usage block is keyed by its message's id; a
            # top-level `usage` counts on its own even beside a `message.id`.
            message_id = (
                message.get("id")
                if isinstance(message, dict) and message.get("usage") is usage
                else None
            )
            if not isinstance(message_id, str):
                anon.append((counts, model))
                continue
            merged = by_id.setdefault(message_id, dict.fromkeys(_CLAUDE_USAGE_KEYS, 0))
            for key, value in counts.items():
                merged[key] = max(merged[key], value)
    if not seen:
        return None
    buckets: dict[str | None, TokenUsage] = {}
    attributed = [(counts, id_model.get(message_id)) for message_id, counts in by_id.items()]
    total = TokenUsage()
    for counts, model in [*attributed, *anon]:
        usage = _claude_usage(counts)
        total.add(usage)
        add_to_bucket(buckets, model, usage)
    found = tuple(models)
    total.models = found
    total.by_model = settle_by_model(buckets, found)
    return total


# ----------------------------------------------------------- codex-rollout


# The raw cumulative `token_count` fields a per-model delta is taken over.
_CODEX_RAW_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")


def _codex_rollout_total(
    transcript_path: Path,
    models: dict[str, None],
    buckets: dict[str | None, dict[str, int]],
) -> dict[str, Any] | None:
    """The rollout's last cumulative `token_count` totals, or None when it has
    none. A subagent's rollout holds only its own turns, so this rule holds
    for children as well as for the parent. The same read records each
    `turn_context` line's `payload.model` (a non-empty str) in `models`, an
    insertion-ordered set, and adds each reading's raw delta over the previous
    one (`_CODEX_RAW_KEYS`) to `buckets` under the latest `turn_context`'s
    model (None before any, or when it names none): the deltas telescope, so
    the buckets sum to the last reading. OSError/ValueError propagate, with
    `buckets` partly filled — a caller that skips the file passes a scratch
    dict."""
    last: dict[str, Any] | None = None
    current: str | None = None
    previous = dict.fromkeys(_CODEX_RAW_KEYS, 0)
    for entry in _jsonl_entries(transcript_path):
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        if entry.get("type") == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                models.setdefault(model, None)
                current = model
            else:
                current = None
            continue
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        totals = info.get("total_token_usage")
        last = totals if isinstance(totals, dict) else info
        reading = {key: _int(last.get(key)) for key in _CODEX_RAW_KEYS}
        bucket = buckets.setdefault(current, dict.fromkeys(_CODEX_RAW_KEYS, 0))
        for key in _CODEX_RAW_KEYS:
            bucket[key] += reading[key] - previous[key]
        previous = reading
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
# good; only a first line still being written (no newline yet) is re-read, and
# a complete first line that is not JSON raises, uncached, every time.
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
    """None for a JSON line that is not a usable session_meta; ValueError
    (JSONDecodeError, UnicodeDecodeError) for one that is not JSON at all."""
    entry = json.loads(line)
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
    OSError propagates, and so does ValueError for a complete first line that
    does not parse; a first line still being written reads as None."""
    key = str(path)
    with _codex_meta_lock:
        if key in _codex_meta_cache:
            return _codex_meta_cache[key]
    line = _read_codex_first_line(path)
    complete = line.endswith(b"\n") or len(line) >= _CODEX_META_READ_CAP
    try:
        meta = _parse_codex_meta(line)
    except ValueError:
        if complete:
            raise
        meta = None
    if complete:
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


def _codex_sibling_candidates(transcript_path: Path, unread: list[str]) -> list[Path]:
    """Rollouts that may be the parent's subagents: `rollout-*.jsonl` in the
    parent's day dir and each later day dir through the day after the parent's
    last write (capped at local today + 1; dirs are local dates and a child
    can start after midnight), whose filename timestamp is not earlier than
    the parent's. A parent outside a YYYY/MM/DD tree searches its own dir.
    A missing day dir holds no rollouts; one that cannot be listed is named
    in ``unread`` (by its date path) and the other dirs are still searched —
    listed directly, since ``Path.glob`` swallows a scan's OSError."""
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
        try:
            paths = sorted(each.iterdir())
        except FileNotFoundError:
            continue
        except OSError as e:
            unread.append(f"{'/'.join(each.parts[-3:])}/: {type(e).__name__}")
            continue
        for path in paths:
            if not fnmatch.fnmatchcase(path.name, "rollout-*.jsonl"):
                continue
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
    an unlistable day dir and an unreadable or unparseable parent first line
    (which leaves the children uncounted). Parent body errors propagate as before, and None
    still means no usage signal anywhere: a skip can only qualify a figure
    that exists. The budget guard's live samples ignore `unread`, since a
    child mid-write can tear a UTF-8 read; the engine's post-session read
    journals it.

    `models` holds the distinct `turn_context` model ids, the parent's first,
    then each read child's, `()` when none was found; an unreadable child adds
    none. `by_model` sums each rollout's per-model raw deltas (see
    `_codex_rollout_total`) across the parent and the read children, then
    splits each model's cached input out as for the totals; usage read before
    any `turn_context` is `UNATTRIBUTED_MODEL`."""
    models: dict[str, None] = {}  # distinct, first-seen order
    raw: dict[str | None, dict[str, int]] = {}
    parent_totals = _codex_rollout_total(transcript_path, models, raw)
    unread: list[str] = []
    try:
        parent_meta = _codex_meta(transcript_path)
    except (OSError, ValueError) as e:
        parent_meta = None
        unread.append(f"{transcript_path.name}: {type(e).__name__}")
    totals: list[dict[str, Any]] = [] if parent_totals is None else [parent_totals]
    if parent_meta is not None:
        root = parent_meta[0]
        metas: dict[str, _CodexMeta] = {}
        for path in _codex_sibling_candidates(transcript_path, unread):
            try:
                meta = _codex_meta(path)
            except (OSError, ValueError) as e:
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
            child_models: dict[str, None] = {}
            child_raw: dict[str | None, dict[str, int]] = {}
            try:
                child_totals = _codex_rollout_total(child, child_models, child_raw)
            except (OSError, ValueError) as e:
                unread.append(f"{child.name}: {type(e).__name__}")
                continue
            for model in child_models:
                models.setdefault(model, None)
            for model, counts in child_raw.items():
                merged = raw.setdefault(model, dict.fromkeys(_CODEX_RAW_KEYS, 0))
                for key, value in counts.items():
                    merged[key] += value
            if child_totals is not None:
                totals.append(child_totals)
    if not totals:
        return None
    found = tuple(models)
    usage = TokenUsage(unread=tuple(unread), models=found)
    for each in totals:
        usage.add(_codex_usage(each))
    buckets = {model: _codex_usage(counts) for model, counts in raw.items()}
    usage.by_model = settle_by_model(buckets, found)
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
    """Sum the last `tokens` record per message id, or None when none has one.

    `models` holds the distinct `model` ids of `type == "gemini"` messages in
    file order (a re-appended message repeats its id), `()` when none was
    found. `by_model` bills each message's winning `tokens` to the last model
    an occurrence of that id named (`type == "gemini"`), so a re-emission
    without `model` keeps it; else `UNATTRIBUTED_MODEL`."""
    by_id: dict[str, dict[str, Any]] = {}
    id_model: dict[str, str] = {}  # message id -> last model an occurrence named
    models: dict[str, None] = {}  # distinct, first-seen order
    for entry in _jsonl_entries(transcript_path):
        for message in _gemini_messages(entry):
            model = message.get("model")
            message_id = message.get("id")
            if message.get("type") == "gemini" and isinstance(model, str) and model:
                models.setdefault(model, None)
                if isinstance(message_id, str):
                    id_model[message_id] = model
            tokens = message.get("tokens")
            if isinstance(tokens, dict) and isinstance(message_id, str):
                by_id[message_id] = tokens
    if not by_id:
        return None
    buckets: dict[str | None, TokenUsage] = {}
    total = TokenUsage()
    for message_id, tokens in by_id.items():
        named = id_model.get(message_id)
        cached = _int(tokens.get("cached"))
        usage = TokenUsage(
            input_tokens=max(0, _int(tokens.get("input")) - cached),
            output_tokens=_int(tokens.get("output")) + _int(tokens.get("thoughts")),
            cache_read_tokens=cached,
        )
        total.add(usage)
        add_to_bucket(buckets, named, usage)
    found = tuple(models)
    total.models = found
    total.by_model = settle_by_model(buckets, found)
    return total


# --------------------------------------------------------- copilot-events


def _copilot_metrics(data: dict[str, Any]) -> dict[str, Any] | None:
    """The non-empty `modelMetrics` snapshot an events.jsonl `data` block
    carries, or None: `session.shutdown` has it at the top level, and every
    `session.usage_checkpoint` (written after each turn) only under
    `accountingSnapshot`."""
    for holder in (data, data.get("accountingSnapshot")):
        if isinstance(holder, dict):
            metrics = holder.get("modelMetrics")
            if isinstance(metrics, dict) and metrics:
                return metrics
    return None


def tally_copilot_events(transcript_path: Path) -> TokenUsage | None:
    """Sum the last cumulative `modelMetrics` snapshot of a Copilot
    events.jsonl, or None when no entry carries one.

    Checked against Copilot CLI 1.0.105 on Linux (tests/fixtures/
    copilot-1.0.105/): a `session.usage_checkpoint` after every turn and the
    `session.shutdown` line (written on /exit and when the window is killed)
    each carry the session's running totals per model, keyed by bare model id
    (`mai-code-1.1-flash`). So the LAST snapshot holds the session totals, and
    a session that ends without a shutdown line still reads its last
    checkpoint. `inputTokens` includes the cache reads (its `tokenDetails`
    split shows `input + cache_read`) and, per the Copilot SDK's usage-event
    docs, the cache writes, so both are subtracted (the 1.0.105 capture wrote
    none). `reasoningTokens` is a subset of `outputTokens` (same docs), so it
    is not added again.

    `session.start` `selectedModel` is NOT a model source: it echoes the
    `--model` flag verbatim (absent without one), even when Copilot rejects
    the model and routes to another, so it can name a model that never ran."""
    return _copilot_tally(transcript_path)[0]


def _copilot_tally(transcript_path: Path) -> tuple[TokenUsage | None, bool]:
    """`tally_copilot_events`, plus whether a `session.shutdown` line was read
    (the final totals; see `read_usage_settled`)."""
    last: dict[str, Any] | None = None
    shut_down = False
    for entry in _jsonl_entries(transcript_path):
        if entry.get("type") == "session.shutdown":
            shut_down = True
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        metrics = _copilot_metrics(data)
        if metrics is not None:
            last = metrics
    if last is None:
        return None, shut_down
    # The models are the last modelMetrics' keys, in insertion order, and each
    # key's usage is its own `by_model` bucket.
    found = tuple(key for key in last if isinstance(key, str) and key)
    buckets: dict[str | None, TokenUsage] = {}
    total = TokenUsage()
    for key, model_metrics in last.items():
        if not isinstance(model_metrics, dict):
            continue
        usage = model_metrics.get("usage")
        if not isinstance(usage, dict):
            continue
        cached = _int(usage.get("cacheReadTokens"))
        written = _int(usage.get("cacheWriteTokens"))
        each = TokenUsage(
            input_tokens=max(0, _int(usage.get("inputTokens")) - cached - written),
            output_tokens=_int(usage.get("outputTokens")),
            cache_read_tokens=cached,
            cache_creation_tokens=written,
        )
        total.add(each)
        add_to_bucket(buckets, key if isinstance(key, str) and key else None, each)
    total.models = found
    total.by_model = settle_by_model(buckets, found)
    return total, shut_down


# ------------------------------------------------- antigravity-transcript

_AGY_USAGE_KEYS = ("input_tokens", "cache_read_tokens", "output_tokens")
# The file every conversation's usage lives in, under <conv>/AGY_LOGS_DIR.
_AGY_TRANSCRIPT = "transcript_full.jsonl"
_AGY_LOGS_DIR = (".system_generated", "logs")
# Mirrors the antigravity profile's `subagent_spawn_marker`
# (`.system_generated/subagents/{session_id}.json`): the folder, under a
# conversation, that lists the children it spawned.
_AGY_SUBAGENTS_DIR = (".system_generated", "subagents")


def _agy_steps(transcript_path: Path, *, required: bool = False) -> list[dict[str, int]] | None:
    """The transcript's per-step usage counts, or None when no step carries one.

    A step with an int `step_index` counts once, from its last usage-bearing
    occurrence; one without each counts. OSError/ValueError propagate. A
    missing or non-regular file reads as None, unless ``required`` (a child a
    spawn marker names): then a missing file raises FileNotFoundError and a
    non-regular one ValueError, so the absence is reported, never a zero."""
    if required and not stat.S_ISREG(transcript_path.stat().st_mode):
        raise ValueError(f"{transcript_path} is not a regular file")
    by_step: dict[int, dict[str, int]] = {}
    anon: list[dict[str, int]] = []
    for entry in _jsonl_entries(transcript_path):
        if not any(key in entry for key in _AGY_USAGE_KEYS):
            continue
        counts = {key: _int(entry.get(key)) for key in _AGY_USAGE_KEYS}
        index = entry.get("step_index")
        if isinstance(index, int) and not isinstance(index, bool):
            by_step[index] = counts
        else:
            anon.append(counts)
    if not by_step and not anon:
        return None
    return [*by_step.values(), *anon]


def _agy_children(conversation: Path, label: str, unread: list[str]) -> list[str]:
    """The child conversation ids `conversation`'s spawn markers name, sorted.

    Only a `.json` marker whose stem is a canonical UUID names a child, so a
    garbled name can never leave the brain store. A missing folder lists none;
    one that cannot be listed is named in ``unread`` as ``<label>subagents/``."""
    folder = conversation.joinpath(*_AGY_SUBAGENTS_DIR)
    try:
        names = sorted(each.name for each in folder.iterdir())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        unread.append(f"{label}subagents/: {type(e).__name__}")
        return []
    children: list[str] = []
    for name in names:
        stem, dot, suffix = name.rpartition(".")
        if not dot or suffix != "json":
            continue
        try:
            canonical = str(uuid.UUID(stem))
        except ValueError:
            continue
        if canonical == stem:
            children.append(stem)
    return children


def tally_antigravity_transcript(transcript_path: Path) -> TokenUsage | None:
    """Sum the per-step usage of an agy conversation and its subagents, or None
    when no step anywhere carries usage.

    Verified against agy 1.4.9: each `PLANNER_RESPONSE` step (one API call)
    carries top-level `input_tokens` (exclusive of cache reads),
    `cache_read_tokens` and `output_tokens`; there are no running totals, so
    steps are summed (see `_agy_steps` for duplicate steps). A step whose counts
    are all zero still counts as tracked. Earlier agy (1.1.3) wrote no counts,
    which reads as None.

    A subagent is its own conversation, absent from the parent's transcript: at
    spawn agy writes brain/<conv>/.system_generated/subagents/<childId>.json
    (the profile's `subagent_spawn_marker`), and the child logs to
    brain/<childId>/.system_generated/logs/transcript_full.jsonl. So when the
    path has agy's `<conv>/.system_generated/logs/<file>` shape, each listed
    child is folded in, recursively, every conversation at most once; any other
    path tallies alone. A child's usage makes the session tracked even when the
    parent reports none. An unreadable child transcript (OSError/ValueError) —
    one a marker names but that is missing or not a regular file included — is
    skipped and named in `TokenUsage.unread` as
    `<childId>/transcript_full.jsonl: <ExcType>`, an unlistable marker folder as
    `subagents/: <ExcType>` (prefixed `<childId>/` for a child's); the totals
    are then a lower bound. Errors on the parent transcript itself propagate.

    The 1.5.0 transcript has the same step layout. `models` is None: the
    transcript names no model through 1.5.0 (the Stop hook payload's
    `modelName` is the only place agy reports one; it feeds the adapter's
    `session-model` row, never `models`)."""
    steps: list[dict[str, int]] = []
    parent_steps = _agy_steps(transcript_path)
    seen = parent_steps is not None
    steps.extend(parent_steps or ())
    unread: list[str] = []
    logs = transcript_path.parent
    if (logs.parent.name, logs.name) == _AGY_LOGS_DIR:
        conversation = logs.parent.parent
        brain = conversation.parent
        visited = {conversation.name}
        pending = [(conversation, "")]
        while pending:
            folder, label = pending.pop(0)
            for child in _agy_children(folder, label, unread):
                if child in visited:
                    continue
                visited.add(child)
                child_folder = brain / child
                try:
                    child_steps = _agy_steps(
                        child_folder.joinpath(*_AGY_LOGS_DIR, _AGY_TRANSCRIPT), required=True
                    )
                except (OSError, ValueError) as e:
                    unread.append(f"{child}/{_AGY_TRANSCRIPT}: {type(e).__name__}")
                else:
                    if child_steps is not None:
                        seen = True
                        steps.extend(child_steps)
                pending.append((child_folder, f"{child}/"))
    if not seen:
        return None
    total = TokenUsage(unread=tuple(unread))
    for counts in steps:
        total.add(
            TokenUsage(
                input_tokens=counts["input_tokens"],
                output_tokens=counts["output_tokens"],
                cache_read_tokens=counts["cache_read_tokens"],
            )
        )
    return total
