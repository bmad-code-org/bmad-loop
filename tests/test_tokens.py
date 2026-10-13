import json
import os

import pytest

from bmad_loop import tokens
from bmad_loop.model import TokenUsage
from bmad_loop.tokens import (
    read_usage,
    tally,
    tally_codex_rollout,
    tally_copilot_events,
    tally_gemini_chat,
)


def test_weighted_total():
    usage = TokenUsage(
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=1000,
        cache_creation_tokens=10,
    )
    assert usage.weighted_total(0.1) == 100 + 50 + 10 + 100
    assert usage.weighted_total(1.0) == usage.total
    assert usage.weighted_total(0.0) == 160


def test_tally_mixed_shapes(tmp_path):
    lines = [
        # Claude Code shape: usage nested in message
        {
            "type": "assistant",
            "message": {"usage": {"input_tokens": 100, "output_tokens": 50}},
        },
        # cache fields
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 300,
                }
            },
        },
        # top-level usage shape
        {"type": "message", "usage": {"input_tokens": 1, "output_tokens": 1}},
        # noise: no usage, malformed values tolerated
        {"type": "user", "message": {"content": "hi"}},
        {"type": "summary"},
    ]
    path = tmp_path / "t.jsonl"
    with path.open("w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
        f.write("not json at all\n")
        f.write("\n")

    usage = tally(path)
    assert usage is not None
    assert usage.input_tokens == 111
    assert usage.output_tokens == 56
    assert usage.cache_read_tokens == 2000
    assert usage.cache_creation_tokens == 300
    assert usage.total == 111 + 56 + 2000 + 300


def _write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _assistant(message_id, input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0):
    # Claude Code shape: one entry per content block, each repeating the
    # response's usage under the same message.id.
    message = {
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        }
    }
    if message_id is not None:
        message["id"] = message_id
    return {"type": "assistant", "message": message}


def test_tally_dedupes_content_blocks_by_message_id(tmp_path):
    path = tmp_path / "sid.jsonl"
    block = _assistant("msg_1", input_tokens=10, output_tokens=20, cache_read=300, cache_creation=4)
    _write_jsonl(path, [block, block, block, _assistant("msg_2", input_tokens=1, output_tokens=2)])

    usage = tally(path)
    assert usage == TokenUsage(
        input_tokens=11, output_tokens=22, cache_read_tokens=300, cache_creation_tokens=4
    )


def test_tally_dedupe_keeps_max_streamed_output(tmp_path):
    # output_tokens grows while the response streams; the final count is the
    # max, whatever order the blocks were written in.
    path = tmp_path / "sid.jsonl"
    _write_jsonl(
        path,
        [
            _assistant("msg_1", input_tokens=5, output_tokens=1),
            _assistant("msg_1", input_tokens=5, output_tokens=340),
            _assistant("msg_1", input_tokens=5, output_tokens=1),
        ],
    )

    usage = tally(path)
    assert usage is not None
    assert usage.input_tokens == 5
    assert usage.output_tokens == 340


def test_tally_entries_without_message_id_each_count(tmp_path):
    # Back-compat: id-less blocks and the top-level `usage` shape are not deduped.
    path = tmp_path / "sid.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(None, input_tokens=10, output_tokens=5),
            _assistant(None, input_tokens=10, output_tokens=5),
            {"type": "message", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ],
    )

    usage = tally(path)
    assert usage is not None
    assert usage.input_tokens == 30
    assert usage.output_tokens == 15


def test_tally_top_level_usage_beside_message_id_each_count(tmp_path):
    # The id keys only a message-level usage block; a top-level `usage` next
    # to a usage-less `message.id` is not deduped.
    path = tmp_path / "sid.jsonl"
    entry = {"message": {"id": "msg_1"}, "usage": {"input_tokens": 10, "output_tokens": 5}}
    _write_jsonl(path, [entry, entry])

    usage = tally(path)
    assert usage is not None
    assert usage.input_tokens == 20
    assert usage.output_tokens == 10


def test_tally_includes_subagent_transcripts(tmp_path):
    path = tmp_path / "sid.jsonl"
    _write_jsonl(path, [_assistant("msg_p", input_tokens=100, output_tokens=10)])
    subagents = tmp_path / "sid" / "subagents"
    _write_jsonl(
        subagents / "agent-a.jsonl", [_assistant("msg_a", input_tokens=7, output_tokens=3)]
    )
    # sidecar metadata is not a transcript
    (subagents / "agent-a.meta.json").write_text(
        json.dumps({"usage": {"input_tokens": 999}}), encoding="utf-8"
    )

    usage = tally(path)
    assert usage is not None
    assert usage.input_tokens == 107
    assert usage.output_tokens == 13


def test_tally_includes_nested_workflow_subagents(tmp_path):
    path = tmp_path / "sid.jsonl"
    _write_jsonl(path, [_assistant("msg_p", input_tokens=100)])
    _write_jsonl(
        tmp_path / "sid" / "subagents" / "workflows" / "wf_x" / "agent-b.jsonl",
        [_assistant("msg_b", input_tokens=40)],
    )

    usage = tally(path)
    assert usage is not None
    assert usage.input_tokens == 140


def test_tally_dedupes_across_forked_subagents(tmp_path):
    # A fork subagent copies its parent's history, message ids included, so
    # the dedupe spans every file of the session.
    shared = _assistant("msg_shared", input_tokens=50, output_tokens=5)
    path = tmp_path / "sid.jsonl"
    _write_jsonl(path, [shared])
    subagents = tmp_path / "sid" / "subagents"
    _write_jsonl(subagents / "agent-a.jsonl", [shared, _assistant("msg_a", input_tokens=1)])
    _write_jsonl(subagents / "agent-b.jsonl", [shared, _assistant("msg_b", input_tokens=2)])

    usage = tally(path)
    assert usage is not None
    assert usage.input_tokens == 53
    assert usage.output_tokens == 5


def test_tally_tracked_when_only_subagent_has_usage(tmp_path):
    path = tmp_path / "sid.jsonl"
    _write_jsonl(path, [{"type": "user", "message": {"content": "hi"}}])
    _write_jsonl(
        tmp_path / "sid" / "subagents" / "agent-a.jsonl",
        [_assistant("msg_a", input_tokens=9, output_tokens=1)],
    )

    usage = tally(path)
    assert usage is not None
    assert usage.input_tokens == 9
    assert usage.output_tokens == 1


def test_tally_no_subagent_dir_unchanged(tmp_path):
    path = tmp_path / "sid.jsonl"
    _write_jsonl(
        path,
        [
            _assistant("msg_1", input_tokens=10, output_tokens=2),
            _assistant("msg_2", input_tokens=20, output_tokens=4),
        ],
    )
    assert not (tmp_path / "sid").exists()

    usage = tally(path)
    assert usage is not None
    assert usage.input_tokens == 30
    assert usage.output_tokens == 6


def test_tally_missing_file(tmp_path):
    # Untracked, not free (DW-364): no transcript is no usage signal.
    assert tally(tmp_path / "nope.jsonl") is None


def test_tally_no_usage_lines_is_untracked(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "summary"},
        {"type": "assistant", "message": {"usage": {}}},  # empty block: no signal
        # non-empty block with no recognized token key: no signal either
        {"type": "assistant", "message": {"usage": {"service_tier": None}}},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + "garbage\n")
    assert tally(path) is None
    assert read_usage("claude-jsonl", path) is None


def test_tally_block_without_token_keys_is_untracked(tmp_path):
    path = tmp_path / "t.jsonl"
    entry = {"type": "assistant", "message": {"usage": {"service_tier": None}}}
    path.write_text(json.dumps(entry) + "\n")
    assert tally(path) is None


def test_tally_all_zero_usage_is_tracked_zero(tmp_path):
    # An all-zero usage block still counts as seen: tracked zero spend (DW-364).
    path = tmp_path / "t.jsonl"
    lines = [
        {"type": "assistant", "message": {"usage": {"input_tokens": 0, "output_tokens": 0}}},
        {"type": "message", "usage": {"input_tokens": 0}},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    usage = tally(path)
    assert usage is not None
    assert usage == TokenUsage()
    assert usage.total == 0


def test_codex_rollout_last_cumulative_wins(tmp_path):
    lines = [
        {"type": "session_meta", "payload": {"id": "abc"}},
        # token_count payloads are cumulative; only the last one counts
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 10,
                    }
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "agent_message"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 500,
                        "cached_input_tokens": 200,
                        "output_tokens": 60,
                    }
                },
            },
        },
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")

    usage = tally_codex_rollout(path)
    assert usage.input_tokens == 300  # cached portion split out of input
    assert usage.cache_read_tokens == 200
    assert usage.output_tokens == 60


def test_codex_rollout_without_token_counts_is_none(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}) + "\n")
    assert tally_codex_rollout(path) is None
    assert tally_codex_rollout(tmp_path / "nope.jsonl") is None


SESSION = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"


def _codex_rollout(home, day, session_id, *, utc="2026-10-11T23:30:00.000Z", output=7):
    """A minimal rollout at ``home/sessions/<day>/rollout-<ts>-<id>.jsonl``.
    ``day`` is the LOCAL date dir, independent of the UTC session_meta stamp."""
    folder = home / "sessions" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{day.replace('/', '-')}T00-30-00-{session_id}.jsonl"
    lines = [
        {"timestamp": utc, "type": "session_meta", "payload": {"id": session_id}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 50, "output_tokens": output}},
            },
        },
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def test_resolve_codex_rollout_by_session_id(tmp_path):
    """The rollout sits in the LOCAL-date dir (10/12) while its session_meta is
    stamped the previous UTC day: the lookup globs every day dir rather than
    deriving one from a timestamp, and the resolved file tallies.

    Ablation: make `find_codex_rollout` return None and this fails."""
    path = _codex_rollout(tmp_path, "2026/10/12", SESSION, utc="2026-10-11T23:30:00.000Z")
    env = {"CODEX_HOME": str(tmp_path)}

    assert tokens.resolve_transcript("codex-rollout", SESSION, env) == path
    # Any UUID spelling canonicalises to the filename's.
    assert tokens.resolve_transcript("codex-rollout", SESSION.upper(), env) == path
    assert read_usage("codex-rollout", path) == TokenUsage(input_tokens=50, output_tokens=7)


@pytest.mark.parametrize("session_id", ["*", "../x", "", None, "abc"])
def test_resolve_rejects_non_uuid_ids(tmp_path, session_id):
    """A hook payload is unvalidated, so an id that is not a UUID never reaches
    the glob — "*" would otherwise match the sentinel rollout below.

    Ablation: drop the `uuid.UUID` canonicalisation (glob the raw id) and the
    "*" row fails."""
    _codex_rollout(tmp_path, "2026/10/12", SESSION)
    env = {"CODEX_HOME": str(tmp_path)}
    assert tokens.resolve_transcript("codex-rollout", session_id, env) is None


def test_resolve_ignores_other_uuids_and_parsers(tmp_path):
    """Only the session's own rollout resolves, and only for codex-rollout: a
    claude or gemini transcript is never looked up by id.

    Ablation: drop the parser check in `resolve_transcript` and the
    claude-jsonl row resolves the codex file."""
    other = "019a2b3c-0000-7000-8000-000000000000"
    _codex_rollout(tmp_path, "2026/10/12", other)
    path = _codex_rollout(tmp_path, "2026/10/12", SESSION)
    env = {"CODEX_HOME": str(tmp_path)}

    assert tokens.resolve_transcript("codex-rollout", SESSION, env) == path
    missing = "019a2b3c-ffff-7fff-8fff-ffffffffffff"
    assert tokens.resolve_transcript("codex-rollout", missing, env) is None
    for parser in ("claude-jsonl", "gemini-chat", "copilot-events", ""):
        assert tokens.resolve_transcript(parser, SESSION, env) is None


def test_resolve_defaults_to_home_dot_codex(tmp_path, monkeypatch):
    """Without CODEX_HOME (or with it empty) the lookup roots at ~/.codex, read
    at call time. HOME and USERPROFILE are both patched: Windows' Path.home()
    reads USERPROFILE, never HOME.

    Ablation: drop the `Path.home() / ".codex"` default (return None) and this
    fails."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    path = _codex_rollout(tmp_path / ".codex", "2026/10/12", SESSION)

    assert tokens.resolve_transcript("codex-rollout", SESSION, {}) == path
    assert tokens.resolve_transcript("codex-rollout", SESSION, {"CODEX_HOME": ""}) == path


def test_resolve_newest_mtime_wins(tmp_path):
    """Two rollouts with one id (a resumed session written into a later day dir)
    resolve to the newer by mtime, whatever order the glob returns them in.

    Ablation: take the first glob match instead of the max mtime and one of the
    two assertions fails, whichever order the glob yields."""
    older = _codex_rollout(tmp_path, "2026/10/11", SESSION)
    newer = _codex_rollout(tmp_path, "2026/10/12", SESSION)
    os.utime(older, (2_000_000_000, 2_000_000_000))
    os.utime(newer, (1_000_000_000, 1_000_000_000))
    env = {"CODEX_HOME": str(tmp_path)}
    assert tokens.resolve_transcript("codex-rollout", SESSION, env) == older
    os.utime(newer, (3_000_000_000, 3_000_000_000))
    assert tokens.resolve_transcript("codex-rollout", SESSION, env) == newer


def test_gemini_chat_dedupes_reemitted_messages(tmp_path):
    # shape captured from a real ~/.gemini/tmp/<project>/chats/session-*.jsonl
    # (2026-06-11): a JSONL patch stream where the same message id is
    # re-emitted as it accretes content, and `input` includes `cached`.
    lines = [
        {"sessionId": "s1", "projectHash": "x", "kind": "main"},
        {"$set": {"messages": [{"id": "u1", "type": "user", "content": []}]}},
        {
            "id": "g1",
            "type": "gemini",
            "tokens": {
                "input": 12273,
                "output": 45,
                "cached": 0,
                "thoughts": 87,
                "tool": 0,
            },
        },
        {"$set": {"lastUpdated": "..."}},
        # same message re-emitted with toolCalls added: must not double-count
        {
            "id": "g1",
            "type": "gemini",
            "toolCalls": [{}],
            "tokens": {
                "input": 12273,
                "output": 45,
                "cached": 0,
                "thoughts": 87,
                "tool": 0,
            },
        },
        {
            "id": "g2",
            "type": "gemini",
            "tokens": {
                "input": 12429,
                "output": 2,
                "cached": 11367,
                "thoughts": 16,
                "tool": 0,
            },
        },
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")

    usage = tally_gemini_chat(path)
    assert usage.input_tokens == 12273 + (12429 - 11367)  # cached split out of input
    assert usage.cache_read_tokens == 11367
    assert usage.output_tokens == (45 + 87) + (2 + 16)  # output + thoughts


def test_gemini_chat_without_tokens_is_none(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"id": "u1", "type": "user", "content": []}) + "\n")
    assert tally_gemini_chat(path) is None
    assert tally_gemini_chat(tmp_path / "nope.jsonl") is None


def test_copilot_events_last_cumulative_across_models(tmp_path):
    # shape from ~/.copilot/session-state/<session>/events.jsonl: per line
    # {id, type, data:{...}}; data.modelMetrics.<model>.usage is cumulative.
    lines = [
        {"id": "e0", "type": "session_start", "data": {"sessionId": "s1"}},
        # an earlier, smaller cumulative snapshot — superseded by the last one
        {
            "id": "e1",
            "type": "metrics",
            "data": {"modelMetrics": {"gpt-5-mini": {"usage": {"inputTokens": 100}}}},
        },
        {"id": "e2", "type": "message", "data": {"content": "noise"}},
        # final cumulative snapshot, two models — totals come from here
        {
            "id": "e3",
            "type": "metrics",
            "data": {
                "modelMetrics": {
                    "gpt-5-mini": {
                        "usage": {
                            "inputTokens": 500,
                            "outputTokens": 60,
                            "cacheReadTokens": 200,
                            "cacheWriteTokens": 30,
                            "reasoningTokens": 5,
                        }
                    },
                    "gpt-5": {
                        "usage": {
                            "inputTokens": 40,
                            "outputTokens": 8,
                            "reasoningTokens": 2,
                        }
                    },
                }
            },
        },
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")

    usage = tally_copilot_events(path)
    assert usage.input_tokens == 540  # 500 + 40
    assert usage.output_tokens == 75  # (60 + 5) + (8 + 2), reasoning folded in
    assert usage.cache_read_tokens == 200
    assert usage.cache_creation_tokens == 30


def test_copilot_events_without_metrics_is_none(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"id": "e0", "type": "message", "data": {"content": "hi"}}) + "\n")
    assert tally_copilot_events(path) is None
    assert tally_copilot_events(tmp_path / "nope.jsonl") is None


def test_read_usage_dispatch(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"usage": {"input_tokens": 1, "output_tokens": 2}}) + "\n")
    assert read_usage("claude-jsonl", path).total == 3
    assert read_usage("none", path) is None

    cop = tmp_path / "events.jsonl"
    cop.write_text(
        json.dumps({"data": {"modelMetrics": {"m": {"usage": {"inputTokens": 7}}}}}) + "\n"
    )
    assert read_usage("copilot-events", cop).input_tokens == 7
