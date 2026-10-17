"""CheckpointFlowMixin's flags seam, at the unit layer.

The stories flows through the mixin are pinned end to end in
tests/test_stories_engine.py. What a stories run cannot reach is the ``refined``
flag (stories entries are never refined), so its gate is held here."""

from __future__ import annotations

import pytest

from bmad_loop.checkpoints import CheckpointFlags, CheckpointFlowMixin
from bmad_loop.model import StoryTask


class _Probe(CheckpointFlowMixin):
    """The mixin over a scripted ``_plan_produced``; counts the disk reads."""

    def __init__(self, produced: bool) -> None:
        self.produced = produced
        self.reads = 0

    def _plan_produced(self, task: StoryTask) -> bool:
        self.reads += 1
        return self.produced


@pytest.mark.parametrize(
    ("flags", "produced", "halts"),
    [
        (CheckpointFlags(plan_checkpoint=True, done_checkpoint=False), False, True),
        (CheckpointFlags(plan_checkpoint=True, done_checkpoint=False), True, False),
        (CheckpointFlags(plan_checkpoint=False, done_checkpoint=True), False, False),
        (CheckpointFlags(plan_checkpoint=True, done_checkpoint=False, refined=True), False, False),
    ],
    ids=["leg-1", "plan-produced", "no-plan-checkpoint", "refined"],
)
def test_plan_halt_leg_reads_the_flags(flags, produced, halts):
    """Only an unrefined plan_checkpoint unit with no plan on disk halts after
    planning. A refined unit's plan was approved before dispatch, so it never takes
    the two-leg halt.

    Ablation, performed: drop ``and not flags.refined`` from
    ``CheckpointFlowMixin._plan_halt_leg`` and the ``refined`` row reds."""
    probe = _Probe(produced)
    assert probe._plan_halt_leg(StoryTask("1", 0), flags) is halts
    assert probe.reads == 1  # a present unit always reads the disk


def test_plan_halt_leg_without_a_unit_never_reads():
    """No such unit: no halt, and no disk read."""
    probe = _Probe(produced=False)
    assert probe._plan_halt_leg(StoryTask("1", 0), None) is False
    assert probe.reads == 0
