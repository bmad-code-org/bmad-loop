"""SCAN machinery: transcript discovery, schema inference, registration,
CLI plumbing, and end-to-end scrub-through. No live CLI required."""

import json

import pytest

from bmad_loop import cli, probe
from bmad_loop.adapters.profile import get_profile

# ----------------------------------------------------------- fixtures / helpers


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


CLAUDE_ROWS = [
    {"type": "assistant", "message": {"usage": {"input_tokens": 100, "output_tokens": 50}}},
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
]

CODEX_ROWS = [
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

GEMINI_ROWS = [
    {"$set": {"messages": [{"id": "u1", "type": "user", "content": []}]}},
    {"id": "g1", "type": "gemini", "tokens": {"input": 12273, "output": 45, "cached": 0}},
]


# ----------------------------------------------------------- token inference


def test_infer_claude_candidates_and_self_check(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    schema = probe.infer_token_schema("claude-jsonl", path)
    assert "message.usage.input_tokens:int" in schema.token_field_candidates
    assert "message.usage.output_tokens:int" in schema.token_field_candidates
    # parsed self-check matches the real parser
    assert schema.parsed_usage == {
        "input_tokens": 110,
        "output_tokens": 55,
        "cache_read_tokens": 2000,
        "cache_creation_tokens": 300,
    }


def test_infer_codex_nested_candidates(tmp_path):
    path = _write_jsonl(tmp_path / "r.jsonl", CODEX_ROWS)
    schema = probe.infer_token_schema("codex-rollout", path)
    assert "payload.info.total_token_usage.input_tokens:int" in schema.token_field_candidates
    assert schema.parsed_usage["output_tokens"] == 60


def test_infer_gemini_list_paths_collapse(tmp_path):
    path = _write_jsonl(tmp_path / "s.jsonl", GEMINI_ROWS)
    schema = probe.infer_token_schema("gemini-chat", path)
    # list indices collapse to [] so the per-message tokens are one path
    assert any(
        "$set.messages[].tokens.input:int" == p for p in schema.token_field_candidates
    ) or any("tokens.input:int" in p for p in schema.token_field_candidates)


def test_key_paths_carry_types_never_values(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    schema = probe.infer_token_schema("claude-jsonl", path)
    blob = "\n".join(schema.key_paths)
    # types appear, raw integer values never do
    assert ":int" in blob
    assert "100" not in blob and "2000" not in blob


def test_infer_with_parser_none_still_finds_candidates(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    schema = probe.infer_token_schema("none", path)
    assert schema.parsed_usage is None  # no parser to self-check
    assert "message.usage.input_tokens:int" in schema.token_field_candidates


# ----------------------------------------------------------- discovery


def test_discover_picks_newest_mtime(tmp_path):
    base = tmp_path / "sessions"
    old = _write_jsonl(base / "old.jsonl", CLAUDE_ROWS)
    new = _write_jsonl(base / "new.jsonl", CLAUDE_ROWS)
    import os

    os.utime(old, (1, 1))
    os.utime(new, (10_000_000, 10_000_000))
    hints = probe.Hints(session_dir=str(base))
    found = probe.discover_transcript("none", cli="custom", hints=hints)
    assert found.real_path == new
    assert found.multiple is True


def test_discover_transcript_override(tmp_path):
    path = _write_jsonl(tmp_path / "exact.jsonl", CLAUDE_ROWS)
    found = probe.discover_transcript(
        "claude-jsonl", cli="claude", hints=probe.Hints(transcript=str(path))
    )
    assert found.real_path == path
    assert found.location and "exact.jsonl" in found.location


def test_discover_missing_override_notes(tmp_path):
    found = probe.discover_transcript(
        "claude-jsonl", cli="claude", hints=probe.Hints(transcript=str(tmp_path / "nope.jsonl"))
    )
    assert found.real_path is None
    assert "does not exist" in found.note


def test_discover_location_redacts_username(tmp_path, monkeypatch):
    # a munged-cwd dir embedding a username must not survive verbatim
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_jsonl(tmp_path / ".secret-home-dir" / "abc-123.jsonl", CLAUDE_ROWS)
    found = probe.discover_transcript("none", cli="x", hints=probe.Hints(transcript=str(path)))
    assert found.location.startswith("~/")
    assert ".secret-home-dir" not in found.location
    assert "abc-123.jsonl" in found.location  # the id-like filename survives


# ----------------------------------------------------------- registration


@pytest.mark.parametrize("dialect_cli", ["claude", "codex", "gemini", "copilot"])
def test_probe_hook_registers_under_native_events(dialect_cli):
    from bmad_loop.install import merge_hooks

    profile = get_profile(dialect_cli)
    registrations = {
        native: f"python3 /tmp/bmad_loop_probe_hook.py {canonical}"
        for native, canonical in profile.hooks.events.items()
    }
    config, changed = merge_hooks({}, registrations, profile.hooks.dialect)
    assert changed
    for native in profile.hooks.events:
        assert native in config["hooks"]
    # idempotent re-run
    again, changed2 = merge_hooks(config, registrations, profile.hooks.dialect)
    assert not changed2


def test_scan_reports_registered_state(project):
    proj = project.project
    profile = get_profile("claude")
    finding = probe.scan(cli="claude", profile=profile, project=proj, hints=probe.Hints())
    assert finding.registered is False  # nothing installed in the sandbox
    # now install hooks and re-scan
    from bmad_loop.install import install_into

    install_into(proj, clis=("claude",))
    finding2 = probe.scan(cli="claude", profile=profile, project=proj, hints=probe.Hints())
    assert finding2.registered is True


# ----------------------------------------------------------- CLI plumbing


def test_cli_scan_produces_sections(tmp_path, capsys):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    rc = cli.main(
        ["probe-adapter", "claude", "--project", str(tmp_path), "--transcript", str(path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Profile finalize report — claude (scan)" in out
    assert "## Hook payload shape" in out
    assert "## Token usage schema" in out
    assert "message.usage.input_tokens:int" in out


def test_cli_unknown_cli_without_binary_fails(tmp_path, capsys):
    rc = cli.main(["probe-adapter", "no-such-cli", "--project", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FAIL" in err


def test_cli_unknown_cli_with_binary_reduced_report(tmp_path, capsys):
    rc = cli.main(["probe-adapter", "no-such-cli", "--project", str(tmp_path), "--binary", "true"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "reduced report" in out or "no (reduced report)" in out


def test_cli_out_writes_file(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    out_file = tmp_path / "report.md"
    rc = cli.main(
        [
            "probe-adapter",
            "claude",
            "--project",
            str(tmp_path),
            "--transcript",
            str(path),
            "--out",
            str(out_file),
        ]
    )
    assert rc == 0
    assert out_file.is_file()
    assert "Profile finalize report" in out_file.read_text()


def test_cli_json_block_appended(tmp_path, capsys):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    rc = cli.main(
        ["probe-adapter", "claude", "--project", str(tmp_path), "--transcript", str(path), "--json"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "## JSON" in out
    # the JSON block must parse
    blob = out.split("```json", 1)[1].rsplit("```", 1)[0]
    data = json.loads(blob)
    assert data["cli"] == "claude" and data["mode"] == "scan"


# ----------------------------------------------------------- scrub-through


def test_scan_report_contains_no_pii(tmp_path, capsys, monkeypatch):
    """A transcript carrying an email + a home path produces a report with neither."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rows = [
        {
            "type": "assistant",
            "author": "secret@example.com",
            "cwd": f"{tmp_path}/private/project",
            "message": {"usage": {"input_tokens": 7, "output_tokens": 3}},
        }
    ]
    path = _write_jsonl(tmp_path / "t.jsonl", rows)
    rc = cli.main(
        ["probe-adapter", "claude", "--project", str(tmp_path), "--transcript", str(path), "--json"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "secret@example.com" not in out
    assert "private/project" not in out
    # but the token schema is still there
    assert "message.usage.input_tokens:int" in out
