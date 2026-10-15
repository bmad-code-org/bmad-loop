"""The probe half of `ticketstore` on real git repositories: what
`probe_store_facts` reads, what `assess_store` / `store_conflict` answer for each
sandbox layout, and `redirect_mount_store` on disk. Real-spawn budget: 0 spawns of
tickets.py (git only)."""

from __future__ import annotations

import tomllib

import pytest
from conftest import git, remove_tree

from bmad_loop import bmadconfig, ticketstore, verify
from bmad_loop.ticketstore import StoreRefusal, StoreTopology


def _paths(layout):
    return bmadconfig.load_paths(layout.project, want_tickets=True)


def test_probe_tracked_markers(project_v7):
    """The tracked store of the default sandbox: every tree marker is tracked in the
    code repo, the store's toplevel is the code repo's, and the personal config is
    ignored — layout T1."""
    paths = _paths(project_v7)
    facts = ticketstore.probe_store_facts(paths, paths.initiative_folder)
    rels = [rel for rel, _ in facts.tracked]
    assert "_bmad-output/initiative-demo/tickets.toml" in rels
    assert "_bmad-output/initiative-demo/epic-cart/epic-cart.md" in rels
    assert all(tracked for _, tracked in facts.tracked)
    assert facts.store_toplevel == facts.repo_toplevel == project_v7.project.resolve()
    assert facts.user_config_ignored and not facts.user_config_tracked
    assert ticketstore.assess_store(paths, "worktree") == StoreTopology(
        "T1", "checkout", "code-repo", project_v7.output_folder.resolve()
    )


def test_probe_reads_nested_store_toplevel(project_v7_store_repo):
    """A nested, ignored store repository: its own toplevel, nothing tracked in the
    code repo, the initiative ignored — layout T3, its repository recorded."""
    layout = project_v7_store_repo
    paths = _paths(layout)
    facts = ticketstore.probe_store_facts(paths, paths.initiative_folder)
    store = layout.output_folder.resolve()
    assert facts.store_toplevel == store != facts.repo_toplevel
    assert facts.tracked == () and facts.ignored
    assert ticketstore.assess_store(paths, "worktree") == StoreTopology(
        "T3", "shared-in-project", "store-repo", store, store
    )


def test_probe_external_store_repo(project_v7_external_store):
    layout = project_v7_external_store
    store = layout.output_folder.resolve()
    assert ticketstore.assess_store(_paths(layout), "worktree") == StoreTopology(
        "T4", "shared-outside", "store-repo", store, store
    )


def test_probe_nested_store_repo_not_ignored_is_refused(project_v7_store_repo):
    """Drop the code repo's ignore rule and the nested repository shows as the
    untracked `dir/` `dirty_paths` reports — refused as `store-not-ignored`."""
    root = project_v7_store_repo.project
    (root / ".gitignore").write_text(
        ".bmad-loop/runs/\n_bmad/custom/*.user.toml\n", encoding="utf-8"
    )
    assert "_bmad-output/" in verify.dirty_paths(root)
    result = ticketstore.assess_store(_paths(project_v7_store_repo), "none")
    assert isinstance(result, StoreRefusal) and result.code == "store-not-ignored"


def test_probe_non_repo_workspace(project_v7):
    """The project is not a repository (a BMAD workspace): T5, refused."""
    remove_tree(project_v7.project / ".git")
    result = ticketstore.assess_store(_paths(project_v7), "none")
    assert isinstance(result, StoreRefusal) and result.code == "workspace-unsupported"


def test_probe_git_fault_is_a_refusal_not_a_guess(project_v7, monkeypatch):
    """A git that cannot answer refuses (`probe-failed`) rather than classifying."""

    def boom(path):
        raise verify.GitSpawnError(f"git rev-parse failed to spawn in {path}")

    monkeypatch.setattr(verify, "git_toplevel", boom)
    result = ticketstore.assess_store(_paths(project_v7), "none")
    assert isinstance(result, StoreRefusal) and result.code == "probe-failed"
    assert "failed to spawn" in (ticketstore.store_topology_conflict(result) or "")


def test_store_conflict_is_silent_outside_tickets_mode(project_v7_store_repo):
    project = project_v7_store_repo.project
    assert ticketstore.store_conflict(project, "sprint-status", "worktree") is None
    assert "layout T3" in (ticketstore.store_conflict(project, "tickets", "worktree") or "")


def test_store_conflict_refuses_a_store_that_changed_since_the_run_started(
    project_v7_store_repo,
):
    """Resume passes the recorded topology; a run recorded on T1 now finding T3 is
    refused as a changed store, not merely as an unsupported layout."""
    recorded = StoreTopology(
        "T1", "checkout", "code-repo", project_v7_store_repo.output_folder.resolve()
    ).to_dict()
    text = ticketstore.store_conflict(
        project_v7_store_repo.project, "tickets", "none", recorded=recorded
    )
    assert text is not None and "changed since this run started" in text


def test_redirect_mount_store_rewrites_the_user_layer(project_v7, tmp_path):
    """The mount's personal layer points `output_folder` at the main store, the
    other keys kept, and the merged config reads it back the way tickets.py does."""
    store = tmp_path / "main-store"
    ticketstore.redirect_mount_store(project_v7.project, store)
    text = (project_v7.project / ticketstore.USER_CONFIG_REL).read_text(encoding="utf-8")
    assert tomllib.loads(text)["core"] == {
        "output_folder": str(store),
        "active_initiative": project_v7.initiative,
    }
    assert bmadconfig.read_core_value(project_v7.project, "output_folder")[0] == str(store)
    assert git(project_v7.project, "status", "--porcelain") == ""


def test_redirect_mount_store_refuses_an_unredirectable_config(project_v7, tmp_path):
    (project_v7.project / ticketstore.USER_CONFIG_REL).write_text(
        'core = { active_initiative = "x" }\n', encoding="utf-8"
    )
    with pytest.raises(ticketstore.TicketStoreError):
        ticketstore.redirect_mount_store(project_v7.project, tmp_path / "store")
