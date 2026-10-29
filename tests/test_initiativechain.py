"""initiativechain.next_child: which ready child of a parent initiative a chain
builds next, and why the chain ends when none is taken; next_child_after_close:
the same pick once the chain has closed its drained children."""

from __future__ import annotations

from bmad_loop.initiativechain import (
    DRAINED,
    NO_FOLDER,
    PassedOver,
    WaitingOn,
    drained,
    next_child,
    next_child_after_close,
)
from bmad_loop.ticketmodel import ChildInitiative, InitiativeEdge, ParentView


def _child(
    slug: str,
    *,
    ready: bool = False,
    folder: str | None = "",
    status: str = "in-progress",
    after: tuple[str, ...] = (),
    waiting_on: tuple[str, ...] = (),
    closed: int | None = None,
) -> ChildInitiative:
    """A child as `parse_parent_next` builds it; ``folder=""`` means "its slug",
    and a folder-less child carries no status, as upstream prints it. ``closed``
    gives it four tickets, that many of them done."""
    return ChildInitiative(
        id=slug,
        slug=slug,
        title=slug.title(),
        after=tuple(InitiativeEdge(a, "") for a in after),
        folder=(slug if folder == "" else folder),
        status=(status if folder is not None else ""),
        counts={} if closed is None else {"total": 4, "done": closed},
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


def _passes_drained(child: ChildInitiative) -> str | None:
    return DRAINED if drained(child) else None


def test_after_close_takes_the_dependent_of_a_drained_child():
    """AUTH drained and open, BILLING waiting on it: the chain closes AUTH before
    it picks, so the re-judged pick takes BILLING and names AUTH closed.

    Ablation (performed): return `next_child(view, passes_over)` without the
    re-judge and BILLING still waits — this FAILS."""
    auth = _child("auth", ready=True, status="", closed=4)
    billing = _child("billing", after=("auth",), waiting_on=("auth",), closed=0)

    pick = next_child_after_close(_view(auth, billing), _passes_drained)

    assert pick.child is not None and pick.child.slug == "billing"
    assert pick.child.ready and pick.child.waiting_on == ()
    assert pick.closed == ("auth",)
    assert (pick.passed_over, pick.waiting, pick.waiting_on, pick.end) == ((), (), (), None)


def test_after_close_keeps_a_child_waiting_on_an_undrained_prerequisite():
    """A genuinely blocked chain: BILLING waits on AUTH, open with tickets left
    and itself waiting on a folder-less IDENTITY. Nothing is drained, nothing
    closes, and no child can start."""
    identity = _child("identity", ready=True, folder=None)
    auth = _child("auth", status="", after=("identity",), waiting_on=("identity",), closed=1)
    billing = _child("billing", after=("auth",), waiting_on=("auth",), closed=0)

    pick = next_child_after_close(_view(identity, auth, billing), _passes_drained)

    assert pick.child is None and pick.end == "waiting"
    assert pick.closed == ()
    assert pick.passed_over == (PassedOver("identity", NO_FOLDER),)
    assert pick.waiting_on == (
        WaitingOn("auth", "identity", NO_FOLDER),
        WaitingOn("billing", "auth", "open"),
    )


def test_after_close_drops_only_the_closed_prerequisites():
    """REPORTS waits on drained AUTH and on in-progress BILLING: closing AUTH
    leaves it waiting on BILLING alone."""
    auth = _child("auth", ready=True, status="", closed=4)
    billing = _child("billing", ready=True, closed=0)
    reports = _child("reports", after=("auth", "billing"), waiting_on=("auth", "billing"))

    pick = next_child_after_close(_view(auth, billing, reports), _passes_drained)

    assert pick.child is billing
    assert pick.waiting == ("reports",)
    assert pick.waiting_on == (WaitingOn("reports", "billing", "in-progress"),)


def test_after_close_asks_closes_about_drained_children_only():
    """``closes`` is asked about each drained child; one it keeps open is judged
    as ``next`` read it — here passed over as drained, its dependent waiting."""
    auth = _child("auth", ready=True, status="", closed=4)
    billing = _child("billing", after=("auth",), waiting_on=("auth",), closed=0)
    asked: list[str] = []

    def closes(child: ChildInitiative) -> bool:
        asked.append(child.slug)
        return False

    pick = next_child_after_close(_view(auth, billing), _passes_drained, closes)

    assert asked == ["auth"]
    assert pick.child is None and pick.end == "waiting" and pick.closed == ()
    assert pick.passed_over == (PassedOver("auth", DRAINED),)


def test_after_close_carries_the_rejudged_children():
    """AUTH drained, BILLING and REPORTS both after it: the pick takes BILLING,
    and ``children`` holds the view it was judged on — AUTH read ``done``, REPORTS
    ready with no wait left — so a tagger reads REPORTS ready, not waiting.

    Ablation (performed): carry ``view.initiatives`` (the parent as ``next`` read
    it) in ``next_child_after_close``'s ``children`` and this FAILS."""
    auth = _child("auth", ready=True, status="", closed=4)
    billing = _child("billing", after=("auth",), waiting_on=("auth",), closed=0)
    reports = _child("reports", after=("auth",), waiting_on=("auth",), closed=0)

    pick = next_child_after_close(_view(auth, billing, reports), _passes_drained)

    assert pick.child is not None and pick.child.slug == "billing"
    assert pick.closed == ("auth",) and pick.waiting == ()
    assert [(c.slug, c.status, c.ready, c.waiting_on) for c in pick.children] == [
        ("auth", "done", False, ()),
        ("billing", "in-progress", True, ()),
        ("reports", "in-progress", True, ()),
    ]


def test_after_close_readies_a_folder_less_dependent():
    """REPORTS, declared without a folder, after drained AUTH: once AUTH closes it
    is ready (a tagger reads it as having no folder, not waiting), and the pick
    passes it over ``no-folder`` on the way to nothing else."""
    auth = _child("auth", ready=True, status="", closed=4)
    reports = _child("reports", folder=None, after=("auth",), waiting_on=("auth",))

    pick = next_child_after_close(_view(auth, reports), _passes_drained)

    assert pick.child is None and pick.end == "passed-over"
    assert pick.passed_over == (PassedOver("reports", NO_FOLDER),)
    rejudged = pick.children[1]
    assert (rejudged.slug, rejudged.ready, rejudged.folder) == ("reports", True, None)


def test_next_child_carries_the_view_children():
    """Without a close, ``children`` is the view's own children, as ``next`` read
    them."""
    auth = _child("auth", ready=True)
    billing = _child("billing", after=("auth",), waiting_on=("auth",))
    view = _view(auth, billing)

    assert next_child(view).children == view.initiatives
    assert next_child_after_close(view).children == view.initiatives
