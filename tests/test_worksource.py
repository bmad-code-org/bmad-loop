"""Static work-source registry (`worksource.py`) and the per-source dispatch
tables that key on it.

The tables are what force a new source to visit every site that branches per
source: a source registered in `worksource.SOURCES` without a row in one of them
fails `test_cli_dispatch_tables_are_exhaustive` here, rather than a `KeyError`
at the first run that reaches the site.
"""

from __future__ import annotations

import dataclasses

import pytest

from bmad_loop import cli, policy, worksource


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


@pytest.mark.parametrize(
    "table",
    ["_DRY_RUNNERS", "_STATUS_PRINTERS", "_QUEUE_VALIDATORS", "_RUN_PREFLIGHTS"],
)
def test_cli_dispatch_tables_are_exhaustive(table):
    """Each cli per-source table carries exactly one row per registered source.

    Ablation (performed): deleting the `stories` row from `_DRY_RUNNERS` turns
    this red."""
    assert set(getattr(cli, table)) == set(worksource.SOURCES)


def test_actionable_key_readers_are_exhaustive():
    assert set(cli._ACTIONABLE_KEY_READERS) == set(worksource.SOURCES)
