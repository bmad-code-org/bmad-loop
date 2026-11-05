import datetime
import json
import os
import shutil
from pathlib import Path

import pytest

from bmad_loop import tokens
from bmad_loop.model import UNATTRIBUTED_MODEL, TokenUsage
from bmad_loop.tokens import (
    read_usage,
    read_usage_settled,
    tally,
    tally_antigravity_transcript,
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


def _with_model(entry, model):
    entry["message"]["model"] = model
    return entry


def test_tally_collects_distinct_models_main_first(tmp_path):
    # #859: distinct ids in walk order — main transcript first, then the
    # subagents — independent of the message.id dedupe; Claude Code's
    # `<synthetic>` API-error placeholder and empty/non-str values are skipped.
    path = tmp_path / "sid.jsonl"
    opus = _with_model(_assistant("msg_1", input_tokens=10), "claude-opus-5-5")
    _write_jsonl(
        path,
        [
            opus,
            opus,  # a repeated content block of the same response
            _with_model(_assistant("msg_err", input_tokens=0), "<synthetic>"),
            _with_model(_assistant("msg_2", input_tokens=5), "claude-sonnet-5-5"),
            _with_model(_assistant("msg_3", input_tokens=5), "claude-opus-5-5"),
            _with_model(_assistant("msg_4", input_tokens=1), ""),
            _with_model(_assistant("msg_5", input_tokens=1), 7),
            {"type": "user", "message": {"role": "user", "content": "hi"}},
        ],
    )
    _write_jsonl(
        tmp_path / "sid" / "subagents" / "agent-a.jsonl",
        [
            _with_model(_assistant("msg_a", input_tokens=3), "claude-haiku-5-5"),
            _with_model(_assistant("msg_b", input_tokens=3), "claude-opus-5-5"),
        ],
    )

    usage = tally(path)
    assert usage is not None
    assert usage.models == ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
    assert usage.input_tokens == 10 + 5 + 5 + 1 + 1 + 3 + 3  # dedupe untouched


def test_tally_models_empty_when_no_assistant_model(tmp_path):
    # Tracked but none found is `()`, never None: the claude parser has a model
    # source, so the engine journals `models_unresolved` rather than nothing.
    path = tmp_path / "sid.jsonl"
    _write_jsonl(
        path,
        [
            _assistant("msg_1", input_tokens=10),
            _with_model(_assistant("msg_err", input_tokens=0), "<synthetic>"),
        ],
    )

    usage = tally(path)
    assert usage is not None
    assert usage.models == ()


def test_gemini_chat_models_empty_without_model(tmp_path):
    # A tracking parser that finds no model returns `()`, never `None`, so the
    # engine journals `models_unresolved` for it.
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"id": "g1", "type": "gemini", "tokens": {"input": 4, "output": 2}}) + "\n",
        encoding="utf-8",
    )

    usage = tally_gemini_chat(path)
    assert usage is not None
    assert usage.models == ()


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


def test_resolve_default_home_comes_from_the_passed_env(tmp_path, monkeypatch):
    """The pane's env decides where Codex writes: a HOME/USERPROFILE override
    there must root the fallback, not this process's own home.

    Ablation: read `Path.home()` unconditionally and this fails."""
    monkeypatch.setenv("HOME", str(tmp_path / "controller"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "controller"))
    pane_home = tmp_path / "pane"
    path = _codex_rollout(pane_home / ".codex", "2026/10/12", SESSION)
    env = {"HOME": str(pane_home), "USERPROFILE": str(pane_home)}

    assert tokens.resolve_transcript("codex-rollout", SESSION, env) == path


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


ROOT = "019a0000-0000-7000-8000-000000000001"
CHILD = "019a0000-0000-7000-8000-000000000002"
GRANDCHILD = "019a0000-0000-7000-8000-000000000003"
OTHER = "019a0000-0000-7000-8000-000000000004"


def _local_ts(day, hour=12, minute=0):
    year, month, dom = (int(part) for part in day.split("/"))
    return datetime.datetime(year, month, dom, hour, minute).timestamp()


def _codex_thread(
    home, day, clock, thread_id, *, session_id=None, parent=None, usage=None, models=()
):
    """A rollout at ``home/sessions/<day>/rollout-<day>T<clock>-<id>.jsonl``
    whose session_meta carries the given tree fields, one `turn_context` line
    per entry of ``models``, plus one cumulative token_count when ``usage`` =
    (input, cached, output). Its mtime is pinned to noon of its LOCAL ``day``,
    which bounds the sibling search window."""
    payload = {"id": thread_id}
    if session_id is not None:
        payload["session_id"] = session_id
    if parent is not None:
        payload["parent_thread_id"] = parent
    lines = [{"type": "session_meta", "payload": payload}]
    lines.extend({"type": "turn_context", "payload": {"model": model}} for model in models)
    if usage is not None:
        input_tokens, cached, output = usage
        totals = {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output,
        }
        lines.append(
            {
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": totals}},
            }
        )
    path = home / "sessions" / day / f"rollout-{day.replace('/', '-')}T{clock}-{thread_id}.jsonl"
    _write_jsonl(path, lines)
    stamp = _local_ts(day)
    os.utime(path, (stamp, stamp))
    return path


def test_codex_tally_sums_subagent_rollouts(tmp_path):
    """Codex keeps each subagent's usage in its own rollout, out of the
    parent's totals: a depth-1 and a depth-2 child (both naming the root as
    session_id) add their own last cumulative totals; an unrelated rollout
    from the same day does not.

    Ablation: drop the `session_id == root` filter (every sibling a member)
    and the unrelated rollout is counted."""
    parent = _codex_thread(
        tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT, usage=(100, 40, 10)
    )
    _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-05-00",
        CHILD,
        session_id=ROOT,
        parent=ROOT,
        usage=(50, 10, 5),
    )
    _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-06-00",
        GRANDCHILD,
        session_id=ROOT,
        parent=CHILD,
        usage=(20, 0, 2),
    )
    _codex_thread(
        tmp_path, "2026/10/01", "10-07-00", OTHER, session_id=OTHER, usage=(1000, 0, 1000)
    )

    assert tally_codex_rollout(parent) == TokenUsage(
        input_tokens=60 + 40 + 20, output_tokens=17, cache_read_tokens=50
    )


def test_codex_tally_collects_turn_context_models(tmp_path):
    """#859: the distinct `turn_context` model ids, parent first, then each
    child, in first-seen order; a repeated turn adds nothing and an unrelated
    rollout's model is not the session's.

    Ablation: drop the `turn_context` branch in `_codex_rollout_total` and
    `models` is `()`; drop the child merge and the child's model is missing."""
    parent = _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-00-00",
        ROOT,
        session_id=ROOT,
        usage=(10, 0, 1),
        models=("gpt-5.5-codex", "gpt-5.5-codex", "gpt-5.5"),
    )
    _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-05-00",
        CHILD,
        session_id=ROOT,
        parent=ROOT,
        usage=(5, 0, 5),
        models=("gpt-5.5", "gpt-5.5-mini", ""),
    )
    _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-07-00",
        OTHER,
        session_id=OTHER,
        usage=(1, 0, 1),
        models=("other-model",),
    )

    usage = tally_codex_rollout(parent)
    assert usage is not None
    assert usage.models == ("gpt-5.5-codex", "gpt-5.5", "gpt-5.5-mini")


def test_codex_tally_models_empty_without_turn_context(tmp_path):
    """A rollout with usage but no `turn_context` line tracks models and found
    none: `()`, never `None`.

    Ablation: build the TokenUsage without `models=` and it is `None`."""
    parent = _codex_thread(
        tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT, usage=(10, 0, 1)
    )

    usage = tally_codex_rollout(parent)
    assert usage is not None
    assert usage.models == ()


def test_codex_tally_unreadable_child_adds_no_models(tmp_path):
    """A child whose body fails to decode after its `turn_context` line is
    named in `unread` and contributes no model, even the one read before the
    fault.

    Ablation: scan the child into the shared `models` dict directly and its
    model leaks in."""
    parent = _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-00-00",
        ROOT,
        session_id=ROOT,
        usage=(10, 0, 1),
        models=("gpt-5.5",),
    )
    broken = _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-06-00",
        GRANDCHILD,
        session_id=ROOT,
        parent=ROOT,
        models=("gpt-5.5-mini",),
    )
    # Pad past the text reader's first decode chunk so the `turn_context`
    # line is yielded before the fault, as in a large real rollout.
    filler = json.dumps({"type": "event_msg", "payload": {"type": "noise", "pad": "x" * 200}})
    with broken.open("ab") as f:
        f.write((filler + "\n").encode("utf-8") * 200)
        f.write(b'{"type": "event_msg", "payload": "\xff\xfe"}\n')

    usage = tally_codex_rollout(parent)
    assert usage is not None
    assert usage.unread == (f"{broken.name}: UnicodeDecodeError",)
    assert usage.models == ("gpt-5.5",)


def test_codex_tally_child_across_midnight(tmp_path):
    """Day dirs are local dates, so a child spawned after midnight lands in
    the next day's dir; the parent's last write (00:20) puts it in the window.

    Ablation: search only the parent's own day dir and this fails."""
    parent = _codex_thread(
        tmp_path, "2026/10/01", "23-50-00", ROOT, session_id=ROOT, usage=(10, 0, 1)
    )
    after_midnight = _local_ts("2026/10/02", hour=0, minute=20)
    os.utime(parent, (after_midnight, after_midnight))
    _codex_thread(
        tmp_path,
        "2026/10/02",
        "00-10-00",
        CHILD,
        session_id=ROOT,
        parent=ROOT,
        usage=(7, 0, 3),
    )

    assert tally_codex_rollout(parent) == TokenUsage(input_tokens=17, output_tokens=4)


def test_codex_tally_parent_without_session_meta_is_parent_only(tmp_path):
    """A parent that does not open with a session_meta has no thread id to
    match children against, so it tallies alone — even beside a sibling whose
    session_meta carries no tree fields at all.

    Ablation: search siblings with a None root instead of returning the
    parent's own total and the bare sibling (session_id None == root) is
    counted."""
    parent = tmp_path / "sessions/2026/10/01" / f"rollout-2026-10-01T10-00-00-{ROOT}.jsonl"
    _write_jsonl(
        parent,
        [
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 30, "output_tokens": 3}},
                },
            }
        ],
    )
    stamp = _local_ts("2026/10/01")
    os.utime(parent, (stamp, stamp))
    _codex_thread(tmp_path, "2026/10/01", "10-05-00", CHILD, usage=(500, 0, 50))

    assert tally_codex_rollout(parent) == TokenUsage(input_tokens=30, output_tokens=3)


def test_codex_tally_tracked_when_only_a_child_has_usage(tmp_path):
    """A parent that reported no token_count yet still reads as tracked when
    a child did: None stays reserved for no usage signal anywhere.

    Ablation: return None whenever the parent has no totals and this fails."""
    parent = _codex_thread(tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT)
    _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-05-00",
        CHILD,
        session_id=ROOT,
        parent=ROOT,
        usage=(9, 0, 1),
    )

    assert tally_codex_rollout(parent) == TokenUsage(input_tokens=9, output_tokens=1)


def test_codex_tally_parent_thread_chain_without_session_id(tmp_path):
    """Older rollouts carry no session_id; membership then follows the
    parent_thread_id chain into the parent's tree, to a fixpoint — the
    grandchild's file sorts before its parent's, so one pass would miss it.
    A rollout chained to an unknown thread stays out.

    Ablation: run the chain loop a single pass and the grandchild is
    missed."""
    parent = _codex_thread(tmp_path, "2026/10/01", "10-00-00", ROOT, usage=(10, 0, 1))
    _codex_thread(tmp_path, "2026/10/01", "10-30-00", CHILD, parent=ROOT, usage=(20, 0, 2))
    _codex_thread(tmp_path, "2026/10/01", "10-20-00", GRANDCHILD, parent=CHILD, usage=(30, 0, 3))
    _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-40-00",
        OTHER,
        parent="019a0000-0000-7000-8000-0000000000ff",
        usage=(900, 0, 9),
    )

    assert tally_codex_rollout(parent) == TokenUsage(input_tokens=60, output_tokens=6)


def test_codex_tally_skips_unreadable_sibling(tmp_path):
    """A child whose body cannot be decoded is skipped and named in `unread`;
    the readable child and the parent still count.

    Ablation: let the child's read error propagate and the tally raises; drop
    the child's `unread` append and the `unread` assertion fails."""
    parent = _codex_thread(
        tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT, usage=(10, 0, 1)
    )
    _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-05-00",
        CHILD,
        session_id=ROOT,
        parent=ROOT,
        usage=(5, 0, 5),
    )
    broken = _codex_thread(
        tmp_path, "2026/10/01", "10-06-00", GRANDCHILD, session_id=ROOT, parent=ROOT
    )
    with broken.open("ab") as f:
        f.write(b'{"type": "event_msg", "payload": "\xff\xfe"}\n')

    usage = tally_codex_rollout(parent)
    assert usage == TokenUsage(input_tokens=15, output_tokens=6)
    assert usage is not None and usage.unread == (f"{broken.name}: UnicodeDecodeError",)


def test_codex_tally_names_sibling_whose_first_line_fails(tmp_path, monkeypatch):
    """A sibling whose session_meta cannot be read may be a child, so it is
    named in `unread` too, by file name only: no directory reaches the journal.

    Ablation: drop the candidate's `unread` append and the assertion fails."""
    monkeypatch.setattr(tokens, "_codex_meta_cache", {})
    parent = _codex_thread(
        tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT, usage=(10, 0, 1)
    )
    locked = _codex_thread(
        tmp_path, "2026/10/01", "10-05-00", CHILD, session_id=ROOT, usage=(5, 0, 5)
    )
    real_read = tokens._read_codex_first_line

    def read(path):
        if path == locked:
            raise PermissionError(13, "denied", str(path))
        return real_read(path)

    monkeypatch.setattr(tokens, "_read_codex_first_line", read)

    usage = tally_codex_rollout(parent)
    assert usage == TokenUsage(input_tokens=10, output_tokens=1)
    assert usage is not None and usage.unread == (f"{locked.name}: PermissionError",)


def test_codex_tally_names_sibling_whose_first_line_does_not_parse(tmp_path, monkeypatch):
    """A complete first line that is not JSON (invalid UTF-8 here) may hide a
    child, so it is named in `unread` rather than read as "not a subagent".

    Ablation: let `_codex_meta` return None on a parse failure and the
    assertion fails."""
    monkeypatch.setattr(tokens, "_codex_meta_cache", {})
    parent = _codex_thread(
        tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT, usage=(10, 0, 1)
    )
    garbled = tmp_path / "sessions/2026/10/01" / f"rollout-2026-10-01T10-05-00-{CHILD}.jsonl"
    garbled.write_bytes(b'{"type": "session_meta", "payload": "\xff"}\n')

    usage = tally_codex_rollout(parent)
    assert usage == TokenUsage(input_tokens=10, output_tokens=1)
    assert usage is not None and usage.unread == (f"{garbled.name}: UnicodeDecodeError",)
    # failures are not cached: the next tally names it again
    usage = tally_codex_rollout(parent)
    assert usage is not None and usage.unread == (f"{garbled.name}: UnicodeDecodeError",)


def test_codex_tally_names_parent_whose_first_line_does_not_parse(tmp_path, monkeypatch):
    """An unparseable parent first line leaves its children uncounted, so the
    parent's own total is a lower bound and says so.

    Ablation: catch only OSError around the parent's `_codex_meta` and the
    tally raises."""
    monkeypatch.setattr(tokens, "_codex_meta_cache", {})
    parent = tmp_path / "sessions/2026/10/01" / f"rollout-2026-10-01T10-00-00-{ROOT}.jsonl"
    _write_jsonl(
        parent,
        [
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 30, "output_tokens": 3}},
                },
            }
        ],
    )
    body = parent.read_bytes()
    parent.write_bytes(b"{not json\n" + body)

    usage = tally_codex_rollout(parent)
    assert usage == TokenUsage(input_tokens=30, output_tokens=3)
    assert usage is not None and usage.unread == (f"{parent.name}: JSONDecodeError",)


def test_codex_tally_skips_sibling_first_line_still_being_written(tmp_path, monkeypatch):
    """A first line with no newline yet is a rollout mid-creation, not a
    fault: it reads as not-a-child for now, uncached, and names nothing.

    Ablation: raise for an incomplete line too and `unread` is non-empty."""
    monkeypatch.setattr(tokens, "_codex_meta_cache", {})
    parent = _codex_thread(
        tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT, usage=(10, 0, 1)
    )
    torn = tmp_path / "sessions/2026/10/01" / f"rollout-2026-10-01T10-05-00-{CHILD}.jsonl"
    torn.write_bytes(b'{"type": "session_meta", "payl')

    usage = tally_codex_rollout(parent)
    assert usage == TokenUsage(input_tokens=10, output_tokens=1)
    assert usage is not None and usage.unread == ()
    assert str(torn) not in tokens._codex_meta_cache


def test_codex_tally_names_unlistable_day_dir(tmp_path, monkeypatch):
    """`Path.glob` swallows a scan's OSError, so the dirs are listed directly:
    an unlistable day dir is named in `unread` by its date path, and the
    other days are still searched.

    Ablation: list the dirs with `glob("rollout-*.jsonl")` again and the
    `unread` assertion fails."""
    parent = _codex_thread(
        tmp_path, "2026/10/01", "23-50-00", ROOT, session_id=ROOT, usage=(10, 0, 1)
    )
    after_midnight = _local_ts("2026/10/02", hour=0, minute=20)
    os.utime(parent, (after_midnight, after_midnight))
    _codex_thread(
        tmp_path, "2026/10/01", "23-55-00", CHILD, session_id=ROOT, parent=ROOT, usage=(7, 0, 3)
    )
    _codex_thread(
        tmp_path,
        "2026/10/02",
        "00-10-00",
        GRANDCHILD,
        session_id=ROOT,
        parent=ROOT,
        usage=(500, 0, 50),
    )
    locked = tmp_path / "sessions/2026/10/02"
    real_iterdir = type(locked).iterdir

    def iterdir(self):
        if self == locked:
            raise PermissionError(13, "denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(type(locked), "iterdir", iterdir)

    usage = tally_codex_rollout(parent)
    assert usage == TokenUsage(input_tokens=17, output_tokens=4)
    assert usage is not None and usage.unread == ("2026/10/02/: PermissionError",)


def test_codex_clean_tally_has_no_unread(tmp_path):
    parent = _codex_thread(
        tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT, usage=(10, 0, 1)
    )
    _codex_thread(tmp_path, "2026/10/01", "10-05-00", CHILD, session_id=ROOT, usage=(5, 0, 5))

    usage = tally_codex_rollout(parent)
    assert usage is not None and usage.unread == ()


def test_codex_membership_cache_hit(tmp_path, monkeypatch):
    """The budget guard tallies every heartbeat: after the first pass each
    sibling's first line is cached (negatives too), so a repeat tally reads
    none, and a new sibling costs exactly one first-line read.

    Ablation: skip the cache store and the second tally re-reads every first
    line."""
    monkeypatch.setattr(tokens, "_codex_meta_cache", {})
    reads = []
    real_read = tokens._read_codex_first_line

    def counting_read(path):
        reads.append(path.name)
        return real_read(path)

    monkeypatch.setattr(tokens, "_read_codex_first_line", counting_read)
    parent = _codex_thread(
        tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT, usage=(10, 0, 1)
    )
    _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-05-00",
        CHILD,
        session_id=ROOT,
        parent=ROOT,
        usage=(5, 0, 1),
    )
    _codex_thread(tmp_path, "2026/10/01", "10-06-00", OTHER, session_id=OTHER, usage=(9, 0, 9))

    assert tally_codex_rollout(parent) == TokenUsage(input_tokens=15, output_tokens=2)
    assert len(reads) == 3
    reads.clear()
    assert tally_codex_rollout(parent) == TokenUsage(input_tokens=15, output_tokens=2)
    assert reads == []
    late = _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-09-00",
        GRANDCHILD,
        session_id=ROOT,
        parent=CHILD,
        usage=(1, 0, 1),
    )
    assert tally_codex_rollout(parent) == TokenUsage(input_tokens=16, output_tokens=3)
    assert reads == [late.name]


def test_codex_membership_cache_is_bounded(tmp_path, monkeypatch):
    """The cache evicts oldest-first at its cap, so a long-lived engine never
    grows it without bound.

    Ablation: drop the eviction loop and the cache outgrows the cap."""
    monkeypatch.setattr(tokens, "_codex_meta_cache", {})
    monkeypatch.setattr(tokens, "CODEX_META_CACHE_CAP", 2)
    parent = _codex_thread(tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT)
    for index, thread in enumerate((CHILD, GRANDCHILD, OTHER)):
        _codex_thread(
            tmp_path,
            "2026/10/01",
            f"10-0{index + 1}-00",
            thread,
            session_id=ROOT,
            parent=ROOT,
            usage=(1, 0, 1),
        )

    assert tally_codex_rollout(parent) == TokenUsage(input_tokens=3, output_tokens=3)
    assert len(tokens._codex_meta_cache) == 2


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


def test_gemini_chat_collects_models(tmp_path):
    """#859: the distinct `model` ids of `type == "gemini"` messages in file
    order; a re-emitted message repeats its model once, and a user message's
    `model` (or an empty one) is ignored.

    Ablation: drop the `type == "gemini"` check and the user message's model
    appears."""
    lines = [
        {"$set": {"messages": [{"id": "u1", "type": "user", "model": "user-side", "content": []}]}},
        {"id": "g1", "type": "gemini", "model": "gemini-3-pro", "tokens": {"input": 4}},
        {"id": "g1", "type": "gemini", "model": "gemini-3-pro", "tokens": {"input": 4}},
        {"id": "g2", "type": "gemini", "model": "", "tokens": {"input": 1}},
        {
            "$set": {
                "messages": [
                    {"id": "g3", "type": "gemini", "model": "gemini-3-flash", "tokens": {}}
                ]
            }
        },
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    usage = tally_gemini_chat(path)
    assert usage is not None
    assert usage.models == ("gemini-3-pro", "gemini-3-flash")


def test_gemini_chat_without_tokens_is_none(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"id": "u1", "type": "user", "content": []}) + "\n")
    assert tally_gemini_chat(path) is None
    assert tally_gemini_chat(tmp_path / "nope.jsonl") is None


def test_copilot_events_last_cumulative_across_models(tmp_path):
    # Synthetic two-model session (the real capture below ran one model): per
    # line {id, type, data:{...}}; data.modelMetrics.<model>.usage is cumulative.
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
    assert usage.input_tokens == 310  # (500 - 200 cache reads - 30 cache writes) + 40
    assert usage.output_tokens == 68  # 60 + 8: reasoningTokens are part of outputTokens
    assert usage.cache_read_tokens == 200
    assert usage.cache_creation_tokens == 30


def test_copilot_events_models_are_metric_keys(tmp_path):
    """#859: the models are the LAST modelMetrics' keys, in insertion order;
    an earlier snapshot's model that the last one drops is not listed.

    Ablation: read the keys of every snapshot and `gpt-5-old` appears."""
    lines = [
        {"data": {"modelMetrics": {"gpt-5-old": {"usage": {"inputTokens": 1}}}}},
        {
            "data": {
                "modelMetrics": {
                    "gpt-5-mini": {"usage": {"inputTokens": 5}},
                    "gpt-5": {"usage": {"inputTokens": 2}},
                }
            }
        },
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    usage = tally_copilot_events(path)
    assert usage is not None
    assert usage.models == ("gpt-5-mini", "gpt-5")


def test_copilot_events_models_empty_without_named_model(tmp_path):
    """An empty `modelMetrics` carries no usage (None); the only reachable
    tracked-but-unnamed case is a metrics dict whose sole key is empty: `()`.

    Ablation: build the TokenUsage without `models=` and it is `None`; drop
    the empty-key filter and it is `("",)`."""
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"data": {"modelMetrics": {}}}) + "\n", encoding="utf-8")
    assert tally_copilot_events(path) is None

    path.write_text(
        json.dumps({"data": {"modelMetrics": {"": {"usage": {"inputTokens": 3}}}}}) + "\n",
        encoding="utf-8",
    )
    usage = tally_copilot_events(path)
    assert usage is not None
    assert usage.input_tokens == 3
    assert usage.models == ()


# Sanitized from real Copilot CLI 1.0.105 captures (Linux, 2026-11-04); see the
# fixture README. Prompts, paths and ids are redacted; event order, key layout,
# model ids and token counts are verbatim.
COPILOT_STATE = Path(__file__).parent / "fixtures" / "copilot-1.0.105" / "session-state"
# One turn launched with `--model claude-haiku-4.5`, which the plan rejected
# (auto-routed to mai-code-1.1-flash); ended with /exit.
COPILOT_REJECTED_MODEL = COPILOT_STATE / "c0000000-0000-4000-8000-000000000001" / "events.jsonl"
# Two turns with no --model; ended with /exit.
COPILOT_TWO_TURNS = COPILOT_STATE / "c0000000-0000-4000-8000-000000000002" / "events.jsonl"
COPILOT_MODEL = "mai-code-1.1-flash"


def _copilot_events_until(tmp_path, source, keep):
    """A copy of `source` holding only the lines `keep(entry)` accepts."""
    lines = source.read_text(encoding="utf-8").splitlines()
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(line + "\n" for line in lines if keep(json.loads(line))), encoding="utf-8"
    )
    return path


def test_copilot_events_real_capture_totals():
    """A real 1.0.105 session: the shutdown snapshot's bare model-id key names
    the model, and input excludes the cache reads (`tokenDetails.input` is 570).

    Ablation: drop the cache-read subtraction and input reads 35898."""
    usage = tally_copilot_events(COPILOT_TWO_TURNS)
    assert usage == TokenUsage(input_tokens=570, output_tokens=11, cache_read_tokens=35328)
    assert usage.models == (COPILOT_MODEL,)
    assert usage.by_model == {COPILOT_MODEL: usage}


def test_copilot_events_ignore_the_rejected_model_flag():
    """`session.start` `selectedModel` echoes the `--model` flag even when
    Copilot rejects it, so the models come from the metrics, not from it."""
    start = json.loads(COPILOT_REJECTED_MODEL.read_text(encoding="utf-8").splitlines()[0])
    assert start["type"] == "session.start"
    assert start["data"]["selectedModel"] == "claude-haiku-4.5"

    usage = tally_copilot_events(COPILOT_REJECTED_MODEL)
    assert usage == TokenUsage(input_tokens=16520, output_tokens=5, cache_read_tokens=1408)
    assert usage.models == (COPILOT_MODEL,)


def test_copilot_events_read_the_last_checkpoint_without_a_shutdown(tmp_path):
    """A session with no `session.shutdown` line still reads its totals and
    model from the last per-turn `session.usage_checkpoint`, whose metrics sit
    under `accountingSnapshot`; the checkpoints are cumulative.

    Ablation: read `modelMetrics` only at the top of `data` and both are None."""
    no_shutdown = _copilot_events_until(
        tmp_path, COPILOT_TWO_TURNS, lambda entry: entry["type"] != "session.shutdown"
    )
    usage = tally_copilot_events(no_shutdown)
    assert usage == tally_copilot_events(COPILOT_TWO_TURNS)
    assert usage is not None and usage.models == (COPILOT_MODEL,)
    assert usage.by_model == {
        COPILOT_MODEL: TokenUsage(input_tokens=570, output_tokens=11, cache_read_tokens=35328)
    }

    seen: list[str] = []

    def first_turn(entry):
        seen.append(entry["type"])
        return seen.count("session.usage_checkpoint") <= 1 and entry["type"] != "session.shutdown"

    usage = tally_copilot_events(_copilot_events_until(tmp_path, COPILOT_TWO_TURNS, first_turn))
    assert usage == TokenUsage(input_tokens=518, output_tokens=5, cache_read_tokens=17408)
    assert usage.models == (COPILOT_MODEL,)


def test_copilot_events_settle_only_on_the_shutdown_line(tmp_path):
    """A checkpoint reads usage but is not final: only `session.shutdown`
    settles a copilot read, so a post-session poll waits for it. A
    shutdown with no prior snapshot settles a None read.

    Ablation: report every copilot read settled and the checkpoint-only read
    flips to True."""
    usage, settled = read_usage_settled("copilot-events", COPILOT_TWO_TURNS)
    assert settled is True
    assert usage == TokenUsage(input_tokens=570, output_tokens=11, cache_read_tokens=35328)

    no_shutdown = _copilot_events_until(
        tmp_path, COPILOT_TWO_TURNS, lambda entry: entry["type"] != "session.shutdown"
    )
    usage, settled = read_usage_settled("copilot-events", no_shutdown)
    assert settled is False
    assert usage is not None

    bare = tmp_path / "bare.jsonl"
    bare.write_text(json.dumps({"type": "session.shutdown", "data": {}}) + "\n", encoding="utf-8")
    assert read_usage_settled("copilot-events", bare) == (None, True)


def test_read_usage_settled_other_parsers_read_settled(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"usage": {"input_tokens": 1, "output_tokens": 2}}) + "\n")
    usage, settled = read_usage_settled("claude-jsonl", path)
    assert settled is True and usage is not None and usage.total == 3
    assert read_usage_settled("none", path) == (None, True)


def test_copilot_events_without_a_snapshot_is_none(tmp_path):
    """A real session cut before its first checkpoint carries no usage."""
    cut = _copilot_events_until(
        tmp_path,
        COPILOT_TWO_TURNS,
        lambda entry: entry["type"] not in ("session.usage_checkpoint", "session.shutdown"),
    )
    assert tally_copilot_events(cut) is None


def test_copilot_events_without_metrics_is_none(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"id": "e0", "type": "message", "data": {"content": "hi"}}) + "\n")
    assert tally_copilot_events(path) is None
    assert tally_copilot_events(tmp_path / "nope.jsonl") is None


# --------------------------------------------------- antigravity-transcript

# Sanitized from a real agy 1.4.9 parent + foreground-subagent capture: synthetic
# conversation ids and placeholder text, token counts and key layout verbatim.
AGY_BRAIN = Path(__file__).parent / "fixtures" / "antigravity-1.4.9" / "brain"
AGY_PARENT = "d0000000-0000-4000-8000-000000000001"
AGY_CHILD = "b0000000-0000-4000-8000-000000000002"
# The parent's own steps: input 6899+2482+2313, cache 0+4845+5278, output 201+117+38.
AGY_PARENT_USAGE = TokenUsage(input_tokens=11694, output_tokens=356, cache_read_tokens=10123)


def _agy_transcript(brain, conversation):
    return brain / conversation / ".system_generated" / "logs" / "transcript_full.jsonl"


def _agy_marker(brain, parent, child):
    marker = brain / parent / ".system_generated" / "subagents" / f"{child}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"conversationId": child}), encoding="utf-8")
    return marker


def _agy_step(index, input_tokens=0, cache_read=0, output_tokens=0):
    return {
        "step_index": index,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "output_tokens": output_tokens,
    }


@pytest.fixture
def agy_brain(tmp_path):
    """A mutable copy of the 1.4.9 fixture brain store."""
    return Path(shutil.copytree(AGY_BRAIN, tmp_path / "brain"))


def test_antigravity_transcript_folds_spawned_subagent():
    """Parent + child sums, read through the dispatcher as the adapter does at
    session end; `models` stays None (no model source), so `session-end`
    carries neither `models` nor `models_unresolved`."""
    usage = read_usage("antigravity-transcript", _agy_transcript(AGY_BRAIN, AGY_PARENT))
    assert usage == TokenUsage(input_tokens=25760, output_tokens=627, cache_read_tokens=26131)
    assert usage.cache_creation_tokens == 0
    assert usage.unread == ()
    assert usage.models is None
    assert usage.by_model is None  # DW-618: no model source, no split


def test_antigravity_transcript_parent_only(agy_brain):
    shutil.rmtree(agy_brain / AGY_PARENT / ".system_generated" / "subagents")
    usage = tally_antigravity_transcript(_agy_transcript(agy_brain, AGY_PARENT))
    assert usage == AGY_PARENT_USAGE
    assert usage.unread == ()
    assert usage.models is None


def test_antigravity_transcript_outside_agy_layout_tallies_alone(agy_brain):
    """The fold applies only to `<conv>/.system_generated/logs/<file>`: the same
    transcript one folder over, in the same conversation, reads alone."""
    elsewhere = agy_brain / AGY_PARENT / ".system_generated" / "other" / "transcript_full.jsonl"
    elsewhere.parent.mkdir()
    shutil.copy(_agy_transcript(agy_brain, AGY_PARENT), elsewhere)
    assert tally_antigravity_transcript(elsewhere) == AGY_PARENT_USAGE


def test_antigravity_transcript_pre_usage_shape_is_none(tmp_path):
    """agy 1.1.3 wrote no counts: untracked, never zero."""
    path = _agy_transcript(tmp_path, AGY_PARENT)
    _write_jsonl(
        path,
        [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "x"},
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "OK"},
        ],
    )
    assert tally_antigravity_transcript(path) is None
    assert read_usage("antigravity-transcript", path) is None


def test_antigravity_transcript_missing_file_is_none(tmp_path):
    assert tally_antigravity_transcript(_agy_transcript(tmp_path, AGY_PARENT)) is None


def test_antigravity_transcript_all_zero_step_is_tracked(tmp_path):
    path = _agy_transcript(tmp_path, AGY_PARENT)
    _write_jsonl(path, [_agy_step(1)])
    assert tally_antigravity_transcript(path) == TokenUsage()


def test_antigravity_transcript_skips_unreadable_child(agy_brain, monkeypatch):
    child = _agy_transcript(agy_brain, AGY_CHILD)
    real_open = Path.open

    def guarded_open(self, *args, **kwargs):
        if self == child:
            raise PermissionError("denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    usage = tally_antigravity_transcript(_agy_transcript(agy_brain, AGY_PARENT))
    assert usage == AGY_PARENT_USAGE
    assert usage.unread == (f"{AGY_CHILD}/transcript_full.jsonl: PermissionError",)


def test_antigravity_transcript_names_a_listed_child_without_a_transcript(agy_brain):
    """A marker names the child, so its missing transcript is a lower bound the
    engine journals (`usage_unread`), never a silent parent-only total."""
    _agy_transcript(agy_brain, AGY_CHILD).unlink()
    usage = tally_antigravity_transcript(_agy_transcript(agy_brain, AGY_PARENT))
    assert usage == AGY_PARENT_USAGE
    assert usage.unread == (f"{AGY_CHILD}/transcript_full.jsonl: FileNotFoundError",)


def test_antigravity_transcript_names_a_listed_child_transcript_not_a_file(agy_brain):
    child = _agy_transcript(agy_brain, AGY_CHILD)
    child.unlink()
    child.mkdir()
    usage = tally_antigravity_transcript(_agy_transcript(agy_brain, AGY_PARENT))
    assert usage == AGY_PARENT_USAGE
    assert usage.unread == (f"{AGY_CHILD}/transcript_full.jsonl: ValueError",)


def test_antigravity_transcript_names_unlistable_marker_folder(agy_brain, monkeypatch):
    subagents = agy_brain / AGY_PARENT / ".system_generated" / "subagents"
    real_iterdir = Path.iterdir

    def guarded_iterdir(self):
        if self == subagents:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    usage = tally_antigravity_transcript(_agy_transcript(agy_brain, AGY_PARENT))
    assert usage == AGY_PARENT_USAGE
    assert usage.unread == ("subagents/: PermissionError",)


def test_antigravity_transcript_child_only_usage_is_tracked(agy_brain):
    parent = _agy_transcript(agy_brain, AGY_PARENT)
    _write_jsonl(parent, [{"step_index": 0, "type": "USER_INPUT", "content": "x"}])
    usage = tally_antigravity_transcript(parent)
    assert usage == TokenUsage(input_tokens=14066, output_tokens=271, cache_read_tokens=16008)


@pytest.mark.parametrize(
    "name",
    [
        "notauuid.json",
        # a UUID, but not canonical: uppercase and unhyphenated spellings
        f"{AGY_CHILD.upper()}.json",
        f"{AGY_CHILD.replace('-', '')}.json",
        # canonical stem, wrong suffix
        f"{AGY_CHILD}.txt",
    ],
)
def test_antigravity_transcript_ignores_non_canonical_marker(agy_brain, name):
    """The child's conversation folder is renamed to the marker's stem, so only
    the marker gate keeps it out of the tally."""
    subagents = agy_brain / AGY_PARENT / ".system_generated" / "subagents"
    (subagents / f"{AGY_CHILD}.json").rename(subagents / name)
    stem = name.rpartition(".")[0]
    if stem != AGY_CHILD:
        (agy_brain / AGY_CHILD).rename(agy_brain / stem)
    usage = tally_antigravity_transcript(_agy_transcript(agy_brain, AGY_PARENT))
    assert usage == AGY_PARENT_USAGE


def test_antigravity_transcript_recurses_once_per_conversation(agy_brain):
    """A grandchild folds in; a marker cycle back to the parent or a child the
    tree already holds visits nothing twice."""
    grandchild = "c0000000-0000-4000-8000-000000000003"
    _write_jsonl(_agy_transcript(agy_brain, grandchild), [_agy_step(1, 1, 2, 3)])
    _agy_marker(agy_brain, AGY_CHILD, grandchild)
    _agy_marker(agy_brain, grandchild, AGY_PARENT)
    _agy_marker(agy_brain, grandchild, AGY_CHILD)
    usage = tally_antigravity_transcript(_agy_transcript(agy_brain, AGY_PARENT))
    assert usage == TokenUsage(input_tokens=25761, output_tokens=630, cache_read_tokens=26133)


def test_antigravity_transcript_duplicate_step_counts_once(tmp_path):
    path = _agy_transcript(tmp_path, AGY_PARENT)
    anon = _agy_step(None, 100, 0, 1)
    del anon["step_index"]
    _write_jsonl(
        path,
        [
            _agy_step(1, 10, 20, 30),
            _agy_step(1, 11, 21, 31),  # the same step re-emitted: last wins
            _agy_step(2, 1, 0, 0),
            anon,
            anon,  # no int step_index: each counts
            {**_agy_step(3, 1000), "step_index": "3"},
            {**_agy_step(3, 1000), "step_index": True},
        ],
    )
    usage = tally_antigravity_transcript(path)
    assert usage == TokenUsage(
        input_tokens=11 + 1 + 200 + 2000, output_tokens=33, cache_read_tokens=21
    )


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

    agy = tmp_path / "transcript_full.jsonl"
    agy.write_text(json.dumps({"step_index": 1, "input_tokens": 5}) + "\n", encoding="utf-8")
    assert read_usage("antigravity-transcript", agy).input_tokens == 5


def test_read_usage_tracked_parsers_report_models(tmp_path):
    """#859: each of the four tracked parsers, reached through the dispatcher,
    returns a non-None `models` naming the fixture's model."""
    claude = tmp_path / "claude.jsonl"
    _write_jsonl(claude, [_with_model(_assistant("msg_1", input_tokens=1), "claude-opus-5-5")])
    codex = _codex_thread(
        tmp_path, "2026/10/01", "10-00-00", ROOT, usage=(1, 0, 1), models=("gpt-5.5",)
    )
    gemini = tmp_path / "gemini.jsonl"
    gemini.write_text(
        json.dumps({"id": "g1", "type": "gemini", "model": "gemini-3-pro", "tokens": {}}) + "\n",
        encoding="utf-8",
    )

    for parser, path, model in [
        ("claude-jsonl", claude, "claude-opus-5-5"),
        ("codex-rollout", codex, "gpt-5.5"),
        ("gemini-chat", gemini, "gemini-3-pro"),
        ("copilot-events", COPILOT_TWO_TURNS, COPILOT_MODEL),
    ]:
        usage = read_usage(parser, path)
        assert usage is not None, parser
        assert usage.models == (model,), parser


# ------------------------------------------------- per-model split (DW-618)


def _assert_split(usage):
    """The DW-618 invariants: `by_model` sums field-wise to the totals, and
    every key is an observed model or the sentinel."""
    assert usage is not None and usage.by_model is not None
    summed = TokenUsage()
    for each in usage.by_model.values():
        summed.add(each)
    assert summed == usage
    assert set(usage.by_model) <= {*usage.models, UNATTRIBUTED_MODEL}


def test_tally_splits_tokens_per_model(tmp_path):
    """A deduped response bills to the first usable model its blocks name; an
    id-less entry to its own; usage with no model (or only `<synthetic>`)
    to the sentinel. Subagent usage lands on the subagent's model.

    Ablation: key the claude buckets by the LAST block's model and `msg_late`
    bills to sonnet; drop the anon model and the id-less sonnet entry is
    unattributed."""
    path = tmp_path / "sid.jsonl"
    opus = _with_model(_assistant("msg_1", input_tokens=10, output_tokens=20), "claude-opus-5-5")
    _write_jsonl(
        path,
        [
            opus,
            opus,  # a repeated content block: deduped
            _with_model(_assistant("msg_err", input_tokens=0), "<synthetic>"),
            _assistant("msg_3", input_tokens=7),  # no model at all
            {"type": "message", "usage": {"input_tokens": 1}},  # top-level shape
            _with_model(_assistant(None, input_tokens=4), "claude-sonnet-5-5"),
            _assistant("msg_late", output_tokens=6),
            _with_model(_assistant("msg_late", output_tokens=6), "claude-opus-5-5"),
            _with_model(_assistant("msg_late", output_tokens=6), "claude-sonnet-5-5"),
        ],
    )
    _write_jsonl(
        tmp_path / "sid" / "subagents" / "agent-a.jsonl",
        [_with_model(_assistant("msg_a", input_tokens=3, output_tokens=2), "claude-haiku-5-5")],
    )

    usage = tally(path)
    _assert_split(usage)
    assert usage.by_model == {
        "claude-opus-5-5": TokenUsage(input_tokens=10, output_tokens=26),
        "claude-sonnet-5-5": TokenUsage(input_tokens=4),
        "claude-haiku-5-5": TokenUsage(input_tokens=3, output_tokens=2),
        UNATTRIBUTED_MODEL: TokenUsage(input_tokens=8),
    }
    assert list(usage.by_model) == [*usage.models, UNATTRIBUTED_MODEL]


def test_tally_split_takes_the_model_from_a_usage_less_block(tmp_path):
    """A block that names the response's model but carries no usage still
    attributes the response: its usage block, without a model, bills there.

    Ablation: associate a message id's model only on usage-bearing blocks
    and the response is unattributed."""
    path = tmp_path / "sid.jsonl"
    named = {"type": "assistant", "message": {"id": "msg_1", "model": "claude-opus-5-5"}}
    _write_jsonl(path, [named, _assistant("msg_1", input_tokens=5)])

    usage = tally(path)
    _assert_split(usage)
    assert usage.by_model == {"claude-opus-5-5": TokenUsage(input_tokens=5)}


def test_tally_split_omits_a_zero_sentinel(tmp_path):
    """The sentinel bucket appears only when it holds usage: a zero-usage
    `<synthetic>` entry leaves the split model-only.

    Ablation: always add the stray bucket in `settle_by_model` and the
    sentinel key appears with zeros."""
    path = tmp_path / "sid.jsonl"
    _write_jsonl(
        path,
        [
            _with_model(_assistant("msg_1", input_tokens=5), "claude-opus-5-5"),
            _with_model(_assistant("msg_err", input_tokens=0), "<synthetic>"),
        ],
    )

    usage = tally(path)
    _assert_split(usage)
    assert usage.by_model == {"claude-opus-5-5": TokenUsage(input_tokens=5)}


def test_tally_split_is_sentinel_only_without_a_model(tmp_path):
    """Tracked with no model (`models == ()`): everything is unattributed —
    `by_model` is never None for a tracking parser."""
    path = tmp_path / "sid.jsonl"
    _write_jsonl(path, [_assistant("msg_1", input_tokens=5)])

    usage = tally(path)
    _assert_split(usage)
    assert usage.models == ()
    assert usage.by_model == {UNATTRIBUTED_MODEL: TokenUsage(input_tokens=5)}


def _codex_reading(input_tokens, cached, output):
    totals = {"input_tokens": input_tokens, "cached_input_tokens": cached, "output_tokens": output}
    return {
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": totals}},
    }


def _codex_turn(model):
    return {"type": "turn_context", "payload": {"model": model}}


def _append_codex(path, lines):
    """Append lines to a `_codex_thread` rollout, keeping its pinned mtime."""
    stamp = path.stat().st_mtime
    with path.open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(line) + "\n" for line in lines))
    os.utime(path, (stamp, stamp))


def test_codex_tally_splits_cumulative_readings_per_model(tmp_path):
    """Cumulative token_count readings bill their deltas to the latest
    `turn_context` model (a reading before any is unattributed); a switch
    back resumes the first model's bucket, and a child's own deltas merge
    into its model's. Each bucket splits cached input out as the totals do.

    Ablation: bill each reading in full (no delta) and the split overshoots
    the totals; bill every delta to the last model and A collapses into B."""
    parent = _codex_thread(tmp_path, "2026/10/01", "10-00-00", ROOT, session_id=ROOT)
    _append_codex(
        parent,
        [
            _codex_reading(10, 0, 1),  # before any turn_context
            _codex_turn("gpt-a"),
            _codex_reading(100, 40, 10),
            _codex_turn("gpt-b"),
            _codex_reading(160, 60, 15),
            _codex_turn("gpt-a"),
            _codex_reading(200, 70, 20),
        ],
    )
    child = _codex_thread(tmp_path, "2026/10/01", "10-05-00", CHILD, session_id=ROOT, parent=ROOT)
    _append_codex(child, [_codex_turn("gpt-b"), _codex_reading(50, 10, 5)])

    usage = tally_codex_rollout(parent)
    assert usage == TokenUsage(input_tokens=130 + 40, output_tokens=25, cache_read_tokens=80)
    _assert_split(usage)
    assert usage.by_model == {
        "gpt-a": TokenUsage(input_tokens=80, output_tokens=14, cache_read_tokens=50),
        "gpt-b": TokenUsage(input_tokens=80, output_tokens=10, cache_read_tokens=30),
        UNATTRIBUTED_MODEL: TokenUsage(input_tokens=10, output_tokens=1),
    }


def test_codex_tally_unreadable_child_adds_nothing_to_the_split(tmp_path):
    """A child that faults mid-read after a reading contributes no bucket, so
    the split still sums to the totals (which skip it too).

    Ablation: tally the child straight into the shared buckets and its
    reading leaks into the split, breaking the sum."""
    parent = _codex_thread(
        tmp_path,
        "2026/10/01",
        "10-00-00",
        ROOT,
        session_id=ROOT,
        usage=(10, 0, 1),
        models=("gpt-5.5",),
    )
    broken = _codex_thread(
        tmp_path, "2026/10/01", "10-06-00", GRANDCHILD, session_id=ROOT, parent=ROOT
    )
    filler = json.dumps({"type": "event_msg", "payload": {"type": "noise", "pad": "x" * 200}})
    stamp = broken.stat().st_mtime
    with broken.open("ab") as f:
        f.write((json.dumps(_codex_turn("gpt-5.5")) + "\n").encode("utf-8"))
        f.write((json.dumps(_codex_reading(500, 0, 50)) + "\n").encode("utf-8"))
        f.write((filler + "\n").encode("utf-8") * 200)
        f.write(b'{"type": "event_msg", "payload": "\xff\xfe"}\n')
    os.utime(broken, (stamp, stamp))

    usage = tally_codex_rollout(parent)
    assert usage is not None and usage.unread == (f"{broken.name}: UnicodeDecodeError",)
    _assert_split(usage)
    assert usage.by_model == {"gpt-5.5": TokenUsage(input_tokens=10, output_tokens=1)}


def test_gemini_chat_splits_tokens_per_model(tmp_path):
    """Each message id's winning (last) `tokens` bill to the last model any
    `type == "gemini"` occurrence of that id named; an id no gemini occurrence
    names a model for, or a non-gemini message carrying tokens, is
    unattributed.

    Ablation: bill by the first occurrence's model and `g1` stays on pro."""
    lines = [
        {"id": "g1", "type": "gemini", "model": "gemini-3-pro", "tokens": {"input": 1}},
        {
            "$set": {
                "messages": [
                    {
                        "id": "g1",
                        "type": "gemini",
                        "model": "gemini-3-flash",
                        "tokens": {"input": 10, "cached": 4, "output": 2, "thoughts": 1},
                    }
                ]
            }
        },
        {"id": "g2", "type": "gemini", "model": "gemini-3-pro", "tokens": {"input": 5}},
        {"id": "g3", "type": "gemini", "tokens": {"input": 3}},
        {"id": "u1", "type": "user", "model": "user-side", "tokens": {"output": 2}},
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    usage = tally_gemini_chat(path)
    _assert_split(usage)
    assert usage.by_model == {
        "gemini-3-pro": TokenUsage(input_tokens=5),
        "gemini-3-flash": TokenUsage(input_tokens=6, output_tokens=3, cache_read_tokens=4),
        UNATTRIBUTED_MODEL: TokenUsage(input_tokens=3, output_tokens=2),
    }


def test_gemini_chat_split_keeps_the_model_across_a_model_less_reemission(tmp_path):
    """A re-emission of a message id that omits `model` still bills its winning
    `tokens` to the model an earlier occurrence of that id named.

    Ablation: bill by the winning occurrence's own `model` and the tokens land
    on the unattributed sentinel."""
    lines = [
        {"id": "g1", "type": "gemini", "model": "gemini-3-pro", "tokens": {"input": 1}},
        {"$set": {"messages": [{"id": "g1", "type": "gemini", "tokens": {"input": 7}}]}},
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    usage = tally_gemini_chat(path)
    _assert_split(usage)
    assert usage.by_model == {"gemini-3-pro": TokenUsage(input_tokens=7)}


def test_copilot_events_splits_tokens_per_metric_key(tmp_path):
    """One bucket per named `modelMetrics` key of the last snapshot; an empty
    key's usage is unattributed, and a key without a usage dict has none.

    Ablation: sum every key into one bucket and the split names one model."""
    metrics = {
        "gpt-5": {
            "usage": {
                "inputTokens": 12,
                "outputTokens": 1,
                "reasoningTokens": 2,
                "cacheReadTokens": 7,
                "cacheWriteTokens": 3,
            }
        },
        "gpt-5-mini": {"usage": {"inputTokens": 4}},
        "": {"usage": {"inputTokens": 9}},
        "claude-x": {"usage": "garbled"},
    }
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"data": {"modelMetrics": metrics}}) + "\n", encoding="utf-8")

    usage = tally_copilot_events(path)
    _assert_split(usage)
    assert usage.by_model == {
        "gpt-5": TokenUsage(
            input_tokens=2, output_tokens=1, cache_read_tokens=7, cache_creation_tokens=3
        ),
        "gpt-5-mini": TokenUsage(input_tokens=4),
        UNATTRIBUTED_MODEL: TokenUsage(input_tokens=9),
    }


def test_read_usage_tracked_parsers_report_a_split(tmp_path):
    """Each tracked parser, through the dispatcher, returns a `by_model`
    naming its one model with the whole usage; antigravity returns None."""
    claude = tmp_path / "claude.jsonl"
    _write_jsonl(claude, [_with_model(_assistant("msg_1", input_tokens=1), "claude-opus-5-5")])
    codex = _codex_thread(
        tmp_path, "2026/10/01", "10-00-00", ROOT, usage=(1, 0, 1), models=("gpt-5.5",)
    )
    gemini = tmp_path / "gemini.jsonl"
    gemini.write_text(
        json.dumps({"id": "g1", "type": "gemini", "model": "gemini-3-pro", "tokens": {"input": 2}})
        + "\n",
        encoding="utf-8",
    )

    for parser, path, model in [
        ("claude-jsonl", claude, "claude-opus-5-5"),
        ("codex-rollout", codex, "gpt-5.5"),
        ("gemini-chat", gemini, "gemini-3-pro"),
        ("copilot-events", COPILOT_TWO_TURNS, COPILOT_MODEL),
    ]:
        usage = read_usage(parser, path)
        assert usage is not None, parser
        assert usage.by_model == {model: usage}, parser
    agy = read_usage("antigravity-transcript", _agy_transcript(AGY_BRAIN, AGY_PARENT))
    assert agy is not None and agy.by_model is None
