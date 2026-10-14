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

from bmad_loop import cli, documents, policy, worksource
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
    """A persisted run with an unrecognised source has always been resumed,
    reported and replayed as a sprint run; the resume-side lookup keeps that."""
    assert worksource.resumed_traits_for("bogus") is worksource.TRAITS["sprint-status"]
    assert worksource.resumed_traits_for("stories") is worksource.TRAITS["stories"]


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
    }


@pytest.mark.parametrize(
    ("source", "retrospective", "roles"),
    [
        ("sprint-status", "auto", ("dev", "review", "retro")),
        ("sprint-status", "notify", ("dev", "review")),
        ("sprint-status", "off", ("dev", "review")),
        ("stories", "auto", ("dev", "review")),
        ("stories", "off", ("dev", "review")),
    ],
)
def test_launch_roles(source, retrospective, roles):
    """Only a source with epics launches the auto retrospective's `retro` role —
    the same roles `cmd_run` / resume warned about before the registry."""
    assert worksource.launch_roles(source, retrospective) == roles


def test_validate_mode():
    assert worksource.validate_mode("sprint-status") == "sprint"
    assert worksource.validate_mode("stories") == "stories"


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
    and carries the spec folder only for a source that pins one."""
    traits = worksource.traits_for(source)
    doc = documents.validate_document(ValidationReport(), traits.validate_mode, "specs/feat")
    assert doc["mode"] == traits.validate_mode
    assert doc["spec_folder"] == ("specs/feat" if traits.has_spec_folder else "")
    # Reserved for the ticket-tree source: accepted, not yet emitted.
    assert (
        documents.validate_document(
            ValidationReport(), traits.validate_mode, "specs/feat", initiative="i", era="v7"
        )
        == doc
    )


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
    assert [name for _, name in runnable()] == list(worksource.SOURCES)

    stories = worksource.TRAITS[worksource.SOURCE_STORIES]
    monkeypatch.setitem(
        worksource.TRAITS,
        worksource.SOURCE_STORIES,
        dataclasses.replace(stories, runnable=False),
    )
    assert await options() == [
        (worksource.TRAITS[worksource.SOURCE_SPRINT].label, worksource.SOURCE_SPRINT)
    ]
