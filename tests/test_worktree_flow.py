"""Unit tests for the WorktreeFlow collaborator (issue #244 F-3/F-9a).

WorktreeFlow was carved out of Engine's worktree isolation/integration cluster.
These exercise it in isolation — built from narrow deps + stub engine callbacks,
no Engine instance — which is the point of the extraction. End-to-end behavior
under a real Engine stays covered by test_engine_worktree.py.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import git, refuse_to_resolve

from bmad_loop import verify
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
from bmad_loop.worktree_flow import WorktreeFlow, _setup_mcp_agent_id, provision_worktree

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
            else SimpleNamespace(target_branch="", run_id="run-1", tasks={})
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


def test_mount_resolution_fault_is_typed_and_defers_only_the_unit(tmp_path, monkeypatch):
    """An uncertain mount is an ordinary per-unit open failure, not a spawn fault.

    Ablation: delete the mount-resolution translation and the raw provider fault
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
    refuse_to_resolve(monkeypatch, mount)

    with pytest.raises(verify.GitError) as excinfo:
        open_unit_workspace(repo, paths, "run-1", "1-1", "main", "story", tmp_path)
    assert "worktree mount path" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, OSError)

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
