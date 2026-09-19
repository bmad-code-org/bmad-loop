import pytest

from bmad_loop.model import TERMINAL_PHASES, Phase, StoryTask
from bmad_loop.statemachine import TRANSITIONS, IllegalTransition, advance


def test_table_covers_every_phase():
    assert set(TRANSITIONS) == set(Phase)


def test_terminal_phases_are_exactly_the_dead_ends():
    """`TERMINAL_PHASES` and the transition table are two spellings of the same
    fact, in two modules, with nothing linking them: a new terminal phase added
    to the table with no outgoing edges but forgotten in the frozenset would be
    driven as if it were still live (`StoryTask.terminal` is False), and every
    other test would still pass. Pin them to each other."""
    dead_ends = {phase for phase, targets in TRANSITIONS.items() if not targets}
    assert dead_ends == set(TERMINAL_PHASES)


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


def test_park_path_sequence():
    """A story owing human-only external actions rides the NORMAL commit path and
    parks at the end of it: COMMITTING is the only phase it is reachable from, so
    a park can never skip the gates and the commit a DONE story clears."""
    task = StoryTask(story_key="1-1-x", epic=1)
    for phase in (
        Phase.DEV_RUNNING,
        Phase.DEV_VERIFY,
        Phase.REVIEW_RUNNING,
        Phase.REVIEW_VERIFY,
        Phase.COMMITTING,
        Phase.AWAITING_OPERATOR,
    ):
        advance(task, phase)
    assert task.terminal


@pytest.mark.parametrize(
    "source",
    [p for p in Phase if p is not Phase.COMMITTING],
)
def test_awaiting_operator_is_reachable_only_from_committing(source):
    """The explicit inverse of the happy path above. `test_exhaustive_transitions`
    covers this pairwise too, but only as one cell of an N-squared grid whose
    expectation is read out of the table under test — this states the rule
    independently, so widening the table cannot silently widen the test."""
    task = StoryTask(story_key="1-1-x", epic=1, phase=source)
    with pytest.raises(IllegalTransition):
        advance(task, Phase.AWAITING_OPERATOR)
    assert task.phase == source


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


def test_migration_triage_commit_path_sequence():
    task = StoryTask(story_key="sweep-migrate", epic=0)
    for phase in (
        Phase.TRIAGE_RUNNING,
        Phase.TRIAGE_VERIFY,
        Phase.COMMITTING,
        Phase.DONE,
    ):
        advance(task, phase)
    assert task.terminal
