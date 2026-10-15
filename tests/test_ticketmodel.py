"""Pure parse tests for `bmad_loop.ticketmodel`: hand-built documents shaped like
the vendored `tests/fixtures/bmad_v7/tickets.py` output (`public()`, `next_view`,
`status_view`, `cmd_find`, `cmd_mark`). No spawns; the real script's shapes are
pinned by the contract rows in tests/test_tickettree.py."""

from __future__ import annotations

import copy

import pytest

from bmad_loop import ticketmodel
from bmad_loop.ticketmodel import (
    NEXT_GROUPS,
    REQUIRED_STATUSES,
    REQUIRED_VERBS,
    Capability,
    TicketsSchemaError,
    capability_problems,
    parse_find,
    parse_help,
    parse_mark,
    parse_next,
    parse_requires_python,
    parse_status,
    requires_python_satisfied,
)


def _row(**over: object) -> dict[str, object]:
    """One row as `tickets.py public()` prints it (no optional keys)."""
    row: dict[str, object] = {
        "epic": "epic-cart",
        "id": 1,
        "file": None,
        "type": "story",
        "tracker_id": "",
        "title": "Cart model",
        "status": "",
        "tracker_status": "",
        "state": "planned",
        "assignee": "",
        "hitl": False,
        "risk": "",
        "covers": [],
        "estimate": "",
        "refine": False,
        "refined": False,
        "blocked_at": "",
        "blocked_reason": "",
        "ref": "1.1",
        "after": [],
    }
    row.update(over)
    return row


def _next(**over: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "store": "repo",
        "folder": "initiative-demo",
        "ready_to_refine": [],
        "ready_to_start": [],
        "in_progress": [],
        "blocked": [],
        "unpinned_after": [],
        "undeclared_after": [],
        "order_conflict": [],
    }
    doc.update(over)
    return doc


def test_parse_next_reads_every_group_in_build_order():
    doc = _next(
        ready_to_refine=[_row(ref="2.2", id=2, type="bug", refine=True)],
        ready_to_start=[_row(ref="1.1"), _row(ref="1.3", id=3)],
        in_progress=[_row(ref="1.4", id=4, status="in-progress", state="in-progress")],
        blocked=[_row(ref="1.2", id=2, after=[1], waiting_on=[1])],
        problems=["epic-cart/x-plan.md: ticket 9 names no entry"],
    )
    view = parse_next(doc)
    assert [r.ref for r in view.ready_to_start] == ["1.1", "1.3"]
    assert [r.ref for g in NEXT_GROUPS for r in view.group(g)] == [
        "2.2",
        "1.1",
        "1.3",
        "1.4",
        "1.2",
    ]
    assert view.store == "repo" and view.folder == "initiative-demo"
    assert view.problems == ("epic-cart/x-plan.md: ticket 9 names no entry",)
    assert view.blocked[0].after == ("1",) and view.blocked[0].waiting_on == ("1",)
    assert view.backlog is None and view.backlog_error is None
    with pytest.raises(KeyError):
        view.group("tickets")


def test_parse_next_nested_backlog_inherits_store_and_reads_an_error_object():
    backlog = _next(
        ready_to_start=[_row(ref="story-x.md", file="story-x.md", id=None, epic="backlog")]
    )
    del backlog["store"]  # upstream prints the store once, at the top
    view = parse_next(_next(backlog=backlog))
    assert view.backlog is not None and view.backlog.store == "repo"
    assert view.backlog.ready_to_start[0].ref == "story-x.md"
    assert view.backlog.ready_to_start[0].id is None
    broken = parse_next(_next(backlog={"folder": "backlog", "error": "two entries with id 1"}))
    assert broken.backlog is None and broken.backlog_error == "two entries with id 1"


def test_parse_row_normalizes_int_id_to_str():
    view = parse_next(_next(ready_to_start=[_row(id=7, after=[3, "2.1", "epic-cart"])]))
    row = view.ready_to_start[0]
    assert row.id == "7"
    assert row.after == ("3", "2.1", "epic-cart")
    assert parse_next(_next(ready_to_start=[_row(id="6a")])).ready_to_start[0].id == "6a"


@pytest.mark.parametrize(
    "field", ["ref", "epic", "id", "status", "state", "hitl", "refine", "after"]
)
def test_parse_row_missing_pinned_key_is_schema_error(field):
    row = _row()
    del row[field]
    with pytest.raises(TicketsSchemaError) as caught:
        parse_next(_next(ready_to_start=[row]))
    assert caught.value.field == field and caught.value.command == "next"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hitl", "yes"),
        ("refined", 1),
        ("id", True),  # bool is an int subclass; never an id upstream
        ("after", "1"),  # a bare string must not explode per character
        ("after", [None]),
        ("title", 5),
        ("status", "shipped"),
        ("state", "wip"),
        ("blocked_at", ["2026-01-01"]),
    ],
)
def test_parse_row_mistyped_value_is_schema_error(field, value):
    with pytest.raises(TicketsSchemaError) as caught:
        parse_next(_next(ready_to_start=[_row(**{field: value})]))
    assert caught.value.field == field


def test_optional_checkpoint_flags_default_false():
    row = parse_next(_next(ready_to_start=[_row()])).ready_to_start[0]
    assert (row.plan_checkpoint, row.done_checkpoint, row.drift) == (False, False, False)
    assert (row.unknown, row.gated_by, row.waiting_on, row.blocks) == ("", (), (), ())
    flagged = parse_next(
        _next(
            ready_to_start=[
                _row(
                    plan_checkpoint=True,
                    done_checkpoint=True,
                    drift={"hitl": {"file": True, "entry": False}},
                    gated_by=["epic-cart"],
                    unknown="which tax table?",
                )
            ]
        )
    ).ready_to_start[0]
    assert (flagged.plan_checkpoint, flagged.done_checkpoint, flagged.drift) == (True, True, True)
    assert flagged.gated_by == ("epic-cart",) and flagged.unknown == "which tax table?"


@pytest.mark.parametrize("value", [1, "true", False, None])
def test_checkpoint_non_bool_is_schema_error(value):
    """Upstream prints the flag only when set, so present-but-not-`true` is skew.
    Ablation: reading the flag with `bool(...)` instead of `is True` lets every
    value but False/None through and turns this red."""
    for field in ("plan_checkpoint", "done_checkpoint"):
        with pytest.raises(TicketsSchemaError) as caught:
            parse_next(_next(ready_to_start=[_row(**{field: value})]))
        assert caught.value.field == field


def test_blocked_at_bare_int_reads_as_text():
    row = parse_next(_next(blocked=[_row(blocked_at=2026, status="blocked", state="in-progress")]))
    assert row.blocked[0].blocked_at == "2026"


@pytest.mark.parametrize("doc", [None, [], "next", 3])
def test_non_object_document_is_schema_error(doc):
    for parse in (parse_next, parse_status, parse_find, parse_mark):
        with pytest.raises(TicketsSchemaError):
            parse(doc)


def test_parse_next_missing_group_is_schema_error():
    doc = _next()
    del doc["in_progress"]
    with pytest.raises(TicketsSchemaError) as caught:
        parse_next(doc)
    assert caught.value.field == "in_progress"


def _status(**over: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "store": "repo",
        "folder": "initiative-demo",
        "tickets": [_row(blocks=[2]), _row(ref="1.2", id=2, after=[1])],
        "counts": {"total": 2, "planned": 2},
        "longest_remaining_chain": ["1.1", "1.2"],
        "unpinned_after": [],
        "undeclared_after": [],
        "order_conflict": [],
        "epics": [
            {
                "slug": "epic-cart",
                "id": 1,
                "status": "in-progress",
                "after": [],
                "gated_by": [],
                "blocks": [],
                "next_id": 3,
            },
            {
                "slug": "epic-checkout",
                "id": 2,
                "status": "in-progress",
                "after": [{"epic": "epic-cart", "needs": "epic-cart closed"}],
                "gated_by": ["epic-cart"],
                "blocks": [],
                "next_id": 1,
            },
        ],
    }
    doc.update(over)
    return doc


def test_parse_status_reads_tickets_counts_and_epics():
    view = parse_status(_status())
    assert [t.ref for t in view.tickets] == ["1.1", "1.2"]
    assert view.tickets[0].blocks == ("2",)
    assert dict(view.counts) == {"total": 2, "planned": 2}
    assert view.longest_remaining_chain == ("1.1", "1.2")
    assert [e.slug for e in view.epics] == ["epic-cart", "epic-checkout"]
    checkout = view.epics[1]
    assert checkout.id == "2" and checkout.gated_by == ("epic-cart",)
    assert checkout.after[0].epic == "epic-cart" and checkout.after[0].needs == "epic-cart closed"
    assert view.next_id is None


def test_parse_status_single_folder_carries_next_id_and_no_epics():
    doc = _status(next_id=4)
    del doc["epics"]
    view = parse_status(doc)
    assert view.epics == () and view.next_id == 4


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["counts"].update(total="2"),
        lambda d: d["epics"][0].pop("next_id"),
        lambda d: d["epics"][1]["after"][0].pop("epic"),
        lambda d: d.pop("tickets"),
    ],
    ids=["count-not-int", "epic-no-next-id", "edge-no-epic", "no-tickets"],
)
def test_parse_status_malformed_is_schema_error(mutate):
    doc = copy.deepcopy(_status())
    mutate(doc)
    with pytest.raises(TicketsSchemaError):
        parse_status(doc)


def _find(**over: object) -> dict[str, object]:
    doc = _row(file="story-cart-model.md", refined=True)
    doc.update(
        {
            "folder": "epic-cart",
            "description": "",
            "verify": "",
            "references": [],
            "notes": [],
            "unknown": "",
            "epic_file": "/p/_bmad-output/initiative-demo/epic-cart/epic-cart.md",
            "story_file": "/p/_bmad-output/initiative-demo/epic-cart/story-cart-model.md",
            "plan": "/p/_bmad-output/initiative-demo/epic-cart/story-cart-model-plan.md",
        }
    )
    doc.update(over)
    return doc


def test_parse_find_reads_row_and_locations():
    found = parse_find(_find())
    assert found.row.ref == "1.1" and found.row.refined is True
    assert found.folder == "epic-cart"
    assert found.plan.endswith("story-cart-model-plan.md")
    assert found.story_file is not None and found.story_file.endswith("story-cart-model.md")
    entry = parse_find(_find(file=None, story_file=None, description="d", verify="v"))
    assert entry.story_file is None and (entry.description, entry.verify) == ("d", "v")


@pytest.mark.parametrize("field", ["plan", "folder", "epic_file", "story_file"])
def test_parse_find_missing_location_is_schema_error(field):
    doc = _find()
    del doc[field]
    with pytest.raises(TicketsSchemaError) as caught:
        parse_find(doc)
    assert caught.value.command == "find" and caught.value.field == field


def test_parse_mark_reads_written_fields():
    report = parse_mark(
        {
            "plan": "/p/x-plan.md",
            "created": False,
            "status": "blocked",
            "assignee": "",
            "blocked_at": "2026-10-14",
            "blocked_reason": "awaiting operator: approve",
        }
    )
    assert (report.status, report.created) == ("blocked", False)
    assert report.blocked_reason == "awaiting operator: approve"
    with pytest.raises(TicketsSchemaError) as caught:
        parse_mark({"plan": "/p/x-plan.md", "created": "no", "status": "done"})
    assert caught.value.field == "created"


TOP_HELP = """usage: tickets.py [-h] [--project-root PROJECT_ROOT]
                  {next,status,find,pull,mark,mirror} ...

tickets — read a ticket tree. Placeholders like {output} and {project-root} in
the description are not choice sets.
"""
MARK_HELP = """usage: tickets.py mark [-h] [--assignee ASSIGNEE] [--blocked REASON]
                       [dir] ref
                       {draft,ready-for-dev,in-progress,in-review,built,done,blocked,dropped}

Set a ticket's status in its plan.
"""


def test_parse_help_extracts_verbs_and_mark_statuses():
    cap = parse_help(TOP_HELP, MARK_HELP, requires_python=">=3.11")
    assert cap.verbs == frozenset({"next", "status", "find", "pull", "mark", "mirror"})
    assert cap.statuses == frozenset(ticketmodel.PLAN_STATUSES)
    assert cap.requires_python == ">=3.11"
    assert capability_problems(cap) == []


def test_parse_help_reads_only_the_usage_block():
    """Ablation: scanning the whole help text picks up `{output}`/`{project-root}`
    from the description and turns this red."""
    cap = parse_help(TOP_HELP, MARK_HELP)
    assert "project-root" not in cap.verbs and "output" not in cap.verbs


def test_capability_problems_names_missing_built():
    cap = Capability(verbs=REQUIRED_VERBS, statuses=REQUIRED_STATUSES - {"built"})
    assert capability_problems(cap) == ["tickets.py mark does not accept status `built`"]
    no_find = Capability(verbs=REQUIRED_VERBS - {"find"}, statuses=REQUIRED_STATUSES)
    assert capability_problems(no_find) == ["tickets.py has no `find` command"]


def test_required_statuses_cover_every_plan_transition_target():
    """The probe checks every status `mark` may write, not just the happy path.
    Ablation: pin REQUIRED_STATUSES back to {built, done, blocked} and this FAILS
    (replan's `draft`, rearm/rollback's `ready-for-dev`, ... go unprobed)."""
    from bmad_loop.statemachine import PLAN_TRANSITIONS

    targets = {s for rule in PLAN_TRANSITIONS.values() for s in rule.targets}
    assert REQUIRED_STATUSES == targets
    assert {"draft", "ready-for-dev", "in-progress", "in-review"} <= REQUIRED_STATUSES
    cap = Capability(verbs=REQUIRED_VERBS, statuses=frozenset({"built", "done", "blocked"}))
    assert capability_problems(cap) == [
        f"tickets.py mark does not accept status `{s}`"
        for s in ("draft", "in-progress", "in-review", "ready-for-dev")
    ]


def test_parse_requires_python_reads_the_pep723_block():
    script = (
        '#!/usr/bin/env python3\n# /// script\n# requires-python = ">=3.11"\n# ///\n"""doc"""\n'
    )
    assert parse_requires_python(script) == ">=3.11"
    assert parse_requires_python('"""no header"""\n# requires-python = ">=3.9"\n') is None
    assert parse_requires_python("# /// script\n# dependencies = []\n# ///\n") is None


@pytest.mark.parametrize(
    ("spec", "version", "expected"),
    [
        (">=3.11", (3, 11, 0), True),
        (">=3.11", (3, 13, 1), True),
        (">=3.12", (3, 11, 9), False),
        (">= 3.11.2", (3, 11, 2), True),
        (">= 3.11.2", (3, 11, 1), False),
        (">=3.11.2", (3, 12, 0), True),
        (">=4.0", (3, 13, 0), False),
        ("~=3.11", (3, 11, 0), None),
        (">=3.11,<4", (3, 11, 0), None),
        ("==3.11", (3, 11, 0), None),
        (None, (3, 11, 0), None),
    ],
)
def test_requires_python_only_understands_ge(spec, version, expected):
    assert requires_python_satisfied(spec, version) is expected
