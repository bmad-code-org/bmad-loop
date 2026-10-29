"""initiativechain.next_child: which ready child of a parent initiative a chain
builds next, and why the chain ends when none is taken."""

from __future__ import annotations

from bmad_loop.initiativechain import NO_FOLDER, PassedOver, WaitingOn, next_child
from bmad_loop.ticketmodel import ChildInitiative, InitiativeEdge, ParentView


def _child(
    slug: str,
    *,
    ready: bool = False,
    folder: str | None = "",
    status: str = "in-progress",
    after: tuple[str, ...] = (),
    waiting_on: tuple[str, ...] = (),
) -> ChildInitiative:
    """A child as `parse_parent_next` builds it; ``folder=""`` means "its slug",
    and a folder-less child carries no status, as upstream prints it."""
    return ChildInitiative(
        id=slug,
        slug=slug,
        title=slug.title(),
        after=tuple(InitiativeEdge(a, "") for a in after),
        folder=(slug if folder == "" else folder),
        status=(status if folder is not None else ""),
        counts={},
        waiting_on=waiting_on,
        ready=ready,
    )


def _view(*children: ChildInitiative) -> ParentView:
    return ParentView(
        store="repo",
        folder="initiative-platform",
        initiatives=children,
        ready=tuple(c.slug for c in children if c.ready),
        order_conflict=(),
    )


def test_takes_the_first_ready_child_in_build_order():
    done = _child("auth", status="done")
    billing = _child("billing", ready=True, after=("auth",))
    reports = _child("reports", ready=True)
    search = _child("search", after=("billing",), waiting_on=("billing",))

    pick = next_child(_view(done, billing, reports, search))

    assert pick.child is billing
    assert (pick.passed_over, pick.end) == ((), None)
    assert pick.waiting == ("search",)


def test_waiting_only_takes_no_child_and_ends_waiting():
    """Ablation (performed): drop the `ready` filter in `next_child` and the
    waiting child (it has a folder and is not done) is taken — this FAILS."""
    done = _child("auth", status="done")
    billing = _child("billing", after=("payments",), waiting_on=("payments",))
    payments = _child("payments", folder=None, after=("auth",), waiting_on=("billing",))

    pick = next_child(_view(done, billing, payments))

    assert pick.child is None and pick.end == "waiting"
    assert pick.waiting == ("billing", "payments")
    assert pick.passed_over == ()


def test_folder_less_ready_child_is_passed_over_and_reported():
    reports = _child("reports", ready=True, folder=None)
    billing = _child("billing", ready=True)

    pick = next_child(_view(reports, billing))

    assert pick.child is billing
    assert pick.passed_over == (PassedOver("reports", NO_FOLDER),)


def test_only_folder_less_ready_children_end_passed_over():
    pick = next_child(
        _view(_child("auth", status="done"), _child("reports", ready=True, folder=None))
    )

    assert pick.child is None and pick.end == "passed-over"
    assert pick.passed_over == (PassedOver("reports", NO_FOLDER),)
    assert pick.waiting == ()


def test_empty_parent_ends_all_done():
    pick = next_child(_view())

    assert (pick.child, pick.passed_over, pick.waiting, pick.end) == (None, (), (), "all-done")


def test_every_child_closed_ends_all_done():
    pick = next_child(_view(_child("auth", status="done"), _child("legacy", status="dropped")))

    assert pick.child is None and pick.end == "all-done" and pick.waiting == ()


def test_drained_child_keeps_its_dependents_waiting():
    """A child the probe passes over (nothing left to dispatch) whose initiative
    is not marked done leaves its dependents waiting: the chain ends `waiting`,
    naming both."""
    auth = _child("auth", ready=True)
    billing = _child("billing", after=("auth",), waiting_on=("auth",))
    asked: list[str] = []

    def drained(child: ChildInitiative) -> str | None:
        asked.append(child.slug)
        return "drained"

    pick = next_child(_view(auth, billing), drained)

    assert asked == ["auth"]
    assert pick.child is None and pick.end == "waiting"
    assert pick.passed_over == (PassedOver("auth", "drained"),)
    assert pick.waiting == ("billing",)


def test_probe_is_not_asked_about_a_folder_less_child():
    reports = _child("reports", ready=True, folder=None)
    billing = _child("billing", ready=True)
    asked: list[str] = []

    def take(child: ChildInitiative) -> str | None:
        asked.append(child.slug)
        return None

    pick = next_child(_view(reports, billing), take)

    assert asked == ["billing"] and pick.child is billing


def test_waiting_children_name_each_prerequisite_with_its_status():
    """One row per prerequisite a waiting child waits on, in build order, whether
    or not a child is taken: an initiative status, `open` for a folder with no status, `no-folder` for one
    not created yet, and `?` for a slug the parent does not list."""
    auth = _child("auth", ready=True, status="")
    search = _child("search", status="dropped")
    ledger = _child("ledger", ready=True, folder=None)
    billing = _child(
        "billing",
        after=("auth", "search", "ledger"),
        waiting_on=("auth", "search", "ledger"),
    )
    reports = _child("reports", status="in-progress", waiting_on=("billing", "gone"))

    pick = next_child(_view(auth, search, ledger, billing, reports))

    assert pick.child is auth
    assert pick.waiting == ("billing", "reports")
    assert pick.waiting_on == (
        WaitingOn("billing", "auth", "open"),
        WaitingOn("billing", "search", "dropped"),
        WaitingOn("billing", "ledger", NO_FOLDER),
        WaitingOn("reports", "billing", "in-progress"),
        WaitingOn("reports", "gone", "?"),
    )
