"""The pure half of `ticketstore`: the layout classifier, the refusal text, the
recorded-topology round trip and the `config.user.toml` redirect rewrite. Every
fact is hand-built — no git, no disk (the probe has tests/test_ticketstore_probe.py).
Real-spawn budget: 0."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from bmad_loop import ticketstore
from bmad_loop.ticketstore import (
    StoreFacts,
    StoreRefusal,
    StoreTopology,
    TicketStoreError,
    classify_store,
    redirected_user_config_text,
    store_topology_conflict,
)

ROOT = Path("/w/code").resolve()
STORE = ROOT / "_bmad-output"
INITIATIVE = STORE / "initiative-demo"


def _facts(**over: object) -> StoreFacts:
    """A tracked in-checkout store (T1) unless overridden."""
    base = StoreFacts(
        project=ROOT,
        repo_root=ROOT,
        store_root=STORE,
        initiative=INITIATIVE,
        project_is_repo=True,
        repo_toplevel=ROOT,
        store_toplevel=ROOT,
        tracked=(
            ("_bmad-output/initiative-demo/tickets.toml", True),
            ("_bmad-output/initiative-demo/epic-cart/tickets.toml", True),
        ),
        ignored=False,
        user_config_tracked=False,
        user_config_ignored=True,
    )
    return replace(base, **over)  # type: ignore[arg-type]


def _ignored(**over: object) -> StoreFacts:
    """An ignored plain folder in the checkout (T2) unless overridden."""
    fields: dict[str, object] = {
        "tracked": (("_bmad-output/initiative-demo/tickets.toml", False),),
        "ignored": True,
        **over,
    }
    return _facts(**fields)


@pytest.mark.parametrize("isolation", ["none", "worktree"])
def test_classify_tracked_store_is_per_worktree(isolation):
    result = classify_store(_facts(), isolation)
    assert result == StoreTopology("T1", "checkout", "code-repo", STORE)
    assert isinstance(result, StoreTopology)
    assert result.per_worktree() and not result.needs_redirect(isolation)
    assert store_topology_conflict(result) is None


def test_classify_ignored_in_repo_store_needs_redirect_only_under_worktree():
    for isolation, redirect in (("none", False), ("worktree", True)):
        result = classify_store(_ignored(), isolation)
        assert result == StoreTopology("T2", "shared-in-project", "none", STORE)
        assert isinstance(result, StoreTopology)
        assert result.needs_redirect(isolation) is redirect


def test_classify_nested_store_repo():
    result = classify_store(_ignored(store_toplevel=STORE, tracked=()), "worktree")
    assert result == StoreTopology("T3", "shared-in-project", "store-repo", STORE, STORE)


def test_classify_external_store_repo():
    outside = Path("/w/store").resolve()
    facts = _facts(
        store_root=outside,
        initiative=outside / "initiative-demo",
        store_toplevel=outside,
        tracked=(),
        ignored=False,
    )
    result = classify_store(facts, "worktree")
    assert result == StoreTopology("T4", "shared-outside", "store-repo", outside, outside)


def test_classify_external_plain_folder():
    outside = Path("/w/store").resolve()
    facts = _facts(store_root=outside, initiative=outside / "i", store_toplevel=None, tracked=())
    result = classify_store(facts, "none")
    assert result == StoreTopology("T6", "shared-outside", "none", outside)


@pytest.mark.parametrize("isolation", ["none", "worktree"])
def test_workspace_layout_refused(isolation):
    """T5 — the project root is not a repository (a BMAD workspace holding sibling
    code repos) — is refused under either isolation; it is deferred. Ablation
    (performed): drop the `project_is_repo` arm and this FAILS (it classifies)."""
    result = classify_store(_facts(project_is_repo=False, repo_toplevel=None), isolation)
    assert isinstance(result, StoreRefusal)
    assert result.code == "workspace-unsupported"
    assert result.check == "tickets.workspace-unsupported"
    assert "workspace" in (store_topology_conflict(result) or "")


def test_partial_tracking_refused():
    """Some tree markers tracked and some not is neither T1 nor T2: refused, naming
    the untracked ones. Ablation (performed): delete the mixed-tracking arm in
    `classify_store` and this FAILS — the store falls through to the untracked arm
    and is refused as `store-not-ignored` instead."""
    facts = _facts(
        tracked=(
            ("_bmad-output/initiative-demo/tickets.toml", True),
            ("_bmad-output/initiative-demo/epic-cart/tickets.toml", False),
        )
    )
    result = classify_store(facts, "none")
    assert isinstance(result, StoreRefusal)
    assert result.code == "partial-tracking"
    assert result.check == "tickets.tree-untracked"
    text = store_topology_conflict(result) or ""
    assert "epic-cart/tickets.toml" in text and "partly tracked" in text


def test_nested_store_not_ignored_refused():
    """A nested store repository the code repo does not ignore is untracked dirt
    (`dir/` in `git status`) and build-auto halts on it. Ablation (performed): drop
    the not-ignored check in the own-repo arm and this FAILS (it classifies T3)."""
    result = classify_store(_facts(store_toplevel=STORE, tracked=(), ignored=False), "none")
    assert isinstance(result, StoreRefusal)
    assert result.code == "store-not-ignored"
    assert result.path == STORE  # the repository root, which is what must be ignored
    assert f"store repository {STORE} is nested" in (store_topology_conflict(result) or "")


def test_untracked_unignored_folder_refused():
    result = classify_store(
        _facts(tracked=(("_bmad-output/initiative-demo/tickets.toml", False),)), "none"
    )
    assert isinstance(result, StoreRefusal) and result.code == "store-not-ignored"


def test_tracked_store_outside_the_project_refused_under_worktree():
    """The DW-485 shape: a tracked store inside `repo_root` but outside a nested
    project stays at the main checkout under worktree isolation, so a unit would
    mark the main copy. Fine with isolation "none"."""
    project = ROOT / "app"
    facts = _facts(project=project)
    refused = classify_store(facts, "worktree")
    assert isinstance(refused, StoreRefusal) and refused.code == "tracked-outside-project"
    assert classify_store(facts, "none") == StoreTopology("T1", "checkout", "code-repo", STORE)


@pytest.mark.parametrize("isolation", ["none", "worktree"])
def test_tracked_store_outside_repo_root_refused(isolation):
    """A tracked store in the code repository but outside a nested `repo_root`:
    bmad-loop's by-path commits cover only `repo_root`, so it is refused under
    either isolation."""
    facts = _facts(repo_root=ROOT / "app", project=ROOT / "app")
    refused = classify_store(facts, isolation)
    assert isinstance(refused, StoreRefusal) and refused.code == "tracked-outside-checkout"
    text = store_topology_conflict(refused) or ""
    assert str(INITIATIVE) in text and str(ROOT / "app") in text


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"user_config_tracked": True, "user_config_ignored": False}, "user-config-tracked"),
        ({"user_config_ignored": False}, "user-config-not-ignored"),
    ],
)
def test_tracked_user_config_refuses_redirect(over, code):
    """A shared store under worktree isolation is reached through the redirect,
    which rewrites `_bmad/custom/config.user.toml` — refused when that file is
    tracked (the rewrite would ride the unit commit) or not gitignored (it would
    dirty the mount). Not refused without the redirect. Ablation (performed): drop
    the redirect precondition block and both rows FAIL."""
    refused = classify_store(_ignored(**over), "worktree")
    assert isinstance(refused, StoreRefusal)
    assert refused.code == code
    assert refused.check == "tickets.own-repo-isolation"
    assert "config.user.toml" in (store_topology_conflict(refused) or "")
    assert isinstance(classify_store(_ignored(**over), "none"), StoreTopology)


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"user_config_tracked": True, "user_config_ignored": False}, "user-config-tracked"),
        ({"user_config_ignored": False}, "user-config-not-ignored"),
    ],
)
def test_chained_run_refuses_an_unsafe_user_config_on_a_tracked_store(over, code):
    """A chained run under worktree isolation rewrites the mount's
    `config.user.toml` (its child as `active_initiative`) on every layout, so a
    tracked store (T1), which needs no redirect, is refused like a shared one when
    the file is tracked or not gitignored. An unchained T1 run writes nothing there
    and still runs; without isolation there is no mount, so a chained run writes
    nothing either and runs. Ablation (performed): drop `chained` from the
    precondition in `classify_store` and both rows FAIL."""
    refused = classify_store(_facts(**over), "worktree", chained=True)
    assert isinstance(refused, StoreRefusal) and refused.code == code
    assert "chained run's child initiative" in (store_topology_conflict(refused) or "")
    t1 = StoreTopology("T1", "checkout", "code-repo", STORE)
    assert classify_store(_facts(**over), "worktree") == t1
    assert classify_store(_facts(**over), "none", chained=True) == t1
    assert classify_store(_facts(), "worktree", chained=True) == t1


@pytest.mark.parametrize("layout", ["T2", "T3", "T4", "T6"])
def test_shared_layouts_run(layout):
    """Every classified shared layout runs: its plans are marked after the code
    lands (and committed in a store repository of its own), so no layout but a
    refusal or a changed store refuses a run. Ablation (performed): restore the
    non-T1 arm of `store_topology_conflict` and every row FAILS."""
    versioning = {"T2": "none", "T3": "store-repo", "T4": "store-repo", "T6": "none"}[layout]
    repo = STORE if versioning == "store-repo" else None
    topology = StoreTopology(layout, "shared-in-project", versioning, STORE, repo)  # type: ignore[arg-type]
    assert store_topology_conflict(topology) is None
    assert store_topology_conflict(topology, recorded=topology.to_dict()) is None


def test_changed_topology_refused_against_the_recorded_one():
    """Resume compares the live topology with the recorded one. Ablation
    (performed): drop the `recorded` arm and this FAILS (T1 passes)."""
    live = StoreTopology("T1", "checkout", "code-repo", STORE)
    assert store_topology_conflict(live, recorded=live.to_dict()) is None
    moved = {**live.to_dict(), "store_root": "/elsewhere"}
    text = store_topology_conflict(live, recorded=moved) or ""
    assert "changed since this run started" in text


def test_topology_dict_round_trip():
    for topology in (
        StoreTopology("T1", "checkout", "code-repo", STORE),
        StoreTopology("T3", "shared-in-project", "store-repo", STORE, STORE),
    ):
        assert StoreTopology.from_dict(topology.to_dict()) == topology


@pytest.mark.parametrize(
    "mangled",
    [
        {"layout": "T1"},
        {**StoreTopology("T1", "checkout", "code-repo", STORE).to_dict(), "layout": "T9"},
        {**StoreTopology("T1", "checkout", "code-repo", STORE).to_dict(), "versioning": "none"},
        {**StoreTopology("T1", "checkout", "code-repo", STORE).to_dict(), "store_repo": "/r"},
        {**StoreTopology("T1", "checkout", "code-repo", STORE).to_dict(), "extra": None},
    ],
)
def test_topology_from_dict_refuses_other_shapes(mangled):
    with pytest.raises(ValueError):
        StoreTopology.from_dict(mangled)


def test_with_shared_store_sets_the_store_only_when_shared():
    from bmad_loop.bmadconfig import ProjectPaths

    paths = ProjectPaths(
        project=ROOT, implementation_artifacts=INITIATIVE, planning_artifacts=INITIATIVE
    )
    t1 = StoreTopology("T1", "checkout", "code-repo", STORE).to_dict()
    t2 = StoreTopology("T2", "shared-in-project", "none", STORE).to_dict()
    assert ticketstore.with_shared_store(paths, {}) is paths
    assert ticketstore.with_shared_store(paths, t1).shared_store is None
    assert ticketstore.with_shared_store(paths, t2).shared_store == STORE


MAIN = Path("/main/_bmad-output")


def _redirected(text: str) -> dict:
    return tomllib.loads(redirected_user_config_text(text, main_store=MAIN, initiative=None))


def test_redirected_user_config_text_inserts_into_core_table():
    text = '[core]\nactive_initiative = "initiative-demo"\n'
    out = redirected_user_config_text(text, main_store=MAIN, initiative=None)
    assert out.startswith("[core]\noutput_folder = ")
    assert tomllib.loads(out)["core"] == {
        "output_folder": str(MAIN),
        "active_initiative": "initiative-demo",
    }


def test_redirected_user_config_text_replaces_existing_output_folder():
    text = (
        '[core]\noutput_folder = "elsewhere"\nactive_initiative = "x"\n[other]\noutput_folder = 1\n'
    )
    data = _redirected(text)
    assert data["core"]["output_folder"] == str(MAIN)
    assert data["other"] == {"output_folder": 1}  # another table's key is kept


def test_redirected_user_config_text_appends_core_table_when_absent():
    assert _redirected('[modules.x]\nk = "v"')["core"] == {"output_folder": str(MAIN)}
    assert redirected_user_config_text(
        "", main_store=MAIN, initiative=None
    ) == f'[core]\noutput_folder = "{MAIN}"\n'.replace("\\", "\\\\")


@pytest.mark.parametrize(
    "text", ['core = { active_initiative = "x" }\n', 'core.active_initiative = "x"\n']
)
def test_redirected_user_config_text_refuses_inline_core_table(text):
    with pytest.raises(TicketStoreError):
        redirected_user_config_text(text, main_store=MAIN, initiative=None)


def test_redirected_user_config_text_preserves_crlf():
    text = '# mine\r\n[core]\r\nactive_initiative = "x"\r\n'
    out = redirected_user_config_text(text, main_store=MAIN, initiative=None)
    assert out.count("\r\n") == out.count("\n") == 4
    assert out.startswith("# mine\r\n[core]\r\noutput_folder = ")


@pytest.mark.parametrize("raw", ['/w/a "quoted" dir', "C:\\Users\\me\\store"])
def test_redirected_user_config_text_escapes_the_path(raw):
    """Quotes and backslashes (a Windows path) survive as a TOML basic string."""
    store = Path(raw)
    assert tomllib.loads(redirected_user_config_text("", main_store=store, initiative=None))[
        "core"
    ]["output_folder"] == str(store)


def test_redirected_user_config_text_writes_only_the_initiative():
    """A chained run over a tracked store sets `active_initiative` alone: the
    existing value is replaced in place under `[core]`, no `output_folder` appears,
    and every other line is kept."""
    text = '# mine\n[core]\nactive_initiative = "initiative-parent"\nlang = "en"\n'
    out = redirected_user_config_text(text, main_store=None, initiative="initiative-child")
    assert out == '# mine\n[core]\nactive_initiative = "initiative-child"\nlang = "en"\n'


def test_redirected_user_config_text_writes_both_values():
    text = '[core]\nactive_initiative = "initiative-parent"\noutput_folder = "x"\n[other]\nk = 1\n'
    data = tomllib.loads(
        redirected_user_config_text(text, main_store=MAIN, initiative="initiative-child")
    )
    assert data == {
        "core": {"output_folder": str(MAIN), "active_initiative": "initiative-child"},
        "other": {"k": 1},
    }
    appended = redirected_user_config_text("", main_store=MAIN, initiative="initiative-child")
    assert tomllib.loads(appended)["core"] == data["core"]


def test_redirected_user_config_text_keeps_crlf_with_both_values():
    text = '[core]\r\nactive_initiative = "p"\r\n'
    out = redirected_user_config_text(text, main_store=MAIN, initiative="c")
    assert out.count("\r\n") == out.count("\n") == 3
    assert tomllib.loads(out)["core"] == {"output_folder": str(MAIN), "active_initiative": "c"}


def test_redirected_user_config_text_escapes_the_initiative():
    raw = 'odd "quoted" \\ name'
    out = redirected_user_config_text("", main_store=None, initiative=raw)
    assert tomllib.loads(out)["core"] == {"active_initiative": raw}


def test_redirected_user_config_text_needs_a_value():
    """Nothing to write is a caller fault; a blank initiative would read as no
    active initiative, so it is refused rather than written."""
    with pytest.raises(ValueError):
        redirected_user_config_text("", main_store=None, initiative=None)
    with pytest.raises(TicketStoreError, match="blank initiative"):
        redirected_user_config_text("", main_store=MAIN, initiative="  ")


@pytest.mark.parametrize(
    "text", ['core = { active_initiative = "x" }\n', 'core.active_initiative = "x"\n']
)
def test_redirected_user_config_text_refuses_inline_core_table_for_the_initiative(text):
    with pytest.raises(TicketStoreError):
        redirected_user_config_text(text, main_store=None, initiative="initiative-child")
