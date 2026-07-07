"""Model of sprint-status.yaml — the single source of workflow truth.

The dev primitive `bmad-dev-auto` deliberately does not touch sprint-status
("the orchestrator's business"), so the orchestrator is the single writer via
:func:`advance` — idempotent, never-regress, epic-lift. The orchestrator
otherwise only re-reads this file to pick the next story and verify what a
session claims, so the no-races invariant holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# Sprint-status key classifiers. The original regexes only accepted pure-digit
# epics + stories (epic-N, N-N-slug). Extended here to accept:
#   - epic-1a, epic-18a, epic-0-p2        (letter / phase suffix on epics)
#   - 1a-1-..., 3-3a-..., 18-0a-...       (letter suffix on story segments)
#   - sprint-NN + sprint-NN-*             (sprint grouping container)
#   - backlog + backlog-*                 (unscheduled work under a virtual epic)
#   - {status: done, size: M, rationale: ...} struct values (Phase 4 entries
#     attach size + rationale inline)
EPIC_RE = re.compile(r"^epic-(\d+[a-z]?(?:-p\d+)?)$")
RETRO_RE = re.compile(r"^epic-(\d+[a-z]?(?:-p\d+)?)-retrospective$")
RETRO_ITEM_RE = re.compile(r"^epic-(\d+[a-z]?(?:-p\d+)?)-retro-item-(\d+)-(.+)$")
STORY_RE = re.compile(r"^(\d+[a-z]?(?:-p\d+)?)-(\d+[a-z]?)-(.+)$")
SPRINT_RE = re.compile(r"^sprint-(\d+)$")
SPRINT_RETRO_RE = re.compile(r"^sprint-(\d+)-retrospective$")
SPRINT_STORY_RE = re.compile(r"^sprint-(\d+)-(.+)$")
BACKLOG_RE = re.compile(r"^backlog$")
BACKLOG_STORY_RE = re.compile(r"^backlog-(.+)$")
SHORT_REF_RE = re.compile(r"^(\d+)[-.](\d+)$")  # short story ref: 3-1 or 3.1
BARE_NUM_RE = re.compile(r"^(\d+)$")  # a lone story number, needs --epic
# Sprint/backlog story prefix detector — splits domain code, story number, slug.
# Examples:
#   "qua-5g1-auth-session-state-extraction" -> ("qua", "5g1", "auth-session-state-extraction")
#   "deploy-monolith-image-go-live"         -> ("deploy", "",   "monolith-image-go-live")
SPRINT_STORY_SPLIT_RE = re.compile(r"^([a-z]+)-(\d+[a-z]?)-(.+)$")
SPRINT_STORY_NOSPLIT_RE = re.compile(r"^([a-z]+)-(.+)$")

STORY_STATUSES = {"backlog", "ready-for-dev", "in-progress", "review", "done"}
# Lifecycle order, earliest -> latest. `advance` never moves a story backward
# through this sequence (matches sync-sprint-status's "never regress").
STATUS_ORDER = ("backlog", "ready-for-dev", "in-progress", "review", "done")
LEGACY_STORY_STATUSES = {"drafted": "ready-for-dev"}
ACTIONABLE_STATUSES = {"backlog", "ready-for-dev"}


class SprintStatusError(Exception):
    pass


@dataclass(frozen=True)
class Story:
    key: str
    # epic is a string id: "1", "1a", "0-p2", "sprint-08", or "backlog".
    # Original bmad-loop typed this as int; we widen to str so the orchestrator
    # can group letter-suffix / phase-suffix / sprint / backlog stories.
    epic: str
    # num is a string id: "1", "3a", "5g1", or "" when the format has no number.
    num: str
    slug: str
    status: str


@dataclass(frozen=True)
class RetroItem:
    """A retrospective action item tracked in sprint-status under the
    RETRO ACTION ITEMS section: ``epic-{epic}-retro-item-{num}-{slug}``.

    Recognized so they no longer fall into ``unknown_keys``; the orchestrator
    does not yet drive them as work (see roadmap: retro-item automation).
    """

    key: str
    epic: str  # was int — accepts "1", "1a", etc.
    num: int
    slug: str
    status: str


@dataclass(frozen=True)
class SprintStatus:
    path: Path
    # Keys are epic ids (str); "epic-" / "sprint-" / "backlog" prefixes live in
    # the value-side labels, not the key (matches the YAML literal key style).
    epics: dict[str, str]
    stories: tuple[Story, ...]
    retros: dict[str, str]
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

    epics: dict[str, str] = {}
    stories: list[Story] = []
    retros: dict[str, str] = {}
    retro_items: list[RetroItem] = []
    unknown: list[str] = []
    for key, raw_status in dev.items():
        key = str(key)
        # Status can be a plain string ("done") or a struct with metadata
        # ({status: done, size: M, rationale: "...", last_updated: ...}).
        # The project's Phase 4 entries use the struct shape to attach size +
        # rationale inline; upstream bmad-loop treated them as plain strings.
        if isinstance(raw_status, dict):
            status = str(raw_status.get("status", "")).strip()
        else:
            status = str(raw_status).strip()
        if m := RETRO_ITEM_RE.match(key):
            retro_items.append(
                RetroItem(
                    key=key,
                    epic=m.group(1),  # str — was int
                    num=int(m.group(2)),
                    slug=m.group(3),
                    status=status,
                )
            )
        elif m := RETRO_RE.match(key):
            retros[m.group(1)] = status  # str key — was int
        elif m := EPIC_RE.match(key):
            epics[m.group(1)] = status  # str key — was int
        elif m := STORY_RE.match(key):
            status = LEGACY_STORY_STATUSES.get(status, status)
            stories.append(
                Story(
                    key=key,
                    epic=m.group(1),  # str — was int
                    num=m.group(2),   # str — was int (preserves "3a", "5g1", etc.)
                    slug=m.group(3),
                    status=status,
                )
            )
        elif m := SPRINT_RETRO_RE.match(key):
            retros[f"sprint-{m.group(1)}"] = status
        elif m := SPRINT_RE.match(key):
            epics[f"sprint-{m.group(1)}"] = status
        elif m := SPRINT_STORY_RE.match(key):
            status = LEGACY_STORY_STATUSES.get(status, status)
            rest = m.group(2)
            # Best-effort split into (domain, num, slug). Sprint story keys have
            # inconsistent shape (e.g. "qua-5g1-auth-..." has a num, but
            # "deploy-monolith-..." and "bugfix-..." do not), so num may be "".
            sm = SPRINT_STORY_SPLIT_RE.match(rest)
            if sm:
                num = sm.group(2)
                slug = f"{sm.group(1)}-{sm.group(3)}"
            else:
                # No numeric segment (e.g. "deploy-monolith-..." / "bugfix-..."):
                # treat the entire rest as slug with empty num.
                num = ""
                slug = rest
            stories.append(
                Story(
                    key=key,
                    epic=f"sprint-{m.group(1)}",
                    num=num,
                    slug=slug,
                    status=status,
                )
            )
        elif m := BACKLOG_RE.match(key):
            epics["backlog"] = status
        elif m := BACKLOG_STORY_RE.match(key):
            status = LEGACY_STORY_STATUSES.get(status, status)
            rest = m.group(1)
            sm = re.match(r"^([a-z]+)-(\d+)-(.+)$", rest)
            if sm:
                num = sm.group(2)
                slug = f"{sm.group(1)}-{sm.group(3)}"
            else:
                num = ""
                slug = rest
            stories.append(
                Story(
                    key=key,
                    epic="backlog",
                    num=num,
                    slug=slug,
                    status=status,
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
    ss: SprintStatus, skip: set[str] | None = None, *, epic: str | None = None
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


def _set_mapping_value(lines: list[str], key: str, new_value: str) -> bool:
    """In-place replace the value of the first `key:` line, preserving
    indentation and any trailing ` # comment`. Returns True on a real change. A
    minimal line edit (not a YAML round-trip) so the file's comments and
    structure — STATUS DEFINITIONS, WORKFLOW NOTES — survive verbatim. The value
    region may contain spaces (e.g. `last_updated: 01-06-2026 10:00`); a trailing
    inline comment is recognized only when preceded by whitespace (YAML rule)."""
    # value = everything after the gap up to an optional ` #...` inline comment
    pat = re.compile(
        rf"^(?P<indent>\s*){re.escape(key)}:(?P<gap>[ \t]+)"
        r"(?P<val>\S(?:.*?\S)?)(?P<rest>[ \t]+#.*)?$"
    )
    for i, line in enumerate(lines):
        m = pat.match(line.rstrip("\n"))
        if not m:
            continue
        if m.group("val") == new_value:
            return False  # already at target — idempotent no-op
        nl = "\n" if line.endswith("\n") else ""
        lines[i] = (
            f"{m.group('indent')}{key}:{m.group('gap')}{new_value}{m.group('rest') or ''}" + nl
        )
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

    text = path.read_text(encoding="utf-8")
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
        # Derive parent epic from the story_key prefix. STORY_RE covers
        # numeric/letter-suffix/phase-suffix epics; SPRINT_STORY_RE and
        # BACKLOG_STORY_RE cover Phase 4 + unscheduled work.
        #
        # Two keys here:
        #   - yaml_key is what `_set_mapping_value` writes to the YAML file
        #     (literal key in the document, e.g. "epic-3" or "sprint-08").
        #   - lookup_id is what `ss.epics` is keyed by after `load()` (no
        #     prefix, e.g. "3" or "sprint-08" or "backlog"). The split lets
        #     letter-suffix epics (epic-1a, epic-0-p2) round-trip cleanly.
        yaml_key: str | None = None
        lookup_id: str | None = None
        if m_story := STORY_RE.match(story_key):
            bare = m_story.group(1)
            yaml_key = f"epic-{bare}"
            lookup_id = bare
        elif m_sprint := SPRINT_STORY_RE.match(story_key):
            yaml_key = f"sprint-{m_sprint.group(1)}"
            lookup_id = yaml_key  # sprint epics keep "sprint-NN" as their id
        elif BACKLOG_STORY_RE.match(story_key):
            yaml_key = "backlog"
            lookup_id = "backlog"
        if yaml_key is not None and lookup_id is not None:
            ss = load(path)
            if ss.epics.get(lookup_id) == "backlog":
                changed = _set_mapping_value(lines, yaml_key, "in-progress") or changed

    if now is not None:
        changed = _set_mapping_value(lines, "last_updated", now) or changed

    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return target


@dataclass(frozen=True)
class StorySelector:
    """Resolves a human story reference (``--epic``/``--story``) to the
    stories it selects. Forms accepted by :func:`parse_selector`:

    * full key ``3-1-user-auth`` — exact match
    * short ref ``3-1`` / ``3.1`` — epic 3, story 1 (any slug)
    * bare number ``1`` with ``--epic 3`` — epic 3, story 1
    * slug fragment ``user-auth`` / ``auth`` — substring of the slug (must be unique)
    * epic only (``--epic 3``, blank story) — every story in the epic
    * Phase-4 keys: ``--epic sprint-08 --story qua-5g1-...`` or the full key
      as a slug fragment.
    """

    epic: str | None = None
    num: str | None = None  # str — was int; preserves "3a", "5g1", or ""
    key: str | None = None  # exact full key
    slug: str | None = None  # slug substring

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
        if self.slug is not None and self.slug not in story.slug:
            return False
        return True


def parse_selector(epic: str | None, story: str | None) -> StorySelector:
    """Translate the ``--epic``/``--story`` pair into a :class:`StorySelector`.

    Raises :class:`SprintStatusError` on bad or ambiguous input.
    """
    text = (story or "").strip()
    if not text:
        return StorySelector(epic=epic)

    def _check_epic(parsed_epic: str) -> None:
        if epic is not None and epic != parsed_epic:
            raise SprintStatusError(
                f"--epic {epic} conflicts with story '{text}' (epic {parsed_epic})"
            )

    if m := STORY_RE.match(text):  # full key 3-1-slug (or 1a-1-slug, etc.)
        e, n = m.group(1), m.group(2)  # keep as str — was int
        _check_epic(e)
        return StorySelector(epic=e, num=n, key=text)
    if m := SHORT_REF_RE.match(text):  # 3-1 / 3.1
        e, n = m.group(1), m.group(2)  # keep as str — was int
        _check_epic(e)
        return StorySelector(epic=e, num=n)
    if m := BARE_NUM_RE.match(text):  # bare story number, needs --epic
        if epic is None:
            raise SprintStatusError(
                f"ambiguous story '{text}': use --epic E --story {text}, or E-{text}"
            )
        return StorySelector(epic=epic, num=m.group(1))  # str — was int
    return StorySelector(epic=epic, slug=text)  # slug fragment


def select_actionable(ss: SprintStatus, epic: str | None, story: str | None) -> list[Story]:
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
