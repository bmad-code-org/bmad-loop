"""ProjectPaths.repo_root / rebased and load_paths(repo_root) — the Phase 1
Workspace-seam foundation. repo_root defaults to project (today's behavior);
rebased re-roots artifacts onto a worktree-style checkout. Plus
worktree_isolation_conflict, the #414 refusal predicate built on the same pair."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import UNRESOLVABLE, install_bmad_config, refuse_to_resolve

from bmad_loop import bmadconfig, platform_util
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.workspace import Workspace


def test_repo_root_defaults_to_project(tmp_path: Path) -> None:
    paths = ProjectPaths(
        project=tmp_path / "p",
        implementation_artifacts=tmp_path / "p" / "impl",
        planning_artifacts=tmp_path / "p" / "plan",
    )
    assert paths.repo_root == paths.project


def test_repo_root_explicit_is_kept(tmp_path: Path) -> None:
    paths = ProjectPaths(
        project=tmp_path / "p",
        implementation_artifacts=tmp_path / "p" / "impl",
        planning_artifacts=tmp_path / "p" / "plan",
        repo_root=tmp_path / "repo",
    )
    assert paths.repo_root == tmp_path / "repo"


def test_load_paths_repo_root_defaults_to_project(project) -> None:
    install_bmad_config(project)
    loaded = bmadconfig.load_paths(project.project)
    assert loaded.repo_root == project.project.resolve()


def test_load_paths_reads_repo_root_key(project) -> None:
    install_bmad_config(project)
    cfg = project.project / "_bmad" / "bmm" / "config.yaml"
    cfg.write_text(cfg.read_text() + "repo_root: '{project-root}/sub'\n")
    loaded = bmadconfig.load_paths(project.project)
    assert loaded.repo_root == (project.project / "sub").resolve()


def test_load_paths_non_utf8_config_raises_bmad_config_error(project) -> None:
    """An undecodable config.yaml is as much a BmadConfigError as malformed YAML.
    `read_text` raises UnicodeDecodeError, which is a ValueError and NOT an OSError,
    so raw it slips past every `except (BmadConfigError, OSError)` degrade handler —
    the `cli` command tails, `tui/data.py`'s project scans — and kills the process
    instead of reporting a bad config. Asserting the type is the point:
    UnicodeDecodeError is an exception too."""
    install_bmad_config(project)
    cfg = project.project / "_bmad" / "bmm" / "config.yaml"
    cfg.write_bytes(b"implementation_artifacts: '\xff\xfe'\n")
    with pytest.raises(bmadconfig.BmadConfigError, match="not valid UTF-8"):
        bmadconfig.load_paths(project.project)


def test_load_paths_non_mapping_config_raises_bmad_config_error(project) -> None:
    """A syntactically valid YAML sequence is still an invalid BMAD config.

    The typed boundary matters to best-effort observers such as interactive resolve:
    they catch ``BmadConfigError`` and can fall back to the run's recorded roots.
    """
    install_bmad_config(project)
    cfg = project.project / "_bmad" / "bmm" / "config.yaml"
    cfg.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(bmadconfig.BmadConfigError, match="top-level mapping"):
        bmadconfig.load_paths(project.project)


def test_rebased_reroots_project_and_artifacts(tmp_path: Path) -> None:
    src = tmp_path / "main"
    paths = ProjectPaths(
        project=src,
        implementation_artifacts=src / "out" / "impl",
        planning_artifacts=src / "out" / "plan",
    )
    wt = tmp_path / "worktree"
    rebased = paths.rebased(wt)

    assert rebased.project == wt.resolve()
    assert rebased.repo_root == wt.resolve()
    assert rebased.implementation_artifacts == (wt / "out" / "impl").resolve()
    assert rebased.planning_artifacts == (wt / "out" / "plan").resolve()
    # derived artifact files follow the rebase
    assert rebased.sprint_status == (wt / "out" / "impl" / "sprint-status.yaml").resolve()
    assert rebased.deferred_work == (wt / "out" / "impl" / "deferred-work.md").resolve()


def test_rebased_leaves_external_artifacts_in_place(tmp_path: Path) -> None:
    src = tmp_path / "main"
    external = tmp_path / "shared" / "impl"
    paths = ProjectPaths(
        project=src,
        implementation_artifacts=external,
        planning_artifacts=src / "out" / "plan",
    )
    rebased = paths.rebased(tmp_path / "worktree")
    # configured outside the project tree → shared, not per-checkout
    assert rebased.implementation_artifacts == external
    assert rebased.planning_artifacts == (tmp_path / "worktree" / "out" / "plan").resolve()


def test_workspace_default_uses_repo_root(tmp_path: Path) -> None:
    paths = ProjectPaths(
        project=tmp_path / "p",
        implementation_artifacts=tmp_path / "p" / "impl",
        planning_artifacts=tmp_path / "p" / "plan",
        repo_root=tmp_path / "repo",
    )
    ws = Workspace.default(paths)
    assert ws.root == tmp_path / "repo"
    assert ws.paths is paths


def test_worktree_isolation_conflict_compares_normalized_paths(tmp_path: Path) -> None:
    """A refusal gate's false positives are worse than the bug it forecloses (#414):
    this one would refuse an ordinary isolated project whose `repo_root` merely spells
    the same directory a different way. `load_paths` resolves both sides, but nothing
    obliges a hand-built ProjectPaths — or a future caller — to have done so."""
    (tmp_path / "p").mkdir()
    paths = ProjectPaths(
        project=tmp_path / "p",
        implementation_artifacts=tmp_path / "p" / "impl",
        planning_artifacts=tmp_path / "p" / "plan",
        repo_root=tmp_path / "p" / ".." / "p",
    )
    assert paths.repo_root != paths.project, "the two spellings really are different"
    assert bmadconfig.worktree_isolation_conflict(paths, "worktree") is None


# ------------- a project root the OS refuses to canonicalize (#552) -------------


def _write_config(root: Path, **keys: str) -> None:
    """The `_bmad/bmm/config.yaml` `load_paths` reads, with arbitrary key overrides —
    `install_bmad_config` hard-codes `{project-root}` forms, and these rows need a
    config string that names somewhere else entirely."""
    body = {
        "implementation_artifacts": "{project-root}/_bmad-output/implementation-artifacts",
        "planning_artifacts": "{project-root}/_bmad-output/planning-artifacts",
        **keys,
    }
    cfg = root / "_bmad" / "bmm"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text(
        "".join(f"{k}: '{v}'\n" for k, v in body.items()), encoding="utf-8"
    )


def test_load_paths_raises_typed_when_the_root_cannot_canonicalize(
    tmp_path: Path, monkeypatch
) -> None:
    """Hardening `cli._project` alone would not have been enough: `load_paths`
    re-resolves the very same root, so on the failing host the second call raised a
    bare OSError — straight past every caller's `except BmadConfigError` and into
    `main()`'s backstop. The raise stays, but *typed*, which is the whole fix: every
    caller already catches BmadConfigError, `cmd_validate` records the failure and
    still reaches the platform preflight that names the host, and `diagnose` never
    loads paths at all. Degrading instead was tried and retired — a lexical root
    beside any canonically spelled member is a half-canonical snapshot that `rebased`
    mis-files, sending a worktree-isolated run's writes into the original checkout,
    and each review round found another route to that split."""
    root = tmp_path / "p"
    root.mkdir()
    _write_config(root)
    refuse_to_resolve(monkeypatch, root)

    # the full prefix, not just "cannot canonicalize": _resolve raises a message
    # sharing that stem for a configured path, and this row pins the ROOT boundary
    with pytest.raises(bmadconfig.BmadConfigError, match="cannot canonicalize the project root"):
        bmadconfig.load_paths(root)


def test_worktree_isolation_conflict_degrades_rather_than_re_raising(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`cmd_validate` runs this gate *before* the platform preflight, so under
    `isolation = "worktree"` a raise here put the #332 finding back out of reach for
    exactly the operators it is written for. The default shape is what is pinned:
    no `repo_root` key, so both sides are the project and the gate must pass."""
    root = tmp_path / "p"
    root.mkdir()
    paths = ProjectPaths(
        project=root,
        implementation_artifacts=root / "impl",
        planning_artifacts=root / "plan",
        output_folder=root / "out",
    )
    refuse_to_resolve(monkeypatch, root)

    assert bmadconfig.worktree_isolation_conflict(paths, "worktree") is None
    assert capsys.readouterr().err == "", "the default shape must not ask the OS at all"


def test_worktree_isolation_conflict_survives_a_flapping_resolve(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The two `resolve_or_lexical` calls below the short-circuit are independent, and
    the guard catches every `OSError` rather than only a persistent WinError 64. So a
    provider that fails on one call and answers on the next would have one side degrade
    to lexical while the other canonicalized — making a path unequal to *itself* and
    refusing an ordinary isolated run with the #414 text. Comparing raw paths first is
    what keeps the default shape out of that window.

    The stub fails exactly once, which is the whole scenario: `repo_root` and `project`
    are the same object here, so any disagreement between the two calls is spurious by
    construction."""
    # The root must be reached through a symlink or the row is vacuous: on a canonical
    # tmp_path the lexical and resolved spellings are the same string, so the two sides
    # would agree even when one degraded and the other did not, and deleting the
    # short-circuit would leave this green.
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "p"
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError as e:  # Windows without SeCreateSymbolicLink / developer mode
        pytest.skip(f"cannot create a symlink here: {e}")
    assert root.resolve() != root, "the two spellings really are different"

    paths = ProjectPaths(
        project=root,
        implementation_artifacts=root / "impl",
        planning_artifacts=root / "plan",
        output_folder=root / "out",
    )
    real = Path.resolve
    failures = [OSError(0, UNRESOLVABLE, None, 64)]

    def flaky(self, strict: bool = False):
        if str(self) == str(root) and failures:
            raise failures.pop()
        return real(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", flaky)
    monkeypatch.setattr(platform_util, "_LEXICAL_FALLBACK_NOTED", set())

    assert bmadconfig.worktree_isolation_conflict(paths, "worktree") is None
    assert failures, "the flap never fired — the short-circuit answered first"


@pytest.mark.parametrize(
    "spelling",
    [
        # an absolute path elsewhere: a config string can name a dead share of its
        # own while `--project` points at a perfectly healthy local directory
        "external-absolute",
        # the round-4 shape: spelled *inside* the project, where an in-tree symlink
        # or junction can carry it to a dead share outside — degrading to this
        # spelling made `rebased` file it as internal, and a worktree-isolated run
        # then wrote a worktree-local directory instead of the configured one
        "in-tree-token",
    ],
)
def test_load_paths_refuses_a_config_path_it_cannot_canonicalize(
    spelling: str, tmp_path: Path, monkeypatch
) -> None:
    """`_resolve` takes the same boundary as the project root, and not for the same
    reason: a config string the OS cannot canonicalize has an *unknowable* location —
    its lexical spelling can sit inside the project while its target lies outside —
    so any in-tree/external classification read off the spelling is a guess, and
    `rebased` acting on a wrong guess is a silent wrong-directory write. Pinned with
    the project root left resolvable so only the config string is what refuses."""
    root = tmp_path / "p"
    root.mkdir()
    if spelling == "external-absolute":
        target = tmp_path / "elsewhere" / "impl"
        _write_config(root, implementation_artifacts=str(target))
    else:
        target = root.resolve() / "artifacts"
        _write_config(root, implementation_artifacts="{project-root}/artifacts")
    refuse_to_resolve(monkeypatch, target)

    with pytest.raises(bmadconfig.BmadConfigError, match="cannot canonicalize the configured"):
        bmadconfig.load_paths(root)


def test_load_paths_refuses_a_degraded_root_beside_canonically_spelled_config_paths(
    tmp_path: Path, monkeypatch
) -> None:
    """The route no per-member plumbing could cover, pinned so the raise never quietly
    becomes a degrade again: an *explicit absolute* artifact path written in
    config.yaml in canonical spelling needs no `.resolve()` call to arrive canonical.
    Had the root degraded to its lexical spelling instead of raising, the snapshot
    would be half-canonical with the two spellings either side of the symlink —
    `rebased`'s `relative_to` files the in-tree artifact dir as external, and a
    worktree-isolated run writes into the original checkout. Silent, and a write.
    Refusing the root is the one boundary that closes this route and every future
    one at once.

    The symlinked root is what makes the row bite: on a canonical `tmp_path` the
    lexical and canonical spellings are the same string, and the row would stay green
    with the raise ablated back to a degrade."""
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "p"
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError as e:  # Windows without SeCreateSymbolicLink / developer mode
        pytest.skip(f"cannot create a symlink here: {e}")
    # The operator wrote the artifact dir absolutely, already in canonical spelling —
    # no resolve() is involved in it arriving canonical. Only the root refuses.
    _write_config(
        root,
        implementation_artifacts=str(target / "_bmad-output" / "implementation-artifacts"),
    )
    refuse_to_resolve(monkeypatch, root)

    # the full prefix: this row pins the ROOT refusing, not a member's shared stem
    with pytest.raises(bmadconfig.BmadConfigError, match="cannot canonicalize the project root"):
        bmadconfig.load_paths(root)
