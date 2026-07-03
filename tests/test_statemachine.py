import pytest

from bmad_loop.model import Phase, StoryTask
from bmad_loop.statemachine import TRANSITIONS, IllegalTransition, advance


def test_table_covers_every_phase():
    assert set(TRANSITIONS) == set(Phase)


@pytest.mark.parametrize("source", list(Phase))
@pytest.mark.parametrize("target", list(Phase))
def test_exhaustive_transitions(source, target):
    task = StoryTask(story_key="1-1-x", epic=1, phase=source)
    if target in TRANSITIONS[source]:
        advance(task, target)
        assert task.phase == target
    else:
        with pytest.raises(IllegalTransition):
            advance(task, target)
        assert task.phase == source


def test_happy_path_sequence():
    task = StoryTask(story_key="1-1-x", epic=1)
    for phase in (
        Phase.DEV_RUNNING,
        Phase.DEV_VERIFY,
        Phase.REVIEW_RUNNING,
        Phase.REVIEW_VERIFY,
        Phase.COMMITTING,
        Phase.DONE,
    ):
        advance(task, phase)
    assert task.terminal


def test_triage_path_sequence():
    task = StoryTask(story_key="sweep-triage", epic=0)
    for phase in (
        Phase.TRIAGE_RUNNING,
        Phase.TRIAGE_VERIFY,
        Phase.TRIAGE_RUNNING,  # invalid triage output retries
        Phase.TRIAGE_VERIFY,
        Phase.DONE,
    ):
        advance(task, phase)
    assert task.terminal
