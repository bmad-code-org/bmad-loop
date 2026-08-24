"""The committed per-story park records (#335 part 3, #356).

The record is what `bmad-loop confirm` can find; the spec and the board are what
it is allowed to believe. These tests pin both halves: the store round-trip
(committed per-story records over a legacy machine-local index), and the join
back onto the committed truth that decides whether an entry is confirmable or
drifted.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from conftest import git, install_bmad_config, spec_path, write_spec, write_sprint

from bmad_loop import devcontract, operatoractions, platform_util, verify

ACTIONS = ["buy example.com at the registrar", "publish the _acme-challenge TXT record"]


def _record_only(paths, key="1-1-a", **kw):
    """Write a park record WITHOUT the committed state it points at, so a test
    about the store itself is not confounded by the spec/board a park also
    leaves."""
    operatoractions.record_park(
        paths.project,
        key,
        actions=kw.get("actions", ACTIONS),
        spec_file=kw.get("spec_file", "spec.md"),
        run_id="run-1",
        parked_at="2026-07-28",
    )


def _legacy_entry(paths, key="1-1-a", *, actions=None, commit="abcdef1234567890"):
    """Write a pre-#356 machine-local index entry byte-shaped the way the old
    version wrote it — including the stored `commit` the new records no longer
    carry."""
    store = operatoractions.legacy_store_path(paths.project)
    store.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(store.read_text()) if store.is_file() else {}
    data[key] = {
        "actions": ACTIONS if actions is None else actions,
        "spec_file": "spec.md",
        "commit": commit,
        "run_id": "run-legacy",
        "parked_at": "2026-07-01",
    }
    store.write_text(json.dumps(data, indent=2, sort_keys=True))


def _park(paths, key="1-1-a", *, actions=None, status="awaiting-operator", board=None):
    """Record a park and write the committed state it points at."""
    if not (paths.project / "_bmad" / "bmm" / "config.yaml").is_file():
        install_bmad_config(paths)
    acts = ACTIONS if actions is None else actions
    spec = spec_path(paths, key)
    write_spec(spec, status, "base", operator_actions=acts)
    board_now = {}
    if paths.sprint_status.is_file():
        import yaml

        board_now = yaml.safe_load(paths.sprint_status.read_text())["development_status"]
    write_sprint(paths, {**board_now, "epic-1": "in-progress", key: board or status})
    operatoractions.record_park(
        paths.project,
        key,
        actions=list(acts) if isinstance(acts, list) else [],
        spec_file=spec.relative_to(paths.project).as_posix(),
        run_id="run-1",
        parked_at="2026-07-28",
    )
    return spec


# ------------------------------------------------------------------ store I/O


def test_record_and_load_round_trip(project):
    _park(project)
    data = operatoractions.load(project.project)
    assert list(data) == ["1-1-a"]
    assert data["1-1-a"]["story_key"] == "1-1-a"
    assert data["1-1-a"]["actions"] == ACTIONS
    assert data["1-1-a"]["run_id"] == "run-1"
    assert data["1-1-a"]["parked_at"] == "2026-07-28"
    # never stored: the record rides the very commit it would name (#356), so
    # provenance is derived at read time instead
    assert "commit" not in data["1-1-a"]


def test_re_parking_replaces_rather_than_accumulates(project):
    """A story owes whatever its LATEST park says it owes. Appending would leave
    a human acknowledging actions an earlier attempt declared and this one
    dropped."""
    _park(project)
    operatoractions.record_park(
        project.project,
        "1-1-a",
        actions=["only this one now"],
        spec_file="spec.md",
        run_id="run-2",
        parked_at="2026-07-29",
    )
    data = operatoractions.load(project.project)
    assert data["1-1-a"]["actions"] == ["only this one now"]
    assert data["1-1-a"]["run_id"] == "run-2"


def test_drop_removes_the_record_and_reports_whether_it_was_there(project):
    _park(project)
    assert operatoractions.drop(project.project, "1-1-a") is True
    assert operatoractions.load(project.project) == {}
    # a second drop is a no-op, not an error — confirming a story the store never
    # knew about must not create a file just to delete from it
    assert operatoractions.drop(project.project, "1-1-a") is False
    assert operatoractions.drop(project.project, "never-existed") is False


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("{ not json", "malformed JSON"),
        ("[1, 2, 3]", "a JSON array where the store is an object"),
        ('"a string"', "a bare JSON scalar"),
    ],
)
def test_load_degrades_on_an_unusable_legacy_store(project, content, why):
    """The legacy index was a convenience over committed truth, so an unreadable
    one reads as "no index" rather than blocking a human from confirming their
    own work."""
    store = operatoractions.legacy_store_path(project.project)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(content)
    assert operatoractions.load(project.project) == {}, why


def test_a_mangled_record_file_is_still_listed(project):
    """A record file that cannot be parsed still NAMES an obligation — its
    presence in the tree is the committed claim. It degrades to an empty entry
    under its filename stem, which `resolve` reports as drift ("no spec file
    could be located for it"), rather than vanishing the way a mangled
    legacy store does: the legacy store was one file for every story, so
    degrading it wholesale lost nothing attributable; a per-story file IS the
    attribution."""
    rp = operatoractions.record_path(project.project, "1-1-a")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("{ not json")
    assert operatoractions.load(project.project) == {"1-1-a": {}}


def test_load_of_an_absent_store_is_empty(project):
    assert operatoractions.load(project.project) == {}


def test_the_record_is_written_atomically_and_parsable(project):
    _park(project)
    raw = operatoractions.record_path(project.project, "1-1-a").read_text()
    assert json.loads(raw)  # valid JSON, not a half-written temp
    assert not list(operatoractions.records_dir(project.project).glob("*.tmp"))


def test_a_failed_record_write_leaves_no_tmp_residue(project, monkeypatch):
    """The record is written just ahead of `finalize_commit`'s `git add -A`, so
    a stranded temp would ride the story's own commit — one story's half-write
    polluting its own history forever.

    The fault is injected at the PUBLISH, one layer BELOW the helper `record_park`
    delegates to (#379, #593), and that placement is the whole test: stubbing
    `atomic_write_text_confined` itself would mean no temp was ever created, so
    "no residue" would pass for that reason rather than for the unlink. Faulting
    the publish instead runs the helper's real body — temp create, write, fsync —
    so the assertion grades a temp that genuinely existed.

    `os.replace`, not `platform_util.atomic_replace`: the confined writer's
    anchored arm publishes with a bare dir_fd-relative `os.replace` and never
    reaches `atomic_replace` at all, so the old injection point would no longer
    fire and this test would pass having faulted nothing. Filtered to the record's
    own name so an unrelated replace during the test is not collateral.

    Ablation: drop the `os.unlink(tmp, dir_fd=dir_fd)` in
    `platform_util._atomic_write_at`'s except arm and this fails."""
    real_replace = os.replace

    def boom(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if str(dst).endswith(".json"):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        operatoractions.record_park(
            project.project,
            "1-1-a",
            actions=ACTIONS,
            spec_file="spec.md",
            run_id="run-1",
            parked_at="2026-07-28",
        )
    records = operatoractions.records_dir(project.project)
    assert not list(records.glob("*.tmp")) and not list(records.glob("*.json"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_record_replaces_a_planted_symlink_by_name(project, tmp_path):
    """#379. The one row that grades this SITE's choice of a writer that replaces
    the NAME, rather than the helper's implementation of it, which
    `test_platform_util.py` pins by calling the helper directly. The site no
    longer spells that choice as `follow_symlinks=False`: since #593 it calls
    `atomic_write_text_confined`, which is no-follow by construction — the
    anchored arm publishes with a dir_fd-relative `os.replace` that does not
    dereference its destination, and the win32 arm passes the no-follow itself.

    Behaviour-preserving: the hand-rolled write this replaced ended in
    `os.replace`, which never dereferenced its destination either, so passing the
    default True here would have CHANGED what `record_park` does rather than
    merely relaxing it. It is also what the record IS — a machine-minted file
    under a project root a driven session writes all run long — so honouring a
    link planted at the record's own name would turn the park's bookkeeping into
    a host-side write at a path of that session's choosing.

    Ablation: swap `record_park`'s writer for
    `atomic_write_text(path, ...)` at its follow-the-link default and this reddens
    alone — the link survives and the planted target is what gets rewritten."""
    outside = tmp_path / "someone-elses-file"
    outside.write_text("not a park record\n", encoding="utf-8")
    link = operatoractions.record_path(project.project, "1-1-a")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    _record_only(project)

    assert not link.is_symlink()  # the NAME was replaced
    assert operatoractions.load(project.project)["1-1-a"]["actions"] == ACTIONS
    assert outside.read_text(encoding="utf-8") == "not a park record\n"  # not through


# ------------------------------------------------------- git sees the record


def test_a_park_record_is_visible_to_git(project):
    """The design premise of #356, INVERTED from the machine-local index this
    store replaced: the record is written inside the story's commit window
    precisely so `finalize_commit`'s `git add -A` folds it into the story's own
    commit — which requires git to SEE it. An excluded record would silently
    fall out of every commit and the fresh-clone acceptance would be a lie.

    Ablation: re-add an exclude-from-git call to `record_park` (the legacy
    `_worktree_local_exclude` pattern) and this fails — the tree stays clean and
    nothing would ever commit the record."""
    assert verify.worktree_clean(project.project)
    _record_only(project)
    assert operatoractions.record_path(project.project, "1-1-a").is_file()
    assert not verify.worktree_clean(project.project)


def test_a_record_does_not_touch_the_repository_exclude_file(project):
    """The legacy store registered itself in `.git/info/exclude` at every write.
    The committed record must not: an exclude line would hide it from the very
    `git add -A` it exists to ride."""
    _record_only(project)
    exclude = project.project / ".git" / "info" / "exclude"
    if exclude.is_file():
        assert ".bmad-loop/operator" not in exclude.read_text()


def test_the_record_write_survives_a_non_git_project(tmp_path):
    """A project with no git at all must not raise: the record is still a
    correct record, and `confirm` still works from the files alone."""
    operatoractions.record_park(
        tmp_path,
        "1-1-a",
        actions=["do the thing"],
        spec_file="spec.md",
        run_id="r",
        parked_at="2026-07-28",
    )
    assert operatoractions.load(tmp_path)["1-1-a"]["actions"] == ["do the thing"]


# ------------------------------------------- the legacy machine-local index


def test_load_merges_legacy_entries_under_per_story_records(project):
    """A park written by a pre-#356 version must stay confirmable on the machine
    that wrote it, so `load` still reads the legacy store — but a per-story
    record wins its key: the record travels with the commit, the legacy entry is
    a machine-local cache."""
    _legacy_entry(project, "1-1-a", actions=["the stale legacy copy"])
    _legacy_entry(project, "2-2-b")
    _record_only(project, "1-1-a")
    data = operatoractions.load(project.project)
    assert data["1-1-a"]["actions"] == ACTIONS  # the record won
    assert data["2-2-b"]["actions"] == ACTIONS  # legacy-only key still listed
    assert data["2-2-b"]["commit"] == "abcdef1234567890"


def test_drop_prunes_the_legacy_entry_too(project):
    """Confirming a story must remove it EVERYWHERE it is listed, or `validate`
    warns forever about a story that was genuinely confirmed. The emptied legacy
    store is deleted outright rather than rewritten as `{}` — nothing writes it
    anymore, and an empty husk would linger as bookkeeping nobody owns."""
    _legacy_entry(project, "1-1-a")
    _legacy_entry(project, "2-2-b")
    _record_only(project, "1-1-a")
    assert operatoractions.drop(project.project, "1-1-a") is True
    assert "1-1-a" not in operatoractions.load(project.project)
    assert operatoractions.legacy_store_path(project.project).is_file()  # 2-2-b remains
    assert operatoractions.drop(project.project, "2-2-b") is True
    assert not operatoractions.legacy_store_path(project.project).is_file()


def test_a_failed_legacy_prune_leaves_no_tmp_residue(project, monkeypatch):
    """`drop` runs out of band from `confirm`, not inside a commit window, so a
    stranded `.bmad-loop/operator-actions.tmp` does not ride a commit — it sits
    UNTRACKED (`.bmad-loop/` is not ignored, and the pre-#356 exclude line named
    the `.json` only), which `verify.worktree_clean` reads as a dirty tree that
    blocks the next run's preflight and the epic-boundary auto-sweep.

    TWO entries, deliberately: with one, the prune takes the `else` arm and
    unlinks the store outright, never reaching the rewrite. That is what the
    `pytest.raises` is for — it fails LOUDLY on `DID NOT RAISE` rather than
    letting the residue assertion pass over a rewrite that never ran.

    Faulted at the publish for its sibling row's reason (#379, #593): below the
    helper, so a temp is really created and really unlinked — and at `os.replace`
    rather than `platform_util.atomic_replace`, because the confined writer's
    anchored arm publishes dir_fd-relative and never reaches `atomic_replace`.

    Ablation: drop the `os.unlink(tmp, dir_fd=dir_fd)` in
    `platform_util._atomic_write_at`'s except arm and this fails."""
    real_replace = os.replace

    def boom(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if str(dst).endswith(".json"):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    _legacy_entry(project, "1-1-a")
    _legacy_entry(project, "2-2-b")
    store = operatoractions.legacy_store_path(project.project)
    before = sorted(p.name for p in store.parent.iterdir())
    assert before == ["operator-actions.json"]  # a snapshot with something IN it

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        operatoractions.drop(project.project, "1-1-a")

    # Equality against that non-empty snapshot, not `not glob("*.tmp")`: the
    # bare negative would pass just as happily on a `.bmad-loop/` that was never
    # created. The failed rewrite also left the store itself as it found it, so
    # the entry stays confirmable once whatever broke the write is fixed.
    assert sorted(p.name for p in store.parent.iterdir()) == before
    assert sorted(operatoractions.load(project.project)) == ["1-1-a", "2-2-b"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_legacy_prune_replaces_a_planted_symlink_by_name(project, tmp_path):
    """The `_drop_legacy` half of the record row above (#379): same property, a
    different write, and its own ablation. Reachable for a different reason too —
    `drop` is what `bmad-loop confirm` calls, out of band and on a machine whose
    repo a driven session has been writing all run long.

    TWO entries again, so the prune takes the rewrite arm; the one-entry `else`
    arm unlinks the store outright and never reaches the helper at all.

    Ablation: swap `_drop_legacy`'s writer for
    `atomic_write_text(path, ...)` at its follow-the-link default and this reddens
    alone — the link survives and the planted target is what gets rewritten."""
    _legacy_entry(project, "1-1-a")
    _legacy_entry(project, "2-2-b")
    store = operatoractions.legacy_store_path(project.project)
    kept = store.read_text(encoding="utf-8")
    outside = tmp_path / "someone-elses-file"
    outside.write_text(kept, encoding="utf-8")  # the store's real bytes, behind the link
    store.unlink()
    store.symlink_to(outside)

    assert operatoractions.drop(project.project, "1-1-a") is True

    assert not store.is_symlink()  # the NAME was replaced
    assert sorted(operatoractions.load(project.project)) == ["2-2-b"]
    assert outside.read_text(encoding="utf-8") == kept  # not written through


def test_drop_matches_the_records_own_key_not_its_filename(project):
    """The record's `story_key` field is authoritative over the filename stem
    (`safe_segment` can mangle a hostile key, a human can rename a file). A drop
    keyed only on the computed path would leave a renamed record behind —
    resolvable, confirmable, and then impossible to remove."""
    rp = operatoractions.record_path(project.project, "1-1-a")
    _record_only(project)
    rp.rename(rp.with_name("renamed-by-hand.json"))
    assert operatoractions.drop(project.project, "1-1-a") is True
    assert operatoractions.load(project.project) == {}


# ------------------------------------------------------- derived provenance


def test_resolve_derives_the_commit_from_the_record_history(project):
    """The record rides the very commit it would name, so it cannot store the
    sha — `resolve` derives it from `git log` over the record's own path.

    Ablation: make `_derive_commit` return "" unconditionally and this fails."""
    _park(project)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "auto: 1-1-a (awaiting operator)")
    (story,) = operatoractions.resolve(project.project, project)
    assert story.commit == git(project.project, "rev-parse", "HEAD")


def test_an_uncommitted_record_derives_an_empty_commit(project):
    """A record not yet in any commit (NO_VCS park, or read between write and
    commit) has no provenance to show — and that must not gate anything:
    `commit` is display-only, never a predicate input."""
    _park(project)
    (story,) = operatoractions.resolve(project.project, project)
    assert story.commit == ""
    assert story.confirmable, story.drift()


def test_a_legacy_entry_keeps_its_stored_commit(project):
    """A legacy entry stored its sha at park time (it was written AFTER the
    commit, outside it); derivation is only for records that could not."""
    install_bmad_config(project)
    write_sprint(project, {"1-1-a": "awaiting-operator"})
    _legacy_entry(project, "1-1-a")
    (story,) = operatoractions.resolve(project.project, project)
    assert story.commit == "abcdef1234567890"


# ------------------------------------------------- reading actions back


@pytest.mark.parametrize(
    ("stored", "expected", "why"),
    [
        (["a", "b"], ("a", "b"), "the ordinary list"),
        (["a", "a", "b"], ("a", "b"), "deduped, order preserved"),
        (["  a  ", ""], ("a",), "stripped, blanks dropped"),
        ("a string", (), "a bare string is not a list of actions"),
        ([{"action": "x", "check": "dig"}], (), "the deliberate v2 shape reads as nothing in v1"),
        ([None], (), "None is not an action"),
        (None, (), "absent"),
    ],
)
def test_actions_of_shares_the_frontmatter_reading(stored, expected, why):
    """The record is written from a spec and compared back against one, so a
    shape that collapses to () on the spec side must collapse to () here too."""
    assert operatoractions.actions_of({"actions": stored}) == expected, why


# --------------------------------------------- joining the records to truth


def test_resolve_joins_the_record_to_spec_and_board(project):
    _park(project)
    (story,) = operatoractions.resolve(project.project, project)
    assert story.story_key == "1-1-a"
    assert story.actions == tuple(ACTIONS)
    assert story.spec_status == "awaiting-operator"
    assert story.board_status == "awaiting-operator"
    assert story.run_id == "run-1"
    assert story.confirmable and story.drift() is None


def test_resolve_prefers_the_specs_actions_over_the_recorded_copy(project):
    """The spec is the committed truth. A spec edited after the park must show the
    human what they owe NOW, not what the run recorded then."""
    spec = _park(project)
    write_spec(spec, "awaiting-operator", "base", operator_actions=["the revised action"])
    (story,) = operatoractions.resolve(project.project, project)
    assert story.actions == ("the revised action",)


def test_resolve_falls_back_to_the_recorded_actions_when_the_spec_has_none(project):
    """A spec whose declaration was lost still shows what the run recorded — the
    fallback is what keeps a human from being told they owe nothing."""
    spec = _park(project)
    write_spec(spec, "awaiting-operator", "base")  # no operator_actions
    (story,) = operatoractions.resolve(project.project, project)
    assert story.actions == tuple(ACTIONS)


def test_resolve_is_sorted_by_story_key(project):
    _park(project, "1-2-b")
    _park(project, "1-1-a")
    assert [s.story_key for s in operatoractions.resolve(project.project, project)] == [
        "1-1-a",
        "1-2-b",
    ]


@pytest.mark.parametrize(
    ("spec_status", "board", "expect_drift"),
    [
        ("awaiting-operator", "awaiting-operator", None),
        ("done", "awaiting-operator", "its spec now says status: done"),
        ("awaiting-operator", "done", "the board now says done"),
        ("in-progress", "in-progress", "its spec now says status: in-progress"),
    ],
)
def test_drift_names_the_side_that_disagrees(project, spec_status, board, expect_drift):
    """ "The record says parked, the board says done" is a different message and a
    different remedy from "no such story" — so both sides are reported, and the
    spec is reported first because a spec that moved on explains everything."""
    _park(project, status=spec_status, board=board)
    (story,) = operatoractions.resolve(project.project, project)
    assert story.drift() == expect_drift
    assert story.confirmable is (expect_drift is None)


def test_drift_renders_a_blank_spec_status_readably(project):
    """A YAML-null `status:` reads back as "" (`status_of`, #358), so this line would
    otherwise trail off after "status: " and tell a human nothing. The remedy is the
    same as for any moved-on spec — but only if they can see what it actually says."""
    spec = _park(project)
    spec.write_text(
        spec.read_text().replace("status: 'awaiting-operator'", "status:"), encoding="utf-8"
    )
    (story,) = operatoractions.resolve(project.project, project)
    assert story.spec_status == ""
    assert story.drift() == "its spec now says status: (blank)"
    assert story.confirmable is False


def test_drift_reports_a_missing_spec_before_a_missing_status(project):
    spec = _park(project)
    spec.unlink()
    (story,) = operatoractions.resolve(project.project, project)
    assert story.spec_status is None
    assert "missing or unreadable" in (story.drift() or "")
    assert not story.confirmable


def test_drift_reports_an_entry_with_no_spec_path(project):
    install_bmad_config(project)
    write_sprint(project, {"1-1-a": "awaiting-operator"})
    operatoractions.record_park(
        project.project,
        "1-1-a",
        actions=ACTIONS,
        spec_file="",
        run_id="r",
        parked_at="2026-07-28",
    )
    (story,) = operatoractions.resolve(project.project, project)
    assert story.spec_path is None
    assert story.drift() == "no spec file could be located for it"


def test_a_mangled_record_resolves_as_drift_not_absence(project):
    """The other half of the load test above: the empty entry a mangled record
    degrades to must come out of `resolve` as a reportable drift, so `validate`
    and `confirm --list` name the file a human has to look at."""
    install_bmad_config(project)
    write_sprint(project, {"1-1-a": "awaiting-operator"})
    rp = operatoractions.record_path(project.project, "1-1-a")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("{ not json")
    (story,) = operatoractions.resolve(project.project, project)
    assert story.drift() == "no spec file could be located for it"
    assert not story.confirmable


def test_drift_reports_a_story_absent_from_the_board(project):
    _park(project)
    write_sprint(project, {"epic-1": "in-progress"})  # story key gone
    (story,) = operatoractions.resolve(project.project, project)
    assert story.board_status is None
    assert story.drift() == "it is not on the sprint board"


def test_a_park_declaring_nothing_readable_is_not_confirmable(project):
    """A park is DEFINED by owing at least one action. An entry with none is not
    something a human can acknowledge, so it must not read as confirmable."""
    _park(project, actions=[])
    (story,) = operatoractions.resolve(project.project, project)
    assert story.actions == ()
    assert story.drift() == "it declares no readable actions"
    assert not story.confirmable


def test_resolve_degrades_on_an_unreadable_board(project):
    """`resolve` backs a listing and a validate warning; neither may be the thing
    that crashes on a board someone broke."""
    _park(project)
    project.sprint_status.write_text("{{ not yaml")
    (story,) = operatoractions.resolve(project.project, project)
    assert story.board_status is None
    assert not story.confirmable


def test_resolve_tolerates_a_non_dict_legacy_entry(project):
    """A hand-mangled legacy store must degrade, not raise, on the way to the
    warning that reports it."""
    install_bmad_config(project)
    write_sprint(project, {"1-1-a": "awaiting-operator"})
    store = operatoractions.legacy_store_path(project.project)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"1-1-a": "not a record"}))
    (story,) = operatoractions.resolve(project.project, project)
    assert story.actions == () and story.spec_path is None


def test_verify_exports_the_same_token(project):
    """One spelling of the token across the park and its exit."""
    assert operatoractions.AWAITING_OPERATOR == verify.AWAITING_OPERATOR == "awaiting-operator"


# ------------------------------------------- an interrupted confirmation


def _interrupt(project, key="1-1-a", *, board="awaiting-operator"):
    """Leave on disk exactly what `confirm` leaves when it dies between its spec
    writes and its board write: the audit section appended, the spec at `done`,
    the board where the park left it, and the record still there."""
    spec = _park(project, key, status="done", board=board)
    devcontract.append_operator_confirmation(
        spec, ACTIONS, date="2026-07-28", confine_root=project.project
    )
    return spec


def test_resolve_reads_back_whether_the_confirmation_section_is_there(project):
    """The section on disk IS the acknowledgment, so `resolve` has to carry it —
    a spec and board reading alone cannot tell an interrupted confirmation from a
    story someone finished by hand."""
    _park(project)
    (fresh,) = operatoractions.resolve(project.project, project)
    assert fresh.confirmation_recorded is False

    _interrupt(project)
    (story,) = operatoractions.resolve(project.project, project)
    assert story.confirmation_recorded is True


def test_an_interrupted_confirmation_is_resumable(project):
    """Spec signed off and at `done`, board still parked, record still there:
    the state `confirm` leaves when `sprintstatus.advance` raises or returns
    unchanged. `drift()` calls it stale, so without this predicate a re-run
    refuses the very state a re-run exists to clear."""
    _interrupt(project)
    (story,) = operatoractions.resolve(project.project, project)
    assert story.resumable is True
    assert story.confirmable is False
    assert story.drift() == "its spec now says status: done"


def test_a_hand_fixed_board_stays_resumable(project):
    """⚠️ The board arm accepts `done`, not just `awaiting-operator`. The
    interruption message tells the human to fix the board by hand; if they do and
    the predicate then excludes the entry, it is stranded — nothing drops it and
    `validate` warns about it forever.

    Ablation: narrow the arm to `== AWAITING_OPERATOR` and this fails."""
    _interrupt(project, board="done")
    (story,) = operatoractions.resolve(project.project, project)
    assert story.board_status == "done"
    assert story.resumable is True


@pytest.mark.parametrize(
    ("spec_status", "board", "confirmed", "why"),
    [
        ("awaiting-operator", "awaiting-operator", True, "still parked — a fresh confirm"),
        ("done", "done", False, "no section: finished by hand or re-driven, not signed off"),
        ("done", "in-progress", True, "the board moved somewhere confirm never sends it"),
        ("in-progress", "awaiting-operator", True, "the spec was re-driven past the park"),
    ],
)
def test_resumable_requires_all_three_readings(project, spec_status, board, confirmed, why):
    """Each arm is load-bearing. Resuming writes no audit section, so a spec at
    `done` WITHOUT one would have the story declared confirmed on a sign-off that
    never happened."""
    spec = _park(project, status=spec_status, board=board)
    if confirmed:
        devcontract.append_operator_confirmation(
            spec, ACTIONS, date="2026-07-28", confine_root=project.project
        )
    (story,) = operatoractions.resolve(project.project, project)
    assert story.resumable is False, why


def test_a_fenced_confirmation_heading_does_not_make_a_story_resumable(project):
    """#52 symmetry, and the stakes are higher here than on the auto-run path: a
    frozen intent quoting an example ``## Operator Confirmation`` block must not
    let `confirm` conclude a human signed off and skip to advancing the board."""
    spec = _park(project, status="done")
    spec.write_text(
        spec.read_text() + f"\n```markdown\n{devcontract.OPERATOR_CONFIRM_HEADING}\n```\n"
    )
    (story,) = operatoractions.resolve(project.project, project)
    assert story.confirmation_recorded is False
    assert story.resumable is False


# ------------------------------------ committed drift, split from the record's own


def test_committed_drift_ignores_an_unreadable_action_list(project):
    """The split exists so a caller reporting the malformed list can ALSO report a
    co-occurring board disagreement. `drift()` collapses both into whichever it
    finds first; `committed_drift()` answers only about spec and board."""
    _park(project, actions=[], board="done")
    (story,) = operatoractions.resolve(project.project, project)
    assert story.committed_drift() == "the board now says done"
    assert story.drift() == "the board now says done"


def test_committed_drift_is_none_for_a_park_owing_nothing_readable(project):
    """The one input where the two answers differ: the committed state still
    describes a park, and only the record's own list is unreadable."""
    _park(project, actions=[])
    (story,) = operatoractions.resolve(project.project, project)
    assert story.committed_drift() is None
    assert story.drift() == "it declares no readable actions"
    assert not story.confirmable


@pytest.mark.parametrize(
    ("spec_status", "board"),
    [
        ("awaiting-operator", "awaiting-operator"),
        ("done", "awaiting-operator"),
        ("awaiting-operator", "done"),
        ("in-progress", "in-progress"),
    ],
)
def test_drift_still_answers_exactly_as_it_did_for_a_readable_list(project, spec_status, board):
    """Characterization: splitting the predicate changed no answer `drift()` gave.
    Every committed-side cause outranks the empty-actions arm, which is where it
    already sat."""
    _park(project, status=spec_status, board=board)
    (story,) = operatoractions.resolve(project.project, project)
    assert story.drift() == story.committed_drift()


# ------------------------------ the confined writes under `.bmad-loop/` (#593, #597)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_record_write_refuses_a_symlinked_bmad_loop(project, tmp_path):
    """The escape #593 names, at this site. `follow_symlinks=False` refused a link
    planted at the record's own name (the row above pins that); it never refused
    one at `.bmad-loop/`, and `record_park`'s own
    `mkdir(parents=True, exist_ok=True)` ACCEPTS a symlink-to-a-directory, so the
    planted parent survives the setup step and both the temp and the published
    record land wherever the link points.

    The stakes are this store's, not a generic writer's: the record is written
    inside the story's commit window by a driven session that has been writing
    under `.bmad-loop/` all run long, so the escalation the no-follow was added to
    close costs a directory swap instead of a file swap — and the write it
    redirects is one `finalize_commit`'s `git add -A` is about to look for.

    The assertions after the raise are the load-bearing half: refusing loudly is
    worth nothing if the write already landed outside the project, and
    `pytest.raises` alone passes just as happily on a raise that came AFTER the
    escape. They are stated as "no record CONTENT escaped" rather than
    "`outside/` is empty", because an empty `operator/` DOES land there — the
    `mkdir` runs before the writer and is precisely the step #593 says a planted
    parent survives. The confinement is what stops the bytes, and the bytes are
    what a record is; an empty directory names no obligation and `load` reads it
    as nothing.

    Ablation: revert `record_park` to
    `atomic_write_text(path, ..., follow_symlinks=False)` and this fails
    `DID NOT RAISE`, with `1-1-a.json` sitting in `outside/operator/`."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (project.project / ".bmad-loop").symlink_to(outside, target_is_directory=True)

    with pytest.raises(platform_util.UnconfinedWriteError):
        _record_only(project)

    assert [p.name for p in outside.iterdir()] == ["operator"]  # the mkdir, nothing more
    assert list(outside.rglob("*.json")) == []  # no record escaped the project
    assert not list(outside.rglob("*.tmp"))  # nor a temp it was staged through
    assert operatoractions.load(project.project) == {}


def test_the_record_write_lands_on_a_clean_tree(project):
    """The positive control for the refusal above. Without it that test passes for
    a `record_park` wired to refuse everything, which is every reason a record
    could be absent from `outside/` — and a park that cannot record is a story a
    human can never be told they owe."""
    _record_only(project)

    assert operatoractions.record_path(project.project, "1-1-a").is_file()
    assert operatoractions.load(project.project)["1-1-a"]["actions"] == ACTIONS


def test_the_record_write_refuses_a_readonly_record(project):
    """#597 at this site: `record_park` is the THIRD writer of this same file —
    `Engine._restore_park_record` and `confirm`'s prune are the others — so write
    semantics belong to the file, not to whichever path reached it last. An
    operator who marks a park record read-only gets back the `PermissionError` a
    bare `Path.write_text` raised before #379, rather than having the mode routed
    around by a replace that only needs the DIRECTORY writable.

    Re-parking the SAME key, deliberately: that is the one path that overwrites an
    existing record (`test_re_parking_replaces_rather_than_accumulates`), so it is
    the only one where a target mode exists to be honoured.

    The bytes assertion is the load-bearing one — `pytest.raises` alone would pass
    for a writer that clobbered the record and then raised on something else.

    No skipif: 0444 sets READONLY on win32 too, and the win32 degrade arm carries
    the same probe. chmod is on the per-test copytree copy the `project` fixture
    makes, never the session template, and it is restored in a `finally` because
    Windows rmtree refuses a READONLY file at cleanup.

    Ablation: drop `require_writable_target=True` from `record_park` and this
    fails `DID NOT RAISE`, with the record rewritten to `run-2` and still 0444."""
    _record_only(project)
    rp = operatoractions.record_path(project.project, "1-1-a")
    before = rp.read_bytes()
    rp.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            operatoractions.record_park(
                project.project,
                "1-1-a",
                actions=["a second park nobody may land"],
                spec_file="spec.md",
                run_id="run-2",
                parked_at="2026-07-29",
            )
    finally:
        rp.chmod(0o644)

    assert rp.read_bytes() == before  # the re-park never landed
    # and it left no temp beside it: the probe refuses BEFORE the temp is created,
    # so a residue here would mean the write body ran and was undone after the fact
    assert not list(operatoractions.records_dir(project.project).glob("*.tmp"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_legacy_prune_refuses_a_symlinked_bmad_loop(project, tmp_path):
    """The `_drop_legacy` half of the row above (#593): same escape, a different
    write, and its own ablation. Reachable for a different reason too — `drop` is
    what `bmad-loop confirm` calls, out of band and on a machine whose repo a
    driven session has been writing all run long.

    The arrangement is forced by the writer's own precondition. `_drop_legacy`
    reaches the rewrite only when `_load_legacy` FOUND the key, so the store has
    to stay readable THROUGH the planted link — seed it, move its bytes into
    `outside/`, and let the link resolve back to them. Seeding into a real
    `.bmad-loop/` and then replacing the directory would strand the store, the
    read would come back empty, and the test would pass having never reached a
    writer at all. Hence the explicit precondition assertion: two entries visible
    through the link, so the prune takes the rewrite arm (with ONE entry it takes
    the `else` and unlinks the store outright).

    `outside/` is not asserted empty here — the store legitimately lives there —
    so the load-bearing assertions are that its BYTES are untouched and that no
    temp joined it: either would be the redirected write landing.

    Ablation: revert `_drop_legacy` to
    `atomic_write_text(path, ..., follow_symlinks=False)` and this fails
    `DID NOT RAISE`, with `operator-actions.json` in `outside/` rewritten to one
    entry through the link."""
    outside = tmp_path / "outside"
    outside.mkdir()
    _legacy_entry(project, "1-1-a")
    _legacy_entry(project, "2-2-b")
    store = operatoractions.legacy_store_path(project.project)
    kept = store.read_bytes()
    (outside / store.name).write_bytes(kept)
    store.unlink()
    store.parent.rmdir()
    (project.project / ".bmad-loop").symlink_to(outside, target_is_directory=True)
    # precondition: the store is READ through the link, so the prune gets past its
    # `story_key not in data` early return and actually reaches the writer
    assert sorted(operatoractions.load(project.project)) == ["1-1-a", "2-2-b"]

    with pytest.raises(platform_util.UnconfinedWriteError):
        operatoractions.drop(project.project, "1-1-a")

    assert [p.name for p in outside.iterdir()] == [store.name]  # no temp escaped either
    assert (outside / store.name).read_bytes() == kept  # not written through
    assert sorted(operatoractions.load(project.project)) == ["1-1-a", "2-2-b"]


def test_the_legacy_prune_lands_on_a_clean_tree(project):
    """The positive control for the refusal above, and it grades the same rewrite
    arm: TWO entries, so the prune rewrites rather than unlinking, which is the
    only arm that reaches the confined writer at all."""
    _legacy_entry(project, "1-1-a")
    _legacy_entry(project, "2-2-b")

    assert operatoractions.drop(project.project, "1-1-a") is True

    assert operatoractions.legacy_store_path(project.project).is_file()
    assert sorted(operatoractions.load(project.project)) == ["2-2-b"]


def test_the_legacy_prune_refuses_a_readonly_store(project):
    """#597 at this site. The legacy store is the one file here a HUMAN may have
    curated — it is machine-local, predates #356, and nothing writes it anymore —
    so a mode set on it is a deliberate instruction, not stale state, and the
    prune reports it instead of routing around it.

    TWO entries again for the rewrite arm; with one, `_drop_legacy` takes the
    `else` and unlinks the store, and `unlink` needs only the DIRECTORY writable,
    so a read-only store would be removed and this test would grade nothing.

    Ablation: drop `require_writable_target=True` from `_drop_legacy` and this
    fails `DID NOT RAISE`, with the store rewritten to `2-2-b` alone and the
    confirmed-nothing entry gone."""
    _legacy_entry(project, "1-1-a")
    _legacy_entry(project, "2-2-b")
    store = operatoractions.legacy_store_path(project.project)
    before = store.read_bytes()
    store.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            operatoractions.drop(project.project, "1-1-a")
    finally:
        store.chmod(0o644)

    assert store.read_bytes() == before  # the prune never landed
    assert sorted(operatoractions.load(project.project)) == ["1-1-a", "2-2-b"]
    assert [p.name for p in store.parent.iterdir()] == [store.name]  # no temp residue
