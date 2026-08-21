"""Model of sprint-status.yaml — the single source of workflow truth.

The dev primitive `bmad-build-auto` deliberately does not touch sprint-status
("the orchestrator's business"), so the orchestrator is the single writer via
:func:`advance` — idempotent, never-regress, epic-lift. The orchestrator
otherwise only re-reads this file to pick the next story and verify what a
session claims, so the no-races invariant holds.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from .platform_util import atomic_write_bytes

EPIC_RE = re.compile(r"^epic-(\d+)$")
RETRO_RE = re.compile(r"^epic-(\d+)-retrospective$")
RETRO_ITEM_RE = re.compile(r"^epic-(\d+)-retro-item-(\d+)-(.+)$")
# The story number may carry a single lowercase split suffix (2-6a / 2-6b —
# the shape BMAD produces when an oversized story is split, see issue #144).
STORY_RE = re.compile(r"^(\d+)-(\d+)([a-z]?)-(.+)$")
SHORT_REF_RE = re.compile(r"^(\d+)[-.](\d+)([a-z]?)$")  # short story ref: 3-1, 3.1, 3-1a
BARE_NUM_RE = re.compile(r"^(\d+)([a-z]?)$")  # a lone story number, needs --epic

# Lifecycle order, earliest -> latest. `advance` never moves a story backward
# through this sequence (matches sync-sprint-status's "never regress"), and it is
# the only ordering any caller may use — a token absent from it cannot be ordered
# at all, so every consumer treats "unknown" conservatively rather than guessing.
# `awaiting-operator` sits immediately before `done`: parking is the last stop on
# the way to finished, so confirming a parked story is a legal forward advance
# through the sole writer, while nothing can ever regress `done` back into it.
STATUS_ORDER = (
    "backlog",
    "ready-for-dev",
    "in-progress",
    "review",
    "awaiting-operator",
    "done",
)
LEGACY_STORY_STATUSES = {"drafted": "ready-for-dev"}
# Statuses a story may be PICKED UP from. `awaiting-operator` is deliberately
# absent: the story's agent-doable work is already committed, so re-driving it
# would redo finished work while the human's external actions stay outstanding.
ACTIONABLE_STATUSES = {"backlog", "ready-for-dev"}


class SprintStatusError(Exception):
    pass


@dataclass(frozen=True)
class Story:
    key: str
    epic: int
    num: int
    slug: str
    status: str
    suffix: str = ""  # split-story letter ("a" in 2-6a), "" for a whole story


@dataclass(frozen=True)
class RetroItem:
    """A retrospective action item tracked in sprint-status under the
    RETRO ACTION ITEMS section: ``epic-{epic}-retro-item-{num}-{slug}``.

    Recognized so they no longer fall into ``unknown_keys``; the orchestrator
    does not yet drive them as work (see roadmap: retro-item automation).
    """

    key: str
    epic: int
    num: int
    slug: str
    status: str


@dataclass(frozen=True)
class SprintStatus:
    path: Path
    epics: dict[int, str]
    stories: tuple[Story, ...]
    retros: dict[int, str]
    retro_items: tuple[RetroItem, ...]
    unknown_keys: tuple[str, ...]


def load(path: Path) -> SprintStatus:
    if not path.is_file():
        raise SprintStatusError(f"sprint status file not found: {path}")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SprintStatusError(f"sprint status is not valid YAML: {path}: {e}") from e
    if not isinstance(doc, dict):
        raise SprintStatusError(f"sprint status has no top-level mapping: {path}")
    dev = doc.get("development_status")
    if not isinstance(dev, dict):
        raise SprintStatusError(f"sprint status missing development_status map: {path}")

    epics: dict[int, str] = {}
    stories: list[Story] = []
    retros: dict[int, str] = {}
    retro_items: list[RetroItem] = []
    unknown: list[str] = []
    for key, raw_status in dev.items():
        key = str(key)
        status = str(raw_status).strip()
        if m := RETRO_ITEM_RE.match(key):
            retro_items.append(
                RetroItem(
                    key=key,
                    epic=int(m.group(1)),
                    num=int(m.group(2)),
                    slug=m.group(3),
                    status=status,
                )
            )
        elif m := RETRO_RE.match(key):
            retros[int(m.group(1))] = status
        elif m := EPIC_RE.match(key):
            epics[int(m.group(1))] = status
        elif m := STORY_RE.match(key):
            status = LEGACY_STORY_STATUSES.get(status, status)
            stories.append(
                Story(
                    key=key,
                    epic=int(m.group(1)),
                    num=int(m.group(2)),
                    slug=m.group(4),
                    status=status,
                    suffix=m.group(3),
                )
            )
        else:
            unknown.append(key)

    return SprintStatus(
        path=path,
        epics=epics,
        stories=tuple(stories),
        retros=retros,
        retro_items=tuple(retro_items),
        unknown_keys=tuple(unknown),
    )


def next_actionable(
    ss: SprintStatus, skip: set[str] | None = None, *, epic: int | None = None
) -> Story | None:
    """First story in file order whose status allows starting work. When
    ``epic`` is given, only stories of that epic are considered — the caller
    uses this to exhaust the current epic before advancing to another."""
    skip = skip or set()
    for story in ss.stories:
        if story.key in skip:
            continue
        if epic is not None and story.epic != epic:
            continue
        if story.status in ACTIONABLE_STATUSES:
            return story
    return None


def story_status(path: Path, key: str) -> str | None:
    """Fresh re-read of one story's status, for post-session verification."""
    ss = load(path)
    for story in ss.stories:
        if story.key == key:
            return story.status
    return None


# Stage 2 of the value/comment split, applied to the remainder after the key's
# colon and its gap. Which one runs is decided by the remainder's FIRST
# character, because that is the only place the scalar's own boundary is
# knowable from a line edit: a quote opens a scalar that owns every `#` to its
# right, an unquoted scalar cedes the first whitespace-preceded one.
#
# `_QUOTED_VALUE_RE` recognizes NO comment (there is no `rest` group to carry):
# the whole remainder is the value. `_UNQUOTED_VALUE_RE`'s `val` is lazy, so the
# FIRST ` #` wins rather than the last — the split is where YAML puts it, not
# wherever the line happens to end. Both arms demand a trailing `\S`, so a line
# carrying anything after the value that neither arm can account for — trailing
# whitespace, no comment — is refused whole rather than silently rewritten
# without it. Line terminators are excluded before either scalar matcher runs
# and carried separately per line, so CRLF's `\r` is never mistaken for trailing
# scalar whitespace (#576).
_QUOTED_VALUE_RE = re.compile(r"^(?P<val>['\"](?:.*\S)?)$")
_UNQUOTED_VALUE_RE = re.compile(r"^(?P<val>\S(?:.*?\S)?)(?P<rest>[ \t]+#.*)?$")


def _set_mapping_value(lines: list[str], key: str, new_value: str) -> bool:
    """In-place replace the value of the first `key:` line, preserving
    indentation and any trailing ` # comment`. Returns True on a real change. A
    minimal line edit (not a YAML round-trip) so the file's comments and
    structure — STATUS DEFINITIONS, WORKFLOW NOTES — survive verbatim.

    The split between value and comment is two-stage: the key prefix is matched
    first and the whole remainder captured, then that remainder decides for
    itself. An unquoted value keeps the wide class this board needs — it
    legitimately contains spaces (`last_updated: 01-06-2026 10:00`), which is why
    it cannot borrow `frontmatter._VALUE_COMMENT_RE`'s conservative token gate —
    and cedes an inline comment only at whitespace, as YAML does. A remainder
    that OPENS WITH A QUOTE is taken whole and no comment is recognized in it at
    all: a fused pattern would guess the boundary from the last ` #` on the line
    and turn `status: "a # b"` into `status: done # b"`, promoting scalar text
    into a comment the board never had (#366). Nothing here can tell where a
    quoted scalar ends — the closing quote may be escaped, or on another line —
    so a comment sitting after one is dropped rather than guessed at. Lossy,
    never wrong, and only a hand-edit reaches it: the writer replaces such a
    value with a bare token on the next advance.

    A remainder neither arm can read leaves the line alone, exactly like a key
    that never matched — `advance` reports the unchanged status rather than
    claiming a write it did not make. Each line's terminator is excluded from
    the scalar match and then reattached exactly as authored (#576)."""
    key_pat = re.compile(rf"^(?P<indent>\s*){re.escape(key)}:(?P<gap>[ \t]+)(?P<body>\S.*)$")
    for i, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        m = key_pat.match(stripped)
        if not m:
            continue
        body = m.group("body")
        value_pat = _QUOTED_VALUE_RE if body[0] in "'\"" else _UNQUOTED_VALUE_RE
        vm = value_pat.match(body)
        if not vm:
            continue  # unreadable remainder — leave the line as authored
        if vm.group("val") == new_value:
            return False  # already at target — idempotent no-op
        rest = vm.groupdict().get("rest") or ""
        nl = line[len(stripped) :]
        lines[i] = f"{m.group('indent')}{key}:{m.group('gap')}{new_value}{rest}" + nl
        return True
    return False


def advance(path: Path, story_key: str, target: str, *, now: str | None = None) -> str | None:
    """Advance a story's sprint-status to `target` for the generic-skill path.

    Mirrors sync-sprint-status.md: skip when the file is missing or the story is
    absent (returns None); never regress (returns the current status unchanged
    when it is already at or past `target` in STATUS_ORDER); lift a `backlog`
    parent epic to `in-progress` only when advancing a story to `in-progress`;
    refresh `last_updated` when `now` is given. Comments/structure are preserved
    via line edits. Returns the story's status after the call (== `target` on a
    write), or None when nothing was eligible.

    The rewrite is atomic and symlink-following (#379), and every existing CRLF,
    LF, bare CR, or mixed per-line terminator is preserved (#576). The board is
    read as raw UTF-8 bytes, each edited line carries its own terminator, and
    `atomic_write_bytes` publishes the byte-exact result. This is a
    read-modify-rewrite of the board, and a truncating write that faults partway
    through corrupts it SILENTLY: YAML cut at a line boundary is still a valid
    mapping, just a smaller one, so the epics past the tear cease to exist rather
    than raising. AGENTS.md makes this the orchestrator's sole write path to
    sprint-status.yaml, so nothing downstream would contradict the shortened
    board — the run would simply walk off the end of the sprint. The atomic
    helper keeps the file entire: either the old contents or the whole new ones,
    never a prefix.

    Symlinks are FOLLOWED (the helper's default), which is what the old
    truncating write did too — the board is an operator-curated file at a
    project-relative path, and a repo that symlinks it somewhere must keep being
    a symlink.
    """
    if not path.is_file():
        return None
    current = story_status(path, story_key)
    if current is None:
        return None
    if (
        current in STATUS_ORDER
        and target in STATUS_ORDER
        and STATUS_ORDER.index(current) >= STATUS_ORDER.index(target)
    ):
        return current  # already at or past target — never regress

    text = path.read_bytes().decode("utf-8")
    lines = text.splitlines(keepends=True)
    # story_status() resolves keys via a full YAML parse, but _set_mapping_value
    # rewrites via a line regex that can't touch every shape it finds (quoted or
    # block-scalar keys). If the story line itself wasn't rewritten, report the
    # unchanged status rather than falsely claiming we advanced to target.
    story_changed = _set_mapping_value(lines, story_key, target)
    if not story_changed:
        return current
    changed = story_changed

    if target == "in-progress":
        m = STORY_RE.match(story_key)
        if m:
            epic_key = f"epic-{int(m.group(1))}"
            ss = load(path)
            if ss.epics.get(int(m.group(1))) == "backlog":
                changed = _set_mapping_value(lines, epic_key, "in-progress") or changed

    if now is not None:
        changed = _set_mapping_value(lines, "last_updated", now) or changed

    if changed:
        atomic_write_bytes(path, "".join(lines).encode("utf-8"))
    return target


def advanced_bytes(source: bytes, story_key: str, target: str) -> bytes | None:
    """What :func:`advance` would leave behind, given ``source`` as the board's bytes.

    For the caller that has to know whether a board on disk holds THIS run's advance
    and nothing else, and so needs the intended content recomputed from a baseline it
    trusts rather than read back out of the file it is about to commit.

    Goes through ``advance`` itself, against a throwaway copy, rather than
    reimplementing the edit. Never-regress, the epic lift, and
    ``_set_mapping_value``'s quoted-scalar, inline-comment and per-line-terminator
    handling ARE what makes two boards "the same advance" — a second implementation of
    them would drift from the writer silently, and for this caller a silent drift means
    committing somebody else's bytes.

    No ``now=``: the caller's own carry passes none either, and a ``last_updated`` line
    rewritten here and not there would make every comparison fail.

    Returns None when ``advance`` declines to write at all — the board's row is absent,
    or its line is a shape ``_set_mapping_value`` will not rewrite. There is then no
    intended content to compare against, and a caller must not read "I could not
    compute it" as "the tree is mine"."""
    with tempfile.TemporaryDirectory() as tmp:
        shadow = Path(tmp) / "sprint-status.yaml"
        shadow.write_bytes(source)
        if advance(shadow, story_key, target) is None:
            return None
        return shadow.read_bytes()


@dataclass(frozen=True)
class StorySelector:
    """Resolves a human story reference (``--epic``/``--story``) to the
    stories it selects. Forms accepted by :func:`parse_selector`:

    * full key ``3-1-user-auth`` — exact match
    * short ref ``3-1`` / ``3.1`` — epic 3, story 1 (any slug)
    * suffixed short ref ``2-6a`` / ``2.6a`` — exactly the ``a`` half of a
      split story; the plain ``2-6`` matches the whole ``2-6a``/``2-6b`` family
    * bare number ``1`` (or ``6a``) with ``--epic 3`` — epic 3, story 1 (or 6a)
    * slug fragment ``user-auth`` / ``auth`` — substring of the slug (must be unique)
    * epic only (``--epic 3``, blank story) — every story in the epic
    """

    epic: int | None = None
    num: int | None = None
    key: str | None = None  # exact full key
    slug: str | None = None  # slug substring
    suffix: str | None = None  # split-story letter; None matches any suffix

    @property
    def is_targeted(self) -> bool:
        """True when the selector names one intended story rather than just
        an epic-wide (or empty) filter."""
        return any(v is not None for v in (self.key, self.num, self.slug))

    def matches(self, story: Story) -> bool:
        if self.key is not None:
            return story.key == self.key
        if self.epic is not None and story.epic != self.epic:
            return False
        if self.num is not None and story.num != self.num:
            return False
        if self.suffix is not None and story.suffix != self.suffix:
            return False
        if self.slug is not None and self.slug not in story.slug:
            return False
        return True


def parse_selector(epic: int | None, story: str | None) -> StorySelector:
    """Translate the ``--epic``/``--story`` pair into a :class:`StorySelector`.

    Raises :class:`SprintStatusError` on bad or ambiguous input.
    """
    text = (story or "").strip()
    if not text:
        return StorySelector(epic=epic)

    def _check_epic(parsed_epic: int) -> None:
        if epic is not None and epic != parsed_epic:
            raise SprintStatusError(
                f"--epic {epic} conflicts with story '{text}' (epic {parsed_epic})"
            )

    # empty suffix group -> None: a plain `2-6` matches the whole split family
    if m := STORY_RE.match(text):  # full key 3-1-slug
        e, n = int(m.group(1)), int(m.group(2))
        _check_epic(e)
        return StorySelector(epic=e, num=n, key=text, suffix=m.group(3) or None)
    if m := SHORT_REF_RE.match(text):  # 3-1 / 3.1 / 3-1a
        e, n = int(m.group(1)), int(m.group(2))
        _check_epic(e)
        return StorySelector(epic=e, num=n, suffix=m.group(3) or None)
    if m := BARE_NUM_RE.match(text):  # bare story number, needs --epic
        if epic is None:
            raise SprintStatusError(
                f"ambiguous story '{text}': use --epic E --story {text}, or E-{text}"
            )
        return StorySelector(epic=epic, num=int(m.group(1)), suffix=m.group(2) or None)
    return StorySelector(epic=epic, slug=text)  # slug fragment


def select_actionable(ss: SprintStatus, epic: int | None, story: str | None) -> list[Story]:
    """Stories selected by ``--epic``/``--story`` that are ready to start, in
    file order. Raises :class:`SprintStatusError` with a targeted message when a
    named story is missing, ambiguous, or exists but is not actionable.
    """
    sel = parse_selector(epic, story)
    matches = [s for s in ss.stories if sel.matches(s)]
    if sel.is_targeted:
        if not matches:
            raise SprintStatusError(f"no story matches '{story}'")
        if sel.slug is not None:
            keys = sorted({s.key for s in matches})
            if len(keys) > 1:
                raise SprintStatusError(
                    f"story '{sel.slug}' is ambiguous — matches: {', '.join(keys)}"
                )
    actionable = [s for s in matches if s.status in ACTIONABLE_STATUSES]
    if sel.is_targeted and matches and not actionable:
        s = matches[0]
        raise SprintStatusError(
            f"story {story} matched {s.key} but its status is " f"'{s.status}' (not actionable)"
        )
    return actionable
