"""Model of stories.yaml — the Story Breakdown manifest for "stories mode".

The optional Story Breakdown step of `bmad-spec` emits ``stories.yaml``, a
fixed-name sibling of ``SPEC.md`` in the spec folder (discovered by name, never
referenced from frontmatter). It is a flat list, one entry per story, in strict
execution order — **there is no ``depends_on`` field**, so the schedule is a
single left-to-right scan, not a DAG. Each entry pins a stable, prefix-free,
machine-opaque ``id`` plus ``title``/``description`` and the caller-only knobs
``spec_checkpoint`` / ``done_checkpoint`` / ``invoke_dev_with``. ``status`` is
deliberately absent: bmad-spec is the sole writer of ``stories.yaml`` and
bmad-dev-auto is the sole writer of each story spec's status — the orchestrator
writes neither.

This module is the strict, typed parser the orchestrator reads it through. The
upstream schema (validity rule 4) already says ids are quoted strings of
letters/digits/dashes, but an LLM-authored file may still emit an unquoted
``id: 1`` (PyYAML -> int) or ``id: 3.5`` (-> float); we ``str()``-normalize then
charset-validate as defense-in-depth. Everything here is pure contract: no
engine or sprint-mode coupling (only :mod:`verify`'s frontmatter readers, for
the id-keyed disk resolution).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .verify import read_frontmatter, status_of

# Fixed-name discovery, like SPEC.md / .memlog.md — never listed in companions.
STORIES_FILENAME = "stories.yaml"
# Story specs live under <spec-folder>/stories/, keyed <id>-<slug>.md.
STORIES_SUBDIR = "stories"

# Schema validity rule 4: ids are letters, digits, and dashes only. Matches the
# upstream authoring rule exactly — ids become filename segments and task keys,
# so a stray character must fail loud, not slip into a path.
ID_RE = re.compile(r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$")

REQUIRED_FIELDS = ("id", "title", "description")

# The dispatch-protocol read model. Non-terminal statuses a re-dispatch resumes
# from (the session died mid-flight); `done` is terminal-skip; `blocked` stops
# the run. A story spec that is absent reads as PENDING (never dispatched).
RESUMABLE_STATUSES = frozenset({"draft", "ready-for-dev", "in-progress", "in-review"})
DONE = "done"
BLOCKED = "blocked"

# resolve_story_spec state kinds.
KIND_PENDING = "pending"  # no story spec on disk yet
KIND_PRESENT = "present"  # exactly one real story spec; carries .status
KIND_AMBIGUOUS = "ambiguous"  # >1 matching file — an anomaly, refuse to pick
KIND_SENTINEL = "sentinel"  # the single match is a fixed-slug skeletal sentinel

# Fixed-slug skeletal specs the skill writes on a pre-planning HALT, kept inside
# the <id>-*.md glob so "no file = pending" holds; recoverable by deletion.
SENTINEL_UNRESOLVED = "unresolved"
SENTINEL_AMBIGUOUS = "ambiguous"
SENTINEL_SLUGS = (SENTINEL_UNRESOLVED, SENTINEL_AMBIGUOUS)

# schedule() outcomes.
SCHEDULE_NEXT = "next"  # .entry is the next story to dispatch
SCHEDULE_COMPLETE = "complete"  # every story is done — the run is finished
SCHEDULE_WEDGED = "wedged"  # scan stopped on a blocked/sentinel/ambiguous entry


class StoriesError(Exception):
    pass


@dataclass(frozen=True)
class StoryEntry:
    """One story in the breakdown. ``id`` is stable once its spec file exists;
    the checkpoint flags are independent (a story may set both and pause twice).
    ``invoke_dev_with`` is free text appended verbatim to the dispatch prompt —
    the single planner->dev channel, never interpreted here."""

    id: str
    title: str
    description: str
    spec_checkpoint: bool = False
    done_checkpoint: bool = False
    invoke_dev_with: str = ""


@dataclass(frozen=True)
class Stories:
    path: Path
    entries: tuple[StoryEntry, ...]

    def get(self, story_id: str) -> StoryEntry | None:
        sid = str(story_id).strip()
        return next((e for e in self.entries if e.id == sid), None)


def load_stories(spec_folder: Path | str) -> Stories:
    """Parse + validate ``<spec-folder>/stories.yaml`` into a typed :class:`Stories`.

    Validates: required fields present, ids unique and prefix-free, no ``status``
    key, id charset. Ids are ``str()``-normalized before validation (int/float
    coercion defense). Raises :class:`StoriesError` with the pinned
    ``no stories.yaml found`` message when the file is absent. **No DAG / cycle
    validation** — the list is strictly linear.
    """
    path = Path(spec_folder) / STORIES_FILENAME
    if not path.is_file():
        raise StoriesError("no stories.yaml found")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise StoriesError(f"stories.yaml is not valid YAML: {path}: {e}") from e
    if doc is None or (isinstance(doc, list) and not doc):
        raise StoriesError("stories.yaml has no story entries")
    if not isinstance(doc, list):
        raise StoriesError("stories.yaml must be a top-level list of story entries")

    entries: list[StoryEntry] = []
    seen: set[str] = set()
    seen_folded: dict[str, str] = {}  # casefolded id -> first id that used it
    for index, raw in enumerate(doc):
        entry = _parse_entry(raw, index)
        if entry.id in seen:
            raise StoriesError(f"stories.yaml has a duplicate id {entry.id!r}")
        # Story specs resolve by the `<id>-*.md` glob, which is case-insensitive on
        # Windows/macOS filesystems (both in the CI matrix), so two ids that differ
        # only by case would cross-match the same files. Reject them up front rather
        # than let resolution become filesystem-dependent.
        folded = entry.id.casefold()
        if folded in seen_folded:
            raise StoriesError(
                f"stories.yaml ids {seen_folded[folded]!r} and {entry.id!r} differ only "
                "by case — story specs resolve by the case-insensitive glob <id>-*.md, so "
                "on a case-insensitive filesystem (Windows/macOS) they would cross-match"
            )
        seen.add(entry.id)
        seen_folded[folded] = entry.id
        entries.append(entry)
    _validate_prefix_free([e.id for e in entries])
    return Stories(path=path, entries=tuple(entries))


def _parse_entry(raw: object, index: int) -> StoryEntry:
    if not isinstance(raw, dict):
        raise StoriesError(f"stories.yaml entry {index} is not a mapping")
    if "status" in raw:
        raise StoriesError(
            f"stories.yaml entry {index} has a forbidden 'status' key — a story's "
            "status lives in its story spec, never in stories.yaml"
        )
    story_id = _parse_id(raw, index)
    return StoryEntry(
        id=story_id,
        title=_require_text(raw, "title", story_id),
        description=_require_text(raw, "description", story_id),
        spec_checkpoint=_bool_field(raw, "spec_checkpoint", story_id),
        done_checkpoint=_bool_field(raw, "done_checkpoint", story_id),
        invoke_dev_with=_text_field(raw, "invoke_dev_with", story_id),
    )


def _parse_id(raw: dict, index: int) -> str:
    if raw.get("id") is None:
        raise StoriesError(f"stories.yaml entry {index} is missing required field 'id'")
    # str()-normalize: schema rule 4 says ids are quoted strings, but an
    # LLM-authored file may still emit an unquoted `id: 1` (PyYAML -> int) or
    # `id: 3.5` (-> float). Coerce, then charset-validate — a float's `.` fails.
    story_id = str(raw["id"]).strip()
    if not ID_RE.match(story_id):
        raise StoriesError(
            f"stories.yaml entry {index} has invalid id {story_id!r}: ids must be "
            "letters, digits, and dashes (^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$)"
        )
    return story_id


def _require_text(raw: dict, key: str, story_id: str) -> str:
    value = raw.get(key)
    if value is None:
        raise StoriesError(f"stories.yaml story {story_id!r} is missing required field {key!r}")
    if not isinstance(value, str):
        raise StoriesError(f"stories.yaml story {story_id!r} field {key!r} must be a string")
    value = value.strip()
    if not value:
        raise StoriesError(f"stories.yaml story {story_id!r} field {key!r} is empty")
    return value


def _bool_field(raw: dict, key: str, story_id: str) -> bool:
    """A checkpoint flag: bool, defaulting False when missing/null. Strict — a
    non-bool (`1`, `"true"`) is a schema error, not a silent truthy coercion. In
    Python ``bool`` is an ``int`` subclass, but ``isinstance(1, bool)`` is False,
    so an integer 1 is correctly rejected."""
    value = raw.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise StoriesError(
            f"stories.yaml story {story_id!r} field {key!r} must be a boolean "
            f"(got {type(value).__name__})"
        )
    return value


def _text_field(raw: dict, key: str, story_id: str) -> str:
    """Optional free-text field, defaulting "" when missing/null. Not stripped:
    ``invoke_dev_with`` is appended to the dispatch prompt verbatim."""
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise StoriesError(f"stories.yaml story {story_id!r} field {key!r} must be a string")
    return value


def _validate_prefix_free(ids: list[str]) -> None:
    """No id may equal another id plus a ``-suffix`` (schema validity rule 2).

    Story specs are discovered by the ``<id>-*.md`` glob, so if ``3`` and
    ``3-2`` were both ids the ``3-*.md`` glob for story ``3`` would also match
    ``3-2-slug.md`` — the id would no longer resolve to a single file. ``3`` vs
    ``31`` is fine: ``3-*.md`` never matches ``31-slug.md``.

    The check is case-insensitive for the same reason ``load_stories`` rejects
    case-only duplicates: on a case-insensitive filesystem ``Auth-*.md`` also
    matches ``auth-2-slug.md``, so ``Auth`` and ``auth-2`` collide there too.
    Case-only duplicates (equal casefold) are caught earlier in ``load_stories``;
    by here every id has a distinct casefold, so this map is unambiguous.
    """
    by_fold = {i.casefold(): i for i in ids}
    for story_id in ids:
        parts = story_id.casefold().split("-")
        for k in range(1, len(parts)):
            prefix = "-".join(parts[:k])
            other = by_fold.get(prefix)
            if other is not None:
                raise StoriesError(
                    f"stories.yaml id {story_id!r} is not prefix-free: {other!r} is "
                    f"also an id, so the {other}-*.md glob would match both"
                )


def find_entry(stories: Stories, story_id: str) -> StoryEntry:
    """The entry for ``story_id`` or a :class:`StoriesError` with the pinned
    ``story id not found in stories.yaml`` message."""
    entry = stories.get(story_id)
    if entry is None:
        raise StoriesError("story id not found in stories.yaml")
    return entry


@dataclass(frozen=True)
class StoryState:
    """The resolved on-disk state of one story (see :func:`resolve_story_spec`).

    ``status`` is set only for :data:`KIND_PRESENT`; ``path`` for PRESENT /
    SENTINEL; ``paths`` for AMBIGUOUS; ``sentinel_kind`` for SENTINEL.
    """

    kind: str
    status: str = ""
    path: Path | None = None
    paths: tuple[Path, ...] = ()
    sentinel_kind: str = ""


def resolve_story_spec(spec_folder: Path | str, story_id: str) -> StoryState:
    """Deterministic on-disk state of one story, keyed by id.

    Globs ``<spec-folder>/stories/<id>-*.md``. Ids are prefix-free, so a
    conforming tree yields at most one match: no match = :data:`KIND_PENDING`
    (never dispatched); a fixed-slug ``<id>-unresolved.md`` / ``<id>-ambiguous.md``
    = :data:`KIND_SENTINEL`; any other single file = :data:`KIND_PRESENT` with
    its frontmatter status read off disk. More than one match =
    :data:`KIND_AMBIGUOUS` (an anomaly the dispatcher must refuse rather than
    silently pick one).

    The glob result is filtered to names starting with the **exact-case**
    ``<id>-`` prefix so resolution is deterministic across filesystems: a
    case-insensitive FS (Windows/macOS) would otherwise let ``Auth-*.md`` also
    match ``auth-2-slug.md``, matching what a case-sensitive FS (Linux) never
    would. This keeps resolution in step with the exact-case sentinel comparison
    below and verify's id-prefix gate — a wrong-case hit that resolved here would
    only fail those and cause a spurious retry.
    """
    sid = str(story_id).strip()
    if not ID_RE.match(sid):
        # An id that isn't charset-valid can't name a conforming `<id>-*.md` file
        # and must never reach glob() — a stray metacharacter (`*`, `?`, `[`) or a
        # path separator would mis-match (or an escape). Every live caller already
        # passes a manifest id validated by load_stories; this guards a future
        # caller that doesn't. A clean "no resolvable spec" (PENDING) is what every
        # caller already handles, matching the module's fail-loud-not-slip rule.
        return StoryState(kind=KIND_PENDING)
    stories_dir = Path(spec_folder) / STORIES_SUBDIR
    matches = (
        sorted(m for m in stories_dir.glob(f"{sid}-*.md") if m.name.startswith(f"{sid}-"))
        if stories_dir.is_dir()
        else []
    )
    if not matches:
        return StoryState(kind=KIND_PENDING)
    if len(matches) > 1:
        return StoryState(kind=KIND_AMBIGUOUS, paths=tuple(matches))
    path = matches[0]
    for sentinel_kind in SENTINEL_SLUGS:
        if path.name == f"{sid}-{sentinel_kind}.md":
            return StoryState(kind=KIND_SENTINEL, path=path, sentinel_kind=sentinel_kind)
    return StoryState(kind=KIND_PRESENT, status=status_of(read_frontmatter(path)), path=path)


@dataclass(frozen=True)
class Schedule:
    """The scheduler's verdict. ``outcome`` is one of :data:`SCHEDULE_NEXT`
    (``entry`` is the next story to dispatch), :data:`SCHEDULE_COMPLETE` (all
    done), or :data:`SCHEDULE_WEDGED` (``entry``/``state`` name the
    blocked/sentinel/ambiguous story that stopped the scan)."""

    outcome: str
    entry: StoryEntry | None = None
    state: StoryState | None = None

    @property
    def is_complete(self) -> bool:
        return self.outcome == SCHEDULE_COMPLETE

    @property
    def is_wedged(self) -> bool:
        return self.outcome == SCHEDULE_WEDGED


def schedule(
    stories: Stories,
    states: dict[str, StoryState],
    selector: str | None = None,
) -> Schedule:
    """Linear scheduler: the first list entry ready to (re)dispatch.

    The manifest is a flat list in strict execution order (no ``depends_on``),
    so scheduling is a single left-to-right scan. An entry is actionable when
    its state is PENDING or a resumable non-terminal (``draft`` / ``ready-for-dev``
    / ``in-progress`` / ``in-review`` = died mid-flight, re-dispatch resumes); a
    ``done`` entry is skipped (never re-dispatch done); a ``blocked``, sentinel,
    ambiguous, or unknown-status entry STOPS the scan (:data:`SCHEDULE_WEDGED` —
    the run pauses for resolve; a blocked story cannot be leapfrogged to later
    work). Falling off the end with everything done is :data:`SCHEDULE_COMPLETE`.

    ``selector`` restricts the scan to a single story id (raises when the id is
    unknown), for ``--story`` runs. A state missing from ``states`` is treated
    as PENDING (no spec on disk).
    """
    entries: tuple[StoryEntry, ...]
    entries = (find_entry(stories, selector),) if selector is not None else stories.entries
    for entry in entries:
        state = states.get(entry.id)
        if state is None:
            state = StoryState(kind=KIND_PENDING)
        disposition = _classify(state)
        if disposition == "actionable":
            return Schedule(SCHEDULE_NEXT, entry=entry, state=state)
        if disposition == "done":
            continue
        return Schedule(SCHEDULE_WEDGED, entry=entry, state=state)
    return Schedule(SCHEDULE_COMPLETE)


def _classify(state: StoryState) -> str:
    """One of ``'actionable'`` | ``'done'`` | ``'wedged'`` for a resolved state."""
    if state.kind == KIND_PENDING:
        return "actionable"
    if state.kind == KIND_PRESENT:
        if state.status == DONE:
            return "done"
        if state.status in RESUMABLE_STATUSES:
            return "actionable"
        # blocked, or a status the skill itself would HALT on as unrecognized.
        return "wedged"
    # AMBIGUOUS or SENTINEL — not actionable without dispatcher/human recovery.
    return "wedged"
