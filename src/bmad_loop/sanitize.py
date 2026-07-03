"""PII-scrubbing chokepoint for `bmad-loop probe-adapter` and `bmad-loop diagnose`.

Pure stdlib, no bmad_loop imports — the single audited place that decides what
data from a foreign CLI (or a user's own run dir) is safe to show a maintainer.
Both the probe and the diagnostic-dump commands route every captured payload,
help/version blob, discovered path, and run-state value through here before
rendering; nothing is displayed raw.

Guarantees:
- token *counts* are non-PII, so numbers/bools/null pass through verbatim;
- dict **keys** are kept verbatim — field names/casing are the whole point of a
  payload probe — but every leaf **string** is `$HOME`-redacted and then kept
  ONLY if it matches a conservative identifier shape (a short slug with no
  spaces / `@` / `/`, e.g. ``claude-opus-4-8`` or ``session-abc_123``);
  anything else (prose, code, paths, emails) becomes ``<redacted:str>``;
- an identifier-shaped string that looks like a **secret** (a known credential
  prefix such as ``ghp_``/``sk-``/``AKIA``, a JWT, or a long high-entropy blob)
  becomes ``<redacted:secret>`` even though it would otherwise pass — the one
  hole the identifier shape would leave open;
- list lengths are preserved (the count is structural, the contents aren't);
- recursion is depth-guarded so a pathological payload can't blow the stack.

It also offers two helpers the dump leans on but the probe does not need:
:class:`Pseudonymizer` (stable, irreversible per-dump aliases for proprietary
identifiers — story keys, branches, SHAs — that *are* identifier-shaped and so
would otherwise survive verbatim) and :func:`assert_no_leak` (a final-output
self-check the dump runs over its own rendered bytes before writing, so a
routing bug or a future field can never silently ship a secret/PII/path).
"""

from __future__ import annotations

import getpass
import hashlib
import math
import os
import re
import secrets
from collections import Counter
from typing import Any, Iterable

# A conservative "this is a machine identifier, not prose or PII" shape: starts
# alphanumeric, then only word-ish chars (letters, digits, ``.`` ``_`` ``-``),
# bounded length. No spaces, no ``@``, no ``/`` — so emails, paths, and sentences
# can never satisfy it. Model ids and session/conversation ids do.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IDENTIFIER_MAX = 80

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Known credential token shapes — provider prefixes plus the JWT header. These
# are exactly the strings that are identifier-shaped (so would pass the slug
# gate) yet must never be surfaced. Anchored: a value *starting* with one of
# these is treated as a secret.
_SECRET_PREFIX_RE = re.compile(
    r"^(?:"
    r"sk-|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|gss_|"
    r"xox[baprs]-|AKIA|ASIA|AIza|ya29\.|AGAPP|hf_|npm_|dop_v1_|sk-ant-"
    r")"
)
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+")
# Contiguous alphanumeric runs — a UUID/slug breaks into short runs at its
# hyphens, but a raw API token / hex secret is one long dense run.
_ALNUM_RUN_RE = re.compile(r"[A-Za-z0-9]+")
_SECRET_RUN_MIN = 32  # length of contiguous alnum run that triggers the entropy gate
_SECRET_ENTROPY_MIN = 3.5  # bits/char; pure hex ~4.0, base64 ~6.0, prose/slug well below

# Token shape used by assert_no_leak to re-scan rendered output for secrets.
_LEAK_TOKEN_RE = re.compile(r"[A-Za-z0-9._/+-]{6,}")
_URL_CRED_RE = re.compile(r"https?://[^/\s]*:[^/@\s]+@")
_ABS_HOME_RE = re.compile(r"/home/|/Users/|/root/|[A-Za-z]:\\Users\\", re.I)

_REDACTED_STR = "<redacted:str>"
_REDACTED_SECRET = "<redacted:secret>"  # nosec B105 - redaction marker, not a credential
_REDACTED_EMAIL = "<redacted:email>"
_REDACTED_DEPTH = "<redacted:depth>"


def _home() -> str:
    home = os.path.expanduser("~")
    return home if home and home != "~" else ""


def redact_home(s: str) -> str:
    """Replace the current user's home directory prefix with ``~``.

    Catches the literal expanded home (``/home/alice`` -> ``~``); the munged,
    slash-stripped forms some CLIs use for directory names (``-home-alice-...``)
    do not match a path and are handled by the identifier filter instead.
    """
    home = _home()
    if home and home != "/" and home in s:
        s = s.replace(home, "~")
    return s


def looks_like_identifier(s: str) -> bool:
    """True for a short machine slug safe to surface verbatim (no PII)."""
    return 0 < len(s) <= _IDENTIFIER_MAX and bool(_IDENTIFIER_RE.match(s))


def _shannon_entropy(s: str) -> float:
    """Bits per character — high for random tokens, low for words/slugs."""
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def looks_like_secret(s: str) -> bool:
    """True for a credential-shaped string that must never be surfaced.

    Catches values that *are* identifier-shaped (so :func:`looks_like_identifier`
    would pass them) but are secrets: a known provider prefix (``ghp_``, ``sk-``,
    ``AKIA``, ``xoxb-`` …), a JWT, or a long high-entropy contiguous run (a raw
    API token / hex key). A UUID or hyphenated slug breaks into short runs at its
    separators, so ``claude-opus-4-8`` and ``01234567-89ab-cdef-…`` stay safe."""
    if _SECRET_PREFIX_RE.match(s) or _JWT_RE.match(s):
        return True
    runs = _ALNUM_RUN_RE.findall(s)
    longest = max(runs, key=len) if runs else ""
    return len(longest) >= _SECRET_RUN_MIN and _shannon_entropy(longest) >= _SECRET_ENTROPY_MIN


def scrub_text(s: str, *, max_lines: int | None = None) -> str:
    """Sanitize free text (a CLI's ``--help`` / ``--version`` / a log tail).

    Less aggressive than :func:`scrub_json` — help text is the CLI's own and
    flag lines must survive — so we only redact the home dir and any emails,
    then optionally cap the line count.
    """
    s = redact_home(s)
    s = _EMAIL_RE.sub(_REDACTED_EMAIL, s)
    if max_lines is not None:
        lines = s.splitlines()
        if len(lines) > max_lines:
            dropped = len(lines) - max_lines
            lines = lines[:max_lines] + [f"… ({dropped} more lines redacted)"]
        s = "\n".join(lines)
    return s


def _is_word_boundary(ch: str) -> bool:
    # a string edge ("") or any non-word char counts as a boundary; "word" = [A-Za-z0-9_]
    return ch == "" or not (ch.isalnum() or ch == "_")


def _contains_standalone(text: str, needle: str) -> bool:
    """Whole-token search for an opaque needle: matches only when the needle is not
    flanked by word characters. Unlike re's ``\\b`` on the needle's own edges, this
    still fires when the needle begins or ends with punctuation (e.g. ``.acme``)."""
    start = 0
    while (idx := text.find(needle, start)) >= 0:
        before = text[idx - 1] if idx else ""
        after = text[idx + len(needle)] if idx + len(needle) < len(text) else ""
        if _is_word_boundary(before) and _is_word_boundary(after):
            return True
        start = idx + 1
    return False


def _scrub_str(s: str) -> str:
    """Sanitize a single string: redact a home path, drop free-form prose, and
    redact a credential-shaped token; an identifier-shaped slug passes verbatim."""
    red = redact_home(s)
    if not looks_like_identifier(red):
        return _REDACTED_STR
    return _REDACTED_SECRET if looks_like_secret(red) else red


def _scrub(obj: Any, depth: int, max_depth: int) -> Any:
    if depth > max_depth:
        return _REDACTED_DEPTH
    # bool is an int subclass — handled by the numeric branch; both pass through.
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return _scrub_str(obj)
    if isinstance(obj, dict):
        # Keys get the same scrub as string values, so a home-path or
        # credential-shaped key can't leak where the equivalent value would be
        # caught (diagnostics routes unknown/future fields through here). Two
        # distinct non-identifier keys can collapse to the same <redacted:str>
        # and merge — acceptable under safe-by-default (redaction over fidelity).
        return {_scrub_str(str(k)): _scrub(v, depth + 1, max_depth) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, depth + 1, max_depth) for v in obj]
    # any other type (shouldn't appear in JSON) is treated as an opaque string
    return _REDACTED_STR


def scrub_json(obj: Any, *, max_depth: int = 40) -> Any:
    """Recursively sanitize a JSON-shaped value (see module docstring)."""
    return _scrub(obj, 0, max_depth)


def scrub_event_payload(payload: Any) -> Any:
    """Sanitize one captured hook payload — the probe's per-event chokepoint."""
    return scrub_json(payload)


class Pseudonymizer:
    """Stable, irreversible aliases for proprietary identifiers in a dump.

    Story keys, branch names, spec filenames and SHAs are identifier-shaped, so
    :func:`scrub_json` would pass them verbatim and leak the customer's feature
    names. The dump routes each through :meth:`alias` instead: a per-invocation
    random salt makes the alias unguessable across dumps, while caching makes it
    *stable within* one dump — so a maintainer can see that the story which
    escalated is the same one that later deferred, without ever learning its
    name. The alias is a salted BLAKE2s digest; the salt is never persisted, so
    no map survives that could reverse it.

    The :meth:`legend` (alias -> original) exists only as a LOCAL convenience for
    the user who generated the dump; it is never written into the shipped report,
    and :func:`assert_no_leak` is fed its values to prove none slipped through.
    """

    def __init__(self, salt: bytes | None = None):
        self._salt = salt if salt is not None else secrets.token_bytes(16)
        self._map: dict[tuple[str, str], str] = {}
        self._aliases: dict[str, str] = {}  # alias -> original, for collision rejection

    def alias(self, value: Any, *, ns: str = "id", epic: int | None = None) -> Any:
        """Map ``value`` to its stable alias. ``None``/empty pass through; a story
        alias prefixes the epic for legibility (``s1-3f2a9c``)."""
        if value is None or value == "":
            return value
        value = str(value)
        key = (ns, value)
        cached = self._map.get(key)
        if cached is not None:
            return cached
        prefix = f"s{epic}" if (ns == "story" and epic is not None) else ns
        # 48-bit digest makes collisions vanishingly unlikely even for large
        # dumps; but a clash is not cosmetic — it would merge two stories'
        # per_alias_event_counts and overwrite a legend() entry — so on the rare
        # collision re-hash with a counter until the alias is free.
        counter = 0
        while True:
            material = self._salt + value.encode("utf-8")
            if counter:
                material += counter.to_bytes(4, "big")
            alias = f"{prefix}-{hashlib.blake2s(material, digest_size=6).hexdigest()}"
            owner = self._aliases.get(alias)
            if owner is None or owner == value:
                break
            counter += 1
        self._aliases[alias] = value
        self._map[key] = alias
        return alias

    def legend(self) -> dict[str, str]:
        """alias -> original, for LOCAL use only. Never write this into a dump."""
        return {alias: value for (_, value), alias in self._map.items()}


def assert_no_leak(text: str, *, extra: Iterable[str] = ()) -> list[str]:
    """Re-scan already-rendered output for anything that must not ship.

    The defense-in-depth backstop to the per-field routing: even if a handler is
    wrong or a new field is added, this catches an email, a credential-shaped
    token (same logic as :func:`looks_like_secret`), URL-embedded creds, an
    absolute home path, the current username, or any caller-supplied sensitive
    string (e.g. a project basename, or every :meth:`Pseudonymizer.legend` value)
    in the final bytes. Returns the list of rule names that fired — empty means
    clean. Callers fail closed (refuse to write) on a non-empty result.
    """
    fired: list[str] = []
    if _EMAIL_RE.search(text):
        fired.append("email")
    if _URL_CRED_RE.search(text):
        fired.append("url-credentials")
    if _ABS_HOME_RE.search(text):
        fired.append("absolute-home-path")
    if any(looks_like_secret(tok) for tok in _LEAK_TOKEN_RE.findall(text)):
        fired.append("secret")
    try:
        user = getpass.getuser()
    except Exception:
        # No passwd entry / no USER env (minimal containers): this one
        # defense-in-depth rule simply can't run; the rest still cover the output.
        user = ""
    if len(user) >= 5 and _contains_standalone(text, user):
        fired.append("username")
    for i, item in enumerate(extra):
        item = str(item)
        # delimiter check so a short basename ("proj") can't false-positive on a
        # common word that contains it ("project"), yet a value whose own edge is
        # punctuation (".acme") is still caught — a blind spot of a \b regex.
        # Report the position only — never echo the value, since this rule name is
        # surfaced in the CLI failure message and would otherwise leak it.
        if len(item) >= 4 and _contains_standalone(text, item):
            fired.append(f"sensitive[{i}]")
    return fired
