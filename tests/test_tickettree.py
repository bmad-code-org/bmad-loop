"""`bmad_loop.tickettree`: contract rows against the vendored `tickets.py` on
`project_v7`, plus error mapping through fake scripts and canned child results.

Real-spawn budget: **24 spawns of the vendored `tickets.py`** — next 1, status 1,
find 2 (a hit and a miss), capability 2 (`--help`, `mark --help`; the cached
second call spawns nothing), the mark chokepoint 8 (write + read back: find
and mark, 2; refused below built: find, 1; missing plan: find, 1; park then
re-arm: 2 × find and mark, 4), and a parent initiative's child folder 4
(`next <parent>`, 1; `next <child>`, then find and mark, 3), and closing a
drained child 6 (LF, CRLF and BOM files: the close's `next <parent>` and the
check's, 2 each). The fake-script rows add 5 short Python children
(timeout 1, exit-2 JSON 1, exit-2 text 1, capability 2); every other row maps a
canned `childrun.ChildRun`, answers `find`/`mark` in-process, or refuses before
spawning, at no spawn cost. The Windows leg sits at its time budget, so keep new
rows on the canned path."""

from __future__ import annotations

import codecs
import contextlib
import dataclasses
import json
import os
import re
import sys
from pathlib import Path

import pytest
from conftest import (
    DEFAULT_TREE,
    V7_USER_CONFIG_REL,
    EntrySpec,
    EpicSpec,
    FakeTicketTree,
    V7Layout,
    fake_find,
    ticket_row,
    v7_layout,
    write_plan,
    write_script_launcher,
    write_ticket_tree,
)

from bmad_loop import childrun, frontmatter, platform_util, runs, ticketlayout, tickettree
from bmad_loop.childrun import ChildRun
from bmad_loop.journal import Journal
from bmad_loop.statemachine import InitiativeCloseRefused, PlanTransitionRefused
from bmad_loop.ticketmodel import NEXT_GROUPS, capability_problems
from bmad_loop.tickettree import (
    TicketsCrashed,
    TicketsError,
    TicketsFault,
    TicketsInitiativeRefused,
    TicketsInterrupted,
    TicketsLockBusy,
    TicketsNoMatch,
    TicketsParentInitiative,
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
    TicketsV6KeyUnreadable,
    TicketTree,
    TicketTreeLike,
    V6KeyClaim,
    read_v6_key_claims,
    read_v6_key_entries,
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
    epic_dir = project_v7.initiative_folder / "tickets" / "epic-checkout" / "tickets"
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


def test_script_stat_oserror_is_unavailable_and_spawns_nothing(tmp_path, monkeypatch):
    """A script whose existence probe fails (a stat error other than "not
    there") raises the typed `TicketsUnavailable` from every verb, so callers
    catching only `TicketsError` (`install.tickets_preflight`) still see it.

    Ablation: put `self.script.is_file()` back in `_call` and `next` reddens
    with a raw `PermissionError` before 3.14."""
    script = tmp_path / "tickets.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    seen = canned(monkeypatch, ChildRun(0, "{}", ""))
    real = Path.stat

    def stat(self: Path, *, follow_symlinks: bool = True):
        if self == script:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    tree = TicketTree(tmp_path, script)
    for call in (tree.next, tree.capability):
        with pytest.raises(TicketsUnavailable, match="Permission denied"):
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
        "    print('usage: tickets.py mark [-h] ref {draft,ready-for-dev,in-progress,in-review,done,blocked}')\n"
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


@pytest.mark.parametrize("verb", ["next", "status"])
def test_parent_view_is_a_parent_initiative_fault(tmp_path, monkeypatch, verb):
    """A parent initiative's `parent_view` (children, no tickets) re-raises as
    `TicketsParentInitiative`: still a schema fault to every `except`, with its own
    `kind` and the children kept. No spawn (canned child). Ablation (performed):
    drop its `except` arm in `_parsed` and the plain schema error FAILS this."""
    from bmad_loop import ticketmodel

    doc = {
        "store": "repo",
        "folder": "initiative-platform",
        "initiatives": [{"slug": "initiative-auth"}, {"slug": "initiative-billing"}],
        "order_conflict": [],
    }
    canned(monkeypatch, ChildRun(0, json.dumps(doc), ""))
    with pytest.raises(tickettree.TicketsParentInitiative) as caught:
        getattr(tree_at(tmp_path), verb)()
    e = caught.value
    assert isinstance(e, tickettree.TicketsSchemaError)
    assert isinstance(e, ticketmodel.TicketsParentInitiative)
    assert e.kind == "parent-initiative" and e.command == verb
    assert e.children == ("initiative-auth", "initiative-billing")
    assert "is a parent initiative (children: initiative-auth, initiative-billing)" in str(e)


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


# ---------------------------------------------------------------- child folder


@pytest.mark.parametrize("folder", [None, "child"], ids=["active", "child-folder"])
def test_dir_positional_rides_only_with_a_folder(tmp_path, monkeypatch, folder):
    """Every tree read names the child folder as `<dir>` when the tree has one,
    and the argv is the active initiative's, unchanged, when it has none. No
    spawn (canned child). Ablation: drop `*self._dir()` from a verb's tail and
    its child-folder row reddens."""
    seen = canned(monkeypatch, ChildRun(1, "", '{"error": "stop here"}'))
    child = tmp_path / "out" / "initiative-auth"
    tree = tree_at(tmp_path, folder=child if folder else None)
    for call in (tree.next, tree.status, lambda: tree.find("1.1")):
        with pytest.raises(TicketsFault):
            call()
    tails = [argv[argv.index("--project-root") + 2 :] for argv in seen]
    if folder is None:
        assert tree.folder is None
        assert tails == [["next"], ["status"], ["find", "--", "1.1"]]
    else:
        assert tree.folder == child
        assert tails == [
            ["next", str(child)],
            ["status", str(child)],
            ["find", str(child), "--", "1.1"],
        ]


def test_explicit_status_folder_overrides_the_tree_folder(tmp_path, monkeypatch):
    seen = canned(monkeypatch, ChildRun(1, "", '{"error": "stop here"}'))
    other = tmp_path / "elsewhere"
    with pytest.raises(TicketsFault):
        tree_at(tmp_path, folder=tmp_path / "child").status(other)
    assert seen[0][-2:] == ["status", str(other)]


PARENT = "initiative-platform"
AUTH, BILLING, REPORTS = "initiative-auth", "initiative-billing", "initiative-reports"


def _parent_initiative(project: V7Layout) -> tuple[Path, V7Layout, V7Layout]:
    """Turn `project_v7` into a parent initiative made active: `PARENT` lists
    `AUTH`, `BILLING` (after `AUTH`) and `REPORTS` (declared, no folder), and
    both existing children hold `DEFAULT_TREE` — the same refs in each, so only
    the `<dir>` tells them apart. Returns the parent folder and the two child
    layouts."""
    root = project.project
    parent = project.output_folder / PARENT
    (parent / ticketlayout.TICKETS).mkdir(parents=True)
    (parent / f"{PARENT}.md").write_text(
        "---\ntype: initiative\ntitle: Platform\nparent: none\n---\n\n# Platform\n",
        encoding="utf-8",
    )
    (parent / ticketlayout.TICKETS / ticketlayout.BREAKDOWN).write_text(
        f'[[initiative]]\nid = 1\nslug = "{AUTH}"\ntitle = "Auth"\n\n'
        f'[[initiative]]\nid = 2\nslug = "{BILLING}"\ntitle = "Billing"\n'
        'after = [{ initiative = 1, needs = "auth live" }]\n\n'
        f'[[initiative]]\nid = 3\nslug = "{REPORTS}"\ntitle = "Reports"\n',
        encoding="utf-8",
    )
    children = []
    for slug in (AUTH, BILLING):
        layout = v7_layout(root, initiative=slug)
        write_ticket_tree(layout, DEFAULT_TREE)
        children.append(layout)
    (root / V7_USER_CONFIG_REL).write_text(
        f'[core]\nactive_initiative = "{PARENT}"\n', encoding="utf-8"
    )
    return parent, children[0], children[1]


def test_parent_next_against_vendored_tickets_py(project_v7: V7Layout, spawns):
    """`next <parent>` on the live script parses as a `ParentView`: build order,
    the waiting child not ready, the folder-less child ready. 1 real spawn."""
    parent, _, _ = _parent_initiative(project_v7)
    view = TicketTree(project_v7.project, pinned_initiative=PARENT).parent_next(parent)
    assert spawns[0][-2:] == ["next", str(parent)] and len(spawns) == 1
    assert (view.store, view.folder) == ("repo", PARENT)
    assert [c.slug for c in view.initiatives] == [AUTH, BILLING, REPORTS]
    assert view.ready == (AUTH, REPORTS)
    auth, billing, reports = view.initiatives
    assert (auth.folder, auth.status, auth.counts["total"], auth.ready) == (AUTH, "", 4, True)
    assert billing.waiting_on == (AUTH,) and not billing.ready
    assert billing.after[0].initiative == AUTH and billing.after[0].needs == "auth live"
    assert (reports.folder, reports.ready, dict(reports.counts)) == (None, True, {})


def test_child_folder_reads_and_marks_a_child_ticket_under_the_parent_pin(
    project_v7: V7Layout, spawns, journal
):
    """With the parent active and pinned, a tree on `BILLING`'s folder reads its
    `next` and marks its 1.1 — the plan in `BILLING`, never `AUTH`'s twin. 3 real
    spawns (next, then the mark's find and mark).

    Ablation: drop the `<dir>` from `find`/`mark` and the script, reading the
    active parent, refuses with "a parent initiative holds initiatives" (a
    `TicketsFault`), so this reddens."""
    _, auth, billing = _parent_initiative(project_v7)
    tree = TicketTree(
        project_v7.project, pinned_initiative=PARENT, folder=billing.initiative_folder
    )
    view = tree.next()
    assert view.folder == BILLING and [r.ref for r in view.ready_to_start] == ["1.1"]
    assert view.backlog is None
    plan = _cart_plan(billing, "built")
    twin = _cart_plan(auth, "built")

    result = tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)

    assert (result.plan, result.outcome) == (plan, "written")
    assert _fields(plan) == ("done", "") and _fields(twin) == ("built", "")
    child = str(billing.initiative_folder)
    tails = [a[a.index("--project-root") + 2 :] for a in spawns]
    assert tails == [
        ["next", child],
        ["find", child, "--", "1.1"],
        ["mark", child, "--", "1.1", "done"],
    ]


def test_child_folder_pin_still_names_the_parent(project_v7: V7Layout, monkeypatch):
    """The pin stays the parent's: the live value naming the child is a
    mismatch, and nothing is spawned."""
    seen = canned(monkeypatch, ChildRun(0, "{}", ""))
    _parent_initiative(project_v7)
    (project_v7.project / V7_USER_CONFIG_REL).write_text(
        f'[core]\nactive_initiative = "{BILLING}"\n', encoding="utf-8"
    )
    folder = project_v7.output_folder / BILLING
    tree = TicketTree(project_v7.project, pinned_initiative=PARENT, folder=folder)
    for call in (tree.next, lambda: tree.find("1.1"), lambda: tree.parent_next(folder.parent)):
        with pytest.raises(TicketsPinMismatch) as caught:
            call()
        assert (caught.value.pinned, caught.value.live) == (PARENT, BILLING)
    assert seen == []


def test_parent_next_on_a_plain_initiative_is_a_schema_error(tmp_path, monkeypatch):
    """`next` on a folder that is no parent lists no `ready` children: version
    skew or a wrong folder, never an empty parent. No spawn (canned child)."""
    doc = {"store": "repo", "folder": "initiative-demo", "ready_to_start": []}
    canned(monkeypatch, ChildRun(0, json.dumps(doc), ""))
    with pytest.raises(TicketsSchemaError) as caught:
        tree_at(tmp_path).parent_next(tmp_path / "initiative-demo")
    assert not isinstance(caught.value, TicketsParentInitiative)
    assert caught.value.field == "ready"


# ------------------------------------------------- closing a drained child initiative
#
# The real-spawn rows (6 spawns, stated above) drain AUTH and close it against the
# vendored script; every other row answers `next <parent>` with a canned document.

AUTH_PLANS = (
    ("epic-cart", "story-cart-model-plan", 1),
    ("epic-cart", "story-cart-rules-plan", 2),
    ("epic-checkout", "story-checkout-form-plan", 1),
    ("epic-checkout", "bug-checkout-crash-plan", 2),
)


def _initiative_marks(journal: Journal) -> list[dict[str, object]]:
    return [e for e in journal.entries() if e["kind"] == "initiative-mark"]


@pytest.mark.parametrize("shape", ["lf", "crlf", "bom"])
def test_mark_initiative_done_closes_a_drained_child_for_the_script(
    project_v7: V7Layout, spawns, journal, shape
):
    """AUTH's tickets all done and its file carrying no `status:` line: the close
    inserts `status: done` before the closing `---` (the block's own line ending,
    a BOM kept), every other byte stays, and the script then reads AUTH done and
    BILLING ready. 2 real spawns per shape (the close's `next`, the check's).

    Ablation: make the key insert a no-op (`_edit_frontmatter_block` returns None
    for a missing key) and the read-back raises `TicketsReadBackMismatch`."""
    parent, auth, _ = _parent_initiative(project_v7)
    for epic, stem, ticket in AUTH_PLANS:
        write_plan(auth, epic, stem, ticket, "done")
    file = auth.initiative_folder / f"{AUTH}.md"
    nl, bom = ("\r\n" if shape == "crlf" else "\n"), ("\ufeff" if shape == "bom" else "")
    lf = file.read_text(encoding="utf-8")
    assert "status" not in lf
    original = (bom + lf.replace("\n", nl)).encode("utf-8")
    assert original.startswith(codecs.BOM_UTF8) == (shape == "bom")
    file.write_bytes(original)
    tree = TicketTree(project_v7.project, pinned_initiative=PARENT)

    result = tree.mark_initiative_done(parent, AUTH, journal=journal)

    assert (result.slug, result.file, result.outcome) == (AUTH, file, "written")
    assert (result.before, result.after, result.preimage) == ("", "done", original)
    closed = lf.replace("parent: none\n---", "parent: none\nstatus: done\n---")
    assert file.read_bytes() == (bom + closed.replace("\n", nl)).encode("utf-8")
    view = tree.parent_next(parent)
    assert len(spawns) == 2 and spawns[0][-2:] == ["next", str(parent)]
    auth_row, billing_row = view.child(AUTH), view.child(BILLING)
    assert auth_row is not None and billing_row is not None
    assert auth_row.status == "done" and dict(auth_row.counts) == {"total": 4, "done": 4}
    assert billing_row.ready and billing_row.waiting_on == () and BILLING in view.ready
    (mark,) = _initiative_marks(journal)
    assert {k: mark[k] for k in ("parent", "slug", "file", "before", "after", "outcome")} == {
        "parent": PARENT,
        "slug": AUTH,
        "file": str(file),
        "before": "",
        "after": "done",
        "outcome": "written",
    }


def _child_doc(slug: str, status: str | None, counts: dict[str, int]) -> dict[str, object]:
    """One `initiatives` row as upstream prints it; ``status`` None is a child
    with no folder (no `status`, no `counts`)."""
    row: dict[str, object] = {
        "id": slug[-1],
        "slug": slug,
        "title": slug,
        "after": [],
        "folder": None,
        "waiting_on": [],
    }
    if status is not None:
        row.update(folder=slug, status=status, counts=counts)
    return row


def _closing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    counts: dict[str, int] | None = None,
) -> tuple[TicketTree, Path, Path, list[list[str]]]:
    """A parent `PARENT` under `tmp_path / "out"` whose child AUTH's file holds
    ``status`` (no line when ``""``), and every spawn answered with the parent's
    `next`: AUTH at ``status`` with ``counts`` (drained by default) and the
    folder-less REPORTS. Returns the tree, the parent, AUTH's file and the argvs."""
    out = tmp_path / "out"
    parent = out / PARENT
    parent.mkdir(parents=True)
    file = out / AUTH / f"{AUTH}.md"
    file.parent.mkdir()
    line = f"status: {status}\n" if status else ""
    file.write_text(f"---\ntype: initiative\ntitle: Auth\n{line}---\n\n# Auth\n", encoding="utf-8")
    counts = {"total": 2, "done": 2} if counts is None else counts
    doc = {
        "store": "repo",
        "folder": PARENT,
        "initiatives": [_child_doc(AUTH, status, counts), _child_doc(REPORTS, None, {})],
        "ready": [REPORTS],
        "order_conflict": [],
    }
    seen = canned(monkeypatch, ChildRun(0, json.dumps(doc), ""))
    return tree_at(tmp_path), parent, file, seen


def test_mark_initiative_done_from_in_progress_writes(tmp_path, monkeypatch, journal):
    """in-progress -> done replaces the line in place, under the file's lock, and
    the pre-image is the file as it was. Dropped tickets count as closed."""
    tree, parent, file, seen = _closing(
        tmp_path, monkeypatch, "in-progress", {"total": 3, "done": 1, "dropped": 2}
    )
    original = file.read_bytes()
    real = tree.parent_next
    held: list[bool] = []

    def probe(folder: Path):
        held.append(_lock_is_held(file))
        return real(folder)

    monkeypatch.setattr(tree, "parent_next", probe)

    result = tree.mark_initiative_done(parent, AUTH, journal=journal)

    assert (result.before, result.after, result.outcome) == ("in-progress", "done", "written")
    assert result.preimage == original
    assert file.read_bytes() == original.replace(b"status: in-progress", b"status: done")
    assert held == [True] and not _lock_is_held(file) and len(seen) == 1
    assert [e["outcome"] for e in _initiative_marks(journal)] == ["written"]


def test_mark_initiative_done_on_a_done_child_is_a_noop(tmp_path, monkeypatch, journal):
    tree, parent, file, _ = _closing(tmp_path, monkeypatch, "done")
    before = (file.read_bytes(), file.stat().st_mtime_ns)

    result = tree.mark_initiative_done(parent, AUTH, journal=journal)

    assert (result.before, result.after, result.outcome) == ("done", "done", "noop")
    assert result.preimage is None
    assert (file.read_bytes(), file.stat().st_mtime_ns) == before
    assert [e["outcome"] for e in _initiative_marks(journal)] == ["noop"]


@pytest.mark.parametrize(
    ("status", "counts", "why"),
    [
        ("dropped", None, "a dropped initiative is final"),
        ("in-progress", {"total": 4, "done": 3}, "not drained: 3 of 4"),
        ("", {"total": 4, "done": 2, "in-progress": 2}, "not drained: 2 of 4"),
        ("", {"total": 0}, "not drained: 0 of 0"),
    ],
)
def test_mark_initiative_done_refusals_leave_the_file(
    tmp_path, monkeypatch, journal, status, counts, why
):
    """A dropped child is never overridden, and an open child with a ticket left
    open (or no tickets) is not drained: refused, the file untouched.

    Ablation: delete the drained check (the dropped arm) in
    `check_initiative_close` and the not-drained rows (the dropped row) redden."""
    tree, parent, file, _ = _closing(tmp_path, monkeypatch, status, counts)
    before = (file.read_bytes(), file.stat().st_mtime_ns)

    with pytest.raises(TicketsInitiativeRefused, match=why) as caught:
        tree.mark_initiative_done(parent, AUTH, journal=journal)

    assert isinstance(caught.value, InitiativeCloseRefused) and caught.value.kind == "refused"
    assert (file.read_bytes(), file.stat().st_mtime_ns) == before
    marks = _initiative_marks(journal)
    assert [(m["before"], m["after"], m["outcome"]) for m in marks] == [(status, status, "refused")]


@pytest.mark.parametrize(("slug", "shown"), [(BILLING, "no such child"), (REPORTS, "no folder")])
def test_mark_initiative_done_refuses_a_child_the_parent_does_not_list(
    tmp_path, monkeypatch, journal, slug, shown
):
    """A slug the parent does not list, or the folder-less REPORTS, is refused
    before any decision: the cross-check `mark` makes with `find`."""
    tree, parent, file, _ = _closing(tmp_path, monkeypatch, "")
    original = file.read_bytes()

    with pytest.raises(TicketsPlanMismatch, match=shown):
        tree.mark_initiative_done(parent, slug, journal=journal)

    assert file.read_bytes() == original
    marks = _initiative_marks(journal)
    assert [(m["slug"], m["outcome"]) for m in marks] == [(slug, "refused")]
    assert marks[0]["file"] == str(parent.parent / slug / f"{slug}.md")


def test_mark_initiative_done_lock_busy(tmp_path, monkeypatch, journal):
    """A held lock is a typed `TicketsLockBusy` that spawns, reads and writes nothing."""
    tree, parent, file, seen = _closing(tmp_path, monkeypatch, "")
    original = file.read_bytes()

    @contextlib.contextmanager
    def busy(path, *, blocking=True):
        raise platform_util.LockUnavailableError(f"{path} is held")
        yield  # pragma: no cover

    monkeypatch.setattr(platform_util, "file_lock", busy)
    with pytest.raises(TicketsLockBusy):
        tree.mark_initiative_done(parent, AUTH, journal=journal)
    assert seen == [] and file.read_bytes() == original
    assert [e["outcome"] for e in _initiative_marks(journal)] == ["failed"]


def test_restore_preimage_undoes_an_initiative_close(tmp_path, monkeypatch, journal):
    """The close's pre-image, handed to `restore_preimage`, puts the original
    bytes back — the undo a later caller makes."""
    tree, parent, file, _ = _closing(tmp_path, monkeypatch, "")
    original = file.read_bytes()
    result = tree.mark_initiative_done(parent, AUTH, journal=journal)
    assert result.preimage == original and file.read_bytes() != original

    tree.restore_preimage(result.file, result.preimage, journal)

    assert file.read_bytes() == original
    restored = [e for e in journal.entries() if e["kind"] == "ticket-plan-restored"]
    assert [(e["before"], e["after"], e["outcome"]) for e in restored] == [("done", "", "restored")]


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
    epic_dir = layout.initiative_folder / "tickets" / "epic-cart" / "tickets"
    return (epic_dir / f"{CART_MODEL_PLAN}.md").resolve()


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


def test_mark_retypes_a_plan_stat_oserror(tmp_path, monkeypatch, journal):
    """A plan whose existence probe fails (a stat error other than "not there")
    raises the typed `TicketsPlanIOError` and journals its `failed`
    `ticket-mark`, rather than escaping raw (`Path.is_file` before 3.14) or
    reading as "no plan" (3.14 on).

    Ablation: put `plan.is_file()` back in `_plan_fields` and this reddens on
    every supported Python."""
    tree, plan, fake = _tree_with_plan(tmp_path, monkeypatch, "built")
    real = Path.stat

    def stat(self: Path, *, follow_symlinks: bool = True):
        if self == plan:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(TicketsPlanIOError, match="cannot stat"):
        tree.mark("1.1", "done", reason="accept", expected_plan=plan, journal=journal)
    assert fake.events == ["find"]
    assert [(e["after"], e["outcome"]) for e in _marks(journal)] == [("?", "failed")]


def test_mark_clears_a_stale_blocked_at(tmp_path, monkeypatch, journal):
    """A plan already at the re-arm target but still stamped `blocked_at` (no
    reason) is written — the script reads it as blocked — and the read-back
    requires the stamp gone.

    Ablation: drop `blocked_at=` from the `check_plan_transition` call in
    `TicketTree.mark` and this reddens (a no-op, no `mark` child)."""
    tree, plan, fake = _tree_with_plan(tmp_path, monkeypatch, "in-progress")
    plan.write_text(
        "---\nticket: 1\nstatus: in-progress\nblocked_at: 2026-10-14\n---\n\n# plan\n",
        encoding="utf-8",
    )
    result = tree.mark("1.1", "in-progress", reason="rearm", expected_plan=plan, journal=journal)
    assert (result.before, result.after, result.outcome) == (
        "in-progress",
        "in-progress",
        "written",
    )
    assert fake.events == ["find", "mark"]
    assert "blocked_at" not in frontmatter.read_frontmatter(plan)

    plan.write_text(
        "---\nticket: 1\nstatus: in-progress\nblocked_at: 2026-10-14\n---\n\n# plan\n",
        encoding="utf-8",
    )
    real_call = tree._call

    def mark_keeping_the_stamp(tail, *, verb, json_out=True):
        doc = real_call(tail, verb=verb, json_out=json_out)
        text = plan.read_text(encoding="utf-8")
        plan.write_text(text.replace("---\n\n", "blocked_at: 2026-10-14\n---\n\n", 1), "utf-8")
        return doc

    monkeypatch.setattr(tree, "_call", mark_keeping_the_stamp)
    with pytest.raises(TicketsReadBackMismatch, match="blocked_at"):
        tree.mark("1.1", "in-progress", reason="rearm", expected_plan=plan, journal=journal)


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


# ------------------------------------------------------------- v6_key claims

V6_TREE = (
    EpicSpec(
        1,
        "epic-cart",
        "Cart",
        (
            EntrySpec(1, "Cart model", v6_key="3-2-invite-link"),
            EntrySpec(2, "Cart rules"),  # carries no v6_key: claims nothing
        ),
    ),
    EpicSpec(2, "epic-checkout", "Checkout", (EntrySpec(1, "Checkout form", v6_key="4-1-pay"),)),
)


def _v6_layout(tmp_path: Path) -> V7Layout:
    """`V6_TREE` in the preview layout: these claims tests pin the flat reader and
    never spawn the script (`_tickets_layout_tree` is the `tickets/` twin)."""
    layout = v7_layout(tmp_path / "sandbox")
    write_ticket_tree(layout, V6_TREE, flat=True)
    return layout


def test_v6_key_claims_read_from_the_epic_ticket_files(tmp_path: Path):
    """Every entry's `v6_key` is a claim on its `E.M` ref, the epic id taken from
    the initiative's `[[epic]]` row for its folder. An epic folder the table does
    not list still claims, with no ref, and a folder without a `tickets.toml`
    holds no entries."""
    layout = _v6_layout(tmp_path)
    orphan = layout.initiative_folder / "epic-orphan"
    orphan.mkdir()
    (orphan / "tickets.toml").write_text(
        '[[entry]]\nid = "06"\ntype = "story"\nv6_key = "5-1-x"\n', encoding="utf-8"
    )
    (layout.initiative_folder / "epic-empty").mkdir()

    assert read_v6_key_claims(layout.initiative_folder) == (
        ("3-2-invite-link", "1.1"),
        ("4-1-pay", "2.1"),
        ("5-1-x", None),
    )


def test_v6_key_entries_carry_the_ticket_identity(tmp_path: Path):
    """Each claim keeps the identity its ref is derived from — the epic folder and
    the entry id as the script compares it — so `names` matches the picked
    ticket's binding (`06` is entry `6`) even when the ref has moved."""
    layout = _v6_layout(tmp_path)
    orphan = layout.initiative_folder / "epic-orphan"
    orphan.mkdir()
    (orphan / "tickets.toml").write_text(
        '[[entry]]\nid = "06"\ntype = "story"\nv6_key = "5-1-x"\n', encoding="utf-8"
    )

    entries = read_v6_key_entries(layout.initiative_folder)

    assert entries[-1] == V6KeyClaim("5-1-x", None, "epic-orphan", "6")
    assert entries[-1].names("epic-orphan", "06") and entries[-1].names("epic-orphan", "6")
    assert not entries[-1].names("epic-orphan", "7")
    assert not entries[-1].names(V6_TREE[0].slug, "6")
    assert tuple((c.key, c.ref) for c in entries) == read_v6_key_claims(layout.initiative_folder)


def test_v6_key_claims_absent_tree_reads_empty(tmp_path: Path):
    """No initiative, a missing initiative folder, or epics without ticket files
    hold no claims — absence, not a fault."""
    layout = _v6_layout(tmp_path)
    assert read_v6_key_claims(None) == ()
    assert read_v6_key_claims(tmp_path / "nowhere") == ()
    for epic in V6_TREE:
        (layout.initiative_folder / epic.slug / "tickets.toml").unlink()
    assert read_v6_key_claims(layout.initiative_folder) == ()


def test_v6_key_claims_skip_a_byte_order_mark(tmp_path: Path):
    """A BOM ahead of the first table is skipped, as `tickets.py` reads with
    `utf-8-sig`."""
    layout = _v6_layout(tmp_path)
    toml = layout.initiative_folder / "epic-checkout" / "tickets.toml"
    toml.write_bytes(b"\xef\xbb\xbf" + toml.read_bytes())
    assert ("4-1-pay", "2.1") in read_v6_key_claims(layout.initiative_folder)


@pytest.mark.parametrize(
    ("epic_toml", "needle"),
    [
        ("[[entry]\nid = 1\n", "epic-checkout"),  # not TOML
        ("[[entry]]\nid = 1\nv6_key = 32\n", "`v6_key` is not a string"),
        ('[[entry]]\nid = true\nv6_key = "3-2"\n', "is not an id"),
        ('[[entry]]\nid = "a.b"\n', "is not an id"),
        (
            '[[entry]]\nid = 1\n\n[[entry]]\nid = "01"\nv6_key = "4-1-pay"\n',
            "two entries with id 1",
        ),
        ("entry = [1, 2]\n", "write `[[entry]]` tables"),
        ("entry = 3\n", "write `[[entry]]` tables"),
    ],
)
def test_v6_key_claims_fail_loud_on_a_mistyped_ticket_file(tmp_path: Path, epic_toml, needle):
    """A ticket file that is not TOML, that mistypes an entry, or that gives two
    entries one id (which `tickets.py` refuses) raises
    `TicketsV6KeyUnreadable` (kind `probe-failed`) rather than reading as "no
    claims", which would leave a v6 token legacy under the wrong remedy."""
    layout = _v6_layout(tmp_path)
    (layout.initiative_folder / "epic-checkout" / "tickets.toml").write_text(
        epic_toml, encoding="utf-8"
    )
    with pytest.raises(TicketsV6KeyUnreadable, match=re.escape(needle)) as raised:
        read_v6_key_claims(layout.initiative_folder)
    assert raised.value.kind == "probe-failed"
    assert isinstance(raised.value, TicketsError)


@pytest.mark.parametrize(
    ("initiative_toml", "needle"),
    [
        ("[[epic]]\nid = 1\n", "epic 1 needs a `slug`"),
        ('[[epic]]\nid = false\nslug = "epic-cart"\n', "epic id False is not an id"),
        ("epic = 1\n", "write `[[epic]]` tables"),
        (
            '[[epic]]\nid = 1\nslug = "epic-cart"\n\n[[epic]]\nid = 2\nslug = "epic-cart"\n',
            "two epics with slug epic-cart",
        ),
        (
            '[[epic]]\nid = 1\nslug = "epic-cart"\n\n[[epic]]\nid = "01"\nslug = "epic-checkout"\n',
            "two epics with id 1",
        ),
    ],
)
def test_v6_key_claims_fail_loud_on_a_mistyped_epic_table(tmp_path: Path, initiative_toml, needle):
    """The initiative table that maps a folder to its epic id is read as strictly:
    an epic row without a slug, with a refused id, not a table, or an id or slug
    two rows share raises, as `tickets.py` refuses that tree."""
    layout = _v6_layout(tmp_path)
    (layout.initiative_folder / "tickets.toml").write_text(initiative_toml, encoding="utf-8")
    with pytest.raises(TicketsV6KeyUnreadable, match=re.escape(needle)) as raised:
        read_v6_key_claims(layout.initiative_folder)
    assert raised.value.kind == "probe-failed"


def test_v6_key_claims_fail_loud_on_an_unreadable_ticket_file(tmp_path: Path, monkeypatch):
    """A read fault other than absence raises; it is never read as no entries."""
    layout = _v6_layout(tmp_path)
    target = layout.initiative_folder / "epic-cart" / "tickets.toml"
    real = Path.read_text

    def refuse(self: Path, *args, **kwargs) -> str:
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)
    with pytest.raises(TicketsV6KeyUnreadable, match="Permission denied") as raised:
        read_v6_key_claims(layout.initiative_folder)
    assert raised.value.path == target and raised.value.kind == "probe-failed"


def test_v6_key_claims_fail_loud_on_a_refused_stat(tmp_path: Path, monkeypatch):
    """A refused existence probe of one ticket file raises; only absence reads as
    no entries."""
    layout = _v6_layout(tmp_path)
    target = layout.initiative_folder / "epic-checkout" / "tickets.toml"
    real = Path.stat

    def refuse(self: Path, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", refuse)
    with pytest.raises(TicketsV6KeyUnreadable, match="Permission denied") as raised:
        read_v6_key_claims(layout.initiative_folder)
    assert raised.value.path == target and raised.value.kind == "probe-failed"


def test_v6_key_claims_fail_loud_on_a_refused_listing(tmp_path: Path, monkeypatch):
    """A refused listing of the initiative folder raises rather than reading as
    "no epics" (which `Path.glob` would answer)."""
    layout = _v6_layout(tmp_path)
    real = os.scandir

    def refuse(path=".", *args, **kwargs):
        if Path(path) == layout.initiative_folder:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", refuse)
    with pytest.raises(TicketsV6KeyUnreadable, match="Permission denied") as raised:
        read_v6_key_claims(layout.initiative_folder)
    assert raised.value.path == layout.initiative_folder
    assert raised.value.kind == "probe-failed"


def test_v6_key_claims_read_zero_padded_ids_as_numbers(tmp_path: Path):
    """Ids compare as the script compares them, digits alone being the number, so
    an epic `"06"` and an entry `"06"` make the ref `6.6`."""
    layout = v7_layout(tmp_path / "sandbox")
    folder = layout.initiative_folder / "epic-pad"
    folder.mkdir(parents=True)
    (layout.initiative_folder / "tickets.toml").write_text(
        '[[epic]]\nid = "06"\nslug = "epic-pad"\n', encoding="utf-8"
    )
    (folder / "tickets.toml").write_text(
        '[[entry]]\nid = "06"\ntype = "story"\nv6_key = "6-6-pad"\n', encoding="utf-8"
    )
    assert read_v6_key_claims(layout.initiative_folder) == (("6-6-pad", "6.6"),)


def _tickets_layout_tree(tmp_path: Path) -> Path:
    """`V6_TREE` hand-written in the current upstream layout: the initiative's
    `tickets.toml` and epic folders in its `tickets/` folder, each epic's
    `tickets.toml` in the epic's own `tickets/` folder beside nothing but its
    `<slug>.md` — plus one epic left flat inside it, as the layout is decided per
    container."""
    layout = v7_layout(tmp_path / "sandbox")
    breakdown = layout.initiative_folder / "tickets"
    breakdown.mkdir(parents=True)
    (layout.initiative_folder / f"{layout.initiative}.md").write_text(
        "---\ntitle: Demo\n---\n", encoding="utf-8"
    )
    (breakdown / "tickets.toml").write_text(
        '[[epic]]\nid = 1\nslug = "epic-cart"\n\n[[epic]]\nid = 2\nslug = "epic-checkout"\n',
        encoding="utf-8",
    )
    for slug, entries in (
        ("epic-cart", '[[entry]]\nid = 1\nv6_key = "3-2-invite-link"\n\n[[entry]]\nid = 2\n'),
        ("epic-checkout", '[[entry]]\nid = 1\nv6_key = "4-1-pay"\n'),
    ):
        (breakdown / slug / "tickets").mkdir(parents=True)
        (breakdown / slug / f"{slug}.md").write_text("---\nstatus: open\n---\n", encoding="utf-8")
        (breakdown / slug / "tickets" / "tickets.toml").write_text(entries, encoding="utf-8")
    flat = breakdown / "epic-orphan"
    flat.mkdir()
    (flat / "tickets.toml").write_text('[[entry]]\nid = "06"\nv6_key = "5-1-x"\n', encoding="utf-8")
    return layout.initiative_folder


def test_v6_key_claims_read_from_the_tickets_folder_layout(tmp_path: Path):
    """A tree in the `tickets/` layout claims what the flat tree claims: the
    initiative's epic table and folders are read in its `tickets/` folder, each
    epic's entries in its own (or beside its file, for an epic still flat).
    Ablation (performed): read the initiative folder itself in `_epic_folders`
    and no epic is found — this FAILS."""
    initiative = _tickets_layout_tree(tmp_path)
    assert read_v6_key_claims(initiative) == (
        ("3-2-invite-link", "1.1"),
        ("4-1-pay", "2.1"),
        ("5-1-x", None),
    )


def test_v6_key_claims_name_the_tickets_folder_file_in_a_fault(tmp_path: Path):
    """A fault in a `tickets/`-layout file names that file, not the flat path."""
    initiative = _tickets_layout_tree(tmp_path)
    target = initiative / "tickets" / "epic-checkout" / "tickets" / "tickets.toml"
    target.write_text("entry = 3\n", encoding="utf-8")
    with pytest.raises(TicketsV6KeyUnreadable) as raised:
        read_v6_key_claims(initiative)
    assert raised.value.path == target


@pytest.mark.parametrize("container", ["", "tickets/epic-cart"], ids=["initiative", "epic"])
def test_v6_key_claims_fail_loud_on_a_refused_tickets_probe(
    tmp_path: Path, monkeypatch, container: str
):
    """A refused stat of a container's `tickets/` folder raises
    `TicketsV6KeyUnreadable` naming it; it is never read as the flat layout,
    which would answer "no epics" or "no entries". Ablation (performed): drop the
    `OSError` wrap in `_breakdown_dir` and a bare `PermissionError` escapes —
    this FAILS."""
    initiative = _tickets_layout_tree(tmp_path)
    target = (initiative / container if container else initiative) / "tickets"
    real = os.stat

    def refuse(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", refuse)
    with pytest.raises(TicketsV6KeyUnreadable, match="Permission denied") as raised:
        read_v6_key_claims(initiative)
    assert raised.value.path == target and raised.value.kind == "probe-failed"


def test_v6_key_claims_read_bare_cr_line_endings(tmp_path: Path):
    """A ticket file with bare-CR line endings reads as the script's universal
    newlines read it, not as a TOML fault."""
    layout = _v6_layout(tmp_path)
    toml = layout.initiative_folder / "epic-checkout" / "tickets.toml"
    toml.write_bytes(toml.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r"))
    assert ("4-1-pay", "2.1") in read_v6_key_claims(layout.initiative_folder)
