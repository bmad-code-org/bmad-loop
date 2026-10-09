"""`unitreplay.mint_replay_run` (DW-525): the transaction that mints a replay run for a
finished run's kept DEFERRED worktree unit. The CLI flow and the replay engine run end
to end in `tests/test_engine_worktree.py` (`resolve <finished-run> --reverify`); this
file pins the mint itself — what it publishes, and that a failure after the worktree
move puts everything back."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_engine_worktree import _finished_kept_unit, _state_bytes

from bmad_loop import runs, unitreplay, verify
from bmad_loop.journal import Journal, load_state, save_state
from bmad_loop.model import Phase
from bmad_loop.platform_util import root_identity_record

_KEY = "1-1-a"


def _mint(project, engine, **kwargs):
    task = load_state(engine.run_dir).tasks[_KEY]
    params = dict(
        project=project.project,
        paths=project,
        policy=engine.policy,
        finished_run_dir=engine.run_dir,
        story_key=_KEY,
        expected_generation=task.generation,
        trusted_config_digest="pin",
    )
    params.update(kwargs)
    return unitreplay.mint_replay_run(**params)


def _run_dirs(engine) -> list[Path]:
    return sorted(p for p in engine.run_dir.parent.iterdir() if p != engine.run_dir)


def test_mint_publishes_a_latched_replay_run_and_leaves_the_finished_run_alone(project, tmp_path):
    engine, _marker = _finished_kept_unit(project, tmp_path)
    finished_before = _state_bytes(engine.run_dir)
    old = load_state(engine.run_dir).tasks[_KEY]
    src = Path(old.worktree_path)

    replay_dir = _mint(project, engine)

    # the finished run: state.json byte-identical, one handoff pointer appended
    assert _state_bytes(engine.run_dir) == finished_before
    handoff = Journal(engine.run_dir).entries()[-1]
    assert handoff["kind"] == "unit-replay-handoff"
    assert handoff["replay_run"] == replay_dir.name and handoff["from_worktree"] == str(src)
    # the worktree moved under the replay run and is registered there
    replay = load_state(replay_dir)
    task = replay.tasks[_KEY]
    dst = Path(task.worktree_path)
    assert dst == replay_dir / "worktrees" / _KEY and dst.is_dir() and not src.exists()
    assert verify.worktree_is_registered(project.repo_root, dst)
    assert not verify.worktree_is_registered(project.repo_root, src)
    assert verify.current_branch(dst) == old.branch == task.branch
    assert task.worktree_identity == root_identity_record(dst)
    # the seeded task, latched exactly as the paused re-arm latches it
    assert task.phase == Phase.DEV_VERIFY and task.reverify_from == "deferred"
    assert task.defer_reason is None and task.generation == old.generation + 1
    assert task.baseline_commit == old.baseline_commit
    record = task.sessions[-1]
    assert record.result_json is not None
    assert Path(record.result_json["spec_file"]).is_relative_to(dst)
    assert Path(record.result_json["spec_file"]).is_file()
    # the replay run: its own record, scoped to the one unit, resumable
    assert replay.replay_of == engine.run_dir.name and replay.run_type == "story"
    assert replay.story_filter == _KEY and replay.max_stories == 1
    assert replay.target_branch == load_state(engine.run_dir).target_branch
    assert list(replay.tasks) == [_KEY]
    assert not replay.finished and not replay.paused
    [start] = Journal(replay_dir).entries()
    assert start["kind"] == "unit-replay-start"
    assert start["replay_of"] == engine.run_dir.name and start["story_key"] == _KEY
    assert start["from_worktree"] == str(src) and start["worktree"] == str(dst)
    assert start["branch"] == old.branch and start["baseline"] == old.baseline_commit


def test_mint_failure_after_the_move_restores_the_worktree_and_removes_the_run(
    project, tmp_path, monkeypatch
):
    """A failure after `git worktree move` (here: the replay run's state write) moves
    the worktree back to the finished run and removes the claimed run dir.

    Ablation: delete the move-back in `mint_replay_run`'s except arm and the
    worktree is lost with the removed run dir (the original path no longer exists)."""
    engine, _marker = _finished_kept_unit(project, tmp_path)
    finished_before = _state_bytes(engine.run_dir)
    journal_before = Journal(engine.run_dir).entries()
    src = Path(load_state(engine.run_dir).tasks[_KEY].worktree_path)

    def broken_save(_run_dir, _state):
        raise OSError("disk full")

    monkeypatch.setattr(unitreplay, "save_state", broken_save)

    with pytest.raises(OSError, match="disk full"):
        _mint(project, engine)

    assert src.is_dir() and verify.worktree_is_registered(project.repo_root, src)
    assert verify.current_branch(src) == load_state(engine.run_dir).tasks[_KEY].branch
    assert _run_dirs(engine) == []
    assert _state_bytes(engine.run_dir) == finished_before
    assert Journal(engine.run_dir).entries() == journal_before


def test_mint_names_both_paths_when_the_move_back_fails(project, tmp_path, monkeypatch):
    """The move-back is a repair write: when it fails too, the error names where the
    worktree is and where it came from, and the claimed dir that holds it is kept."""
    engine, _marker = _finished_kept_unit(project, tmp_path)
    src = Path(load_state(engine.run_dir).tasks[_KEY].worktree_path)
    real_move = verify.worktree_move
    calls = []

    def move(repo, a, b):
        calls.append((a, b))
        if len(calls) > 1:
            raise verify.GitError("cannot move back")
        real_move(repo, a, b)

    def broken_save(_run_dir, _state):
        raise OSError("disk full")

    monkeypatch.setattr(verify, "worktree_move", move)
    monkeypatch.setattr(unitreplay, "save_state", broken_save)

    with pytest.raises(unitreplay.ReplayError) as exc:
        _mint(project, engine)

    [replay_dir] = _run_dirs(engine)
    dst = replay_dir / "worktrees" / _KEY
    assert str(dst) in str(exc.value) and str(src) in str(exc.value)
    assert "disk full" in str(exc.value)  # the reason the replay failed survives
    assert dst.is_dir()


def test_mint_restores_a_worktree_the_failing_move_already_renamed(project, tmp_path, monkeypatch):
    """git renames the tree before updating its admin files, so a move that RAISES
    may still have moved it: the rollback checks the destination rather than the
    move's return, puts the tree back and removes the claimed dir.

    Ablation: record `moved` only after `verify.worktree_move` returns and the
    claimed dir is removed with the moved worktree inside it."""
    engine, _marker = _finished_kept_unit(project, tmp_path)
    finished_before = _state_bytes(engine.run_dir)
    src = Path(load_state(engine.run_dir).tasks[_KEY].worktree_path)
    real_move = verify.worktree_move
    calls = []

    def move(repo, a, b):
        calls.append((a, b))
        real_move(repo, a, b)
        if len(calls) == 1:
            raise verify.GitError("git died after the rename")

    monkeypatch.setattr(verify, "worktree_move", move)

    with pytest.raises(unitreplay.ReplayError, match="git died after the rename"):
        _mint(project, engine)

    assert len(calls) == 2  # moved, then moved back
    assert src.is_dir() and verify.worktree_is_registered(project.repo_root, src)
    assert _run_dirs(engine) == []
    assert _state_bytes(engine.run_dir) == finished_before


def test_mint_locked_recheck_refuses_a_changed_story(project, tmp_path):
    """The lock-free CLI checks saw one generation; a different one under the lock
    refuses before anything is claimed or moved."""
    engine, _marker = _finished_kept_unit(project, tmp_path)
    finished_before = _state_bytes(engine.run_dir)
    task = load_state(engine.run_dir).tasks[_KEY]

    with pytest.raises(unitreplay.ReplayError, match="changed while resolve"):
        _mint(project, engine, expected_generation=task.generation + 1)

    assert Path(task.worktree_path).is_dir()
    assert _run_dirs(engine) == []
    assert _state_bytes(engine.run_dir) == finished_before


@pytest.mark.parametrize("live", ["alive", "unknown"])
def test_mint_locked_recheck_refuses_a_live_engine(project, tmp_path, monkeypatch, live):
    """Under the lock a provably-live engine refuses, and an unverifiable one does
    without `force` — before anything is claimed or moved."""
    engine, _marker = _finished_kept_unit(project, tmp_path)
    finished_before = _state_bytes(engine.run_dir)
    src = Path(load_state(engine.run_dir).tasks[_KEY].worktree_path)
    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: live)

    with pytest.raises(unitreplay.ReplayError, match="still live|unverifiable pid"):
        _mint(project, engine, force=False)

    assert src.is_dir() and _run_dirs(engine) == []
    assert _state_bytes(engine.run_dir) == finished_before


def test_mint_force_admits_an_unknown_liveness(project, tmp_path, monkeypatch):
    engine, _marker = _finished_kept_unit(project, tmp_path)
    monkeypatch.setattr(runs, "engine_liveness", lambda _rd: "unknown")

    replay_dir = _mint(project, engine, force=True)

    assert _run_dirs(engine) == [replay_dir]
    assert load_state(replay_dir).replay_of == engine.run_dir.name


def test_mint_locked_recheck_catches_a_refusal_arising_after_the_callers_checks(project, tmp_path):
    """The worktree vanished between the caller's lock-free checks and the mint: the
    refusal is re-run under the lock and nothing is claimed."""
    engine, _marker = _finished_kept_unit(project, tmp_path)
    finished_before = _state_bytes(engine.run_dir)
    src = Path(load_state(engine.run_dir).tasks[_KEY].worktree_path)
    verify.worktree_remove(project.repo_root, src, force=True)

    with pytest.raises(unitreplay.ReplayError, match="is gone"):
        _mint(project, engine)

    assert _run_dirs(engine) == []
    assert _state_bytes(engine.run_dir) == finished_before


def test_mint_rehomes_an_absolute_restore_patch_into_the_moved_worktree(project, tmp_path):
    """A latched intent-gap patch spelled absolutely inside the kept tree follows the
    move, as the spec paths do — the engine re-applies it after every reset.

    Ablation: drop the `restore_patch` rehome in `unitreplay._replay_state` and the
    seeded latch still points into the moved-away tree."""
    engine, _marker = _finished_kept_unit(project, tmp_path)
    state = load_state(engine.run_dir)
    src = Path(state.tasks[_KEY].worktree_path)
    state.tasks[_KEY].restore_patch = str(src / "intent-gap.patch")
    save_state(engine.run_dir, state)

    replay_dir = _mint(project, engine)

    task = load_state(replay_dir).tasks[_KEY]
    assert task.restore_patch == str(Path(task.worktree_path) / "intent-gap.patch")
