"""The shipped profiles' pane patterns, held to both halves of the contract.

"Pane patterns" are the three fields matched against what a session's log or
visible pane says: env_fault_patterns (a provider outage) and the #610 pair
usage_limit_patterns / usage_wait_patterns (a subscription limit the CLI is
blocked behind, or waiting out). The usage pair is matched against the live pane
AND, as ordinary env-fault families, against the post-mortem log tail, so it
answers to exactly the same corpus as env_fault_patterns. parked_prompt_patterns
is not held here (tests/test_profile.py pins it).

A pattern that misses a real provider outage burns a story's retry budget on an
environment fault (the bug that motivated this file). A pattern that fires on
ordinary model output pauses a *healthy* run — the inverse bug, and the worse of
the two, because the miss merely reproduces today's behaviour while the false
positive halts work that was fine. Both halves are cheap to assert and expensive
to rediscover, so the corpora below are the gate: add a line here before
loosening a pattern.

Only two profiles seed patterns, and only because captured output exists for
them: opencode seeds env_fault_patterns, claude seeds all three fields
(SEEDED_PANE_FIELDS). The other four stay inert deliberately — see
test_unseeded_profiles_stay_inert.

WHAT BAIT IS FOR. The tmux adapters match against a tmux pane capture, which
contains the MODEL'S OWN OUTPUT: a story about quota handling prints this
vocabulary constantly, in fixtures, banners, acceptance criteria, docs and
assertions. Every BAIT line is something a healthy session plausibly emits.
The opencode adapter is structurally different — the file it scans is
`<task_id>.server.out`, the `opencode serve` process's own stdout/stderr, which
the model cannot write to at all. (NOT `<task_id>.log`, which is that adapter's
curated `[bmad]` conversation transcript and does carry the model's words.) It is
held to the same corpus anyway, because a pattern loose enough to be wrong on a
pane log should not be trusted just because today's log happens to be clean —
and because which file is scanned has already changed once underneath these
patterns.

KNOWN LIMIT, so nobody mistakes a green run here for proof. This gate matches
line-by-line against text, whereas the real pane log is a raw `tmux pipe-pane`
byte stream carrying cursor-addressed repaints (the TUI renders the same file
through pyte — see tui/data.py). Line and column position in that stream do not
correspond to what was on screen. So patterns must not depend on POSITION,
only on content; a positional anchor would pass here and be meaningless in
production.
"""

from __future__ import annotations

import subprocess
from importlib import resources
from pathlib import Path

import pytest
import regex

from bmad_loop.adapters.opencode_protocol import format_provider_error
from bmad_loop.adapters.profile import get_profile

# The profiles whose patterns are backed by captured output from a real outage.
# They are safe for DIFFERENT reasons, and both reasons are load-bearing:
# `opencode` because the file it scans (`<task_id>.server.out`) is the serve
# process's own stdout, which the model cannot write to at all; `claude` because
# its log IS model-written (a tmux pane capture) and each of its patterns instead
# reproduces the whole first sentence of a captured CLI error (a prefix match),
# which a paraphrase cannot reach.
# Both are held to the full BAIT corpus.
SEEDED_PROFILES = ("opencode", "claude")
# The seeded profiles whose scanned log is a tmux PANE CAPTURE — bytes the model
# itself wrote. Their patterns are the ones that have to survive contact with
# ordinary prose, so the two repo-scanning guards below are scoped to them.
# opencode is deliberately out of scope there: it scans `<task_id>.server.out`,
# the serve process's own stdout, which the model cannot write to.
PANE_CAPTURE_PROFILES = ("claude",)
# Profiles that deliberately seed NOTHING, so classification stays inert for them.
# Patterns were drafted for these and withdrawn: they could only be written from
# error strings scraped off public issue trackers, for CLIs nobody here has run.
# An unverified pattern is not a neutral bet — one that fires on a healthy session
# pauses the whole run, which is worse than the fault it was meant to catch.
# Seeding nothing costs only the status quo. See test_unseeded_profiles_stay_inert.
UNSEEDED_PROFILES = ("codex", "gemini", "copilot", "antigravity")
ALL_PROFILES = SEEDED_PROFILES + UNSEEDED_PROFILES
# The pane pattern fields this module holds to the corpus. Every BAIT-style
# assertion below is made against the UNION of them: a usage-limit hit on a
# healthy session parks the run exactly as an env-fault hit pauses it.
PANE_PATTERN_FIELDS = ("env_fault_patterns", "usage_limit_patterns", "usage_wait_patterns")
# Exactly the (profile, field) pairs that ship patterns, each backed by captured
# output in REAL_BY_PROFILE. Listed rather than discovered, so withdrawing a seed
# reddens test_pane_fields_are_seeded_exactly_where_listed instead of silently
# shrinking every parametrization keyed on this.
SEEDED_PANE_FIELDS = (
    ("opencode", "env_fault_patterns"),
    ("claude", "env_fault_patterns"),
    ("claude", "usage_limit_patterns"),
    ("claude", "usage_wait_patterns"),
)

# The two logfmt shapes `opencode serve` emitted during a real 5-hour provider
# quota outage on the opencode-http adapter (zai-coding-plan/glm-5.2). The line
# SHAPE and the AI_APICallError text are reproduced exactly — those are what the
# patterns key on — but the correlation ids are FAKES: the run this was captured
# from was a private client project, and a real `run=`/`session.id=` pair is a
# usable handle on it. Keep them fake; ids carry no test signal.
OPENCODE_REAL = [
    'timestamp=2026-07-26T13:12:53.262Z level=ERROR run=fake0001 message="stream error" '
    "providerID=zai-coding-plan modelID=glm-5.2 session.id=ses_fake0000000000000000000001 "
    'small=false agent=general mode=subagent error.error="AI_APICallError: Usage limit '
    'reached for 5 hour. Your limit will reset at 2026-07-26 22:49:46"',
    'timestamp=2026-07-26T22:52:57.193Z level=ERROR run=fake0002 message="stream error" '
    "providerID=zai-coding-plan modelID=glm-5.2 session.id=ses_fake0000000000000000000002 "
    'small=false agent=build mode=primary error.error="AI_APICallError: Cannot connect to '
    'API: The socket connection was closed unexpectedly."',
    *(
        # OpenCode 2.x: the `provider-errors.log` lines the adapter writes
        # (`format_provider_error`) for frames captured live from 2.0.27
        # (2026-10-12, zero tokens, a local mock provider answering a 429 rate
        # limit, a 429 and a 402 `insufficient_quota`, and a closed port). Every
        # byte is reproduced — test_v2_corpus_lines_are_what_the_adapter_writes
        # rebuilds them from those frames' payloads.
        "provider-error event=session.retry.scheduled type=provider.rate-limit status=429 "
        'attempt=2 message="Rate limit reached for requests"',
        "provider-error event=session.step.failed type=provider.rate-limit status=429 "
        'attempt=- message="Rate limit reached for requests"',
        "provider-error event=session.execution.failed type=provider.quota status=429 "
        'attempt=- message="You exceeded your current quota, please check your plan and '
        'billing details."',
        "provider-error event=session.step.failed type=provider.quota status=402 "
        'attempt=- message="You exceeded your current quota, please check your plan and '
        'billing details."',
        "provider-error event=session.retry.scheduled type=provider.transport status=- "
        'attempt=2 message="ConnectionRefused: Unable to connect. Is the computer able to '
        'access the url?"',
        "provider-error event=session.execution.failed type=provider.transport status=- "
        'attempt=- message="ConnectionRefused: Unable to connect. Is the computer able to '
        'access the url?"',
    ),
]

# The captured 2.x frames behind the provider-errors lines above: `(event,
# data)` with the `data` payload as the server sent it (ids shortened; they carry
# no test signal), `response.body` included — the adapter must drop it.
_RATE_LIMIT_ERROR = {
    "type": "provider.rate-limit",
    "message": "Rate limit reached for requests",
    "status": 429,
    "response": {
        "body": '{"error": {"message": "Rate limit reached for requests", "type": "requests", '
        '"param": null, "code": "rate_limit_exceeded"}}'
    },
}
_QUOTA_MESSAGE = "You exceeded your current quota, please check your plan and billing details."
_QUOTA_BODY = (
    '{"error": {"message": "You exceeded your current quota, please check your plan and '
    'billing details.", "type": "insufficient_quota", "param": null, '
    '"code": "insufficient_quota"}}'
)
_TRANSPORT_ERROR = {
    "type": "provider.transport",
    "message": "ConnectionRefused: Unable to connect. Is the computer able to access the url?",
}
OPENCODE_V2_CAPTURED_FRAMES = [
    (
        "session.retry.scheduled",
        {
            "sessionID": "ses_1",
            "assistantMessageID": "msg_1",
            "attempt": 2,
            "at": 1791830376429,
            "error": _RATE_LIMIT_ERROR,
        },
    ),
    (
        "session.step.failed",
        {
            "sessionID": "ses_1",
            "assistantMessageID": "msg_1",
            "error": _RATE_LIMIT_ERROR,
            "snapshot": "34371aaf",
            "files": [],
        },
    ),
    (
        "session.execution.failed",
        {
            "sessionID": "ses_2",
            "error": {
                "type": "provider.quota",
                "message": _QUOTA_MESSAGE,
                "status": 429,
                "response": {"body": _QUOTA_BODY},
            },
        },
    ),
    (
        "session.step.failed",
        {
            "sessionID": "ses_3",
            "assistantMessageID": "msg_3",
            "error": {
                "type": "provider.quota",
                "message": _QUOTA_MESSAGE,
                "status": 402,
                "response": {"body": _QUOTA_BODY},
            },
            "snapshot": "34371aaf",
            "files": [],
        },
    ),
    (
        "session.retry.scheduled",
        {
            "sessionID": "ses_4",
            "assistantMessageID": "msg_4",
            "attempt": 2,
            "at": 1791830566207,
            "error": _TRANSPORT_ERROR,
        },
    ),
    ("session.execution.failed", {"sessionID": "ses_4", "error": _TRANSPORT_ERROR}),
]
OPENCODE_V2_REAL = [line for line in OPENCODE_REAL if line.startswith("provider-error ")]

# Claude Code surfaces provider failures behind its own "API Error" prefix, and
# claude.toml reproduces each message's first sentence WHOLE (as a prefix match —
# the rest of the message is not required) rather than pairing the prefix with a
# loose cause — that completeness is the only thing separating an
# emitted line from a story writing about one, because this profile's log is a
# pane capture of the model's own output (#507).
#
# Provenance, since a pattern here may only be seeded from a captured line: the
# first two were captured in issue #194, run `20260718-191419-44e7`, on the
# `claude` adapter. The last three come from Claude Code's own JSONL transcripts,
# from entries structurally tagged `"isApiErrorMessage": true`.
#
# The quota class is still ABSENT from env_fault_patterns, and deliberately so:
# no captured Claude Code provider-side quota line exists — not in this repo, not
# in #194/#323/#507, not in the local transcripts. Seeding one needs a captured
# line with its run cited, the bar test_unseeded_profiles_stay_inert states; a
# plausible-looking string is exactly what that bar refuses. The SUBSCRIPTION
# usage limits Claude Code prints on its own screen were captured (#610) and live
# in their own fields — see CLAUDE_USAGE_REAL.
CLAUDE_REAL = [
    "API Error: Unable to connect to API (ConnectionRefused)",
    "API Error: Unable to connect to API: Self-signed certificate detected. "
    "Please check your network settings.",
    "API Error: Connection closed mid-response. The response above may be incomplete.",
    "API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again "
    "in a moment. If it persists, check https://status.claude.com.",
    "API Error: 500 Internal server error. This is a server-side issue, usually temporary — "
    "try again in a moment. If it persists, check https://status.claude.com.",
]

# Claude Code's subscription usage-limit lines (#610), as its pane prints them —
# behind the `⎿` gutter glyph, with one or two spaces after it depending on the
# terminal. Keyed by the field each must be caught by, because the two are
# disjoint by contract: usage_limit_patterns is BLOCKED (credit exhaustion, an
# operator must act), usage_wait_patterns is WAITING for a reset the CLI then
# continues from by itself.
#
# Provenance, verbatim from #610:
# * credits: psmux/Windows 11, bmad-loop 0.11.1, runs 20260907-084618-824f
#   (dev-1 and review-1) and 20260907-090132-98e3 (dev-1 and dev-2) —
#   `⎿ You're out of usage credits. Run /usage-credits to keep using Fable 5.1 or
#   /model to switch models.`
# * session limit: the 2026-09-07 psmux capture
#   `⎿ You've hit your session limit · resets 12:50pm (Asia/Ho_Chi_Minh)`, and
#   macOS/tmux run 20260913-165153-b850 `⎿  You've hit your session limit ·
#   resets 7:20pm` (two spaces after the glyph, no zone).
# The gutter-prefix and zone variants are the same captured sentences under the
# other observed spacing, and with the optional zone on or off.
_CLAUDE_CREDITS_SENTENCES = (
    "You're out of usage credits. Run /usage-credits to keep using Fable 5.1 or "
    "/model to switch models.",
)
_CLAUDE_SESSION_LIMIT_SENTENCES = (
    "You've hit your session limit · resets 12:50pm (Asia/Ho_Chi_Minh)",
    "You've hit your session limit · resets 12:50pm",
    "You've hit your session limit · resets 7:20pm",
    "You've hit your session limit · resets 7:20pm (Asia/Ho_Chi_Minh)",
)
_CLAUDE_PANE_GUTTERS = ("⎿ ", "⎿  ", "")
CLAUDE_USAGE_REAL = {
    field: [gutter + sentence for sentence in sentences for gutter in _CLAUDE_PANE_GUTTERS]
    for field, sentences in (
        ("usage_limit_patterns", _CLAUDE_CREDITS_SENTENCES),
        ("usage_wait_patterns", _CLAUDE_SESSION_LIMIT_SENTENCES),
    )
}

# Every seeded (profile, field), paired with the lines its own CLI actually emits.
REAL_BY_PROFILE = [
    (name, field, line)
    for name, field, corpus in (
        ("opencode", "env_fault_patterns", OPENCODE_REAL),
        ("claude", "env_fault_patterns", CLAUDE_REAL),
        *(("claude", field, lines) for field, lines in CLAUDE_USAGE_REAL.items()),
    )
    for line in corpus
]

# Bait that actually REACHES a seeded profile's anchor. Kept separate because it
# is the half that is easiest to get wrong: a corpus can be large and still prove
# nothing if every line bounces off the anchor before the rest of the pattern is
# ever exercised. This list did exactly that once — it was written while claude
# was still seeded, and after claude was withdrawn not one line could reach the
# only remaining anchor, so the test guarded nothing while looking thorough.
#
# So the bar for a line here is mechanical, and test_anchor_reaching_bait_is_not_
# vacuous enforces it: every seeded (profile, field) must have at least one line
# carrying each of its literal anchors. Lines below reproduce
# `error.error="AI_APICallError: ` (the opencode anchor) inside ordinary
# healthy-session output.
ANCHOR_REACHING_BAIT = [
    # The cause OUTSIDE the quoted error value: a real but UNRELATED provider
    # error whose trailing logfmt fields merely happen to contain a cause word.
    # These are why the gap in the pattern is [^"] and not `.` — with `.` the lazy
    # gap walks past the closing quote and finds "quota" in a later field, so an
    # invalid-API-key error reads as a quota outage.
    'timestamp=2026-07-26T13:12:53.262Z level=ERROR message="stream error" '
    'error.error="AI_APICallError: Invalid API key" request.path=/v1/quota',
    'timestamp=2026-07-26T13:12:53.262Z level=ERROR message="stream error" '
    'error.error="AI_APICallError: Model not found" endpoint=https://example/rate-limits',
    'error.error="AI_APICallError: Model not found" note=see-the-rate-limit-docs',
    # The anchor named but not completed — prose about the field, not the field.
    "docs: the server writes error.error= with an AI_APICallError when the quota trips",
    # The 2.x provider-errors line, reaching `^provider-error event=`. Real
    # adapter output for faults that are NOT an outage — the step an interrupt
    # cancels, a rejected permission, an unseeded provider type — with the cause
    # words only inside the message, where the pattern never looks.
    'provider-error event=session.step.failed type=aborted status=- attempt=- message="Step interrupted"',
    "provider-error event=session.execution.failed type=permission.rejected status=- "
    'attempt=- message="provider.quota rate-limit"',
    "provider-error event=session.retry.scheduled type=provider.auth status=401 attempt=2 "
    'message="quota exceeded; rate-limit; transport down"',
    "provider-error event=session.execution.failed type=provider.no-route status=- "
    'attempt=- message="Variant unavailable for mock/mock-1: rate-limit"',
    # A type that only STARTS like a seeded one (the pattern needs the whole
    # token), and the sanitized remains of a hostile `type` value.
    "provider-error event=session.execution.failed type=provider.quota-exceeded status=- "
    'attempt=- message=""',
    "provider-error event=session.execution.failed type=provider.unknown_type_provider.quota "
    'status=- attempt=- message=""',
    # A message that forges a whole line: escaped, so it stays mid-line.
    "provider-error event=session.execution.failed type=provider.error status=500 "
    'attempt=- message="x\\nprovider-error event=session.retry.scheduled '
    'type=provider.quota status=429 attempt=2 message=\\"y\\""',
    # The line quoted rather than written — indented, or behind prose — as the
    # model-written transcript or a story's own docs would carry it.
    "  provider-error event=session.retry.scheduled type=provider.rate-limit status=429 "
    'attempt=2 message="Rate limit reached for requests"',
    "docs: the adapter writes provider-error event=session.retry.scheduled "
    "type=provider.transport status=- attempt=2 per retry",
    # claude's framing. These were written for the day a claude pattern was
    # seeded again — that day is now, so they are live assertions against the
    # shipped profile rather than a corpus held in reserve.
    '// TODO: surface "API Error: 429" to the user with a Retry-After hint',
    'expect(banner).toBe("API Error: 429 rate_limit_error")',
    "docs: document the API Error 429 quota path per AC-3",
    '  it("renders API Error: overloaded", () => {',
    "- [ ] AC-2: map API Error: 429 to a friendly rate limit message",
    # The five lines below WERE the strict-xfail corpus of
    # test_claude_pattern_is_known_to_false_positive: prose that the withdrawn
    # `API Error.*<cause>` pattern matched, because an unbounded gap between the
    # framing token and the cause makes any line MENTIONING both a hit (#507).
    # They are the reason this test bites for claude at all — the shipped
    # patterns matched none of BAIT and none of the nine lines above even
    # BEFORE the fix, so promoting claude into SEEDED_PROFILES proves nothing on
    # its own. Do not "simplify" them back out of the corpus: they are the
    # paraphrases the first-sentence patterns exist to reject.
    '  assert log == "API Error: Connection refused"',
    'docs: explain the "API Error: Unable to connect" retry path',
    "- [ ] AC-5: show a banner on API Error: Connection timed out",
    'expect(msg).toBe("API Error: Connection error")',
    "// handle API Error: ECONNREFUSED by retrying with backoff",
    # The 5xx half of the same rule, and the one place a whole first sentence is
    # still not enough on its own: the status is a field, so ranging over it
    # (`5[0-9][0-9]`) re-admits the paraphrase the sentence was meant to exclude.
    # Only two pairings were ever captured (`529 Overloaded`, `500 Internal
    # server error`); a range accepted 100 codes against either reason phrase,
    # so all four lines below classified — including the two that CROSS-PAIR the
    # captured halves into a sentence Claude Code has never printed. Writing an
    # HTTP error path is ordinary story work, so each is a live pane-capture
    # line, and a hit halts the run for an operator exactly as #507 describes.
    'expect(banner).toBe("API Error: 503 Internal server error")',
    '  assert log == "API Error: 502 Overloaded"',
    "- [ ] AC-6: retry on API Error: 500 Overloaded and surface a banner",
    'it("renders API Error: 529 Internal server error", () => {',
    # claude's usage-limit sentences (#610), each anchor carried but the sentence
    # left unfinished — the opening words a story about a credits banner or a
    # session-limit toast writes all day. The patterns run to `credits. Run
    # /usage-credits` and `limit · resets`, so none of these reach a match. (The
    # `You've used <N>% of your session limit` advisory never reaches the wait
    # anchor at all — it is in BAIT.)
    "You're out of usage credits!",
    "You're out of usage credits.",
    '  assert banner.text == "You\'re out of usage credits. Upgrade to keep going."',
    "- [ ] AC-3: when You're out of usage credits, link to /usage-credits in the toast",
    "docs: You're out of usage credits. Run the billing page to top up",
    "You've hit your session limit, so the banner says to wait for the reset.",
    "You've hit your session limit — resets at 7:20pm (em dash, not the CLI's separator)",
    'expect(toast).toBe("You\'ve hit your session limit")',
    "- [ ] AC-4: You've hit your session limit · show the reset time in the header",
]

# The limit of content-based matching, pinned rather than papered over.
#
# A story that quotes a provider's error VERBATIM — the whole framing token and a
# real cause, inside the quotes — produces a line byte-identical to the emitted
# one. No content-based pattern can separate those, because there is nothing left
# to separate them BY. It is a property of the approach, not a gap in these
# patterns, and it is the reason four profiles ship nothing at all: their real
# refusals are bare sentences, so every citation of them is indistinguishable.
#
# It is acceptable for opencode ONLY because of where that profile's log comes
# from: the file the classifier scans is logs/<task_id>.server.out — the
# `opencode serve` process's own stdout/stderr, which the model cannot write to,
# so a citation cannot physically reach the scanned bytes. NOT logs/<task_id>.log,
# which is that adapter's curated `[bmad]` transcript and does carry the model's
# words (the file is chosen by EnvFaultMixin.ENV_FAULT_LOG_SUFFIX). If the
# classifier is ever repointed at the transcript, or the adapter tees model output
# into the server log, these lines become live false positives and the patterns
# must be withdrawn.
#
# Characterisation test — asserts they DO match, so the day that changes it shows.
INSEPARABLE_VERBATIM_CITATIONS = [
    'docs: the server logs error.error="AI_APICallError: Usage limit reached" on quota exhaustion',
    "  assert line == 'error.error=\"AI_APICallError: Usage limit reached for 5 hour\"'",
    '# fixture: error.error="AI_APICallError: rate limit" — see tests/fixtures/quota.log',
    "expect(parse(line)).toEqual({error: 'error.error=\"AI_APICallError: quota exceeded\"'})",
]

# The literal each seeded profile's patterns key on, DERIVED from the live
# patterns rather than hand-copied (DW-373): a hand copy only proved the corpus
# below reached the COPY, and nothing noticed when a pattern edit moved the anchor
# away from it. Used to prove the corpus actually reaches the anchor, rather than
# trusting that it does. `test_anchor_literal_is_the_live_patterns_anchor` guards
# the derivation itself against going vacuous (an empty anchor is `in` every line).
_REGEX_METACHARS = frozenset(".^$*+?{}[]()|\\")
_QUANTIFIERS = frozenset("?*+{")
_SINGLE_CHAR_CLASS = regex.compile(r"\[([^\\\]^])\]")
MIN_ANCHOR_LEN = 8


def _leading_literal(pattern: str) -> list[tuple[str, str]]:
    """The (raw spelling, literal char) tokens a pattern opens with: plain chars, a
    single-character class `[x]`, and a backslash-escaped punctuation char each
    read as that literal char; anything else ends the run. A token followed by a
    quantifier is optional (or repeated), so it and everything after it are not a
    guaranteed prefix — the run ends before it."""
    tokens: list[tuple[str, str]] = []
    i = 0
    if pattern.startswith("^"):
        # A line-start anchor opens the run as a zero-width token: it adds
        # position, never a literal character.
        tokens.append(("^", ""))
        i = 1
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern) and not pattern[i + 1].isalnum():
            raw, lit = pattern[i : i + 2], pattern[i + 1]
        elif c == "[":
            m = _SINGLE_CHAR_CLASS.match(pattern, i)
            if m is None:
                break
            raw, lit = m.group(0), m.group(1)
        elif c in _REGEX_METACHARS:
            break
        else:
            raw, lit = c, c
        i += len(raw)
        if i < len(pattern) and pattern[i] in _QUANTIFIERS:
            break
        tokens.append((raw, lit))
    return tokens


def _anchor_literal(pattern: str) -> str:
    """One pattern's contribution to its anchor family: its leading literal run —
    cut before the first single-character class when that run is the WHOLE
    pattern. A wholly literal pattern (the #610 usage sentences) has nothing past
    its leading literal, so an anchor spanning all of it could only be reached by
    a line that matches; the first class is where the profile's own source line
    stops spelling the sentence, and so where the rest of the pattern begins to
    discriminate."""
    tokens = _leading_literal(pattern)
    if "".join(raw for raw, _ in tokens) == pattern:
        cut = next((i for i, (raw, _) in enumerate(tokens) if raw.startswith("[")), len(tokens))
        tokens = tokens[:cut]
    return "".join(lit for _, lit in tokens)


def _derive_anchors(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """Each anchor family's literal: the patterns' anchor literals (see
    `_anchor_literal`), grouped by a shared prefix of at least MIN_ANCHOR_LEN
    characters, each group reduced to its longest common prefix. One field can
    key on more than one framing (opencode: the 1.x server log's `error.error=`
    field and the 2.x provider-errors line), and every family has to be reached
    by the corpus."""
    anchors: list[str] = []
    for literal in (_anchor_literal(p) for p in patterns):
        for i, anchor in enumerate(anchors):
            n = 0
            while n < min(len(anchor), len(literal)) and anchor[n] == literal[n]:
                n += 1
            if n >= MIN_ANCHOR_LEN:
                anchors[i] = anchor[:n]
                break
        else:
            anchors.append(literal)
    return tuple(anchors)


def _leading_spelling(pattern: str, anchor: str) -> str | None:
    """The raw regex spelling of ``pattern``'s leading tokens whose literal
    characters spell ``anchor``, or None when its leading literal does not."""
    spelling, literal = "", ""
    for raw, lit in _leading_literal(pattern):
        if len(literal) >= len(anchor):
            break
        spelling, literal = spelling + raw, literal + lit
    return spelling if literal == anchor else None


PROFILE_ANCHOR_LITERALS = {
    (name, field): _derive_anchors(getattr(get_profile(name), field))
    for name, field in SEEDED_PANE_FIELDS
}

# Ordinary output from a healthy session working on a story that involves rate
# limiting. Every one of these is reachable in a tmux pane capture. None may
# classify — a hit here means the loop pauses a run that was working fine.
BAIT = [
    "  def test_rate_limit_retries_with_backoff(self):  # story 4 AC-2",
    "PASS  tests/rate_limiter.test.ts (12 tests)",
    "Implementing the quota exceeded banner per AC-3.",
    "wrote src/middleware/rate_limit.py",
    "- [x] AC-4: requests over quota return 429 with a Retry-After header",
    "I'll add a 429 handler that surfaces a friendly message to the user.",
    "429 lines changed across 12 files",
    "grep -c 'rate limit' logs/app.log  # 429",
    "throw new Error('Rate limit exceeded');  // added in src/api/client.ts",
    "docs: explain the quota model and the 429 response contract",
    "INFO cleanup prune=7.days",
    "timestamp=2026-07-26T15:45:39.732Z level=INFO message=stream providerID=zai-coding-plan",
    # A story's own passing test whose NAME mentions the very error class the
    # anchors key on — the nastiest realistic collision.
    "PASS tests/test_api_error_handling.py::test_rate_limit_error_is_retried",
    # …and the same collision on a FAILING test, which puts an error-shaped word
    # ahead of the cause on the line. A pattern anchored on a bare "error|failed"
    # rather than on a CLI-specific token trips on exactly this.
    "FAILED tests/test_retry.py::test_econnrefused_is_retried",
    "FAILED tests/network.spec.ts > retries on ETIMEDOUT",
    "  ✗ Error: handles RESOURCE_EXHAUSTED from the quota middleware",
    "AssertionError: expected 429, got 200 — see src/quota/limiter.ts",
    # ---------------------------------------------------------------------
    # THE CLASS THAT WAS MISSED: the model CITING the provider's error, verbatim.
    #
    # Everything above is a story *paraphrasing* rate limiting, so a
    # CLI-specific token was enough to separate it. These lines instead contain
    # a CLI's exact error string — because the story's job IS that error:
    # fixture literals, golden JSON, assertion text, test names, banner copy,
    # ACs, doc bullets, commit subjects, diff hunks, stack frames.
    #
    # This class is why four profiles ship NO patterns. Their real refusals are
    # bare provider sentences ("Resource has been exhausted (e.g. check quota).")
    # with no framing to anchor on, so no amount of vocabulary separates the
    # emitted line from the quoted one. A positional scheme was tried — anchor on
    # the CLI "owning" column 0 — and abandoned as unsound: the pane log is a raw
    # pipe-pane byte stream, so position in it is not position on screen (see this
    # module's header). Lines below are retained as the standing bar any future
    # pattern for those CLIs must clear before it is seeded.
    'ERROR in test setup: expected "code": "insufficient_quota"',
    'assert msg == "Resource has been exhausted (e.g. check quota)."',
    '"You have exhausted your daily quota on this model." // banner copy, AC-2',
    'it("renders TerminalQuotaError", () => {',
    'docs: quota copy - "You exceeded your current quota, please check your plan..."',
    'expected = {"code": "quota_exceeded"}',
    '  mock.return_value = {"code":"rate_limited"}',
    'expected = {"error": {"code": 429, "message": "Resource has been exhausted '
    '(e.g. check quota).", "status": "RESOURCE_EXHAUSTED"}}',
    '  Received: {"error":{"code":429,"message":"Resource has been exhausted '
    '(e.g. check quota).","status":"RESOURCE_EXHAUSTED"}}',
    '+   {"error": {"code": 429, "message": "Resource has been exhausted '
    '(e.g. check quota).", "status": "RESOURCE_EXHAUSTED"}}',
    '  "status": "RESOURCE_EXHAUSTED",',
    'fixtures/gemini_429.json: {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}',
    "class TerminalQuotaError extends Error { constructor() { super('quota exhausted'); } }",
    "export { TerminalQuotaError, GaxiosError } from './errors';",
    "fix(quota): raise TerminalQuotaError: quota exhausted when the token bucket empties",
    "  47 |   throw new TerminalQuotaError('quota exhausted');",
    '# The upstream copy is "You have exhausted your daily quota on this model."',
    'TODO: handle "Quota exceeded for metric: generativelanguage.googleapis.com/'
    'generate_content_free_tier_requests"',
    "docs(errors): document Resource has been exhausted (e.g. check quota). handling",
    "* Copy per AC-6: You have exhausted your daily quota on this model.",
    '#   ERROR codex_api::endpoint::responses: error=http 429 Too Many Requests: Some("{',
    '    "ERROR: exceeded retry limit, last status: 429 Too Many Requests, '
    'request id: 9bd33f31fd269bb1-HNL",',
    '    "You have exhausted your daily quota on this model.",',
    '+  "Sorry, you\'ve hit a rate limit that restricts the number of Copilot model requests "',
    "2026-07-27T11:02:03.114Z ERROR app::api::client: error=http 429 Too Many Requests",
    "2026-07-27T11:02:03.114Z ERROR codex_client_stub: error=http 429 Too Many Requests",
    "Error sending request for url (https://api.openai.com/v1/chat/completions): "
    "error trying to connect: ECONNREFUSED",
    "Error sending request for url (http://localhost:8080/v1/responses): "
    "error trying to connect: ECONNREFUSED",
    "GaxiosError: request to https://sheets.googleapis.com/v4/spreadsheets failed, "
    "reason: connect ECONNREFUSED 127.0.0.1:443",
    "✗ Model call failed: surfaces quota_exceeded as a 402 (12 ms)",
    "  ✓ Model call failed: retries rate_limited twice then gives up",
    "│ Sorry, you've hit a rate limit — that's the banner string we render, per AC-2.",
    '  assert body["message"] == "Sorry, you have exceeded your Copilot token usage."',
    '- Model call failed: {"message":"You have no quota","code":"quota_exceeded"}  '
    "<- tests/fixtures/copilot_402.json",
    'it.each([["rate_limited", 429], ["quota_exceeded", 402]])("maps %s to %d", (code, status) => {',
    '  File "src/retry.py", line 41, in _raise_exhausted: '
    'RuntimeError("exceeded retry limit, last status: 429 Too Many Requests")',
    'raise UpstreamError("exceeded retry limit, last status: 503 Service Unavailable")',
    "> exceeded retry limit, last status: 429 Too Many Requests   (expected output, docs/retry.md)",
    '+    RESOURCE_EXHAUSTED_COPY = "Resource has been exhausted (e.g. check quota)."',
    '-        code: "rate_limit_exceeded",',
    'FIXTURE = {"error": {"code": 429, "message": "Resource has been exhausted '
    '(e.g. check quota).", "status": "RESOURCE_EXHAUSTED"}}',
    'expect(body).toEqual({"error":{"code":429,"message":"Resource has been exhausted '
    '(e.g. check quota).","status":"RESOURCE_EXHAUSTED"}})',
    'assert_eq!(err.to_string(), "exceeded retry limit, last status: 429 Too Many Requests");',
    'assert res.json()["error"]["status"] == "RESOURCE_EXHAUSTED"  # 429 path',
    'it("gives up once it has exceeded retry limit, last status: 503", async () => {',
    "  1) rate limiter › Model call failed: rate_limited (expected, retries once)",
    "stdout | src/quota.test.ts > Model call failed: quota_exceeded",
    '✗ should render "Quota exceeded for metric: generativelanguage.googleapis.com/x" in the toast',
    "  ✓ TerminalQuotaError is raised after the third 429 (18ms)",
    "# You have exhausted your daily quota on this model.  <- exact upstream copy, do not reword",
    "* You exceeded your current quota, please check your plan and billing details.",
    "> Resource has been exhausted (e.g. check quota).",
    "  GaxiosError: request to https://generativelanguage.googleapis.com/v1beta/models/x "
    "failed, reason: connect ECONNREFUSED  # expected in test_offline",
    "  ERROR codex_api::endpoint::responses: error=http 429 Too Many Requests   "
    "# pasted from openai/codex#9135",
    "Quota exceeded for metric: {metric}, limit: {limit}",
    'You have exhausted your daily quota on this model. Retry at {reset_at}."',
    "TerminalQuotaError = class extends Error {}",
    "2026-07-27T04:11:02.001Z ERROR quota_api::endpoint::responses: "
    "error=http 429 Too Many Requests",
    "2026-07-27T04:12:00.113Z ERROR app_api::responses: error=http 503 Service Unavailable",
    '[ERROR] billing: Model call failed shim returned {"code":"rate_limited"} (mocked)',
    "feat(retry): give up after exceeded retry limit, last status: 429",
    "fix(api): map RESOURCE_EXHAUSTED to a TerminalQuotaError subclass",
    "chore(copy): use \"Sorry, you've hit a rate limit that restricts the number of "
    'Copilot model requests" verbatim',
    "printf 'ERROR: exceeded retry limit, last status: %d\\n' \"$status\"",
    'console.log("Model call failed: " + JSON.stringify({ code: "rate_limited" }))',
    'throw new GaxiosError("connect ETIMEDOUT", { code: "ETIMEDOUT" });',
    '> Model call failed: {"message":"You have no quota","code":"quota_exceeded"}',
    'banner_text = "Sorry, you have exceeded your Copilot token usage. '
    'Please review our Terms of Service."',
    "- [ ] AC-7: surface the \"Sorry, you've hit a rate limit that restricts the number "
    'of Copilot model requests" toast verbatim',
    'RESPONSE_FIXTURE = {"error": {"code": 429, "message": "Resource has been exhausted '
    '(e.g. check quota).", "status": "RESOURCE_EXHAUSTED"}}',
    "expect(err).toBeInstanceOf(GaxiosError);  // ECONNREFUSED path, AC-5",
    'test("GaxiosError: request to https://generativelanguage.googleapis.com/v1beta failed, '
    'reason: connect ECONNREFUSED is retried", async () => {',
    'throw new Error("exceeded retry limit, last status: " + res.status);',
    'docs: document the "ERROR: exceeded retry limit, last status: 429" line codex prints',
    "fix(api): map codex_api error=http 429 onto our RetryableError type",
    '  ✓ Model call failed: {"message":"You have no quota","code":"quota_exceeded"} '
    "→ renders upgrade CTA (3 ms)",
    "      \"Sorry, you've hit a rate limit that restricts the number of Copilot model "
    'requests you can make within a specific time period."',
    "[app] Error sending request for url (http://localhost:8080/v1/chat): "
    "error trying to connect: ECONNREFUSED",
    "  Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_input_token_count, limit: 0  # golden fixture",
    "2026-07-27T09:14:02.117Z ERROR app::quota: error=http 429 Too Many Requests "
    "(raised by our own middleware)",
    '  ✗ quota banner shows "You have exhausted your daily quota on this model."',
    'chore: add fixture for the {"status": "RESOURCE_EXHAUSTED"} envelope',
    "You have exhausted your daily quota on this model.  <- expected copy for AC-2",
    'console.log("Model call failed: " + JSON.stringify({code: "rate_limited"}));',
    "TerminalQuotaError = class extends Error {}  // src/errors.ts",
    "  at TerminalQuotaError.handle (src/quota/errors.ts:42:11)",
    'PASS tests/quota.spec.ts > "You exceeded your current quota, please check your plan '
    'and billing details."',
    "Resource has been exhausted (e.g. check quota).  # copied from the Google docs",
    'banner.text = "Sorry, you have exceeded your Copilot token usage"',
    "2026-07-27T09:15:44.001Z ERROR codex_api_shim::mock: replaying error=http 429 "
    "fixture for the retry test",
    "- Sorry, you've hit a rate limit that restricts the number of Copilot model requests",
    # ---------------------------------------------------------------------
    # claude's usage-limit vocabulary (#610). The first three are Claude Code's
    # OWN advisory, captured from a HEALTHY session in run 20260913-165153-b850
    # (thirteen of them for every two real limit lines): the session is still
    # working, so a hit here parks a run mid-story. `used <N>% of your` versus
    # `hit your` is all that separates it from the wait line.
    "You've used 90% of your session limit · resets 7:20pm…",
    "You've used 96% of your session limit · resets 7:20pm…",
    "You've used 97% of your session limit · resets 7:20pm…",
    # …and a story implementing a credits banner, in its own words.
    "AC-3: show a banner when the user is out of usage credits",
    "PASS tests/test_credits.py::test_out_of_usage_credits_banner",
    "docs: implement the /usage-credits banner copy",
    "session limit reached — resets 3pm",
    "Run /usage-credits to keep using the model",
]

# A duplicated bait line silently weakens the corpus (it reads as coverage it is
# not), and pytest's parametrize ids collide on it. Guard the list itself.
assert len(BAIT) == len(set(BAIT)), "BAIT contains duplicate lines"


def _patterns(
    name: str, fields: tuple[str, ...] = PANE_PATTERN_FIELDS
) -> tuple[regex.Pattern[str], ...]:
    """``name``'s compiled patterns across ``fields`` — by default every pane
    field, since a hit in any of them stops a healthy session."""
    profile = get_profile(name)
    return tuple(regex.compile(p) for field in fields for p in getattr(profile, field))


def _matches(name: str, line: str, fields: tuple[str, ...] = PANE_PATTERN_FIELDS) -> bool:
    return any(p.search(line) for p in _patterns(name, fields))


@pytest.mark.parametrize("name", SEEDED_PROFILES)
@pytest.mark.parametrize("line", BAIT)
def test_seeded_profiles_never_classify_healthy_model_output(name: str, line: str) -> None:
    """The false-positive half. A hit pauses a run that was working."""
    assert not _matches(name, line), f"{name}.toml false-positives on: {line}"


@pytest.mark.parametrize("name", SEEDED_PROFILES)
@pytest.mark.parametrize("line", ANCHOR_REACHING_BAIT)
def test_seeded_profiles_survive_bait_that_reaches_their_anchor(name: str, line: str) -> None:
    """The half that a large corpus can silently fail to test. Lines here carry the
    CLI's own framing token, so they get past the anchor and put the rest of the
    pattern under real pressure."""
    assert not _matches(name, line), f"{name}.toml false-positives on: {line}"


@pytest.mark.parametrize("line", INSEPARABLE_VERBATIM_CITATIONS)
def test_verbatim_citation_of_the_real_error_is_inseparable(line: str) -> None:
    """Characterises the limit of content-based matching: a verbatim citation of
    the provider's error IS the provider's error, byte for byte, so it matches.

    Asserted positively on purpose. It is safe only because opencode's scanned log
    is the serve process's own stdout — ``<task_id>.server.out``, which the model
    cannot write to. So if this ever starts failing, either the patterns gained a
    citation-awareness they cannot soundly have, or the scanned file gained model
    output and the patterns must go. Either way it should not change silently.

    That second case is not hypothetical: ``<task_id>.log`` USED to be the server's
    stdout and these patterns were seeded on that basis, then it was repurposed as
    a curated `[bmad]` conversation transcript carrying the model's own words. The
    classifier kept scanning it. Every test here still passed, because they write
    their fixture to whatever path the code resolves — only the end-to-end test
    caught it. If the scanned file is ever repointed again, re-derive this safety
    argument before trusting it: see EnvFaultMixin.ENV_FAULT_LOG_SUFFIX."""
    assert _matches("opencode", line, ("env_fault_patterns",))


@pytest.mark.parametrize(("name", "field"), SEEDED_PANE_FIELDS)
def test_anchor_reaching_bait_is_not_vacuous(name: str, field: str) -> None:
    """Guards the guard. The test above is worthless unless its corpus actually
    contains the anchor under test — and it silently stopped doing so once, when
    the profile the corpus had been written for was withdrawn. Assert reachability
    mechanically instead of trusting it, for every anchor family each seeded
    field keys on."""
    for anchor in PROFILE_ANCHOR_LITERALS[name, field]:
        reaching = [line for line in ANCHOR_REACHING_BAIT if anchor in line]
        assert reaching, f"no ANCHOR_REACHING_BAIT line contains {name}'s {field} anchor {anchor!r}"


@pytest.mark.parametrize(("name", "field"), SEEDED_PANE_FIELDS)
def test_anchor_literal_is_the_live_patterns_anchor(name: str, field: str) -> None:
    """DW-373: guards the derivation of `PROFILE_ANCHOR_LITERALS`. The vacuity guard
    above is only as good as its input — an empty or trivially short anchor is
    `in` every bait line, so a derivation that silently went vacuous would pass it
    for every reason. Assert each derived anchor is meaningful, and that every live
    pattern opens with a regex spelling of one of them: the pattern's own leading
    tokens, run through the regex engine, must match the anchor exactly — so a
    decoding slip in `_leading_literal` cannot fake the prefix.

    And an anchor must not by itself SATISFY a pattern: if it did, every line
    reaching it would be a match, and the bait that reaches it could only ever
    redden the test above — the trap a wholly literal pattern sets
    (`_anchor_literal`).

    ABLATION: make `_derive_anchors` return ("",) (or stop decoding `[.]`) and this
    reddens; move a live pattern's anchor off the corpus and the guard above does."""
    patterns = getattr(get_profile(name), field)
    assert patterns, f"{name} ships no {field}"
    anchors = PROFILE_ANCHOR_LITERALS[name, field]
    for anchor in anchors:
        assert (
            len(anchor) >= MIN_ANCHOR_LEN
        ), f"{name}.{field}: derived anchor {anchor!r} is shorter than {MIN_ANCHOR_LEN} chars"
        assert not any(
            regex.search(pattern, anchor) for pattern in patterns
        ), f"{name}.{field}: derived anchor {anchor!r} alone matches a live pattern"
    for pattern in patterns:
        spellings = [
            (anchor, spelling)
            for anchor in anchors
            if (spelling := _leading_spelling(pattern, anchor)) is not None
        ]
        assert spellings, f"{name}.{field}: live pattern {pattern!r} opens with none of {anchors!r}"
        for anchor, spelling in spellings:
            assert pattern.startswith(spelling), (name, field, pattern)
            assert regex.fullmatch(spelling, anchor), (
                f"{name}.{field}: live pattern {pattern!r} does not open with anchor "
                f"{anchor!r} (its leading spelling {spelling!r})"
            )


def test_pane_fields_are_seeded_exactly_where_listed() -> None:
    """SEEDED_PANE_FIELDS is the whole truth: every listed (profile, field) ships
    patterns and no other does. A withdrawn seed must surface here rather than
    silently drop its cases out of every parametrization keyed on the list, and a
    new seed must arrive WITH its captured lines in REAL_BY_PROFILE."""
    seeded = {
        (name, field)
        for name in ALL_PROFILES
        for field in PANE_PATTERN_FIELDS
        if getattr(get_profile(name), field)
    }
    assert seeded == set(SEEDED_PANE_FIELDS)
    assert {(name, field) for name, field, _ in REAL_BY_PROFILE} == set(SEEDED_PANE_FIELDS)


@pytest.mark.parametrize(("name", "field", "line"), REAL_BY_PROFILE)
def test_each_seeded_profile_catches_its_own_cli_error_lines(
    name: str, field: str, line: str
) -> None:
    """The false-negative half, per CLI and per field. Each seeded field is
    anchored on a sentence its CLI emits, so a miss here means a real provider
    outage burns the story's retry budget instead of pausing the run — or, for
    the usage fields, a usage-limited session idles into a stall nudge and a
    timeout instead of parking.

    And ONLY by its own field: the fields are disjoint by contract. A credits line
    read as a session-window wait sits out a reset that never comes; a provider
    outage read as a usage limit escapes the token veto."""
    assert _matches(name, line, (field,)), f"{name}.{field} misses a real line: {line[:120]}"
    others = tuple(f for f in PANE_PATTERN_FIELDS if f != field)
    assert not _matches(name, line, others), f"{name}: {line[:120]!r} matches outside {field}"


@pytest.mark.parametrize("name", SEEDED_PROFILES)
def test_patterns_require_an_anchor_not_a_bare_cause(name: str) -> None:
    """The doctrine itself: a cause word alone must never be sufficient. Guards
    against a future edit that drops an anchor for 'better coverage'."""
    for bare in (
        "quota",
        "rate limit",
        "429",
        "usage limit reached",
        "too many requests",
        "type=provider.quota ",
        "provider.rate-limit",
        "type=provider.transport status=- attempt=2",
        "session limit",
        "/usage-credits",
        "resets 3pm",
    ):
        assert not _matches(name, bare), f"{name}.toml classifies the bare cause {bare!r}"


@pytest.mark.parametrize("name", PANE_CAPTURE_PROFILES)
def test_shipped_patterns_do_not_match_their_own_profile_line(name: str) -> None:
    """A profile must not classify ITSELF. The pane log carries the model's own
    output, so a session that prints, greps or diffs its profile would otherwise
    read its own configuration as a provider outage (or a usage limit) — one of
    the four non-test false positives #507 names, and the most absurd of them.
    Every pane field's patterns are held to every line of the file, comments
    included: the usage comments quote their captured sentences, split.

    What prevents it is the single-character classes in the shipped patterns
    (`t[o]`, `respons[e]`, `5[2]9`, `5[0]0`, `credit[s]`, `[.]`, `limi[t]`): they
    change nothing about what the regex MATCHES, and everything about what the
    regex's own source line matches. This test is why they must not be
    "cleaned up"."""
    packaged = resources.files("bmad_loop.data").joinpath("profiles").joinpath(f"{name}.toml")
    # Compiled ONCE rather than per line via _matches: that helper re-resolves the
    # profile and recompiles its patterns on every call, which is free for a
    # one-line corpus entry and minutes across a whole file.
    patterns = _patterns(name)
    assert patterns, f"{name} compiled no pane patterns; this guard would be vacuous"
    hits = [
        f"{name}.toml:{n}: {line.strip()[:120]}"
        for n, line in enumerate(packaged.read_text(encoding="utf-8").splitlines(), 1)
        if any(p.search(line) for p in patterns)
    ]
    assert not hits, f"{name}.toml classifies its own lines:\n" + "\n".join(hits)


# Files that hold matching lines ON PURPOSE, and are therefore excluded from the
# repo-wide guard below.
#
# * this module is the corpus: CLAUDE_REAL and CLAUDE_USAGE_REAL quote the
#   captured CLI sentences verbatim, because a pattern may only be seeded from a captured line and the
#   false-negative half has to assert against the real thing.
# * test_generic_tmux.py drives the REAL classifier end to end, so its fixture
#   logs must be lines the shipped patterns actually match.
#
# Both are pinned as non-vacuous below: an entry only stays legitimate while the
# file really does carry a matching line, so the list cannot be padded to silence
# a genuine regression.
DELIBERATE_CORPUS_FILES = ("tests/test_env_fault_patterns.py", "tests/test_generic_tmux.py")


def test_pane_capture_patterns_do_not_match_this_repo() -> None:
    """#507's own methodology, kept as a standing guard: compile the pane-capture
    profiles' patterns — every pane field, the #610 usage pair included — and
    scan every tracked file line-by-line.

    bmad-loop is developed by bmad-loop, so this repo's own text is pane-log
    input. Measured against the tree this guard landed on, the withdrawn
    `API Error.*<cause>` pattern matched 44 tracked lines: three outside the test
    suite (a docs/FEATURES.md bullet, a comment in adapters/profile.py, and the
    pattern's OWN line in claude.toml) plus 41 lines of ordinary
    pytest/grep/diff output. So a session working on this repo could classify
    itself as a provider outage, re-arm the budget and halt the run for an
    operator. (#507 counted 37 when it was filed, and named a CHANGELOG entry
    that has since been reworded out of range — the count moves with the tree,
    which is why this asserts zero rather than a number.)

    This is what fails if a future CHANGELOG entry, doc bullet or profile comment
    writes the framing token and a cause on the SAME line; write them separated
    (`API Error` … `<cause>`) instead — or, for a usage sentence, split it
    (`You've hit your session limi` + `t · resets`). Only
    DELIBERATE_CORPUS_FILES are exempt.

    opencode is out of scope on purpose, not by oversight: the file its patterns
    are matched against is `<task_id>.server.out` (see
    EnvFaultMixin.ENV_FAULT_LOG_SUFFIX), the `opencode serve` process's own
    stdout, which the model cannot write to — so a tracked line matching them is
    inert. Its own profile comment (opencode.toml) does self-quote, and that is
    why that is not a bug.

    git is spawned directly rather than through verify._run_git: that chokepoint
    is orchestrator-scoped, and a harness must not depend on the artifact it
    validates (AGENTS.md)."""
    root = Path(__file__).resolve().parents[1]
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout.decode("utf-8", errors="replace")
    tracked = [rel for rel in listed.split("\0") if rel]
    assert len(tracked) > 100, "git ls-files answered implausibly few files; guard would be vacuous"

    # Compiled ONCE, not per line via _matches: that helper re-resolves the
    # profile and recompiles its patterns on every call, which costs nothing for a
    # one-line corpus entry and minutes across every tracked line in the repo.
    patterns = [p for name in PANE_CAPTURE_PROFILES for p in _patterns(name)]
    assert patterns, "no pane-capture patterns compiled; this guard would be vacuous"
    # Every seeded pane field of every pane-capture profile is in the scan — a
    # field dropped from PANE_PATTERN_FIELDS would otherwise leave its patterns
    # free to match this repo unseen.
    for name, field in SEEDED_PANE_FIELDS:
        if name in PANE_CAPTURE_PROFILES:
            assert all(
                any(p.pattern == raw for p in patterns) for raw in getattr(get_profile(name), field)
            ), f"{name}.{field} is not in the repo scan"

    hits: list[str] = []
    corpus_hits: dict[str, int] = dict.fromkeys(DELIBERATE_CORPUS_FILES, 0)
    for rel in tracked:
        path = root / rel
        if not path.is_file():
            continue  # a submodule, or a symlink pointing outside the tree
        text = path.read_bytes().decode("utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if not any(p.search(line) for p in patterns):
                continue
            if rel in corpus_hits:
                corpus_hits[rel] += 1
                continue
            hits.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not hits, (
        "tracked lines classify as an environment fault or a usage limit on a "
        "pane-capture profile; split the framing token from the cause:\n" + "\n".join(hits)
    )
    # Non-vacuity for the exemptions themselves: each excluded file must still be
    # carrying the matching lines it was excluded FOR. A file that stops matching
    # has either lost its fixtures or no longer needs the exemption.
    for rel, count in corpus_hits.items():
        assert count, f"{rel} is exempt but holds no matching line — drop the exemption"


@pytest.mark.parametrize("name", UNSEEDED_PROFILES)
def test_unseeded_profiles_stay_inert(name: str) -> None:
    """These four ship no patterns ON PURPOSE, and this pins that.

    Patterns for them were drafted and withdrawn. They could only be written
    against error strings scraped from public issue trackers — no captured output
    from a real outage on any of these CLIs exists here — and an unverified
    pattern is not a neutral bet: one that fires on a healthy session pauses the
    entire run, which is worse than the fault it is meant to catch. Seeding
    nothing costs only the status quo.

    Do not seed these from a plausible-looking string. Seed them from a captured
    log line, with the run it came from cited, and add it to REAL_BY_PROFILE,
    SEEDED_PANE_FIELDS and this module's BAIT in the same change. The bar covers
    every pane field: a usage pattern stops a healthy session just as surely."""
    profile = get_profile(name)
    for field in PANE_PATTERN_FIELDS:
        assert getattr(profile, field) == (), f"{name} seeds {field}"


def test_v2_corpus_lines_are_what_the_adapter_writes() -> None:
    """Provenance for the 2.x half of OPENCODE_REAL: each line is exactly what
    the adapter writes (`format_provider_error`) for a frame captured live —
    so the corpus is the real thing, not a plausible-looking string."""
    written = [format_provider_error(event, data) for event, data in OPENCODE_V2_CAPTURED_FRAMES]
    assert written == OPENCODE_V2_REAL
    for line in written:
        assert "response" not in line and "insufficient_quota" not in line  # body dropped


@pytest.mark.parametrize("line", OPENCODE_V2_REAL)
def test_v2_provider_error_line_is_model_free_shape(line: str) -> None:
    """The 2.x pattern matches only the structured head of an adapter-written
    line: cut the line at `message=` and it still classifies, so nothing
    provider- or model-sourced is what made it match; and the same line behind
    any leading text — quoted in the transcript, a doc, a fixture — does not."""
    head = line.split(' message="', 1)[0] + " "
    v2 = [
        p
        for p in _patterns("opencode", ("env_fault_patterns",))
        if p.pattern.startswith("^provider-error")
    ]
    assert len(v2) == 1, "the opencode profile should ship one 2.x provider-error pattern"
    assert v2[0].search(head)
    assert v2[0].search(line)
    assert not v2[0].search("  " + line)
    assert not v2[0].search("log: " + line)
