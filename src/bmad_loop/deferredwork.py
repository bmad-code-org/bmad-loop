"""Deterministic reading and editing of the deferred-work ledger.

The ledger (`{implementation_artifacts}/deferred-work.md`) is append-only
markdown in the canonical form documented at
bmad-loop-sweep/deferred-work-format.md: `### DW-<seq>: <title>` headings with
`origin:`/`location:`/`reason:`/`status:` field lines. The one sanctioned
rewrite is :func:`archive_closed`, which moves closed entries verbatim to a
sibling archive file and leaves id-preserving stubs. Pre-#2651 dev primitives
and the attended `bmad-build` append flatter entries here directly, which the
orchestrator normalizes on sweep; the current unattended primitive records its
findings in the spec's frontmatter instead and the engine harvests them into
canonical entries. The
orchestrator never trusts an LLM to have edited it — status flips and decision
records happen here, and gates re-read the file from disk.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as calendar_date
from pathlib import Path

from . import sprintstatus
from .fences import fenced_spans
from .platform_util import atomic_write_text, neutralize_surrogates

HEADING_RE = re.compile(r"^### (DW-\d+): (.+?)\s*$", re.MULTILINE)
# Where a canonical entry ENDS, in every shape CommonMark spells an ATX heading:
# up to three spaces of indent, a space OR a tab after the hashes, and an empty
# heading (`##` alone — the separator may be the end of the line). A fourth space
# of indent is an indented code block rather than a heading, so those lines
# deliberately keep absorbing, and the indent class is spaces only for that same
# reason — a leading tab is four columns, so `\t## Notes` is a code block too.
# Read as permissively as the syntax is, because a missed boundary here does not
# merely lose a section header: the span runs on and the next section's
# `status:`/`gate:` lines are read as this entry's, so an open entry that never
# declared a gate takes a story hostage and the operator finds no gate in the
# entry the refusal names (#516). Where a miss would WRITE, the strict
# column-zero reading is the right one (`devcontract`'s destructive edits pin it
# deliberately); this only decides how far a read reaches.
# A lookahead rather than a consuming group: `parse_ledger` reads `.start()`, and
# holding the match to the opener leaves nothing free to grow a dependency on
# where the separator ended.
ANY_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?=[ \t]|$)", re.MULTILINE)
# The flat appender's opening line, in the two forms this module needs it: as a
# bullet in the raw ledger (FLAT_ENTRY_RE, the canonical-span boundary in
# parse_ledger) and as bullet *content* after `_BULLET_RE` has stripped the
# marker (`_FLAT_SOURCE_RE`, legacy section). One shape, two anchors — they have
# to agree, or a block the legacy parser recognizes stays invisible to it (#304).
# Keyed on the opening line alone, deliberately: also requiring the block's
# `summary:`/`evidence:` lines would narrow the boundary below the parser's own
# recognition, leaving the bug in place for every partial shape it accepts.
_FLAT_SOURCE_BODY = r"source_spec:[ \t]"
FLAT_ENTRY_RE = re.compile(rf"^[-*][ \t]+{_FLAT_SOURCE_BODY}", re.IGNORECASE | re.MULTILINE)
STATUS_RE = re.compile(r"^status:[ \t]*(.*)$", re.MULTILINE)
# The mechanical half of a hard gate. An entry could always *say* it blocked a
# story — `HARD GATE: must land before 3-2` in the reason line — and saying it
# stopped nothing: the queue picked the story up anyway, and the gate surfaced
# afterwards in the diff of work built on a leg nobody had wired. `gate:` names
# the blocked story keys in a form a check can match, so the claim can refuse.
# Parsed exactly like `status:`: a field line, read inside `parse_ledger`'s
# canonical span, so a line under a flat-append bullet belongs to that block and
# not to the entry above it.
GATE_RE = re.compile(r"^gate:[ \t]*(.*)$", re.MULTILINE)
# A story key as either queue spells one: a sprint key (`3-2-invite-link`), the
# stories-mode id it starts with (`3-2`), or a bare slug. Whitespace and
# separators are deliberately out — a token nothing can match is the same silent
# no-op the field exists to end, so it is surfaced rather than dropped.
GATE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# The second half of "can this token gate anything", and a different miss from the
# one above: `GATE_TOKEN_RE` rejects the spellings a *line* cannot carry (a space,
# a bare separator), this rejects the ones no *key* can carry. `gate: 3.2` passes
# the first and can never match `gates_story` against any legal key, so it used to
# report a green `ok` while gating nothing — the field's own silent no-op, one
# keystroke away from the shape that works.
#
# Two arms, because BMAD spells a story key two ways and they are NOT
# interchangeable. A stories-mode id is alphanumeric segments joined by single
# dashes (`_STORIES_ID_RE`), so `3.2` and `3_2` are out. A sprint key's slug is
# unconstrained (`sprintstatus.STORY_RE`'s trailing group), so `3-2-foo.bar` and
# `3-2-a_b` are LEGAL keys that gate correctly — which is why this is a
# whole-token shape test and not a ban on `.`/`_`. Only those characters in the
# *number* prefix are unmatchable; banning them outright would refuse real gates.
#
# Sound in the direction that matters: a token matching either arm is itself a
# legal key, so a story it could gate can exist. `gates_story`'s prefix and split
# arms only ever extend a key rightward past a `-`, and every such prefix of a
# legal key matches one of these arms too.
#
# `sprintstatus` is imported for its regex; `stories.ID_RE` is copied rather than
# imported because `stories` imports *this* module (a cycle). The copy is pinned
# to the original by a drift test rather than to a comment.
_STORIES_ID_RE = re.compile(r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$")
# The tokens `gates_story`'s split arm may fire for: a bare `<epic>-<story>`, both
# numeric. `sprintstatus.STORY_RE` attaches the split letter straight after the
# story *number*, so that is the only token a split can extend. "Ends in a digit"
# is a weaker test that reads the same shape into a slug — `3-2-v2` would take the
# arm and refuse `3-2-v2a-followup`, a different and legal key.
_SPLITTABLE_TOKEN_RE = re.compile(r"^\d+-\d+$")
# A `gate:` line the strict field pattern above will never see. `GATE_RE` is
# anchored to a lowercase `gate:` in column 0, exactly like `status:`, and that
# strictness fails in opposite directions for the two fields: a missed `status:`
# leaves an entry unresolved, which now gates conservatively, while a missed
# `gate:` leaves no gate at all. `Gate: 3-2` and an indented `  gate: 3-2` are
# therefore surfaced as unenforceable rather than silently absent — and surfaced
# rather than *accepted*, because accepting an indented line would read a fenced
# example inside an entry as a live gate and refuse a story nobody meant to block.
_GATE_NEAR_RE = re.compile(r"^[ \t]*gate[ \t]*:", re.IGNORECASE | re.MULTILINE)
# The prose convention `gate:` replaces, matched anywhere on a line rather than
# at its start: real ledgers hard-wrap their `reason:` prose, so the declaration
# routinely lands mid-line and a line-anchored pattern misses exactly the entries
# that have one. The quote lookbehind is what keeps that from over-firing — an
# entry *citing* the phrase (`names a "HARD GATE: ..."`) is discussion, not a
# declaration — and the colon does the rest of the work, since a sentence about
# "this HARD GATE is textual only" never reaches the pattern at all.
# The class covers the backtick and the curly quotes as well as the ASCII pair:
# a ledger is markdown, so `HARD GATE:` is the citation form an author reaches for
# first, and an LLM-written entry curls its quotes. Missing them made the warning
# fire on entries documenting the convention — including this repo's own docs.
# The lookbehind only reaches an *inline* citation, though; the block form of the
# same quoting is a fence, and no character precedes a line inside one. Callers
# read this through `declares_prose_gate`, which masks those out.
HARD_GATE_PROSE_RE = re.compile(r"""(?<!["'`«“”‘’])HARD GATE:""")
# Everything `str.splitlines()` splits on, not `\n` alone (#305). The writers
# below interpolate their arguments into a line-oriented file, so a break in a
# value injects ledger lines. The C1/Unicode members are load-bearing rather
# than decorative: `parse_legacy` scans with `splitlines()` while `parse_ledger`
# matches with `re.MULTILINE`, so a U+2028 splits an entry for one reader and is
# invisible to the other — the two then disagree about what the ledger says.
LINE_BREAK_RE = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]+")
# The writers' date shape. Deliberately a separate literal from the legacy
# parser's `_DATE_TOKEN_RE`, which happens to look similar today: that one
# decides whether a freeform heading is a dated section, and tightening what the
# orchestrator will *write* must never quietly retune what `parse_legacy` reads.
# Spelled `[0-9]` rather than `\d`, which also matches Arabic-Indic, fullwidth
# and mathematical digit forms — the ledger's readers understand none of them.
_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class DWEntry:
    id: str
    title: str
    status: str  # the status field value, "" when the line is missing
    body: str  # full entry text including the heading
    span: tuple[int, int]  # char offsets of the entry in the ledger text
    # Body-relative offsets of the line `status` was read from; None when the
    # entry has no status line. Carried rather than re-derived because the reader
    # picks the status with a fence-aware lookup at file scope, and a writer that
    # ran `STATUS_RE.search(body)` again would pick the *first* raw match instead.
    # Those differ exactly when an entry quotes an example above its live status:
    # the writer rewrote the quoted line, the reader kept reporting the real
    # `status: open`, and the close reported success while the entry — and any
    # `gate:` it carries — stayed open forever. No default: `parse_ledger` is the
    # only constructor, and a fallback here would silently restore that split.
    status_span: tuple[int, int] | None
    # The whole-file fence index the entry was carved with, so the gate scans can
    # ask the question the heading and status reads already ask at file scope. A
    # body slice cannot see a fence opened above the heading, and the two views
    # disagree: under a stray unclosed ``` above the heading, whole-file scope
    # treats the opener as text (so this entry EXISTS) while the body sees a later
    # matched `~~~` pair as a real fence and reads a live `gate:` as an example.
    # That direction loses a gate in silence, which is what the field exists to
    # end. No default, for `status_span`'s reason: the fallback IS the bug.
    examples: _Examples

    @property
    def open(self) -> bool:
        return self.status.split()[0] == "open" if self.status else False

    @property
    def done(self) -> bool:
        """Whether the entry has landed.

        Deliberately NOT ``not open``. A status line the format does not
        understand — ``status: opne``, or no status line at all — is neither open
        nor done, and the readers that ask want *opposite* answers about it:
        :func:`open_ids` drops it (it may already be finished), while a gate on it
        has to hold (it may not be). Deriving one from the other is what let
        ``gate:`` fail open on a one-character typo — the entry read as closed, so
        the gate was skipped and ``validate`` reported an all-clear naming it.
        """
        return self.status.split()[0] == "done" if self.status else False


@dataclass(frozen=True)
class _Examples:
    """The ledger's fenced worked examples, indexed for repeated offset queries.

    ``fenced_spans`` returns its ranges in increasing order and non-overlapping (a
    fence cannot open inside an open one), so a query is a binary search for the
    last span starting at or before the offset. Kept as an index rather than a bare
    list because both scales are in play at once: a parse asks once per heading and
    several times per entry, so a linear membership test would leave the parse
    quadratic whenever a ledger's examples grow with its entries.
    """

    spans: tuple[tuple[int, int], ...]
    starts: tuple[int, ...]

    def covers(self, offset: int) -> bool:
        i = bisect_right(self.starts, offset)
        return i > 0 and offset < self.spans[i - 1][1]


def _example_spans(text: str) -> _Examples:
    """The ledger's fenced worked examples, as offset ranges.

    Read at WHOLE-FILE scope, which is the whole point. A fence that opens above
    a quoted ``### DW-n:`` heading is stranded in the *previous* entry once spans
    are carved, so an entry-local query reads the example as live — a phantom
    entry whose ``gate:`` refuses a story nobody deferred. `deferred-work-format.md`
    ships exactly that shape (a complete entry inside a ```markdown fence), so
    quoting it into a ledger is the expected trigger, not a corner case.

    ``unclosed_hides_rest=False`` repeats the answer `gates()` gives one level
    down, and here for a stronger reason: under ``True`` a single stray opener
    would erase every heading below it, dropping real open work out of
    ``open_ids()`` in silence. A phantom entry from an unterminated fence is
    today's behaviour and is visible; a vanished ledger is neither.

    Walked once per :func:`parse_ledger` and passed down to the offset checks. The
    walk covers the whole file, so recomputing it per offset made the parse
    quadratic in the number of entries — and `Engine._refuse_gated_story` re-parses
    before every story dispatch, so a mature ledger paid it on the dispatch path.
    """
    spans = tuple(fenced_spans(text, unclosed_hides_rest=False))
    return _Examples(spans=spans, starts=tuple(s for s, _ in spans))


def _example(examples: _Examples, offset: int) -> bool:
    """Whether ``offset`` sits in a fenced worked example rather than the ledger.

    Takes the index rather than the text: the answer must come from the same
    whole-file walk for every offset in one parse, and a signature that re-derived
    it per call is what made that expensive enough to matter.
    """
    return examples.covers(offset)


def _unfenced(
    pattern: re.Pattern[str],
    text: str,
    start: int,
    end: int,
    examples: _Examples,
) -> re.Match[str] | None:
    """First match of ``pattern`` within ``text[start:end]`` that is not quoted.

    Not `search()` plus a check: the first match may be the quoted one, and the
    real boundary sits after it. Bounded by ``endpos`` so a match beyond the span
    cannot claim it, while ``examples`` still describes fence state from offset 0.
    """
    for m in pattern.finditer(text, start, end):
        if not _example(examples, m.start()):
            return m
    return None


def parse_ledger(text: str) -> list[DWEntry]:
    """Extract DW entries; non-conforming sections are skipped, an entry
    without a status line parses with status "" (not open).

    Fenced matches are skipped by every scan below, not just the heading one: a
    heading or flat bullet quoted inside an example must not start an entry, end
    one, or bound a block out of one. Filtering only the headings would trade the
    phantom entry for a truncation — a fenced ``## heading`` would still cut a
    real entry short at its own boundary, and a `gate:` line below the example
    would fall outside the span and stop gating, which is the failure this field
    exists to end.
    """
    entries = []
    examples = _example_spans(text)
    headings = [m for m in HEADING_RE.finditer(text) if not _example(examples, m.start())]
    for i, m in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        # an entry also ends at any intervening heading (e.g. a "## Deferred
        # from:" section header between freeform and DW-format content)
        other = _unfenced(ANY_HEADING_RE, text, m.end(), end, examples)
        if other:
            end = other.start()
        # ...and at a flat appender block, which belongs to no canonical entry
        # (#304). This span is what parse_legacy() masks out before scanning, so
        # absorbing the block hides the finding from every reader of the ledger.
        # Searched from the entry's own `status:` line, never from above it:
        # truncating over the status leaves the entry reading as neither open nor
        # done (open_ids() drops it, classify() calls it malformed), which trades
        # one lost flat block for one lost tracked entry. An entry with no status
        # line has nothing to protect, so the whole span is fair game.
        status_m = _unfenced(STATUS_RE, text, m.end(), end, examples)
        flat = _unfenced(
            FLAT_ENTRY_RE, text, status_m.end() if status_m else m.end(), end, examples
        )
        if flat:
            end = flat.start()
        body = text[m.start() : end]
        # Re-read rather than reuse the probe above: `end` may have moved, and the
        # status must be the one inside the final span. Searched over `text` at
        # absolute offsets because `_example` reads fence state from the top of the
        # file — a body slice cannot see an opener that sits above the heading.
        status_m = _unfenced(STATUS_RE, text, m.start(), end, examples)
        entries.append(
            DWEntry(
                id=m.group(1),
                title=m.group(2),
                status=status_m.group(1).strip() if status_m else "",
                body=body,
                span=(m.start(), end),
                status_span=(
                    (status_m.start() - m.start(), status_m.end() - m.start()) if status_m else None
                ),
                examples=examples,
            )
        )
    return entries


def open_ids(text: str) -> set[str]:
    return {e.id for e in parse_ledger(text) if e.open}


@dataclass(frozen=True)
class EntryGates:
    """One entry's ``gate:`` declaration, split by what a check can act on.

    Every shape that is not an enforceable token is reported by ``validate``,
    because none of them is a *weaker* gate than a valid one — each is the prose
    gate again wearing the field's clothes, and silence about it is what let the
    story run. ``lines`` is what distinguishes "declared nothing usable" from
    "declared nothing at all": an entry with no ``gate:`` line has made no claim,
    while ``gate:`` with an empty value has made one and inertly.

    ``empty`` counts those inert lines individually rather than folding them into
    an entry-wide verdict, because the two coexist: ``gate: 3-2`` followed by a
    bare ``gate:`` has both a gate in force and a line that names nothing, and an
    aggregate answer can only report one of them. Reporting the tokens and
    swallowing the empty line is the worse half to lose — the operator who wrote
    it believes a second story is held back.
    """

    tokens: tuple[str, ...] = ()
    malformed: tuple[str, ...] = ()
    lines: int = 0
    empty: int = 0
    near_miss: int = 0

    @property
    def inert(self) -> bool:
        """Every ``gate:`` line named nothing — ``gate:`` or ``gate: ,`` and no others."""
        return self.lines > 0 and not self.tokens and not self.malformed


def _quoted(entry: DWEntry, offset: int) -> bool:
    """Whether a BODY-relative ``offset`` sits in a fenced example.

    The single rule every gate scan in this module reads through, so that a fence
    means the same thing to all of them: an entry documenting the field quotes it,
    and a quoted example is not a declaration. Sharing it is the point — the prose
    scan was left on the raw body once, on the reasoning that a warning is cheap
    and its quote lookbehind was guard enough. It is not: that lookbehind reaches
    an inline citation only, so an entry explaining the old convention in a fenced
    block was told to convert a gate it was not declaring.

    Asked at FILE scope, like the heading and status reads in :func:`parse_ledger`
    and for the same reason: a body slice cannot see a fence opened above the
    heading, so the two views can disagree about the same line. They disagree in
    the direction that matters — a stray unclosed ``` above the heading leaves the
    entry standing at file scope while the body reads a later matched ``~~~`` pair
    as a real fence, masking a live ``gate:`` into an example. A gate lost in
    silence is the failure this field exists to end; a spurious refusal in an entry
    whose markdown is already malformed is the cheaper wrong answer.
    """
    return entry.examples.covers(entry.span[0] + offset)


def declares_prose_gate(entry: DWEntry) -> bool:
    """Whether the entry declares a gate in the pre-``gate:`` prose convention.

    :data:`HARD_GATE_PROSE_RE` filtered the way every other gate scan here is
    filtered. Lives beside them rather than at the caller so the fence rule has
    one implementation: ``validate`` is the only reader today, and a second one
    reaching for the bare pattern would reintroduce exactly the half-applied rule
    this replaced.
    """
    return any(not _quoted(entry, m.start()) for m in HARD_GATE_PROSE_RE.finditer(entry.body))


def gates(entry: DWEntry) -> EntryGates:
    """Every ``gate:`` token in one entry's canonical span, order-preserving.

    Multiple ``gate:`` lines union: an entry blocking three stories may list them
    on one line or on three, and to a line-oriented file neither spelling is the
    wrong one. Within a line the separator is a comma, and only a comma — a
    space-separated ``gate: 3-2 3-3`` lands in ``malformed`` rather than being
    read leniently, so the operator is told the spelling gated nothing instead of
    finding out from a story that ran.

    Duplicates collapse (an id repeated across lines is one claim, not two);
    empty items drop, so a trailing separator is not a token — but the *line* is
    still counted, which is how an all-empty declaration stays reportable.

    ``near_miss`` counts the lines this function deliberately did NOT read as a
    declaration: a `gate:` the strict field anchor misses (see
    :data:`_GATE_NEAR_RE`). They are counted rather than parsed so the operator is
    told the spelling gated nothing — the same trade the space-separated token
    makes, one level up.
    """
    tokens: list[str] = []
    malformed: list[str] = []
    lines = 0
    empty = 0

    # Both scans below skip fenced matches: an entry documenting this field quotes
    # it, and a quoted example is not a declaration — a fenced `gate: 3-2` sits in
    # column 0, right where the anchor looks, and the answer here is a *refusal*.
    for m in GATE_RE.finditer(entry.body):
        if _quoted(entry, m.start()):
            continue
        lines += 1
        named = False
        for raw in m.group(1).split(","):
            token = raw.strip()
            if not token:
                continue
            named = True
            bucket = tokens if _matchable_token(token) else malformed
            if token not in bucket:
                bucket.append(token)
        if not named:
            empty += 1
    near_miss = sum(
        # `^` puts every match at a line start, so this asks whether the same line
        # would have satisfied `GATE_RE` — i.e. whether it is the canonical spelling
        # already counted above — without re-running the anchor against a slice.
        not entry.body.startswith("gate:", m.start())
        for m in _GATE_NEAR_RE.finditer(entry.body)
        if not _quoted(entry, m.start())
    )
    return EntryGates(
        tokens=tuple(tokens),
        malformed=tuple(malformed),
        lines=lines,
        empty=empty,
        near_miss=near_miss,
    )


def _matchable_token(token: str) -> bool:
    """Whether ``token`` could gate any legal story key — the test that decides
    :attr:`EntryGates.tokens` vs :attr:`EntryGates.malformed`.

    Both halves are required and neither implies the other: ``GATE_TOKEN_RE``
    alone admits ``3.2``, which nothing can match, and the key shapes alone admit
    ``3-2 3-3`` via the sprint slug, which is one token pretending to be two.
    """
    if not GATE_TOKEN_RE.match(token):
        return False
    return bool(_STORIES_ID_RE.match(token) or sprintstatus.STORY_RE.match(token))


def gates_story(token: str, story_key: str) -> bool:
    """Whether ``token`` gates ``story_key``: equal, or its prefix at a key boundary.

    The prefix arm is what lets one token reach both queues — stories mode keys on
    the bare id (``3-2``) while sprint mode keys on the full ``3-2-invite-link``,
    and an author gating "story 3-2" means the story, not the spelling. The
    boundary is required rather than a bare ``startswith`` so ``3-2`` cannot sweep
    in its numeric neighbours: ``3-20-later`` is a different story.

    Two boundaries count, because BMAD spells a story key two ways. The plain one
    is ``-``. The other is a **split**: ``sprintstatus.STORY_RE`` lets an oversized
    story become ``3-2a-...`` / ``3-2b-...`` at breakdown time, and a token that
    only knew ``-`` would lose its gate the moment the gated story was split —
    silently, which is the worst thing a gate can do. One lowercase ASCII letter
    followed by ``-`` is therefore also a boundary. Exactly one letter, and the
    ``-`` after it is required, so ``3-2ab-x`` and a bare ``3-2a`` are not swept in.

    The split arm applies only to a token that *is* a bare ``<epic>-<story>``,
    because that is the only place a split letter can attach: ``STORY_RE`` puts it
    straight after the story *number*. Without that guard the arm reads any
    trailing letter as a split and gates a story nobody named — ``stories.ID_RE``
    admits word ids, so ``gate: auth`` refused ``authz-login``, and a hard failure
    on an unrelated story is the one way this check can be worse than the prose it
    replaced. "Ends in a digit" is the same guard written too loosely: the digit
    can belong to a *slug*, so ``gate: 3-2-v2`` took the arm and refused
    ``3-2-v2a-followup`` — a different, legal key — and ``gate: 3`` refused the
    distinct stories id ``3a-task``.
    """
    if story_key == token or story_key.startswith(f"{token}-"):
        return True
    # The `startswith` guard is load-bearing, not redundant with the slice below:
    # `story_key[len(token):]` says nothing about what preceded it, so without it
    # `3-2` would gate `9-9a-x` on the tail alone.
    if not story_key.startswith(token) or not _SPLITTABLE_TOKEN_RE.match(token):
        return False
    rest = story_key[len(token) :]
    return len(rest) >= 2 and "a" <= rest[0] <= "z" and rest[1] == "-"


def parse_declaration(raw: object) -> tuple[tuple[str, ...], str | None]:
    """The single reading of a ``closes_deferred:`` declaration (#234), shared by
    the ``stories.yaml`` parser, the engine's close hook, and ``validate``.

    Returns the normalized ids plus an error describing a wrong *container*.
    Missing / YAML-null is an empty declaration, not an error.

    Strict about the container, lenient about each item. A bare
    ``closes_deferred: DW-1`` is a schema error rather than a silently-wrapped
    single id — a string is iterable, so a lenient reading would quietly turn one
    id into a list of characters — while items are ``str()``-normalized and
    stripped, because an LLM-authored manifest may emit an unquoted ``DW-1`` as a
    string but a bare ``5`` as an int. Blanks drop and duplicates collapse
    (order-preserving): both are noise, not a contradiction.

    Callers decide the severity: the manifest parser raises, the engine journals,
    ``validate`` warns. What they must NOT do is disagree — before this, a wrong
    container was a hard schema error in ``stories.yaml`` and a silent empty
    declaration in frontmatter, so the same mistake either failed the parse or
    vanished depending on which file it was made in.

    Whether an id names a real entry is not decided here; that needs the ledger
    (:func:`classify`).
    """
    if raw is None:
        return (), None
    if not isinstance(raw, list):
        return (), f"must be a list of deferred-work ids (got {type(raw).__name__})"
    return tuple(dict.fromkeys(item for item in (str(x).strip() for x in raw) if item)), None


@dataclass(frozen=True)
class Declared:
    """How declared ids line up against one ledger snapshot (#234).

    Four outcomes, not two, because "not open" hides two very different cases.
    ``already_done`` is a satisfied declaration — a resume re-driving a close that
    already landed — and must stay silent. ``malformed`` is an entry that exists
    but carries neither an ``open`` nor a ``done`` status: nothing can be marked,
    and saying nothing would leave the operator believing it was.

    ``duplicates`` cross-cuts the other four: it names the declared ids the ledger
    carries more than once, whichever bucket they landed in. A duplicate id is a
    corrupt ledger (#286), and the entry this classification describes is only one
    of them — so the close is reported, never silent.
    """

    open_ids: tuple[str, ...] = ()
    already_done: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    malformed: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()


def classify(text: str, ids: Sequence[str]) -> Declared:
    """Partition `ids` against a single ledger snapshot, preserving order.

    Classifying from a snapshot rather than from :func:`mark_done`'s return value
    is deliberate: that return conflates "already done" with "absent from the
    ledger", and those need opposite treatment (silence vs. a warning).

    **The FIRST entry of a duplicated id wins**, because that is the one
    :func:`_find_entry` — and so every mutation in this module — acts on. Indexing
    last-wins instead made the two disagree, and a ledger carrying one `DW-1` open
    and another done then closed nothing while saying nothing, in either order: a
    done-first ledger classified the id `open`, sent it to
    :func:`mark_done_many`, and had :func:`_apply_done` refuse the done copy it
    found first (marked nothing, so not even an unmatched warning); an open-first
    ledger classified it `already_done` and never attempted the write at all
    (#284 round-6 review, finding 4). The duplicate itself is reported through
    ``duplicates`` rather than swallowed — one id naming two entries is a fault
    about the ledger, not an answer about the work."""
    by_id: dict[str, DWEntry] = {}
    duplicated: set[str] = set()
    for e in parse_ledger(text):
        if e.id in by_id:
            duplicated.add(e.id)
            continue  # first wins: `_find_entry` mutates that one
        by_id[e.id] = e
    buckets: dict[str, list[str]] = {"open": [], "done": [], "unknown": [], "malformed": []}
    for dw_id in ids:
        entry = by_id.get(dw_id)
        if entry is None:
            buckets["unknown"].append(dw_id)
            continue
        word = entry.status.split()[0] if entry.status else ""
        buckets[word if word in ("open", "done") else "malformed"].append(dw_id)
    return Declared(
        open_ids=tuple(buckets["open"]),
        already_done=tuple(buckets["done"]),
        unknown=tuple(buckets["unknown"]),
        malformed=tuple(buckets["malformed"]),
        duplicates=tuple(dw_id for dw_id in dict.fromkeys(ids) if dw_id in duplicated),
    )


def _find_entry(text: str, dw_id: str) -> DWEntry | None:
    for entry in parse_ledger(text):
        if entry.id == dw_id:
            return entry
    return None


def _insert_after_status(text: str, entry: DWEntry, line: str) -> str:
    """Insert a field line right after the entry's status line (or at the end
    of the entry when no status line exists)."""
    if entry.status_span:
        pos = entry.span[0] + entry.status_span[1]
        return text[:pos] + "\n" + line + text[pos:]
    insert_at = entry.span[0] + len(entry.body.rstrip())
    return text[:insert_at] + "\n" + line + text[insert_at:]


def _one_line(value: str) -> str:
    """Collapse every run of line-break characters in `value` to a single space.

    The whole of the #305 fix. These writers interpolate their arguments into a
    line-oriented file, so a value carrying a break mints a phantom
    `### DW-<n>` entry, truncates the entry's span at :data:`FLAT_ENTRY_RE` and
    re-surfaces the tail as a legacy item, or leaves the entry carrying two
    `status:` lines.

    Note what the last shape does *not* do: `STATUS_RE` takes the first match, so
    an injected `status:` never changes what `parse_ledger` reports. A test that
    asserts on `entry.status` therefore passes with this guard deleted — the
    observable is the line structure.

    Sanitizes; never raises, and nothing upstream rejects on a break either. The
    close paths call these writers bare (`sweep._close_resolved`,
    `decisions.apply_pre_answer`), so a `ValueError` would end the sweep as
    crashed; refusing the same text back at `validate_triage` only moved the
    stoppage to a pause. Collapsing is lossless enough — the ledger wants one
    line anyway — so this is the fix, and the skill docs are guidance that
    reduces occurrences without gating on them.

    That contract covers one hazard more than the break collapse alone, which is
    what the `neutralize_surrogates` pass in front of it buys (#329). A lone
    surrogate is not a line break, so it sailed through untouched — but it has
    no UTF-8 encoding, and `atomic_write_text`'s strict encode raises
    `UnicodeEncodeError` (a `ValueError` subclass) on it, from inside those same
    bare close-path calls. It arrives the way the break did: a triage
    `result.json` is cached with `json.dumps`, whose `ensure_ascii` keeps the
    code point a harmless `\\ud800` escape, and the reload's `json.loads` revives
    the real thing into `ResolvedEntry.evidence` and on into the `mark_done`
    note. Refusing it upstream would only move the stoppage again — same
    doctrine, same answer.

    A value with neither a break nor a surrogate is returned **untouched**, so an
    existing ledger is never reformatted and a clean write is byte-identical to
    before the guard; each pass keeps its own fast path, so the common value is
    scanned twice and copied never. The trailing `.strip()` removes all
    surrounding whitespace, not merely the space a leading or trailing break left
    behind — which is why it must stay on the far side of that fast path.

    A break-only value therefore sanitizes to `""`. Keeping it non-empty *here*
    could only yield bare whitespace, which trades an unfindable entry for an
    unidentifiable one, so each caller handles its own empties — and by two
    different strategies, which is why neither belongs in this helper.
    :func:`append_entry` **substitutes**, naming a vanished title
    `(untitled DW-<n>)` so the id it just burned stays findable.
    :func:`append_decision` **drops**, shedding the ` — ` separator along with an
    empty detail rather than promising one that is not there. Its `label` needs
    neither: every member of :data:`LINE_BREAK_RE` is `str.isspace()`, and
    `validate_triage` builds each `DecisionOption` with `.strip() or key`, so a
    break-only label has already become the option key before it arrives.

    A surrogate-only value, by contrast, sanitizes to a truthy `"�"`, so neither
    caller's empty-handling fires for it. That is the point of replacing rather
    than stripping: a title reading `�` still says *something unencodable was
    here*, where a vanished one would silently become `(untitled DW-<n>)`."""
    value = neutralize_surrogates(value)
    if not LINE_BREAK_RE.search(value):
        return value
    return LINE_BREAK_RE.sub(" ", value).strip()


def _iso_date_or_none(value: str) -> str | None:
    """`value` when it is a strict ISO ``YYYY-MM-DD`` calendar date, else None.

    The shared shape of the ledger's two date checks, so a skip-not-raise caller
    (:func:`_close_date`) and a raise caller (:func:`_require_iso_date`) cannot
    drift apart on what counts as a close date. The regex is not redundant with
    ``date.fromisoformat``: since 3.11 that also accepts ``20260611`` and ISO
    week dates, neither of which the ledger's own readers recognize, and it is
    the regex — via ``[0-9]`` — that pins the digits to ASCII. ``fromisoformat``
    in turn rejects the well-shaped impossible day (``2026-02-30``) that no
    pattern can catch."""
    if not _ISO_DATE_RE.fullmatch(value):
        return None
    try:
        calendar_date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _require_iso_date(value: str) -> None:
    """Raise unless `value` is a strict ISO `YYYY-MM-DD` calendar date.

    Raising is right here and wrong for free text: `date` is orchestrator-owned
    (`Engine._today()`), never model-authored, so a bad value is a programmer
    bug. Letting it through writes a `status:` line that reads as neither open
    nor done, which `classify` reports as malformed and `open_ids` drops — the
    entry silently leaves the sweep's world."""
    if _iso_date_or_none(value) is None:
        raise ValueError(f"date must be YYYY-MM-DD: {value!r}")


def _require_canonical_status(status: str) -> None:
    """Raise unless `status` is exactly `open` or `done YYYY-MM-DD`.

    Two halves with two different dependents. The *first word* is what
    :attr:`DWEntry.open` and :func:`classify` branch on, so anything but `open`
    or `done` makes an entry unreadable to both. The *date* is invisible to them
    — they read `status.split()[0]` and cannot tell `done 2026-02-30` from a real
    day — but it is not invisible downstream: the whole status value is carried
    verbatim to readers (the TUI's deferred pane, the `--json` projections), so a
    malformed date is rendered to a human as though it were one."""
    if status == "open":
        return
    if status.startswith("done "):
        _require_iso_date(status.removeprefix("done "))
        return
    raise ValueError(f"status must be 'open' or 'done YYYY-MM-DD': {status!r}")


def _operation_digest(operation_id: str) -> str:
    """Encode a stable close-operation id as one ledger-safe token."""
    if not operation_id:
        raise ValueError("operation_id must not be empty")
    return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()


def _apply_done(
    text: str,
    dw_id: str,
    date: str,
    note: str,
    *,
    undo_owner: str | None = None,
) -> str | None:
    """Flip one entry to `status: done <date>` + a resolution note *within* `text`.
    None when the entry is missing or not open. The entry is re-located after the
    status rewrite because that edit shifts every later span offset.

    The note is sanitized here, at the point of interpolation, rather than on
    :func:`mark_done`: that is a one-id wrapper over :func:`mark_done_many`, which
    `Engine._apply_deferred_closes` calls directly, so a wrapper-side guard would
    never see a story close (#305). `date` is validated by the sole caller, at its
    entry, so the check does not depend on a ledger existing."""
    note = _one_line(note)
    entry = _find_entry(text, dw_id)
    if entry is None or not entry.open:
        return None
    assert entry.status_span is not None  # open implies a status line
    start = entry.span[0] + entry.status_span[0]
    end = entry.span[0] + entry.status_span[1]
    previous_status_line = entry.body[entry.status_span[0] : entry.status_span[1]]
    if undo_owner is not None and LINE_BREAK_RE.search(previous_status_line):
        # An undo marker must never preserve a value that becomes more than one line
        # under the ledger readers' shared splitlines semantics. Standard closes
        # retain their existing behavior; the undo-capable path refuses the mark.
        return None
    done_status_line = f"status: done {date}"
    text = text[:start] + done_status_line + text[end:]
    entry = _find_entry(text, dw_id)
    assert entry is not None
    tail = f"resolution: {note}"
    if undo_owner is not None:
        # The owner digest makes this close distinguishable from an earlier run
        # that reused its human-readable note. The encoded prior line makes the
        # undo lossless for parser-accepted spacing and annotations. Hex keeps
        # every payload on one ASCII line, including Unicode annotations.
        previous_status_hex = previous_status_line.encode("utf-8").hex()
        tail += f"\nresolution-undo: {undo_owner} {date} {previous_status_hex}"
    return _insert_after_status(text, entry, tail)


def _mark_done_many(
    path: Path,
    dw_ids: Sequence[str],
    date: str,
    note: str,
    *,
    operation_id: str | None = None,
) -> list[str]:
    """Shared atomic implementation for the public close operations."""
    _require_iso_date(date)
    undo_owner = _operation_digest(operation_id) if operation_id is not None else None
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    marked: list[str] = []
    for dw_id in dw_ids:
        updated = _apply_done(text, dw_id, date, note, undo_owner=undo_owner)
        if updated is None:
            continue
        text = updated
        marked.append(dw_id)
    if not marked:
        return []
    atomic_write_text(path, text)
    return marked


def mark_done_many(path: Path, dw_ids: Sequence[str], date: str, note: str) -> list[str]:
    """Flip every entry in `dw_ids` to `status: done <date>` + a resolution note,
    in ONE read and ONE atomic write. Returns the ids actually flipped (missing
    and already-done ids are skipped), in the order given.

    All-or-nothing on purpose. A per-id read-modify-write loop leaves marks on
    disk when it raises partway through several ids — a half-applied closure the
    caller never gets to journal, so the ledger claims resolutions the run has no
    record of. Here a failure writes nothing, and the returned list is exactly
    what landed.

    The write goes through :func:`~bmad_loop.platform_util.atomic_write_text`
    rather than a bare tmp+replace: swapping a fresh inode over the ledger
    otherwise resets its mode (a ``0600`` ledger silently becoming world-readable)
    and turns a symlinked ledger into a regular file.

    ``date`` is validated before the ``is_file`` short-circuit so a programmer bug
    fails the same way whether or not a ledger happens to exist — a guard that
    only fires when the file is present is one an absent fixture hides."""
    return _mark_done_many(path, dw_ids, date, note)


def mark_done_many_reopenable(
    path: Path,
    dw_ids: Sequence[str],
    date: str,
    note: str,
    operation_id: str,
) -> list[str]:
    """Close entries atomically with a durable, operation-specific undo marker.

    ``operation_id`` must be stable and recomputable across crash/replay from
    already-persisted identity (for example ``run_id`` + ``story_key``), never an
    ephemeral random value. Only entries actually flipped receive its marker;
    skipped, already-done ids therefore cannot be reopened by this operation.

    The ordinary :func:`mark_done_many` deliberately emits no marker and retains
    its existing ledger format. Use this variant only for a transaction with a
    later rollback leg.
    """
    return _mark_done_many(path, dw_ids, date, note, operation_id=operation_id)


def mark_done(path: Path, dw_id: str, date: str, note: str) -> bool:
    """Flip one entry to `status: done <date>` and record a resolution note.
    Returns False (no write) when the entry is missing or already done."""
    return bool(mark_done_many(path, [dw_id], date, note))


_MARK_DONE_TAIL_RE = re.compile(
    r"\nresolution:[ \t]*(.*)"
    r"\nresolution-undo:[ \t]*([0-9a-f]{64})[ \t]+"
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})[ \t]+([0-9a-f]+)$",
    re.MULTILINE,
)


def mark_open(path: Path, dw_id: str, note: str, operation_id: str) -> bool:
    """Undo one close written by :func:`mark_done_many_reopenable`.

    The entry must still carry the operation's adjacent resolution and undo-marker
    lines. A standard or earlier close has no matching marker and cannot be
    reopened merely because it reused the same human-readable note.

    A live ``archived:`` stamp is demoted to :data:`_ARCHIVED_BODY_FIELD` rather
    than dropped: the reopened entry is no longer archived, but the body its
    close moved out still is, and that line is the only thing a later triage has
    to find it with.
    """
    undo_owner = _operation_digest(operation_id)
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    entry = _find_entry(text, dw_id)
    if entry is None or entry.open:
        return False
    if entry.status_span is None:
        # parse_ledger deliberately tolerates status-less entries. This primitive
        # is later called from _defer, where an AttributeError would crash the run
        # instead of completing the deferral.
        return False
    status_line = entry.body[entry.status_span[0] : entry.status_span[1]]
    try:
        _require_canonical_status(entry.status)
    except ValueError:
        # Only a canonical status written by mark_done is eligible for undo.
        # Preserve malformed or human-authored statuses for validation/reporting.
        return False
    res_m = _MARK_DONE_TAIL_RE.match(entry.body, entry.status_span[1])
    if res_m is None:
        return False
    if res_m.group(1).strip() != _one_line(note).strip() or res_m.group(2) != undo_owner:
        return False
    if status_line != f"status: done {res_m.group(3)}":
        return False
    try:
        previous_status_line = bytes.fromhex(res_m.group(4)).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return False
    if LINE_BREAK_RE.search(previous_status_line):
        return False
    previous_status_m = STATUS_RE.fullmatch(previous_status_line)
    previous_status = previous_status_m.group(1).strip() if previous_status_m else ""
    if not previous_status or previous_status.split()[0] != "open":
        return False
    start = entry.span[0] + entry.status_span[0]
    end = entry.span[0] + res_m.end()
    # Demote the entry's live `archived:` stamps along with the close they
    # describe, rather than deleting them. A stub's stamp says "this body lives
    # in the archive file"; once the close is undone the body is here and the
    # line is a lie, and leaving it standing is not merely untidy — status +
    # undo tail + stamp is the exact `_STUB_BODY_RE` shape, so the next
    # reopenable close reconstitutes a stub `archive_closed` skips forever,
    # stranding the entry outside every future archive (#711).
    #
    # Cutting the line outright strands the entry a second way: a stub keeps
    # neither `location:` nor `reason:` (`_PRESERVED_FIELD_RE`), so the stamp is
    # the reopened entry's ONLY route back to the body, and triage arrives with
    # a heading and nothing to triage (#711 review). Renaming the field keeps
    # both properties — the value still names the archive block, since an id may
    # own several and each carries its own `archived:` date, while the renamed
    # line matches neither `_ARCHIVED_FIELD_RE` nor `_STUB_BODY_RE`, so the
    # entry reads as live and re-archives normally. Rehydrating the body here
    # instead was the alternative and is worse: several blocks per id is by
    # design, so a rollback's reopen would have to guess which one, and a wrong
    # guess overwrites live content with a stale body.
    #
    # Cuts are disjoint (an `^archived:` line cannot start inside the status
    # line or its adjacent tail) and applied back-to-front so earlier offsets
    # stay valid.
    cuts = [(start, end, previous_status_line)]
    for cut_start, cut_end in _archived_line_spans(entry):
        # Everything after the field name — value, spacing and the terminating
        # newline — carries over verbatim; the span starts at the anchor, so
        # the first colon is the field's own.
        stamp = entry.body[cut_start:cut_end].split(":", 1)[1]
        cuts.append(
            (
                entry.span[0] + cut_start,
                entry.span[0] + cut_end,
                f"{_ARCHIVED_BODY_FIELD}{stamp}",
            )
        )
    for cut_start, cut_end, replacement in sorted(cuts, reverse=True):
        text = text[:cut_start] + replacement + text[cut_end:]
    atomic_write_text(path, text)
    return True


def append_decision(path: Path, dw_id: str, date: str, label: str, detail: str) -> bool:
    """Record a human decision on an entry without changing its status.

    `label` and `detail` come from a triage session's `DecisionOption`, so they
    are sanitized to one line rather than refused — see :func:`_one_line`. This
    is also where a build option's `intent` gets flattened, since it reaches the
    ledger only as `detail = option.resolution or option.intent`.

    Precondition: `date` is ISO `YYYY-MM-DD`; anything else raises `ValueError`,
    checked before the ``is_file`` short-circuit so an absent ledger cannot hide
    the bug.

    The write goes through :func:`~bmad_loop.platform_util.atomic_write_text` for
    the reasons documented on :func:`mark_done_many`, plus one this sibling shares
    with it: a bare ``Path.write_text`` truncates *before* it encodes, so any
    failure between the two — an unencodable value, ``ENOSPC``, ``EIO`` — leaves a
    zero-byte ledger where every entry used to be (#328)."""
    _require_iso_date(date)
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    entry = _find_entry(text, dw_id)
    if entry is None:
        return False
    label = _one_line(label)
    # Sanitize before the emptiness test, never after: a break-only detail
    # collapses to "" and must then drop the separator with it, or the entry
    # carries a dangling `— ` promising a detail that is not there.
    detail = _one_line(detail)
    detail_part = f" — {detail}" if detail else ""
    text = _insert_after_status(text, entry, f"decision: {date} {label}{detail_part}")
    atomic_write_text(path, text)
    return True


DW_ID_RE = re.compile(r"\bDW-(\d+)\b")


def next_seq(text: str) -> int:
    """The next free DW sequence number — one past the highest DW-<n> anywhere
    in the ledger (malformed entries included, so a number is never reused and
    the sweep numbering check stays satisfied)."""
    nums = [int(m.group(1)) for m in DW_ID_RE.finditer(text)]
    return (max(nums) + 1) if nums else 1


def field_line_present(body: str, field: str, value: str) -> bool:
    """True when `body` has a `field:` line whose value is exactly `value`,
    matching the shapes append_entry writes (plain, or backtick-wrapped as for
    `source_spec:`). Anchored per-line so an incidental substring elsewhere in
    the body (e.g. inside `reason:`) never counts as a match."""
    v = re.escape(value)
    return re.search(rf"(?m)^{re.escape(field)}:[ \t]*`?{v}`?[ \t]*$", body) is not None


def append_entry(
    path: Path,
    *,
    title: str,
    origin: str,
    source_spec: str,
    reason: str,
    location: str = "n/a",
    status: str = "open",
    severity: str | None = None,
) -> str | None:
    """Append a new canonical `### DW-<seq>` entry numbered past the highest
    existing DW id, returning the new id (e.g. "DW-42").

    Idempotent: returns None without writing when an open entry already carries
    the same `origin:` marker and `source_spec:` — so re-running the same defer
    (e.g. a second sweep of the same story) never duplicates the entry. Creates
    the ledger (and parent dir) if it does not yet exist.

    Free text is sanitized (:func:`_one_line`) **before** the idempotence scan,
    which compares the caller's value against the stored one via
    :func:`field_line_present`: sanitizing afterwards would compare a raw value
    against a sanitized line, so every replay of the same multiline defer would
    miss its own entry and append another. `status` and `severity` are
    orchestrator-owned enumerations and raise instead.

    The write goes through :func:`~bmad_loop.platform_util.atomic_write_text` for
    the reasons documented on :func:`mark_done_many`, plus one this sibling shares
    with it: a bare ``Path.write_text`` truncates *before* it encodes, so any
    failure between the two — an unencodable value, ``ENOSPC``, ``EIO`` — leaves a
    zero-byte ledger where every entry used to be (#328)."""
    _require_canonical_status(status)
    # The whitelist is derived from the legacy parser's alias table (defined
    # below; resolved at call time) so what this writer emits and what
    # `field_severity` normalizes to cannot drift apart.
    if severity and severity not in _CANONICAL_SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(_CANONICAL_SEVERITIES)}: {severity!r}")
    given_title = bool(title)
    title = _one_line(title)
    origin = _one_line(origin)
    source_spec = _one_line(source_spec)
    reason = _one_line(reason)
    location = _one_line(location)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for entry in parse_ledger(text):
        if (
            entry.open
            and field_line_present(entry.body, "origin", origin)
            and field_line_present(entry.body, "source_spec", source_spec)
        ):
            return None
    dw_id = f"DW-{next_seq(text)}"
    if given_title and not title.strip():
        # A break-only title sanitizes to nothing, and `### DW-<n>: ` is a
        # heading `HEADING_RE`'s `(.+?)` does not match: the caller is handed an
        # id no reader can find while `next_seq` has already burned it.
        #
        # Tested with `.strip()`, not `not title`: a title of `"  "` carries no
        # break at all, so `_one_line` returns it unchanged by the byte-identity
        # fast path and it stays truthy. It parses, but renders blank in
        # `status`, `--json` and the TUI — the unidentifiable half of the same
        # problem, reached without ever touching the sanitizer.
        #
        # Scoped to a title that *had* content: an already-empty one keeps its
        # long-standing behavior, and the invariant is about non-empty values.
        title = f"(untitled {dw_id})"
    lines = [
        f"### {dw_id}: {title}",
        f"origin: {origin}",
        f"location: {location}",
        f"source_spec: `{source_spec}`",
    ]
    if severity:
        lines.append(f"severity: {severity}")
    lines.append(f"reason: {reason}")
    lines.append(f"status: {status}")
    block = "\n".join(lines) + "\n"
    # exactly one blank line between the previous content and the new entry
    if text == "" or text.endswith("\n\n"):
        sep = ""
    elif text.endswith("\n"):
        sep = "\n"
    else:
        sep = "\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text + sep + block)
    return dw_id


ARCHIVE_REL = "deferred-work-archive.md"
# A stub left by a prior archive_closed run carries this field. The next run
# reads it to skip entries whose body has already been moved — without it,
# every run would re-archive the stub (a heading + status line) and the
# archive would accumulate duplicates.
_ARCHIVED_FIELD_RE = re.compile(r"^archived:", re.MULTILINE)

# What :func:`mark_open` leaves where that stamp was. A reopened entry is not
# archived — its body is back in the ledger — but the body the undone close
# moved out still is, and this line is what a triage session follows to it.
# Deliberately a different field name: `archived:` means "the body is
# elsewhere", which a reopened entry must not claim, and a line matching
# `_ARCHIVED_FIELD_RE` here would rebuild the exact `_STUB_BODY_RE` shape on
# the next reopenable close.
_ARCHIVED_BODY_FIELD = "archived-body:"


def _archived_line_spans(entry: DWEntry) -> list[tuple[int, int]]:
    """Body-relative spans of the entry's live ``archived:`` field lines, each
    covering the whole line including its terminating newline.

    Reads through :func:`_quoted` for the same reason every gate scan does:
    an entry documenting the archive field in a fenced example carries the
    line in column 0, right where the anchor looks, and without the fence
    check a quoted ``archived:`` would be mistaken for the real thing. The one
    place that rule is written, so the three questions asked about the field —
    is this entry archived, what does its body say apart from the stamp, and
    which bytes must a reopen rename — cannot answer it differently.

    Whole lines rather than match starts because both cutting callers remove
    the line, and a span ending at the anchor would leave the stamp's value
    behind as orphaned text.
    """
    spans: list[tuple[int, int]] = []
    for m in _ARCHIVED_FIELD_RE.finditer(entry.body):
        if _quoted(entry, m.start()):
            continue
        line_end = entry.body.find("\n", m.end())
        spans.append((m.start(), len(entry.body) if line_end == -1 else line_end + 1))
    return spans


def _is_archived(entry: DWEntry) -> bool:
    """Whether the entry carries a live ``archived:`` field line (not a quoted
    example), marking it as touched by :func:`archive_closed` — a stub in the
    live ledger, or an archived body in the archive file.
    """
    return bool(_archived_line_spans(entry))


def _body_without_archived(entry: DWEntry) -> str:
    """The entry's body with its live ``archived:`` stamps and trailing blank
    lines removed — the comparison key for :func:`archive_closed`'s
    crash-recovery skip.

    An archived twin is its ledger entry plus exactly one ``archived:`` line,
    so the two are the same content only once that line is discounted; trailing
    newlines go with it because they record where the entry sat in its file,
    not what it says. Everything else is compared verbatim, deliberately: the
    cheap wrong answer is archiving a body twice, and the expensive one is
    deciding a divergent re-closure was already saved and dropping it (#711).
    """
    body = entry.body
    for start, end in reversed(_archived_line_spans(entry)):
        body = body[:start] + body[end:]
    return body.rstrip("\n")


# Field lines a stub must carry when the archived body had them, because
# downstream readers key on them regardless of status: `gate:` (validate's
# closed-entry gate report deliberately keeps speaking), `origin:` +
# `source_spec:` (the engine's status-agnostic harvest-replay dedupe), and the
# reopenable-close undo tail (`mark_open`'s adjacency requirement).
_PRESERVED_FIELD_RE = re.compile(r"^(gate:.*|origin:.*|source_spec:.*)$", re.MULTILINE)

# The exact stub shape :func:`archive_closed` leaves in the live ledger.
# A done entry that merely carries a hand-written `archived:` line does NOT
# match — it is a real entry, not a stub, and must still be archived.
_STUB_BODY_RE = re.compile(
    r"### .*: .*\n\n"
    r"status: done [0-9]{4}-[0-9]{2}-[0-9]{2}\n"
    # Separators mirror `_MARK_DONE_TAIL_RE`, which tolerates tabs: that regex
    # decides what `_preserved_stub_lines` copies into the stub verbatim, so a
    # stricter shape here reads a stub this module just wrote as a live entry
    # and re-archives it on every run, forever, appending nothing (#711).
    r"(?:resolution:[ \t]*[^\n]*\nresolution-undo:[ \t]*[0-9a-f]{64}[ \t]+[^\n]*\n)?"
    r"(?:(?:gate:|origin:|source_spec:)[^\n]*\n)*"
    r"archived: [^\n]*\n"
    r"\n?"
)


def _is_stub(entry: DWEntry) -> bool:
    """Whether the entry is a stub left by a prior :func:`archive_closed` run.

    Shape-based rather than `archived:`-line-based: a done entry a human
    annotated with a stray unfenced ``archived:`` line is a real entry whose
    body still belongs in the live ledger — skipping it forever on the strength
    of one line would silently exclude it from every future archive.
    """
    return entry.done and _STUB_BODY_RE.fullmatch(entry.body.rstrip("\n") + "\n") is not None


def _preserved_stub_lines(entry: DWEntry) -> list[str]:
    """The load-bearing field lines a stub must keep from the archived body.

    Scanned fence-aware like every field read in this module: a fenced example
    documenting `origin:` is not a declaration. The undo tail is read with the
    same adjacency regex :func:`mark_open` will later use against the stub, so
    what qualifies here is exactly what remains undoable there.
    """
    lines = [
        entry.body[m.start() : m.end()]
        for m in _PRESERVED_FIELD_RE.finditer(entry.body)
        if not _quoted(entry, m.start())
    ]
    if entry.status_span is not None:
        tail = _MARK_DONE_TAIL_RE.match(entry.body, entry.status_span[1])
        if tail is not None:
            lines = [tail.group(0).lstrip("\n")] + lines
    return lines


def _close_date(entry: DWEntry) -> str | None:
    """The ISO close date from a ``done <date>`` status, or None when the
    entry is not done, is done without a date suffix, or carries a date
    that does not match the ISO ``YYYY-MM-DD`` shape.

    Entries closed with a bare ``status: done`` (no date) or a hand-edited
    non-ISO date are skipped by :func:`archive_closed`: there is no close
    date to compare against a ``--before`` cutoff, and the stub the function
    leaves in the ledger needs one to stay readable as done.
    """
    if not entry.done:
        return None
    parts = entry.status.split()
    if len(parts) != 2:  # exactly `done YYYY-MM-DD` — extra tokens are not a close date
        return None
    # Same shape check as `_require_iso_date` (well-formed regex AND a real
    # calendar day), skip-not-raise: a hand-edited close is data, not a bug.
    return _iso_date_or_none(parts[1])


def archive_closed(
    path: Path,
    *,
    before: str | None = None,
    archive_date: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Move closed (``status: done <date>``) ledger entries to a sibling
    archive file (:data:`ARCHIVE_REL`), replacing each with a minimal stub
    that preserves the DW- id for grep and ``closes_deferred``
    cross-references.

    Returns the list of archived ids, in ledger order. ``dry_run=True``
    returns the ids that *would* be archived without writing anything.

    Each archived entry's body is preserved verbatim in the archive file,
    with an ``archived: <date>`` field line appended after the entry's status
    line. The stub left in the live ledger keeps the heading, a ``status:
    done <date>`` line (so :func:`parse_ledger` reads it as done and
    :func:`open_ids` drops it), an ``archived: <date>`` line (so a subsequent
    run skips it rather than re-archiving the stub), and the entry's
    load-bearing field lines — ``gate:``, ``origin:``/``source_spec:``, and
    the reopenable-close undo tail — because downstream readers key on those
    regardless of status (validate's closed-gate report, the engine's
    harvest-replay dedupe, and sweep bundle rollback respectively).

    ``before`` (ISO ``YYYY-MM-DD``) archives only entries closed strictly
    *before* that date. Entries with ``status: done`` (no date) are always
    skipped — there is no close date to compare against a cutoff or to stamp
    the stub with. Open and legacy entries are never touched.

    Dates are validated with :func:`_require_iso_date` (same validation as
    the existing close-path writers), ahead of the ``is_file`` short-circuit
    so a programmer bug fails the same way whether or not a ledger exists.
    Both writes — the trimmed ledger and the appended archive — go through
    :func:`atomic_write_text`, the same primitive every ledger writer uses.
    The archive file accumulates on repeat runs: new entries are appended to
    the existing file, never overwritten, and stubs from a prior run are
    skipped by their exact stub shape.
    """
    if before is not None:
        _require_iso_date(before)
    if archive_date is not None:
        _require_iso_date(archive_date)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    to_archive: list[tuple[DWEntry, str]] = []
    for entry in parse_ledger(text):
        close_date = _close_date(entry)
        if close_date is None:
            continue  # not done, or done without a date
        if before is not None and close_date >= before:
            continue  # closed on or after the cutoff
        if _is_stub(entry):
            continue  # stub from a prior archive_closed run
        to_archive.append((entry, close_date))
    if not to_archive:
        return []
    archived_ids = [e.id for e, _ in to_archive]
    if dry_run:
        return archived_ids
    stamp = archive_date or calendar_date.today().isoformat()
    archive_path = path.parent / ARCHIVE_REL
    existing = archive_path.read_text(encoding="utf-8") if archive_path.is_file() else ""
    # Append an `archived:` line after each entry's status line. The status
    # span is body-relative, so the insertion works within the body slice —
    # same offset math as `_insert_after_status`, applied to the body.
    #
    # Crash recovery: the archive is written BEFORE the ledger (see below), so
    # a crash between the two writes leaves the ledger with full entries whose
    # bodies are already in the archive. A retry must still stub those ledger
    # entries (completing the interrupted operation) but must NOT append their
    # bodies again — an append-only archive accumulating duplicates. Entries
    # whose parsed archive twin carries a live (non-fenced) ``archived:``
    # field are therefore skipped here and only replaced with stubs below.
    #
    # The twin must match in BODY, not merely in id and close date. A DW id is
    # reusable across closures (`mark_open` reopens, a re-close follows) and a
    # closed entry still accepts writes (`append_decision` does not read
    # status), so id + date names a *closure slot*, not its content: reopened
    # and re-closed the same day with a new resolution, or annotated with a
    # decision after its body was archived, the ledger entry and its twin
    # differ. Skipping on the slot alone stubbed that entry over its own
    # content while reporting the id as archived — the body reached neither
    # file (#711). A body that differs is appended instead; the archive holds
    # several blocks per id by design, and over-archiving is recoverable where
    # a silent drop is not.
    archive_blocks: list[str] = []
    already_archived = {
        e.id: (_close_date(e), _body_without_archived(e))
        for e in parse_ledger(existing)
        if _is_archived(e)
    }  # fence-aware: a quoted example in the archive is not a real body
    for entry, close_date in to_archive:
        if already_archived.get(entry.id) == (close_date, _body_without_archived(entry)):
            continue  # this closure's body is already archived (crashed prior run)
        body = entry.body
        assert entry.status_span is not None  # done with a date implies a status line
        pos = entry.status_span[1]
        body = body[:pos] + f"\narchived: {stamp}" + body[pos:]
        archive_blocks.append(body)
    if archive_blocks:
        if existing == "" or existing.endswith("\n\n"):
            sep = ""
        elif existing.endswith("\n"):
            sep = "\n"
        else:
            sep = "\n\n"
        archive_content = existing + sep + "".join(archive_blocks)
    else:
        archive_content = existing  # pure crash-recovery pass: only stub the ledger
    # Replace each archived entry's span with a stub, working backwards so
    # earlier spans are unaffected by later replacements — the same
    # text-surgery pattern as `_apply_done`, applied to multiple entries.
    for entry, close_date in reversed(to_archive):
        preserved = "".join(f"{line}\n" for line in _preserved_stub_lines(entry))
        stub = (
            f"### {entry.id}: {entry.title}\n\n"
            f"status: done {close_date}\n"
            f"{preserved}"
            f"archived: {stamp}\n\n"
        )
        start, end = entry.span
        text = text[:start] + stub + text[end:]
    # Write the archive BEFORE the ledger: a crash between writes leaves the
    # archive with extra content (harmless — the archive is append-only) and
    # the ledger unchanged (safe — the bodies are still in the live file).
    # Writing the ledger first would leave stubs in the ledger with no bodies
    # in the archive — content lost.
    atomic_write_text(archive_path, archive_content)
    atomic_write_text(path, text)
    return archived_ids


# ------------------------------------------------------------------- legacy
#
# Ledgers written before the DW format (older BMAD-method projects) are
# freeform markdown: "## Deferred from: ..." sections holding id'd or
# strikethrough bullets, "### D-1.2-003: title — RESOLVED" entry headings,
# topic sections closed with "(... — DONE)". parse_legacy() reads them
# tolerantly so the TUI can display them and a sweep can migrate them; the
# strict DW contract above is untouched — legacy items have no status line
# to flip, so mark_done/open_ids never see them.

# Severity is extracted forgivingly (the ledger is LLM-written): a
# `severity:`/`priority:` field line in any case, plain or bold-bulleted
# ("- **Severity:** high"), common synonyms accepted.
SEVERITY_ALIASES = {
    "critical": "critical",
    "blocker": "critical",
    "high": "high",
    "major": "high",
    "medium": "medium",
    "med": "medium",
    "moderate": "medium",
    "low": "low",
    "minor": "low",
    "trivial": "low",
}
# What every alias above normalizes to, and so the only values `append_entry` may
# write. Derived rather than restated: a hand-copied whitelist drifts the moment
# an alias is added for a new canonical level.
_CANONICAL_SEVERITIES = frozenset(SEVERITY_ALIASES.values())

SEVERITY_FIELD_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?(?:severity|priority)[ \t]*:[ \t]*(?:\*\*)?[ \t]*"
    r"([A-Za-z][\w-]*)",
    re.IGNORECASE | re.MULTILINE,
)


def field_severity(body: str) -> str | None:
    m = SEVERITY_FIELD_RE.search(body)
    return SEVERITY_ALIASES.get(m.group(1).lower()) if m else None


@dataclass(frozen=True)
class LegacyEntry:
    key: str  # stable content-derived identity, unique within the file
    id: str  # native id ("W2", "D-CAP-001", "0-1"), "" when the item has none
    title: str  # cleaned one-line title (markers/strikethrough stripped)
    done: bool
    severity: str | None  # normalized critical/high/medium/low, None unknown
    body: str  # the bullet/heading block verbatim
    section: str  # enclosing ##/### heading text, "" at top level
    span: tuple[int, int]  # char offsets in the ledger text


_DONE_WORDS = r"(?:DONE|RESOLVED|CLOSED|VERIFIED|DOCUMENTED|FIXED)"
_LINE_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
# a single whitespace-free digit-bearing token before ":" or "—" makes a
# heading an entry ("### D-CAP-001: title", "## D-8.6-001 — title");
# "## Epic 0: ..." has a space and "## 2026-06-09 — ..." is a date, so: section
_ENTRY_HEADING_RE = re.compile(r"^(~~)?([^\s:*~]*\d[^\s:*~]*)(?::[ \t]+|[ \t]+[—–][ \t]+)(.+)$")
_DATE_TOKEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SECTION_DONE_RE = re.compile(rf"(?:—|–|-|\()[ \t]*{_DONE_WORDS}\b[^)]*\)?[ \t]*$")
_TITLE_DONE_SUFFIX_RE = re.compile(rf"[ \t]*(?:—|–|-)[ \t]*{_DONE_WORDS}[ \t]*$")
_BARE_DONE_SUFFIX_RE = re.compile(rf"[ \t]*{_DONE_WORDS}\b.*$")
_BOLD_DONE_RE = re.compile(rf"\*\*{_DONE_WORDS}\b[^*]*\*\*")
_BRACKET_DONE_RE = re.compile(rf"\[{_DONE_WORDS}\]")
_DONE_PREFIX_RE = re.compile(rf"^\*\*{_DONE_WORDS}\b[^*]*\*\*:?[ \t]*")
# "- W-1.2-c — CLOSED: ..." / "CLOSED 2026-06-11 (story 1.11). ..."
_LEAD_DONE_RE = re.compile(rf"^{_DONE_WORDS}\b")
_LEAD_DONE_STRIP_RE = re.compile(
    rf"^{_DONE_WORDS}\b(?:[ \t]+\d{{4}}-\d{{2}}-\d{{2}})?(?:[ \t]*\([^)]*\))?[ \t]*[:.—–-]?[ \t]*"
)
# The flat review appender — pre-#2651 dev primitives and the attended
# `bmad-build` — writes a flat block per finding:
#   - source_spec: `spec-foo.md`
#     summary: <one sentence>
#     evidence: <why this is real>
# We recognize it so the `summary` becomes the title (not the source_spec path)
# and the entry migrates cleanly into the canonical `### DW-<seq>` shape. The
# opening line comes from the same `_FLAT_SOURCE_BODY` as FLAT_ENTRY_RE, which
# bounds canonical spans on it — see that constant for why they must not drift.
_FLAT_SOURCE_RE = re.compile(rf"^{_FLAT_SOURCE_BODY}", re.IGNORECASE)
_FLAT_SUMMARY_RE = re.compile(r"^[ \t]*summary:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)
_BULLET_RE = re.compile(r"^[-*][ \t]+(.*)$")
_ITEM_ID_RE = re.compile(
    r"^(?:\*\*)?([^\s:*~]*\d[^\s:*~]*)(?:\*\*)?(?:[ \t]*[—–][ \t]+|:[ \t]+|[ \t]+-[ \t]+)"
)
_BRACKET_TOKEN_RE = re.compile(r"\[([A-Za-z]+)[^\]]*\]")
_LEAD_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*")
_STRUCK_LINE_RE = re.compile(r"^~~(.*)~~")
_TRAIL_BRACKET_RE = re.compile(r"[ \t]*\[[^\]]+\][ \t.]*$")


def _bracket_severity(s: str) -> str | None:
    for m in _BRACKET_TOKEN_RE.finditer(s):
        sev = SEVERITY_ALIASES.get(m.group(1).lower())
        if sev:
            return sev
    return None


def _clean_title(s: str) -> str:
    return " ".join(s.replace("**", "").split())


def _item_entry(first: str, body: str, section: str, section_done: bool) -> dict:
    """Interpret one bullet item; returns the pre-key entry fields."""
    if _FLAT_SOURCE_RE.match(first):
        # flat appender block (legacy/attended era): title is the `summary`
        sm = _FLAT_SUMMARY_RE.search(body)
        summary = sm.group(1).strip() if sm else ""
        return {
            "id": "",
            "title": _clean_title(summary) if summary else _clean_title(first),
            "done": section_done,
            "severity": field_severity(body),
            "section": section,
        }
    content = first
    struck = False
    m = _STRUCK_LINE_RE.match(content)
    if m:  # "~~text~~ DONE" / "~~text~~ → resolution" on the first line
        struck = True
        content = m.group(1)
    elif content.startswith("~~") and "~~" in body[2:]:
        struck = True  # strikethrough closes on a later line
        content = content[2:]
    item_id = ""
    m = _ITEM_ID_RE.match(content)
    if m:
        item_id = m.group(1)
        content = content[m.end() :]
    done = (
        struck
        or section_done
        or bool(_LEAD_DONE_RE.match(content))
        or bool(_BOLD_DONE_RE.search(body))
        or bool(_BRACKET_DONE_RE.search(body))
    )
    content = _DONE_PREFIX_RE.sub("", content)
    content = _LEAD_DONE_STRIP_RE.sub("", content)
    while True:  # trailing "[MINOR]" / "[CLOSED]" tokens are not title text
        trimmed = _TRAIL_BRACKET_RE.sub("", content)
        if trimmed == content:
            break
        content = trimmed
    bold = _LEAD_BOLD_RE.match(content)
    if bold and len(bold.group(1).split()) >= 3:
        title = bold.group(1)  # notey: the bold phrase is the title
    else:
        title = content
    return {
        "id": item_id,
        "title": _clean_title(title),
        "done": done,
        "severity": _bracket_severity(body) or field_severity(body),
        "section": section,
    }


def _heading_entry(struck: bool, hid: str, rest: str, body: str, section: str) -> dict:
    """Interpret one '### D-1: title' entry heading (story-maker shape)."""
    title = rest
    done = struck
    m = _TITLE_DONE_SUFFIX_RE.search(title)
    if m:
        done = True
        title = title[: m.start()]
    if struck:
        title = _BARE_DONE_SUFFIX_RE.sub("", title.replace("~~", ""))
    return {
        "id": hid,
        "title": _clean_title(title),
        "done": done,
        "severity": field_severity(body) or _bracket_severity(body),
        "section": section,
    }


def parse_legacy(text: str) -> list[LegacyEntry]:
    """Extract legacy (non-DW) deferred items. Canonical DW entries and fenced
    examples are masked out first, so mixed ledgers parse both ways without
    overlap and a quoted example contributes nothing to either reading.

    The fenced half is not symmetry for its own sake. `parse_ledger` used to hand
    a quoted example over as a phantom canonical entry, whose span masked the
    example here by accident; once it stopped doing that, the same quotation
    surfaced on this side instead — a bullet or `### DW-n:` heading inside a fence
    read as a legacy finding (#514).
    """
    masked = text
    # `unclosed_hides_rest=False` for the reason the canonical side uses it: one
    # stray opener must not blank every legacy finding below it out of view. The
    # delimiter lines survive as a lone backtick or tilde plus spaces, which no
    # pattern below can start an item on — masking them too made no test disagree.
    spans = [e.span for e in parse_ledger(text)] + fenced_spans(text, unclosed_hides_rest=False)
    for s, t in spans:
        masked = masked[:s] + re.sub(r"[^\n]", " ", masked[s:t]) + masked[t:]

    found: list[tuple[dict, tuple[int, int]]] = []
    section = ""
    section_done = False
    # a done section with no items yet: emitted as its own done entry unless
    # bullets, an entry heading, or a deeper child heading claim it first
    pending: dict | None = None  # {"level", "fields", "span"}
    item: dict | None = None  # accumulating bullet or entry heading

    def close_item(end: int) -> None:
        nonlocal item
        if item is None:
            return
        body = text[item["start"] : end].rstrip()
        span = (item["start"], item["start"] + len(body))
        if item["kind"] == "item":
            fields = _item_entry(item["first"], body, item["section"], item["section_done"])
        else:
            fields = _heading_entry(
                item["struck"], item["hid"], item["rest"], body, item["section"]
            )
        found.append((fields, span))
        item = None

    def emit_pending() -> None:
        nonlocal pending
        if pending is not None:
            found.append((pending["fields"], pending["span"]))
            pending = None

    offset = 0
    for line in text.splitlines(keepends=True):
        masked_line = masked[offset : offset + len(line)].rstrip("\n")
        hm = _LINE_HEADING_RE.match(masked_line)
        if hm:
            level = len(hm.group(1))
            close_item(offset)
            if level == 1:
                emit_pending()
                section, section_done = "", False
            elif level in (2, 3):
                if pending is not None and level > pending["level"]:
                    pending = None  # a child heading: the parent is structure
                else:
                    emit_pending()
                em = _ENTRY_HEADING_RE.match(hm.group(2))
                if em and _DATE_TOKEN_RE.fullmatch(em.group(2)):
                    em = None  # "## 2026-06-09 — ..." is a dated section
                if em:
                    pending = None
                    item = {
                        "kind": "heading",
                        "start": offset,
                        "struck": bool(em.group(1)),
                        "hid": em.group(2),
                        "rest": em.group(3),
                        "section": section,
                        "section_done": section_done,
                    }
                else:
                    htext = hm.group(2)
                    struck = htext.startswith("~~") and "~~" in htext[2:]
                    section = _clean_title(htext.replace("~~", ""))
                    section_done = struck or bool(_SECTION_DONE_RE.search(htext))
                    if section_done:
                        pending = {
                            "level": level,
                            "span": (offset, offset + len(line.rstrip("\n"))),
                            "fields": {
                                "id": "",
                                "title": section,
                                "done": True,
                                "severity": None,
                                "section": "",
                            },
                        }
            offset += len(line)
            continue
        if item is not None and item["kind"] == "heading":
            if masked_line.strip() == "---" or (masked_line.strip() == "" and line.strip() != ""):
                close_item(offset)  # rule, or a masked canonical entry
            offset += len(line)
            continue
        bm = _BULLET_RE.match(masked_line)
        if bm:
            close_item(offset)
            pending = None
            item = {
                "kind": "item",
                "start": offset,
                "first": bm.group(1),
                "section": section,
                "section_done": section_done,
            }
        elif masked_line.strip() in ("", "---"):
            # a masked canonical entry reads as blank: it still bounds the item
            if masked_line.strip() == "---" or line.strip() != masked_line.strip():
                close_item(offset)
        elif masked_line[0] in " \t":
            pass  # indented continuation of the current item
        else:
            close_item(offset)  # column-0 prose ends an item, emits nothing
        offset += len(line)
    close_item(len(text))
    emit_pending()

    entries: list[LegacyEntry] = []
    counts: dict[str, int] = {}
    for fields, span in found:
        base = hashlib.sha1(
            f"{fields['section']}\0{fields['id'] or fields['title']}".encode(),
            usedforsecurity=False,  # display/identity key, not a credential
        ).hexdigest()[:10]
        n = counts.get(base, 0) + 1
        counts[base] = n
        entries.append(
            LegacyEntry(
                key=base if n == 1 else f"{base}-{n}",
                id=fields["id"],
                title=fields["title"],
                done=fields["done"],
                severity=fields["severity"],
                body=text[span[0] : span[1]],
                section=fields["section"],
                span=span,
            )
        )
    return entries


def has_legacy(text: str) -> bool:
    return bool(parse_legacy(text))
