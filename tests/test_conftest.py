"""Contract tests for the shared fixtures and host-capability gates in
`tests/conftest.py`.

`project` hands every test a copytree clone of a session-scoped template repo, so
the template's shape is a shared dependency of most of the suite. What is pinned
here is the part of that shape other modules rely on without asserting it.

The gates need the same treatment for a sharper reason: `opencode_runs` decides
whether an entire `*_live.py` module runs or skips, and nothing downstream can
notice when it answers wrongly — a gate that wrongly says "absent" reports a
tidy skip, not a failure. Its call shape and each of its three refusals are
therefore pinned here, one fact per row.
"""

from __future__ import annotations

import ast
import errno
import functools
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
import warnings
from dataclasses import fields, replace
from pathlib import Path

import conftest
import pytest
from conftest import make_git_noisy

from bmad_loop import bmadconfig, envvars, verify


def test_template_drops_sample_hooks_but_keeps_hooks_dir_and_exclude(project):
    """`git init` seeds 14 dead `*.sample` hooks that nothing reads; the template
    deletes the files so each per-test copy stops replicating them.

    Both halves are load-bearing, and they pull against each other: the obvious
    shortcut for the first (`git init --template=` pointed at an empty dir) also
    removes `.git/hooks/` itself and `.git/info/exclude`, which the suite does
    depend on — three sites write `.git/hooks/pre-commit` with no `mkdir`
    (tests/test_engine.py twice, tests/test_verify.py once) and
    tests/test_install.py reads `.git/info/exclude`. Deleting the sample files
    is therefore the only cleanup that satisfies both.

    Ablation target: delete the `sample.unlink()` loop from `_project_template`
    and the first assertion fails alone; swap that loop for an empty
    `git init --template=` and the first assertion passes while the two
    survival assertions fail instead — disjoint failures, which is what proves
    the two halves are independent rather than one implying the other."""
    git_dir = project.project / ".git"
    hooks = git_dir / "hooks"

    assert list(hooks.glob("*.sample")) == []
    assert hooks.is_dir()
    assert (git_dir / "info" / "exclude").is_file()


def test_state_roots_are_independent(_state_root_allocator, tmp_path):
    """Every root is a distinct, freshly created, empty directory, and a write into one
    is invisible in any other — including this test's own autouse root.

    Ablation target: make `StateRootAllocator.allocate` hand back one fixed child (an
    `exist_ok=True` mkdir of a constant name) and the distinctness and cross-write
    assertions fail; point `_isolate_state_root` back at `tmp_path_factory.mktemp` and
    the own-root assertion fails, because that root no longer lives under the
    worker's allocator."""
    own = Path(os.environ[envvars.STATE_DIR])
    assert own.is_dir()
    assert own.is_relative_to(
        _state_root_allocator.base
    ), "the autouse state root is not drawn from the per-worker allocator"

    allocator = conftest.StateRootAllocator(tmp_path / "base")
    allocator.base.mkdir()
    first, second = allocator.allocate(), allocator.allocate()
    live = _state_root_allocator.allocate()
    roots = [own, first, second, live]
    assert len({r.resolve() for r in roots}) == len(roots)
    for root in (first, second, live):
        assert root.is_dir()
        assert list(root.iterdir()) == []

    (first / "runs" / "RID").mkdir(parents=True)
    (first / "runs" / "RID" / "state.json").write_text("first\n", encoding="utf-8")
    (second / "marker").write_text("second\n", encoding="utf-8")

    assert sorted(p.relative_to(first).as_posix() for p in first.rglob("*")) == [
        "runs",
        "runs/RID",
        "runs/RID/state.json",
    ]
    assert [p.name for p in second.iterdir()] == ["marker"]
    assert (second / "marker").read_text(encoding="utf-8") == "second\n"
    assert list(live.iterdir()) == []
    assert not (own / "marker").exists()
    assert not (own / "runs" / "RID").exists()


def test_state_allocation_does_not_rescan_prior_roots(tmp_path, tmp_path_factory):
    """Allocation cost is a constant number of directory operations: no listing of the
    roots already handed out, one `mkdir` per root plus one per bucket, and no
    directory holding more than one bucket's worth of entries.

    Counted, not timed: a wall-clock threshold would be both flaky and blind on a fast
    disk. The control half proves the counter can see a scan at all — pytest's own
    numbered `mktemp`, the allocation this replaced, lists its parent every call.

    Ablation target: derive `allocate`'s index from `len(list(self.base.iterdir()))`
    (or any other listing) and the zero-listing assertion fails; drop the bucketing and
    the fanout assertion does."""
    allocator = conftest.StateRootAllocator(tmp_path / "base")
    allocator.base.mkdir()
    size = conftest.StateRootAllocator.BUCKET_SIZE
    count = 3 * size + 5
    buckets = -(-count // size)

    listings: list[str] = []
    mkdirs: list[Path] = []
    real_scandir, real_listdir, real_mkdir = os.scandir, os.listdir, Path.mkdir

    def scandir(path=".", *args, **kwargs):
        listings.append(str(path))
        return real_scandir(path, *args, **kwargs)

    def listdir(path=".", *args, **kwargs):
        listings.append(str(path))
        return real_listdir(path, *args, **kwargs)

    def mkdir(self, *args, **kwargs):
        mkdirs.append(self)
        return real_mkdir(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "scandir", scandir)
        mp.setattr(os, "listdir", listdir)
        mp.setattr(Path, "mkdir", mkdir)
        roots = [allocator.allocate() for _ in range(count)]
        allocation_listings, allocation_mkdirs = list(listings), len(mkdirs)
        tmp_path_factory.mktemp("rescan-control")
        control_listings = len(listings) - len(allocation_listings)

    assert control_listings >= 1, "the listing counter cannot see pytest's own scan"
    assert allocation_listings == []
    assert allocation_mkdirs == count + buckets
    assert len(set(roots)) == count
    assert all(root.is_dir() for root in roots)
    assert len(list(allocator.base.iterdir())) == buckets
    assert max(len(list(b.iterdir())) for b in allocator.base.iterdir()) == size


@pytest.mark.parametrize("operator", ["operator-state-root", None], ids=["set", "unset"])
def test_state_root_override_is_restored(tmp_path, operator):
    """`point_state_root` — the whole body of the autouse fixture — changes exactly one
    variable, and only through the monkeypatch it is handed, so undoing that monkeypatch
    restores the operator's own value or its absence. HOME and every other variable
    are left as they were.

    Driven with its own MonkeyPatch pair rather than the test's `monkeypatch`: the
    restoration happens at teardown, where no assertion in the test body can see it.

    Ablation target: write `os.environ[envvars.STATE_DIR]` directly in
    `point_state_root` and both rows fail on the restored value."""
    allocator = conftest.StateRootAllocator(tmp_path / "base")
    allocator.base.mkdir()
    with pytest.MonkeyPatch.context() as operator_env:
        if operator is None:
            operator_env.delenv(envvars.STATE_DIR, raising=False)
        else:
            operator_env.setenv(envvars.STATE_DIR, operator)
        before = dict(os.environ)

        fixture_mp = pytest.MonkeyPatch()
        try:
            root = conftest.point_state_root(fixture_mp, allocator)
            during = dict(os.environ)
        finally:
            fixture_mp.undo()
        after = dict(os.environ)

    changed = {k for k in before.keys() | during.keys() if before.get(k) != during.get(k)}
    assert changed == {envvars.STATE_DIR}
    assert during[envvars.STATE_DIR] == str(root)
    assert root.is_dir()
    assert after.get(envvars.STATE_DIR) == operator
    assert after == before


def _project_shape(paths: bmadconfig.ProjectPaths) -> dict[str, str]:
    root = paths.project.resolve()
    return {
        f.name: getattr(paths, f.name).resolve().relative_to(root).as_posix() for f in fields(paths)
    }


def _files_outside_git(root: Path) -> list[tuple[str, bytes | None]]:
    return sorted(
        (p.relative_to(root).as_posix(), p.read_bytes() if p.is_file() else None)
        for p in root.rglob("*")
        if ".git" not in p.relative_to(root).parts
    )


def test_project_tree_matches_project_paths_without_git(project_tree, _project_template, tmp_path):
    """`project_tree` is `project` minus the repository: same `ProjectPaths` shape, same
    sandbox name, byte-identical working files — and no `.git` at all.

    Compared against a fresh copy of the real template rather than the `project`
    fixture, because both fixtures claim `tmp_path / "sandbox"` and cannot coexist.

    Ablation target: seed an extra file in `_project_template` only (after the shared
    `seed_project_files` call) and the file comparison fails — the two fixtures would
    describe different projects."""
    real = conftest.copy_project(_project_template, tmp_path / "real" / "sandbox")

    assert project_tree.project == tmp_path / "sandbox"
    assert project_tree.project.name == real.project.name
    assert _project_shape(project_tree) == _project_shape(real)
    assert project_tree.implementation_artifacts.is_dir()
    assert project_tree.planning_artifacts.is_dir()
    assert _files_outside_git(project_tree.project) == _files_outside_git(real.project)
    assert (real.project / ".git").is_dir()
    assert not (project_tree.project / ".git").exists()


def test_project_copies_keep_independent_index_refs_and_hooks(project, _project_template, tmp_path):
    """Two sandbox copies share no Git state: a commit, a new branch, a staged change
    and an installed hook in one leave the other copy and the template untouched.

    Ablation target: make `copy_project` link instead of copy
    (`shutil.copytree(..., copy_function=os.link)`) and the same-file assertions fail
    before any mutation is even attempted."""
    other = conftest.copy_project(_project_template, tmp_path / "other" / "sandbox")
    a_git, b_git, t_git = (p / ".git" for p in (project.project, other.project, _project_template))
    assert a_git.is_dir() and b_git.is_dir(), "a copy's .git must be its own directory"
    for rel in ("index", "HEAD", "config", "refs/heads/main"):
        for x, y in ((a_git, b_git), (a_git, t_git), (b_git, t_git)):
            assert not os.path.samefile(x / rel, y / rel), f"{rel} is shared: {x} / {y}"
    head = conftest.git(_project_template, "rev-parse", "HEAD")

    (project.project / "src.txt").write_text("changed\n", encoding="utf-8")
    conftest.git(project.project, "add", "src.txt")
    conftest.git(project.project, "commit", "-q", "-m", "mutate a")
    conftest.git(project.project, "branch", "side")
    (project.project / "staged.txt").write_text("staged\n", encoding="utf-8")
    conftest.git(project.project, "add", "staged.txt")
    (a_git / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    assert conftest.git(project.project, "rev-parse", "HEAD") != head

    for repo in (other.project, _project_template):
        assert conftest.git(repo, "rev-parse", "HEAD") == head
        assert conftest.git(repo, "branch", "--list", "side") == ""
        assert conftest.git(repo, "status", "--porcelain") == ""
        assert conftest.git(repo, "ls-files") == ".gitignore\nsrc.txt"
        assert not (repo / ".git" / "hooks" / "pre-commit").exists()
        assert (repo / "src.txt").read_text(encoding="utf-8") == "original\n"


def test_plant_root_markers_refuses_physical_aliases(tmp_path):
    """Different spellings of one directory do not make a divergent-roots probe."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_alias = tmp_path / "project-alias"
    try:
        project_alias.symlink_to(repo_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(AssertionError, match="DIFFERENT roots"):
        conftest.plant_root_markers(repo_root=repo_root, project=project_alias)


@pytest.mark.parametrize(
    ("stale_root", "marker"),
    [
        ("project", conftest.MARKER_IN_REPO_ROOT),
        ("repo_root", conftest.MARKER_IN_PROJECT),
    ],
)
def test_plant_root_markers_refuses_opposite_root_residue(tmp_path, stale_root, marker):
    """A stale opposite-root marker cannot turn the cwd probe into a false green."""
    repo_root = tmp_path / "repo"
    project = tmp_path / "project"
    repo_root.mkdir()
    project.mkdir()
    {"repo_root": repo_root, "project": project}[stale_root].joinpath(marker).write_text("stale\n")

    with pytest.raises(AssertionError, match="stale"):
        conftest.plant_root_markers(repo_root=repo_root, project=project)


def test_write_repo_root_override_creates_the_config_tree(project, tmp_path):
    """The standalone writer does not depend on another fixture running first."""
    config = project.project / conftest.BMAD_CONFIG_REL
    assert not config.parent.exists()
    code_root = tmp_path / "code-root"
    code_root.mkdir()

    conftest.write_repo_root_override(project, code_root)

    assert config.is_file()
    assert bmadconfig.load_paths(project.project).repo_root == code_root.resolve()


def test_write_repo_root_override_quotes_yaml_punctuation(project, tmp_path):
    """YAML punctuation and non-BMP Unicode survive the config round trip."""
    config = project.project / conftest.BMAD_CONFIG_REL
    config.parent.mkdir(parents=True)
    code_root = tmp_path / "code'root-😀"
    code_root.mkdir()

    conftest.write_repo_root_override(project, code_root)

    assert bmadconfig.load_paths(project.project).repo_root == code_root.resolve()


def test_write_repo_root_override_refuses_a_relative_code_root(project):
    """A relative override cannot acquire process-cwd semantics by accident."""
    with pytest.raises(AssertionError, match="absolute code_root"):
        conftest.write_repo_root_override(project, conftest.Path("relative-code-root"))

    assert not (project.project / conftest.BMAD_CONFIG_REL).exists()


def test_scripted_verify_runner_refuses_the_wrong_canonical_cwd(tmp_path):
    """A cwd-discarding command double cannot hide a caller-root regression.

    Ablation: delete the cwd equality assertion in `scripted_verify_runner` and
    this row fails because the wrong-root call no longer raises.
    """
    expected = tmp_path / "expected"
    wrong = tmp_path / "wrong"
    expected.mkdir()
    wrong.mkdir()
    runner = conftest.scripted_verify_runner(expected, lambda: ["scripted"])

    with pytest.raises(AssertionError, match="wrong root"):
        runner(None, wrong)

    assert runner(None, expected / ".." / expected.name) == ["scripted"]


def test_nested_repo_root_paths_round_trips_committed_config_and_conflict(project):
    """The nested fixture is a production-loadable config, not a hand-built snapshot,
    and it is the layout worktree isolation SUPPORTS (DW-379): the project lies inside
    `repo_root`, so neither isolation mode is refused.

    Ablation: widen `worktree_isolation_conflict` back to "any `repo_root` override"
    and this row fails because the nested loaded config is refused again.
    """
    paths = conftest.nested_repo_root_paths(project)

    assert bmadconfig.load_paths(paths.project) == paths
    assert paths.project == paths.project.resolve()
    assert paths.repo_root == paths.repo_root.resolve()
    assert paths.project.parent == paths.repo_root
    config_rel = (paths.project / conftest.BMAD_CONFIG_REL).relative_to(paths.repo_root)
    assert conftest.git(paths.repo_root, "ls-files", "--error-unmatch", config_rel.as_posix())
    assert paths.repo_root != paths.project, "premise: the roots really diverge"
    assert bmadconfig.worktree_isolation_conflict(paths, "none") is None
    assert bmadconfig.worktree_isolation_conflict(paths, "worktree") is None


def test_nested_repo_root_paths_canonicalizes_a_dotdot_input(project):
    """A portable alias spelling cannot collapse pathspecs through mixed roots."""
    alias = project.project / ".." / project.project.name
    assert alias != alias.resolve()
    assert alias.resolve() == project.project.resolve()
    aliased = replace(
        project,
        project=alias,
        implementation_artifacts=alias / "_bmad-output" / "implementation-artifacts",
        planning_artifacts=alias / "_bmad-output" / "planning-artifacts",
        output_folder=alias / "_bmad-output",
        repo_root=alias,
    )

    paths = conftest.nested_repo_root_paths(aliased)

    assert all(
        path == path.resolve()
        for path in (
            paths.project,
            paths.implementation_artifacts,
            paths.planning_artifacts,
            paths.output_folder,
            paths.repo_root,
        )
    )
    assert paths.project.parent == paths.repo_root == project.project.resolve()
    spec = paths.implementation_artifacts / "spec-1-1-a.md"
    assert verify.verify_dev_exclude_relpaths(paths, spec, root=paths.repo_root)
    assert verify._stories_relpaths(paths.repo_root, paths.planning_artifacts / "epic-a")


def test_nested_repo_root_paths_seeds_the_complete_init_ignore_shape(project):
    """Nested generated state cannot become proof-of-work residue."""
    paths = conftest.nested_repo_root_paths(project)
    expected = [
        ".bmad-loop/runs/",
        ".bmad-loop/cache/",
        ".bmad-loop/policy.toml",
        "_bmad/render/",
    ]
    assert (paths.project / ".gitignore").read_text(encoding="utf-8").splitlines() == expected

    candidates = [
        ".bmad-loop/runs/run/state.json",
        ".bmad-loop/cache/plugin/cache.bin",
        ".bmad-loop/policy.toml",
        "_bmad/render/skill/workflow.md",
    ]
    for rel in candidates:
        path = paths.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n", encoding="utf-8")
    ignored = conftest.git(paths.repo_root, "check-ignore", *[f"app/{rel}" for rel in candidates])
    assert ignored.splitlines() == [f"app/{rel}" for rel in candidates]


def test_nested_repo_root_paths_refuses_a_nonempty_index(project):
    """Its seed commit must never absorb setup another fixture already staged."""
    staged = project.project / "staged.txt"
    staged.write_text("belongs to the caller\n", encoding="utf-8")
    conftest.git(project.project, "add", staged.name)

    with pytest.raises(AssertionError, match="empty index"):
        conftest.nested_repo_root_paths(project)

    assert not (project.project / conftest.NESTED_SUBDIR).exists()


def test_nested_repo_root_paths_refuses_already_divergent_input(project, tmp_path):
    """The builder owns divergence and leaves pre-diverged input untouched."""
    paths = replace(project, repo_root=tmp_path / "other-root")

    with pytest.raises(AssertionError, match="already have one"):
        conftest.nested_repo_root_paths(paths)

    assert not (project.project / conftest.NESTED_SUBDIR).exists()


def test_nested_repo_root_paths_refuses_an_existing_nested_project(project):
    """The builder never overwrites a caller-owned `app/` directory."""
    nested = project.project / conftest.NESTED_SUBDIR
    nested.mkdir()
    sentinel = nested / "caller-owned.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="already exists"):
        conftest.nested_repo_root_paths(project)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not (nested / "src.txt").exists()
    assert not (nested / ".gitignore").exists()


def test_nested_repo_root_paths_refuses_a_dangling_nested_symlink(project):
    """A dangling `app` alias hits the helper precondition before any writes."""
    nested = project.project / conftest.NESTED_SUBDIR
    missing = project.project / "missing-app-target"
    try:
        nested.symlink_to(missing, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    assert nested.is_symlink() and not nested.exists()
    status_before = conftest.git(project.project, "status", "--porcelain")

    with pytest.raises(AssertionError, match="already exists or is a symlink"):
        conftest.nested_repo_root_paths(project)

    assert nested.is_symlink() and not nested.exists()
    assert not missing.exists()
    assert conftest.git(project.project, "status", "--porcelain") == status_before


def test_seed_outer_decoy_ledger_names_the_unprefixed_spelling_in_the_code_tree(project):
    """The decoy IS the file a `project`-rooted pathspec names in the code tree.

    Pins the VALUE under the real artifact layout: the same un-prefixed spelling the
    consumer rows assert `_harvest_gate_exclude` must NOT produce. That it is derived
    rather than re-spelled is the row below.
    """
    paths = conftest.nested_repo_root_paths(project)
    head_before = verify.rev_parse_head(paths.repo_root)

    decoy, decoy_bytes = conftest.seed_outer_decoy_ledger(paths)

    assert decoy == paths.repo_root / "_bmad-output/implementation-artifacts/deferred-work.md"
    assert decoy.resolve() != paths.deferred_work.resolve()
    assert decoy.read_bytes() == decoy_bytes == conftest.OUTER_DECOY_LEDGER
    # SEEDS ONLY, pinned so BOTH ways of breaking it redden. A cached-diff check alone
    # is satisfied by a helper that stages AND commits, which leaves the index clean
    # again — so pin the decoy UNTRACKED and pin that HEAD never moved across the call.
    assert conftest.git(paths.repo_root, "diff", "--cached", "--name-only") == ""
    with pytest.raises(subprocess.CalledProcessError):
        conftest.git(paths.repo_root, "ls-files", "--error-unmatch", decoy.as_posix())
    assert verify.rev_parse_head(paths.repo_root) == head_before


def test_seed_outer_decoy_ledger_follows_a_relocated_artifact_tail(project):
    """The decoy is DERIVED from `paths.deferred_work`, not re-spelled from a literal.

    Move the artifact tail somewhere else inside `project` and the decoy has to move
    with it — that is what keeps the helper locked to
    `engine._harvest_gate_exclude`'s rule if the layout ever changes.

    Ablation: replace the helper's derivation with the literal
    ``repo_root / "_bmad-output" / "implementation-artifacts" / "deferred-work.md"``
    and this row reddens while every other helper and consumer row stays green — it
    is the only row that separates the derivation from today's layout.
    """
    paths = conftest.nested_repo_root_paths(project)
    relocated = replace(paths, implementation_artifacts=paths.project / "artifacts" / "impl")
    assert relocated.deferred_work.is_relative_to(relocated.project)

    decoy, decoy_bytes = conftest.seed_outer_decoy_ledger(relocated)

    assert decoy == paths.repo_root / "artifacts" / "impl" / "deferred-work.md"
    assert decoy.read_bytes() == decoy_bytes


def test_seed_outer_decoy_ledger_refuses_collapsed_roots(project):
    """Collapsed, the "decoy" would BE the ledger the consumer rows exclude.

    Seeded with sentinel bytes first, so the row grades that the real ledger SURVIVES
    rather than that an absent one stayed absent — a bare `not exists()` would pass
    for every reason the file could be missing, the sandbox template's shape included.
    """
    sentinel = b"# the project's own ledger\n"
    project.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    project.deferred_work.write_bytes(sentinel)

    with pytest.raises(AssertionError, match="NESTED shape"):
        conftest.seed_outer_decoy_ledger(project)

    assert project.deferred_work.read_bytes() == sentinel


def test_seed_outer_decoy_ledger_refuses_disjoint_roots(project, tmp_path):
    """The sibling shape has no outer ledger to name.

    Nothing raises on its own: the tail resolves independently of `repo_root`, so an
    un-guarded helper would seed a decoy into a tree with no outer project at all.
    `other_root` staying absent is what shows the guard fired before any write.
    """
    other_root = tmp_path / "other-root"
    paths = replace(project, repo_root=other_root)

    with pytest.raises(AssertionError, match="NESTED shape"):
        conftest.seed_outer_decoy_ledger(paths)

    assert not other_root.exists()
    assert not project.deferred_work.exists()


def test_seed_outer_decoy_ledger_refuses_an_inherited_decoy(project):
    """An inherited outer ledger would make the "graded by value" premise an accident."""
    paths = conftest.nested_repo_root_paths(project)
    decoy = paths.repo_root / paths.deferred_work.relative_to(paths.project)
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_bytes(b"belongs to the caller\n")

    with pytest.raises(AssertionError, match="graded by value"):
        conftest.seed_outer_decoy_ledger(paths)

    assert decoy.read_bytes() == b"belongs to the caller\n"


def test_refuse_to_resolve_defaults_to_the_unc_refusal_and_stays_scoped(project, monkeypatch):
    """`refuse_to_resolve`'s DEFAULT arm, which ~43 call sites take without saying so.

    Those sites pass no `error=` and depend on the exact fault: WinError 64 carrying
    `UNRESOLVABLE`, the registered-but-not-serving UNC provider's answer (#529/#536/#552).
    A regression in the default would surface as a confusing failure in whichever
    unrelated module ran first, so the contract is pinned at the seam instead.

    Scope is half the contract: a blanket stub would break every unrelated resolve in the
    process, and a row asserting "the command survived" would then pass for a reason that
    has nothing to do with the guard under test.
    """
    target = project.deferred_work
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ledger\n", encoding="utf-8")
    sibling = target.parent / "untouched.md"
    sibling.write_text("sibling\n", encoding="utf-8")
    conftest.refuse_to_resolve(monkeypatch, target)

    with pytest.raises(OSError) as caught:
        target.resolve()

    assert conftest.UNRESOLVABLE in str(caught.value)
    if sys.platform == "win32":
        # The 4th constructor arg is surfaced only there — and it is authoritative:
        # CPython DERIVES `errno` from a supplied `winerror` (`winerror_to_errno`, whose
        # fallback for a code it has no mapping for — ERROR_NETNAME_DELETED among them —
        # is EINVAL), so the `0` placeholder the helper passes never reaches the
        # exception on Windows.
        assert caught.value.winerror == 64
        assert caught.value.errno == errno.EINVAL
    else:
        assert caught.value.errno == 0
    assert sibling.resolve() == Path(os.path.realpath(sibling))  # unnamed paths still resolve


def test_refuse_to_resolve_raises_the_caller_supplied_class(project, monkeypatch):
    """The `error=` keyword (DW-195): a DIFFERENT resolve fault, same scoping.

    It exists so a caller grading a handler that catches several exception classes can
    drive each class on its own — a resolve really can fail as `RuntimeError` (a pre-3.13
    symlink loop) rather than `OSError`, and a joint ablation passes while an untested
    class hides behind a tested one. A fresh instance per raise, so a consumer resolving
    the same target twice does not accumulate `__traceback__`/`__context__` on one object.
    """
    target = project.deferred_work
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ledger\n", encoding="utf-8")
    sibling = target.parent / "untouched.md"
    sibling.write_text("sibling\n", encoding="utf-8")
    conftest.refuse_to_resolve(monkeypatch, target, error=RuntimeError("Symlink loop from 'x'"))

    with pytest.raises(RuntimeError, match="Symlink loop from 'x'") as first:
        target.resolve()
    with pytest.raises(RuntimeError, match="Symlink loop from 'x'") as second:
        target.resolve()

    assert first.value is not second.value  # fresh per raise, so no traceback accumulation
    assert second.value.__context__ is None
    assert sibling.resolve() == Path(os.path.realpath(sibling))  # still scoped to `target`


def test_template_leaves_no_detached_git_maintenance_writing_into_the_copies(project, tmp_path):
    """No background git process may outlive a commit into the sandbox.

    `git commit` normally ends by spawning `git maintenance run --auto --quiet
    --detach`. Detached, it outlives the command that started it and keeps writing
    under `.git/objects/` — and the template it writes into is exactly what
    `project` copytrees for every test. `objects/maintenance.lock` gets listed by
    scandir, unlinked by that child, then opened by copy2 and is already gone, so
    one arbitrary unrelated test dies at fixture setup on `[Errno 2]`. It reddens a
    different test each time and only on whichever interpreter leg loses the race,
    which is the flake signature this suite treats as a bug.

    Graded on the behavior, not on the config key: reading back
    `maintenance.auto` would pass on a git that had stopped honouring it. This
    commits into a real copy under GIT_TRACE2 and pins the child list instead.

    Ablation target: delete the `maintenance.auto` line from `_project_template`
    and this row fails alone, naming the spawned `git maintenance run` in the
    assertion message. The trace-recorded-the-commit assertion is the anti-vacuity
    guard: `spawned` reads empty both when no child ran and when the trace parsed
    into nothing we recognize, so a trace2 event or field rename would otherwise
    leave this row green for having observed nothing. It does not guard an absent
    trace — a git without trace2, or a mistyped env var, writes no file at all and
    the read below raises `FileNotFoundError`, which is loud on its own."""
    repo = project.project
    (repo / "src.txt").write_text("changed\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)

    trace = tmp_path / "trace2.json"
    # `GIT_CONFIG_COUNT=0` drops any inherited command-scope `GIT_CONFIG_KEY_n`
    # pair, which outranks `.git/config` exactly as `git -c` does. Measured: an
    # ambient `maintenance.auto=true` re-arms the spawn straight through the
    # fixture's own `false` and reddens this row, and an ambient `false` would
    # hold it green with the fixture line ablated — the vacuity this row exists
    # to refuse. Scoped to this one probe, not to the suite-wide env fixtures,
    # which shadow only the variables they must on purpose.
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "second"],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_TRACE2_EVENT": str(trace), "GIT_CONFIG_COUNT": "0"},
    )

    events = []
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:  # trace2 writes one JSON object per line; skip any partial
            continue

    # Anti-vacuity: the trace really did observe this commit.
    assert any(
        e.get("event") == "cmd_name" and e.get("name") == "commit" for e in events
    ), f"GIT_TRACE2 recorded no commit; nothing was actually observed: {events}"

    spawned = [" ".join(e.get("argv") or []) for e in events if e.get("event") == "child_start"]
    assert not [c for c in spawned if "maintenance" in c or "gc" in c], spawned


def test_make_git_noisy_produces_rc_zero_stderr(project):
    """The anti-vacuity guard for the suite's only host-noise dimension (#442).

    `make_git_noisy` is what makes the merged and the stdout-alone reads
    distinguishable; its whole premise is that an unknown VALUE for a known config
    KEY is a `warning:` on stderr at rc 0, not an error. If a future git ever stops
    emitting it, THIS test fails loudly — instead of the four tests that depend on
    the helper all passing for the wrong reason, with the bug restored. Do not
    delete it as a duplicate of them: it is the only row that would notice.

    Ablation target: delete the `git config` line from `make_git_noisy` and the
    premise is dead — the helper's own probe catches it, so this row and the four
    that depend on the helper all report SKIPPED, none PASSED. Delete the probe as
    well, so nothing masks the dead premise, and this row FAILS on the stderr
    assertion. Deleting the probe ALONE reddens nothing while the host git still
    warns, and that is the point rather than a gap: the probe is what turns a future
    silent git into four skips instead of four false greens, and this row into the
    one loud failure."""
    repo = project.project
    make_git_noisy(repo)

    proc = verify._run_git(["git", "-C", str(repo), "rev-parse", "HEAD"], repo)

    assert proc.returncode == 0  # a warning, not a failure
    assert proc.stderr.strip()  # git really did write to stderr
    sha = proc.stdout.strip()
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_opencode_gate_probes_the_resolved_binary(monkeypatch):
    """The one row that owns the probe's call shape.

    `opencode_runs` decides whether `tests/test_opencode_live.py` runs at all,
    and the shape of this single call is what makes that decision mean
    anything: the resolved path rather than the bare name (`which` already
    answered that question), a bounded `timeout` so a wedged shim cannot hang
    collection, `check=False` so a nonzero exit arrives as data instead of an
    exception the caller never asked to handle, and `stdin=DEVNULL` so a shim
    that prompts is refused immediately instead of stalling for the full
    timeout on the runner's inherited tty.

    The `kwargs` assertions are deliberately a SUBSET, not a dict equality:
    equality would make deleting `timeout=10` redden this row and both refusal
    rows at once, grading none of them. Each fact is graded here and only here,
    and an additive kwarg stays free.

    Ablation target: delete `timeout=10` from the `subprocess.run` call and this
    test fails alone, on `KeyError: 'timeout'`; delete `stdin=subprocess.DEVNULL`
    and it fails alone the same way. Neither mutation is visible to any other
    row in this file."""
    calls = []

    def probe(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode=0)

    monkeypatch.setattr(conftest.sys, "platform", "linux")
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(conftest.subprocess, "run", probe)

    assert conftest.opencode_runs()

    ((command, kwargs),) = calls
    assert command == ["/usr/bin/opencode", "--version"]
    assert kwargs["timeout"] == 10
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_opencode_gate_refuses_a_binary_that_exits_nonzero(monkeypatch):
    """#294 itself: the dead shim `shutil.which` resolves without complaint.

    A stale WSL interop stub, or an npm wrapper whose target was uninstalled,
    still occupies a PATH entry and still answers `--version` — nonzero. Before
    the probe the live module read that as an install and ran the entire smoke
    against something that could never serve a session.

    Ablation target: replace `return probe.returncode == 0` with `return True`
    and this test fails alone, on the leading `not` — the call-shape row still
    sees its one correctly-shaped call, and both launch-fault parameters still
    return False out of the `except` without reaching the changed line."""
    calls = []

    def failed_probe(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode=2)

    monkeypatch.setattr(conftest.sys, "platform", "linux")
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(conftest.subprocess, "run", failed_probe)

    assert not conftest.opencode_runs()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error",
    [OSError("broken shim"), subprocess.TimeoutExpired("opencode", timeout=10)],
    ids=["launch-fault", "timeout"],
)
def test_opencode_gate_refuses_a_binary_that_cannot_be_launched(monkeypatch, error):
    """The two ways a resolved path fails before it can exit at all: the exec
    faults (`OSError` — a shim naming a deleted interpreter, a dropped mount),
    or it never returns inside the bound (`TimeoutExpired`). Both are host-shaped
    absence rather than a suite defect, so both have to become a skip — an
    exception here escapes at module import of the live suite, where it is an
    error, not a skip.

    Ablation target: delete the `except (OSError, subprocess.SubprocessError):
    return False` and this test fails alone, in BOTH parameters, on the escaped
    exception. `TimeoutExpired` is what proves the `SubprocessError` half of the
    tuple is load-bearing: it is not an `OSError`, so an `except OSError` alone
    reddens that parameter and only that one."""
    calls = []

    def raise_error(command, **kwargs):
        calls.append((command, kwargs))
        raise error

    monkeypatch.setattr(conftest.sys, "platform", "linux")
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(conftest.subprocess, "run", raise_error)

    assert not conftest.opencode_runs()
    assert len(calls) == 1


def test_opencode_gate_answers_win32_without_touching_the_host(monkeypatch):
    """The win32 early-out, which nothing else in the suite grades.

    opencode-on-Windows is unverified for this adapter (README adapter table),
    so the answer there is False by policy — and it has to be reached before the
    PATH lookup and before the probe, because Windows CI should pay for
    neither. Poisoning both `shutil.which` and `subprocess.run` is how the
    ordering is asserted rather than just the return value: either one being
    reached is an `AssertionError`.

    Ablation target: delete the `if sys.platform == "win32": return False` early
    return and this test fails alone, on the `AssertionError` the poisoned
    `shutil.which` raises. The other three rows all pin `platform` to "linux" to
    stay host-independent, so they stay GREEN under that same mutation — which
    is the whole reason this row exists: without it, deleting the early return
    leaves this file, and the suite, entirely green."""

    def refuse(*args, **kwargs):
        raise AssertionError("win32 must answer before any PATH lookup or probe")

    monkeypatch.setattr(conftest.sys, "platform", "win32")
    monkeypatch.setattr(conftest.shutil, "which", refuse)
    monkeypatch.setattr(conftest.subprocess, "run", refuse)

    assert not conftest.opencode_runs()


# --- DW-95: the real-tmux xdist grouping guard --------------------------------
#
# Real-tmux E2Es are the suite's only wall-clock-sensitive tests, and the fix for
# DW-95 is a pair of declarations that are BOTH silently inert on their own: the
# `xdist_group` mark does nothing under xdist's default `load` scheduler, and
# `--dist loadgroup` groups nothing if a module forgets the mark. Neither omission
# raises — the suite just goes back to letting these tests starve each other under
# load, which is how the 2026-09-02 py3.13 CI leg failed. So both halves are
# asserted here, next to the conftest constants they read.

REAL_MUX_MARK_ALIAS = "real_mux_e2e"
_TESTS_DIR = Path(__file__).resolve().parent
_PYPROJECT = _TESTS_DIR.parent / "pyproject.toml"


def _dotted(node: ast.expr) -> str | None:
    """Dotted spelling of an attribute/name expression (``pytest.mark.skipif``)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_inline_tmux_skipif(mark: ast.expr) -> bool:
    """Whether ``mark`` is an INLINE ``pytest.mark.skipif(...)`` naming tmux.

    Keys on the reason STRING rather than on the `HAVE_TMUX` name: the condition is
    evaluated at decoration time and reaches the AST as a bare boolean expression,
    so the reason is the only durable discriminator. It matches both real-tmux
    modules ("tmux not available", "stories E2E needs real tmux on Linux") and
    correctly misses `test_psmux_live.py` ("requires Windows with psmux on PATH" —
    no "tmux" substring) and `test_opencode_live.py`.

    KNOWN BLIND SPOT, deliberately not closed: the reason must be a string LITERAL.
    A reason built from an f-string or read out of a module constant is invisible
    here, and such a gate would slip past the guard ungrouped — resolving arbitrary
    expressions means evaluating them, which a static scan will not do. A probe row
    below pins that gap so it is a recorded limit rather than a surprise. Keep new
    gates' reasons literal.
    """
    if not isinstance(mark, ast.Call):
        return False
    head = _dotted(mark.func)
    if head is None or head.rsplit(".", 1)[-1] != "skipif":
        return False
    for kw in mark.keywords:
        if kw.arg != "reason":
            continue
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return "tmux" in kw.value.value.lower()
    return False


def _conftest_aliases(tree: ast.Module, wanted: frozenset[str]) -> set[str]:
    """Local spellings this module bound for the named `tests/conftest.py` objects.

    Both import forms, because the suite uses both: `from conftest import X` (with or
    without `as`) binds a bare name, `import conftest` binds the dotted access.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "conftest":
            aliases.update(a.asname or a.name for a in node.names if a.name in wanted)
        elif isinstance(node, ast.Import):
            aliases.update(
                f"{a.asname or a.name}.{name}"
                for a in node.names
                if a.name == "conftest"
                for name in wanted
            )
    return aliases


def _local_gate_bindings(tree: ast.Module) -> set[str]:
    """Module-level ``NAME = pytest.mark.skipif(..., reason=<names tmux>)`` bindings.

    A gate does not have to be written inline on the test. `tests/conftest.py` already
    ships `needs_strict_codec` in exactly this shape for three modules to import, and
    DW-95 adds the same idiom for the group mark — so a tmux gate bound to a name and
    applied as a bare decorator is an idiom the suite actively uses, not a hypothetical.
    """
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if _is_inline_tmux_skipif(value):
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


@functools.cache
def _conftest_gate_names() -> frozenset[str]:
    """Names in `tests/conftest.py` bound to a tmux `skipif` (empty today, by design)."""
    source = (_TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
    return frozenset(_local_gate_bindings(ast.parse(source, filename="conftest.py")))


def _xdist_group_names(mark: ast.expr, aliases: set[str]) -> set[str]:
    """Every group name ``mark`` puts its test in; empty if it is not a group mark.

    Returns the SET rather than a "matches?" boolean because xdist joins the names of
    every group mark on an item into one composite group
    (`xdist/remote.py`: ``item._nodeid = f"{item.nodeid}@{'_'.join(sorted(gnames))}"``).
    A test carrying `real_mux_e2e` AND a second `xdist_group` therefore lands in a
    group that is not the shared one, and a caller asking only "does ANY mark match?"
    would wave it through.
    """
    if isinstance(mark, ast.Call):
        head = _dotted(mark.func)
        if head is None or head.rsplit(".", 1)[-1] != "xdist_group":
            return set()
        nodes = [*mark.args, *(k.value for k in mark.keywords if k.arg == "name")]
        return {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    if _dotted(mark) in aliases:
        return {conftest.REAL_MUX_XDIST_GROUP}
    return set()


def _iter_test_defs(body: list[ast.stmt], inherited: list[ast.expr]):
    """Every ``test_*`` def in ``body``, with the marks it inherits from enclosing classes.

    Descends into `ast.ClassDef`: `test_generic_tmux.py` already defines classes, so a
    method-shaped E2E is a real reachable shape, and a scan over module-level defs only
    would report it as absent — silently green while it ran ungrouped. Class decorators
    are carried down because a class-level `@real_mux_e2e` genuinely groups its methods.
    """
    for stmt in body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            if stmt.name.startswith("test_"):
                yield stmt, [*stmt.decorator_list, *inherited]
        elif isinstance(stmt, ast.ClassDef):
            yield from _iter_test_defs(stmt.body, [*stmt.decorator_list, *inherited])


def _module_pytestmark(tree: ast.Module) -> list[ast.expr]:
    """The marks a module-level ``pytestmark`` applies to every test in the file."""
    marks: list[ast.expr] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        marks.extend(value.elts if isinstance(value, ast.List | ast.Tuple) else [value])
    return marks


def _scan_source(
    src: str, rel: str, conftest_gates: frozenset[str] | None = None
) -> list[tuple[str, str, bool]]:
    """Every tmux-gated test in one source → ``(rel, test_name, grouped)``.

    Split out from the tree walk so the probe rows below drive known-bad and
    known-good sources through THIS function — the same code path the real scan
    uses. A repo-wide "no offenders today" assertion is equally green when the
    invariant holds and when the scan quietly stopped scanning; the probes are what
    tell those two apart.

    ``conftest_gates`` defaults to whatever `tests/conftest.py` actually binds (nothing
    today). A probe row passes it explicitly, so the import-resolution branch is graded
    on the same code path instead of waiting for the suite to grow its first shared
    tmux gate.
    """
    if conftest_gates is None:
        conftest_gates = _conftest_gate_names()
    tree = ast.parse(src, filename=rel)
    group_aliases = _conftest_aliases(tree, frozenset({REAL_MUX_MARK_ALIAS}))
    gate_aliases = _local_gate_bindings(tree) | _conftest_aliases(tree, conftest_gates)
    module_marks = _module_pytestmark(tree)
    found: list[tuple[str, str, bool]] = []
    for stmt, own_marks in _iter_test_defs(tree.body, []):
        marks = [*own_marks, *module_marks]
        gated = any(_is_inline_tmux_skipif(m) or _dotted(m) in gate_aliases for m in marks)
        if not gated:
            continue
        groups: set[str] = set()
        for mark in marks:
            groups |= _xdist_group_names(mark, group_aliases)
        found.append((rel, stmt.name, groups == {conftest.REAL_MUX_XDIST_GROUP}))
    return found


# Exceptions are named by behavior, never inferred from a timeout's current value.
_DELIBERATE_TIMEOUT_TESTS = frozenset(
    {
        "test_tmux_timeout_with_flushed_spec_rescued_post_kill",
        "test_tmux_timeout_silent_session_not_rescued",
    }
)
_EXPECTED_SESSION_WALL_SITES = {
    "test_tmux_end_to_end_with_fake_cli": 1,
    "test_tmux_reused_task_id_ignores_stale_artifacts": 1,
    "test_tmux_end_to_end_with_a_relay_that_only_knows_the_legacy_dir": 1,
    "test_tmux_crash_detected": 1,
    **dict.fromkeys(_DELIBERATE_TIMEOUT_TESTS, 1),
}


def _scan_session_walls(src: str, rel: str) -> tuple[dict[str, int], list[str]]:
    """Inspect generic real-tmux SessionSpec calls using repository source forms.

    Reuses the scheduling detector's gate recognition. Constructor calls must spell
    SessionSpec directly, as this module does. Only direct constructor keywords are
    checked; later replacements and binding/dataflow changes are outside this focused
    guard. Missing timeout keywords fail too, since the
    default would bypass the named ceiling. Stories subprocess budgets are separate.
    """
    tree = ast.parse(src, filename=rel)
    gated = {name for _rel, name, _grouped in _scan_source(src, rel)}
    ceilings = _conftest_aliases(tree, frozenset({"REAL_MUX_HANG_CEILING_S"}))
    sites: dict[str, int] = {}
    offenders: list[str] = []
    for stmt, _marks in _iter_test_defs(tree.body, []):
        if stmt.name not in gated:
            continue
        sites[stmt.name] = 0
        deliberate = stmt.name in _DELIBERATE_TIMEOUT_TESTS
        expected = (
            "literal 6.0 timeout trigger" if deliberate else "imported REAL_MUX_HANG_CEILING_S"
        )
        for call in ast.walk(stmt):
            if not isinstance(call, ast.Call) or _dotted(call.func) != "SessionSpec":
                continue
            sites[stmt.name] += 1
            value = next((kw.value for kw in call.keywords if kw.arg == "timeout_s"), None)
            if deliberate:
                valid = (
                    isinstance(value, ast.Constant)
                    and type(value.value) is float
                    and value.value == 6.0
                )
            else:
                valid = value is not None and _dotted(value) in ceilings
            if not valid:
                actual = ast.unparse(value) if value is not None else "missing timeout_s"
                offenders.append(
                    f"{rel}::{stmt.name}:{call.lineno}: expected {expected}, got {actual}"
                )
        if sites[stmt.name] == 0:
            offenders.append(f"{rel}::{stmt.name}: no SessionSpec wait inspected")
    return sites, offenders


def test_real_tmux_session_walls_use_the_shared_ceiling_or_deliberate_trigger():
    assert conftest.REAL_MUX_HANG_CEILING_S == 90.0, (
        "conftest.REAL_MUX_HANG_CEILING_S must remain 90.0; shortening the shared "
        "constant would restore a load-sensitive completion wall"
    )
    path = _TESTS_DIR / "test_generic_tmux.py"
    sites, offenders = _scan_session_walls(path.read_text(encoding="utf-8"), path.name)
    assert not offenders, "\n".join(offenders)
    for name, expected in _EXPECTED_SESSION_WALL_SITES.items():
        assert sites.get(name) == expected, (
            f"{path.name}::{name}: expected {expected} SessionSpec site, "
            f"inspected {sites.get(name, 0)}; update the inventory for intentional changes"
        )


@pytest.mark.parametrize(
    ("name", "value", "imports", "accepted"),
    [
        ("test_completion", "REAL_MUX_HANG_CEILING_S", True, True),
        ("test_completion", "30.0", True, False),
        ("test_completion", "REAL_MUX_HANG_CEILING_S", False, False),
        ("test_completion", None, True, False),
        *[
            (name, value, True, accepted)
            for name in sorted(_DELIBERATE_TIMEOUT_TESTS)
            for value, accepted in [("6.0", True), ("7.0", False)]
        ],
    ],
)
def test_session_wall_detector_grades_completion_and_deliberate_waits(
    name, value, imports, accepted
):
    source = "from conftest import REAL_MUX_HANG_CEILING_S\n" if imports else ""
    keyword = f"timeout_s={value}" if value is not None else ""
    source += (
        '@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")\n'
        f"def {name}():\n    spec = SessionSpec({keyword})\n"
    )
    sites, offenders = _scan_session_walls(source, "test_probe.py")
    assert sites == {name: 1}
    if accepted:
        assert offenders == []
    else:
        assert len(offenders) == 1
        assert f"test_probe.py::{name}:" in offenders[0]


@pytest.mark.parametrize(
    ("body", "site_count", "offender_count"),
    [
        ("pass", 0, 1),
        ("SessionSpec(timeout_s=REAL_MUX_HANG_CEILING_S); SessionSpec(timeout_s=30.0)", 2, 1),
    ],
)
def test_session_wall_detector_cannot_pass_by_missing_a_site(body, site_count, offender_count):
    source = (
        "from conftest import REAL_MUX_HANG_CEILING_S\n"
        '@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")\n'
        f"def test_completion():\n    {body}\n"
    )
    sites, offenders = _scan_session_walls(source, "test_probe.py")
    assert sites == {"test_completion": site_count}
    assert len(offenders) == offender_count
    assert "test_probe.py::test_completion" in offenders[0]


def test_session_wall_detector_leaves_scripted_clock_waits_alone():
    sites, offenders = _scan_session_walls(
        "def test_scripted_clock():\n    SessionSpec(timeout_s=2.0)\n", "test_probe.py"
    )
    assert sites == {}
    assert offenders == []


# DW-108: the stories E2E polls three reap deadlines off `time.monotonic()`. They were
# bare 10-second budgets — the same load-sensitivity class DW-95 removed from the
# generic-tmux SessionSpec walls — and this file was the one real-tmux module the
# wall guard above deliberately excluded, so nothing stopped a reintroduction.
_EXPECTED_REAP_DEADLINE_SITES = {
    "test_e2e_session_timeout_teardown": 1,
    "test_e2e_detached_writer_reaped_before_worktree_teardown": 1,
    "test_e2e_detached_writer_publication_fault_still_reaped": 1,
}


_REAP_PROBE_GATE = (
    '@pytest.mark.skipif(not HAVE_TMUX, reason="stories E2E needs real tmux on Linux")\n'
)


def _scan_reap_deadlines(src: str, rel: str) -> tuple[dict[str, int], list[str]]:
    """Inspect ``time.monotonic() + <budget>`` poll deadlines in a real-tmux module.

    A SECOND scanner rather than a widening of `_scan_session_walls`: that one grades
    `SessionSpec(timeout_s=...)` constructor keywords and carries the deliberate-6.0
    trigger exception list, neither of which has an analogue here — and the stories
    file has no `SessionSpec` calls at all, so folding the shapes together would mean
    threading an expression-kind switch through both.

    Only ``time.monotonic() + <expr>`` additions inside a tmux-gated `test_*`
    def count, so the `assert time.monotonic() < deadline` poll lines — comparisons,
    not additions — fall outside without special-casing. The budget must resolve, via
    `_conftest_aliases`, to the conftest-imported `REAL_MUX_HANG_CEILING_S` under
    either import form; a same-spelled local that was never imported is not it.

    Unlike the SessionSpec scan this does NOT flag a gated test holding zero sites:
    most stories E2Es have no poll deadline at all. `_reap_inventory_offenders` is
    what keeps a scan that found nothing from passing vacuously. Subprocess `timeout=`
    budgets in that file are a separate scope and are not inspected here.

    KNOWN BLIND SPOTS, deliberately not closed — the shape above is matched literally,
    so every one of these scans CLEAN and would carry a bare budget past the guard:
    commuted operands (`10 + time.monotonic()`), an `AugAssign` top-up (`d =
    time.monotonic()` then `d += 10`), a different clock (`time.time() + 10`,
    `time.perf_counter() + 10`), outer re-scaling around a clean inner Add
    (`time.monotonic() + REAL_MUX_HANG_CEILING_S - 80`, an effective 10s budget), and a
    local rebinding that shadows the imported ceiling. Closing them means grading
    arbitrary arithmetic and local dataflow, which this focused scan will not do; the
    narrow shape is the point. Probe rows below pin the commuted and outer-arithmetic
    cases so the limit is executable rather than prose — the same doctrine
    `_is_inline_tmux_skipif` uses for its non-literal-reason gap. Write these deadlines
    in the plain shape and the guard sees them.
    """
    tree = ast.parse(src, filename=rel)
    gated = {name for _rel, name, _grouped in _scan_source(src, rel)}
    ceilings = _conftest_aliases(tree, frozenset({"REAL_MUX_HANG_CEILING_S"}))
    sites: dict[str, int] = {}
    offenders: list[str] = []
    for stmt, _marks in _iter_test_defs(tree.body, []):
        if stmt.name not in gated:
            continue
        for node in ast.walk(stmt):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
                continue
            head = _dotted(node.left.func) if isinstance(node.left, ast.Call) else None
            if head != "time.monotonic":
                continue
            sites[stmt.name] = sites.get(stmt.name, 0) + 1
            if _dotted(node.right) not in ceilings:
                offenders.append(
                    f"{rel}::{stmt.name}:{node.lineno}: expected imported "
                    f"REAL_MUX_HANG_CEILING_S, got {ast.unparse(node.right)}"
                )
    return sites, offenders


def _reap_inventory_offenders(sites: dict[str, int], rel: str) -> list[str]:
    """Mismatches between a reap-deadline scan and the named expected-site inventory."""
    return [
        f"{rel}::{name}: expected {expected} reap deadline site, inspected "
        f"{sites.get(name, 0)}; update the inventory for intentional changes"
        for name, expected in _EXPECTED_REAP_DEADLINE_SITES.items()
        if sites.get(name) != expected
    ]


def test_stories_e2e_reap_deadlines_use_the_shared_ceiling():
    path = _TESTS_DIR / "test_stories_e2e.py"
    sites, offenders = _scan_reap_deadlines(path.read_text(encoding="utf-8"), path.name)
    assert not offenders, "\n".join(offenders)
    inventory = _reap_inventory_offenders(sites, path.name)
    assert not inventory, "\n".join(inventory)


@pytest.mark.parametrize(
    ("budget", "imports", "reported"),
    [
        ("REAL_MUX_HANG_CEILING_S", "from", None),
        ("10", "from", "got 10"),
        ("REAL_MUX_HANG_CEILING_S", "none", "got REAL_MUX_HANG_CEILING_S"),
        ("conftest.REAL_MUX_HANG_CEILING_S", "dotted", None),
    ],
)
def test_reap_deadline_detector_grades_poll_budgets(budget, imports, reported):
    header = {
        "from": "from conftest import REAL_MUX_HANG_CEILING_S\n",
        "dotted": "import conftest\n",
        "none": "",
    }[imports]
    source = (
        header
        + _REAP_PROBE_GATE
        + "def test_reap():\n"
        + f"    deadline = time.monotonic() + {budget}\n"
        + "    assert time.monotonic() < deadline\n"
    )
    sites, offenders = _scan_reap_deadlines(source, "test_probe.py")
    assert sites == {"test_reap": 1}  # the `<` poll comparison is not a second site
    if reported is None:
        assert offenders == []
    else:
        assert len(offenders) == 1
        assert "test_probe.py::test_reap:" in offenders[0]
        assert reported in offenders[0]


@pytest.mark.parametrize(
    ("gate", "body", "sites"),
    [
        # Gating is the only thing keeping this scanner off unrelated tests.
        ("", "deadline = time.monotonic() + 10", {}),
        # Documented blind spots: matched literally, so these carry a bare 10s budget
        # past the guard. Pinned as must-stay-silent so the limit cannot rot into a
        # believed-covered shape — see the detector docstring.
        (_REAP_PROBE_GATE, "deadline = 10 + time.monotonic()", {}),
        (
            _REAP_PROBE_GATE,
            "deadline = clock.monotonic() + REAL_MUX_HANG_CEILING_S",
            {},
        ),
        (
            _REAP_PROBE_GATE,
            "deadline = time.monotonic() + REAL_MUX_HANG_CEILING_S - 80",
            {"test_reap": 1},
        ),
    ],
)
def test_reap_deadline_detector_leaves_lookalikes_alone(gate, body, sites):
    source = (
        "from conftest import REAL_MUX_HANG_CEILING_S\n" f"{gate}def test_reap():\n    {body}\n"
    )
    scanned, offenders = _scan_reap_deadlines(source, "test_probe.py")
    assert scanned == sites
    assert offenders == []


def test_reap_deadline_detector_cannot_pass_by_scanning_nothing():
    source = (
        "from conftest import REAL_MUX_HANG_CEILING_S\n"
        + _REAP_PROBE_GATE
        + "def test_e2e_session_timeout_teardown():\n    pass\n"
    )
    sites, offenders = _scan_reap_deadlines(source, "test_probe.py")
    assert sites == {}
    assert offenders == []  # the scan alone is silent — the inventory is what bites
    inventory = _reap_inventory_offenders(sites, "test_probe.py")
    assert len(inventory) == len(_EXPECTED_REAP_DEADLINE_SITES)
    assert "test_probe.py::test_e2e_session_timeout_teardown" in inventory[0]
    assert "inspected 0" in inventory[0]


# DW-174: the stories detached-writer fake pins its bash-side detach acknowledgement
# budget by SPLICING the shared ceiling into a module-level f-string. That module asserts
# the fragment's presence and order in the RENDERED text, which cannot tell the splice
# apart from a hardcoded `90` — the two render byte-identically, so a bare literal would
# restore exactly the load-sensitive budget DW-95/DW-108 paid down with every row still
# green. This third scanner grades that module's own AST for the EXPRESSION instead.
#
# A third focused scanner rather than a widening of the two above: `_scan_session_walls`
# grades `SessionSpec(...)` constructor keywords and `_scan_reap_deadlines` grades
# `time.monotonic() + <expr>` inside a test def. Neither reaches a module-level string
# concatenation, and folding a third expression kind into either would mean threading a
# shape switch through both.
#
# MODULE-LEVEL assignments only, which is what keeps `_run_detach_gate` out of scope for
# free: that helper deliberately VARIES `detach_ack_ceiling_s={ceiling_s}` per row to
# drive the direct-harness gate, and it lives inside a def.
_DETACH_CEILING_FRAGMENT = "detach_ack_ceiling_s="

_EXPECTED_DETACH_CEILING_SITES = {"DETACHED_WRITER_FAKE_CLI": 1}


def _ceiling_excerpt(text: str) -> str:
    """The one line of ``text`` carrying the fragment, so an offender names the spelling."""
    return next(
        (line for line in text.splitlines() if _DETACH_CEILING_FRAGMENT in line),
        text,
    )


def _scan_detach_ceiling_splices(src: str, rel: str) -> tuple[dict[str, int], list[str]]:
    """Inspect module-level ``detach_ack_ceiling_s=`` splices in a real-tmux module.

    Sites are keyed by the assignment's FIRST `Name` target, since the property belongs to
    the fake-CLI constant rather than to a test def — so a chained `OTHER =
    DETACHED_WRITER_FAKE_CLI = ...` keys the site under `OTHER` and makes the inventory
    fire on a file that is in fact correct. The derived `PUBLICATION_FAULT_FAKE_CLI`
    inherits the gate through a `str.replace` and holds no fragment of its own, so it is
    deliberately not a site here; the rendered-text asserts in `tests/test_stories_e2e.py`
    are what cover that inheritance.

    Accepts exactly ``int(<conftest alias of REAL_MUX_HANG_CEILING_S>)`` immediately after
    the fragment, under either import form via `_conftest_aliases` — the `int()` is load
    bearing, because bash arithmetic cannot take the `90.0` float. Everything else offends,
    including a plain (non-f-string) constant that carries the fragment: that is the
    hardcoded-90 case this guard exists for, and it counts as a site AND an offender.

    Like `_scan_reap_deadlines` this does not flag an assignment holding zero sites — most
    module constants have no ceiling at all. `_detach_ceiling_inventory_offenders` is what
    keeps a scan that found nothing from passing vacuously.

    KNOWN BLIND SPOTS, deliberately not closed — the shape above is matched literally, so
    every one of these scans CLEAN and would carry a hardcoded budget past the guard:

    * a name-built spelling (`f"{key}={int(...)}"`), the fragment never being a literal;
    * a ceiling assembled outside a module-level assignment — a helper call, or a dict
      entry consumed later;
    * `tree.body` is iterated directly, so an assignment nested in a module-level `if`,
      `try` or `with` is never reached;
    * an `AugAssign` append (`FAKE += "detach_ack_ceiling_s=90\\n"`) is not an `Assign`
      and so is not inspected;
    * a SECOND fragment occurring EARLIER in the same f-string part — only the part's
      tail is graded, and the site still counts 1, so the earlier one is invisible;
    * a `conversion` or `format_spec` on the formatted value is accepted, since only the
      wrapped expression is graded (`{int(CEILING)!r}` and `{int(CEILING):d}` both pass);
    * a module-level REBINDING of the imported ceiling name is unseen — `_conftest_aliases`
      resolves the import, not the current binding. `_scan_reap_deadlines` shares this
      limit exactly, and diverging here would split the two scanners' alias doctrine;
    * scope is `tests/test_stories_e2e.py` alone, so the same fragment hardcoded in any
      other module is out of scope entirely.

    KNOWN FALSE POSITIVE, accepted for the same reason: the fragment must END an f-string
    part IMMEDIATELY before a `FormattedValue`, so a legitimate quoted spelling
    (`f'detach_ack_ceiling_s="{int(REAL_MUX_HANG_CEILING_S)}"'`) is reported as an offender
    even though it splices the shared ceiling correctly. Write the bare shape and the
    guard sees it.

    Closing any of the above means resolving arbitrary dataflow or widening past the shape
    the spec fixes; the narrow shape is the point, and probe rows below pin the limits that
    matter so they stay recorded limits rather than surprises.
    """
    tree = ast.parse(src, filename=rel)
    ceilings = _conftest_aliases(tree, frozenset({"REAL_MUX_HANG_CEILING_S"}))
    sites: dict[str, int] = {}
    offenders: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        name = names[0] if names else ast.unparse(targets[0])

        def offend(lineno: int, found: str, name: str = name) -> None:
            offenders.append(
                f"{rel}::{name}:{lineno}: expected "
                f"`{_DETACH_CEILING_FRAGMENT}{{int(REAL_MUX_HANG_CEILING_S)}}`, got {found}"
            )

        spliced: set[int] = set()
        for node in ast.walk(value):
            if not isinstance(node, ast.JoinedStr):
                continue
            for i, part in enumerate(node.values):
                if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                    continue
                spliced.add(id(part))
                if _DETACH_CEILING_FRAGMENT not in part.value:
                    continue
                sites[name] = sites.get(name, 0) + 1
                after = node.values[i + 1] if i + 1 < len(node.values) else None
                if not part.value.endswith(_DETACH_CEILING_FRAGMENT) or not isinstance(
                    after, ast.FormattedValue
                ):
                    offend(node.lineno, repr(_ceiling_excerpt(part.value)))
                    continue
                inner = after.value
                valid = (
                    isinstance(inner, ast.Call)
                    and _dotted(inner.func) == "int"
                    and len(inner.args) == 1
                    and not inner.keywords
                    and _dotted(inner.args[0]) in ceilings
                )
                if not valid:
                    offend(node.lineno, ast.unparse(inner))
        for node in ast.walk(value):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in spliced
                and _DETACH_CEILING_FRAGMENT in node.value
            ):
                sites[name] = sites.get(name, 0) + 1
                offend(node.lineno, f"plain literal {_ceiling_excerpt(node.value)!r}")
    return sites, offenders


def _detach_ceiling_inventory_offenders(sites: dict[str, int], rel: str) -> list[str]:
    """Mismatches between a detach-ceiling scan and the named expected-site inventory."""
    return [
        f"{rel}::{name}: expected {expected} detach-ceiling splice site, inspected "
        f"{sites.get(name, 0)}; update the inventory for intentional changes"
        for name, expected in _EXPECTED_DETACH_CEILING_SITES.items()
        if sites.get(name) != expected
    ]


def test_stories_e2e_detach_ceiling_splices_the_shared_ceiling():
    path = _TESTS_DIR / "test_stories_e2e.py"
    sites, offenders = _scan_detach_ceiling_splices(path.read_text(encoding="utf-8"), path.name)
    assert not offenders, "\n".join(offenders)
    inventory = _detach_ceiling_inventory_offenders(sites, path.name)
    assert not inventory, "\n".join(inventory)


_DETACH_PROBE_TARGET = "DETACHED_WRITER_FAKE_CLI"
_DETACH_PROBE_HEADERS = {
    "from": "from conftest import REAL_MUX_HANG_CEILING_S\n",
    "dotted": "import conftest\n",
    "none": "",
}


@pytest.mark.parametrize(
    ("value", "imports", "reported"),
    [
        ('f"detach_ack_ceiling_s={int(REAL_MUX_HANG_CEILING_S)}\\n"', "from", None),
        ('f"detach_ack_ceiling_s={int(conftest.REAL_MUX_HANG_CEILING_S)}\\n"', "dotted", None),
        # The hardcoded literals this guard exists for: both render as text the stories
        # module's own `count(...)` assert accepts (90) or rejects only by luck (10).
        ('"detach_ack_ceiling_s=90\\n"', "from", "'detach_ack_ceiling_s=90'"),
        ('"detach_ack_ceiling_s=10\\n"', "from", "'detach_ack_ceiling_s=10'"),
        # A same-spelled local that was never imported from conftest is not the ceiling.
        (
            'f"detach_ack_ceiling_s={int(REAL_MUX_HANG_CEILING_S)}\\n"',
            "none",
            "got int(REAL_MUX_HANG_CEILING_S)",
        ),
        ('f"detach_ack_ceiling_s={int(SOME_LOCAL)}\\n"', "from", "int(SOME_LOCAL)"),
        # The float would reach bash arithmetic as `90.0`; the `int()` is load bearing.
        (
            'f"detach_ack_ceiling_s={REAL_MUX_HANG_CEILING_S}\\n"',
            "from",
            "got REAL_MUX_HANG_CEILING_S",
        ),
        # Fragment present but nothing formatted after it inside the f-string.
        ('f"detach_ack_ceiling_s=90\\n{tail}"', "from", "'detach_ack_ceiling_s=90'"),
    ],
)
def test_detach_ceiling_detector_grades_the_module_level_splice(value, imports, reported):
    source = _DETACH_PROBE_HEADERS[imports] + f"{_DETACH_PROBE_TARGET} = {value}\n"
    sites, offenders = _scan_detach_ceiling_splices(source, "test_probe.py")
    assert sites == {_DETACH_PROBE_TARGET: 1}
    if reported is None:
        assert offenders == []
    else:
        assert len(offenders) == 1
        assert f"test_probe.py::{_DETACH_PROBE_TARGET}:" in offenders[0]
        assert reported in offenders[0]


@pytest.mark.parametrize(
    "body",
    [
        # `_run_detach_gate` deliberately varies the budget per row; it lives in a def, so
        # module-level-only scanning excludes it without an exception list.
        'def _run_detach_gate(ceiling_s):\n    return f"detach_ack_ceiling_s={ceiling_s}\\n"\n',
        # The bash READ of the variable (`AWAIT_DETACHED_SESSION_SH`) is not a splice.
        'AWAIT = "detach_deadline=$(( SECONDS + detach_ack_ceiling_s ))\\n"\n',
        # The derived fake inherits the gate through a replace and holds no fragment.
        "PUBLICATION_FAULT_FAKE_CLI = DETACHED_WRITER_FAKE_CLI.replace(A, B, 1)\n",
    ],
)
def test_detach_ceiling_detector_leaves_lookalikes_alone(body):
    source = _DETACH_PROBE_HEADERS["from"] + body
    sites, offenders = _scan_detach_ceiling_splices(source, "test_probe.py")
    assert sites == {}
    assert offenders == []


def test_detach_ceiling_detector_cannot_pass_by_scanning_nothing():
    source = _DETACH_PROBE_HEADERS["from"] + f'{_DETACH_PROBE_TARGET} = "no fragment here\\n"\n'
    sites, offenders = _scan_detach_ceiling_splices(source, "test_probe.py")
    assert sites == {}
    assert offenders == []  # the scan alone is silent — the inventory is what bites
    inventory = _detach_ceiling_inventory_offenders(sites, "test_probe.py")
    assert len(inventory) == len(_EXPECTED_DETACH_CEILING_SITES)
    assert f"test_probe.py::{_DETACH_PROBE_TARGET}" in inventory[0]
    assert "inspected 0" in inventory[0]


def test_detach_ceiling_inventory_bites_an_over_count_too():
    """Equality, not a floor: a SECOND splice site fails as loudly as a missing one.

    Driven directly, because the scan over the real module can only ever hand this helper
    the count that module actually holds.
    """
    expected = _EXPECTED_DETACH_CEILING_SITES[_DETACH_PROBE_TARGET]
    over = {_DETACH_PROBE_TARGET: expected + 1}
    inventory = _detach_ceiling_inventory_offenders(over, "test_probe.py")
    assert len(inventory) == 1
    assert f"test_probe.py::{_DETACH_PROBE_TARGET}" in inventory[0]
    assert f"expected {expected} detach-ceiling splice site" in inventory[0]
    assert f"inspected {expected + 1}" in inventory[0]
    assert "update the inventory for intentional changes" in inventory[0]


def test_detach_ceiling_detector_reads_an_annotated_assignment():
    """The `ast.AnnAssign` branch: every parametrized row above is a bare `NAME = value`."""
    splice = 'f"detach_ack_ceiling_s={int(REAL_MUX_HANG_CEILING_S)}\\n"'
    source = _DETACH_PROBE_HEADERS["from"] + f"{_DETACH_PROBE_TARGET}: str = {splice}\n"
    sites, offenders = _scan_detach_ceiling_splices(source, "test_probe.py")
    assert sites == {_DETACH_PROBE_TARGET: 1}
    assert offenders == []


def _scan_tests() -> list[tuple[str, str, bool]]:
    """Scan EVERY `test_*.py` module, resolving conftest's shared gates once for all of
    them rather than letting each `_scan_source` call fall back to its own lookup."""
    conftest_gates = _conftest_gate_names()
    found: list[tuple[str, str, bool]] = []
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        found.extend(_scan_source(path.read_text(encoding="utf-8"), path.name, conftest_gates))
    return found


def _declares_loadgroup(addopts: object) -> bool:
    """Whether an ``addopts`` value selects xdist's loadgroup scheduler.

    LAST wins, because argparse resolves repeated options that way: `--dist loadgroup
    --dist load` runs the DEFAULT scheduler, so a reader returning True on the first
    match would bless a config in which every group mark is inert. A list is coerced
    rather than rejected — pytest accepts a TOML array for `addopts`, and `shlex.split`
    would raise `TypeError` on one.
    """
    if not isinstance(addopts, str):
        addopts = " ".join(str(part) for part in addopts)  # type: ignore[union-attr]
    opts = shlex.split(addopts)
    chosen: str | None = None
    for i, opt in enumerate(opts):
        if opt.startswith("--dist="):
            chosen = opt.split("=", 1)[1]
        elif opt == "--dist" and i + 1 < len(opts):
            chosen = opts[i + 1]
    return chosen == "loadgroup"


# EXACT per-module, per-BUCKET gated-def inventory (DW-173, bucketed by DW-189), the same
# inventory contract `_EXPECTED_SESSION_WALL_SITES` and `_EXPECTED_REAP_DEADLINE_SITES`
# already carry: every assertion below is equality, so it bites in BOTH directions —
# adding a tmux-gated def fails it just as deleting one does, and either way the fix is to
# re-pin the number for the intentional change. A floor could only bite downward, which is
# how these numbers silently went stale before (DW-159 found stories at an actual 44
# against a floor of 30: fourteen E2Es could have been deleted with every row still green).
#
# Per-module rather than one suite-wide number: a shared count couples the modules, so a
# deletion in one blames the detector for a change the author made in the other. Both
# modules are carried here for the same reason — a half-pinned inventory leaves the
# unpinned module free to drift.
#
# Per-BUCKET rather than one per-module total (DW-189): a total alone lets a BREAKDOWN rot
# into a wrong justification for a right number. Stories used to be pinned at 47 with a
# comment explaining it as `16 + 3 + 28`; an E2E renamed into a harness name cancels
# against the harness count, so the total holds, every row stays green, and the comment
# quietly becomes false. What the buckets below buy is precise about which parts: the
# NAMED prefixes (`test_tmux_`, `test_e2e_`, `test_reap_e2e_`) are graded as families, so
# a def leaving or joining one of them is named. `_OTHER_DEFS` is a RESIDUAL, not a
# family — any 28 gated defs no named prefix claims satisfy it — so what the pair of
# buckets really pins is the BOUNDARY between the E2E families and the remainder, which
# is the edge the rotted `16 + 3 + 28` could cross undetected. Changes wholly inside the
# residual still cancel, and deliberately so: the prose being enforced named three
# families, the third being "everything else", and subdividing further would assert a
# breakdown that prose never made.
#
# The attribution RULE `_bucket_for` implements and the rows below grade:
#   * a def is attributed to the LONGEST declared prefix matching its name, so dict ORDER
#     is never load-bearing. No two prefixes NEST today (`test_reap_e2e_x` does not start
#     with `test_e2e_`, so every rule agrees on it); longest-match matters the moment a
#     nested pair like `{"a_": 1, "a_b_": 1}` is declared, where `a_b_x` must read as an
#     `a_b_`, and `test_e2e_def_count_inventory_attributes_to_the_longest_declared_prefix`
#     pins exactly that shape;
#   * a def no declared prefix claims falls to the module's `_OTHER_DEFS` bucket WHEN the
#     module declares one;
#   * a module declaring NO `_OTHER_DEFS` bucket refuses an unexplained def BY NAME rather
#     than absorbing it into a count.
_OTHER_DEFS = "<other>"  # explicit catch-all; a module omitting it refuses unexplained defs
_EXPECTED_E2E_DEF_COUNTS: dict[str, dict[str, int]] = {
    "test_generic_tmux.py": {"test_tmux_": 6},
    "test_tui_launch_tmux_e2e.py": {"test_e2e_": 1},
    "test_tmux_locale_e2e.py": {"test_e2e_": 1},
    "test_stories_e2e.py": {
        "test_e2e_": 18,
        "test_reap_e2e_": 3,
        # Not asserted, and deliberately so — see the residual note above. Today these are
        # the local-process identity harness defs: `test_detach_gate_*`,
        # `test_kill_recorded_child_*`, `test_live_child_*`, `test_proc_starttime_*`,
        # `test_reap_identity_*`, `test_recorded_*`.
        _OTHER_DEFS: 28,
    },
}


def _bucket_for(name: str, buckets: dict[str, int]) -> str | None:
    """Which declared bucket of `buckets` claims a gated def called `name`, if any.

    LONGEST declared prefix wins, so the inventory's dict order never decides an
    attribution: `test_reap_e2e_*` reads as its own family under either ordering, and a
    future prefix pair cannot silently change which bucket a def lands in. Returns
    `_OTHER_DEFS` when no declared prefix matches and the module declares the catch-all,
    and `None` — meaning UNEXPLAINED — when it does not.
    """
    prefixes = [p for p in buckets if p != _OTHER_DEFS and name.startswith(p)]
    if prefixes:
        return max(prefixes, key=len)
    return _OTHER_DEFS if _OTHER_DEFS in buckets else None


def _e2e_def_count_offenders(found: list[tuple[str, str, bool]]) -> list[str]:
    """Mismatches between a gated-def scan and the named expected-count inventory.

    Split out of the live test the way `_reap_inventory_offenders` is, and for the same
    reason: the live guard reads the real `tests/` tree through `_scan_tests()`, so the
    mismatch path this inventory exists to enforce cannot be driven there — only a
    synthetic `found` list can exercise it, in either direction.

    Two kinds of offender, matching the two ways a bucketed inventory can be wrong: a
    bucket whose count no longer matches what the scan attributed to it, and a def in a
    module with no `_OTHER_DEFS` bucket that no declared prefix claims. The second names
    the DEF, not just a count, because "6 gated defs, one of which is not a `test_tmux_`"
    is the failure a per-module total could never report at all.

    PRECONDITION, and the reason it is written down: this helper iterates the INVENTORY,
    so it is blind to a module the inventory does not declare — a wholly new module full
    of gated defs yields no offenders here. The live caller's
    `set(_EXPECTED_E2E_DEF_COUNTS) == {rel for ...}` assertion is the only thing that
    covers that case, and leaving the dependency implicit would be the same unenforced
    claim this bucketing exists to remove.
    """
    offenders: list[str] = []
    for rel, buckets in _EXPECTED_E2E_DEF_COUNTS.items():
        seen = dict.fromkeys(buckets, 0)
        for found_rel, name, _grouped in found:
            if found_rel != rel:
                continue
            bucket = _bucket_for(name, buckets)
            if bucket is None:
                # Its own remedy clause: none of the three fixes here is re-pinning a
                # number, so the count offenders' "update the inventory" tail would send
                # a reader looking for a count to change that does not exist.
                offenders.append(
                    f"{rel}::{name}: no declared prefix claims this tmux-gated test def "
                    f"and {rel} declares no catch-all bucket; rename the def to match a "
                    "declared prefix, declare a new prefix bucket for its family, or give "
                    f"{rel} an explicit {_OTHER_DEFS} bucket"
                )
                continue
            seen[bucket] += 1
        for bucket, expected in buckets.items():
            if seen[bucket] != expected:
                offenders.append(
                    f"{rel} [{bucket}]: expected {expected} tmux-gated test defs, "
                    f"inspected {seen[bucket]}; "
                    "update the inventory for intentional changes"
                )
    return offenders


def _inventory_copy() -> dict[str, dict[str, int]]:
    """A mutable deep-enough copy of the inventory, so a perturbing row cannot leak."""
    return {rel: dict(buckets) for rel, buckets in _EXPECTED_E2E_DEF_COUNTS.items()}


# A name stem no DECLARED prefix in any module claims. `test_the_unexplained_stem_is_
# claimed_by_no_declared_prefix` below holds that generically, over every module in the
# inventory, rather than by hand per module — a third module, or a new prefix on an
# existing one, would otherwise void the invariant silently.
_UNEXPLAINED_DEF_STEM = "test_unexplained_"


def _synthetic_found(counts: dict[str, dict[str, int]]) -> list[tuple[str, str, bool]]:
    """A `_scan_tests()`-shaped list holding the named per-module, per-bucket counts.

    Every minted name is asserted to attribute BACK to the bucket that asked for it,
    against the module's DECLARED buckets. Without that check a future prefix pair (one
    declared prefix being another plus digits) would leave these synthetic rows exercising
    a different attribution from the live scan they exist to share, and the both-directions
    rows would grade a bucket nobody pinned.
    """
    found: list[tuple[str, str, bool]] = []
    for rel, buckets in counts.items():
        # The module must be DECLARED. Falling back to the caller's own `buckets` would
        # attribute minted names against a shape the real inventory does not hold, so a
        # row perturbing an undeclared module would grade itself rather than the
        # inventory — and `_e2e_def_count_offenders` never even looks at such a module.
        assert rel in _EXPECTED_E2E_DEF_COUNTS, (
            f"{rel} is not in _EXPECTED_E2E_DEF_COUNTS; `_e2e_def_count_offenders` "
            "iterates the inventory and would silently ignore every def minted for it"
        )
        declared = _EXPECTED_E2E_DEF_COUNTS[rel]
        for bucket, n in buckets.items():
            for i in range(n):
                name = (
                    f"{_UNEXPLAINED_DEF_STEM}{i}" if bucket == _OTHER_DEFS else f"{bucket}row_{i}"
                )
                assert _bucket_for(name, declared) == bucket, (
                    f"{rel}: the synthetic name {name!r} minted for bucket {bucket!r} "
                    f"attributes to {_bucket_for(name, declared)!r} instead — these rows "
                    "would be grading a different attribution from the live scan"
                )
                found.append((rel, name, True))
    return found


def test_every_real_tmux_e2e_joins_the_serialized_xdist_group():
    """The live scan: no real-tmux E2E may run outside the shared group (DW-95).

    The counts are not decoration — without them a glob that stopped matching, or a
    discriminator that stopped discriminating, reports zero offenders and passes while
    enforcing nothing."""
    found = _scan_tests()
    offenders = [f"{rel}::{name}" for rel, name, grouped in found if not grouped]
    assert offenders == [], (
        "these real-tmux E2Es are not in exactly the shared xdist group; import "
        f"`{REAL_MUX_MARK_ALIAS}` from conftest and apply it (and remove any other "
        f"`xdist_group` mark, which xdist would merge into a different group): {offenders}"
    )
    # `_e2e_def_count_offenders` iterates the INVENTORY, so this is the only assertion
    # that can see a module the inventory does not declare. See that helper's docstring.
    assert set(_EXPECTED_E2E_DEF_COUNTS) == {rel for rel, _name, _grouped in found}, (
        "the set of modules holding real-tmux E2Es changed. If a new module legitimately "
        "drives real tmux, confirm it applies `real_mux_e2e` and then add it to "
        f"_EXPECTED_E2E_DEF_COUNTS with its own bucket breakdown; "
        f"found {sorted({r for r, _n, _g in found})}"
    )
    counts = _e2e_def_count_offenders(found)
    assert not counts, "\n".join(counts)


def test_live_e2e_scan_reads_shared_gates_once(monkeypatch):
    """The live scan resolves conftest's shared gates ONCE and hands that one set to
    every module it scans — and still scans every `test_*.py` module.

    `_scan_source` is replaced by a recorder, so this row reads the tree without
    re-parsing it: the parse cost is the live guard's own, and doubling it here would
    tax exactly the runtime this structure exists to bound.

    Ablation target: stop passing `conftest_gates` from `_scan_tests` and the recorded
    gates read `None` while the lookup count climbs to one per module; narrow the glob
    and the module inventory assertion fails."""
    module = sys.modules[__name__]
    lookups: list[None] = []
    scanned: list[tuple[str, frozenset[str] | None]] = []
    real_gates = _conftest_gate_names

    def counted_gates() -> frozenset[str]:
        lookups.append(None)
        return real_gates()

    def recording_scan(src, rel, conftest_gates=None):
        assert src, f"{rel} was handed to the scan empty"
        scanned.append((rel, conftest_gates))
        return []

    monkeypatch.setattr(module, "_conftest_gate_names", counted_gates)
    monkeypatch.setattr(module, "_scan_source", recording_scan)

    assert _scan_tests() == []

    assert len(lookups) == 1
    assert [rel for rel, _gates in scanned] == sorted(p.name for p in _TESTS_DIR.glob("test_*.py"))
    assert {gates for _rel, gates in scanned} == {real_gates()}


def test_the_unexplained_stem_is_claimed_by_no_declared_prefix():
    """`_UNEXPLAINED_DEF_STEM` must stay unexplained in EVERY module of the inventory.

    Generic over the inventory rather than spot-checked per module: the rows below use
    this stem to mint both the catch-all bucket's names and the by-name refusal probe, so
    a third module — or a new prefix on an existing one — that happened to claim the stem
    would quietly turn those rows into something else while leaving them green. A
    hand-written line per module is exactly the second hand-maintained claim DW-189 exists
    to delete.
    """
    for rel, buckets in _EXPECTED_E2E_DEF_COUNTS.items():
        # SYMMETRIC, because the stem is used to mint `stem + suffix` names, not the bare
        # stem: a declared prefix SHORTER than the stem claims the stem itself, and one
        # LONGER than the stem (`test_unexplained_0`) claims the minted names while
        # leaving the bare stem unexplained. Testing one direction only would let the
        # second shape mis-bucket every synthetic name with this row still green.
        claimed = [
            p
            for p in buckets
            if p != _OTHER_DEFS
            and (_UNEXPLAINED_DEF_STEM.startswith(p) or p.startswith(_UNEXPLAINED_DEF_STEM))
        ]
        assert claimed == [], (
            f"{rel} declares {claimed}, which claims _UNEXPLAINED_DEF_STEM "
            f"({_UNEXPLAINED_DEF_STEM!r}) or the names minted from it; pick a stem that "
            "neither extends nor is extended by any declared prefix"
        )
        assert _bucket_for(_UNEXPLAINED_DEF_STEM, buckets) == (
            _OTHER_DEFS if _OTHER_DEFS in buckets else None
        )


@pytest.mark.parametrize(("delta", "direction"), [(1, "def added"), (-1, "def removed")])
def test_e2e_def_count_inventory_bites_in_both_directions(delta, direction):
    """Equality, not a floor, PER BUCKET: an ADDED gated def fails as loudly as a removed
    one, and it does so in whichever bucket moved rather than only in a module total.

    Driven synthetically because the live guard above scans the real `tests/` tree — the
    only mismatch it can ever see is one an author has already committed.
    """
    for rel, buckets in _EXPECTED_E2E_DEF_COUNTS.items():
        for bucket, expected in buckets.items():
            counts = _inventory_copy()
            counts[rel][bucket] = expected + delta
            offenders = _e2e_def_count_offenders(_synthetic_found(counts))
            assert len(offenders) == 1, f"{direction} in {rel} [{bucket}]: {offenders}"
            assert offenders[0].startswith(f"{rel} [{bucket}]: ")
            assert f"expected {expected} tmux-gated test defs" in offenders[0]
            assert f"inspected {expected + delta}" in offenders[0]
            assert "update the inventory for intentional changes" in offenders[0]


def test_e2e_def_count_inventory_bites_a_rotted_breakdown_at_an_unchanged_total():
    """The property a per-module TOTAL could not hold, and the whole reason for DW-189.

    One def moves across the E2E/residual boundary while the module's total stays put, so
    the flat inventory this replaced stayed green while its comment's `16 + 3 + 28`
    justification became false. Both moved buckets must be named.

    BOTH directions, because they fail differently in practice. An E2E renamed into a
    harness-shaped name (`shifted = -1`) quietly deletes real-tmux coverage; a harness def
    renamed into a `test_e2e_` name (`shifted = +1`) INFLATES the apparent real-tmux E2E
    count, which is the more dangerous direction — it makes the suite look better covered
    than it is. A one-directional row would grade only the first.

    The unchanged total is DERIVED from the inventory below, never restated as a literal:
    writing the number here would reintroduce the hand-maintained figure the header
    comment claims is gone.
    """
    rel = "test_stories_e2e.py"
    buckets = _EXPECTED_E2E_DEF_COUNTS[rel]
    for shifted in (-1, 1):
        counts = _inventory_copy()
        counts[rel]["test_e2e_"] += shifted
        counts[rel][_OTHER_DEFS] -= shifted
        found = _synthetic_found(counts)
        # total holds — derived, never a literal
        assert sum(1 for r, _n, _g in found if r == rel) == sum(buckets.values()), shifted

        offenders = _e2e_def_count_offenders(found)
        assert len(offenders) == 2, (shifted, offenders)
        assert any(o.startswith(f"{rel} [test_e2e_]: ") for o in offenders), (shifted, offenders)
        assert any(o.startswith(f"{rel} [{_OTHER_DEFS}]: ") for o in offenders), (
            shifted,
            offenders,
        )
        for offender in offenders:
            assert "update the inventory for intentional changes" in offender


def test_e2e_def_count_inventory_names_an_unexplained_def_in_a_catch_all_free_module():
    """The second offender kind: a module with NO catch-all refuses a def BY NAME.

    `test_generic_tmux.py` declares only `test_tmux_`, so a gated def that does not start
    with it is not "one too many defs" — the inventory can say exactly which def it could
    not explain. Its declared bucket still balances, so the single offender is the naming
    one and nothing else.
    """
    rel = "test_generic_tmux.py"
    assert _OTHER_DEFS not in _EXPECTED_E2E_DEF_COUNTS[rel]  # the premise of the row
    stray = f"{_UNEXPLAINED_DEF_STEM}stray"
    found = [*_synthetic_found(_inventory_copy()), (rel, stray, True)]

    offenders = _e2e_def_count_offenders(found)
    assert len(offenders) == 1, offenders
    assert offenders[0].startswith(f"{rel}::{stray}: ")
    assert "no declared prefix claims this tmux-gated test def" in offenders[0]
    # its OWN remedy clause: none of the three fixes is re-pinning a count, so it must
    # not inherit the count offenders' "update the inventory" tail
    assert "update the inventory for intentional changes" not in offenders[0]
    assert "rename the def to match a declared prefix" in offenders[0]
    assert "declare a new prefix bucket for its family" in offenders[0]
    assert f"give {rel} an explicit {_OTHER_DEFS} bucket" in offenders[0]


def test_e2e_def_count_inventory_is_blind_to_a_module_it_does_not_declare():
    """The PRECONDITION `_e2e_def_count_offenders`' docstring states, graded rather than
    asserted in prose.

    The helper iterates the INVENTORY, so a wholly new module full of gated defs — even
    ones no prefix could explain — produces no offender here at all. That is not a defect
    to fix in this helper: the live caller's
    `set(_EXPECTED_E2E_DEF_COUNTS) == {rel for ...}` assertion is the backstop, and
    duplicating it here would give two places to keep in step. What must not happen is the
    dependency being documented and unenforced, which is the exact shape DW-189 exists to
    remove — so the blindness is pinned here and the backstop is pinned by asserting the
    live scan would catch it.

    `found` is built literally, not through `_synthetic_found`: that helper now REFUSES an
    undeclared module for this same reason, so it cannot be used to build the input.
    """
    undeclared = "test_a_brand_new_tmux_module.py"
    assert undeclared not in _EXPECTED_E2E_DEF_COUNTS
    found = [
        *_synthetic_found(_inventory_copy()),
        (undeclared, f"{_UNEXPLAINED_DEF_STEM}0", True),
        (undeclared, "test_tmux_1", True),
    ]

    assert _e2e_def_count_offenders(found) == []  # blind, by construction

    # ...and the live caller's module-set assertion is what would catch it
    assert set(_EXPECTED_E2E_DEF_COUNTS) != {rel for rel, _name, _grouped in found}
    # the refusal that keeps `_synthetic_found` from papering over the same hole
    with pytest.raises(AssertionError, match="not in _EXPECTED_E2E_DEF_COUNTS"):
        _synthetic_found({undeclared: {"test_tmux_": 1}})


def test_e2e_def_count_inventory_attributes_to_the_longest_declared_prefix():
    """`test_reap_e2e_*` is its own family, under any dict ordering.

    Both `test_e2e_` and `test_reap_e2e_` are declared for the stories module and neither
    is a prefix of the other, but a shortest-match or first-match rule over a differently
    ordered dict would still be a live hazard the moment a nested pair is declared.
    Pinning LONGEST here is what lets the header comment promise order is never
    load-bearing.
    """
    buckets = _EXPECTED_E2E_DEF_COUNTS["test_stories_e2e.py"]
    assert _bucket_for("test_reap_e2e_x", buckets) == "test_reap_e2e_"
    assert _bucket_for("test_e2e_x", buckets) == "test_e2e_"
    assert _bucket_for(f"{_UNEXPLAINED_DEF_STEM}x", buckets) == _OTHER_DEFS
    # the same name in a module without the catch-all is unexplained, not absorbed
    assert _bucket_for(f"{_UNEXPLAINED_DEF_STEM}x", {"test_tmux_": 6}) is None
    # a longest-match probe over a synthetic nested pair, which the live inventory has no
    # instance of: shortest-match would answer "a_" here and silently mis-bucket
    assert _bucket_for("a_b_x", {"a_": 1, "a_b_": 1}) == "a_b_"


def test_e2e_def_count_inventory_stays_silent_on_an_exact_match():
    assert _e2e_def_count_offenders(_synthetic_found(_inventory_copy())) == []


def test_the_shared_mark_really_carries_the_shared_group_name():
    """Closes the loop the AST scan cannot: the scan accepts the bare alias
    `real_mux_e2e` on trust, so if that alias were ever rebound to a different group
    (or to something that is not an `xdist_group` mark at all), every module using it
    would pass the scan while being scheduled apart. Asserted against the live object,
    not the source text."""
    mark = getattr(conftest, REAL_MUX_MARK_ALIAS)
    assert mark.name == "xdist_group"
    assert mark.args == (conftest.REAL_MUX_XDIST_GROUP,)


def test_pyproject_addopts_selects_the_loadgroup_scheduler():
    """The other inert half: `xdist_group` marks do nothing under the default `load`
    scheduler, and xdist raises no warning about it. Declaring the flag in `addopts`
    rather than only in the CI workflow is what makes a local `-n logical` run
    schedule the way CI does.

    Read with chained `.get` so a moved or renamed table produces this row's actionable
    message rather than a bare `KeyError` from the indexing itself."""
    config = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    ini = config.get("tool", {}).get("pytest", {}).get("ini_options", {})
    addopts = ini.get("addopts", "")
    assert _declares_loadgroup(addopts), (
        "pyproject.toml's [tool.pytest.ini_options] must pass `--dist loadgroup`; "
        f"without it every xdist_group mark is silently inert (got {addopts!r})"
    )


@pytest.mark.parametrize("category", [DeprecationWarning, PendingDeprecationWarning])
def test_pyproject_filterwarnings_turns_deprecations_into_errors(request, category):
    """DW-370: without the `filterwarnings` posture a new deprecation — first-party,
    a dependency's, or one a new Python leg introduces — is a line in the warnings
    summary and the matrix stays green. Asserted twice: the loaded ini carries the
    entry, and a warning emitted inside a test actually raises, which is the fact that
    matters (a later blanket `ignore` would leave the entry present but inert).

    Ablation: delete the pyproject `filterwarnings` line and the `getini` assert fails;
    keep it but append a blanket `"ignore::DeprecationWarning"` after it and the
    `DeprecationWarning` row fails on `pytest.raises` (DID NOT RAISE)."""
    entry = f"error::{category.__name__}"
    assert entry in request.config.getini(
        "filterwarnings"
    ), f"pyproject.toml's [tool.pytest.ini_options] filterwarnings must carry {entry!r}"
    with pytest.raises(category):
        warnings.warn("DW-370 posture probe", category, stacklevel=1)


_MECHANISM_MODULE = """
import pytest


@pytest.mark.xdist_group("{group}")
def test_grouped_one():
    pass


@pytest.mark.xdist_group("{group}")
def test_grouped_two():
    pass


def test_loose_one():
    pass


def test_loose_two():
    pass
"""


def _run_grouping_probe(tmp_path, dist: str) -> dict[str, tuple[str, str]]:
    """Run a synthetic 4-test module under ``--dist <dist>`` → {test: (worker, nodeid)}.

    A subprocess because the scheduler is chosen at session start: nothing inside a
    running test can observe it (`pytestconfig.getoption("dist")` reads "no" on a plain
    run AND on a worker, and `request.node.nodeid` carries no group suffix in-test), so
    an in-process assertion would either be vacuous or break `uv run pytest`.

    `-o addopts=` drops the repo's own `--dist` so this run's scheduler is only the one
    passed here; the module is written under `tmp_path`, never into the repo.
    """
    module = tmp_path / "test_grouping_mechanism.py"
    module.write_text(
        _MECHANISM_MODULE.format(group=conftest.REAL_MUX_XDIST_GROUP), encoding="utf-8"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            module.name,
            "-p",
            "no:cacheprovider",
            "-v",
            "-n",
            "2",
            "-o",
            "addopts=",
            "--dist",
            dist,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert (
        proc.returncode == 0
    ), f"probe run failed under --dist {dist}:\n{proc.stdout}\n{proc.stderr}"
    reported: dict[str, tuple[str, str]] = {}
    for line in proc.stdout.splitlines():
        match = re.match(r"^\[(gw\d+)\] \[[^\]]*\] PASSED (\S+)", line.strip())
        if match is None:
            continue
        worker, nodeid = match.group(1), match.group(2)
        reported[nodeid.split("::")[-1].split("@")[0]] = (worker, nodeid)
    assert len(reported) == 4, f"expected 4 reported tests under --dist {dist}, got {reported}"
    return reported


def test_loadgroup_actually_schedules_the_group_onto_one_worker(tmp_path):
    """The MECHANISM, not the declaration: everything else in this guard reads source
    text, and source text is exactly what stays green when the scheduler is wrong.
    Verified: under `--dist load` the three static rows all pass while every group mark
    is inert. So this row observes a real xdist run instead.

    The node-id suffix is the load-bearing signal, not worker equality: with 4 tests on
    2 workers the two grouped tests frequently land together under plain `load` by
    chance (observed), so equality alone does not discriminate. xdist renames a grouped
    item to `<nodeid>@<group>`, and that rename happens if and only if the scheduler
    honoured the mark."""
    grouped = ("test_grouped_one", "test_grouped_two")

    honoured = _run_grouping_probe(tmp_path, "loadgroup")
    workers = {honoured[name][0] for name in grouped}
    assert len(workers) == 1, f"loadgroup split the shared group across {workers}"
    for name in grouped:
        assert honoured[name][1].endswith(
            f"@{conftest.REAL_MUX_XDIST_GROUP}"
        ), f"{name} was not scheduled into the group: {honoured[name][1]}"

    # The discriminating control: same module, default scheduler, marks inert.
    ignored = _run_grouping_probe(tmp_path, "load")
    for name in grouped:
        assert "@" not in ignored[name][1], (
            f"`--dist load` was expected to ignore the group mark, but {name} reported "
            f"as {ignored[name][1]} — the control no longer discriminates"
        )


@pytest.mark.parametrize(
    "addopts",
    [
        "--dist loadgroup",
        "--dist=loadgroup",
        "-q --dist loadgroup --durations=15",
        "--dist load --dist loadgroup",
        ["-q", "--dist", "loadgroup"],
    ],
    ids=["spaced", "equals", "among-others", "last-wins", "toml-list"],
)
def test_loadgroup_reader_accepts_a_config_that_selects_loadgroup(addopts):
    assert _declares_loadgroup(addopts)


@pytest.mark.parametrize(
    "addopts",
    [
        "",
        "-q",
        "--dist load",
        "--dist=loadscope",
        "--distloadgroup",
        "--dist loadfile",
        "--dist loadgroup --dist load",
        ["-q", "--dist", "load"],
    ],
    ids=[
        "empty",
        "no-dist",
        "explicit-load",
        "loadscope",
        "not-a-flag",
        "loadfile",
        "overridden-last-wins",
        "toml-list-load",
    ],
)
def test_loadgroup_reader_refuses_anything_else(addopts):
    """The must-stay-silent side of the `addopts` row. Two rows carry the weight:
    `explicit-load` is the default scheduler spelled out — the exact configuration
    under which every group mark goes inert — and `overridden-last-wins` is the same
    thing reached by a later flag, which a first-match reader would bless."""
    assert not _declares_loadgroup(addopts)


_GATED_UNGROUPED = """
import pytest

HAVE_TMUX = True


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_spawns_real_tmux():
    pass
"""

_MODULE_GATED_UNGROUPED = """
import pytest

pytestmark = pytest.mark.skipif(False, reason="stories E2E needs real tmux on Linux")


def test_spawns_real_tmux():
    pass
"""

_WRONG_GROUP = """
import pytest


@pytest.mark.skipif(False, reason="tmux not available")
@pytest.mark.xdist_group("something-else")
def test_spawns_real_tmux():
    pass
"""

_TWO_GROUPS = """
import pytest
from conftest import real_mux_e2e


@pytest.mark.skipif(False, reason="tmux not available")
@real_mux_e2e
@pytest.mark.xdist_group("slow-io")
def test_spawns_real_tmux():
    pass
"""

_CLASS_NESTED = """
import pytest
from conftest import real_mux_e2e


class TestRealTmux:
    @pytest.mark.skipif(False, reason="tmux not available")
    def test_spawns_real_tmux(self):
        pass
"""

_ALIAS_BOUND_GATE = """
import pytest

needs_tmux = pytest.mark.skipif(False, reason="tmux not available")


@needs_tmux
def test_spawns_real_tmux():
    pass
"""

_DECORATOR_GROUPED = """
import pytest
from conftest import real_mux_e2e


@pytest.mark.skipif(False, reason="tmux not available")
@real_mux_e2e
def test_spawns_real_tmux():
    pass
"""

_MODULE_LIST_GROUPED = """
import pytest
from conftest import real_mux_e2e

pytestmark = [
    pytest.mark.skipif(False, reason="stories E2E needs real tmux on Linux"),
    real_mux_e2e,
]


def test_spawns_real_tmux():
    pass
"""

_LITERAL_GROUPED = """
import pytest


@pytest.mark.skipif(False, reason="tmux not available")
@pytest.mark.xdist_group("real_mux_e2e")
def test_spawns_real_tmux():
    pass
"""

_CLASS_GROUPED = """
import pytest
from conftest import real_mux_e2e


@real_mux_e2e
class TestRealTmux:
    @pytest.mark.skipif(False, reason="tmux not available")
    def test_spawns_real_tmux(self):
        pass
"""

_NON_TMUX_GATE = """
import sys

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="requires Windows with psmux on PATH")
def test_not_about_tmux():
    pass
"""

_NON_LITERAL_REASON = """
import pytest

WHY = "tmux not available"


@pytest.mark.skipif(False, reason=WHY)
def test_spawns_real_tmux():
    pass
"""


@pytest.mark.parametrize(
    "source",
    [
        _GATED_UNGROUPED,
        _MODULE_GATED_UNGROUPED,
        _WRONG_GROUP,
        _TWO_GROUPS,
        _CLASS_NESTED,
        _ALIAS_BOUND_GATE,
    ],
    ids=[
        "decorator-gated",
        "module-pytestmark-gated",
        "wrong-group-name",
        "two-groups",
        "class-nested",
        "alias-bound-gate",
    ],
)
def test_guard_flags_an_ungrouped_real_tmux_e2e(source):
    """Must-flag rows, one per form. Three are subtle. `wrong-group-name` IS grouped, so
    a detector asking only "is there an xdist_group mark?" passes it while it runs in a
    group of one. `two-groups` carries the correct mark AND another, which xdist merges
    into a third group name — so "does any mark match?" is also not enough.
    `class-nested` and `alias-bound-gate` are shapes a module-body-only walk and an
    inline-only reason check respectively cannot see at all."""
    assert _scan_source(source, "probe.py") == [("probe.py", "test_spawns_real_tmux", False)]


def test_guard_flags_a_gate_imported_from_conftest():
    """The shared-gate form: `tests/conftest.py` binds a tmux `skipif` and a module
    applies it as a bare decorator — the shape `needs_strict_codec` already ships in.
    `conftest_gates` is passed explicitly because no such gate exists yet; the branch
    under test is the same one the live scan runs."""
    source = """
from conftest import needs_tmux


@needs_tmux
def test_spawns_real_tmux():
    pass
"""
    found = _scan_source(source, "probe.py", conftest_gates=frozenset({"needs_tmux"}))
    assert found == [("probe.py", "test_spawns_real_tmux", False)]


@pytest.mark.parametrize(
    "source",
    [_DECORATOR_GROUPED, _MODULE_LIST_GROUPED, _LITERAL_GROUPED, _CLASS_GROUPED],
    ids=["decorator-grouped", "module-list-grouped", "literal-group-name", "class-grouped"],
)
def test_guard_stays_silent_on_a_correctly_grouped_e2e(source):
    assert _scan_source(source, "probe.py") == [("probe.py", "test_spawns_real_tmux", True)]


def test_guard_ignores_a_gate_that_is_not_about_tmux():
    """The lookalike: a host gate with no tmux in its reason. `psmux` is the live
    case — `test_psmux_live.py` runs in its own non-xdist CI invocation, so pulling
    it into this group would be wrong, and it must not even be seen."""
    assert _scan_source(_NON_TMUX_GATE, "probe.py") == []


def test_guard_cannot_see_a_gate_whose_reason_is_not_a_literal():
    """A RECORDED LIMIT, not an endorsement: a reason read from a constant (or built by
    an f-string) is invisible to a static scan, so such a gate slips past ungrouped.
    Closing it means evaluating arbitrary expressions, which this scan will not do.
    Pinned as a row so the gap is a known boundary rather than a silent surprise — if
    it ever needs closing, this row is the one that changes."""
    assert _scan_source(_NON_LITERAL_REASON, "probe.py") == []
