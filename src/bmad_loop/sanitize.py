"""PII-scrubbing chokepoint for `bmad-loop probe-adapter` and `bmad-loop diagnose`.

Pure stdlib, no bmad_loop imports — the single audited place that decides what
data from a foreign CLI (or a user's own run dir) is safe to show a maintainer.
Both the probe and the diagnostic-dump commands route every captured payload,
help/version blob, discovered path, and run-state value through here before
rendering; nothing is displayed raw.

Guarantees:
- token *counts* are non-PII, so numbers/bools/null pass through verbatim;
- dict **keys** get the same scrub as leaf strings (a home-path or
  credential-shaped key can't leak where the equivalent value would be caught);
  every leaf **string** is `$HOME`-redacted and then kept ONLY if it matches a
  conservative identifier shape (a short slug with no spaces / `@` / `/`, e.g.
  ``claude-opus-4-8`` or ``session-abc_123``); anything else (prose, code,
  paths, emails) becomes ``<redacted:str>``;
- an identifier-shaped string that looks like a **secret** (a known credential
  prefix such as ``ghp_``/``sk-``/``AKIA``, a JWT, or a long high-entropy blob)
  becomes ``<redacted:secret>`` even though it would otherwise pass — the one
  hole the identifier shape would leave open;
- list lengths are preserved (the count is structural, the contents aren't);
- recursion is depth-guarded so a pathological payload can't blow the stack.

It also owns the shared egress backstop both commands run over their own
rendered bytes before emitting: :class:`Pseudonymizer` (stable, irreversible
per-report aliases for proprietary identifiers — story keys, branches, SHAs,
project names — that *are* identifier-shaped and so would otherwise survive
verbatim), :func:`assert_no_leak` (the raw re-scan; accepts labeled extras
``(value, label)`` so a hit is reported by a printable label instead of an
opaque index), and :func:`guard` / :func:`assert_clean` (the fail-closed
policy around it: hard-rule hits — email/secret/home-path/url-creds/username —
refuse outright by raising :class:`LeakDetected`, while a stray pseudonymizer
original is repaired via :func:`replace_standalone` under identical
word-boundary semantics, re-verified, and disclosed to the caller). A routing
bug or a future field can therefore never silently ship a secret/PII/path.
"""

# Strict-checked under #245 Stage 2, with the two "expression fully known" rules
# below relaxed for this file only: `_scrub` walks arbitrary, foreign JSON whose
# leaves are `Any` by design (it exists to redact unknown/future fields), so
# isinstance-narrowing that `Any` yields dict/list `[Unknown, ...]` and every
# key/value it iterates is Unknown. Typing it away would defeat the point of a
# catch-all scrubber. Every other strict rule stays on.
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import getpass
import hashlib
import math
import os
import re
import secrets
from collections import Counter
from typing import Any, Iterable, Iterator

# A conservative "this is a machine identifier, not prose or PII" shape: starts
# alphanumeric, then only word-ish chars (letters, digits, ``.`` ``_`` ``-``),
# bounded length. No spaces, no ``@``, no ``/`` — so emails, paths, and sentences
# can never satisfy it. Model ids and session/conversation ids do.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IDENTIFIER_MAX = 80

# Per-line character bound for :func:`scrub_text`. Real ``--version``/``--help``
# lines from the coding CLIs this probes run well under ~120 characters, so 200
# is roughly 2x headroom over legitimate output while still bounding an
# adversarial or corrupted one. Public because `probe.py` passes it explicitly
# rather than relying on a hidden default.
SCRUB_TEXT_MAX_CHARS = 200

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
# The guard constructs whose match can straddle a truncation point AND whose
# leading fragment would still be sensitive once the rest is clipped away.
# `scrub_text`'s cut retracts out of these -- see `_truncate_line`. This sits
# beside assert_no_leak's rule list deliberately: a rule added there needs an
# entry here only if a PREFIX of its match stays sensitive. `_EMAIL_RE` is absent
# because scrub_text redacts emails BEFORE it truncates, so none survives to be
# split; `_ABS_HOME_RE` is absent because a fragment too short to match `/home/`
# no longer carries the home tree the rule names.
_TRUNCATION_HAZARD_RES = (_LEAK_TOKEN_RE, _URL_CRED_RE)
# The same bytes reach assert_no_leak either as raw text (the markdown report)
# or as JSON text (the --json document), and json.dumps DOUBLES a backslash —
# `C:\Users\alice` is serialized as `C:\\Users\\alice`. Matching only the raw
# form let a Windows home path through the JSON render untouched, so the
# separator alternates one-or-two backslashes. POSIX prefixes need no such
# treatment: `/` is not escaped by JSON.
#
# The drive-letter arm is for BACKSLASHES only, deliberately: the forward-slash
# Windows form (`C:/Users/alice`, from Path.as_posix or MSYS-ish tooling) already
# matches the `/Users/` arm as a substring, as does git-bash's `/c/Users/alice`.
# Widening the drive-letter arm to `[\\/]` buys only the mixed-separator oddity
# `C:\Users/alice` — and any string carrying a separator at all is rejected by
# looks_like_identifier upstream and redacted before it can reach here.
#
# The backslash `home`/`root` arm is for the Windows→WSL UNC bridge (#512):
# `\\wsl.localhost\<distro>\home\<user>`, the legacy `\\wsl$\...`, and the
# extended-length `\\?\UNC\wsl.localhost\...` folding all reach a POSIX home
# directory whose separators are BACKSLASHES, so none of the forward-slash arms
# can see them. That shape needs a path rule for a second reason: the identifier
# at risk is the *Linux* username, while the username rule in assert_no_leak
# compares `getpass.getuser()` — the *Windows* account — so on a native-Windows
# interpreter reaching a distro path, no other rule can fire on the name that
# matters. The arm anchors on the home segment rather than the `wsl` host
# because a host anchor misses `\\?\UNC\wsl.localhost\...` entirely: the
# `?\UNC\` segment sits between the leading backslashes and `wsl`. `Users` is
# deliberately NOT in this arm — it stays behind the drive-letter arm above,
# which is the bound the preceding paragraph justifies.
# tests/test_sanitize.py::test_assert_no_leak_fires pins each arm;
# ::test_assert_no_leak_home_rule_is_not_any_absolute_path pins the other
# direction, since a hit makes diagnose refuse to emit at all.
_ABS_HOME_RE = re.compile(
    r"/home/|/Users/|/root/|[A-Za-z]:\\{1,2}Users\\{1,2}|\\{1,2}(?:home|root)\\{1,2}", re.I
)

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


def _truncate_line(line: str, max_chars: int) -> str:
    """One line clipped to ``max_chars``, never in a way that blinds the guard.

    The naive cut is at ``max_chars``. That is unsafe when it lands INSIDE
    something :func:`assert_no_leak` would have flagged, because the fragment it
    leaves behind can keep the sensitive part while no longer tripping the rule
    — turning a fail-closed refusal into an emission. Two shapes reach here:

    * a credential-shaped token, where the entropy arm of
      :func:`looks_like_secret` needs a contiguous alnum run of
      ``_SECRET_RUN_MIN``, so clipping a 36-character token to 30 leaves a
      credential prefix the guard no longer recognizes; and
    * a URL credential, whose match ends at the ``@`` — clip that off and
      ``_URL_CRED_RE`` stops matching while the password prefix still ships.

    Rather than special-case either, the cut retracts out of any
    ``_TRUNCATION_HAZARD_RES`` match it splits whose whole trips the guard while
    its surviving prefix does not, using :func:`assert_no_leak` itself as the
    oracle. Deferring to the guard is what keeps this general: it is the same
    verdict the rendered bytes are judged by, so a rule added there is covered
    here without a second implementation of what "sensitive" means.

    That verdict test is also what keeps the cap from over-firing. ``"x" * 5000``
    is a single 5000-character token, but it trips no rule whole OR clipped, so
    it still truncates at exactly ``max_chars``; a ``ghp_``-prefixed token is
    matched at its start and still trips the guard once clipped, so it is not
    retracted either and the refusal is preserved with no content dropped (#481).
    """
    if len(line) <= max_chars:
        return line
    cut = max_chars
    # Retracting out of one hazard can land the cut inside an earlier one, so
    # iterate to a fixed point. `cut` only ever decreases, so this terminates.
    changed = True
    while changed:
        changed = False
        for pattern in _TRUNCATION_HAZARD_RES:
            for match in pattern.finditer(line):
                if (
                    match.start() < cut < match.end()
                    and assert_no_leak(match.group())
                    and not assert_no_leak(line[match.start() : cut])
                ):
                    cut = match.start()
                    changed = True
    return line[:cut] + f"… ({len(line) - cut} more chars redacted)"


def scrub_text(s: str, *, max_lines: int | None = None, max_chars: int | None = None) -> str:
    """Sanitize free text (a CLI's ``--help`` / ``--version`` / a log tail).

    Less aggressive than :func:`scrub_json` — help text is the CLI's own and
    flag lines must survive — so we only redact the home dir and any emails,
    then optionally cap the line count and each line's length.

    ``max_chars`` bounds each line's **content** at ``max_chars``; a truncated
    line is emitted as at most ``max_chars`` characters plus the
    ``… (N more chars redacted)`` marker, so its total length is at most
    ``max_chars + len(marker)`` — "at most" because a cut that would split a
    credential-shaped token retracts to that token's start rather than emit a
    guard-invisible prefix of it (see :func:`_truncate_line`). That overshoot is
    the same off-by-one ``max_lines`` already has — ``max_lines=5`` emits six lines, five kept plus
    the marker line — and it is deliberate (#481): a marker made to fit inside
    the budget would have to be a bare ellipsis, which does not say that
    redaction happened. The line-count marker is never itself char-truncated.
    Composing both caps bounds the whole result at
    ``max_lines * (max_chars + len(char marker)) + len(line marker)``.

    Passing either cap normalizes line endings and drops a trailing newline
    (``splitlines``/``join``); passing neither is a byte-identical passthrough
    of the redaction step.
    """
    s = redact_home(s)
    s = _EMAIL_RE.sub(_REDACTED_EMAIL, s)
    if max_lines is None and max_chars is None:
        return s
    lines = s.splitlines()
    # Held aside, not appended yet: the line-count marker is a report of what the
    # line cap removed, so char-truncating it would redact the redaction notice.
    line_marker: list[str] = []
    if max_lines is not None and len(lines) > max_lines:
        dropped = len(lines) - max_lines
        line_marker = [f"… ({dropped} more lines redacted)"]
        lines = lines[:max_lines]
    if max_chars is not None:
        lines = [_truncate_line(line, max_chars) for line in lines]
    return "\n".join(lines + line_marker)


def _is_word_boundary(ch: str) -> bool:
    # a string edge ("") or any non-word char counts as a boundary; "word" = [A-Za-z0-9_]
    return ch == "" or not (ch.isalnum() or ch == "_")


def _iter_standalone(text: str, needle: str) -> Iterator[int]:
    """Yield start indices of standalone occurrences of an opaque needle: each is
    flanked by a string edge or a non-word char on both sides. Unlike re's ``\\b``
    on the needle's own edges, this still fires when the needle begins or ends
    with punctuation (e.g. ``.acme``). str.find, never regex — needles routinely
    carry regex metachars. Non-overlapping: a hit resumes scanning past the
    needle. Both detection and repair walk this one loop so their boundary
    semantics can never drift apart."""
    start = 0
    while (idx := text.find(needle, start)) >= 0:
        before = text[idx - 1] if idx else ""
        after = text[idx + len(needle)] if idx + len(needle) < len(text) else ""
        if _is_word_boundary(before) and _is_word_boundary(after):
            yield idx
            start = idx + len(needle)
        else:
            start = idx + 1


def _contains_standalone(text: str, needle: str) -> bool:
    """True when ``needle`` occurs standalone (word-boundary-flanked) in ``text``."""
    return next(_iter_standalone(text, needle), None) is not None


def replace_standalone(text: str, needle: str, replacement: str) -> tuple[str, int]:
    """Replace every standalone occurrence of ``needle``; return ``(text, count)``.

    Occurrences embedded in a longer word stay untouched — that containment is
    the detection side's deliberate false-positive exclusion (``proj`` inside
    ``project``), and this must mirror :func:`_contains_standalone` exactly.
    Scanning consumes past each replaced span, so the replacement is never
    itself rescanned and a replacement containing the needle still terminates."""
    parts: list[str] = []
    pos = 0
    count = 0
    for idx in _iter_standalone(text, needle):
        parts.append(text[pos:idx])
        parts.append(replacement)
        pos = idx + len(needle)
        count += 1
    if not count:
        return text, 0
    parts.append(text[pos:])
    return "".join(parts), count


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

    def entries(self) -> list[tuple[str, str, str]]:
        """``(ns, original, alias)`` triples in insertion order — feeds the
        labeled leak check and alias-substitution repair. Like :meth:`legend`,
        LOCAL ONLY: the originals must never be written into a dump."""
        return [(ns, value, alias) for (ns, value), alias in self._map.items()]


def embeds_current_username(s: str) -> bool:
    """True when the current username (≥5 chars — the :func:`assert_no_leak`
    hard rule's threshold) appears anywhere in ``s``. Collection-time callers
    redact such values pre-emptively — e.g. a kept-verbatim path component like
    ``pytest-of-alice`` — so the guard's username rule (standalone semantics,
    strictly narrower than this substring check) can never fire on them."""
    try:
        user = getpass.getuser()
    except Exception:
        # No passwd entry / no USER env (minimal containers): same degradation
        # as the assert_no_leak username rule.
        return False
    return len(user) >= 5 and user in s


def assert_no_leak(text: str, *, extra: Iterable[str | tuple[str, str]] = ()) -> list[str]:
    """Re-scan already-rendered output for anything that must not ship.

    The defense-in-depth backstop to the per-field routing: even if a handler is
    wrong or a new field is added, this catches an email, a credential-shaped
    token (same logic as :func:`looks_like_secret`), URL-embedded creds, an
    absolute home path, the current username, or any caller-supplied sensitive
    string (e.g. a project basename, or every :meth:`Pseudonymizer.legend` value)
    in the final bytes. Returns the list of rule names that fired — empty means
    clean. Callers fail closed (refuse to write) on a non-empty result.

    An ``extra`` item is either a bare sensitive value (fires as
    ``sensitive[<index>]``) or a ``(value, label)`` pair (fires as
    ``sensitive[<label>]``). The label is echoed verbatim into rule names and
    thence CLI output, so callers must NEVER put the sensitive value (or any
    part of it) in the label — diagnostics builds labels from ``ns:alias``
    only, which are safe to print by construction.
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
        if isinstance(item, tuple):
            value, label = str(item[0]), item[1]
        else:
            # Bare value: report the position only — never echo the value, since
            # this rule name is surfaced in the CLI failure message and would
            # otherwise leak it.
            value, label = str(item), str(i)
        # delimiter check so a short basename ("proj") can't false-positive on a
        # common word that contains it ("project"), yet a value whose own edge is
        # punctuation (".acme") is still caught — a blind spot of a \b regex.
        if len(value) >= 4 and _contains_standalone(text, value):
            fired.append(f"sensitive[{label}]")
    return fired


class LeakDetected(Exception):
    """The rendered report tripped :func:`assert_no_leak` — emission is refused.

    Raised only for hard rules (email/secret/home-path/url-creds/username) or a
    ``sensitive[*]`` repair that did not converge; a plain stray-original hit is
    repaired by alias substitution instead. ``rules`` carries the fired rule
    names — ``sensitive[<ns>:<alias>]`` for pseudonymizer originals, printable
    because the label never contains the original value."""

    def __init__(self, rules: list[str]):
        self.rules = rules
        super().__init__("rendered report tripped leak self-check: " + ", ".join(rules))


_MAX_REPAIR_PASSES = 3


def _repair_candidates(pseudo: Pseudonymizer | None) -> list[tuple[str, str, str]]:
    """``(original, alias, label)`` triples for the leak check and repair.

    Filtered to assert_no_leak's ≥4-char detection threshold (repair must never
    rewrite an occurrence detection would not fire on) and deduped by original
    (a value aliased under two namespaces gets one deterministic label — the
    first insertion's). Labels are ``ns:alias`` — safe to print by construction,
    never the original."""
    if pseudo is None:
        return []
    candidates: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for ns, original, alias in pseudo.entries():
        if len(original) >= 4 and original not in seen:
            seen.add(original)
            candidates.append((original, alias, f"{ns}:{alias}"))
    return candidates


def assert_clean(rendered: str, pseudo: Pseudonymizer | None = None) -> None:
    """One plain, no-repair re-check — run after a repair note is appended so
    the note itself sits inside the verified bytes."""
    extras = [(orig, label) for orig, _alias, label in _repair_candidates(pseudo)]
    fired = assert_no_leak(rendered, extra=extras)
    if fired:
        raise LeakDetected(fired)


def guard(
    rendered: str,
    pseudo: Pseudonymizer | None = None,
    *,
    max_passes: int = _MAX_REPAIR_PASSES,
) -> tuple[str, list[tuple[str, int]]]:
    """Verify the rendered bytes; repair stray pseudonymized originals; fail closed.

    When the pseudonymizer is supplied, its legend's original values (the real
    story keys/branches/project names) are fed into the self-check too, so any
    that slipped through the per-field routing are caught here in the final
    bytes. Unlike a hard-rule hit, such a miss is repairable: the backstop knows
    the original's safe alias, so it substitutes it and re-verifies instead of
    refusing outright. Returns ``(text, [(label, count), ...])`` of applied
    repairs. Raises :class:`LeakDetected` on any hard rule (email / secret /
    home-path / url-creds / username — genuine PII never auto-repairs) or if
    repair does not converge within the pass bound."""
    candidates = _repair_candidates(pseudo)
    extras = [(orig, label) for orig, _alias, label in candidates]
    # Longest-first: a branch embedding a story slug at a "-" boundary must be
    # replaced whole, not spliced into a half-alias mongrel by the inner slug.
    by_length = sorted(candidates, key=lambda c: len(c[0]), reverse=True)

    tally: dict[str, int] = {}
    for _ in range(max_passes):
        fired = assert_no_leak(rendered, extra=extras)
        if not fired:
            return rendered, sorted(tally.items())
        if any(not rule.startswith("sensitive[") for rule in fired):
            raise LeakDetected(fired)
        # A repair pass replaces every standalone occurrence of every candidate,
        # so pass 2 is reachable only if a substitution manufactured a NEW
        # standalone occurrence of a different original — a hash-output
        # coincidence (the alias alphabet is [A-Za-z0-9-] and "-" is itself a
        # boundary char). The bound turns a pathological substitution cycle
        # into a fail-closed refusal instead of a loop.
        for original, alias, label in by_length:
            rendered, n = replace_standalone(rendered, original, alias)
            if n:
                tally[label] = tally.get(label, 0) + n
    fired = assert_no_leak(rendered, extra=extras)
    if fired:
        raise LeakDetected(fired)
    return rendered, sorted(tally.items())
