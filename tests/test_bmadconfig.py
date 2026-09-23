"""ProjectPaths.repo_root / rebased and load_paths(repo_root) — the Phase 1
Workspace-seam foundation. repo_root defaults to project (today's behavior);
rebased re-roots artifacts onto a worktree-style checkout. Plus
worktree_isolation_conflict, the #414 refusal predicate built on the same pair, and
load_paths' two sources: the four-layer central TOML and the legacy YAML (#769)."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest
from conftest import (
    CENTRAL_TEAM_CONFIG,
    CENTRAL_USER_CONFIG,
    NUL_PATH_RESOLVE_FAULTS,
    UNRESOLVABLE,
    install_bmad_central_config,
    install_bmad_config,
    refuse_to_resolve,
)

from bmad_loop import bmadconfig, cli, platform_util
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


def test_diagnostic_text_escapes_nul_and_non_ascii() -> None:
    assert bmadconfig._diagnostic_text("bad-\x00-caf\xe9") == "bad-\\x00-caf\\xe9"


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


@pytest.mark.parametrize("resolve_fault", NUL_PATH_RESOLVE_FAULTS)
@pytest.mark.parametrize("boundary", ["project", "configured-path"])
def test_load_paths_translates_value_error_family_at_canonicalization_boundaries(
    tmp_path: Path, monkeypatch, resolve_fault, boundary: str
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    target = root / "artifacts"
    _write_config(root, implementation_artifacts="{project-root}/artifacts")
    refused = root if boundary == "project" else target
    refuse_to_resolve(monkeypatch, refused, error=resolve_fault)

    with pytest.raises(bmadconfig.BmadConfigError) as excinfo:
        bmadconfig.load_paths(root)

    assert isinstance(excinfo.value.__cause__, type(resolve_fault))
    assert excinfo.value.__cause__.args == resolve_fault.args


def test_run_reports_configured_lone_surrogate_path_on_strict_stderr(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    cfg = root / "_bmad" / "bmm"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(
        'implementation_artifacts: "{project-root}/caf\\u00e9-\\uD800"\n'
        'planning_artifacts: "{project-root}/_bmad-output/planning-artifacts"\n',
        encoding="utf-8",
    )
    configured = root / "caf\xe9-\ud800"
    fault = UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
    refuse_to_resolve(monkeypatch, configured, error=fault)
    stderr_bytes = io.BytesIO()
    stderr = io.TextIOWrapper(stderr_bytes, encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stderr", stderr)

    rc = cli.main(["run", "--project", str(root), "--dry-run"])

    stderr.flush()
    message = stderr_bytes.getvalue().decode("ascii")
    assert rc == 1
    assert "cannot canonicalize the configured path" in message
    assert "caf\\xe9-\\ud800" in message


def test_run_reports_project_root_lone_surrogate_path_on_strict_stderr(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "caf\xe9-\ud800"
    fault = UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
    refuse_to_resolve(monkeypatch, root, error=fault)
    monkeypatch.setattr(cli, "_configure_mux", lambda _project: None)
    stderr_bytes = io.BytesIO()
    stderr = io.TextIOWrapper(stderr_bytes, encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stderr", stderr)

    rc = cli.main(["run", "--project", str(root), "--dry-run"])

    stderr.flush()
    message = stderr_bytes.getvalue().decode("ascii")
    assert rc == 1
    assert "cannot canonicalize the project root" in message
    assert "caf\\xe9-\\ud800" in message


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


# ------------- the four-layer central TOML config (#769, #154) -------------
#
# Fixtures follow BMAD-METHOD v6.12.0 (see `CENTRAL_TEAM_CONFIG` in conftest for the
# installer source they are derived from); resolution follows that tag's
# `src/scripts/config_utils.py` (layers, structural merge) and
# `src/scripts/render_skill.py` `_resolve_short_config` (short-key lookup).

_LAYERS = [str(rel) for rel in bmadconfig.CENTRAL_LAYERS_REL]
_DEFAULT_IMPL = "_bmad-output/implementation-artifacts"


def _write_layer(root: Path, index: int, text: str | bytes) -> Path:
    path = root / bmadconfig.CENTRAL_LAYERS_REL[index]
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def _toml_only(tmp_path: Path) -> Path:
    root = tmp_path / "p"
    root.mkdir()
    _write_layer(root, 0, CENTRAL_TEAM_CONFIG)
    _write_layer(root, 1, CENTRAL_USER_CONFIG)
    return root


def test_load_paths_reads_the_769_toml_only_layout(project) -> None:
    """The reported install: `_bmad/config.toml` and friends, no `_bmad/bmm/`."""
    install_bmad_central_config(project)
    root = project.project.resolve()
    assert not (root / bmadconfig.LEGACY_CONFIG_REL).exists()

    loaded = bmadconfig.load_paths(project.project)

    assert loaded.implementation_artifacts == root / _DEFAULT_IMPL
    assert loaded.planning_artifacts == root / "_bmad-output" / "planning-artifacts"
    assert loaded.output_folder == root / "_bmad-output"
    assert loaded.repo_root == root


def test_toml_only_and_yaml_only_installs_resolve_identically(project, tmp_path) -> None:
    """The same install answers through either source give the same snapshot."""
    install_bmad_central_config(project)
    from_toml = bmadconfig.load_paths(project.project)
    (project.project / "_bmad" / "config.toml").unlink()
    (project.project / "_bmad" / "config.user.toml").unlink()
    (project.project / "_bmad" / "custom" / "config.toml").unlink()
    (project.project / "_bmad" / "custom" / "config.user.toml").unlink()
    install_bmad_config(project)
    assert bmadconfig.load_paths(project.project) == from_toml


@pytest.mark.parametrize("upper", [1, 2, 3])
def test_each_layer_overrides_the_one_below(tmp_path: Path, upper: int) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write_layer(root, 0, CENTRAL_TEAM_CONFIG)
    if upper > 1:
        _write_layer(
            root, upper - 1, '[modules.bmm]\nimplementation_artifacts = "{project-root}/lower"\n'
        )
    _write_layer(root, upper, '[modules.bmm]\nimplementation_artifacts = "{project-root}/upper"\n')

    loaded = bmadconfig.load_paths(root)

    assert loaded.implementation_artifacts == root.resolve() / "upper"
    # tables merge rather than replace: the base layer's sibling key survives
    assert loaded.planning_artifacts == root.resolve() / "_bmad-output" / "planning-artifacts"


def test_a_lower_layer_does_not_beat_a_higher_one(tmp_path: Path) -> None:
    """Order, not presence: the base layer's value loses to the custom user layer's
    even with the layers between them silent."""
    root = tmp_path / "p"
    root.mkdir()
    _write_layer(root, 0, CENTRAL_TEAM_CONFIG)
    _write_layer(root, 3, '[core]\noutput_folder = "{project-root}/mine"\n')
    assert bmadconfig.load_paths(root).output_folder == root.resolve() / "mine"


@pytest.mark.parametrize(
    ("layer", "text", "expected"),
    [
        # the #154 shape: the same key under [core] and [modules.bmm]
        (
            0,
            '[core]\nimplementation_artifacts = "{project-root}/core"\n',
            "core.implementation_artifacts",
        ),
        # an override layer adding the key in a section of its own
        (
            2,
            '[core]\nimplementation_artifacts = "{project-root}/core"\n',
            "core.implementation_artifacts",
        ),
        # arbitrarily deep, as the renderer searches the whole tree
        (
            3,
            '[agents.bmad-agent-dev]\nimplementation_artifacts = "x"\n',
            "agents.bmad-agent-dev.implementation_artifacts",
        ),
    ],
)
def test_a_key_in_two_sections_is_ambiguous(
    tmp_path: Path, layer: int, text: str, expected: str
) -> None:
    """The renderer refuses a short key matched more than once, and so does this —
    no "`[modules.bmm]` beats `[core]`" tie-break (#154's obsolete proposal), and no
    falling back to the YAML, which is present here and would resolve cleanly."""
    root = _toml_only(tmp_path)
    _write_config(root)  # a valid legacy YAML beside it must not rescue the load
    base = (root / bmadconfig.CENTRAL_LAYERS_REL[0]).read_text(encoding="utf-8")
    if layer == 0:
        _write_layer(root, 0, base.replace("[core]\n", text, 1))
    else:
        _write_layer(root, layer, text)

    with pytest.raises(bmadconfig.BmadConfigError) as excinfo:
        bmadconfig.load_paths(root)

    message = str(excinfo.value)
    assert "ambiguous config value `implementation_artifacts` found at:" in message
    assert "modules.bmm.implementation_artifacts" in message
    assert expected in message
    # every location names the layer it came from
    assert str(root.resolve() / _LAYERS[0]) in message
    assert str(root.resolve() / _LAYERS[layer]) in message


def test_the_same_path_in_two_layers_is_an_override_not_an_ambiguity(tmp_path: Path) -> None:
    root = _toml_only(tmp_path)
    _write_layer(root, 2, '[core]\noutput_folder = "{project-root}/team-out"\n')
    assert bmadconfig.load_paths(root).output_folder == root.resolve() / "team-out"


@pytest.mark.parametrize("key", ["implementation_artifacts", "planning_artifacts"])
def test_a_required_key_absent_from_both_sources_fails(tmp_path: Path, key: str) -> None:
    root = _toml_only(tmp_path)
    kept = CENTRAL_TEAM_CONFIG.replace(f"{key} = ", f"unused_{key} = ")
    _write_layer(root, 0, kept)

    with pytest.raises(bmadconfig.BmadConfigError, match=f"missing `{key}`") as excinfo:
        bmadconfig.load_paths(root)
    assert str(root.resolve() / bmadconfig.LEGACY_CONFIG_REL) in str(excinfo.value)


def test_optional_keys_absent_from_both_sources_keep_their_defaults(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write_layer(
        root,
        0,
        '[modules.bmm]\nimplementation_artifacts = "{project-root}/i"\n'
        'planning_artifacts = "{project-root}/pl"\n',
    )
    loaded = bmadconfig.load_paths(root)
    assert loaded.output_folder == root.resolve() / "_bmad-output"
    assert loaded.repo_root == root.resolve()


@pytest.mark.parametrize(
    ("value", "complaint"),
    [
        ('""', "must not be empty"),
        ('"   "', "must not be empty"),
        ("42", "must be a string, got int"),
        ("true", "must be a string, got bool"),
        ('["{project-root}/a"]', "must be a string, got list"),
        ('{ path = "{project-root}/a" }', "must be a string, got table"),
    ],
)
@pytest.mark.parametrize("key", ["implementation_artifacts", "output_folder", "repo_root"])
def test_a_blank_or_wrong_typed_toml_value_refuses_instead_of_falling_back(
    tmp_path: Path, key: str, value: str, complaint: str
) -> None:
    """Present-but-unusable is an error, not an absence: the legacy YAML beside it
    carries a perfectly good value for every key and must not be consulted."""
    root = tmp_path / "p"
    root.mkdir()
    _write_config(
        root,
        output_folder="{project-root}/yaml-out",
        repo_root="{project-root}",
    )
    body = (
        CENTRAL_TEAM_CONFIG.replace(f"{key} = ", f"unused_{key} = ")
        + f"\n[custom]\n{key} = {value}\n"
    )
    _write_layer(root, 0, body)

    with pytest.raises(bmadconfig.BmadConfigError, match=complaint) as excinfo:
        bmadconfig.load_paths(root)
    message = str(excinfo.value)
    assert f"custom.{key}" in message, "the error names the key"
    assert str(root.resolve() / _LAYERS[0]) in message, "the error names the file"


def test_an_array_of_tables_is_opaque_to_the_lookup(tmp_path: Path) -> None:
    """The renderer never descends into arrays, so a same-named key inside an array
    of tables is neither a match nor an ambiguity — and neither is it here."""
    root = _toml_only(tmp_path)
    _write_layer(
        root,
        2,
        '[[extras]]\nid = "a"\nimplementation_artifacts = "{project-root}/nope"\n',
    )
    loaded = bmadconfig.load_paths(root)
    assert loaded.implementation_artifacts == root.resolve() / _DEFAULT_IMPL


def test_a_higher_layer_scalar_replaces_a_lower_table(tmp_path: Path) -> None:
    """`structural_merge` replaces unless both sides are tables: a scalar `modules`
    in an override layer removes `[modules.bmm]` wholesale, so its keys are gone."""
    root = _toml_only(tmp_path)
    _write_layer(root, 3, 'modules = "gone"\n')
    with pytest.raises(bmadconfig.BmadConfigError, match="missing `implementation_artifacts`"):
        bmadconfig.load_paths(root)


# --- mixed installs: TOML wins per key, YAML fills only what TOML lacks ---


@pytest.mark.parametrize(
    "key", ["implementation_artifacts", "planning_artifacts", "output_folder", "repo_root"]
)
def test_mixed_install_toml_value_wins_over_yaml(tmp_path: Path, key: str) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write_config(root, **{key: "{project-root}/from-yaml"})
    _write_layer(root, 0, CENTRAL_TEAM_CONFIG)
    _write_layer(root, 2, f'[custom]\n{key} = "{{project-root}}/from-toml"\n')
    if key != "repo_root":
        # move the base layer's own entry aside so the custom one is the only match
        base = CENTRAL_TEAM_CONFIG.replace(f"{key} = ", f"unused_{key} = ")
        _write_layer(root, 0, base)

    loaded = bmadconfig.load_paths(root)
    assert getattr(loaded, key) == root.resolve() / "from-toml"


@pytest.mark.parametrize(
    "key", ["implementation_artifacts", "planning_artifacts", "output_folder", "repo_root"]
)
def test_mixed_install_yaml_fills_a_key_absent_from_toml(tmp_path: Path, key: str) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write_config(root, **{key: "{project-root}/from-yaml"})
    _write_layer(root, 0, CENTRAL_TEAM_CONFIG.replace(f"{key} = ", f"unused_{key} = "))

    loaded = bmadconfig.load_paths(root)
    assert getattr(loaded, key) == root.resolve() / "from-yaml"
    if key != "implementation_artifacts":
        # and the keys TOML does carry are still TOML's
        assert loaded.implementation_artifacts == root.resolve() / _DEFAULT_IMPL


def test_mixed_install_reports_a_yaml_filled_key_by_its_yaml_origin(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    target = tmp_path / "elsewhere"
    _write_config(root, repo_root=str(target))
    _write_layer(root, 0, CENTRAL_TEAM_CONFIG)
    refuse_to_resolve(monkeypatch, target)

    with pytest.raises(bmadconfig.BmadConfigError, match="cannot canonicalize") as excinfo:
        bmadconfig.load_paths(root)
    assert f"`repo_root` in {root.resolve() / bmadconfig.LEGACY_CONFIG_REL}" in str(excinfo.value)


@pytest.mark.parametrize("layer", [0, 1, 2, 3])
def test_malformed_toml_in_any_layer_refuses_instead_of_falling_back(
    tmp_path: Path, layer: int
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write_config(root)  # a valid legacy YAML must not rescue the load
    for index in range(4):
        _write_layer(root, index, CENTRAL_TEAM_CONFIG if index == 0 else "")
    _write_layer(root, layer, "[core\nbroken = \n")

    with pytest.raises(bmadconfig.BmadConfigError, match="invalid TOML in") as excinfo:
        bmadconfig.load_paths(root)
    assert str(root.resolve() / _LAYERS[layer]) in str(excinfo.value)


@pytest.mark.parametrize("layer", [0, 1, 2, 3])
def test_undecodable_toml_in_any_layer_refuses_instead_of_falling_back(
    tmp_path: Path, layer: int
) -> None:
    """tomllib raises UnicodeDecodeError — a ValueError, not TOMLDecodeError — so it
    needs its own conversion or it escapes every `except BmadConfigError`."""
    root = tmp_path / "p"
    root.mkdir()
    _write_config(root)
    _write_layer(root, 0, CENTRAL_TEAM_CONFIG)
    _write_layer(root, layer, b'[core]\nuser_name = "\xff\xfe"\n')

    with pytest.raises(bmadconfig.BmadConfigError, match="not valid UTF-8") as excinfo:
        bmadconfig.load_paths(root)
    assert str(root.resolve() / _LAYERS[layer]) in str(excinfo.value)


def test_a_layer_that_is_not_a_file_refuses(tmp_path: Path) -> None:
    root = _toml_only(tmp_path)
    (root / bmadconfig.CENTRAL_LAYERS_REL[2]).mkdir(parents=True)
    with pytest.raises(bmadconfig.BmadConfigError, match="not a file"):
        bmadconfig.load_paths(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize("target", ["missing.toml", "config.toml"], ids=["dangling", "loop"])
def test_a_layer_symlink_that_resolves_to_no_file_refuses(tmp_path: Path, target: str) -> None:
    """`exists()` follows the link and reads a dangling (or self-looping) one as
    absent, which would let the YAML fill the key: present-but-unreadable must
    refuse, like a directory at the layer path does.

    Ablation: gate on `exists()` alone and the load succeeds off the YAML."""
    root = _toml_only(tmp_path)
    _write_config(root)  # a valid legacy YAML must not rescue the load
    layer = root / bmadconfig.CENTRAL_LAYERS_REL[2]
    layer.parent.mkdir(parents=True, exist_ok=True)
    layer.symlink_to(target)  # relative to custom/: nothing there, or itself

    with pytest.raises(bmadconfig.BmadConfigError, match="resolves to no file") as excinfo:
        bmadconfig.load_paths(root)
    assert str(root.resolve() / _LAYERS[2]) in str(excinfo.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_layer_symlink_to_a_real_file_is_read(tmp_path: Path) -> None:
    root = _toml_only(tmp_path)
    shared = root / "shared-override.toml"
    shared.write_text(
        '[modules.bmm]\nimplementation_artifacts = "{project-root}/linked-impl"\n',
        encoding="utf-8",
    )
    layer = root / bmadconfig.CENTRAL_LAYERS_REL[3]
    layer.parent.mkdir(parents=True, exist_ok=True)
    layer.symlink_to(shared)

    assert bmadconfig.load_paths(root).implementation_artifacts == root.resolve() / "linked-impl"


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions; root bypasses them"
)
def test_a_layer_behind_an_unreadable_directory_refuses_typed(tmp_path: Path) -> None:
    """An unreadable layer directory is neither absent nor an untyped crash: through
    3.13 `exists()` raises PermissionError past every `except BmadConfigError`, and
    on 3.14+ it reads the layer as absent so the YAML fills the key.

    Ablation: probe with `exists()` again and this raises PermissionError (<=3.13) or
    loads off the YAML (3.14+)."""
    root = _toml_only(tmp_path)
    _write_config(root)  # a valid legacy YAML must not rescue the load
    custom = root / bmadconfig.CENTRAL_LAYERS_REL[2].parent
    custom.mkdir(parents=True)
    custom.chmod(0)
    try:
        with pytest.raises(bmadconfig.BmadConfigError, match="cannot read") as excinfo:
            bmadconfig.load_paths(root)
    finally:
        custom.chmod(0o755)
    assert str(root.resolve() / _LAYERS[2]) in str(excinfo.value)


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions; root bypasses them"
)
@pytest.mark.parametrize("central", [True, False], ids=["mixed", "yaml-only"])
def test_an_unreadable_legacy_directory_refuses_typed(tmp_path: Path, central: bool) -> None:
    """The legacy YAML probe gets the layers' typing: through 3.13 `is_file()` raises
    PermissionError past every `except BmadConfigError`, and on 3.14+ it reads the
    YAML as absent, misreporting an unreadable fallback as a missing key.

    Ablation: probe with `config_path.is_file()` again and this raises
    PermissionError (<=3.13) or the wrong BmadConfigError (3.14+)."""
    root = tmp_path / "p"
    root.mkdir()
    if central:  # a TOML that omits the path keys, so the lookup falls to the YAML
        _write_layer(root, 0, b'[core]\nuser_name = "me"\n')
    _write_config(root)
    legacy_dir = root / bmadconfig.LEGACY_CONFIG_REL.parent
    legacy_dir.chmod(0)
    try:
        with pytest.raises(bmadconfig.BmadConfigError, match="cannot read") as excinfo:
            bmadconfig.load_paths(root)
    finally:
        legacy_dir.chmod(0o755)
    assert str(root.resolve() / bmadconfig.LEGACY_CONFIG_REL) in str(excinfo.value)


def test_no_toml_and_no_yaml_names_both_expected_locations(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    with pytest.raises(bmadconfig.BmadConfigError, match="BMAD config not found") as excinfo:
        bmadconfig.load_paths(root)
    message = str(excinfo.value)
    assert str(root.resolve() / _LAYERS[0]) in message
    assert str(root.resolve() / bmadconfig.LEGACY_CONFIG_REL) in message


def test_yaml_only_keeps_its_falsy_means_absent_semantics(tmp_path: Path) -> None:
    """With no TOML layer the legacy reading is untouched: a blank YAML value is an
    absent key, not the refusal a blank TOML value gets."""
    root = tmp_path / "p"
    root.mkdir()
    _write_config(root, output_folder="")
    assert bmadconfig.load_paths(root).output_folder == root.resolve() / "_bmad-output"


# --- the #552 / worktree guarantees hold for a TOML-sourced path ---


def test_a_toml_path_that_cannot_canonicalize_refuses_naming_file_and_key(
    tmp_path: Path, monkeypatch
) -> None:
    root = _toml_only(tmp_path)
    target = root.resolve() / "_bmad-output" / "implementation-artifacts"
    refuse_to_resolve(monkeypatch, target)

    with pytest.raises(
        bmadconfig.BmadConfigError, match="cannot canonicalize the configured path"
    ) as excinfo:
        bmadconfig.load_paths(root)
    message = str(excinfo.value)
    assert "`modules.bmm.implementation_artifacts`" in message
    assert str(root.resolve() / _LAYERS[0]) in message


def test_a_toml_path_escaping_the_project_is_canonical_and_stays_put_on_rebase(
    tmp_path: Path,
) -> None:
    """`{project-root}/../shared` escapes the tree: it canonicalizes to the shared
    directory, and `rebased` leaves it where it is as for any external path."""
    root = _toml_only(tmp_path)
    _write_layer(
        root, 2, '[modules.bmm]\nimplementation_artifacts = "{project-root}/../shared/impl"\n'
    )
    loaded = bmadconfig.load_paths(root)
    assert loaded.implementation_artifacts == (tmp_path / "shared" / "impl").resolve()

    wt = tmp_path / "wt"
    rebased = loaded.rebased(wt)
    assert rebased.implementation_artifacts == (tmp_path / "shared" / "impl").resolve()
    assert rebased.planning_artifacts == (wt / "_bmad-output" / "planning-artifacts").resolve()


def test_a_symlinked_toml_path_is_classified_by_its_target(tmp_path: Path) -> None:
    """Spelled inside the project, pointing outside: canonicalization follows the
    link, so `rebased` files it as external rather than per-checkout."""
    root = _toml_only(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as e:  # Windows without SeCreateSymbolicLink / developer mode
        pytest.skip(f"cannot create a symlink here: {e}")
    _write_layer(root, 3, '[modules.bmm]\nimplementation_artifacts = "{project-root}/linked"\n')

    loaded = bmadconfig.load_paths(root)
    assert loaded.implementation_artifacts == outside.resolve()
    assert loaded.rebased(tmp_path / "wt").implementation_artifacts == outside.resolve()


def test_rebased_reroots_a_toml_sourced_config(tmp_path: Path) -> None:
    root = _toml_only(tmp_path)
    loaded = bmadconfig.load_paths(root)
    wt = tmp_path / "worktree"

    rebased = loaded.rebased(wt)

    assert rebased.project == rebased.repo_root == wt.resolve()
    assert rebased.implementation_artifacts == (wt / _DEFAULT_IMPL).resolve()
    assert rebased.output_folder == (wt / "_bmad-output").resolve()
    assert rebased.sprint_status == (wt / _DEFAULT_IMPL / "sprint-status.yaml").resolve()
