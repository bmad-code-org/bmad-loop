"""Static work-source registry (`worksource.py`) and the per-source dispatch
tables that key on it.

The tables are what force a new source to visit every site that branches per
source: a source registered in `worksource.SOURCES` without a row in one of them
fails `test_dispatch_tables_are_exhaustive` here, rather than a `KeyError` at the
first run that reaches the site.
"""

from __future__ import annotations

import dataclasses

import pytest

from bmad_loop import cli, documents, policy, runsetup, worksource
from bmad_loop.checks import ValidationReport


def test_sources_equal_policy_stories_sources():
    """The registry and the policy enum name the same sources. policy.py must not
    import worksource (the settings sync test resolves `STORIES_SOURCES` inside
    policy.py), so this is the only thing keeping the two in step. Compared as
    sets: `STORIES_SOURCES` is one."""
    assert set(worksource.SOURCES) == policy.STORIES_SOURCES
    assert len(worksource.SOURCES) == len(set(worksource.SOURCES))


def test_every_source_has_traits():
    assert set(worksource.TRAITS) == set(worksource.SOURCES)
    for source in worksource.SOURCES:
        assert worksource.traits_for(source).name == source


def test_traits_for_refuses_an_unknown_source():
    """Ablation: replacing the raise in `traits_for` with a sprint fallback
    turns this red."""
    with pytest.raises(worksource.UnknownSource, match="'bogus'"):
        worksource.traits_for("bogus")


def test_resumed_traits_for_falls_back_to_sprint_for_an_unknown_source():
    """The read-side lookup (status, TUI, rollback bookkeeping) never crashes on a
    run state another version wrote: an unrecognised source reads as sprint."""
    assert worksource.resumed_traits_for("bogus") is worksource.TRAITS["sprint-status"]
    assert worksource.resumed_traits_for("stories") is worksource.TRAITS["stories"]


def test_resumable_traits_for_refuses_an_unknown_source():
    """Resuming a run whose persisted source this version does not know is
    refused, never rebuilt as a sprint run.

    Ablation: replacing the raise in `resumable_traits_for` with the sprint
    fallback turns this red."""
    with pytest.raises(worksource.UnknownSource, match="'bogus'.*do not downgrade"):
        worksource.resumable_traits_for("bogus")
    assert worksource.resumable_traits_for("tickets") is worksource.TRAITS["tickets"]


def test_v6_traits_are_pinned():
    """Every trait of the two v6 sources, field by field. A trait flip changes the
    behavior of each site that reads it, so it must be a deliberate edit here."""
    assert dataclasses.asdict(worksource.TRAITS["sprint-status"]) == {
        "name": "sprint-status",
        "label": "sprint mode — sprint-status.yaml",
        "validate_mode": "sprint",
        "has_epics": True,
        "has_spec_folder": False,
        "stories_primitive": False,
        "sentinels": False,
        "operator_park": True,
        "replay": True,
        "runnable": True,
        "sprint_board": True,
    }
    assert dataclasses.asdict(worksource.TRAITS["stories"]) == {
        "name": "stories",
        "label": "stories mode — folder+id dispatch",
        "validate_mode": "stories",
        "has_epics": False,
        "has_spec_folder": True,
        "stories_primitive": True,
        "sentinels": True,
        "operator_park": False,
        "replay": False,
        "runnable": True,
        "sprint_board": True,
    }


def test_tickets_traits_are_pinned():
    """The ticket-tree source is registered but not runnable yet: it has no
    numbered epics (its epics are keyed by slug), no spec folder, no stories
    primitive, no sentinels, no replay and no sprint board; it parks at
    awaiting-operator."""
    assert dataclasses.asdict(worksource.TRAITS["tickets"]) == {
        "name": "tickets",
        "label": "tickets mode — BMAD v7 ticket tree",
        "validate_mode": "tickets",
        "has_epics": False,
        "has_spec_folder": False,
        "stories_primitive": False,
        "sentinels": False,
        "operator_park": True,
        "replay": False,
        "runnable": False,
        "sprint_board": False,
    }


@pytest.mark.parametrize(
    ("source", "retrospective", "roles"),
    [
        ("sprint-status", "auto", ("dev", "review", "retro")),
        ("sprint-status", "notify", ("dev", "review")),
        ("sprint-status", "off", ("dev", "review")),
        ("stories", "auto", ("dev", "review")),
        ("stories", "off", ("dev", "review")),
        ("tickets", "auto", ("dev", "review")),
    ],
)
def test_launch_roles(source, retrospective, roles):
    """Only a source with epics launches the auto retrospective's `retro` role —
    the same roles `cmd_run` / resume warned about before the registry."""
    assert worksource.launch_roles(source, retrospective) == roles


def test_validate_mode():
    assert worksource.validate_mode("sprint-status") == "sprint"
    assert worksource.validate_mode("stories") == "stories"
    assert worksource.validate_mode("tickets") == "tickets"


def _dispatch_tables():
    """Every per-source table, by display name. The TUI table is imported lazily:
    the `tui` extra is optional."""
    tables = {
        f"cli.{name}": getattr(cli, name)
        for name in (
            "_DRY_RUNNERS",
            "_STATUS_PRINTERS",
            "_QUEUE_VALIDATORS",
            "_RUN_PREFLIGHTS",
            "_ACTIONABLE_KEY_READERS",
        )
    }
    try:
        from bmad_loop.tui import data
    except ImportError:  # pragma: no cover - the tui extra is installed in CI
        return tables
    tables["tui.data.BOARD_LOADERS"] = data.BOARD_LOADERS
    return tables


@pytest.mark.parametrize("table", sorted(_dispatch_tables()))
def test_dispatch_tables_are_exhaustive(table):
    """Each per-source table (cli dispatch, TUI board loaders) carries exactly one
    row per registered source.

    Ablation (performed): deleting the `stories` row from `_DRY_RUNNERS`, or from
    `tui.data.BOARD_LOADERS`, turns this red."""
    assert set(_dispatch_tables()[table]) == set(worksource.SOURCES)


@pytest.mark.parametrize("source", worksource.SOURCES)
def test_validate_document_mode_per_source(source):
    """`validate --json` reports each source's `validate_mode` trait as `mode`,
    carries the spec folder only for a source that pins one and the initiative
    only in tickets mode, and always carries the era it was handed."""
    traits = worksource.traits_for(source)
    doc = documents.validate_document(
        ValidationReport(), traits.validate_mode, "specs/feat", initiative="cart", era="v7"
    )
    assert doc["mode"] == traits.validate_mode
    assert doc["spec_folder"] == ("specs/feat" if traits.has_spec_folder else "")
    assert doc["initiative"] == ("cart" if source == worksource.SOURCE_TICKETS else "")
    assert doc["era"] == "v7"
    bare = documents.validate_document(ValidationReport(), traits.validate_mode, "specs/feat")
    assert (bare["initiative"], bare["era"]) == ("", "")


def _run_side_tickets_rows():
    """Every run-side dispatch row of the tickets source, called with dummy
    arguments: each must refuse before it reads any of them."""
    rows = {
        "cli._STATUS_PRINTERS": lambda: cli._STATUS_PRINTERS["tickets"](None, None),
        "cli._RUN_PREFLIGHTS": lambda: cli._RUN_PREFLIGHTS["tickets"](None, None, ""),
        "cli._ACTIONABLE_KEY_READERS": lambda: cli._ACTIONABLE_KEY_READERS["tickets"](None, ""),
        "runsetup._engine_cls_for": lambda: runsetup._engine_cls_for(
            "tickets", "", engine_cls=object, stories_engine_cls=object
        ),
    }
    try:
        from bmad_loop.tui import data
    except ImportError:  # pragma: no cover - the tui extra is installed in CI
        return rows
    rows["tui.data.BOARD_LOADERS"] = lambda: data.BOARD_LOADERS["tickets"](None, None)
    return rows


@pytest.mark.parametrize("row", sorted(_run_side_tickets_rows()))
def test_tickets_run_side_rows_refuse(row):
    """Tickets runs land in a later release layer: every run-side row refuses with
    the typed `SourceNotRunnable`, which `cli.main` reports at exit 1.

    Ablation (performed): pointing the tickets `_RUN_PREFLIGHTS` row at
    `_preflight_sprint_queue` turns its row red (it reads the board instead)."""
    with pytest.raises(worksource.SourceNotRunnable, match="tickets mode cannot run yet"):
        _run_side_tickets_rows()[row]()


async def test_modal_select_options_match_runnable_traits(tmp_path, monkeypatch):
    """The start-run modal offers exactly the runnable sources, labelled and
    ordered by the registry.

    Ablation (in-test): a source flipped to `runnable=False` drops out of the
    picker — so the options are read from the traits, not a hand-kept list."""
    pytest.importorskip("textual")
    from textual.app import App
    from textual.widgets import Select

    from bmad_loop.tui.screens.modals import StartRunModal

    async def options() -> list[tuple[object, object]]:
        class _Host(App[None]):
            def on_mount(self) -> None:
                self.push_screen(StartRunModal(tmp_path))

        app = _Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            return list(app.screen.query_one("#source", Select)._options)

    def runnable():
        return [(t.label, t.name) for t in worksource.TRAITS.values() if t.runnable]

    assert await options() == runnable()
    # Tickets is registered but not runnable yet, so the picker must not offer it.
    assert [name for _, name in runnable()] == ["sprint-status", "stories"]

    stories = worksource.TRAITS[worksource.SOURCE_STORIES]
    monkeypatch.setitem(
        worksource.TRAITS,
        worksource.SOURCE_STORIES,
        dataclasses.replace(stories, runnable=False),
    )
    assert await options() == [
        (worksource.TRAITS[worksource.SOURCE_SPRINT].label, worksource.SOURCE_SPRINT)
    ]
