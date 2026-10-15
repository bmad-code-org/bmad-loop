"""`bmad_loop.tickettree`: contract rows against the vendored `tickets.py` on
`project_v7`, plus error mapping through fake scripts and canned child results.

Real-spawn budget: **14 spawns of the vendored `tickets.py`** — next 1, status 1,
find 2 (a hit and a miss), capability 2 (`--help`, `mark --help`; the cached
second call spawns nothing), and the mark chokepoint 8 (write + read back: find
and mark, 2; refused below built: find, 1; missing plan: find, 1; park then
re-arm: 2 × find and mark, 4). The fake-script rows add 5 short Python children
(timeout 1, exit-2 JSON 1, exit-2 text 1, capability 2); every other row maps a
canned `childrun.ChildRun`, answers `find`/`mark` in-process, or refuses before
spawning, at no spawn cost. The Windows leg sits at its time budget, so keep new
rows on the canned path."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sys
from pathlib import Path

import pytest
from conftest import (
    V7_USER_CONFIG_REL,
    FakeTicketTree,
    V7Layout,
    fake_find,
    ticket_row,
    write_plan,
    write_script_launcher,
)

from bmad_loop import childrun, frontmatter, platform_util, runs, tickettree
from bmad_loop.childrun import ChildRun
from bmad_loop.journal import Journal
from bmad_loop.statemachine import PlanTransitionRefused
from bmad_loop.ticketmodel import NEXT_GROUPS, capability_problems
from bmad_loop.tickettree import (
    TicketsCrashed,
    TicketsError,
    TicketsFault,
    TicketsInterrupted,
    TicketsLockBusy,
    TicketsNoMatch,
    TicketsPinMismatch,
    TicketsPlanIOError,
    TicketsPlanMismatch,
    TicketsReadBackMismatch,
    TicketsSchemaError,
    TicketsStoreRefusal,
    TicketsTimeout,
    TicketsTransitionRefused,
    TicketsUnavailable,
    TicketsUsageError,
    TicketTree,
    TicketTreeLike,
)

HEADER = '# /// script\n# requires-python = ">=3.11"\n# ///\n'


@pytest.fixture(autouse=True)
def _fresh_capability_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tickettree, "_CAPABILITIES", {})


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every argv handed to `childrun.run_argv` (the real spawn still happens)."""
    seen: list[list[str]] = []
    real = childrun.run_argv

    def spy(argv, **kw):
        seen.append(list(argv))
        return real(argv, **kw)

    monkeypatch.setattr(childrun, "run_argv", spy)
    return seen


def canned(monkeypatch: pytest.MonkeyPatch, run: ChildRun) -> list[list[str]]:
    """Answer every spawn with ``run`` and spawn nothing. Asserts the spawn asked
    for UTF-8 decoding (ablation: dropping `encoding="utf-8"` from `_call` turns
    every canned row red)."""
    seen: list[list[str]] = []

    def fake(argv, **kw):
        seen.append(list(argv))
        assert kw["encoding"] == "utf-8"
        return run

    monkeypatch.setattr(childrun, "run_argv", fake)
    return seen


def fake_script(tmp_path: Path, body: str) -> Path:
    """A fake `tickets.py` in the `write_script_launcher` style: a Python file the
    tree runs through the interpreter launcher (the PEP 723 header picks it)."""
    launcher = write_script_launcher(tmp_path, "tickets", HEADER + body)
    return launcher.with_name("tickets.py")


def tree_at(tmp_path: Path, script: Path | None = None, **kw) -> TicketTree:
    return TicketTree(tmp_path, script or fake_script(tmp_path, "raise SystemExit(0)\n"), **kw)


# ---------------------------------------------------------------- contract


def test_next_against_vendored_tickets_py_parses(project_v7: V7Layout, spawns):
    view = TicketTree(project_v7.project, pinned_initiative=project_v7.initiative).next()
    assert len(spawns) == 1
    argv = spawns[0]
    assert argv[:5] == [sys.executable, "-I", "-B", "-X", "utf8"]
    assert argv[5:] == [
        str(project_v7.tickets_script),
        "--project-root",
        str(project_v7.project),
        "next",
    ]
    assert (view.store, view.folder) == ("repo", project_v7.initiative)
    by_group = {g: [r.ref for r in view.group(g)] for g in NEXT_GROUPS}
    assert by_group == {
        "ready_to_refine": [],
        "ready_to_start": ["1.1"],
        "in_progress": [],
        "blocked": ["1.2", "2.1", "2.2"],
    }
    first = view.ready_to_start[0]
    assert (first.epic, first.id, first.plan_checkpoint, first.done_checkpoint) == (
        "epic-cart",
        "1",
        True,
        True,
    )
    blocked = {r.ref: r for r in view.blocked}
    assert blocked["1.2"].hitl and blocked["1.2"].waiting_on == ("1",)
    assert blocked["2.1"].refined and blocked["2.1"].gated_by == ("epic-cart",)
    assert blocked["2.2"].type == "bug" and blocked["2.2"].refine
    assert view.backlog is None


def test_status_epics_rows_parse(project_v7: V7Layout):
    view = TicketTree(project_v7.project).status()
    assert [t.ref for t in view.tickets] == ["1.1", "1.2", "2.1", "2.2"]
    assert view.counts["total"] == 4
    assert [(e.slug, e.id, e.status) for e in view.epics] == [
        ("epic-cart", "1", "in-progress"),
        ("epic-checkout", "2", "in-progress"),
    ]
    checkout = view.epics[1]
    assert checkout.gated_by == ("epic-cart",) and checkout.after[0].epic == "epic-cart"
    assert view.tickets[0].blocks == ("2",)
    assert view.longest_remaining_chain == ("1.1", "1.2")


def test_find_returns_absolute_plan_path_under_initiative(project_v7: V7Layout):
    found = TicketTree(project_v7.project).find("2.1")
    epic_dir = project_v7.initiative_folder / "epic-checkout"
    assert Path(found.plan).is_absolute()
    assert Path(found.plan) == (epic_dir / "story-checkout-form-plan.md").resolve()
    assert not Path(found.plan).exists()  # where a build would write it
    assert found.story_file is not None and Path(found.story_file).is_file()
    assert found.epic_file is not None and Path(found.epic_file).name == "epic-checkout.md"
    assert (found.row.ref, found.row.refined, found.folder) == ("2.1", True, "epic-checkout")


def test_find_unknown_ref_is_no_match(project_v7: V7Layout):
    with pytest.raises(TicketsNoMatch) as caught:
        TicketTree(project_v7.project).find("9.9")
    assert isinstance(caught.value, TicketsFault)
    assert caught.value.message == "no ticket matches '9.9'"


def test_capability_of_vendored_script_is_complete(project_v7: V7Layout, spawns):
    tree = TicketTree(project_v7.project)
    cap = tree.capability()
    assert capability_problems(cap) == []
    assert {"next", "status", "find", "mark"} <= cap.verbs
    assert cap.requires_python == ">=3.11"
    assert [a[8:] for a in spawns] == [["--help"], ["mark", "--help"]]
    # A fresh instance on the unchanged script answers from the cache.
    assert TicketTree(project_v7.project).capability() == cap
    assert len(spawns) == 2


# ---------------------------------------------------------------- launcher


def test_launcher_prefers_running_interpreter(tmp_path: Path):
    prefix, env = tree_at(tmp_path).launcher()
    assert prefix == [sys.executable, "-I", "-B", "-X", "utf8"] and env is None


def test_launcher_falls_back_to_uv_when_requires_python_unmet(tmp_path, monkeypatch):
    monkeypatch.setattr(tickettree, "_running_version", lambda: (3, 10, 0))
    monkeypatch.setattr(tickettree.shutil, "which", lambda name: f"/opt/bin/{name}")
    prefix, env = tree_at(tmp_path).launcher()
    assert prefix == ["/opt/bin/uv", "run", "--no-project"]
    assert env is not None and env["PYTHONUTF8"] == "1"


def test_launcher_without_header_uses_uv_and_needs_it(tmp_path, monkeypatch):
    script = tmp_path / "tickets.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    monkeypatch.setattr(tickettree.shutil, "which", lambda name: None)
    with pytest.raises(TicketsUnavailable, match="`uv` is not on PATH"):
        TicketTree(tmp_path, script).launcher()


def test_missing_script_is_unavailable_and_spawns_nothing(tmp_path, monkeypatch):
    seen = canned(monkeypatch, ChildRun(0, "{}", ""))
    tree = TicketTree(tmp_path, tmp_path / "absent" / "tickets.py")
    for call in (tree.next, tree.capability):
        with pytest.raises(TicketsUnavailable):
            call()
    assert seen == []


# ---------------------------------------------------------------- pin


def test_pin_mismatch_refuses_before_spawn(project_v7: V7Layout, monkeypatch):
    """Ablation: dropping the `_check_pin()` call in `_call` lets the canned child
    answer and turns this red."""
    seen = canned(monkeypatch, ChildRun(0, "{}", ""))
    (project_v7.project / V7_USER_CONFIG_REL).write_text(
        '[core]\nactive_initiative = "initiative-other"\n', encoding="utf-8"
    )
    tree = TicketTree(project_v7.project, pinned_initiative=project_v7.initiative)
    for call in (tree.next, tree.status, lambda: tree.find("1.1")):
        with pytest.raises(TicketsPinMismatch) as caught:
            call()
        assert (caught.value.pinned, caught.value.live) == (
            project_v7.initiative,
            "initiative-other",
        )
    assert seen == []


def test_pin_absent_initiative_refuses(project_v7: V7Layout, monkeypatch):
    seen = canned(monkeypatch, ChildRun(0, "{}", ""))
    (project_v7.project / V7_USER_CONFIG_REL).write_text("[core]\n", encoding="utf-8")
    with pytest.raises(TicketsPinMismatch) as caught:
        TicketTree(project_v7.project, pinned_initiative=project_v7.initiative).next()
    assert caught.value.live is None and "none" in str(caught.value)
    assert seen == []


def test_unpinned_tree_does_not_read_the_initiative(tmp_path, monkeypatch):
    canned(monkeypatch, ChildRun(1, "", '{"error": "no active initiative"}'))
    with pytest.raises(TicketsFault, match="no active initiative"):
        tree_at(tmp_path).next()  # no _bmad config at all: the script owns the answer


# ---------------------------------------------------------------- error mapping


def test_timeout_is_typed(tmp_path: Path):
    script = fake_script(tmp_path, "import time\ntime.sleep(60)\n")
    with pytest.raises(TicketsTimeout):
        TicketTree(tmp_path, script, timeout=0.5).next()


def test_exit2_json_is_store_refusal(tmp_path: Path):
    body = (
        "import json, sys\n"
        'print(json.dumps({"error": "store is jira: sync first"}), file=sys.stderr)\n'
        "raise SystemExit(2)\n"
    )
    with pytest.raises(TicketsStoreRefusal, match="store is jira"):
        TicketTree(tmp_path, fake_script(tmp_path, body)).next()


def test_exit2_text_is_usage_error(tmp_path: Path):
    """argparse's plain-text refusal (version skew), told apart from a store
    refusal only by stderr not being JSON. Ablation: mapping every exit 2 to
    `TicketsStoreRefusal` turns this red."""
    body = (
        "import sys\n"
        "print('usage: tickets.py [-h]\\ntickets.py: error: invalid choice: \\'next\\'', file=sys.stderr)\n"
        "raise SystemExit(2)\n"
    )
    with pytest.raises(TicketsUsageError, match="invalid choice"):
        TicketTree(tmp_path, fake_script(tmp_path, body)).next()


def test_capability_flags_missing_verb(tmp_path: Path):
    body = (
        "import sys\n"
        "if sys.argv[-2:] == ['mark', '--help']:\n"
        "    print('usage: tickets.py mark [-h] ref {draft,in-progress,done,blocked}')\n"
        "else:\n"
        "    print('usage: tickets.py [-h] {next,status,mark} ...')\n"
    )
    cap = TicketTree(tmp_path, fake_script(tmp_path, body)).capability()
    assert capability_problems(cap) == [
        "tickets.py has no `find` command",
        "tickets.py mark does not accept status `built`",
    ]


def test_capability_cache_reprobes_a_changed_script(tmp_path, monkeypatch):
    """Ablation: skipping the cache lookup makes the second call spawn again and
    turns this red; keying on the path alone misses the reinstall."""
    seen = canned(monkeypatch, ChildRun(0, "usage: tickets.py {next,status,find,mark}", ""))
    script = fake_script(tmp_path, "pass\n")
    TicketTree(tmp_path, script).capability()
    TicketTree(tmp_path, script).capability()
    assert len(seen) == 2
    script.write_text(script.read_text(encoding="utf-8") + "# reinstalled\n", encoding="utf-8")
    TicketTree(tmp_path, script).capability()
    assert len(seen) == 4


@pytest.mark.parametrize(
    ("run", "error"),
    [
        (ChildRun(1, "", '{"error": "two entries with id 1", "where": "epic-cart"}'), TicketsFault),
        (ChildRun(1, "", '{"error": "no ticket matches \'x\'"}'), TicketsNoMatch),
        (
            ChildRun(1, "", "Traceback (most recent call last):\n  ...\nKeyError: 'id'"),
            TicketsCrashed,
        ),
        (ChildRun(0, "not json", ""), TicketsCrashed),
        (ChildRun(0, "[1, 2]", ""), TicketsCrashed),
        (ChildRun(3, "", '{"error": "odd"}'), TicketsCrashed),
        (ChildRun(None, "", ""), TicketsCrashed),
        (ChildRun(None, "", "", interrupted=True), TicketsInterrupted),
        (ChildRun(None, "", "", timed_out=True), TicketsTimeout),
        (ChildRun(0, '{"store": "repo"}', ""), TicketsSchemaError),
    ],
    ids=[
        "rc1-json-fault",
        "rc1-no-match",
        "rc1-traceback",
        "rc0-not-json",
        "rc0-not-object",
        "rc3",
        "unreaped",
        "interrupted",
        "timed-out",
        "rc0-missing-field",
    ],
)
def test_child_outcome_maps_to_a_typed_error(tmp_path, monkeypatch, run, error):
    canned(monkeypatch, run)
    with pytest.raises(error) as caught:
        tree_at(tmp_path).next()
    assert isinstance(caught.value, TicketsError)
    assert caught.value.kind == error.kind


def test_rc1_json_stderr_is_fault_with_data(tmp_path, monkeypatch):
    canned(monkeypatch, ChildRun(1, "", '{"error": "two entries with id 1", "where": "epic-cart"}'))
    with pytest.raises(TicketsFault) as caught:
        tree_at(tmp_path).next()
    assert type(caught.value) is TicketsFault
    assert caught.value.message == "two entries with id 1"
    assert caught.value.data == {"where": "epic-cart"}


def test_schema_error_is_both_a_tickets_error_and_a_model_error(tmp_path, monkeypatch):
    from bmad_loop import ticketmodel

    canned(monkeypatch, ChildRun(0, '{"store": "repo"}', ""))
    with pytest.raises(ticketmodel.TicketsSchemaError) as caught:
        tree_at(tmp_path).next()
    assert isinstance(caught.value, TicketsError) and caught.value.field == "folder"


def test_spawn_oserror_is_unavailable(tmp_path, monkeypatch):
    def boom(argv, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(childrun, "run_argv", boom)
    with pytest.raises(TicketsUnavailable, match="denied"):
        tree_at(tmp_path).next()


def test_pending_hard_stop_spawns_nothing(tmp_path: Path):
    token = childrun.install_stop_probe(lambda: True)
    try:
        with pytest.raises(TicketsInterrupted):
            tree_at(tmp_path, fake_script(tmp_path, "raise SystemExit('spawned')\n")).next()
    finally:
        childrun.reset_stop_probe(token)


def test_find_passes_the_ref_after_a_separator(tmp_path, monkeypatch):
    seen = canned(monkeypatch, ChildRun(1, "", '{"error": "no ticket matches \'-x\'"}'))
    with pytest.raises(TicketsNoMatch):
        tree_at(tmp_path).find("-x")
    assert seen[0][-3:] == ["find", "--", "-x"]


# ---------------------------------------------------------------- fake


def test_fake_ticket_tree_is_a_ticket_tree_like(tmp_path: Path):
    """The injection seam: what engine tests pass where a `TicketTree` goes."""
    fake = FakeTicketTree.from_rows(tmp_path, ready_to_start=(ticket_row("1.1"),))
    assert isinstance(fake, TicketTreeLike)
    assert [r.ref for r in fake.next().ready_to_start] == ["1.1"]
    assert fake.find("1.1").row.ref == "1.1"
    with pytest.raises(TicketsNoMatch):
        fake.find("2.1")
    assert fake.calls == [("next",), ("find", "1.1"), ("find", "2.1")]


def test_fake_ticket_tree_faults_pin_and_marks(tmp_path: Path):
    fake = FakeTicketTree.from_rows(
        tmp_path,
        ready_to_start=(ticket_row("1.1", status="built"),),
        pinned_initiative="initiative-demo",
    )
    fake.faults["status"] = TicketsFault("tree unreadable")
    with pytest.raises(TicketsFault):
        fake.status()
    report = fake.mark("1.1", "done", reason="accept")
    assert report.after == "done" and fake.find("1.1").row.state == "done"
    assert [(m.ref, m.target, m.reason) for m in fake.marks] == [("1.1", "done", "accept")]
    fake.live_initiative = "initiative-other"
    with pytest.raises(TicketsPinMismatch):
        fake.next()
    assert fake.capability().verbs >= {"next", "find", "status", "mark"}


# ---------------------------------------------------------------- writes
#
# The chokepoint against the vendored script (8 spawns, stated above), then the
# lock, journal and refusal rows on an in-process `find`/`mark` at no spawn cost.

PARK = "awaiting operator: sign the release"
CART_MODEL_PLAN = "story-cart-model-plan"  # DEFAULT_TREE's 1.1, epic-cart, entry 1


def _cart_plan(layout: V7Layout, status: str, **fm: object) -> Path:
    return write_plan(layout, "epic-cart", CART_MODEL_PLAN, 1, status, **fm).resolve()


def _plan_path(layout: V7Layout) -> Path:
    return (layout.initiative_folder / "epic-cart" / f"{CART_MODEL_PLAN}.md").resolve()


def _fields(plan: Path) -> tuple[str, str]:
    fm = frontmatter.read_frontmatter(plan)
    return frontmatter.status_of(fm), str(fm.get("blocked_reason") or "")


def _marks(journal: Journal) -> list[dict[str, object]]:
    return [e for e in journal.entries() if e["kind"] == "ticket-mark"]


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "run")


def _mark_argv(argvs: list[list[str]]) -> list[list[str]]:
    return [a for a in argvs if "mark" in a]


def test_mark_done_writes_and_reads_back(project_v7: V7Layout, spawns, journal):
    """built -> done under `accept`: the script writes it, the native read-back
    agrees, and one `ticket-mark` records the write. 2 real spawns."""
    plan = _cart_plan(project_v7, "built")
    tree = TicketTree(project_v7.project)

    result = tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)

    assert (result.before, result.after, result.outcome, result.created) == (
        "built",
        "done",
        "written",
        False,
    )
    assert _fields(plan) == ("done", "")
    marks = _mark_argv(spawns)
    assert len(marks) == 1 and marks[0][-4:] == ["mark", "--", "1.1", "done"]
    assert len(spawns) == 2  # one find, one mark
    [entry] = _marks(journal)
    assert {k: entry[k] for k in ("ref", "reason", "before", "after", "outcome", "created")} == {
        "ref": "1.1",
        "reason": "accept",
        "before": "built",
        "after": "done",
        "outcome": "written",
        "created": False,
    }
    assert Path(str(entry["plan"])) == plan


def test_mark_done_refused_below_built_writes_nothing(project_v7: V7Layout, spawns, journal):
    """accept on an in-review plan is refused and nothing is written: no `mark`
    child, and the plan still reads in-review. 1 real spawn (the find).

    Ablation: make `check_plan_transition` return True instead of raising (or
    drop the call from `TicketTree.mark`) and this reddens — the vendored script
    happily writes `done` over `in-review`."""
    plan = _cart_plan(project_v7, "in-review")

    with pytest.raises(TicketsTransitionRefused) as refused:
        TicketTree(project_v7.project).mark(
            "1.1", "done", reason="accept", expected_plan=plan, journal=journal
        )

    assert isinstance(refused.value, PlanTransitionRefused)
    assert _fields(plan) == ("in-review", "")
    assert _mark_argv(spawns) == []
    assert [e["outcome"] for e in _marks(journal)] == ["refused"]


def test_mark_done_on_missing_plan_creates_nothing(project_v7: V7Layout, spawns, journal):
    """A ticket with no plan is refused — upstream's `mark` would create a
    frontmatter-only plan, and bmad-loop never creates one. 1 real spawn.

    Ablation: write unconditionally in `TicketTree.mark` (`write = True` in place
    of the `check_plan_transition` call) and this reddens on the plan's existence.
    (The `has_plan` gate alone is not the ablation: "" is no reason's source.)"""
    plan = _plan_path(project_v7)
    assert not plan.exists()

    with pytest.raises(TicketsTransitionRefused, match="no plan"):
        TicketTree(project_v7.project).mark(
            "1.1", "done", reason="accept", expected_plan=plan, journal=journal
        )

    assert not plan.exists()
    assert _mark_argv(spawns) == []


def test_park_sets_blocked_reason_and_rearm_clears_it(project_v7: V7Layout, spawns, journal):
    """park writes `blocked` with the operator reason (and `blocked_at`); a
    re-arm writes the resume status and the script clears both blocked fields.
    4 real spawns."""
    plan = _cart_plan(project_v7, "in-review")
    tree = TicketTree(project_v7.project)

    parked = tree.mark(
        "1.1", "blocked", reason="park", expected_plan=plan, blocked=PARK, journal=journal
    )
    assert (parked.after, parked.outcome) == ("blocked", "written")
    assert _fields(plan) == ("blocked", PARK)
    assert frontmatter.read_frontmatter(plan).get("blocked_at")
    assert _mark_argv(spawns)[0][-6:] == ["mark", "--blocked", PARK, "--", "1.1", "blocked"]

    rearmed = tree.mark("1.1", "in-review", reason="rearm", expected_plan=plan, journal=journal)
    assert (rearmed.before, rearmed.after) == ("blocked", "in-review")
    fm = frontmatter.read_frontmatter(plan)
    assert _fields(plan) == ("in-review", "")
    assert "blocked_at" not in fm and "blocked_reason" not in fm
    assert len(spawns) == 4


# ---- in-process rows (no spawn) -------------------------------------------------


def _plan_text(status: str, blocked: str = "", body: str = "# plan\n") -> str:
    extra = f"blocked_at: 2026-10-14\nblocked_reason: {json.dumps(blocked)}\n" if blocked else ""
    return f"---\nticket: 1\nstatus: {status}\n{extra}---\n\n{body}"


class InProcess:
    """`find` and `mark` answered in-process against a plan file: `find` names
    ``found_plan`` (default: the plan itself), `mark` rewrites the plan the way
    the script would (or as ``write_status`` says, to fake a bad read-back).
    ``events`` records each verb, and ``on_mark`` runs inside the mark call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tree: TicketTree, plan: Path) -> None:
        self.plan = plan
        self.found_plan = plan
        self.events: list[str] = []
        self.write_status: str | None = None
        self.mark_fault: Exception | None = None
        self.on_mark = lambda: None

        def fake_find_(ref: str):
            self.events.append("find")
            return dataclasses.replace(
                fake_find(ticket_row(ref), plan.parent), plan=str(self.found_plan)
            )

        def fake_call(tail, *, verb, json_out=True):
            assert verb == "mark"
            self.events.append("mark")
            self.on_mark()
            if self.mark_fault is not None:
                raise self.mark_fault
            args = list(tail)
            blocked = args[args.index("--blocked") + 1] if "--blocked" in args else ""
            status = self.write_status or args[-1]
            plan.write_text(_plan_text(status, blocked), encoding="utf-8")
            return {
                "plan": str(plan),
                "created": False,
                "status": status,
                "assignee": "",
                "blocked_at": "2026-10-14" if blocked else "",
                "blocked_reason": blocked,
            }

        def no_spawn(*a, **kw):  # pragma: no cover - reaching it is the failure
            raise AssertionError("an in-process row must not spawn")

        monkeypatch.setattr(tree, "find", fake_find_)
        monkeypatch.setattr(tree, "_call", fake_call)
        monkeypatch.setattr(childrun, "run_argv", no_spawn)


def _tree_with_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, blocked: str = ""
) -> tuple[TicketTree, Path, InProcess]:
    plan = tmp_path / "out" / "epic-cart" / f"{CART_MODEL_PLAN}.md"
    plan.parent.mkdir(parents=True)
    if status:
        plan.write_text(_plan_text(status, blocked), encoding="utf-8")
    tree = TicketTree(tmp_path)
    return tree, plan, InProcess(monkeypatch, tree, plan)


def _lock_is_held(plan: Path) -> bool:
    try:
        with platform_util.file_lock(runs.lock_path_for(plan), blocking=False):
            return False
    except platform_util.LockUnavailableError:
        return True


def test_mark_holds_the_plan_lock(tmp_path, monkeypatch, journal):
    """The find, the decision and the `mark` child all run under the plan's
    sidecar lock, and the lock is released afterwards. A held lock is a typed
    `TicketsLockBusy` that reads and writes nothing.

    Ablation: move `self._locate(...)` above `with self._plan_lock(plan):` (or
    drop the lock) and this reddens — the find, or the mark, sees the lock free."""
    tree, plan, fake = _tree_with_plan(tmp_path, monkeypatch, "built")
    held: list[tuple[str, bool]] = []
    real_find = tree.find

    def find_probe(ref: str):
        held.append(("find", _lock_is_held(plan)))
        return real_find(ref)

    monkeypatch.setattr(tree, "find", find_probe)
    fake.on_mark = lambda: held.append(("mark", _lock_is_held(plan)))

    tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)

    assert held == [("find", True), ("mark", True)]
    assert not _lock_is_held(plan)

    @contextlib.contextmanager
    def busy(path, *, blocking=True):
        raise platform_util.LockUnavailableError(f"{path} is held")
        yield  # pragma: no cover

    monkeypatch.setattr(platform_util, "file_lock", busy)
    plan.write_text(_plan_text("built"), encoding="utf-8")
    held.clear()
    with pytest.raises(TicketsLockBusy):
        tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)
    assert held == [] and _fields(plan) == ("built", "")
    assert [e["outcome"] for e in _marks(journal)] == ["written", "failed"]


def test_noop_mark_does_not_spawn_mark(tmp_path, monkeypatch, journal):
    """A replayed accept of a done plan, and a re-park with the same reason,
    decide no-op and spawn no `mark` child.

    Ablation: drop the `if not write` arm in `TicketTree.mark` (always spawn)
    and this reddens on the recorded events."""
    tree, plan, fake = _tree_with_plan(tmp_path, monkeypatch, "done")
    result = tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)
    assert (result.outcome, result.before, result.after) == ("noop", "done", "done")

    plan.write_text(_plan_text("blocked", PARK), encoding="utf-8")
    again = tree.mark(
        "1.1", "blocked", reason="park", expected_plan=plan, blocked=PARK, journal=journal
    )
    assert again.outcome == "noop"
    assert fake.events == ["find", "find"]
    assert [e["outcome"] for e in _marks(journal)] == ["noop", "noop"]


def test_mark_journals_every_outcome(tmp_path, monkeypatch, journal):
    """Exactly one `ticket-mark` per call, whatever happened: written, noop,
    refused (table or cross-check) and failed (the child faulted, or the
    read-back disagreed). A failed write raises."""
    tree, plan, fake = _tree_with_plan(tmp_path, monkeypatch, "built")
    tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)
    tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)
    with pytest.raises(TicketsTransitionRefused):
        tree.mark("1.1", "in-progress", reason="repair", expected_plan=plan, journal=journal)

    plan.write_text(_plan_text("built"), encoding="utf-8")
    fake.mark_fault = TicketsFault("plan is not writable")
    with pytest.raises(TicketsFault):
        tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)

    fake.mark_fault = None
    fake.write_status = "in-review"
    with pytest.raises(TicketsReadBackMismatch):
        tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)

    rows = [(e["reason"], e["before"], e["after"], e["outcome"]) for e in _marks(journal)]
    assert rows == [
        ("accept", "built", "done", "written"),
        ("accept", "done", "done", "noop"),
        ("repair", "done", "done", "refused"),
        ("accept", "built", "built", "failed"),
        ("accept", "built", "in-review", "failed"),
    ]


def test_expected_plan_mismatch_refuses(tmp_path, monkeypatch, journal):
    """`find` naming another plan than the caller expected is refused before
    anything is decided or written: the lock and the write would be on
    different files.

    Ablation: drop the `_locate` cross-check and this reddens — the in-process
    `mark` rewrites the expected plan."""
    tree, plan, fake = _tree_with_plan(tmp_path, monkeypatch, "built")
    fake.found_plan = plan.with_name("story-other-plan.md")

    with pytest.raises(TicketsPlanMismatch):
        tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)

    assert fake.events == ["find"]
    assert _fields(plan) == ("built", "")
    assert [e["outcome"] for e in _marks(journal)] == ["refused"]


def test_restore_preimage_is_byte_exact_under_lock(tmp_path, monkeypatch, journal):
    """A pre-image comes back byte for byte — BOM, CRLF and all — written while
    the plan lock is held; `None` (no plan before) removes the plan. Each
    restore journals `ticket-plan-restored`.

    Ablation: drop `with self._plan_lock(plan):` from `restore_preimage` and
    this reddens on the lock probe."""
    tree, plan, _ = _tree_with_plan(tmp_path, monkeypatch, "done")
    preimage = b"\xef\xbb\xbf---\r\nticket: 1\r\nstatus: built\r\n---\r\n\r\n# plan\r\n"
    held: list[bool] = []
    real_write = platform_util.atomic_write_bytes

    def write_probe(path, data, **kw):
        held.append(_lock_is_held(plan))
        return real_write(path, data, **kw)

    monkeypatch.setattr(platform_util, "atomic_write_bytes", write_probe)

    tree.restore_preimage(plan, preimage, journal)

    assert plan.read_bytes() == preimage
    assert held == [True]
    tree.restore_preimage(plan, None, journal)
    assert not plan.exists()
    restored = [e for e in journal.entries() if e["kind"] == "ticket-plan-restored"]
    assert [(e["before"], e["after"], e["outcome"]) for e in restored] == [
        ("done", "built", "restored"),
        ("built", "", "restored"),
    ]


def test_discard_unbuilt_plan_removes_only_a_frontmatter_plan(tmp_path, monkeypatch, journal):
    """A frontmatter-only plan in a rollback-able status is deleted; one with a
    body, or in a final status, is refused and kept; no plan is a no-op.

    Ablation: drop the `is_frontmatter_only` check and the body row reddens."""
    tree, plan, _ = _tree_with_plan(tmp_path, monkeypatch, "in-progress")
    with pytest.raises(TicketsTransitionRefused, match="more than frontmatter"):
        tree.discard_unbuilt_plan("1.1", plan, journal)
    assert plan.exists()

    plan.write_text("---\nticket: 1\nstatus: done\n---\n", encoding="utf-8")
    with pytest.raises(TicketsTransitionRefused, match="final"):
        tree.discard_unbuilt_plan("1.1", plan, journal)
    assert plan.exists()

    plan.write_text("---\nticket: 1\nstatus: in-progress\n---\n\n", encoding="utf-8")
    result = tree.discard_unbuilt_plan("1.1", plan, journal)
    assert (result.outcome, result.before) == ("discarded", "in-progress")
    assert not plan.exists()
    assert tree.discard_unbuilt_plan("1.1", plan, journal).outcome == "noop"
    assert [e["outcome"] for e in _marks(journal)] == ["refused", "refused", "discarded", "noop"]


def test_discard_unbuilt_plan_reads_a_bom_plan_as_frontmatter_only(tmp_path, monkeypatch, journal):
    """A BOM ahead of `---` is not a body: the plan is discarded, as `_plan_fields`
    already reads its status through the BOM.

    Ablation: read the plan with plain `utf-8` and the discard is refused."""
    tree, plan, _ = _tree_with_plan(tmp_path, monkeypatch, "in-progress")
    plan.write_bytes(b"\xef\xbb\xbf---\r\nticket: 1\r\nstatus: in-progress\r\n---\r\n")
    result = tree.discard_unbuilt_plan("1.1", plan, journal)
    assert (result.outcome, result.before) == ("discarded", "in-progress")
    assert not plan.exists()


def _unreadable(monkeypatch: pytest.MonkeyPatch, plan: Path) -> None:
    """From now on, reading ``plan``'s bytes raises ``PermissionError``."""
    real = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self == plan:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


def test_mark_retypes_a_plan_read_oserror(tmp_path, monkeypatch, journal):
    """A plan that turns unreadable — before the decision, or between the `mark`
    child and the read-back — raises the typed `TicketsPlanIOError`, and the
    call still journals its `failed` `ticket-mark` (status `?`).

    Ablation: drop the `except OSError` arm in `_plan_fields` and this reddens —
    the raw `PermissionError` escapes past the failure record."""
    tree, plan, fake = _tree_with_plan(tmp_path, monkeypatch, "built")
    fake.on_mark = lambda: _unreadable(monkeypatch, plan)
    with pytest.raises(TicketsPlanIOError, match="cannot read"):
        tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)
    assert fake.events == ["find", "mark"]

    with pytest.raises(TicketsPlanIOError):
        tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)
    assert fake.events == ["find", "mark", "find"]

    rows = [(e["before"], e["after"], e["outcome"]) for e in _marks(journal)]
    assert rows == [("built", "?", "failed"), ("", "?", "failed")]


def test_discard_and_restore_retype_a_plan_oserror(tmp_path, monkeypatch, journal):
    """A delete or write of the plan that raises `OSError` surfaces as
    `TicketsPlanIOError`, after the `ticket-mark` / `ticket-plan-restored`
    failure record.

    Ablation: drop either `except OSError` arm (around `plan.unlink()` in
    `discard_unbuilt_plan`, or the restore body) and this reddens."""
    tree, plan, _ = _tree_with_plan(tmp_path, monkeypatch, "in-progress")
    plan.write_text("---\nticket: 1\nstatus: in-progress\n---\n", encoding="utf-8")
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == plan:
            raise PermissionError(13, "Permission denied", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(TicketsPlanIOError, match="cannot delete"):
        tree.discard_unbuilt_plan("1.1", plan, journal)
    assert plan.exists()
    assert [e["outcome"] for e in _marks(journal)] == ["failed"]

    def no_write(path, data, **kw):
        raise OSError(28, "No space left on device", str(path))

    monkeypatch.setattr(platform_util, "atomic_write_bytes", no_write)
    with pytest.raises(TicketsPlanIOError, match="cannot restore"):
        tree.restore_preimage(plan, b"---\nstatus: built\n---\n", journal)
    restored = [e for e in journal.entries() if e["kind"] == "ticket-plan-restored"]
    assert [e["outcome"] for e in restored] == ["failed"]


def test_fake_ticket_tree_decides_marks_with_the_real_table(tmp_path: Path):
    """The fake runs `check_plan_transition`, so an engine test sees the real
    chokepoint's refusals and no-ops; discards and restores are recorded."""
    fake = FakeTicketTree.from_rows(tmp_path, in_progress=(ticket_row("1.1", status="in-review"),))
    with pytest.raises(TicketsTransitionRefused):
        fake.mark("1.1", "done", reason="accept")
    assert fake.mark("1.1", "blocked", reason="park", blocked=PARK).outcome == "written"
    assert fake.find("1.1").row.blocked_reason == PARK
    assert fake.mark("1.1", "blocked", reason="park", blocked=PARK).outcome == "noop"
    with pytest.raises(TicketsPlanMismatch):
        fake.mark("1.1", "in-review", reason="rearm", expected_plan=tmp_path / "x.md")
    assert fake.mark("1.1", "in-review", reason="rearm").after == "in-review"
    assert fake.discard_unbuilt_plan("1.1").outcome == "discarded"
    fake.restore_preimage(tmp_path / "p.md", b"x")
    assert fake.discards == ["1.1"] and fake.restores == [(tmp_path / "p.md", b"x")]
    assert [m.outcome for m in fake.marks] == ["written", "noop", "written"]
