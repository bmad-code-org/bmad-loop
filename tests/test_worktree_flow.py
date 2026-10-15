"""Unit tests for the WorktreeFlow collaborator (issue #244 F-3/F-9a).

WorktreeFlow was carved out of Engine's worktree isolation/integration cluster.
These exercise it in isolation — built from narrow deps + stub engine callbacks,
no Engine instance — which is the point of the extraction. End-to-end behavior
under a real Engine stays covered by test_engine_worktree.py.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import NUL_PATH_RESOLVE_FAULTS, git, refuse_to_resolve

from bmad_loop import artifact_publication, platform_util, verify
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.gates import ATTENTION_FILE
from bmad_loop.install import provision_worktree as install_provision_worktree
from bmad_loop.model import Phase, StoryTask
from bmad_loop.policy import GatesPolicy, LimitsPolicy, NotifyPolicy, Policy, ScmPolicy
from bmad_loop.workspace import (
    UnitWorkspace,
    Workspace,
    open_unit_workspace,
    unit_worktrees_dir,
)
from bmad_loop.worktree_flow import (
    WorktreeFlow,
    _artifact_seed_dropped,
    _pinned_config_edits,
    _pinned_config_forensics,
    _setup_mcp_agent_id,
    _uncarried_ledger_changes,
    provision_worktree,
)

QUIET = NotifyPolicy(desktop=False, file=True)


def _policy(**scm) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(**scm),
        limits=LimitsPolicy(),
    )


class _RecordingJournal:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def append(self, event: str, **fields) -> None:
        self.entries.append((event, fields))

    def events(self) -> list[str]:
        return [e for e, _ in self.entries]

    def fields(self, event: str) -> dict:
        for e, f in self.entries:
            if e == event:
                return f
        raise KeyError(event)


class _FakeProfile:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeAdapter:
    """A dev/review adapter; ``name=None`` mimics a test fake with no CLI profile."""

    def __init__(self, name: str | None = None) -> None:
        self.profile = _FakeProfile(name) if name is not None else None


class _Pause(Exception):
    """Stand-in for the engine's RunPaused, raised by the injected escalation_pause
    so these tests need not import the engine."""

    def __init__(self, reason: str, story_key: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.story_key = story_key


def _make_flow(
    tmp_path: Path,
    *,
    policy: Policy | None = None,
    paths=None,
    state=None,
    journal: _RecordingJournal | None = None,
    adapters_get=None,
    registry=None,
    open_unit_workspace=None,
    workspace=None,
):
    """Build a WorktreeFlow wired to recording stubs. The returned flow carries a
    ``.calls`` namespace tallying the injected callbacks for assertions."""
    calls = SimpleNamespace(
        saves=0,
        emits=[],
        gates=[],
        carries=[],
        pauses=[],
        workspaces=[workspace],
    )

    def _save() -> None:
        calls.saves += 1

    def _emit(stage, task=None, **fields):
        calls.emits.append(stage)
        return None

    def _gate_unit(task) -> bool:
        calls.gates.append(task)
        return True

    def _carry(task) -> None:
        calls.carries.append(task)

    def _pause(reason, story_key="", *, cause=None):
        calls.pauses.append((reason, story_key))
        raise _Pause(reason, story_key)

    flow = WorktreeFlow(
        paths=(
            paths if paths is not None else SimpleNamespace(repo_root=tmp_path, project=tmp_path)
        ),
        policy=policy if policy is not None else _policy(),
        state=(
            state
            if state is not None
            else SimpleNamespace(target_branch="", run_id="run-1", tasks={}, source="sprint-status")
        ),
        journal=journal if journal is not None else _RecordingJournal(),
        run_dir=tmp_path,
        registry=(
            registry
            if registry is not None
            else SimpleNamespace(seed_files=lambda: [], seed_globs=lambda: [])
        ),
        adapters_get=(
            adapters_get
            if adapters_get is not None
            else (lambda: {"dev": _FakeAdapter(), "review": _FakeAdapter()})
        ),
        open_unit_workspace=(
            open_unit_workspace if open_unit_workspace is not None else (lambda *a, **k: None)
        ),
        emit=_emit,
        save=_save,
        gate_unit=_gate_unit,
        carry_isolated_ledger_writes=_carry,
        escalation_pause=_pause,
        workspace_get=lambda: calls.workspaces[-1],
        workspace_set=lambda ws: calls.workspaces.append(ws),
    )
    flow.calls = calls
    return flow


# --------------------------------------------------------------- pure predicates


def test_isolated_reflects_policy(tmp_path):
    assert _make_flow(tmp_path, policy=_policy(isolation="worktree")).isolated is True
    assert _make_flow(tmp_path, policy=_policy(isolation="none")).isolated is False


def test_failed_diff_max_bytes_caps_and_uncaps(tmp_path):
    assert _make_flow(tmp_path, policy=_policy(failed_diff_max_mb=5)).failed_diff_max_bytes() == (
        5 * 1_048_576
    )
    uncapped = _make_flow(tmp_path, policy=_policy(failed_diff_unlimited=True))
    assert uncapped.failed_diff_max_bytes() is None


def test_merge_message_format(tmp_path):
    flow = _make_flow(tmp_path, state=SimpleNamespace(target_branch="main", run_id="r", tasks={}))
    task = StoryTask(story_key="1-1", epic=1)
    task.branch = "bmad-loop/1-1"
    assert flow.merge_message(task) == "Merge bmad-loop/1-1 into main (bmad-loop)"


# ------------------------------------------------------------------- ledger seed
#
# `_ledger_seed` decides, per unit, whether the deferred-work ledger has to be
# copied into the checkout because git will not carry it there (#426). Unit-level
# because each exclusion has a distinct reason a run-level assertion blurs: two of
# them are silent in the journal by design.


def _artifact_flow(tmp_path, *, artifacts: Path | None = None) -> WorktreeFlow:
    """A flow over BMAD-shaped paths, shared by the ledger- and board-seed rows —
    both seeds decide over the same artifacts dir, and `artifacts` moves it out of
    the project tree for the exclusion each has for that case."""
    repo = tmp_path / "repo"
    (repo / "_bmad-output" / "implementation-artifacts").mkdir(parents=True)
    paths = ProjectPaths(
        project=repo,
        implementation_artifacts=(
            artifacts
            if artifacts is not None
            else repo / "_bmad-output" / "implementation-artifacts"
        ),
        planning_artifacts=repo / "_bmad-output" / "planning-artifacts",
    )
    return _make_flow(tmp_path, paths=paths, policy=_policy(isolation="worktree"))


class _ZeroInodeStat:
    """A directory `lstat` from a filesystem that reports no inode (DW-444)."""

    def __init__(self, real):
        self._real = real
        self.st_ino = 0

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def drained_unpinned():
    """An empty zero-inode degrade counter before and after each test."""
    artifact_publication.drain_unpinned_observations()
    yield
    artifact_publication.drain_unpinned_observations()


def _degrade_artifacts_root(monkeypatch, root: Path, times: int) -> None:
    """Drive ``times`` real zero-inode degrades of ``root``'s fallback pin."""
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        real = real_lstat(path, *args, **kwargs)
        return _ZeroInodeStat(real) if str(path) == str(root) else real

    monkeypatch.setattr(os, "lstat", lstat)
    identity = os.lstat(root)
    for _ in range(times):
        assert artifact_publication._still_pinned(root, identity)
    monkeypatch.setattr(os, "lstat", real_lstat)


def test_unpinned_artifact_observations_journal_once_per_drain(
    tmp_path, monkeypatch, drained_unpinned
):
    """DW-444, decision "Degrade observation, journaled": each artifacts root
    whose fallback reads ran on a zero-inode pin becomes ONE
    `artifact-observation-unpinned` event naming the root, its filesystem (and
    its type alone) and the count; the drain clears the counter, so a second
    drain journals nothing.

    Ablation: make `_journal_unpinned_artifact_observations` a no-op and the
    first assertion fails; drop the `clear()` in `drain_unpinned_observations`
    and the second drain journals the same root again."""
    flow = _artifact_flow(tmp_path)
    root = flow.paths.implementation_artifacts
    _degrade_artifacts_root(monkeypatch, root, times=3)

    flow._journal_unpinned_artifact_observations("dw-fix")

    assert flow.journal.events() == ["artifact-observation-unpinned"]
    fields = flow.journal.fields("artifact-observation-unpinned")
    label = platform_util.filesystem_name(root)
    assert fields == {
        "story_key": "dw-fix",
        "root": str(root),
        "filesystem": label,
        "fs_type": platform_util.filesystem_type(label),
        "count": 3,
    }

    flow._journal_unpinned_artifact_observations("dw-fix")
    assert flow.journal.events() == ["artifact-observation-unpinned"]


_DRAIN_SITES = {
    # site -> (artifact_publication function the site wraps, the flow call)
    "capture": ("capture", lambda flow, task, source: flow.run_isolated(task, lambda _t: None)),
    "prepare": ("prepare", lambda flow, task, source: flow.prepare_publication(task, source)),
    "bind": ("bind_armed", lambda flow, task, source: flow.bind_publication(task, source, "dev:0")),
    "validate_staged": (
        "validate_staged",
        lambda flow, task, source: flow.validate_staged_publication(task, source),
    ),
    "validate_committed": (
        "validate_committed",
        lambda flow, task, source: flow.validate_committed_publication(task, source, "rev", {}),
    ),
    "finish": ("publish", lambda flow, task, source: flow.finish_publication(task, None)),
}


@pytest.mark.parametrize(
    ("site", "refused"),
    [
        (site, refused)
        for site in sorted(_DRAIN_SITES)
        for refused in (False, True)
        # capture's success path continues into worktree provisioning, not this seam
        if refused or site != "capture"
    ],
)
def test_every_publication_site_journals_its_unpinned_observations(
    tmp_path, monkeypatch, drained_unpinned, site, refused
):
    """DW-444: each of the six DW-bundle publication entries — baseline capture
    in `run_isolated`, then prepare, bind, validate_staged, validate_committed
    and finish — drains the degrade counter in a `finally`, so the degrades a
    call counted are journaled whether it succeeds or refuses (and, when it
    refuses, ahead of the `artifact-publication-refused` record and the pause).

    Capture is driven only on its refusal path: its success continues into
    worktree provisioning, which is not this seam.

    Ablation: drop the drain from any one site and its cases fail."""
    flow = _artifact_flow(tmp_path)
    flow.state.target_branch = "main"
    flow._open_unit_workspace = lambda *_a, **_k: SimpleNamespace(
        path=tmp_path / "wt", branch="bmad-loop/dw-fix"
    )
    root = flow.paths.implementation_artifacts
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"])
    function, call = _DRAIN_SITES[site]

    def degrading(*_args, **_kwargs):
        _degrade_artifacts_root(monkeypatch, root, times=2)
        if refused:
            raise artifact_publication.PublicationError("refused after observing")
        return {}

    monkeypatch.setattr(artifact_publication, function, degrading)

    if refused:
        with pytest.raises(_Pause):
            call(flow, task, flow.paths)
    else:
        call(flow, task, flow.paths)

    events = flow.journal.events()
    assert events.count("artifact-observation-unpinned") == 1, events
    fields = flow.journal.fields("artifact-observation-unpinned")
    assert (fields["story_key"], fields["root"], fields["count"]) == ("dw-fix", str(root), 2)
    if refused and site != "capture":
        assert events.index("artifact-observation-unpinned") < events.index(
            "artifact-publication-refused"
        )
    assert artifact_publication.drain_unpinned_observations() == []


@pytest.mark.parametrize("fault_target", ["spec", "root"])
def test_accepted_spec_delivery_resolution_fault_records_uncertainty(
    tmp_path, monkeypatch, fault_target
):
    """A containment resolve after a successful file probe stays advisory.

    Arm the fault at the mounted file probe, after the locator has resolved both
    ends. This reaches the advisory's own exception handler without mocking the
    locator or letting an absent file short-circuit the containment expression.
    Ablation: replace that handler's `delivered = False` with `raise`.
    """
    flow = _artifact_flow(tmp_path)
    flow.state.target_branch = "main"
    rel = "_bmad-output/implementation-artifacts/accepted.md"
    source = flow.paths.project / rel
    source.write_bytes(b"accepted bytes\n")
    worktree = tmp_path / "wt"
    mounted = worktree / rel
    mounted.parent.mkdir(parents=True)
    mounted.write_bytes(b"accepted bytes\n")
    source_name = str(source.resolve())
    task = StoryTask(story_key="1-1", epic=1, spec_file=rel)
    refused = mounted if fault_target == "spec" else worktree
    real_is_file = Path.is_file
    real_resolve = Path.resolve
    probed: list[bool] = []
    faulted: list[Path] = []

    def probe(self, *args, **kwargs):
        result = real_is_file(self, *args, **kwargs)
        if self == mounted:
            probed.append(result)
        return result

    def resolve(self, *args, **kwargs):
        if probed and self == refused:
            faulted.append(self)
            raise OSError("injected containment resolution fault")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", probe)
    monkeypatch.setattr(Path, "resolve", resolve)

    flow._warn_accepted_spec_undelivered(task, worktree)

    assert probed == [True]
    assert faulted == [refused]
    assert flow.journal.entries == [
        (
            "accepted-spec-delivery-unreachable",
            {
                "story_key": "1-1",
                "spec_file": source_name,
                "target_branch": "main",
                "located": True,
            },
        )
    ]


def test_ledger_seed_names_a_ledger_the_checkout_cannot_deliver(tmp_path):
    """The default shape: a gitignored ledger is absent from a tracked-only
    checkout, so the orchestrator's own close would be written to — and read back
    from — a file that does not exist."""
    flow = _artifact_flow(tmp_path)
    flow.paths.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert flow._ledger_seed(worktree) == (
        "_bmad-output/implementation-artifacts/deferred-work.md",
    )


def test_ledger_seed_skips_a_ledger_the_checkout_already_has(tmp_path):
    """A tracked ledger is delivered by `git worktree add`. Seeding it anyway
    copies nothing and journals `worktree-seed-skipped` — a diagnostic meaning "a
    seed you asked for did nothing" — on every isolated unit of every ordinary
    project."""
    flow = _artifact_flow(tmp_path)
    flow.paths.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    delivered = worktree / "_bmad-output" / "implementation-artifacts" / "deferred-work.md"
    delivered.parent.mkdir(parents=True)
    delivered.write_text("# Deferred Work\n", encoding="utf-8")

    assert flow._ledger_seed(worktree) == ()


def test_ledger_seed_skips_an_absent_ledger(tmp_path):
    """No ledger yet is the commonest state — the first harvest is what creates
    it. A seed entry naming a non-existent source is dropped by the seed loop
    without `worktree-seed-skipped` OR `worktree-seed-dropped`, so it would be
    invisible rather than merely inert."""
    flow = _artifact_flow(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert not flow.paths.deferred_work.exists()
    assert flow._ledger_seed(worktree) == ()


def test_ledger_seed_skips_a_ledger_outside_the_project_tree(tmp_path):
    """`ProjectPaths.rebased` leaves an out-of-tree artifacts dir unmoved, so the
    worktree already reads this very file and there is nothing to deliver."""
    shared = tmp_path / "shared-artifacts"
    shared.mkdir()
    flow = _artifact_flow(tmp_path, artifacts=shared)
    flow.paths.deferred_work.write_text("# Deferred Work\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert flow.paths.rebased(worktree).deferred_work == flow.paths.deferred_work
    assert flow._ledger_seed(worktree) == ()


# -------------------------------------------------------------------- board seed
#
# `_board_seed` is `_ledger_seed`'s sibling for the sprint board (#350): same three
# exclusions, same worktree-presence predicate, different artifact — and a harsher
# failure when it is missing, since `verify_dev` RAISES on an absent board where
# the ledger's gate merely re-bundles. Unit-level for the ledger's reason: two of
# the exclusions are silent in the journal by design.


def test_board_seed_names_a_board_the_checkout_cannot_deliver(tmp_path):
    """A gitignored board is absent from a tracked-only checkout, so the
    orchestrator's own advance would be written to — and read back from — a file
    that does not exist, and the read-back raises."""
    flow = _artifact_flow(tmp_path)
    flow.paths.sprint_status.write_text("development_status:\n  1-1-a: ready-for-dev\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert flow._board_seed(worktree) == (
        "_bmad-output/implementation-artifacts/sprint-status.yaml",
    )


def test_board_seed_skips_a_board_the_checkout_already_has(tmp_path):
    """A tracked board — the common shape for this file — is delivered by `git
    worktree add`. Seeding it anyway copies nothing and journals
    `worktree-seed-skipped` on every isolated unit of every such project."""
    flow = _artifact_flow(tmp_path)
    flow.paths.sprint_status.write_text("development_status:\n  1-1-a: ready-for-dev\n")
    worktree = tmp_path / "wt"
    delivered = worktree / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"
    delivered.parent.mkdir(parents=True)
    delivered.write_text("development_status:\n  1-1-a: ready-for-dev\n")

    assert flow._board_seed(worktree) == ()


def test_board_seed_skips_an_absent_board(tmp_path):
    """No board at all is a real state for the run types that need none (sweep,
    stories). A seed entry naming a non-existent source is dropped by the seed loop
    without `worktree-seed-skipped` OR `worktree-seed-dropped`, so it would be
    invisible rather than merely inert."""
    flow = _artifact_flow(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert not flow.paths.sprint_status.exists()
    assert flow._board_seed(worktree) == ()


def test_board_seed_skips_a_board_outside_the_project_tree(tmp_path):
    """`ProjectPaths.rebased` leaves an out-of-tree artifacts dir unmoved, so the
    worktree already reads this very file and there is nothing to deliver."""
    shared = tmp_path / "shared-artifacts"
    shared.mkdir()
    flow = _artifact_flow(tmp_path, artifacts=shared)
    flow.paths.sprint_status.write_text("development_status:\n  1-1-a: ready-for-dev\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert flow.paths.rebased(worktree).sprint_status == flow.paths.sprint_status
    assert flow._board_seed(worktree) == ()


def test_worktree_tickets_unit_never_seeds_board(tmp_path):
    """A tickets run has no sprint board (`SourceTraits.sprint_board`), so a stray
    gitignored board at the configured path is neither seeded into a unit worktree
    nor named as a drop, while the ledger seed is untouched. Ablation (performed):
    drop the trait gate from `_board_seed` and the first assertion answers the
    board's rel; drop it from `_artifact_seed_drops` and the escaping link below is
    named."""
    flow = _artifact_flow(tmp_path)
    flow.state.source = "tickets"
    flow.paths.sprint_status.write_text("development_status:\n  1-1-a: ready-for-dev\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert flow._board_seed(worktree) == ()
    flow.state.source = "sprint-status"
    assert flow._board_seed(worktree) != ()

    flow.state.source = "tickets"
    flow.paths.sprint_status.unlink()
    outside = tmp_path / "outside.yaml"
    outside.write_text("development_status: {}\n", encoding="utf-8")
    flow.paths.sprint_status.symlink_to(outside)
    assert flow._artifact_seed_drops(worktree) == []


# ------------------------------------------------------- symlinked artifact seeds
#
# DW-377 (was #462): a leaf-symlinked ledger or board was relativized through its
# TARGET, so the copy landed where no worktree reader looks. Both seeds share
# `_artifact_seed`, so every row runs for both artifacts.

_SEED_ARTIFACTS = [
    pytest.param("_ledger_seed", "deferred_work", id="ledger"),
    pytest.param("_board_seed", "sprint_status", id="board"),
]


@pytest.mark.parametrize(("method", "attr"), _SEED_ARTIFACTS)
def test_artifact_seed_names_the_configured_path_of_a_leaf_symlink(tmp_path, method, attr):
    """The worktree reads the CONFIGURED path (`ProjectPaths.rebased`), so that is
    where the copy must land — not at the link target's rel. Ablation: restore
    `artifact.resolve().relative_to(...)` and this answers `other/target.md`."""
    flow = _artifact_flow(tmp_path)
    repo = flow.paths.repo_root
    configured: Path = getattr(flow.paths, attr)
    target = repo / "other" / "target.md"
    target.parent.mkdir()
    target.write_text("# Deferred Work\n", encoding="utf-8")
    configured.symlink_to(target)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    rel = configured.relative_to(repo).as_posix()
    assert getattr(flow, method)(worktree) == (rel,)
    assert getattr(flow.paths.rebased(worktree), attr) == worktree / rel


@pytest.mark.parametrize(("method", "attr"), _SEED_ARTIFACTS)
def test_artifact_seed_names_the_target_behind_a_tracked_dangling_link(tmp_path, method, attr):
    """A checkout carrying the link itself leaves it dangling when the target is
    untracked; the seed loop refuses to copy through a link, so the target is the
    one path it will write — and the checked-out link then reads that copy."""
    flow = _artifact_flow(tmp_path)
    repo = flow.paths.repo_root
    configured: Path = getattr(flow.paths, attr)
    target = repo / "other" / "target.md"
    target.parent.mkdir()
    target.write_text("# Deferred Work\n", encoding="utf-8")
    configured.symlink_to(Path("..") / ".." / "other" / "target.md")
    worktree = tmp_path / "wt"
    rel = configured.relative_to(repo)
    (worktree / rel).parent.mkdir(parents=True)
    (worktree / rel).symlink_to(Path("..") / ".." / "other" / "target.md")

    assert getattr(flow, method)(worktree) == ("other/target.md",)

    # ...and once the target is in the worktree the link delivers it: nothing to seed.
    (worktree / "other").mkdir()
    (worktree / "other" / "target.md").write_text("# Deferred Work\n", encoding="utf-8")
    assert getattr(flow, method)(worktree) == ()


@pytest.mark.parametrize(("method", "attr"), _SEED_ARTIFACTS)
def test_artifact_seed_skips_a_leaf_symlink_escaping_the_repo(tmp_path, method, attr):
    """The out-of-repo exclusion survives the fix: the seed loop refuses such a
    source whatever rel it is handed."""
    flow = _artifact_flow(tmp_path)
    configured: Path = getattr(flow.paths, attr)
    outside = tmp_path / "outside.md"
    outside.write_text("# Deferred Work\n", encoding="utf-8")
    configured.symlink_to(outside)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert getattr(flow, method)(worktree) == ()


# DW-432: the out-of-project exclusion above stays, but the drop is named. The rows
# call the module probe and the flow method both, over each artifact.


def _dropped(flow: WorktreeFlow, attr: str, worktree: Path) -> tuple[str, ...]:
    """The module probe for one artifact, over the flow's own roots, cross-checked
    against the flow method that merges both artifacts."""
    configured: Path = getattr(flow.paths, attr)
    probe = _artifact_seed_dropped(configured, *flow._mount_roots(worktree))
    drops = flow._artifact_seed_drops(worktree)
    try:
        rel = (configured.parent.resolve() / configured.name).relative_to(
            flow._mount_roots(worktree)[0].resolve()
        )
    except ValueError:  # an out-of-tree artifacts dir: no project rel to name
        assert probe == () and drops == []
        return probe
    assert (rel.as_posix() in drops) == bool(probe)
    return probe


@pytest.mark.parametrize(("method", "attr"), _SEED_ARTIFACTS)
def test_artifact_seed_dropped_names_a_leaf_symlink_escaping_the_repo(tmp_path, method, attr):
    """The configured project-relative rel is named — and still not seeded.
    Ablation: return `()` from the escape arm and this answers `()`."""
    flow = _artifact_flow(tmp_path)
    configured: Path = getattr(flow.paths, attr)
    outside = tmp_path / "outside.md"
    outside.write_text("# Deferred Work\n", encoding="utf-8")
    configured.symlink_to(outside)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    rel = configured.relative_to(flow.paths.repo_root).as_posix()
    assert _dropped(flow, attr, worktree) == (rel,)
    assert getattr(flow, method)(worktree) == ()
    assert flow._artifact_seed_drops(worktree) == [rel]


@pytest.mark.parametrize(("method", "attr"), _SEED_ARTIFACTS)
def test_artifact_seed_dropped_names_a_nested_target_outside_the_project(tmp_path, method, attr):
    """project = repo_root/app; the link targets repo_root/elsewhere — inside the
    checkout, outside the project. Named project-relative; nothing is seeded (the
    copier's checkout-wide containment would let it through)."""
    repo = tmp_path / "repo"
    app = repo / "app"
    impl = app / "_bmad-output" / "implementation-artifacts"
    impl.mkdir(parents=True)
    paths = ProjectPaths(
        project=app,
        implementation_artifacts=impl,
        planning_artifacts=app / "_bmad-output" / "planning-artifacts",
        repo_root=repo,
    )
    flow = _make_flow(tmp_path, paths=paths, policy=_policy(isolation="worktree"))
    configured: Path = getattr(flow.paths, attr)
    target = repo / "elsewhere" / "x.md"
    target.parent.mkdir()
    target.write_text("# Deferred Work\n", encoding="utf-8")
    configured.symlink_to(target)
    worktree = tmp_path / "wt"
    (worktree / "app").mkdir(parents=True)

    rel = configured.relative_to(app).as_posix()
    assert _dropped(flow, attr, worktree) == (rel,)
    assert getattr(flow, method)(worktree) == ()


@pytest.mark.parametrize(("method", "attr"), _SEED_ARTIFACTS)
def test_artifact_seed_dropped_skips_a_link_the_checkout_carries(tmp_path, method, attr):
    """A tracked live link reads a file at the worktree's configured path: delivered,
    so not named. Ablation: drop the `_is_file(worktree / rel)` arm and it is."""
    flow = _artifact_flow(tmp_path)
    configured: Path = getattr(flow.paths, attr)
    outside = tmp_path / "outside.md"
    outside.write_text("# Deferred Work\n", encoding="utf-8")
    configured.symlink_to(outside)
    worktree = tmp_path / "wt"
    rel = configured.relative_to(flow.paths.repo_root)
    (worktree / rel).parent.mkdir(parents=True)
    (worktree / rel).symlink_to(outside)

    assert _dropped(flow, attr, worktree) == ()
    assert flow._artifact_seed_drops(worktree) == []


@pytest.mark.parametrize(("method", "attr"), _SEED_ARTIFACTS)
@pytest.mark.parametrize("shape", ["in-project-link", "plain-file", "absent"])
def test_artifact_seed_dropped_skips_what_the_seed_owns(tmp_path, method, attr, shape):
    """An in-project target and a plain file are `_artifact_seed`'s to deliver, and
    an absent artifact is dropped silently by design: none is named. Ablation: drop
    the `else: return ()` arm and the in-project rows are named."""
    flow = _artifact_flow(tmp_path)
    repo = flow.paths.repo_root
    configured: Path = getattr(flow.paths, attr)
    if shape == "in-project-link":
        target = repo / "other" / "target.md"
        target.parent.mkdir()
        target.write_text("# Deferred Work\n", encoding="utf-8")
        configured.symlink_to(target)
    elif shape == "plain-file":
        configured.write_text("# Deferred Work\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert _dropped(flow, attr, worktree) == ()
    if shape != "absent":
        assert getattr(flow, method)(worktree) != ()


@pytest.mark.parametrize(("method", "attr"), _SEED_ARTIFACTS)
def test_artifact_seed_dropped_skips_an_out_of_tree_artifacts_dir(tmp_path, method, attr):
    """An artifacts DIR outside the project is shared, not per-checkout: the rel
    cannot derive, so there is nothing to name."""
    shared = tmp_path / "shared-artifacts"
    shared.mkdir()
    flow = _artifact_flow(tmp_path, artifacts=shared)
    configured: Path = getattr(flow.paths, attr)
    configured.write_text("# Deferred Work\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert _dropped(flow, attr, worktree) == ()


@pytest.mark.parametrize(("method", "attr"), _SEED_ARTIFACTS)
def test_artifact_seed_dropped_is_total_over_resolve_faults(tmp_path, monkeypatch, method, attr):
    flow = _artifact_flow(tmp_path)
    configured: Path = getattr(flow.paths, attr)
    outside = tmp_path / "outside.md"
    outside.write_text("# Deferred Work\n", encoding="utf-8")
    configured.symlink_to(outside)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    real = Path.resolve

    def resolve(self, *a, **kw):
        if self == configured:
            raise RuntimeError("symlink loop")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert _dropped(flow, attr, worktree) == ()


# ------------------------------------------------------ uncarried ledger changes
#
# `_uncarried_ledger_changes` (DW-375) diffs a seeded ledger against its worktree
# copy and excuses exactly the writes the post-merge carry re-applies.

_SEED_LEDGER = """# Deferred Work

### DW-1: seeded open entry
origin: review of 1-1
location: n/a
source_spec: `spec-a.md`
reason: something
status: open

### DW-2: seeded entry to close
origin: review of 1-2
location: n/a
source_spec: `spec-b.md`
reason: other
status: open
"""

_HARVEST_ENTRY = """
### DW-3: engine harvest
origin: harvest of 1-3
location: n/a
source_spec: `spec-c.md`
reason: harvested
status: open
"""


def _engine_only_ledger() -> str:
    text = _SEED_LEDGER.replace(
        "reason: something\nstatus: open\n",
        "reason: something\nstatus: open\nseen-again: 2026-09-24 (review of 1-9)\n",
    )
    text = text.replace(
        "reason: other\nstatus: open\n", "reason: other\nstatus: done 2026-09-24 (closed)\n"
    )
    return text + _HARVEST_ENTRY


def test_uncarried_ledger_changes_excuses_engine_only_writes():
    changes = _uncarried_ledger_changes(
        _SEED_LEDGER,
        _engine_only_ledger(),
        harvested=[("harvest of 1-3", "spec-c.md")],
        closed=["DW-2"],
    )
    assert changes == ([], 0)


def test_uncarried_ledger_changes_counts_a_flat_block():
    current = _engine_only_ledger() + "\n- source_spec: spec-z.md\n  note: session wrote this\n"
    ids, count = _uncarried_ledger_changes(
        _SEED_LEDGER, current, harvested=[("harvest of 1-3", "spec-c.md")], closed=["DW-2"]
    )
    assert ids == []
    assert count > 0


def test_uncarried_ledger_changes_names_a_session_canonical_entry():
    current = _engine_only_ledger() + _HARVEST_ENTRY.replace("DW-3", "DW-4").replace(
        "harvest of 1-3", "session of 1-3"
    )
    assert _uncarried_ledger_changes(
        _SEED_LEDGER, current, harvested=[("harvest of 1-3", "spec-c.md")], closed=["DW-2"]
    ) == (["DW-4"], 0)


def test_uncarried_ledger_changes_names_an_edited_seed_entry():
    current = _engine_only_ledger().replace("reason: something", "reason: edited by session")
    assert _uncarried_ledger_changes(
        _SEED_LEDGER, current, harvested=[("harvest of 1-3", "spec-c.md")], closed=["DW-2"]
    ) == (["DW-1"], 0)


def test_uncarried_ledger_changes_names_a_removed_seed_entry():
    current = _SEED_LEDGER.split("### DW-2:")[0]
    assert _uncarried_ledger_changes(_SEED_LEDGER, current, harvested=[], closed=[]) == (
        ["DW-2"],
        0,
    )


def _teardown_unit(tmp_path: Path, flow: WorktreeFlow) -> UnitWorkspace:
    wt = tmp_path / "wt"
    return UnitWorkspace(
        workspace=Workspace(root=wt, paths=flow.paths.rebased(wt)),
        repo_root=flow.paths.repo_root,
        branch="bmad-loop/run-1/1-1",
        path=wt,
        baseline="abc123",
    )


def test_uncarried_warning_is_skipped_for_an_unseeded_ledger(tmp_path):
    """A tracked ledger is never seeded, so the snapshot is None and the check never
    reads the worktree — even a divergent copy (it rides the unit merge) is silent.
    Ablation: replace the read with `seed = task.ledger_seed_text or ""` and this
    journals the session block (a literal deletion of the `seed is None` return
    raises inside `parse_ledger(None)` first)."""
    flow = _artifact_flow(tmp_path)
    unit = _teardown_unit(tmp_path, flow)
    ledger = unit.workspace.paths.deferred_work
    ledger.parent.mkdir(parents=True)
    ledger.write_text(_SEED_LEDGER + "\n- source_spec: spec-z.md\n", encoding="utf-8")
    task = StoryTask(story_key="1-1", epic=1)

    flow._warn_isolated_ledger_uncarried(task, unit)

    assert flow.journal.entries == []


def test_uncarried_warning_records_an_unreadable_worktree_ledger(tmp_path, monkeypatch):
    """Observation degrades to a record: a read fault journals the same kind with its
    `error` and no ids, and never raises."""
    import bmad_loop.worktree_flow as worktree_flow

    flow = _artifact_flow(tmp_path)
    unit = _teardown_unit(tmp_path, flow)
    unit.path.mkdir()
    task = StoryTask(story_key="1-1", epic=1, ledger_seed_text=_SEED_LEDGER)
    monkeypatch.setattr(
        worktree_flow.deferredwork,
        "observe_ledger",
        lambda _path: (None, "PermissionError: [Errno 13] denied"),
    )

    flow._warn_isolated_ledger_uncarried(task, unit)

    assert flow.journal.entries == [
        (
            "isolated-ledger-writes-uncarried",
            {
                "story_key": "1-1",
                "ledger": str(flow.paths.deferred_work),
                "error": "PermissionError: [Errno 13] denied",
            },
        )
    ]
    assert task.ledger_seed_text is None


def test_uncarried_warning_is_silent_once_the_worktree_is_gone(tmp_path):
    """A replayed teardown after the worktree was removed must not read the absent
    copy as "every seeded entry removed". Ablation: drop the `is_dir` guard and this
    journals DW-1 and DW-2."""
    flow = _artifact_flow(tmp_path)
    unit = _teardown_unit(tmp_path, flow)
    task = StoryTask(story_key="1-1", epic=1, ledger_seed_text=_SEED_LEDGER)

    assert not unit.path.exists()
    flow._warn_isolated_ledger_uncarried(task, unit)

    assert flow.journal.entries == []
    assert task.ledger_seed_text is None


def test_payload_replay_keeps_the_snapshot_for_a_still_mounted_worktree(tmp_path):
    """The resume replay publishes from the saved payload (`unit=None`) and then,
    while the worktree is still mounted, repeats with the reopened unit — whose
    check needs the snapshot. The record is saved-cleared once, so a second replay
    on the same mount journals nothing new.

    Ablation: clear the snapshot unconditionally in the `unit is None` arm and no
    row is journaled; drop the post-record `_save()` and `saves` stays 0."""
    flow = _artifact_flow(tmp_path)
    unit = _teardown_unit(tmp_path, flow)
    ledger = unit.workspace.paths.deferred_work
    ledger.parent.mkdir(parents=True)
    ledger.write_text(_SEED_LEDGER + "\n- source_spec: spec-z.md\n", encoding="utf-8")
    task = StoryTask(story_key="1-1", epic=1, ledger_seed_text=_SEED_LEDGER)
    task.worktree_path = str(unit.path)

    flow.finish_publication(task, None)
    assert task.ledger_seed_text == _SEED_LEDGER
    assert flow.calls.saves == 0

    flow._warn_isolated_ledger_uncarried(task, unit)
    assert [kind for kind, _ in flow.journal.entries] == ["isolated-ledger-writes-uncarried"]
    assert task.ledger_seed_text is None
    assert flow.calls.saves == 1

    flow._warn_isolated_ledger_uncarried(task, unit)
    assert len(flow.journal.entries) == 1


def test_payload_replay_drops_the_snapshot_once_the_worktree_is_gone(tmp_path):
    """With no mount left, the payload replay is the last teardown call, so it
    drops the snapshot from state.json itself. Ablation: delete that clear and the
    snapshot survives."""
    flow = _artifact_flow(tmp_path)
    task = StoryTask(story_key="1-1", epic=1, ledger_seed_text=_SEED_LEDGER)
    task.worktree_path = str(tmp_path / "wt")

    flow.finish_publication(task, None)

    assert task.ledger_seed_text is None
    assert flow.calls.saves == 1
    assert flow.journal.entries == []


# --------------------------------------------------------------- pinned config edits
# DW-368: a pinned (skip-worktree) tracked hook config hides a story's own edit from
# the unit commit, so success teardown compares it with the recorded rewrite and
# pauses rather than delete the edit with the worktree.

_LEGACY_RELAY = 'python3 "$CLAUDE_PROJECT_DIR"/.bmad-loop/bmad_loop_hook.py Stop'


def _pinned_rewrite(tmp_path: Path, profile_name: str = "claude") -> tuple[str, str, str]:
    """A realistic provisioning rewrite: the operator's settings plus this
    installation's relay registrations, written as provisioning writes it."""
    from bmad_loop.adapters.profile import get_profile
    from bmad_loop.install import _hook_command, merge_hooks

    profile = get_profile(profile_name)
    registrations = {
        native: _hook_command(tmp_path, profile, canonical)
        for native, canonical in profile.hooks.events.items()
    }
    config, _ = merge_hooks(
        {"permissions": {"allow": ["Bash(ls)"]}}, registrations, profile.hooks.dialect
    )
    return profile.hooks.config_path, profile.hooks.dialect, json.dumps(config, indent=2) + "\n"


def _write_pinned(tmp_path: Path, profile_name: str = "claude"):
    wt = tmp_path / "wt"
    rel, dialect, text = _pinned_rewrite(tmp_path, profile_name)
    (wt / rel).parent.mkdir(parents=True, exist_ok=True)
    (wt / rel).write_text(text, encoding="utf-8")
    return wt, rel, {rel: {"dialect": dialect, "text": text}}


def _edit_json(path: Path, mutate) -> None:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    mutate(cfg)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def _swap_relay_for_legacy(cfg: dict) -> None:
    for groups in cfg["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                hook["command"] = _LEGACY_RELAY


def _add_lint_hook(cfg: dict) -> None:
    cfg["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "make lint"}]})


def _hook_only(path: Path, how: str) -> None:
    if how == "reformatted":
        path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8"))), encoding="utf-8")
    elif how == "relay-changed":
        _edit_json(path, _swap_relay_for_legacy)
    else:  # relay-removed: the emptied hooks container is dropped too
        _edit_json(path, lambda cfg: cfg.pop("hooks"))


@pytest.mark.parametrize("how", ["reformatted", "relay-changed", "relay-removed"])
def test_pinned_config_hook_only_difference_is_not_an_edit(tmp_path, how):
    """Relay-hook entries and formatting are orchestrator-owned. Ablation: skip the
    `strip_relay_hooks` normalization and the relay rows report an edit; skip the
    empty-container drop and `relay-removed` does."""
    wt, rel, pins = _write_pinned(tmp_path)
    _hook_only(wt / rel, how)
    assert _pinned_config_edits(wt, pins) == []


def test_pinned_config_antigravity_relay_group_removal_is_not_an_edit(tmp_path):
    """agy keys the relay under its own top-level group, not "hooks"; the emptied
    group is dropped the same way. Ablation: normalize under "hooks" for every
    dialect and this reports an edit to the `bmad-loop` key."""
    wt, rel, pins = _write_pinned(tmp_path, "antigravity")
    from bmad_loop.install import ANTIGRAVITY_HOOK_GROUP

    _edit_json(wt / rel, lambda cfg: cfg.pop(ANTIGRAVITY_HOOK_GROUP))
    assert _pinned_config_edits(wt, pins) == []


def test_pinned_config_untouched_file_is_not_an_edit(tmp_path):
    wt, _rel, pins = _write_pinned(tmp_path)
    assert _pinned_config_edits(wt, pins) == []
    assert _pinned_config_edits(wt, {}) == []


@pytest.mark.parametrize(
    ("mutate", "key"),
    [
        (lambda cfg: cfg["permissions"]["allow"].append("Bash(rm -rf build)"), "permissions"),
        (_add_lint_hook, "hooks"),
        (lambda cfg: cfg.update(model=None), "model"),
        (lambda cfg: cfg.pop("permissions"), "permissions"),
        (lambda cfg: cfg.update(flag=True), "flag"),
    ],
    ids=["permissions-allow", "non-relay-hook", "null-key-added", "key-removed", "bool-to-int"],
)
def test_pinned_config_story_edit_is_reported(tmp_path, mutate, key):
    """A story's own change — including a non-relay hook it adds — is an edit.
    Ablation: compare after stripping the whole hook container and the
    `non-relay-hook` row passes as untouched."""
    wt, rel, pins = _write_pinned(tmp_path)
    if key == "flag":
        # the record holds `"flag": 1`; the story wrote `true`. Python's `==` reads
        # them equal. Ablation: compare parsed dicts with `==` and this returns [].
        recorded = json.loads(pins[rel]["text"])
        recorded["flag"] = 1
        pins[rel]["text"] = json.dumps(recorded, indent=2) + "\n"
    _edit_json(wt / rel, mutate)
    assert _pinned_config_edits(wt, pins) == [
        f"{rel}: changed outside the relay hooks (keys: {key})"
    ]


@pytest.mark.parametrize(
    ("recorded", "fault"),
    [
        ("{not json", "the recorded rewrite cannot be parsed"),
        ("[]\n", "the recorded rewrite is not"),
    ],
    ids=["unparseable", "non-object"],
)
def test_pinned_config_unprovable_record_counts_as_an_edit(tmp_path, recorded, fault):
    """A record that cannot be compared refuses too. Ablation: `continue` on
    either fault and its row returns []."""
    wt, rel, pins = _write_pinned(tmp_path)
    _hook_only(wt / rel, "reformatted")  # skip the byte-equal fast path
    pins[rel]["text"] = recorded
    (edit,) = _pinned_config_edits(wt, pins)
    assert edit.startswith(f"{rel}: {fault}")


@pytest.mark.parametrize(
    ("damage", "fault"),
    [
        (lambda p: p.unlink(), "deleted while pinned"),
        (lambda p: p.write_text("{not json", encoding="utf-8"), "no longer parses as JSON"),
        (lambda p: p.write_text("[]\n", encoding="utf-8"), "no longer a JSON object"),
        (lambda p: p.write_bytes(b'{"a": "\xff"}'), "not valid UTF-8"),
    ],
    ids=["deleted", "unparseable", "non-object", "undecodable"],
)
def test_pinned_config_unprovable_file_counts_as_an_edit(tmp_path, damage, fault):
    """Teardown is irreversible, so a pinned config that cannot be proven
    unedited refuses, the description naming the fault. Ablation: `continue` on
    any of these faults and its row returns []."""
    wt, rel, pins = _write_pinned(tmp_path)
    damage(wt / rel)
    (edit,) = _pinned_config_edits(wt, pins)
    assert edit.startswith(f"{rel}: {fault}")


def test_pinned_config_unreadable_file_counts_as_an_edit(tmp_path, monkeypatch):
    wt, rel, pins = _write_pinned(tmp_path)
    real = Path.read_bytes

    def read_bytes(self):
        if self == wt / rel:
            raise PermissionError(13, "denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    (edit,) = _pinned_config_edits(wt, pins)
    assert edit.startswith(f"{rel}: unreadable while pinned")
    assert "denied" in edit


# DW-479: a DEFERRED unit's forensic patch is `git diff <baseline>`, blind to a
# skip-worktree pin, so the pinned edits are appended as their own section.


def test_pinned_config_forensics_diffs_a_story_edit(tmp_path):
    """Ablation: drop the diff and only the description line is left."""
    wt, rel, pins = _write_pinned(tmp_path)
    _edit_json(wt / rel, lambda cfg: cfg.update(storyKey="added-by-story"))

    text = _pinned_config_forensics(wt, pins)

    assert text.startswith("# bmad-loop (DW-479)")
    assert f"# {rel}: changed outside the relay hooks (keys: storyKey)\n" in text
    assert "commented out and does NOT apply" in text
    assert f"# --- a/{rel}\n# +++ b/{rel}\n" in text
    assert '# +  "storyKey": "added-by-story"' in text
    assert all(line.startswith("#") for line in text.splitlines())


@pytest.mark.parametrize("how", ["untouched", "reformatted", "relay-changed", "relay-removed"])
def test_pinned_config_forensics_is_empty_without_an_edit(tmp_path, how):
    wt, rel, pins = _write_pinned(tmp_path)
    if how != "untouched":
        _hook_only(wt / rel, how)
    assert _pinned_config_forensics(wt, pins) == ""
    assert _pinned_config_forensics(wt, {}) == ""


def test_pinned_config_forensics_diffs_a_deleted_config_to_dev_null(tmp_path):
    wt, rel, pins = _write_pinned(tmp_path)
    (wt / rel).unlink()

    text = _pinned_config_forensics(wt, pins)

    assert f"# {rel}: deleted while pinned\n" in text
    assert f"# --- a/{rel}\n# +++ /dev/null\n" in text
    assert '# -  "permissions": {' in text


def test_pinned_config_forensics_marks_a_missing_final_newline(tmp_path):
    wt, rel, pins = _write_pinned(tmp_path)
    _edit_json(wt / rel, lambda cfg: cfg.update(k=1))
    (wt / rel).write_text((wt / rel).read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")

    text = _pinned_config_forensics(wt, pins)

    assert text.endswith("\n# \\ No newline at end of file\n")
    assert text.count("No newline") == 1


def test_pinned_config_forensics_splits_on_newline_only(tmp_path):
    """U+2028 is legal raw inside a JSON string; `str.splitlines` would break the
    line there and emit a bogus mid-hunk no-newline marker."""
    wt, rel, pins = _write_pinned(tmp_path)
    cfg = json.loads((wt / rel).read_text(encoding="utf-8"))
    cfg["note"] = "a\u2028b"
    (wt / rel).write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )

    text = _pinned_config_forensics(wt, pins)

    assert '# +  "note": "a\u2028b",\n' in text or '# +  "note": "a\u2028b"\n' in text
    assert "No newline" not in text


@pytest.mark.parametrize("fault", ["undecodable", "unreadable"])
def test_pinned_config_forensics_keeps_the_description_without_a_diff(tmp_path, monkeypatch, fault):
    """A file that cannot be read or decoded is described, never diffed, and the
    probe does not raise."""
    wt, rel, pins = _write_pinned(tmp_path)
    if fault == "undecodable":
        (wt / rel).write_bytes(b'{"a": "\xff"}')
    else:
        real = Path.read_bytes

        def read_bytes(self):
            if self == wt / rel:
                raise PermissionError(13, "denied")
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)

    text = _pinned_config_forensics(wt, pins)

    assert f"# {rel}: " in text
    assert "---" not in text and "@@" not in text


def test_pinned_config_forensics_diffs_only_the_edited_rel(tmp_path):
    wt, rel, pins = _write_pinned(tmp_path)
    _other_wt, other_rel, other_pins = _write_pinned(tmp_path, "gemini")
    pins.update(other_pins)
    _edit_json(wt / other_rel, lambda cfg: cfg.update(storyKey=True))

    text = _pinned_config_forensics(wt, pins)

    assert f"a/{other_rel}" in text and f"a/{rel}" not in text


def _pinned_flow(tmp_path, monkeypatch, *, story_edit: bool):
    """A DONE unit whose worktree holds a pinned config, with teardown recorded
    instead of run."""
    import bmad_loop.worktree_flow as worktree_flow

    flow = _artifact_flow(tmp_path)
    flow.state.target_branch = "main"
    unit = _teardown_unit(tmp_path, flow)
    wt, rel, pins = _write_pinned(tmp_path)
    assert wt == unit.path
    if story_edit:
        _edit_json(wt / rel, lambda cfg: cfg["permissions"]["allow"].append("Bash(make)"))
    else:
        _hook_only(wt / rel, "relay-changed")
    task = StoryTask(
        story_key="1-1",
        epic=1,
        phase=Phase.DONE,
        branch="bmad-loop/run-1/1-1",
        worktree_path=str(wt),
        pinned_config_rewrites=pins,
    )
    closed: list[UnitWorkspace] = []

    def close(u, **_k):
        closed.append(u)
        shutil.rmtree(u.path)

    monkeypatch.setattr(worktree_flow, "close_unit_workspace", close)
    return flow, unit, task, rel, closed


def test_finish_publication_refuses_to_tear_down_a_pinned_config_edit(tmp_path, monkeypatch):
    """The acceptance path: the merge landed, the story edited the pinned config,
    so the run pauses (phase stays DONE) with the row journaled and the worktree —
    edit included — still on disk; a retry refuses again while the edit remains.
    Ablation: drop the `_refuse_pinned_config_edits` call in `finish_publication`
    and teardown runs (`closed` is non-empty, no `_Pause`)."""
    flow, unit, task, rel, closed = _pinned_flow(tmp_path, monkeypatch, story_edit=True)

    with pytest.raises(_Pause) as excinfo:
        flow.finish_publication(task, unit)

    assert closed == []
    assert "Bash(make)" in (unit.path / rel).read_text(encoding="utf-8")
    assert task.phase is Phase.DONE
    assert flow.journal.fields("pinned-config-edit-refused") == {
        "story_key": "1-1",
        "worktree": str(unit.path),
        "edits": [f"{rel}: changed outside the relay hooks (keys: permissions)"],
    }
    reason = excinfo.value.reason
    for needle in (
        rel,
        str(unit.path),
        "DW-368",
        "main",
        "git worktree remove --force",
        "delete branch bmad-loop/run-1/1-1",
        f"git -C {unit.path} show HEAD:",
        "relay hook entries out",
    ):
        assert needle in reason
    assert "bmad-loop resume run-1" in reason
    assert excinfo.value.story_key == "1-1"

    with pytest.raises(_Pause):
        flow.finish_publication(task, unit)
    assert closed == []
    assert flow.journal.events().count("pinned-config-edit-refused") == 2
    assert task.pinned_config_rewrites  # kept while mounted, so retries refuse


def test_finish_publication_tears_down_a_hook_only_pinned_diff(tmp_path, monkeypatch):
    """Only the relay command moved, so teardown proceeds and nothing is journaled;
    with the worktree gone the settings-text record is dropped from state.
    Ablation: compare raw text instead of normalized configs and this pauses; drop
    the post-teardown clear and the record survives."""
    flow, unit, task, _rel, closed = _pinned_flow(tmp_path, monkeypatch, story_edit=False)

    flow.finish_publication(task, unit)

    assert closed == [unit]
    assert flow.calls.pauses == []
    assert "pinned-config-edit-refused" not in flow.journal.events()
    assert task.pinned_config_rewrites == {}
    assert flow.calls.saves >= 1


def test_finish_publication_keeps_the_record_while_teardown_leaves_the_mount(tmp_path, monkeypatch):
    """A degraded teardown can leave the worktree mounted; the record must stay so
    a later pass still checks it. Ablation: drop the mounted guard in
    `_drop_pinned_config_record` and the record is cleared."""
    import bmad_loop.worktree_flow as worktree_flow

    flow, unit, task, _rel, _closed = _pinned_flow(tmp_path, monkeypatch, story_edit=False)
    monkeypatch.setattr(worktree_flow, "close_unit_workspace", lambda *_a, **_k: None)

    flow.finish_publication(task, unit)

    assert unit.path.is_dir()
    assert task.pinned_config_rewrites


def test_gc_run_worktrees_drops_the_record_of_a_worktree_removed_by_hand(tmp_path, monkeypatch):
    """The refusal's remedy removes the worktree by hand, so the GC never discards
    it; the settings-text record is still dropped. Ablation: drop the clear after
    the mounted leg and the record survives."""
    import bmad_loop.worktree_flow as worktree_flow

    flow, unit, task, _rel, _closed = _pinned_flow(tmp_path, monkeypatch, story_edit=True)
    flow.state.tasks = {task.story_key: task}
    shutil.rmtree(unit.path)
    monkeypatch.setattr(
        worktree_flow, "discard_worktree", lambda *_a, **_k: pytest.fail("nothing to discard")
    )
    monkeypatch.setattr(worktree_flow.verify, "worktree_prune", lambda *_: None)

    flow.gc_run_worktrees()

    assert flow.calls.pauses == []
    assert task.pinned_config_rewrites == {}


def test_pinned_config_check_is_a_noop_once_the_worktree_is_gone(tmp_path):
    """Removing the worktree is the operator's acknowledgement. Ablation: drop the
    mounted guard and the missing file reads as "deleted while pinned"."""
    flow = _artifact_flow(tmp_path)
    _rel, dialect, text = _pinned_rewrite(tmp_path)
    task = StoryTask(
        story_key="1-1",
        epic=1,
        pinned_config_rewrites={".claude/settings.json": {"dialect": dialect, "text": text}},
    )
    flow._refuse_pinned_config_edits(task, tmp_path / "gone")
    assert flow.journal.entries == []


def test_gc_run_worktrees_drops_the_record_after_discarding_an_unedited_pin(tmp_path, monkeypatch):
    """A hook-only diff lets the GC discard the worktree, after which the
    settings-text record is dropped. Ablation: drop the post-discard clear."""
    import bmad_loop.worktree_flow as worktree_flow

    flow, unit, task, _rel, _closed = _pinned_flow(tmp_path, monkeypatch, story_edit=False)
    flow.state.tasks = {task.story_key: task}
    monkeypatch.setattr(
        worktree_flow, "discard_worktree", lambda _repo, path, *_a, **_k: shutil.rmtree(path)
    )
    monkeypatch.setattr(worktree_flow.verify, "worktree_prune", lambda *_: None)

    flow.gc_run_worktrees()

    assert not unit.path.exists()
    assert task.pinned_config_rewrites == {}


class _StopAfterProvisioning(Exception):
    pass


def test_run_isolated_records_every_pinned_rewrite(tmp_path, monkeypatch):
    """The seam that arms the teardown guard: `run_isolated` hands provisioning an
    `on_pinned` recorder and stores what it reported on the task, last write per
    path winning. Ablation: drop `on_pinned=_record_pin` or the
    `task.pinned_config_rewrites` assignment and the record stays empty."""
    import bmad_loop.worktree_flow as worktree_flow

    repo, wt = tmp_path / "repo", tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    paths = ProjectPaths(
        project=repo,
        implementation_artifacts=repo / "_bmad-output/implementation-artifacts",
        planning_artifacts=repo / "_bmad-output/planning-artifacts",
    )
    unit = UnitWorkspace(
        workspace=Workspace(root=wt, paths=paths.rebased(wt)),
        repo_root=repo,
        branch="bmad-loop/run-1/1-1",
        path=wt,
        baseline="abc123",
    )

    def provision(*_args, on_pinned=None, **_kwargs):
        assert on_pinned is not None
        on_pinned(".claude/settings.json", "claude-settings-json", "first\n")
        on_pinned(".claude/settings.json", "claude-settings-json", "second\n")
        on_pinned(".gemini/settings.json", "gemini-settings-json", "{}\n")
        return []

    def stop(*_args, **_kwargs):
        raise _StopAfterProvisioning

    monkeypatch.setattr(worktree_flow, "provision_worktree", provision)
    monkeypatch.setattr(worktree_flow, "worktree_seed_undelivered", stop)
    state = SimpleNamespace(target_branch="main", run_id="run-1", source="sprint", tasks={})
    flow = _make_flow(
        tmp_path,
        paths=paths,
        state=state,
        open_unit_workspace=lambda *_args, **_kwargs: unit,
    )
    task = StoryTask(
        story_key="1-1",
        epic=1,
        pinned_config_rewrites={"stale.json": {"dialect": "x", "text": "y"}},
    )

    with pytest.raises(_StopAfterProvisioning):
        flow.run_isolated(task, lambda _t: pytest.fail("drive must not run"))

    assert task.pinned_config_rewrites == {
        ".claude/settings.json": {"dialect": "claude-settings-json", "text": "second\n"},
        ".gemini/settings.json": {"dialect": "gemini-settings-json", "text": "{}\n"},
    }


def test_gc_run_worktrees_refuses_to_discard_a_pinned_config_edit(tmp_path, monkeypatch):
    """A resume replays `finish_publication` only for bundles, so the run-end GC is
    where a still-mounted DONE worktree would otherwise be discarded with the edit.
    Ablation: drop the check in `gc_run_worktrees` and `discard_worktree` runs."""
    import bmad_loop.worktree_flow as worktree_flow

    flow, unit, task, _rel, _closed = _pinned_flow(tmp_path, monkeypatch, story_edit=True)
    flow.state.tasks = {task.story_key: task}
    discarded: list[str] = []
    monkeypatch.setattr(
        worktree_flow, "discard_worktree", lambda _repo, path, *_a, **_k: discarded.append(path)
    )

    with pytest.raises(_Pause, match="DW-368"):
        flow.gc_run_worktrees()

    assert discarded == []
    assert unit.path.is_dir()
    assert "pinned-config-edit-refused" in flow.journal.events()


# --------------------------------------------------------------- profiles / agents


def test_worktree_profiles_dedups_dev_and_review(tmp_path):
    flow = _make_flow(
        tmp_path,
        adapters_get=lambda: {"dev": _FakeAdapter("claude"), "review": _FakeAdapter("claude")},
    )
    profiles = flow.worktree_profiles()
    assert [p.name for p in profiles] == ["claude"]


def test_worktree_profiles_ignores_fakes_without_a_profile(tmp_path):
    flow = _make_flow(
        tmp_path, adapters_get=lambda: {"dev": _FakeAdapter(), "review": _FakeAdapter()}
    )
    assert flow.worktree_profiles() == []


def test_worktree_profiles_reads_live_adapters(tmp_path):
    # the getter is live, so a caller (e.g. a test) that rebinds the adapters dict
    # after construction is reflected here — mirrors engine.adapters reassignment.
    holder = {"a": {"dev": _FakeAdapter(), "review": _FakeAdapter()}}
    flow = _make_flow(tmp_path, adapters_get=lambda: holder["a"])
    assert flow.worktree_profiles() == []
    holder["a"] = {"dev": _FakeAdapter("codex"), "review": _FakeAdapter("codex")}
    assert [p.name for p in flow.worktree_profiles()] == ["codex"]


def test_engine_agent_ids_maps_and_dedups(tmp_path):
    two = _make_flow(
        tmp_path,
        adapters_get=lambda: {"dev": _FakeAdapter("claude"), "review": _FakeAdapter("codex")},
    )
    assert two.engine_agent_ids() == ["claude-code", "codex"]
    same = _make_flow(
        tmp_path,
        adapters_get=lambda: {"dev": _FakeAdapter("claude"), "review": _FakeAdapter("claude")},
    )
    assert same.engine_agent_ids() == ["claude-code"]
    assert _make_flow(tmp_path).engine_agent_ids() == []


# --------------------------------------------------------- codex hook trust gate


def _codex_adapter(binary: str = "codex", extra_args: tuple[str, ...] | None = None):
    from bmad_loop.adapters.profile import get_profile

    return SimpleNamespace(profile=get_profile("codex"), binary=binary, extra_args=extra_args)


def _claude_adapter():
    from bmad_loop.adapters.profile import get_profile

    return SimpleNamespace(profile=get_profile("claude"), binary="claude", extra_args=None)


def _record_trust(monkeypatch, verdicts: dict[str, str]):
    """Stub the one trust oracle; ``verdicts`` maps a queried binary to its status."""
    from bmad_loop import codex_trust

    calls: list[tuple[Path, str | None, tuple[str, ...]]] = []

    def trust(path, profile, *, binary=None, marker=None):
        calls.append((path, binary, profile.bypass_args))
        return codex_trust.TrustResult(verdicts.get(binary or "", "trusted"), f"reason-{binary}")

    monkeypatch.setattr(codex_trust, "project_hook_trust", trust)
    return calls


def test_codex_trust_gate_checks_a_codex_review_stage_behind_a_claude_dev(tmp_path, monkeypatch):
    """The gate walks every dev-primitive role, not just dev: a claude dev with a
    Codex reviewer still queries (and escalates on) the review stage's worktree trust.

    Ablation: iterate only ``dev`` in ``gate_codex_hook_trust`` and nothing is queried."""
    calls = _record_trust(monkeypatch, {"codex-review": "untrusted"})
    flow = _make_flow(
        tmp_path,
        adapters_get=lambda: {"dev": _claude_adapter(), "review": _codex_adapter("codex-review")},
    )
    task = StoryTask(story_key="1-1", epic=1)
    wt = tmp_path / "worktrees" / "1-1"

    with pytest.raises(_Pause) as excinfo:
        flow.gate_codex_hook_trust(task, wt)

    assert calls == [(wt, "codex-review", ("--dangerously-bypass-approvals-and-sandbox",))]
    assert task.phase == Phase.ESCALATED
    reason = excinfo.value.reason
    assert "Codex hook trust is untrusted for the review session's worktree" in reason
    assert str(wt) in reason and "reason-codex-review" in reason
    assert flow.journal.events() == ["story-escalated"]
    assert "CRITICAL escalation: 1-1" in (tmp_path / ATTENTION_FILE).read_text()


def test_codex_trust_gate_dedupes_identical_adapters_and_folds_extra_args(tmp_path, monkeypatch):
    """Identical (profile, binary, extra_args) launches are queried once; a stage's
    ``extra_args`` replace ``bypass_args`` in the queried profile, as at launch."""
    calls = _record_trust(monkeypatch, {})
    shared = _codex_adapter()
    flow = _make_flow(tmp_path, adapters_get=lambda: {"dev": shared, "review": _codex_adapter()})
    task = StoryTask(story_key="1-1", epic=1)
    flow.gate_codex_hook_trust(task, tmp_path)
    assert len(calls) == 1 and task.phase == Phase.PENDING

    calls.clear()
    flow = _make_flow(
        tmp_path,
        adapters_get=lambda: {
            "dev": _codex_adapter(),
            "review": _codex_adapter(extra_args=("-x",)),
        },
    )
    flow.gate_codex_hook_trust(task, tmp_path)
    assert [c[2] for c in calls] == [("--dangerously-bypass-approvals-and-sandbox",), ("-x",)]


def test_codex_trust_gate_skips_fakes_and_non_codex_dialects(tmp_path, monkeypatch):
    from bmad_loop import codex_trust

    monkeypatch.setattr(
        codex_trust,
        "project_hook_trust",
        lambda *_a, **_k: pytest.fail("non-Codex adapters must not query Codex trust"),
    )
    for adapters in (
        {"dev": _FakeAdapter(), "review": _FakeAdapter()},
        {"dev": _FakeAdapter("claude"), "review": _FakeAdapter("codex")},  # name-only fakes
        {"dev": _claude_adapter(), "review": _claude_adapter()},
    ):
        flow = _make_flow(tmp_path, adapters_get=lambda a=adapters: a)
        flow.gate_codex_hook_trust(StoryTask(story_key="1-1", epic=1), tmp_path)
    assert (tmp_path / ATTENTION_FILE).exists() is False


def test_codex_trust_gate_untrusted_text_names_the_prompt(tmp_path, monkeypatch):
    """``untrusted`` is cleared by Codex's own prompt: the text says to accept it in
    that worktree — and none of the ``unverifiable`` fixes, which would send the
    operator the wrong way. The recovery command is NOT in the reason:
    ``escalate_unit`` appends its own resolve/resume suffix to the notification, so
    repeating it here would print it twice."""
    _record_trust(monkeypatch, {"codex": "untrusted"})
    flow = _make_flow(tmp_path, adapters_get=lambda: {"dev": _codex_adapter(), "review": None})
    wt = tmp_path / "worktrees" / "1-1"

    with pytest.raises(_Pause) as excinfo:
        flow.gate_codex_hook_trust(StoryTask(story_key="1-1", epic=1), wt, roles=("dev",))

    reason = excinfo.value.reason
    assert f"Codex hook trust is untrusted for the dev session's worktree {wt}" in reason
    assert "(reason-codex)" in reason
    assert reason.endswith("Open Codex in that worktree, accept its hook trust prompt")
    assert "bmad-loop resume" not in reason and "resolve" not in reason
    assert "could not verify" not in reason and "extra_args" not in reason
    # The recovery command appears exactly once — from escalate_unit's suffix.
    attention = (tmp_path / ATTENTION_FILE).read_text()
    assert attention.count("bmad-loop resume") == 1


def test_codex_trust_gate_unverifiable_text_names_the_likely_fixes(tmp_path, monkeypatch):
    """``unverifiable`` usually has a cause a trust prompt cannot clear (stage
    ``extra_args`` / profile ``launch_args`` that move hook discovery, the binary, an
    unreadable config), so the text says trust could not be verified and names those
    fixes before the prompt and the same recovery command.

    Ablation: drop the ``status == "unverifiable"`` branch in ``_codex_trust_reason``
    and the fix list disappears."""
    _record_trust(monkeypatch, {"codex": "unverifiable"})
    flow = _make_flow(tmp_path, adapters_get=lambda: {"dev": None, "review": _codex_adapter()})
    wt = tmp_path / "worktrees" / "1-1"

    with pytest.raises(_Pause) as excinfo:
        flow.gate_codex_hook_trust(StoryTask(story_key="1-1", epic=1), wt, roles=("review",))

    reason = excinfo.value.reason
    assert f"Codex hook trust is unverifiable for the review session's worktree {wt}" in reason
    assert "(reason-codex)" in reason
    assert "bmad-loop could not verify Codex's hook trust for that worktree" in reason
    assert "stage `extra_args`" in reason and "profile `launch_args`" in reason
    assert "Codex binary is on PATH" in reason and "hook config is readable" in reason
    fixes, prompt = reason.index("Likely fixes"), reason.index("Open Codex in that worktree")
    assert fixes < prompt
    assert reason.endswith("Open Codex in that worktree, accept its hook trust prompt")
    assert "bmad-loop resume" not in reason and "resolve" not in reason


def test_codex_trust_gate_roles_limits_the_checked_adapters(tmp_path, monkeypatch):
    """``roles`` scopes the check to the launching session: the per-session gate in
    ``Engine._run_session`` passes ``(role,)``, so an untrusted Codex reviewer does not
    block a trusted dev session — it escalates when the review session launches."""
    calls = _record_trust(monkeypatch, {"codex-review": "untrusted"})
    flow = _make_flow(
        tmp_path,
        adapters_get=lambda: {
            "dev": _codex_adapter("codex-dev"),
            "review": _codex_adapter("codex-review"),
        },
    )
    task = StoryTask(story_key="1-1", epic=1)

    flow.gate_codex_hook_trust(task, tmp_path, roles=("dev",))
    assert [c[1] for c in calls] == ["codex-dev"] and task.phase == Phase.PENDING

    with pytest.raises(_Pause):
        flow.gate_codex_hook_trust(task, tmp_path, roles=("review",))
    assert [c[1] for c in calls] == ["codex-dev", "codex-review"]
    assert task.phase == Phase.ESCALATED


def _sequenced_trust(monkeypatch, results):
    """Stub the trust oracle to answer ``results`` in order; returns the call log."""
    from bmad_loop import codex_trust

    answers = list(results)
    calls: list[Path] = []

    def trust(path, _profile, *, binary=None, marker=None):
        calls.append(path)
        return answers.pop(0)

    monkeypatch.setattr(codex_trust, "project_hook_trust", trust)
    return calls


def test_codex_trust_gate_retries_a_failed_query_once_then_drives(tmp_path, monkeypatch):
    """A ``hooks/list`` query failure (spawn error / timeout) is not Codex's verdict:
    one retry absorbs it, and a trusted second answer lets the unit through.

    Ablation: drop the retry in ``gate_codex_hook_trust`` and the first
    query-failure escalates."""
    from bmad_loop import codex_trust

    calls = _sequenced_trust(
        monkeypatch,
        [
            codex_trust.TrustResult("unverifiable", codex_trust.QUERY_FAILED_REASON),
            codex_trust.TrustResult("trusted", "ok"),
        ],
    )
    flow = _make_flow(tmp_path, adapters_get=lambda: {"dev": _codex_adapter(), "review": None})
    task = StoryTask(story_key="1-1", epic=1)
    wt = tmp_path / "worktrees" / "1-1"

    flow.gate_codex_hook_trust(task, wt, roles=("dev",))

    assert calls == [wt, wt]
    assert task.phase == Phase.PENDING and flow.calls.pauses == []


def test_codex_trust_gate_escalates_after_two_failed_queries(tmp_path, monkeypatch):
    """The retry is exactly one: a second query failure escalates with the
    ``unverifiable`` text, after exactly two queries."""
    from bmad_loop import codex_trust

    failed = codex_trust.TrustResult("unverifiable", codex_trust.QUERY_FAILED_REASON)
    calls = _sequenced_trust(monkeypatch, [failed, failed, failed])
    flow = _make_flow(tmp_path, adapters_get=lambda: {"dev": _codex_adapter(), "review": None})
    task = StoryTask(story_key="1-1", epic=1)
    wt = tmp_path / "worktrees" / "1-1"

    with pytest.raises(_Pause) as excinfo:
        flow.gate_codex_hook_trust(task, wt, roles=("dev",))

    assert calls == [wt, wt]
    assert task.phase == Phase.ESCALATED
    reason = excinfo.value.reason
    assert "Codex hook trust is unverifiable" in reason
    assert codex_trust.QUERY_FAILED_REASON in reason
    assert "bmad-loop could not verify Codex's hook trust" in reason


@pytest.mark.parametrize(
    ("status", "why"),
    [
        ("unverifiable", "hook trust cannot verify profile launch arguments"),
        ("unverifiable", "hook trust Codex binary is unavailable"),
        ("untrusted", "hook trust is stale for Stop; accept hooks in Codex"),
    ],
)
def test_codex_trust_gate_never_retries_a_real_verdict(tmp_path, monkeypatch, status, why):
    """Only the query-failure reason is retried: any other ``unverifiable`` reason
    and every ``untrusted`` verdict escalate on the first answer.

    Ablation: retry every ``unverifiable`` (or every non-trusted) result and the
    call count becomes two."""
    from bmad_loop import codex_trust

    calls = _sequenced_trust(
        monkeypatch,
        [codex_trust.TrustResult(status, why), codex_trust.TrustResult("trusted", "ok")],
    )
    flow = _make_flow(tmp_path, adapters_get=lambda: {"dev": _codex_adapter(), "review": None})
    task = StoryTask(story_key="1-1", epic=1)

    with pytest.raises(_Pause):
        flow.gate_codex_hook_trust(task, tmp_path, roles=("dev",))

    assert calls == [tmp_path]
    assert task.phase == Phase.ESCALATED


# --------------------------------------------------------------- target branch


def test_ensure_target_branch_pins_current_branch(project):
    flow = _make_flow(
        project.repo_root,
        policy=_policy(isolation="worktree"),
        paths=project,
        state=SimpleNamespace(target_branch="", run_id="r", tasks={}),
    )
    flow.ensure_target_branch()
    assert flow.state.target_branch == "main"
    assert "target-branch" in flow.journal.events()
    assert flow.calls.saves == 1


def test_ensure_target_branch_noop_when_not_isolated(project):
    flow = _make_flow(
        project.repo_root,
        policy=_policy(isolation="none"),
        paths=project,
        state=SimpleNamespace(target_branch="", run_id="r", tasks={}),
    )
    flow.ensure_target_branch()
    assert flow.state.target_branch == ""
    assert flow.journal.events() == []
    assert flow.calls.saves == 0


def test_ensure_target_branch_creates_configured_branch(project):
    flow = _make_flow(
        project.repo_root,
        policy=_policy(isolation="worktree", target_branch="release"),
        paths=project,
        state=SimpleNamespace(target_branch="", run_id="r", tasks={}),
    )
    flow.ensure_target_branch()
    assert flow.state.target_branch == "release"
    assert verify.branch_exists(project.repo_root, "release")
    assert verify.current_branch(project.repo_root) == "release"
    assert "target-branch-created" in flow.journal.events()


def test_ensure_target_branch_detached_head_pauses(project):
    head = verify.rev_parse_head(project.repo_root)
    git(project.repo_root, "checkout", "-q", "--detach", head)
    flow = _make_flow(
        project.repo_root,
        policy=_policy(isolation="worktree"),
        paths=project,
        state=SimpleNamespace(target_branch="", run_id="r", tasks={}),
    )
    with pytest.raises(_Pause) as excinfo:
        flow.ensure_target_branch()
    assert "detached HEAD" in excinfo.value.reason
    assert flow.calls.pauses  # escalation_pause was invoked


# --------------------------------------------------------------- run / escalate


def test_run_isolated_relativizes_local_accepted_spec_before_open(tmp_path):
    """Mount creation observes the portable spelling, never the main absolute path.

    Ablation: move normalization below ``_open_unit_workspace`` and the spy sees the
    main-checkout absolute value.
    """
    project = tmp_path / "project"
    artifacts = project / "_bmad-output" / "implementation-artifacts"
    artifacts.mkdir(parents=True)
    spec = artifacts / "spec-1-1.md"
    spec.write_text("spec\n", encoding="utf-8")
    paths = ProjectPaths(
        project=project,
        implementation_artifacts=artifacts,
        planning_artifacts=project / "_bmad-output" / "planning-artifacts",
    )
    task = StoryTask(story_key="1-1", epic=1, spec_file=str(spec))
    observed: list[str | None] = []

    def stop_after_observation(*_args, **_kwargs):
        observed.append(task.spec_file)
        raise verify.GitError("stop after observing pre-open state")

    flow = _make_flow(
        tmp_path,
        paths=paths,
        state=SimpleNamespace(target_branch="main", run_id="run-1", tasks={}),
        open_unit_workspace=stop_after_observation,
    )

    flow.run_isolated(task, lambda _task: pytest.fail("drive must not run"))

    assert observed == ["_bmad-output/implementation-artifacts/spec-1-1.md"]


def test_run_isolated_defers_on_open_failure(tmp_path):
    def boom(*a, **k):
        raise verify.GitError("branch held by a kept-failed unit")

    drove = []
    flow = _make_flow(tmp_path, open_unit_workspace=boom)
    task = StoryTask(story_key="1-1", epic=1)
    flow.run_isolated(task, lambda t: drove.append(t))
    assert task.phase == Phase.DEFERRED
    assert task.defer_reason.startswith("could not open worktree")
    assert "worktree-open-failed" in flow.journal.events()
    assert flow.calls.saves == 1
    assert drove == []  # drive body never ran
    # returned before integration — no merge/close journalled
    assert not any(e.startswith("unit-") for e in flow.journal.events())


@pytest.mark.parametrize(
    "resolve_fault",
    [
        pytest.param(OSError("injected mount resolve fault"), id="oserror"),
        pytest.param(RuntimeError("injected mount resolve fault"), id="runtimeerror"),
        *NUL_PATH_RESOLVE_FAULTS,
    ],
)
def test_mount_resolution_fault_is_typed_and_defers_only_the_unit(
    tmp_path, monkeypatch, resolve_fault
):
    """An uncertain mount is an ordinary per-unit open failure, not a spawn fault.

    Ablation: delete the mount-resolution translation and the raw resolve fault
    escapes ``run_isolated`` instead of reaching DEFERRED/worktree-open-failed.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = ProjectPaths(
        project=repo,
        implementation_artifacts=repo / "_bmad-output/implementation-artifacts",
        planning_artifacts=repo / "_bmad-output/planning-artifacts",
    )
    mount = unit_worktrees_dir(tmp_path) / "1-1"
    refuse_to_resolve(monkeypatch, mount, error=resolve_fault)

    with pytest.raises(verify.GitError) as excinfo:
        open_unit_workspace(repo, paths, "run-1", "1-1", "main", "story", tmp_path)
    assert "worktree mount path" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, type(resolve_fault))
    assert excinfo.value.__cause__.args == resolve_fault.args

    state = SimpleNamespace(
        target_branch="main",
        run_id="run-1",
        source="sprint",
        tasks={},
        crashed=False,
    )
    flow = _make_flow(
        tmp_path,
        paths=paths,
        state=state,
        open_unit_workspace=open_unit_workspace,
    )
    task = StoryTask(story_key="1-1", epic=1)
    drove = []

    flow.run_isolated(task, lambda candidate: drove.append(candidate))

    assert task.phase == Phase.DEFERRED
    assert task.defer_reason.startswith("could not open worktree")
    assert "worktree mount path" in task.defer_reason
    assert flow.journal.events() == ["worktree-open-failed"]
    assert flow.calls.saves == 1
    assert flow.calls.pauses == []  # ordinary GitError, never machine-wide spawn pause
    assert drove == []
    assert state.crashed is False
    assert not mount.exists()


def test_run_isolated_spawn_fault_pauses_instead_of_deferring(tmp_path):
    """#343: a spawn fault is machine-wide, not this unit's — deferring would
    march the whole queue into DEFERRED one notification at a time and end the
    run "finished" over a broken environment. Pause instead; per-unit GitErrors
    still take the defer path.

    Ablation target: delete the `except verify.GitSpawnError` arm in
    `run_isolated` and this fails — the defer arm catches it and the phase
    lands on DEFERRED."""

    def boom(*a, **k):
        raise verify.GitSpawnError("git worktree failed to spawn: [Errno 24] Too many open files")

    drove = []
    flow = _make_flow(tmp_path, open_unit_workspace=boom)
    task = StoryTask(story_key="1-1", epic=1)
    with pytest.raises(_Pause) as excinfo:
        flow.run_isolated(task, lambda t: drove.append(t))
    assert task.phase == Phase.PENDING  # not DEFERRED — nothing was burned
    assert "worktree-open-failed" not in flow.journal.events()
    assert flow.calls.pauses == [(excinfo.value.reason, "1-1")]
    assert "cannot spawn git" in excinfo.value.reason
    assert drove == []  # drive body never ran


def test_run_isolated_escalates_provisioning_root_failure_before_result_probes(
    tmp_path, monkeypatch
):
    """An opened worktree stays mounted when repair cannot identify its roots.

    Ablation: delete the provisioning ``GitError`` catch in ``run_isolated`` and
    this escapes without marking ESCALATED, notifying, saving, or pausing.
    """
    import bmad_loop.worktree_flow as worktree_flow

    repo, wt = tmp_path / "repo", tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    paths = ProjectPaths(
        project=repo,
        implementation_artifacts=repo / "_bmad-output/implementation-artifacts",
        planning_artifacts=repo / "_bmad-output/planning-artifacts",
    )
    unit = UnitWorkspace(
        workspace=Workspace(root=wt, paths=paths.rebased(wt)),
        repo_root=repo,
        branch="bmad-loop/run-1/1-1",
        path=wt,
        baseline="abc123",
    )
    cause = OSError(0, "provider unavailable", None, 64)

    def provisioning_root_failure(*_args, **_kwargs):
        raise verify.GitError("cannot resolve worktree provisioning roots safely") from cause

    monkeypatch.setattr(worktree_flow, "provision_worktree", provisioning_root_failure)
    for probe in (
        "worktree_seed_undelivered",
        "module_skills_seed_undelivered",
        "base_skills_seed_incomplete",
    ):
        monkeypatch.setattr(
            worktree_flow,
            probe,
            lambda *_args, _probe=probe, **_kwargs: pytest.fail(
                f"result probe {_probe} ran after provisioning failed"
            ),
        )
    state = SimpleNamespace(target_branch="main", run_id="run-1", source="sprint", tasks={})
    flow = _make_flow(
        tmp_path,
        paths=paths,
        state=state,
        open_unit_workspace=lambda *_args, **_kwargs: unit,
    )
    task = StoryTask(story_key="1-1", epic=1)
    drove = []

    with pytest.raises(_Pause) as excinfo:
        flow.run_isolated(task, lambda candidate: drove.append(candidate))

    assert task.phase == Phase.ESCALATED
    # The wrapper names the unit; the inner GitError names the cause (#592).
    assert "cannot safely provision the worktree for" in excinfo.value.reason
    assert "cannot resolve worktree provisioning roots safely" in excinfo.value.reason
    assert flow.journal.events() == ["worktree-opened", "story-escalated"]
    assert flow.calls.saves == 1
    assert flow.calls.pauses == [(excinfo.value.reason, "1-1")]
    assert drove == []
    assert task.worktree_path == str(wt)
    assert wt.is_dir()  # retained for inspection; no integration/teardown ran


def test_run_isolated_escalates_an_unparseable_hook_config(tmp_path, monkeypatch):
    """#592: the refusal `provision_worktree` raises over a seeded config that will
    not parse routes to the SAME escalation the root-resolve failure takes — CRITICAL
    notify, run paused, worktree kept — rather than crashing the loop or being
    swallowed into a hooks-only rewrite.

    The raise site is unit-covered in test_install.py; this pins the routing, and that
    the generalized wrapper carries the inner message through INTACT. That message is
    the whole diagnostic — it names the file the operator has to fix — so a wrapper
    that summarized instead of quoting would leave the pause unactionable.

    Ablation: delete the provisioning ``GitError`` catch in ``run_isolated`` and this
    escapes without marking ESCALATED, notifying, saving, or pausing.
    """
    import bmad_loop.worktree_flow as worktree_flow

    repo, wt = tmp_path / "repo", tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    paths = ProjectPaths(
        project=repo,
        implementation_artifacts=repo / "_bmad-output/implementation-artifacts",
        planning_artifacts=repo / "_bmad-output/planning-artifacts",
    )
    unit = UnitWorkspace(
        workspace=Workspace(root=wt, paths=paths.rebased(wt)),
        repo_root=repo,
        branch="bmad-loop/run-1/1-1",
        path=wt,
        baseline="abc123",
    )
    config_path = wt / ".claude" / "settings.json"
    parse_refusal = (
        f"seeded hook config {config_path} cannot be parsed (Expecting ',' delimiter: "
        "line 4 column 3 (char 84)); an unparseable config is evidence of an earlier "
        "fault, not a blank slate — provisioning refuses rather than replace the "
        "operator's allowlist, env, and MCP settings with a hooks-only file; fix or "
        "remove it, then resume (#592)"
    )

    def unparseable_hook_config(*_args, **_kwargs):
        raise verify.GitError(parse_refusal)

    monkeypatch.setattr(worktree_flow, "provision_worktree", unparseable_hook_config)
    state = SimpleNamespace(target_branch="main", run_id="run-1", source="sprint", tasks={})
    flow = _make_flow(
        tmp_path,
        paths=paths,
        state=state,
        open_unit_workspace=lambda *_args, **_kwargs: unit,
    )
    task = StoryTask(story_key="1-1", epic=1)
    drove = []

    with pytest.raises(_Pause) as excinfo:
        flow.run_isolated(task, lambda candidate: drove.append(candidate))

    assert task.phase == Phase.ESCALATED
    assert "cannot safely provision the worktree for 1-1" in excinfo.value.reason
    assert parse_refusal in excinfo.value.reason  # verbatim, not summarized
    assert flow.journal.events() == ["worktree-opened", "story-escalated"]
    assert flow.calls.saves == 1
    assert flow.calls.pauses == [(excinfo.value.reason, "1-1")]
    assert drove == []  # drive body never ran
    assert "CRITICAL escalation: 1-1" in (tmp_path / ATTENTION_FILE).read_text()
    assert wt.is_dir()  # retained for inspection


def test_escalate_unit_marks_escalated_notifies_and_pauses(tmp_path):
    flow = _make_flow(
        tmp_path, state=SimpleNamespace(target_branch="main", run_id="run-9", tasks={})
    )
    task = StoryTask(story_key="2-3", epic=2)
    task.phase = Phase.DONE
    with pytest.raises(_Pause) as excinfo:
        flow.escalate_unit(task, "merge blocked")
    assert task.phase == Phase.ESCALATED
    assert "story-escalated" in flow.journal.events()
    assert flow.calls.saves == 1
    assert flow.calls.pauses == [("merge blocked", "2-3")]
    assert excinfo.value.reason == "merge blocked"
    # notify wrote a CRITICAL line to the run dir's attention file (QUIET file=True)
    assert "CRITICAL escalation: 2-3" in (tmp_path / ATTENTION_FILE).read_text()


def test_reopen_unit_escalates_when_worktree_missing(tmp_path):
    flow = _make_flow(tmp_path, state=SimpleNamespace(target_branch="main", run_id="r", tasks={}))
    task = StoryTask(story_key="1-1", epic=1)
    task.worktree_path = str(tmp_path / "gone")  # never created
    with pytest.raises(_Pause) as excinfo:
        flow.reopen_unit(task)
    assert "is gone" in excinfo.value.reason


def test_gc_run_worktrees_noop_when_not_isolated(tmp_path):
    flow = _make_flow(tmp_path, policy=_policy(isolation="none"))
    flow.gc_run_worktrees()  # returns before touching git
    assert flow.journal.events() == []


# --------------------------------------------------------------- module contract


def test_provision_worktree_reexported_from_install():
    # F-9a: provision_worktree lives here now; install re-exports the same object
    # (lazily) so its own tests and any external importer keep working.
    assert install_provision_worktree is provision_worktree


def test_setup_mcp_agent_id_mapping():
    # only claude carries the "-code" suffix; everything else passes through
    assert _setup_mcp_agent_id("claude") == "claude-code"
    assert _setup_mcp_agent_id("codex") == "codex"
    assert _setup_mcp_agent_id("gemini") == "gemini"
    assert _setup_mcp_agent_id("cursor") == "cursor"
    assert _setup_mcp_agent_id("some-custom-profile") == "some-custom-profile"


def test_gc_retains_unpublished_bundle_sources(project, tmp_path):
    mount = tmp_path / "mounted-unit"
    mount.mkdir()
    task = StoryTask(
        story_key="dw-fix", epic=0, phase=Phase.DONE, dw_ids=["DW-1"], worktree_path=str(mount)
    )
    state = SimpleNamespace(target_branch="main", run_id="run-1", tasks={task.story_key: task})
    flow = _make_flow(tmp_path, paths=project, state=state, policy=_policy(isolation="worktree"))
    with pytest.raises(_Pause, match="publication incomplete"):
        flow.gc_run_worktrees()
    assert mount.is_dir()


def test_gc_reclaims_published_awaiting_operator_source(project, tmp_path, monkeypatch):
    import bmad_loop.worktree_flow as worktree_flow

    mount = tmp_path / "mounted-unit"
    mount.mkdir()
    task = StoryTask(
        story_key="dw-fix",
        epic=0,
        phase=Phase.AWAITING_OPERATOR,
        dw_ids=["DW-1"],
        worktree_path=str(mount),
        artifact_publication_complete=True,
    )
    state = SimpleNamespace(target_branch="main", run_id="run-1", tasks={task.story_key: task})
    flow = _make_flow(tmp_path, paths=project, state=state, policy=_policy(isolation="worktree"))

    def discard(_repo, path, _branch, **_kwargs):
        Path(path).rmdir()

    monkeypatch.setattr(worktree_flow, "discard_worktree", discard)
    monkeypatch.setattr(worktree_flow.verify, "worktree_prune", lambda *_: None)
    flow.gc_run_worktrees()
    assert not mount.exists()


def test_gc_legacy_bundle_with_already_removed_mount_stays_compatible(project, tmp_path):
    task = StoryTask(
        story_key="dw-old",
        epic=0,
        phase=Phase.DONE,
        dw_ids=["DW-1"],
        worktree_path=str(tmp_path / "removed-before-upgrade"),
    )
    state = SimpleNamespace(target_branch="main", run_id="run-1", tasks={task.story_key: task})
    flow = _make_flow(tmp_path, paths=project, state=state, policy=_policy(isolation="worktree"))
    flow.gc_run_worktrees()
    assert flow.calls.pauses == []


# ------------------------------------------------ nested repo_root (DW-379)


def test_nested_ledger_board_and_accepted_spec_seeds_land_under_the_mount_project(
    project, tmp_path
):
    """DW-379: under a nested `repo_root` the orchestrator-owned seeds are spelled
    PROJECT-relative (`_artifact_seed` against the main project) and provisioning,
    handed the project, lands them at the mount project `<worktree>/app/...` — never at
    the checkout root, where the same relative spelling names the outer tree. The
    accepted spec's locator targets the mount project too.

    Ablation: seed against the checkout roots (`self.paths.repo_root, worktree`) and
    the rels come back `app/_bmad-output/...`, which provisioning then lands at
    `<worktree>/app/app/...`."""
    from conftest import nested_repo_root_paths

    paths = nested_repo_root_paths(project)
    repo, app = paths.repo_root, paths.project
    impl_rel = paths.implementation_artifacts.relative_to(app).as_posix()
    # untracked in main, so a fresh checkout cannot deliver any of the three
    paths.deferred_work.write_text("# ledger\n", encoding="utf-8")
    paths.sprint_status.write_text("development_status:\n  1-1-a: ready-for-dev\n")
    accepted = paths.implementation_artifacts / "spec-1-1-a.md"
    accepted.write_text("---\nstatus: ready-for-dev\n---\n", encoding="utf-8")
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    flow = _make_flow(tmp_path, paths=paths)
    task = StoryTask("1-1-a", 1, spec_file=f"{impl_rel}/spec-1-1-a.md")

    seeds = [
        *flow._ledger_seed(wt),
        *flow._board_seed(wt),
        *flow._accepted_spec_seed(task, wt, project_relative_only=True),
    ]

    assert seeds == [
        f"{impl_rel}/deferred-work.md",
        f"{impl_rel}/sprint-status.yaml",
        f"{impl_rel}/spec-1-1-a.md",
    ]
    ends = flow._accepted_spec_pair(task, wt, project_relative_only=True)
    assert ends.destination == (wt / "app" / impl_rel / "spec-1-1-a.md").resolve()

    assert provision_worktree(wt, [], repo, seed_files=seeds, project=app) == []
    for rel in seeds:
        assert (wt / "app" / rel).is_file(), rel
        assert not (wt / rel).exists(), rel
