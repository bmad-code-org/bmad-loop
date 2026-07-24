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
from conftest import git

from bmad_loop import verify
from bmad_loop.gates import ATTENTION_FILE
from bmad_loop.install import provision_worktree as install_provision_worktree
from bmad_loop.model import Phase, StoryTask
from bmad_loop.policy import GatesPolicy, LimitsPolicy, NotifyPolicy, Policy, ScmPolicy
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
        saves=0, emits=[], gates=[], pauses=[], workspaces=[workspace], integrated=[]
    )

    def _save() -> None:
        calls.saves += 1

    def _emit(stage, task=None, **fields):
        calls.emits.append(stage)
        return None

    def _gate_unit(task) -> bool:
        calls.gates.append(task)
        return True

    def _pause(reason, story_key="", *, cause=None):
        calls.pauses.append((reason, story_key))
        raise _Pause(reason, story_key)

    flow = WorktreeFlow(
        paths=paths if paths is not None else SimpleNamespace(repo_root=tmp_path),
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
        escalation_pause=_pause,
        workspace_get=lambda: calls.workspaces[-1],
        workspace_set=lambda ws: calls.workspaces.append(ws),
        on_integrated=lambda task: calls.integrated.append(task),
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


def test_integrate_unit_runs_post_integration_bookkeeping_after_a_merge(tmp_path):
    """The integration chokepoint is the only safe point for bookkeeping the
    unit's own commit could not carry (an out-of-repo deferred-work ledger, #234).
    It fires after merge_local, so it is reached only by a unit that landed."""
    flow = _make_flow(tmp_path, state=SimpleNamespace(target_branch="main", run_id="r", tasks={}))
    merged = []

    def merge(task, unit):
        # the stamp lives inside merge_local, next to the merge it is proof of
        # (see test_merge_local_persists_unit_merged_before_its_journal_tail)
        merged.append(task)
        task.unit_merged = True
        flow._save()

    flow.merge_local = merge
    task = StoryTask(story_key="1-1", epic=1)
    task.phase = Phase.DONE
    # capture the durable-proof flag AS the bookkeeping runs, not after it returns:
    # asserting `task.unit_merged` at the end passes for either ordering, and the
    # ordering is the contract — `_reconcile_pending_deferred_closes` keys the
    # post-crash retry on this flag, and phase DONE is not a substitute for it
    # (the commit stamps DONE, before integration).
    seen = []
    inner = flow._on_integrated
    flow._on_integrated = lambda t: (seen.append((t.unit_merged, flow.calls.saves)), inner(t))[1]

    flow.integrate_unit(task, unit=None)

    assert merged == [task]
    assert flow.calls.integrated == [task]
    assert seen == [(True, 1)]  # flag set AND persisted before the callback fired


def test_merge_local_persists_unit_merged_before_its_journal_tail(tmp_path, monkeypatch):
    """`unit_merged` is the durable proof `_reconcile_pending_deferred_closes`
    keys its post-crash retry on, so the merge itself must stamp it — not
    everything that follows the merge.

    The tail after `verify.merge_branch` returns (a journal append, the post_merge
    emit, the worktree teardown) can each still raise while saying nothing about
    whether the merge happened. Stamping after that tail left the code durably
    merged with persisted state claiming it was not, and resume then abandoned the
    external-ledger closure it was owed (#284 round-5 review, finding 3)."""
    flow = _make_flow(tmp_path, state=SimpleNamespace(target_branch="main", run_id="r", tasks={}))
    monkeypatch.setattr(verify, "clean_incoming_collisions", lambda *a, **kw: [])
    monkeypatch.setattr(verify, "merge_branch", lambda *a, **kw: None)

    def full_disk(event, **fields):
        raise OSError("No space left on device")

    flow.journal.append = full_disk  # the first fallible step after the merge
    task = StoryTask(story_key="1-1", epic=1)
    task.phase = Phase.DONE

    with pytest.raises(OSError):
        flow.merge_local(task, unit=SimpleNamespace(branch="unit/1-1"))

    assert task.unit_merged  # the merge landed, and the proof of it survived
    assert flow.calls.saves == 1  # persisted, not just set in memory


def test_integrate_unit_skips_bookkeeping_when_the_merge_escalates(tmp_path):
    """A merge that escalates raises out of merge_local. Nothing downstream may
    claim the unit's work landed — a shared ledger must still read `open`."""
    flow = _make_flow(tmp_path, state=SimpleNamespace(target_branch="main", run_id="r", tasks={}))

    def boom(task, unit):
        raise _Pause("merge blocked", task.story_key)

    flow.merge_local = boom
    task = StoryTask(story_key="1-1", epic=1)
    task.phase = Phase.DONE

    with pytest.raises(_Pause):
        flow.integrate_unit(task, unit=None)

    assert flow.calls.integrated == []


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
    assert _setup_mcp_agent_id("claude") == "claude-code"
    assert _setup_mcp_agent_id("codex") == "codex"
