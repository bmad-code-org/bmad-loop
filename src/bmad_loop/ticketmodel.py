"""Typed views of the JSON a BMAD v7 project's ``tickets.py`` prints.

``tickets.py`` (installed at ``_bmad/method/scripts/tickets.py``) is the v7
ticket tree's machine interface: ``next``, ``status``, ``find`` and ``mark``
each print one JSON object. Upstream publishes no schema version, so this
module pins the field set bmad-loop reads: every ``parse_*`` narrows the
``object`` the JSON decoder returned into frozen dataclasses and raises
:class:`TicketsSchemaError` (naming the command and the field) when a pinned
field is missing or has the wrong type. A schema error means version skew
between bmad-loop and the installed script, never a tree problem; the tree's
own problems arrive as data (``problems``) or as the script's own error
object, which ``tickettree`` maps.

The field names follow ``tickets.py`` ``public()`` (the row), ``next_view``,
``status_view``, ``cmd_find`` and ``cmd_mark``. Normalizations, all lossless
for the values upstream emits:

- an ``id`` (a number, or letters and digits) is text: ``1`` and ``"1"`` are
  one id upstream, so the int is rendered as its digits;
- ``after``/``waiting_on``/``blocks``/``gated_by`` entries are text the same
  way (upstream prints a sibling's id as a bare int);
- the flags upstream emits only when set (``plan_checkpoint``,
  ``done_checkpoint``, ``unknown``, ``waiting_on``, ``gated_by``, ``drift``)
  default to false/empty; a checkpoint flag must be exactly ``true``;
- ``drift`` (a mapping of differing fields) is read as "present".

``parse_help`` and :func:`capability_problems` gate the installed script on the
verbs and ``mark`` statuses bmad-loop drives, from its ``--help`` output (the
argparse choice sets). :func:`requires_python_satisfied` reads the PEP 723
``requires-python`` header, which decides whether the running interpreter can
launch the script directly.

Pure data and parsing — no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

# Every plan `status` upstream accepts (`tickets.py` STATUSES). The builds write
# draft..built, build-auto writes blocked, an orchestrator writes done, and
# bmad-ticket writes dropped. A row with no plan reads "".
PLAN_STATUSES: Final[tuple[str, ...]] = (
    "draft",
    "ready-for-dev",
    "in-progress",
    "in-review",
    "built",
    "done",
    "blocked",
    "dropped",
)
# A row's derived `state`: `planned` (no leaf file, no plan), else the tracker's
# mirrored word, else STATE_OF[status].
TICKET_STATES: Final[tuple[str, ...]] = (
    "planned",
    "backlog",
    "in-progress",
    "review",
    "done",
    "dropped",
)
# `next`'s groups, in the order upstream prints them.
NEXT_GROUPS: Final[tuple[str, ...]] = (
    "ready_to_refine",
    "ready_to_start",
    "in_progress",
    "blocked",
)
# What bmad-loop drives: the read verbs and `mark`, and the statuses it marks.
REQUIRED_VERBS: Final[frozenset[str]] = frozenset({"next", "find", "status", "mark"})
REQUIRED_STATUSES: Final[frozenset[str]] = frozenset({"built", "done", "blocked"})

_ROW_STATUSES: Final[frozenset[str]] = frozenset(("", *PLAN_STATUSES))


class TicketsSchemaError(ValueError):
    """A ``tickets.py`` document lacks a pinned field or mistypes it: the
    installed script and this bmad-loop disagree on the contract."""

    def __init__(self, command: str, field: str, detail: str) -> None:
        super().__init__(f"tickets.py {command}: {field}: {detail}")
        self.command = command
        self.field = field
        self.detail = detail


@dataclass(frozen=True)
class TicketRow:
    """One ticket as ``next``/``status``/``find`` print it (upstream ``public``).

    ``ref`` is what ``find`` resolves back to this ticket: ``E.M`` for an entry
    of a listed epic, the leaf file name for a backlog row. ``status`` is the
    plan's (``""`` with no plan). ``blocks`` is filled by ``status`` only."""

    ref: str | None
    epic: str
    id: str | None
    file: str | None
    type: str
    tracker_id: str
    title: str
    status: str
    tracker_status: str
    state: str
    assignee: str
    hitl: bool
    risk: str
    refine: bool
    refined: bool
    blocked_at: str
    blocked_reason: str
    after: tuple[str, ...]
    gated_by: tuple[str, ...]
    waiting_on: tuple[str, ...]
    unknown: str
    plan_checkpoint: bool
    done_checkpoint: bool
    drift: bool
    blocks: tuple[str, ...] = ()


@dataclass(frozen=True)
class NextView:
    """``next``: the folder's open tickets by group, in build order.

    ``unpinned_after``/``undeclared_after``/``order_conflict`` are upstream's
    tree warnings, kept opaque. ``backlog`` is the backlog folder's own view
    when ``next`` ran on the active initiative and a backlog exists;
    ``backlog_error`` is set instead when that folder could not be read."""

    store: str
    folder: str
    ready_to_refine: tuple[TicketRow, ...]
    ready_to_start: tuple[TicketRow, ...]
    in_progress: tuple[TicketRow, ...]
    blocked: tuple[TicketRow, ...]
    problems: tuple[str, ...]
    unpinned_after: tuple[Mapping[str, object], ...]
    undeclared_after: tuple[Mapping[str, object], ...]
    order_conflict: tuple[Mapping[str, object], ...]
    backlog: NextView | None = None
    backlog_error: str | None = None

    def group(self, name: str) -> tuple[TicketRow, ...]:
        """The rows of one of :data:`NEXT_GROUPS`."""
        if name not in NEXT_GROUPS:
            raise KeyError(name)
        rows: tuple[TicketRow, ...] = getattr(self, name)
        return rows


@dataclass(frozen=True)
class EpicEdge:
    """One declared ``after`` of an initiative's ``[[epic]]`` table."""

    epic: str
    needs: str


@dataclass(frozen=True)
class EpicRow:
    """``status`` on an initiative: one epic folder. ``status`` is the epic
    file's (in-progress, done, dropped); ``gated_by`` is its file's ``after``,
    the gate ``next`` enforces until each named epic is done; ``after`` is the
    initiative's advisory declaration."""

    slug: str
    id: str | None
    status: str
    after: tuple[EpicEdge, ...]
    gated_by: tuple[str, ...]
    blocks: tuple[str, ...]
    next_id: int


@dataclass(frozen=True)
class StatusView:
    """``status``: every ticket in scope. ``epics`` is filled when the folder is
    an initiative; ``next_id`` when it is a single epic or backlog folder."""

    store: str
    folder: str
    tickets: tuple[TicketRow, ...]
    counts: Mapping[str, int]
    epics: tuple[EpicRow, ...]
    next_id: int | None
    longest_remaining_chain: tuple[str, ...]
    problems: tuple[str, ...]
    backlog: StatusView | None = None
    backlog_error: str | None = None


@dataclass(frozen=True)
class FindResult:
    """``find <ref>``: the one row a reference names, plus where it lives.

    ``plan`` is the absolute path of the ticket's plan as the script prints it:
    the joined plan, else where a build would write one (it may not exist).
    ``epic_file`` is the container's file; ``story_file`` the pulled leaf file,
    or None for an entry that has none."""

    row: TicketRow
    folder: str
    description: str
    verify: str
    references: tuple[str, ...]
    notes: tuple[str, ...]
    unknown: str
    epic_file: str | None
    story_file: str | None
    plan: str


@dataclass(frozen=True)
class MarkReport:
    """``mark``: the plan written and its fields as read back by the script."""

    plan: str
    created: bool
    status: str
    assignee: str
    blocked_at: str
    blocked_reason: str


@dataclass(frozen=True)
class Capability:
    """What the installed script offers: its subcommands, the statuses ``mark``
    accepts, and its PEP 723 ``requires-python`` (None when absent)."""

    verbs: frozenset[str]
    statuses: frozenset[str]
    requires_python: str | None = None


# --------------------------------------------------------------- narrowing


def _obj(doc: object, command: str, where: str) -> Mapping[str, object]:
    if not isinstance(doc, dict):
        raise TicketsSchemaError(command, where, f"expected an object, got {type(doc).__name__}")
    out: dict[str, object] = {}
    for key, value in doc.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(key, str):
            raise TicketsSchemaError(command, where, f"non-text key {key!r}")
        out[key] = value
    return out


def _need(d: Mapping[str, object], command: str, field: str) -> object:
    if field not in d:
        raise TicketsSchemaError(command, field, "missing")
    return d[field]


def _str(d: Mapping[str, object], command: str, field: str, *, default: str | None = None) -> str:
    value = d.get(field, default) if default is not None else _need(d, command, field)
    if not isinstance(value, str):
        raise TicketsSchemaError(command, field, f"expected text, got {value!r}")
    return value


def _opt_str(d: Mapping[str, object], command: str, field: str) -> str | None:
    value = _need(d, command, field)
    if value is not None and not isinstance(value, str):
        raise TicketsSchemaError(command, field, f"expected text or null, got {value!r}")
    return value


def _bool(d: Mapping[str, object], command: str, field: str) -> bool:
    value = _need(d, command, field)
    if not isinstance(value, bool):
        raise TicketsSchemaError(command, field, f"expected true/false, got {value!r}")
    return value


def _int(d: Mapping[str, object], command: str, field: str) -> int:
    value = _need(d, command, field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TicketsSchemaError(command, field, f"expected an integer, got {value!r}")
    return value


def _flag(d: Mapping[str, object], command: str, field: str) -> bool:
    """A flag upstream prints only when set, so absent is false and present must
    be exactly true."""
    if field not in d:
        return False
    if d[field] is not True:
        raise TicketsSchemaError(command, field, f"expected true when present, got {d[field]!r}")
    return True


def _scalar_text(value: object, command: str, field: str) -> str:
    """An id-like scalar as text. A bool is refused before the int check (bool is
    an int subclass, and upstream never uses true/false as an id)."""
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise TicketsSchemaError(command, field, f"expected text or an integer, got {value!r}")


def _opt_id(d: Mapping[str, object], command: str, field: str) -> str | None:
    value = _need(d, command, field)
    return None if value is None else _scalar_text(value, command, field)


def _list(value: object, command: str, field: str) -> list[object]:
    if not isinstance(value, list):
        raise TicketsSchemaError(command, field, f"expected a list, got {value!r}")
    return list(value)  # pyright: ignore[reportUnknownArgumentType]


def _ids(d: Mapping[str, object], command: str, field: str, *, required: bool) -> tuple[str, ...]:
    if not required and field not in d:
        return ()
    items = _list(_need(d, command, field), command, field)
    return tuple(_scalar_text(v, command, field) for v in items)


def _texts(d: Mapping[str, object], command: str, field: str, *, required: bool) -> tuple[str, ...]:
    if not required and field not in d:
        return ()
    items = _list(_need(d, command, field), command, field)
    out: list[str] = []
    for v in items:
        if not isinstance(v, str):
            raise TicketsSchemaError(command, field, f"expected text entries, got {v!r}")
        out.append(v)
    return tuple(out)


def _objects(d: Mapping[str, object], command: str, field: str) -> tuple[Mapping[str, object], ...]:
    if field not in d:
        return ()
    items = _list(d[field], command, field)
    return tuple(_obj(v, command, field) for v in items)


# --------------------------------------------------------------- rows


def _row(doc: object, command: str) -> TicketRow:
    d = _obj(doc, command, "row")
    status = _str(d, command, "status")
    if status not in _ROW_STATUSES:
        raise TicketsSchemaError(command, "status", f"unknown plan status {status!r}")
    state = _str(d, command, "state")
    if state not in TICKET_STATES:
        raise TicketsSchemaError(command, "state", f"unknown state {state!r}")
    blocked_at = _need(d, command, "blocked_at")
    return TicketRow(
        ref=_opt_str(d, command, "ref"),
        epic=_str(d, command, "epic"),
        id=_opt_id(d, command, "id"),
        file=_opt_str(d, command, "file"),
        type=_str(d, command, "type"),
        tracker_id=_str(d, command, "tracker_id", default=""),
        title=_str(d, command, "title"),
        status=status,
        tracker_status=_str(d, command, "tracker_status"),
        state=state,
        assignee=_str(d, command, "assignee"),
        hitl=_bool(d, command, "hitl"),
        risk=_str(d, command, "risk"),
        refine=_bool(d, command, "refine"),
        refined=_bool(d, command, "refined"),
        # A leaf file's raw frontmatter value: a quoted date, or an int when a
        # hand edit left it bare; both read as text.
        blocked_at=_scalar_text(blocked_at, command, "blocked_at"),
        blocked_reason=_str(d, command, "blocked_reason"),
        after=_ids(d, command, "after", required=True),
        gated_by=_ids(d, command, "gated_by", required=False),
        waiting_on=_ids(d, command, "waiting_on", required=False),
        unknown=_str(d, command, "unknown", default=""),
        plan_checkpoint=_flag(d, command, "plan_checkpoint"),
        done_checkpoint=_flag(d, command, "done_checkpoint"),
        drift="drift" in d,
        blocks=_ids(d, command, "blocks", required=False),
    )


def _rows(d: Mapping[str, object], command: str, field: str) -> tuple[TicketRow, ...]:
    return tuple(_row(v, command) for v in _list(_need(d, command, field), command, field))


def _backlog_error(d: Mapping[str, object], command: str) -> str | None:
    """The `{folder, error}` object upstream prints in place of an unreadable
    backlog's view, else None."""
    if "error" not in d:
        return None
    return _str(d, command, "error")


# --------------------------------------------------------------- views


def parse_next(doc: object, *, store: str | None = None) -> NextView:
    """``next``'s document. ``store`` is passed for the nested backlog view,
    which inherits the parent's (upstream prints it once, at the top)."""
    command = "next"
    d = _obj(doc, command, "document")
    backlog: NextView | None = None
    backlog_error: str | None = None
    top_store = _str(d, command, "store") if store is None else store
    if "backlog" in d:
        b = _obj(d["backlog"], command, "backlog")
        backlog_error = _backlog_error(b, command)
        if backlog_error is None:
            backlog = parse_next(b, store=top_store)
    return NextView(
        store=top_store,
        folder=_str(d, command, "folder"),
        ready_to_refine=_rows(d, command, "ready_to_refine"),
        ready_to_start=_rows(d, command, "ready_to_start"),
        in_progress=_rows(d, command, "in_progress"),
        blocked=_rows(d, command, "blocked"),
        problems=_texts(d, command, "problems", required=False),
        unpinned_after=_objects(d, command, "unpinned_after"),
        undeclared_after=_objects(d, command, "undeclared_after"),
        order_conflict=_objects(d, command, "order_conflict"),
        backlog=backlog,
        backlog_error=backlog_error,
    )


def _epic(doc: object, command: str) -> EpicRow:
    d = _obj(doc, command, "epics")
    edges: list[EpicEdge] = []
    for raw in _list(_need(d, command, "after"), command, "after"):
        e = _obj(raw, command, "after")
        edges.append(
            EpicEdge(
                epic=_str(e, command, "epic"),
                needs=_scalar_text(e.get("needs", ""), command, "needs"),
            )
        )
    return EpicRow(
        slug=_str(d, command, "slug"),
        id=_opt_id(d, command, "id"),
        status=_str(d, command, "status"),
        after=tuple(edges),
        gated_by=_ids(d, command, "gated_by", required=True),
        blocks=_ids(d, command, "blocks", required=True),
        next_id=_int(d, command, "next_id"),
    )


def _counts(d: Mapping[str, object], command: str) -> Mapping[str, int]:
    raw = _obj(_need(d, command, "counts"), command, "counts")
    out: dict[str, int] = {}
    for key in raw:
        out[key] = _int(raw, command, key)
    return out


def parse_status(doc: object, *, store: str | None = None) -> StatusView:
    """``status``'s document (``store`` as in :func:`parse_next`)."""
    command = "status"
    d = _obj(doc, command, "document")
    backlog: StatusView | None = None
    backlog_error: str | None = None
    top_store = _str(d, command, "store") if store is None else store
    if "backlog" in d:
        b = _obj(d["backlog"], command, "backlog")
        backlog_error = _backlog_error(b, command)
        if backlog_error is None:
            backlog = parse_status(b, store=top_store)
    epics: tuple[EpicRow, ...] = ()
    if "epics" in d:
        epics = tuple(_epic(v, command) for v in _list(d["epics"], command, "epics"))
    return StatusView(
        store=top_store,
        folder=_str(d, command, "folder"),
        tickets=_rows(d, command, "tickets"),
        counts=_counts(d, command),
        epics=epics,
        next_id=_int(d, command, "next_id") if "next_id" in d else None,
        longest_remaining_chain=_ids(d, command, "longest_remaining_chain", required=False),
        problems=_texts(d, command, "problems", required=False),
        backlog=backlog,
        backlog_error=backlog_error,
    )


def parse_find(doc: object) -> FindResult:
    """``find``'s document: a row plus where the ticket lives."""
    command = "find"
    d = _obj(doc, command, "document")
    return FindResult(
        row=_row(d, command),
        folder=_str(d, command, "folder"),
        description=_str(d, command, "description"),
        verify=_str(d, command, "verify"),
        references=_texts(d, command, "references", required=False),
        notes=_texts(d, command, "notes", required=False),
        unknown=_str(d, command, "unknown", default=""),
        epic_file=_opt_str(d, command, "epic_file"),
        story_file=_opt_str(d, command, "story_file"),
        plan=_str(d, command, "plan"),
    )


def parse_mark(doc: object) -> MarkReport:
    """``mark``'s document: the plan path and the fields the script read back."""
    command = "mark"
    d = _obj(doc, command, "document")
    return MarkReport(
        plan=_str(d, command, "plan"),
        created=_bool(d, command, "created"),
        status=_str(d, command, "status"),
        assignee=_scalar_text(_need(d, command, "assignee"), command, "assignee"),
        blocked_at=_scalar_text(_need(d, command, "blocked_at"), command, "blocked_at"),
        blocked_reason=_scalar_text(_need(d, command, "blocked_reason"), command, "blocked_reason"),
    )


# --------------------------------------------------------------- capability

# An argparse choice set in a usage line: `{next,status,find}`. Lowercase words,
# commas and hyphens only, so a docstring's `{project-root}`-style placeholder
# outside the usage block can never be mistaken for one.
_CHOICES_RE: Final = re.compile(r"\{([a-z][a-z,-]*)\}")
_PEP723_BLOCK_RE: Final = re.compile(r"^# /// script\s*$(.*?)^# ///\s*$", re.M | re.S)
_REQUIRES_PYTHON_RE: Final = re.compile(
    r"""^#\s*requires-python\s*=\s*["']([^"']*)["']\s*$""", re.M
)
_GE_RE: Final = re.compile(r"^\s*>=\s*(\d+)\.(\d+)(?:\.\d+)?\s*$")


def _usage_choices(help_text: str) -> frozenset[str]:
    """The words of every choice set in the usage block (the text before the
    first blank line), where argparse prints the subcommand and choice sets."""
    usage = help_text.replace("\r\n", "\n").split("\n\n", 1)[0]
    words: set[str] = set()
    for match in _CHOICES_RE.finditer(usage):
        words.update(w for w in match.group(1).split(",") if w)
    return frozenset(words)


def parse_requires_python(script_text: str) -> str | None:
    """The PEP 723 ``requires-python`` specifier of a script's inline metadata
    block, or None when the script has no such block or key."""
    block = _PEP723_BLOCK_RE.search(script_text)
    if block is None:
        return None
    hit = _REQUIRES_PYTHON_RE.search(block.group(1))
    return None if hit is None else hit.group(1)


def parse_help(top_help: str, mark_help: str, *, requires_python: str | None = None) -> Capability:
    """The script's verbs (from ``tickets.py --help``) and ``mark`` statuses
    (from ``tickets.py mark --help``)."""
    return Capability(
        verbs=_usage_choices(top_help),
        statuses=_usage_choices(mark_help),
        requires_python=requires_python,
    )


def capability_problems(cap: Capability) -> list[str]:
    """One line per verb or ``mark`` status bmad-loop needs and the installed
    script lacks; empty when the script can drive a tickets run."""
    problems = [f"tickets.py has no `{v}` command" for v in sorted(REQUIRED_VERBS - cap.verbs)]
    problems += [
        f"tickets.py mark does not accept status `{s}`"
        for s in sorted(REQUIRED_STATUSES - cap.statuses)
    ]
    return problems


def requires_python_satisfied(spec: str | None, version: tuple[int, int]) -> bool | None:
    """Whether ``version`` satisfies ``spec``. Only a single ``>=X.Y`` (or
    ``>=X.Y.Z``, compared on X.Y) is understood; any other specifier, or none,
    answers None — the caller then launches through ``uv``, which reads the
    header itself."""
    if spec is None:
        return None
    hit = _GE_RE.match(spec)
    if hit is None:
        return None
    return version >= (int(hit.group(1)), int(hit.group(2)))
