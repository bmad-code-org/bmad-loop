"""The crown-jewel PII case table for the probe sanitizer."""

import re

import pytest

from bmad_loop import sanitize


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # os.path.expanduser reads HOME on POSIX but USERPROFILE on Windows; set both so
    # the fake home actually takes effect on either host (else expanduser returns the
    # real profile, which is a *prefix* of tmp_path → spurious partial redaction).
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return str(tmp_path)


# ------------------------------------------------------------- redact_home


def test_redact_home_replaces_home_prefix(home):
    assert sanitize.redact_home(f"{home}/.claude/x.jsonl") == "~/.claude/x.jsonl"


def test_redact_home_noop_when_absent(home):
    assert sanitize.redact_home("/etc/passwd") == "/etc/passwd"


# ------------------------------------------------------- looks_like_identifier


@pytest.mark.parametrize(
    "value",
    ["claude-opus-4-8", "session-abc_123", "Stop", "gpt-5-codex", "4.8", "abc123"],
)
def test_identifier_accepts_slugs(value):
    assert sanitize.looks_like_identifier(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "has spaces",
        "user@example.com",
        "/home/alice/x",
        "a/b",
        ".claude",  # leading dot is not alphanumeric
        "x" * 200,  # too long to be a slug
        "I am a sentence of prose.",
    ],
)
def test_identifier_rejects_prose_paths_emails(value):
    assert not sanitize.looks_like_identifier(value)


# --------------------------------------------------------------- scrub_json


def test_scrub_json_passes_numbers_bools_null():
    obj = {"input_tokens": 123, "ratio": 1.5, "ok": True, "off": False, "none": None}
    assert sanitize.scrub_json(obj) == obj


def test_scrub_json_keeps_keys_verbatim_redacts_string_leaves(home):
    obj = {
        "session_id": "abc-123",  # identifier -> kept
        "transcript_path": f"{home}/.claude/x.jsonl",  # path -> redacted
        "email": "me@example.com",  # email -> redacted
        "prose": "this is a free-form sentence",  # prose -> redacted
        "model": "claude-opus-4-8",  # identifier -> kept
    }
    out = sanitize.scrub_json(obj)
    assert set(out) == set(obj)  # keys kept verbatim
    assert out["session_id"] == "abc-123"
    assert out["model"] == "claude-opus-4-8"
    assert out["transcript_path"] == "<redacted:str>"
    assert out["email"] == "<redacted:str>"
    assert out["prose"] == "<redacted:str>"


def test_scrub_json_preserves_list_length_not_content():
    out = sanitize.scrub_json({"items": ["a b c", "tok-1", 7]})
    assert out["items"] == ["<redacted:str>", "tok-1", 7]


def test_scrub_json_depth_guard():
    obj = cur = {}
    for _ in range(60):
        cur["next"] = {}
        cur = cur["next"]
    cur["leaf"] = "deep"
    out = sanitize.scrub_json(obj, max_depth=10)
    # walk down to the guard
    node = out
    saw_guard = False
    for _ in range(60):
        if node == "<redacted:depth>":
            saw_guard = True
            break
        node = node.get("next")
        if node is None:
            break
    assert saw_guard


# --------------------------------------------------------------- scrub_text


def test_scrub_text_keeps_flags_redacts_email_and_home(home):
    text = f"Usage: foo [options]\n  --bar    do bar\ncontact me@example.com or see {home}/cfg"
    out = sanitize.scrub_text(text)
    assert "--bar" in out
    assert "me@example.com" not in out
    assert "<redacted:email>" in out
    assert f"{home}/cfg" not in out
    assert "~/cfg" in out


def test_scrub_text_max_lines_truncates():
    out = sanitize.scrub_text("\n".join(f"line{i}" for i in range(50)), max_lines=5)
    assert out.count("\n") == 5  # 5 kept lines + the ellipsis marker
    assert "more lines redacted" in out


def test_scrub_event_payload_is_scrub_json(home):
    payload = {"session_id": "s-1", "cwd": f"{home}/proj", "n": 5}
    out = sanitize.scrub_event_payload(payload)
    assert out == {"session_id": "s-1", "cwd": "<redacted:str>", "n": 5}


# --------------------------------------------------------------- looks_like_secret


@pytest.mark.parametrize(
    "value",
    [
        "ghp_CANARYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx01",  # github token
        "sk-CANARYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx99",  # openai
        "sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxx",  # anthropic
        "AKIAIOSFODNN7EXAMPLE",  # aws access key
        "xoxb-123456789012-abcdefghijkl",  # slack bot token
        "glpat-xxxxxxxxxxxxxxxxxxxx",  # gitlab pat
        "AIzaSyA0000000000000000000000000000000",  # google api key
        "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",  # 40-char high-entropy hex secret
    ],
)
def test_looks_like_secret_catches_credentials(value):
    assert sanitize.looks_like_secret(value)


@pytest.mark.parametrize(
    "value",
    [
        "claude-opus-4-8",
        "gpt-5-codex",
        "session-abc_123",
        "Stop",
        "01234567-89ab-cdef-0123-456789abcdef",  # UUID: short runs at hyphens
        "DW-1",
        "1.2-add-logging",
    ],
)
def test_looks_like_secret_passes_safe_slugs(value):
    assert not sanitize.looks_like_secret(value)


def test_scrub_json_redacts_identifier_shaped_secrets():
    obj = {"model": "claude-opus-4-8", "token": "ghp_CANARYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx01"}
    out = sanitize.scrub_json(obj)
    assert out["model"] == "claude-opus-4-8"
    assert out["token"] == "<redacted:secret>"


def test_scrub_json_scrubs_sensitive_dict_keys(home):
    # diagnostics routes unknown/future fields through scrub_json, so a key —
    # not just a value — that is a home path or credential-shaped must be
    # redacted, while a plain identifier key (and a safe value) survives.
    obj = {
        "ghp_CANARYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx01": "v",  # secret-shaped key
        f"{home}/secret/project": "v",  # home-path key
        "model": "claude-opus-4-8",  # identifier key + safe value
    }
    out = sanitize.scrub_json(obj)
    assert "ghp_CANARYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx01" not in out
    assert not any(home in k for k in out)
    assert out["model"] == "claude-opus-4-8"


# --------------------------------------------------------------- Pseudonymizer


def test_pseudonymizer_is_stable_within_a_dump():
    p = sanitize.Pseudonymizer()
    a = p.alias("1.2-secret", ns="story", epic=1)
    assert a == p.alias("1.2-secret", ns="story", epic=1)  # cached / stable
    assert re.fullmatch(r"s1-[0-9a-f]{12}", a)
    assert p.alias(None) is None and p.alias("") == ""
    # legend reverses locally; original never equals the alias
    assert p.legend()[a] == "1.2-secret"


def test_pseudonymizer_salt_differs_across_instances():
    a = sanitize.Pseudonymizer().alias("x", ns="branch")
    b = sanitize.Pseudonymizer().alias("x", ns="branch")
    assert a != b  # different per-dump salt -> not correlatable across dumps


# --------------------------------------------------------------- assert_no_leak


def test_assert_no_leak_clean_text():
    assert sanitize.assert_no_leak("phase=done tokens=42 model=claude-opus-4-8") == []


@pytest.mark.parametrize(
    "text,rule",
    [
        ("contact me@example.com", "email"),
        ("see https://user:pass@host/x", "url-credentials"),
        ("path /home/alice/x", "absolute-home-path"),
        ("key ghp_CANARYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx01", "secret"),
    ],
)
def test_assert_no_leak_fires(text, rule):
    assert rule in sanitize.assert_no_leak(text)


def test_assert_no_leak_extra_word_boundary():
    # short basename does not false-positive inside a longer word...
    assert sanitize.assert_no_leak("the project root", extra=["proj"]) == []
    # ...but a standalone occurrence is caught — and the rule names the position,
    # never the value, so the failure message can't leak the sensitive string.
    fired = sanitize.assert_no_leak("dir proj here", extra=["proj"])
    assert fired == ["sensitive[0]"]
    assert "proj" not in "".join(fired)
    # values whose own edge is punctuation are still caught (the \b blind spot)
    assert sanitize.assert_no_leak("see .acme here", extra=[".acme"]) == ["sensitive[0]"]
    assert sanitize.assert_no_leak("use acme. now", extra=["acme."]) == ["sensitive[0]"]
