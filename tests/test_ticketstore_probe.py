"""The probe half of `ticketstore` on real git repositories: what
`probe_store_facts` reads, what `assess_store` / `store_conflict` answer for each
sandbox layout, and `redirect_mount_store` on disk. Real-spawn budget: 0 spawns of
tickets.py (git only)."""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

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


def test_probe_tracked_store_outside_a_nested_repo_root_is_refused(project_v7):
    """A `repo_root` nested in the code repository with the tracked store beside it
    (same toplevel, outside `repo_root`): the markers are probed against the
    toplevel, and the tracked store is refused — not a crash deriving a
    `repo_root`-relative path, and not T1, whose by-path commits skip it."""
    root = project_v7.project
    (root / "code").mkdir()
    paths = dataclasses.replace(_paths(project_v7), repo_root=root / "code")
    facts = ticketstore.probe_store_facts(paths, paths.initiative_folder)
    assert facts.tracked and all(tracked for _, tracked in facts.tracked)
    assert "_bmad-output/initiative-demo/tickets.toml" in [rel for rel, _ in facts.tracked]
    for isolation in ("none", "worktree"):
        result = ticketstore.assess_store(paths, isolation)
        assert isinstance(result, StoreRefusal) and result.code == "tracked-outside-checkout"
        assert "outside repo_root" in (ticketstore.store_topology_conflict(result) or "")


def test_probe_unlistable_initiative_is_a_refusal_not_an_empty_tree(project_v7, monkeypatch):
    """An initiative folder git can enter but Python cannot list (an execute-only
    directory, a filesystem race) is `probe-failed`: an empty epic list would let
    the root `tickets.toml` alone read the tree as fully tracked."""
    paths = _paths(project_v7)
    initiative = paths.initiative_folder.resolve()
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self == initiative:
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    result = ticketstore.assess_store(paths, "none")
    assert isinstance(result, StoreRefusal) and result.code == "probe-failed"
    assert "cannot list the initiative folder" in (
        ticketstore.store_topology_conflict(result) or ""
    )


def test_probe_unstattable_epic_is_a_refusal_not_absent(project_v7, monkeypatch):
    """A stat fault on an epic folder is `probe-failed`, never a missing epic."""
    paths = _paths(project_v7)
    epic = paths.initiative_folder.resolve() / "epic-cart"
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self == epic:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    result = ticketstore.assess_store(paths, "none")
    assert isinstance(result, StoreRefusal) and result.code == "probe-failed"
    assert "cannot stat" in (ticketstore.store_topology_conflict(result) or "")


def test_probe_unstattable_initiative_is_a_refusal_not_missing(project_v7, monkeypatch):
    """A stat fault on the initiative folder itself is `probe-failed`: neither an
    untyped `OSError` out of validate/preflight nor (Python 3.14 `is_dir`) a
    missing initiative that records no topology."""
    paths = _paths(project_v7)
    initiative = paths.initiative_folder
    faulty = {initiative, initiative.resolve()}
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self in faulty:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    result = ticketstore.assess_store(paths, "none")
    assert isinstance(result, StoreRefusal) and result.code == "probe-failed"
    assert "cannot stat" in (ticketstore.store_topology_conflict(result) or "")


def test_probe_refused_store_repository_is_a_refusal_not_unversioned(project_v7_store_repo):
    """A store repository git refuses to open (here a broken gitfile; a
    `safe.directory` ownership refusal answers the same way) is `probe-failed`,
    never the unversioned T6 a store in no repository classifies as."""
    store = project_v7_store_repo.output_folder
    (store / ".git").rename(store / ".git-moved")
    (store / ".git").write_text("gitdir: /nonexistent/store.git\n", encoding="utf-8")
    try:
        result = ticketstore.assess_store(_paths(project_v7_store_repo), "worktree")
    finally:  # the fixture's teardown removes the repository it made
        (store / ".git").unlink()
        (store / ".git-moved").rename(store / ".git")
    assert isinstance(result, StoreRefusal) and result.code == "probe-failed"


def test_git_toplevel_reads_only_no_repository_as_none(tmp_path):
    """`git_toplevel` answers None for a directory in no repository and for an
    absent path; a repository git refuses raises."""
    assert verify.git_toplevel(tmp_path) is None
    assert verify.git_toplevel(tmp_path / "absent") is None
    (tmp_path / ".git").write_text("gitdir: /nonexistent/repo.git\n", encoding="utf-8")
    with pytest.raises(verify.GitError, match="show-toplevel"):
        verify.git_toplevel(tmp_path)


def test_git_toplevel_raises_when_the_path_cannot_be_statted(tmp_path, monkeypatch):
    """A failed discovery over a ``path`` that cannot be statted (a permission
    change, a filesystem race) raises rather than reading as an absent path, so
    the probe refuses rather than reading T6. Ablation (performed): restore
    ``Path.is_dir`` in `git_toplevel` and this FAILS (an untyped OSError before
    Python 3.14, None from it)."""
    locked = tmp_path / "locked"
    real = Path.stat

    def stat(self, *, follow_symlinks=True):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(verify.GitError, match="cannot stat"):
        verify.git_toplevel(locked)


def test_git_toplevel_raises_when_the_reported_toplevel_cannot_resolve(project_v7, monkeypatch):
    """A toplevel git reported but `resolve` cannot canonicalize (a permission or
    filesystem race) raises, so the probe refuses rather than reading T6."""
    real = Path.resolve

    def resolve(self, strict=False):
        if strict:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(verify.GitError, match="cannot resolve the toplevel"):
        verify.git_toplevel(project_v7.project)


def test_store_conflict_is_silent_outside_tickets_mode(project_v7_store_repo):
    """Only a tickets run classifies the store: a recorded topology the live T3
    store contradicts refuses a tickets run and nothing else, and the T3 store
    itself runs."""
    project = project_v7_store_repo.project
    recorded = StoreTopology(
        "T1", "checkout", "code-repo", project_v7_store_repo.output_folder.resolve()
    ).to_dict()
    assert (
        ticketstore.store_conflict(project, "sprint-status", "worktree", recorded=recorded) is None
    )
    assert ticketstore.store_conflict(project, "tickets", "worktree", recorded=recorded)
    assert ticketstore.store_conflict(project, "tickets", "worktree") is None


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


def test_redirect_mount_store_refuses_an_unreadable_user_layer(project_v7, tmp_path, monkeypatch):
    """A personal layer that exists but cannot be read is refused, never rewritten
    from empty: Python 3.14's `Path.is_file` reads a stat fault as False, which
    would drop the user's other keys."""
    path = project_v7.project / ticketstore.USER_CONFIG_REL
    before = path.read_bytes()
    real_is_file, real_read_bytes = Path.is_file, Path.read_bytes

    def is_file(self, *args, **kwargs):
        return False if self == path else real_is_file(self, *args, **kwargs)

    def read_bytes(self):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(ticketstore.TicketStoreError, match="Permission denied"):
        ticketstore.redirect_mount_store(project_v7.project, tmp_path / "store")
    monkeypatch.undo()
    assert path.read_bytes() == before
