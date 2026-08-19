"""The crown-jewel PII case table for the probe sanitizer."""

import json
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
        # Every arm of _ABS_HOME_RE, not just the Linux one. The macOS and root
        # arms were correct but unpinned; so was the drive-letter-plus-FORWARD-
        # slash form (`C:/Users/...`, what Path.as_posix and MSYS-ish tooling
        # produce), which the `/Users/` arm already subsumes as a substring —
        # the Windows arm exists for BACKSLASHES only. Reviewers have read the
        # rule as "Windows is handled solely by the drive-letter arm" and
        # proposed widening it to `[\\/]`; these cases show why that is a no-op.
        ("path /Users/alice/x", "absolute-home-path"),
        ("path /root/x", "absolute-home-path"),
        ("path C:/Users/alice/x", "absolute-home-path"),
        # The Windows→WSL UNC bridge (#512). These are NOT redundant with the
        # `/home/` row above: the bridge spelling is BACKSLASH-separated, so no
        # forward-slash arm can see it, and the identifier it carries is the
        # *Linux* username — which assert_no_leak's username rule cannot match,
        # because that rule compares `getpass.getuser()`, the *Windows* account.
        # The path rule is therefore the only rule that can fire on this shape.
        # The last row pins the `root` half of the same arm.
        (r"path \\wsl.localhost\Ubuntu-24.04\home\u\p", "absolute-home-path"),
        (r"path \\wsl$\Ubuntu\home\alice\proj", "absolute-home-path"),
        (r"path \\?\UNC\wsl.localhost\Ubuntu\home\a", "absolute-home-path"),
        (r"path \\wsl.localhost\Ubuntu\root\proj", "absolute-home-path"),
        ("key ghp_CANARYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx01", "secret"),
    ],
)
def test_assert_no_leak_fires(text, rule):
    assert rule in sanitize.assert_no_leak(text)


def test_assert_no_leak_sees_wsl_unc_through_the_json_render():
    """#512's literal reproduction, asserted on BOTH encodings.

    The same bytes reach the guard as raw text (the markdown report) and as JSON
    text (the ``--json`` document), and ``json.dumps`` DOUBLES every backslash —
    the historic trap documented at ``src/bmad_loop/cli.py:3737-3750``. The two
    encodings fail for different reasons, so a test that checked only one would
    pass while the shipped ``--json`` document still leaked.
    """
    raw = r"\\wsl.localhost\Ubuntu-24.04\home\u\p"
    assert "absolute-home-path" in sanitize.assert_no_leak(raw)

    rendered = json.dumps({"env": {"raw_project": raw}})
    assert r"\\\\wsl.localhost" in rendered  # the doubling actually happened
    assert "absolute-home-path" in sanitize.assert_no_leak(rendered)


@pytest.mark.parametrize(
    "text",
    [
        "path D:/data/alice/x",
        "path /var/lib/alice/x",
        # Backslash negatives bound the #512 arm: it names *home* trees, not any
        # UNC path and not any backslash. A share that is not a home tree, a
        # plain drive path, and the two ordinary words that contain the literal
        # substrings `home` and `root` must all stay clean.
        r"path \\server\share\data\alice\x",
        r"path D:\data\alice\x",
        "note homeroom and rootkit are ordinary words",
    ],
)
def test_assert_no_leak_home_rule_is_not_any_absolute_path(text):
    """The rule names *home* directories, and firing is fail-closed — diagnose
    refuses to emit. Matching every absolute path would turn an ordinary dump
    into a refusal, so the bound is load-bearing in the other direction too."""
    assert "absolute-home-path" not in sanitize.assert_no_leak(text)


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


def test_assert_no_leak_labeled_extras():
    # a (value, label) pair reports the label — printable by construction —
    # instead of the opaque enumerate position, and never echoes the value
    fired = sanitize.assert_no_leak(
        "dir secretkey1 here", extra=[("secretkey1", "story:s1-ab12cd34ef56")]
    )
    assert fired == ["sensitive[story:s1-ab12cd34ef56]"]
    assert "secretkey1" not in "".join(fired)
    # mixed: bare items keep their position-based name
    fired = sanitize.assert_no_leak("alpha beta", extra=[("alpha", "branch:b-1"), "beta"])
    assert fired == ["sensitive[branch:b-1]", "sensitive[1]"]
    # labeled values below the 4-char threshold never fire, same as bare ones
    assert sanitize.assert_no_leak("a bc d", extra=[("bc", "story:s-x")]) == []


# --------------------------------------------------------- replace_standalone


@pytest.mark.parametrize(
    "text,needle,expected,count",
    [
        ("dir proj here", "proj", "dir X here", 1),  # mid-text
        ("proj at start", "proj", "X at start", 1),  # string edge (start)
        ("ends with proj", "proj", "ends with X", 1),  # string edge (end)
        ("the project root", "proj", "the project root", 0),  # embedded: untouched
        ("see .acme here", ".acme", "see X here", 1),  # punctuation-edge needle
        ("use acme. now", "acme.", "use X now", 1),
        ("acme.acme", "acme", "X.X", 2),  # adjacent occurrences
        ("no needle here", "zzzz", "no needle here", 0),  # absent
        ("aaa", "aa", "aaa", 0),  # word-flanked overlap never matches
    ],
)
def test_replace_standalone_table(text, needle, expected, count):
    assert sanitize.replace_standalone(text, needle, "X") == (expected, count)


def test_replace_standalone_terminates_and_is_idempotent():
    # a replacement containing the needle is not rescanned within the call
    out, n = sanitize.replace_standalone("ref key1 end", "key1", "x-key1-x")
    assert (out, n) == ("ref x-key1-x end", 1)
    # a replacement free of the needle: a second pass finds nothing
    out, n = sanitize.replace_standalone("dir proj here", "proj", "p-1a2b")
    assert n == 1
    assert sanitize.replace_standalone(out, "proj", "p-1a2b") == (out, 0)
    # replacement mirrors detection exactly: whatever fired assert_no_leak is
    # gone after one substitution with a needle-free replacement
    assert sanitize.assert_no_leak(out, extra=["proj"]) == []


def test_pseudonymizer_entries_expose_ns():
    p = sanitize.Pseudonymizer()
    a_story = p.alias("1.2-secret", ns="story", epic=1)
    a_branch = p.alias("feat/secret", ns="branch")
    assert p.entries() == [
        ("story", "1.2-secret", a_story),
        ("branch", "feat/secret", a_branch),
    ]
    # legend keeps its shape: alias -> original, ns discarded
    assert p.legend() == {a_story: "1.2-secret", a_branch: "feat/secret"}


# ------------------------------------------------- guard / assert_clean
# The fail-closed egress policy shared by diagnose and probe-adapter (#199).
# STORY_KEY embeds a proprietary product name as a substring, so "the original
# never appears in the exception" is asserted against the nastiest shape.

STORY_KEY = "1.2-AcmeQuantumBillingEngine"


def test_guard_clean_text_is_returned_verbatim():
    pseudo = sanitize.Pseudonymizer()
    pseudo.alias(STORY_KEY, ns="story", epic=1)
    assert sanitize.guard("nothing sensitive here", pseudo) == ("nothing sensitive here", [])
    # and a missing pseudonymizer still runs the hard rules
    with pytest.raises(sanitize.LeakDetected) as exc:
        sanitize.guard("contact victim.canary@example.com")
    assert exc.value.rules == ["email"]


def test_guard_repairs_stray_original_and_tallies():
    pseudo = sanitize.Pseudonymizer()
    alias = pseudo.alias(STORY_KEY, ns="story", epic=1)
    text = f"path a/{STORY_KEY}/b and again {STORY_KEY}"
    out, reps = sanitize.guard(text, pseudo)
    assert STORY_KEY not in out
    assert out.count(alias) == 2
    assert reps == [(f"story:{alias}", 2)]
    # the repaired text passes the same check that fired on the input
    assert sanitize.assert_no_leak(out, extra=[STORY_KEY]) == []


def test_guard_hard_rules_never_auto_repair():
    pseudo = sanitize.Pseudonymizer()
    with pytest.raises(sanitize.LeakDetected) as exc:
        sanitize.guard("contact victim.canary@example.com", pseudo)
    assert "email" in exc.value.rules
    # a hard rule alongside a repairable one: refuse immediately, and the
    # sensitive rule rides along under its printable ns:alias label
    key_alias = pseudo.alias(STORY_KEY, ns="story", epic=1)
    with pytest.raises(sanitize.LeakDetected) as exc:
        sanitize.guard(f"{STORY_KEY} contact victim.canary@example.com", pseudo)
    assert "email" in exc.value.rules
    assert f"sensitive[story:{key_alias}]" in exc.value.rules
    assert STORY_KEY not in str(exc.value)


class _CyclicPseudo(sanitize.Pseudonymizer):
    """Adversarial stand-in: each alias embeds the OTHER original at a "-"
    boundary, so every substitution reintroduces the other value — a cycle a
    real Pseudonymizer could only produce by hash-output coincidence."""

    def entries(self):
        return [
            ("story", "alpha-key", "s1-beta-key"),
            ("branch", "beta-key", "branch-alpha-key"),
        ]


def test_guard_repair_bound_terminates_and_fails_closed():
    with pytest.raises(sanitize.LeakDetected):
        sanitize.guard("ref alpha-key end", _CyclicPseudo())


def test_assert_clean_raises_and_never_repairs():
    pseudo = sanitize.Pseudonymizer()
    alias = pseudo.alias(STORY_KEY, ns="story", epic=1)
    with pytest.raises(sanitize.LeakDetected) as exc:
        sanitize.assert_clean(f"stray {STORY_KEY} here", pseudo)
    assert exc.value.rules == [f"sensitive[story:{alias}]"]
    sanitize.assert_clean("all clean", pseudo)  # no raise on clean input


def test_embeds_current_username(monkeypatch):
    monkeypatch.setattr(sanitize.getpass, "getuser", lambda: "alice")
    assert sanitize.embeds_current_username("pytest-of-alice")
    assert sanitize.embeds_current_username("alice")
    assert not sanitize.embeds_current_username("someone-else")
    # below the ≥5 threshold shared with the assert_no_leak username rule
    monkeypatch.setattr(sanitize.getpass, "getuser", lambda: "bob")
    assert not sanitize.embeds_current_username("bob-dir")
