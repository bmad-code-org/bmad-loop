"""Pure spec-frontmatter parsing: read the YAML ``---``…``---`` block, normalize
the status token, and rewrite ``status:`` in place.

No git dependency, so a pure domain module can read spec status without importing
``verify`` and dragging in its whole git surface (assessment finding F-1).
``stories`` still collects on that: ``import bmad_loop.stories`` genuinely leaves
``bmad_loop.verify`` out of ``sys.modules``. ``devcontract`` no longer does — it
imports ``verify`` directly for `read_frontmatter` and `operator_actions_of` — so
the rule now has one beneficiary, not the two it was written for. ``verify``
re-exports these names either way, so every existing ``verify.<name>`` /
``from .verify import <name>`` call site stays valid.

That rule is about ``verify``, NOT about ``subprocess``: this module imports
``platform_util`` (#379), which pulls ``subprocess`` in, and both named modules
already paid that cost anyway — ``devcontract`` imports it directly and
``stories`` reaches it through ``deferredwork``. So the writes here are atomic as
well as byte-verbatim: ``read_bytes().decode`` in, ``atomic_write_bytes`` out.
The byte path is load-bearing on its own — ``read_text``'s universal-newline
translation would hand the writer an all-LF copy of a CRLF spec, and every line
ending in the file would be relaid on the way back out.

READER AND WRITER DEGRADE IN OPPOSITE DIRECTIONS, deliberately. `read_frontmatter`
turns an unparseable or undecodable block into ``{}``: it runs on the observation
path, where the orchestrator's job is to classify what it finds, and every status
gate then reads ``""`` and answers with a clean retry. `set_frontmatter_status`
runs on the *repair* path and does the opposite — when it can see a status it
cannot safely rewrite, it RAISES `FrontmatterWriteError`. That is AGENTS.md's
"observation may degrade, repair writes must raise", and it is what makes a
``False`` return mean one thing only: there was nothing to change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .platform_util import atomic_write_bytes, atomic_write_bytes_confined


def _split_frontmatter(text: str) -> tuple[str, str, str] | None:
    """Split a document into ``(before, block, after)`` around its YAML
    frontmatter, where ``before + block + after == text`` exactly.

    The opening and closing ``---`` are recognized ONLY as standalone delimiter
    lines (``line.rstrip() == "---"``), so a ``---`` substring inside a scalar
    value (e.g. ``title: 'restore --- review'``) is never mistaken for the
    closing boundary — the flaw a plain ``text.split("---", 2)`` has. ``before``
    is the opening delimiter line, ``block`` is the YAML content between the
    delimiters, and ``after`` begins with the closing delimiter line; callers
    rewrite ``block`` and reconstruct the file byte-for-byte. Returns ``None``
    when the text has no opening delimiter line or no closing delimiter line.
    """
    lines = text.splitlines(keepends=True)  # "".join(lines) == text
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return lines[0], "".join(lines[1:i]), "".join(lines[i:])
    return None


def read_frontmatter(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # A non-UTF-8 file carries no readable frontmatter — degrade exactly like
        # unparseable YAML below. Every status gate then reads status "" and
        # returns a clean retry/repair outcome instead of crashing mid-verify
        # (UnicodeDecodeError is a ValueError, so it slipped past callers'
        # except-OSError guards).
        return {}
    split = _split_frontmatter(text)
    if split is None:
        return {}
    try:
        doc = yaml.safe_load(split[1])
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def status_of(fm: dict[str, Any]) -> str:
    """Normalized spec status from a frontmatter dict: stripped + lowercased.

    The single point all spec-frontmatter status gates read through, so casing
    never decides a gate — the spec template and sprint-status tokens are
    lowercase, so a stray ``Done``/``In-Review`` from a hand-edited spec still
    matches. (``devcontract`` reads its frontmatter status through here too; the
    separate lowercasing it keeps is for the skill-written *prose* ``Status:``
    line, where casing genuinely varies.)

    A YAML-null status (a bare ``status:`` line, or ``status: null``) reads as
    ``""`` — the same as a missing key — because a spec template may legitimately
    leave the value blank (see the comment on ``devcontract._FM_STATUS_RE``, whose
    writer side fills exactly that shape). Without the guard ``str(None)`` would
    make it the token ``"none"``, which every allowlist then treats as a
    deliberate custom status (#358). A *literal* ``status: none`` is the string
    ``"none"`` — PyYAML resolves only ``~``/``null``/``Null``/``NULL``/empty as
    null — so a hand-written token still reads back exactly as written.
    """
    raw = fm.get("status", "")
    return ("" if raw is None else str(raw)).strip().lower()


def operator_actions_of(fm: dict[str, Any]) -> tuple[str, ...]:
    """The external, human-only actions a spec's ``operator_actions:`` frontmatter
    declares — normalized, order-preserving, deduped.

    Lives here rather than in ``devcontract`` because both sides of the park
    contract need the same reading and ``verify`` cannot import ``devcontract``
    (the dependency runs the other way).

    Strict about the container, lenient about each scalar item — the
    ``closes_deferred`` reading (:func:`deferredwork.parse_declaration`), for the
    same reason: a bare ``operator_actions: buy the domain`` is iterable, so a
    lenient container reading would silently turn one instruction into a list of
    characters. Items that are themselves containers are dropped rather than
    stringified: ``[{action: ..., check: ...}]`` is the deliberate v2 shape (a
    per-action verification command), and ``str()``-ing it would hand a human a
    line of Python repr as their instruction. ``None`` items drop for the same
    reason — ``str(None)`` is the word "None", not an action.

    Every malformed shape therefore collapses to ``()``, which the verify gates
    read as "declared nothing" and answer with one fixable retry naming the
    expected shape — a park is *defined* by owing at least one action, so an
    empty reading can never be mistaken for a valid park.
    """
    raw = fm.get("operator_actions")
    if not isinstance(raw, list):
        return ()
    items = (str(x).strip() for x in raw if x is not None and not isinstance(x, (list, dict)))
    return tuple(dict.fromkeys(a for a in items if a))


# The two frontmatter keys a bmad-build-auto spec can carry a dev baseline under,
# in precedence order. `baseline_revision` is what the skill's step-03 actually
# stamps; `baseline_commit` is the legacy spelling (the name the orchestrator's
# synthesized result.json uses) kept readable for specs written before the rename.
_BASELINE_KEYS = ("baseline_revision", "baseline_commit")


def auto_dev_baseline_of(fm: dict[str, Any]) -> str:
    """The dev baseline a bmad-build-auto spec CLAIMS: the first non-empty value
    among ``baseline_revision`` then ``baseline_commit``, stripped; ``""`` when the
    spec claims neither.

    Deliberately not the bare ``baseline_of`` the siblings' naming would suggest.
    ``status_of`` and ``operator_actions_of`` are field-generic — they read one key
    and normalize it — whereas this precedence belongs to the bmad-build-auto
    contract specifically: the skill stamps ``baseline_revision``, the orchestrator's
    own result.json says ``baseline_commit``, and a spec re-armed by
    ``runs.rearm_escalation`` can carry both. A bare ``baseline_of`` would read as
    universal when it is not (#716).

    ``baseline_revision`` WINS whenever it is non-empty, even against a
    ``baseline_commit`` that would have matched. Both consumers — the dev
    devcontract's synthesized result and verify's baseline-match gate — read
    through here so the two cannot drift, and the two byte-identical
    ``fm.get("baseline_commit", fm.get("baseline_revision", ""))`` expressions
    this replaces had the precedence the other way round. That flip is deliberate
    and tightening: the legacy key is a leftover the re-arm never removes, so
    ranking it first let a stale sha silently outrank the fresh value the skill
    had just written, killing the gate on an attempt that did everything right.

    An EMPTY legacy key is skipped rather than returned. ``dict.get``'s default
    only fires on a MISSING key, so ``baseline_commit: ''`` used to be selected and
    yield ``""`` — which every consumer reads as "no claim" and which therefore
    disabled the baseline-match gate outright.

    A YAML-null value (a bare ``baseline_commit:`` line, or ``: null``) is treated
    as absent for the same reason ``status_of`` guards it: ``str(None)`` is the
    token ``"None"``, which is not a sha but IS non-empty, so it would flow into
    the gate as a claim and fail an attempt that never made one (#358).
    """
    for key in _BASELINE_KEYS:
        raw = fm.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            return value
    return ""


class FrontmatterWriteError(Exception):
    """A frontmatter block carries a key the reader can see but no minimal line
    edit can safely rewrite.

    Raised, not returned, because ``False`` already means "nothing to change" and
    no caller reads the return value — a ``False`` refusal would be invisible at
    every call site, which is the failure this exists to stop rather than
    relocate. `runs.rearm_escalation` translates it to `RearmError` (already
    surfaced by the CLI and the TUI); `cli.cmd_confirm` catches it and names the
    recoverable state; anything else lands on `cli.main`'s backstop as a clean
    one-liner."""


# A frontmatter ``<key>:`` line. Anchored, so only indentation may precede it —
# a `#` comment line and a `- ` list item are structurally excluded rather than
# excluded by a special case. Matching quotes around the key (`"status": x`),
# and whitespace before the colon (`status : x`), because YAML accepts both and
# the old `lstrip().startswith("status:")` scan silently skipped them. The
# `status_note:` exclusion is structural too: after the key the pattern demands
# a quote-or-whitespace-or-colon, and `_` is none of those.
def _key_line_re(key: str) -> re.Pattern[str]:
    return re.compile(rf"^[ \t]*(?P<q>['\"]?){re.escape(key)}(?P=q)[ \t]*:")


_STATUS_KEY_RE = _key_line_re("status")

# Builds the replacement for a matched key line, line ending included. A hook
# rather than one fixed rendering because the two writers on this helper preserve
# deliberately different things: `_replace_value` drops the value's quotes (its
# callers read the result back as a bare `status: done`), while
# `devcontract.reset_spec_status` keeps them (its own tests pin them). Both carry
# a trailing inline comment through. What they share is the VERIFICATION, which
# is the part that was wrong in all three writers — not the formatting, which
# each already had right.
LineRenderer = Callable[[str, "re.Match[str]", str], str]


# What follows a key line's colon when the remainder is a bare scalar token and
# nothing else but a trailing inline comment. Only `sep` and `comment` are data;
# `q` and `val` are GATES — they certify that the scalar ends exactly where the
# render assumes it does, which is the one fact a `#` on the line cannot tell you
# by itself.
#
# NEVER WIDEN `val` TO `[^#]*?`. `_verified` cannot backstop this. Against
# `status: "a # b"` a widened pattern matches with `val` = `"a` and renders
# `status: done # b"` — which `yaml.safe_load` reads as a clean `status: done`,
# because it strips comments BEFORE the oracle compares. All three `_verified`
# gates pass and a fabricated comment lands in the spec. The conservative token
# class is therefore the gate and `_verified` is only the backstop, the reverse
# of everywhere else in this module. `[A-Za-z0-9._-]` is every shape a status
# token and the ordinary scalar frontmatter values have; anything richer than
# that falls back to the full-drop render, which is merely lossy, never wrong.
#
# `sprintstatus._set_mapping_value` solves the same problem with a wider value
# class than this one accepts, and the asymmetry is deliberate rather than an
# oversight: it writes the orchestrator-owned board, whose `last_updated` is a
# bare scalar WITH SPACES that this token class would refuse outright, while this
# writes a hand-authored spec where a value richer than a token is exactly the
# shape whose boundary cannot be trusted. Since #366 the two agree on the part
# that matters — neither guesses where a QUOTED scalar ends — and they part only
# on how much of an unquoted one they will read. Do not "unify" them by widening
# this one back; the token class is this side's whole gate.
_VALUE_COMMENT_RE = re.compile(
    r"^[ \t]*(?P<q>['\"]?)(?P<val>[A-Za-z0-9._-]*)(?P=q)(?P<sep>[ \t]+)(?P<comment>#.*)$"
)


def _replace_value(line: str, m: re.Match[str], value: str) -> str:
    """Keep everything through the colon verbatim — indent, a quoted key,
    whitespace before the colon — then ``<space><value>``, then any trailing
    inline comment the old value carried, then THIS LINE'S OWN terminator.

    The gap after the colon is normalized rather than preserved because a bare
    ``status:`` has none to preserve, and filling it would write ``status:done``,
    which is not the key at all.

    The comment is carried only when `_VALUE_COMMENT_RE` can certify where the
    scalar ends, and its separating whitespace comes through as authored. When it
    cannot — a quoted value containing a ``#``, a ``#`` abutting the scalar
    (``done#x``, where it is part of the value, not a comment), a value richer
    than a bare token — the whole remainder is dropped, which is what this
    renderer did for every input before. Lossy, never wrong.

    The value's own quotes are still dropped, and that non-preservation is
    load-bearing rather than pending: `conftest.write_spec` writes
    ``status: '<v>'`` and three tests read the result back as an unquoted
    ``status: done`` (tests/test_runs.py, tests/test_stories_e2e.py).

    The terminator is carried rather than re-emitted as ``"\\n"``: a CRLF spec
    would otherwise come back with exactly one bare-LF line — the status line the
    writer was asked to touch — leaving the file mixed. ``rstrip`` is safe here
    because the caller splits with ``splitlines(keepends=True)``, so a line
    carries at most one terminator; ``\\r\\n``, ``\\n``, a bare ``\\r`` and a
    final line with no terminator at all each round-trip as authored."""
    stripped = line.rstrip("\r\n")
    nl = line[len(stripped) :]
    cm = _VALUE_COMMENT_RE.match(stripped[m.end() :])
    if cm is not None:
        return f"{line[: m.end()]} {value}{cm['sep']}{cm['comment']}{nl}"
    return f"{line[: m.end()]} {value}{nl}"


def _last_line_ending(block: str) -> str:
    """The terminator of ``block``'s last line — what an INSERTED line must carry
    so an append does not introduce a foreign ending into the file.

    The same per-line reading `_replace_value` does, for the branch that has no
    line to copy from: an inserted key goes directly after the block's last line,
    so that line's ending is the one it should share. Falls back to ``"\\n"`` for
    an empty block (``---``/``---`` with nothing between) and for a last line
    carrying no terminator at all — neither can happen through
    `_split_frontmatter`, whose block is always followed by the closing delimiter
    line, but an appended key that started on the previous line would be a
    corruption rather than a formatting nit, so it is guarded rather than
    reasoned about."""
    lines = block.splitlines(keepends=True)
    if not lines:
        return "\n"
    last = lines[-1]
    return last[len(last.rstrip("\r\n")) :] or "\n"


def _edit_frontmatter_block(
    block: str,
    key: str,
    value: str,
    *,
    pattern: re.Pattern[str] | None = None,
    render: LineRenderer = _replace_value,
    insert: bool = False,
) -> str | None:
    """Rewrite ``<key>:`` inside a frontmatter block, verifying the edit MEANS
    what it was supposed to mean. Returns the new block, None when there is
    nothing to change, and raises `FrontmatterWriteError` otherwise.

    Enumerating the shapes a line scan must not touch is a losing game — a flow
    mapping, a block scalar, a value continued on the next line, an anchor
    another key aliases, a nested key of the same name, the key quoted inside
    ANOTHER key's literal block. Each of those had the old scanner either write
    nothing or corrupt the spec, and every enumeration written for this (mine and
    both reviewers') missed shapes the next one caught.

    So this does not widen a pattern. It makes the trial edit, re-parses it with
    ``yaml.safe_load`` as an ORACLE, and keeps it only if the block still parses
    as a mapping, its top-level ``key`` is exactly ``value``, and **every other
    key is unchanged**. YAML is never used as a serializer — the edit stays the
    formatting-preserving single-line replacement — so one gate replaces six and
    it also rejects shapes nobody has enumerated. The other-keys comparison is
    the half no pattern-widening design has: it is what catches an edit with the
    right effect on ``key`` and a wrong effect somewhere else.

    Candidates are ITERATED, not broken on at the first match. That is what fixes
    the wrong-target write: a decoy line inside another key's literal block fails
    verification and the real key is still reached.

    The parse is of the block the READER would see, so the two agree on what is
    there. An unparseable block raises rather than returning None: the reader
    degrades it to ``{}`` because observation may, but a writer that concluded
    "no status here" from a block it could not read would report success for a
    spec it never touched.

    ``insert`` adds the key as the block's last line when the reader sees no such
    top-level key — what `verify.set_frontmatter_field` and
    `devcontract.reset_spec_status` need and `set_frontmatter_status` must never
    do. It is gated on the READER's view rather than on a scan miss, which is the
    other half of the same defect: a scan that missed a quoted key then appended
    a SECOND one, and the file ended up with two. The inserted line takes the
    ending of the line it follows (`_last_line_ending`), so an insert cannot
    introduce a foreign line ending any more than a replacement can."""
    pattern = _key_line_re(key) if pattern is None else pattern
    try:
        original = yaml.safe_load(block)
    except yaml.YAMLError as e:
        raise FrontmatterWriteError(
            f"the frontmatter block does not parse as YAML, so a {key!r} edit "
            f"cannot be verified ({e.__class__.__name__}: {e})"
        ) from e
    rest = {k: v for k, v in original.items() if k != key} if isinstance(original, dict) else {}
    if not isinstance(original, dict) or key not in original:
        if not insert:
            return None  # the reader sees no such top-level key — nothing to change
        return _verified(block + f"{key}: {value}{_last_line_ending(block)}", key, value, rest)
    if original[key] == value:
        return None  # already at the target — idempotent no-op, no write
    lines = block.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m is None:
            continue
        trial = list(lines)
        trial[i] = render(line, m, value)
        candidate = _verified("".join(trial), key, value, rest)
        if candidate is not None:
            return candidate
    raise FrontmatterWriteError(
        f"the frontmatter carries {key!r} in a shape no in-place line edit can "
        f"safely rewrite to {value!r} (a flow mapping, a block scalar, a value "
        f"continued on the next line, or an anchor another key aliases) — set it "
        f"as a plain `{key}: <value>` line and re-run"
    )


def _verified(candidate: str, key: str, value: str, rest: dict[str, Any]) -> str | None:
    """``candidate`` if it means what the edit intended, else None.

    Three conditions, and the third is the one no pattern-widening design has:
    the block still parses as a mapping, its top-level ``key`` is exactly
    ``value``, and every OTHER key is unchanged. Without the last one an edit
    with the right effect on ``key`` and a wrong effect elsewhere passes — a
    ``status`` merged in from an anchor block is only reachable by rewriting the
    anchor, which is shared state the story does not own."""
    try:
        parsed = yaml.safe_load(candidate)
    except yaml.YAMLError:
        return None  # the edit broke the block — this line is not a scalar key
    if not isinstance(parsed, dict) or parsed.get(key) != value:
        return None  # edited something that is not the key the reader resolves
    if {k: v for k, v in parsed.items() if k != key} != rest:
        return None  # right key, collateral damage — e.g. another key's block
    return candidate


def set_frontmatter_status(path: Path, status: str, *, confine_root: Path) -> bool:
    """Rewrite the `status:` field in a spec's `---`…`---` frontmatter block.

    A minimal in-place line replacement (not a YAML round-trip) so the spec's
    formatting, comments, and field order survive — only the status value
    changes, and the edit is verified by re-parsing before it lands (see
    `_edit_frontmatter_block`).

    That includes the file's LINE ENDINGS, every one of them: the read is
    ``read_bytes().decode`` and the write is ``write_bytes``, so a CRLF spec
    stays CRLF and a mixed-ending spec keeps each line's own terminator. Going
    through ``read_text``/``write_text`` instead relaid the whole file — CRLF in,
    LF out on POSIX, and every LF out as CRLF on Windows — which is the largest
    violation a writer contracted to "only the status value changes" can commit.

    The rewrite is also atomic (#379): the bytes go out through
    `platform_util.atomic_write_bytes`, which is byte-verbatim on the same terms
    as `write_bytes` and additionally leaves either the old file or the whole new
    one. That matters most here, where the layout is ``before + edited + after``
    — a truncating write that faults after the frontmatter has landed leaves
    intact frontmatter saying ``status: done`` over a decapitated body, a spec
    that lies and that the loop then commits. `devcontract._atomic_write_spec`
    reached the same conclusion on the same files, with fault injection: it cut a
    46-byte spec to 12.

    The write is CONFINED, and this is the canonical statement of the rule the
    three spec writers share (`verify.set_frontmatter_field` and
    `devcontract._atomic_write_spec` restate it by reference):

    * A spec path under ``confine_root`` goes through
      `platform_util.atomic_write_bytes_confined`, which walks the components
      below that root ``O_NOFOLLOW`` and writes through the descriptor the walk
      produced. Refusing a link at the FINAL component was never enough (#593):
      `mkstemp(dir=...)` and `os.replace`'s destination still looked every
      DIRECTORY above the spec up by name, so a link planted at the artifacts
      folder landed both the temp and the published spec wherever it pointed —
      and the callers' own ``mkdir(parents=True, exist_ok=True)`` accepts a
      symlinked directory, so a planted parent survives the setup step.
    * A spec path OUTSIDE ``confine_root`` keeps the plain no-follow write. An
      artifacts folder configured outside the checkout is real, supported
      configuration (`bmadconfig` resolves one; `verify.spec_within_roots` trusts
      it), and a confined writer cannot vouch for a tree it was not given — so
      refusing there would break working setups rather than close a hole.

    ``follow_symlinks=False`` on that second arm matches the name-replacing
    `atomic_replace` this writer's `devcontract` sibling always had. A spec path
    is handed to this writer from a session-driven scan, so honouring a link
    planted there would aim a host-side write wherever that session chose. The
    confined arm needs no such flag — it never follows anything.

    ``confine_root`` is a REQUIRED keyword: a caller that has not decided which
    checkout the spec belongs to is a pyright error rather than an unconfined
    write, which is how every call site of this and its two siblings was found.

    ``require_writable_target=True`` on both arms (#597): a spec is
    operator-editable, and a temp-and-replace write needs write permission on the
    PARENT DIRECTORY, never on the entry it replaces — so before this a spec an
    operator had marked ``0444`` was rewritten anyway and, where the mode was
    inherited, came back reading ``0444`` with nothing in the permission bits to
    record it. The kernel's `PermissionError` is what a bare ``write_bytes``
    raised, and it is what this raises again.

    Returns True when the file was rewritten. Returns False for **nothing to
    change** only: no file, no frontmatter block, no top-level `status` for
    `read_frontmatter` to see, or already at the target. Raises
    `FrontmatterWriteError` when the reader CAN see a status the edit cannot
    safely move — `False` never means "I failed".

    A trailing inline comment on the status line survives too, when the render
    can certify where the scalar ends (`_VALUE_COMMENT_RE`); a value it cannot
    read as a bare token drops the comment rather than guessing at one.

    ONE deliberate non-preservation remains, pinned in tests/test_frontmatter.py:
    the value's own quotes are dropped (`status: 'x'` -> `status: done`), because
    the standard fixture shape is quoted and callers read the result back
    unquoted.
    """
    if not path.is_file():
        return False
    text = path.read_bytes().decode("utf-8")
    split = _split_frontmatter(text)
    if split is None:
        return False
    before, block, after = split
    edited = _edit_frontmatter_block(block, "status", status, pattern=_STATUS_KEY_RE)
    if edited is None:
        return False
    payload = (before + edited + after).encode("utf-8")
    if path.is_relative_to(confine_root):
        atomic_write_bytes_confined(
            path, payload, confine_root=confine_root, require_writable_target=True
        )
    else:
        atomic_write_bytes(path, payload, follow_symlinks=False, require_writable_target=True)
    return True
