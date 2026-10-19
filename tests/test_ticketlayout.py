"""`ticketlayout`: where a v7 container keeps its breakdown — its `tickets/`
folder when that is a directory, else the container itself — and the paths
derived from it. Real-spawn budget: 0 spawns of tickets.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bmad_loop import ticketlayout


def test_breakdown_dir_reads_the_tickets_folder_when_present(tmp_path: Path):
    """A container with a `tickets/` directory keeps its breakdown there."""
    (tmp_path / "init" / "tickets").mkdir(parents=True)
    assert ticketlayout.breakdown_dir(tmp_path / "init") == tmp_path / "init" / "tickets"


def test_breakdown_dir_reads_the_flat_folder_when_absent(tmp_path: Path):
    """A container without one (the preview layout, `backlog/`) is its own
    breakdown folder."""
    container = tmp_path / "init"
    container.mkdir()
    (container / "tickets.toml").write_text("[[epic]]\n", encoding="utf-8")
    assert ticketlayout.breakdown_dir(container) == container


def test_breakdown_dir_reads_a_missing_container_as_flat(tmp_path: Path):
    """A container that does not exist reads as flat: absence, not a fault."""
    assert ticketlayout.breakdown_dir(tmp_path / "nowhere") == tmp_path / "nowhere"


def test_breakdown_dir_treats_a_tickets_file_as_flat(tmp_path: Path):
    """A `tickets` that is a file, not a directory, is no breakdown folder."""
    container = tmp_path / "init"
    container.mkdir()
    (container / "tickets").write_text("", encoding="utf-8")
    assert ticketlayout.breakdown_dir(container) == container


def test_breakdown_dir_raises_on_a_refused_stat(tmp_path: Path, monkeypatch):
    """A stat fault other than absence raises; it is never read as the flat
    layout (`Path.is_dir` would answer False). Ablation (performed): catch
    `OSError` in `breakdown_dir` and the container comes back — this FAILS."""
    container = tmp_path / "init"
    (container / "tickets").mkdir(parents=True)
    target = container / "tickets"
    real = os.stat

    def refuse(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", refuse)
    with pytest.raises(PermissionError, match="Permission denied"):
        ticketlayout.breakdown_dir(container)


@pytest.mark.parametrize("nested", [True, False], ids=["tickets-folder", "flat"])
def test_retrospective_doc_sits_beside_the_epic_file(tmp_path: Path, nested: bool):
    """The retrospective sits at the epic folder's top, beside `<slug>.md`, in
    either layout: the epic folder is found in the initiative's breakdown folder."""
    initiative = tmp_path / "init"
    parent = initiative / "tickets" if nested else initiative
    epic = parent / "epic-cart"
    epic.mkdir(parents=True)
    if nested:
        (epic / "tickets").mkdir()
    assert ticketlayout.epic_folder(initiative, "epic-cart") == epic
    assert ticketlayout.retrospective_doc(initiative, "epic-cart") == (
        epic / "epic-cart-retrospective.md"
    )
