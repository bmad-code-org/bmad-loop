"""SCAN machinery: transcript discovery, schema inference, registration,
CLI plumbing, and end-to-end scrub-through. No live CLI required."""

import json
import re
import subprocess
import sys

import pytest
from conftest import machine_json, needs_strict_codec, write_script_launcher
from test_probe_hook import run_hook

from bmad_loop import cli, probe, sanitize
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


# Captured from a live agy 1.1.3 turn (probe-adapter antigravity --probe). Shape
# pinned deliberately: agy's transcript carries NO usage block, which is why the
# profile's usage_parser is "none" permanently rather than pending a parser.
ANTIGRAVITY_ROWS = [
    {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-07-17T02:05:24Z",
        "content": "<USER_REQUEST>\nReply with exactly: OK\n</USER_REQUEST>",
    },
    {
        "step_index": 2,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-07-17T02:05:24Z",
        "content": "OK",
        "thinking": "**Processing User Request**",
    },
]


def test_infer_antigravity_transcript_has_no_token_fields(tmp_path):
    """agy exposes no usage in its transcript — the evidence for usage_parser="none".

    If this ever starts finding candidates, agy began emitting usage and the
    profile should be revisited (see antigravity.toml).
    """
    path = _write_jsonl(tmp_path / "transcript_full.jsonl", ANTIGRAVITY_ROWS)
    schema = probe.infer_token_schema("none", path)
    assert schema.entries_scanned == 2
    assert schema.token_field_candidates == []
    assert "step_index:int" in schema.key_paths


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
    """A munged-cwd dir embedding a username must not survive verbatim.

    Ablation target: drop the `not sanitize.looks_like_identifier(c)` leg from
    `_redact_location`'s `comp()` and this fails alone, on the
    `".secret-home-dir" not in found.location` assertion. That leg is the only
    one that redacts this component — the name is not identifier-shaped, and the
    `embeds_current_username` leg beside it does not match it — so the negative
    assertion is answering about the gate it names and not about some unrelated
    reason the string could be absent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # `_redact_location` resolves the home it collapses to `~` via
    # os.path.expanduser, which reads HOME on POSIX but USERPROFILE (then
    # HOMEDRIVE+HOMEPATH) on Windows — never HOME. Setting only HOME left the
    # win32 leg asserting the `~` branch while merely *happening* to be in it,
    # because pytest's tmp_path sat under the real profile dir; it broke the
    # moment TMP moved to another volume. Set both, as test_sanitize.py does.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = _write_jsonl(tmp_path / ".secret-home-dir" / "abc-123.jsonl", CLAUDE_ROWS)
    found = probe.discover_transcript("none", cli="x", hints=probe.Hints(transcript=str(path)))
    assert found.location.startswith("~/")
    assert ".secret-home-dir" not in found.location
    assert "abc-123.jsonl" in found.location  # the id-like filename survives


# ----------------------------------------------------------- registration


@pytest.mark.parametrize("dialect_cli", ["claude", "codex", "gemini", "copilot", "antigravity"])
def test_probe_hook_registers_under_native_events(dialect_cli):
    from bmad_loop.install import ANTIGRAVITY_HOOK_GROUP, merge_hooks

    profile = get_profile(dialect_cli)
    registrations = {
        native: f"python3 /tmp/bmad_loop_probe_hook.py {canonical}"
        for native, canonical in profile.hooks.events.items()
    }
    config, changed = merge_hooks({}, registrations, profile.hooks.dialect)
    assert changed
    # agy keys by hook-group name at the top level; the others wrap in "hooks".
    container = (
        config[ANTIGRAVITY_HOOK_GROUP]
        if profile.hooks.dialect == "antigravity-hooks-json"
        else config["hooks"]
    )
    for native in profile.hooks.events:
        assert native in container
    # idempotent re-run
    again, changed2 = merge_hooks(config, registrations, profile.hooks.dialect)
    assert not changed2


@pytest.mark.parametrize("cli", ["claude", "antigravity"])
def test_scan_reports_registered_state(project, cli):
    """Registration detection must follow each dialect's container shape.

    agy keys .agents/hooks.json by hook-GROUP name with no "hooks" wrapper, so
    reading "hooks" there reports a correctly-installed relay as unregistered.
    """
    proj = project.project
    profile = get_profile(cli)
    finding = probe.scan(cli=cli, profile=profile, project=proj, hints=probe.Hints())
    assert finding.registered is False  # nothing installed in the sandbox
    # now install hooks and re-scan
    from bmad_loop.install import install_into

    install_into(proj, clis=(cli,))
    finding2 = probe.scan(cli=cli, profile=profile, project=proj, hints=probe.Hints())
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


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_cli_hookless_profile_politely_refused(tmp_path, capsys, json_mode):
    """opencode-http (resolved via its `opencode` alias) is HTTP/SSE-driven —
    there is no transcript or hook surface to finalize, so probe-adapter
    explains itself instead of producing a meaningless report. The explanation
    is stderr-only, so JSON mode leaves stdout empty rather than half a
    document."""
    argv = ["probe-adapter", "opencode", "--project", str(tmp_path)]
    rc = cli.main([*argv, "--json"] if json_mode else argv)
    assert rc == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "hookless" in err
    assert "opencode_http" in err
    assert "FAIL" not in err  # a polite refusal, not an error dump


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


def _assert_keys_sorted(raw):
    """Every object in the document has its keys in sorted order (`sort_keys=True`).

    Only `object_pairs_hook` can see key ORDER — `json.loads` into a dict keeps
    insertion order, so a plain round-trip comparison can never detect the flag
    being dropped. Sorted output is what makes two probes of the same CLI diffable.

    Takes the RAW string for that reason. Handing it anything re-serialized here
    (`json.dumps(doc, sort_keys=True)`) would sort the keys on the way in and
    assert nothing at all.
    """

    def hook(pairs):
        keys = [k for k, _ in pairs]
        assert keys == sorted(keys), f"object keys not sorted: {keys}"
        return dict(pairs)

    json.loads(raw, object_pairs_hook=hook)


def test_render_json_sorts_keys_at_every_depth(project):
    """Asserted against `render_json`'s own return value, not the CLI's stdout:
    key order lives in the raw bytes, and the shared `--json` CLI helper hands
    back a parsed dict, which has already lost it. This is the renderer's
    property anyway — the CLI just prints what it returns."""
    profile = get_profile("claude")
    finding = probe.scan(
        cli="claude", profile=profile, project=project.project, hints=probe.Hints()
    )
    _assert_keys_sorted(probe.render_json(finding))


def test_cli_json_emits_pure_document(tmp_path, capsys):
    """--json is the whole of stdout: parsing the full stream (not a fence out of
    prose) is itself the assertion that nothing else was printed.

    `err_contains="ok:"` rather than the helper's default empty stderr: probe's
    human trailer is not gone, it moved off stdout, and pinning it here is what
    catches it moving back. The fenced form leaving stdout is covered by the
    helper parsing the whole stream — a fence in prose would not parse.
    """
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    data = machine_json(
        [
            "probe-adapter",
            "claude",
            "--project",
            str(tmp_path),
            "--transcript",
            str(path),
            "--json",
        ],
        capsys,
        err_contains="ok:",
    )
    assert data["cli"] == "claude" and data["mode"] == "scan"
    # schema_version is a NEW sibling of `version`, which holds the probed CLI's
    # own --version output — the two must not be conflated.
    # `!=` between them would be vacuously true (str vs int never compare equal),
    # so pin the TYPE contract instead — that is what breaks if the two are ever
    # conflated: schema_version is our int, version is the CLI's own string.
    assert data["schema_version"] == probe.SCHEMA_VERSION == 2
    assert isinstance(data["schema_version"], int)
    assert "version" in data and (data["version"] is None or isinstance(data["version"], str))


def test_cli_json_out_writes_document_and_keeps_stdout_empty(tmp_path, capsys):
    path = _write_jsonl(tmp_path / "t.jsonl", CLAUDE_ROWS)
    out_file = tmp_path / "report.json"
    rc = cli.main(
        [
            "probe-adapter",
            "claude",
            "--project",
            str(tmp_path),
            "--transcript",
            str(path),
            "--json",
            "--out",
            str(out_file),
        ]
    )
    assert rc == 0
    out, err = capsys.readouterr()
    assert out == ""  # the document went to the file; stdout stays empty
    assert "document written to" in err  # the noun tracks what was produced
    assert json.loads(out_file.read_text())["cli"] == "claude"


def test_cli_json_unknown_profile_notice_goes_to_stderr(tmp_path, capsys):
    """A reduced report still emits a clean document: the notice is stderr-only."""
    rc = cli.main(
        [
            "probe-adapter",
            "no-such-cli",
            "--project",
            str(tmp_path),
            "--binary",
            "definitely-not-a-real-binary",
            "--json",
        ]
    )
    assert rc == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["cli"] == "no-such-cli"
    assert "unknown profile" in err


# ----------------------------------------------------------- scrub-through


def _seed_pii_scan(tmp_path, monkeypatch):
    """A transcript whose rows carry an email, a home-rooted path, and a
    credential-shaped dynamic dict key (identifier-shaped, so only the
    explicit secret gate in _walk_paths keeps it out of the key paths)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rows = [
        {
            "type": "assistant",
            "author": "secret@example.com",
            "cwd": f"{tmp_path}/private/project",
            "message": {"usage": {"input_tokens": 7, "output_tokens": 3}},
            "cache": {"ghp_0123456789abcdefghijklmnopqrstuvwxyz": 1},
        }
    ]
    return _write_jsonl(tmp_path / "t.jsonl", rows)


def _pii_argv(tmp_path, path, *extra):
    return [
        "probe-adapter",
        "claude",
        "--project",
        str(tmp_path),
        "--transcript",
        str(path),
        *extra,
    ]


def test_scan_report_contains_no_pii(tmp_path, capsys, monkeypatch):
    """A transcript carrying an email + a home path produces a report with neither.

    Text mode (no --json): the markdown report is what carries the schema table.
    """
    path = _seed_pii_scan(tmp_path, monkeypatch)
    assert cli.main(_pii_argv(tmp_path, path)) == 0
    out = capsys.readouterr().out
    assert "secret@example.com" not in out
    assert "private/project" not in out
    # the credential-shaped dynamic key collapsed instead of shipping (and
    # instead of tripping the guard's secret rule into a refusal — rc was 0)
    assert "ghp_" not in out
    assert "cache.<key>:int" in out
    # but the token schema is still there
    assert "message.usage.input_tokens:int" in out


def test_scan_json_document_contains_no_pii(tmp_path, capsys, monkeypatch):
    """The same scrub-through holds for the JSON document, which is rendered by a
    separate path (render_json, not render_markdown) — assert it on its own."""
    path = _seed_pii_scan(tmp_path, monkeypatch)
    assert cli.main(_pii_argv(tmp_path, path, "--json")) == 0
    out = capsys.readouterr().out
    assert "secret@example.com" not in out
    assert "private/project" not in out
    assert "ghp_" not in out
    doc = json.loads(out)
    assert "message.usage.input_tokens:int" in doc["tokens"]["key_paths"]
    assert "cache.<key>:int" in doc["tokens"]["key_paths"]


# ----------------------------------------------------------- egress guard (#199)
# Probe's canary kit mirrors tests/test_diagnostics.py: PROJ is identifier-shaped
# (it passes every scrub gate on shape), so only the pseudonymizer/guard pair
# keeps it out of a shipped report.

PROJ = "AcmeQuantumBilling"
EMAIL = "victim.canary@example.com"
SECRET_GH = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
HOME_PATH = "/home/canaryuser/secret/proj"
ALIAS_RE = re.compile(r"project-[0-9a-f]{12}")  # blake2s digest_size=6 → 12 hex chars


def _dirty_finding(**kw):
    defaults = dict(cli="claude", mode="scan", known_profile=True, binary="claude", parser="none")
    defaults.update(kw)
    return probe.ProfileFinding(**defaults)


def test_probe_leakdetected_is_the_shared_sanitize_exception():
    """Same pin as diagnostics': ruff's F401 autofix deletes a bare re-export."""
    assert probe.LeakDetected is sanitize.LeakDetected


def test_renderers_fail_closed_on_dirty_finding():
    """A field that skipped collection scrubbing cannot ship: both renderers
    re-scan their own rendered bytes and raise instead of returning."""
    f = _dirty_finding(warnings=[f"contact {EMAIL}"])
    with pytest.raises(probe.LeakDetected) as exc:
        probe.render_markdown(f)
    assert "email" in exc.value.rules
    with pytest.raises(probe.LeakDetected) as exc:
        probe.render_json(f)
    assert "email" in exc.value.rules


def test_cli_refusal_text_mode_emits_nothing(tmp_path, capsys, monkeypatch):
    """Fail-closed shape: message → stderr, stdout EMPTY, exit != 0."""
    monkeypatch.setattr(probe, "scan", lambda **kw: _dirty_finding(warnings=[f"see {EMAIL}"]))
    rc = cli.main(["probe-adapter", "claude", "--project", str(tmp_path)])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert "FAIL: refusing to emit" in err and "email" in err


def test_cli_refusal_json_out_writes_no_partial_file(tmp_path, capsys, monkeypatch):
    """#195 pure-document contract on refusal: empty stdout AND no --out file."""
    monkeypatch.setattr(probe, "scan", lambda **kw: _dirty_finding(warnings=[f"see {EMAIL}"]))
    out_file = tmp_path / "report.json"
    rc = cli.main(
        ["probe-adapter", "claude", "--project", str(tmp_path), "--json", "--out", str(out_file)]
    )
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert "FAIL: refusing to emit" in err
    assert not out_file.exists()


def test_collect_captures_emits_schema_not_values(tmp_path):
    """The capture reduction ships SHAPE only — no payload value of any kind."""
    capture = tmp_path / "capture"
    capture.mkdir()
    payload = {
        "argv_event": "Stop",
        "session_id": "abc-123",
        "transcript_path": f"{HOME_PATH}/t.jsonl",
        "prompt": "the launch codes are 0000",
        "api_key": SECRET_GH,
        "tool_input": {"command": "cat /home/canaryuser/.ssh/id_rsa"},
        "weird key with spaces": 1,
        "counts": [1, 2, 3],
    }
    (capture / "001-Stop.payload.json").write_text(json.dumps(payload), encoding="utf-8")
    caps = probe._collect_captures(capture, {"Stop": "stop"})
    assert len(caps) == 1
    ev = caps[0]
    assert not hasattr(ev, "payload")  # the value-bearing field is GONE, not empty
    assert ev.native_event == "Stop" and ev.canonical_event == "stop"
    assert "session_id" in ev.payload_keys and "<key>" in ev.payload_keys
    assert "session_id:str" in ev.payload_schema
    assert "tool_input.command:str" in ev.payload_schema
    assert "api_key:str" in ev.payload_schema
    assert "counts[]:int" in ev.payload_schema
    joined = "\n".join(ev.payload_schema + ev.payload_keys)
    for canary in ("canaryuser", "launch codes", "ghp_", "abc-123", "id_rsa"):
        assert canary not in joined


def test_probe_capture_egress_via_hook_seam(tmp_path):
    """End-to-end over the real capture seam: the hook subprocess retains the raw
    payload ("the command scrubs later" — test_probe_hook), so THIS is the test
    that the command actually does, all the way to the rendered egress bytes."""
    capture = tmp_path / "capture"
    env = {"BMAD_LOOP_PROBE_CAPTURE_DIR": str(capture), "BMAD_LOOP_TASK_ID": "probe"}
    payload = {
        "session_id": "sess-canary-1",
        "transcript_path": f"{HOME_PATH}/t.jsonl",
        "prompt": f"proprietary {PROJ} launch plan, contact {EMAIL}",
        "token": SECRET_GH,
    }
    proc = run_hook("Stop", env, payload)
    assert proc.returncode == 0

    caps = probe._collect_captures(capture, {"Stop": "stop"})
    f = _dirty_finding(mode="probe", parser="claude-jsonl", captured_events=caps)
    md = probe.render_markdown(f)
    js = probe.render_json(f)
    combined = md + "\n" + js
    for canary in (PROJ, EMAIL, SECRET_GH, "canaryuser", "sess-canary-1", "launch plan"):
        assert canary not in combined
    # the shape that makes the report useful survived
    assert "prompt:str" in combined and "token:str" in combined
    assert json.loads(js)["captured_events"][0]["payload_schema"]


def test_location_aliases_project_dir_zero_repairs(tmp_path, capsys):
    """The one identifier-shaped value probe KNOWS is the user's — the project
    dir name — is aliased at collection time, so the canonical run needs ZERO
    egress repairs (probe's analogue of diagnostics' zero-repair invariant)."""
    project = tmp_path / PROJ
    transcript = _write_jsonl(project / "transcripts" / "t.jsonl", CLAUDE_ROWS)
    rc = cli.main(
        ["probe-adapter", "claude", "--project", str(project), "--transcript", str(transcript)]
    )
    out, err = capsys.readouterr()
    assert rc == 0
    assert PROJ not in out and PROJ not in err
    assert ALIAS_RE.search(out)  # the location line carries the alias instead
    assert "leak backstop" not in err  # collection routed it — no repair needed
    assert "Backstop repairs" not in out


def test_backstop_repairs_stray_project_name_in_key_paths(tmp_path, capsys):
    """A surface collection can't route — the project name as an identifier-shaped
    dynamic dict key survives _walk_paths into key_paths — is caught at egress,
    repaired to the alias, and disclosed in the document + on stderr."""
    project = tmp_path / PROJ
    project.mkdir()
    rows = [{"message": {"usage": {"input_tokens": 7, PROJ: 7}}}]
    transcript = _write_jsonl(tmp_path / "t.jsonl", rows)
    data = machine_json(
        [
            "probe-adapter",
            "claude",
            "--project",
            str(project),
            "--transcript",
            str(transcript),
            "--json",
        ],
        capsys,
        err_contains="leak backstop pseudonymized",
    )
    assert PROJ not in json.dumps(data)
    (label,) = data["backstop_repairs"]
    assert re.fullmatch(r"project:project-[0-9a-f]{12}", label)
    alias = label.split(":", 1)[1]
    assert f"message.usage.{alias}:int" in data["tokens"]["key_paths"]


def test_render_json_non_ascii_roundtrip_and_guard():
    """ensure_ascii=False is a safety requirement: the guard must see the string
    as itself. Clean non-ASCII round-trips unescaped; a registered non-ASCII
    sensitive value is caught and repaired in the exact emitted bytes."""
    f = _dirty_finding(warnings=["café — naïve ✓"])
    js = probe.render_json(f)
    assert json.loads(js)["warnings"] == ["café — naïve ✓"]
    assert "caf\\u00e9" not in js  # emitted unescaped, not as a \uXXXX escape

    pseudo = sanitize.Pseudonymizer()
    alias = pseudo.alias("café-user", ns="project")
    f2 = _dirty_finding(warnings=["ref café-user end"])
    js2 = probe.render_json(f2, pseudo=pseudo)
    assert "café-user" not in js2
    assert json.loads(js2)["backstop_repairs"] == {f"project:{alias}": 1}


def test_scan_redacts_home_rooted_binary_hint(tmp_path, monkeypatch):
    """--binary under $HOME must not ship an absolute home path (it previously
    rendered verbatim in the summary AND the not-found warning)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    hints = probe.Hints(binary=f"{tmp_path}/.local/bin/claude-x")
    finding = probe.scan(cli="no-such-cli", profile=None, project=tmp_path, hints=hints)
    assert finding.binary == "~/.local/bin/claude-x"
    md = probe.render_markdown(finding)  # and the guard agrees: no refusal
    assert str(tmp_path) not in md


def test_redact_location_strikes_username_components(monkeypatch):
    """An identifier-shaped component that embeds the current username (pytest's
    own tmp roots are the live example: pytest-of-<user>) is redacted, so the
    guard's username hard rule can't refuse the whole report over a path."""
    monkeypatch.setattr(sanitize.getpass, "getuser", lambda: "canaryuser")
    from pathlib import Path

    loc = probe._redact_location(Path("/var/tmp/build-of-canaryuser/session-1/t.jsonl"))
    assert "canaryuser" not in loc
    assert loc == "/var/tmp/<redacted>/session-1/t.jsonl"


def test_redact_location_drops_the_root_anchor_on_every_flavour(monkeypatch):
    """The root separator is structure, not a component. Windows spells it
    ``\\`` rather than ``/``, and letting it reach the identifier gate prepended
    a phantom ``<redacted>`` to every non-home absolute path (a Windows-only CI
    failure). Driven through both flavours so either platform catches it."""
    monkeypatch.setattr(sanitize.getpass, "getuser", lambda: "canaryuser")
    from pathlib import PureWindowsPath

    # A rooted path carries no drive: the anchor vanishes on both flavours.
    assert probe._redact_location(PureWindowsPath("/var/tmp/s-1/t.jsonl")) == (
        "/var/tmp/s-1/t.jsonl"
    )
    # A drive anchor is content, not structure, so it is judged and struck.
    assert probe._redact_location(PureWindowsPath("C:/build/s-1/t.jsonl")) == (
        "/<redacted>/build/s-1/t.jsonl"
    )


# ----------------------------------------------------------- version / help


@needs_strict_codec
def test_run_capture_replaces_undecodable_banner_bytes(tmp_path):
    """A --version/--help banner carrying a byte the locale codec cannot decode
    is decoded with replacement, not strictly, so `run_version_help`'s documented
    "Never raises" stays true: a strict decode raises UnicodeDecodeError, a
    ValueError, which `_run_capture`'s `(OSError, subprocess.SubprocessError)`
    guard does not name.

    U+FFFD is asserted rather than inferred from "did not raise" — under
    `needs_strict_codec` the codec provably cannot decode the byte, so
    `errors="replace"` must have put it there. Its *count* stays unasserted, that
    being what varies by codec, and the ASCII on both sides pins that only the
    offending byte was replaced.

    Driven through a real child on purpose: a monkeypatched `subprocess.run`
    never runs the stdlib decode, so such a test passes with the bug restored.

    Ablation: remove `errors="replace"` from `_run_capture` and this fails with a
    raised UnicodeDecodeError — not with a None return, which the guard above
    would have to catch to produce."""
    # Interpreter is `sys.executable`, never a bare `python`: the tests run under
    # uv, where no `python` need be on PATH.
    script = tmp_path / "emit383.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'before \\xff after\\n')\n"
        "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )

    out = probe._run_capture([sys.executable, str(script)], 10)

    # Load-bearing, not decoration: None is this function's failure sentinel, so
    # this is the assertion that separates "decoded with replacement" from
    # "swallowed the fault and reported no banner at all". Do not weaken it to a
    # bare "did not raise" — that passes on the sentinel too.
    assert out is not None
    assert "�" in out  # the replacement, not a survivor
    assert "before" in out and "after" in out


# ------------------------------------------------------ liveness (#294)


@pytest.mark.parametrize("exit_code", [0, 2, 127], ids=["runs", "rc-2", "rc-127"])
def test_binary_runs_returns_the_exit_code_of_a_real_child(tmp_path, exit_code):
    """`binary_runs` reports the child's code, not a boolean — 0 is the only "this
    install works" answer, and any other value is what the #294 finding carries as
    its `returncode` detail.

    Both nonzero rows are the issue's OWN evidence, not invented codes: its
    transcript reports rc 2 and a reproduction of the same dead shim exits 127.
    They are here to pin `binary_runs` as a faithful pass-through, which is what
    lets cli.py gate on `rc != 0` rather than on an allowlist of shell-ish codes —
    the code depends on the shell and the failure mode, so {126, 127} would miss
    the very case #294 is about.

    Driven through a real child rather than a patched `subprocess.run`: the point
    of this function is that a process actually launched and exited, and a mock
    proves nothing about that.
    """
    launcher = write_script_launcher(tmp_path, "shim", f"import sys\nsys.exit({exit_code})\n")

    assert probe.binary_runs(str(launcher), timeout_s=30) == exit_code


def test_binary_runs_returns_none_when_the_process_cannot_be_launched(tmp_path):
    """A path that is not there at all faults in `subprocess.run` (FileNotFoundError,
    an OSError) and comes back as None — "there is no return code to report", which
    is a different finding from "it ran and said 5", not the same sentinel.

    This is the guard machine.py depends on: every gate in `cmd_validate` runs inside
    a try precisely so "the command has no error path of its own — its rc is purely
    the verdict", and a probe that raised would hand it one.

    Ablation target: delete the `except (OSError, subprocess.SubprocessError)` clause
    in `probe.binary_runs` and this reddens with a raised FileNotFoundError rather
    than a None return — the two outcomes a bare "did not raise" could not tell
    apart, which is why the assertion is on the value.
    """
    missing = tmp_path / "nope" / "not-a-binary"

    assert probe.binary_runs(str(missing), timeout_s=30) is None


def test_binary_runs_returns_none_when_the_child_outlives_the_timeout(tmp_path):
    """A child that never exits is killed at `timeout_s` and reported as None.

    TimeoutExpired is a `subprocess.SubprocessError`, not an OSError, so this row is
    what proves the guard names BOTH families — a hang is the failure mode of a shim
    that prompts, which is exactly the shape `stdin=DEVNULL` exists to avoid.

    Ablation target: narrow the guard to `except OSError` and this reddens with a
    raised subprocess.TimeoutExpired.
    """
    launcher = write_script_launcher(tmp_path, "hang", "import time\ntime.sleep(120)\n")

    assert probe.binary_runs(str(launcher), timeout_s=0.5) is None


def test_binary_runs_pins_devnull_stdin_and_the_caller_timeout():
    """The one contract row over the call itself: argv is `<binary> --version`, stdin
    is DEVNULL, the caller's timeout is honored, and a nonzero exit is data rather
    than an exception (`check=False`).

    `stdin=DEVNULL` is required, not cosmetic: with the caller's tty inherited a shim
    that prompts blocks on the read for the entire timeout (measured 4.00s against
    0.00s), inside an interactive command. Nothing else observes it — the real-child
    rows above pass either way — so it is pinned here or not at all.

    Asserted as a SUBSET of the recorded kwargs, never dict equality: a future kwarg
    (`env`, `cwd`, ...) is additive, and equality would turn every such addition into
    a multi-row breakage in this file.

    Ablation target: drop `stdin=subprocess.DEVNULL` from `probe.binary_runs` and the
    stdin assertion reddens with a KeyError. `check=False` is the stdlib default, so
    dropping it reddens only this row (KeyError) and nothing else — it is pinned as
    intent against a future flip to `check=True`, which is the mutation that bites:
    that turns every nonzero exit into a CalledProcessError, a SubprocessError the
    guard swallows into None, reddening both real-child rows above as well.
    """
    recorded: dict = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(probe.subprocess, "run", fake_run)
        assert probe.binary_runs("some-cli", timeout_s=3.5) == 0

    assert recorded["argv"] == ["some-cli", "--version"]
    kwargs = recorded["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 3.5
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    # No `text=True`: nothing reads the output, so the locale decode that forced
    # `errors="replace"` onto `_run_capture` (#383) never happens here and cannot
    # raise the UnicodeDecodeError the guard above does not name.
    assert kwargs.get("text") is None
