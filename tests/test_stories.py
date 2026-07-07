"""Tests for the stories.yaml contract layer (parse/validate + linear schedule)."""

import re
from pathlib import Path

import pytest

from bmad_loop import stories

FIXTURES = Path(__file__).parent / "fixtures"


def write_stories(spec_folder: Path, text: str) -> Path:
    spec_folder.mkdir(parents=True, exist_ok=True)
    path = spec_folder / stories.STORIES_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


def write_story_spec(spec_folder: Path, filename: str, *, status: str = "") -> Path:
    d = spec_folder / stories.STORIES_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    fm = (
        f"---\ntitle: x\nstatus: {status}\n---\n\nbody\n"
        if status
        else "---\ntitle: x\n---\n\nbody\n"
    )
    path = d / filename
    path.write_text(fm, encoding="utf-8")
    return path


# --------------------------------------------------------------- fixture / parse


def test_load_dogfooded_fixture():
    s = stories.load_stories(FIXTURES)
    assert [e.id for e in s.entries] == ["1", "2", "3", "4", "5"]
    by_id = {e.id: e for e in s.entries}
    # independent checkpoint bools, verbatim invoke_dev_with, no depends_on field
    assert by_id["1"].spec_checkpoint is True and by_id["1"].done_checkpoint is False
    assert by_id["2"].spec_checkpoint is True
    assert "deliberately-minimal-change" in by_id["2"].invoke_dev_with
    assert by_id["3"].spec_checkpoint is False and by_id["3"].invoke_dev_with == ""
    assert by_id["5"].done_checkpoint is True and by_id["5"].spec_checkpoint is False
    assert not hasattr(by_id["1"], "depends_on")


def test_load_missing_file_raises_pinned_message(tmp_path):
    with pytest.raises(stories.StoriesError, match=re.escape("no stories.yaml found")):
        stories.load_stories(tmp_path)


def test_id_unquoted_int_normalized(tmp_path):
    # An LLM-authored file may emit `id: 1` unquoted (PyYAML -> int); we str()-normalize.
    write_stories(tmp_path, "- id: 1\n  title: t\n  description: d\n")
    s = stories.load_stories(tmp_path)
    assert s.entries[0].id == "1"


def test_id_unquoted_composite_is_string(tmp_path):
    # `3-2` is not a YAML number, so it parses as the string "3-2" — a valid id.
    write_stories(tmp_path, "- id: 3-2\n  title: t\n  description: d\n")
    s = stories.load_stories(tmp_path)
    assert s.entries[0].id == "3-2"


def test_id_float_rejected(tmp_path):
    # `id: 3.5` -> float -> str "3.5"; the `.` fails the charset (fail loud).
    write_stories(tmp_path, "- id: 3.5\n  title: t\n  description: d\n")
    with pytest.raises(stories.StoriesError, match="invalid id"):
        stories.load_stories(tmp_path)


def test_id_bad_charset_rejected(tmp_path):
    write_stories(tmp_path, '- id: "a_b"\n  title: t\n  description: d\n')
    with pytest.raises(stories.StoriesError, match="invalid id"):
        stories.load_stories(tmp_path)


def test_id_leading_dash_rejected(tmp_path):
    write_stories(tmp_path, '- id: "-1"\n  title: t\n  description: d\n')
    with pytest.raises(stories.StoriesError, match="invalid id"):
        stories.load_stories(tmp_path)


def test_duplicate_ids_rejected(tmp_path):
    # int 1 and str "1" both normalize to "1" — a duplicate.
    write_stories(
        tmp_path,
        '- id: 1\n  title: a\n  description: d\n- id: "1"\n  title: b\n  description: d\n',
    )
    with pytest.raises(stories.StoriesError, match="duplicate id"):
        stories.load_stories(tmp_path)


def test_prefix_free_violation_rejected(tmp_path):
    # ids "3" and "3-2": the `3-*.md` glob for story 3 would also match `3-2-*.md`.
    write_stories(
        tmp_path,
        '- id: "3"\n  title: a\n  description: d\n' '- id: "3-2"\n  title: b\n  description: d\n',
    )
    with pytest.raises(stories.StoriesError, match="not prefix-free"):
        stories.load_stories(tmp_path)


def test_prefix_free_allows_numeric_neighbor(tmp_path):
    # "3" and "31" do NOT collide: `3-*.md` never matches `31-*.md`.
    write_stories(
        tmp_path,
        '- id: "3"\n  title: a\n  description: d\n' '- id: "31"\n  title: b\n  description: d\n',
    )
    s = stories.load_stories(tmp_path)
    assert [e.id for e in s.entries] == ["3", "31"]


def test_case_only_duplicate_ids_rejected(tmp_path):
    # "Auth" and "auth" are distinct strings but identical under casefold — on a
    # case-insensitive filesystem the `Auth-*.md` glob also matches `auth-*.md`.
    write_stories(
        tmp_path,
        '- id: "Auth"\n  title: a\n  description: d\n'
        '- id: "auth"\n  title: b\n  description: d\n',
    )
    with pytest.raises(stories.StoriesError, match="differ only by case"):
        stories.load_stories(tmp_path)


def test_case_insensitive_prefix_collision_rejected(tmp_path):
    # "Auth" and "auth-2": on a case-insensitive filesystem the `Auth-*.md` glob
    # for story "Auth" also matches `auth-2-*.md`, so this is a prefix collision
    # even though the two ids never collide byte-for-byte.
    write_stories(
        tmp_path,
        '- id: "Auth"\n  title: a\n  description: d\n'
        '- id: "auth-2"\n  title: b\n  description: d\n',
    )
    with pytest.raises(stories.StoriesError, match="not prefix-free"):
        stories.load_stories(tmp_path)


def test_status_key_forbidden(tmp_path):
    write_stories(tmp_path, '- id: "1"\n  title: t\n  description: d\n  status: draft\n')
    with pytest.raises(stories.StoriesError, match="forbidden 'status' key") as exc:
        stories.load_stories(tmp_path)
    # The user-facing vocabulary is "stories.yaml", not the plugin-layer word
    # "manifest" (which names plugin.toml elsewhere in the codebase).
    assert "manifest" not in str(exc.value)
    assert "stories.yaml" in str(exc.value)


def test_missing_required_field_rejected(tmp_path):
    write_stories(tmp_path, '- id: "1"\n  description: d\n')  # no title
    with pytest.raises(stories.StoriesError, match="missing required field 'title'"):
        stories.load_stories(tmp_path)


def test_missing_id_rejected(tmp_path):
    write_stories(tmp_path, "- title: t\n  description: d\n")
    with pytest.raises(stories.StoriesError, match="missing required field 'id'"):
        stories.load_stories(tmp_path)


def test_empty_title_rejected(tmp_path):
    write_stories(tmp_path, '- id: "1"\n  title: "  "\n  description: d\n')
    with pytest.raises(stories.StoriesError, match="field 'title' is empty"):
        stories.load_stories(tmp_path)


def test_checkpoint_bool_defaults_false(tmp_path):
    write_stories(tmp_path, '- id: "1"\n  title: t\n  description: d\n')
    e = stories.load_stories(tmp_path).entries[0]
    assert e.spec_checkpoint is False and e.done_checkpoint is False


def test_both_checkpoints_settable(tmp_path):
    write_stories(
        tmp_path,
        '- id: "1"\n  title: t\n  description: d\n'
        "  spec_checkpoint: true\n  done_checkpoint: true\n",
    )
    e = stories.load_stories(tmp_path).entries[0]
    assert e.spec_checkpoint is True and e.done_checkpoint is True


def test_checkpoint_non_bool_rejected(tmp_path):
    # `spec_checkpoint: 1` is an int, not a bool — strict, no truthy coercion.
    write_stories(tmp_path, '- id: "1"\n  title: t\n  description: d\n  spec_checkpoint: 1\n')
    with pytest.raises(stories.StoriesError, match="must be a boolean"):
        stories.load_stories(tmp_path)


def test_invoke_dev_with_defaults_empty(tmp_path):
    write_stories(tmp_path, '- id: "1"\n  title: t\n  description: d\n')
    assert stories.load_stories(tmp_path).entries[0].invoke_dev_with == ""


def test_invoke_dev_with_non_string_rejected(tmp_path):
    write_stories(tmp_path, '- id: "1"\n  title: t\n  description: d\n  invoke_dev_with: [a, b]\n')
    with pytest.raises(stories.StoriesError, match="must be a string"):
        stories.load_stories(tmp_path)


def test_top_level_must_be_list(tmp_path):
    write_stories(tmp_path, "development_status:\n  a: b\n")
    with pytest.raises(stories.StoriesError, match="top-level list"):
        stories.load_stories(tmp_path)


def test_empty_list_rejected(tmp_path):
    write_stories(tmp_path, "[]\n")
    with pytest.raises(stories.StoriesError, match="no story entries"):
        stories.load_stories(tmp_path)


def test_empty_file_rejected(tmp_path):
    write_stories(tmp_path, "")
    with pytest.raises(stories.StoriesError, match="no story entries"):
        stories.load_stories(tmp_path)


def test_entry_not_mapping_rejected(tmp_path):
    write_stories(tmp_path, "- just a string\n")
    with pytest.raises(stories.StoriesError, match="is not a mapping"):
        stories.load_stories(tmp_path)


def test_invalid_yaml_rejected(tmp_path):
    write_stories(tmp_path, "- id: '1'\n  title: [unterminated\n")
    with pytest.raises(stories.StoriesError, match="not valid YAML"):
        stories.load_stories(tmp_path)


# --------------------------------------------------------------- find_entry


def test_find_entry():
    s = stories.load_stories(FIXTURES)
    assert stories.find_entry(s, "3").title.startswith("Pin write-back")


def test_find_entry_unknown_raises_pinned_message():
    s = stories.load_stories(FIXTURES)
    with pytest.raises(stories.StoriesError, match=re.escape("story id not found in stories.yaml")):
        stories.find_entry(s, "99")


# --------------------------------------------------------------- schedule (linear)


def _stories(*ids: str) -> stories.Stories:
    entries = tuple(stories.StoryEntry(id=i, title=f"t{i}", description="d") for i in ids)
    return stories.Stories(path=Path("stories.yaml"), entries=entries)


def _present(status: str) -> stories.StoryState:
    return stories.StoryState(kind=stories.KIND_PRESENT, status=status, path=Path("x.md"))


def test_schedule_first_pending_is_next():
    s = _stories("1", "2", "3")
    sched = stories.schedule(s, {})  # all missing -> pending
    assert sched.outcome == stories.SCHEDULE_NEXT and sched.entry.id == "1"


def test_schedule_skips_done():
    s = _stories("1", "2", "3")
    states = {"1": _present("done"), "2": stories.StoryState(kind=stories.KIND_PENDING)}
    sched = stories.schedule(s, states)
    assert sched.entry.id == "2"


@pytest.mark.parametrize("status", sorted(stories.RESUMABLE_STATUSES))
def test_schedule_resumes_non_terminal(status):
    # A died-mid-flight story (draft/ready-for-dev/in-progress/in-review) is
    # actionable — re-dispatch resumes it.
    s = _stories("1", "2")
    sched = stories.schedule(s, {"1": _present(status)})
    assert sched.outcome == stories.SCHEDULE_NEXT and sched.entry.id == "1"


def test_schedule_all_done_is_complete():
    s = _stories("1", "2")
    sched = stories.schedule(s, {"1": _present("done"), "2": _present("done")})
    assert sched.is_complete and sched.entry is None


def test_schedule_blocked_stops_scan_before_later_pending():
    # A blocked story earlier in the list wedges the run — the linear contract
    # forbids leapfrogging it to the later pending story.
    s = _stories("1", "2", "3")
    states = {"1": _present("done"), "2": _present("blocked")}  # 3 is pending
    sched = stories.schedule(s, states)
    assert sched.is_wedged and sched.entry.id == "2"


def test_schedule_sentinel_wedges():
    s = _stories("1", "2")
    sentinel = stories.StoryState(
        kind=stories.KIND_SENTINEL, sentinel_kind=stories.SENTINEL_UNRESOLVED, path=Path("1-u.md")
    )
    sched = stories.schedule(s, {"1": sentinel})
    assert sched.is_wedged and sched.entry.id == "1"


def test_schedule_ambiguous_wedges():
    s = _stories("1")
    amb = stories.StoryState(kind=stories.KIND_AMBIGUOUS, paths=(Path("1-a.md"), Path("1-b.md")))
    sched = stories.schedule(s, {"1": amb})
    assert sched.is_wedged and sched.entry.id == "1"


def test_schedule_unknown_status_wedges():
    # A frontmatter status the skill would itself HALT on as unrecognized.
    s = _stories("1")
    sched = stories.schedule(s, {"1": _present("weird-custom")})
    assert sched.is_wedged


def test_schedule_missing_state_is_pending():
    s = _stories("1", "2")
    sched = stories.schedule(s, {"1": _present("done")})  # "2" absent from states
    assert sched.outcome == stories.SCHEDULE_NEXT and sched.entry.id == "2"


def test_schedule_selector_targets_one_story():
    s = _stories("1", "2", "3")
    sched = stories.schedule(s, {}, selector="2")
    assert sched.entry.id == "2"


def test_schedule_selector_done_is_complete():
    s = _stories("1", "2")
    sched = stories.schedule(s, {"2": _present("done")}, selector="2")
    assert sched.is_complete


def test_schedule_selector_unknown_raises():
    s = _stories("1")
    with pytest.raises(stories.StoriesError, match=re.escape("story id not found in stories.yaml")):
        stories.schedule(s, {}, selector="99")


# --------------------------------------------------------------- resolve_story_spec


def test_resolve_pending_no_dir(tmp_path):
    assert stories.resolve_story_spec(tmp_path, "1").kind == stories.KIND_PENDING


def test_resolve_present_reads_status(tmp_path):
    write_story_spec(tmp_path, "1-user-auth.md", status="in-review")
    st = stories.resolve_story_spec(tmp_path, "1")
    assert st.kind == stories.KIND_PRESENT and st.status == "in-review"
    assert st.path.name == "1-user-auth.md"


def test_resolve_ambiguous(tmp_path):
    write_story_spec(tmp_path, "1-one.md")
    write_story_spec(tmp_path, "1-two.md")
    st = stories.resolve_story_spec(tmp_path, "1")
    assert st.kind == stories.KIND_AMBIGUOUS and len(st.paths) == 2


def test_resolve_sentinel_unresolved(tmp_path):
    write_story_spec(tmp_path, "1-unresolved.md", status="blocked")
    st = stories.resolve_story_spec(tmp_path, "1")
    assert st.kind == stories.KIND_SENTINEL and st.sentinel_kind == stories.SENTINEL_UNRESOLVED


def test_resolve_sentinel_ambiguous(tmp_path):
    write_story_spec(tmp_path, "1-ambiguous.md", status="blocked")
    st = stories.resolve_story_spec(tmp_path, "1")
    assert st.kind == stories.KIND_SENTINEL and st.sentinel_kind == stories.SENTINEL_AMBIGUOUS


def test_resolve_prefix_isolation(tmp_path):
    # id "3" must not resolve a file for id "31" — `3-*.md` doesn't match `31-*.md`.
    write_story_spec(tmp_path, "31-other.md", status="done")
    assert stories.resolve_story_spec(tmp_path, "3").kind == stories.KIND_PENDING


def test_resolve_wrong_case_id_is_pending(tmp_path):
    # Only a differently-cased file exists: resolution must be PENDING on every FS.
    # On case-sensitive Linux the glob never matches; on a case-insensitive FS
    # (Windows CI, macOS) the exact-case filter drops the wrong-case hit — without
    # it this would resolve KIND_PRESENT there, making resolution OS-dependent.
    write_story_spec(tmp_path, "AUTH-x.md", status="done")
    assert stories.resolve_story_spec(tmp_path, "auth").kind == stories.KIND_PENDING


@pytest.mark.parametrize("bad_id", ["1*", "1?", "1[a", "a/b", "..", ".", "3 1"])
def test_resolve_charset_invalid_id_is_pending_not_glob(tmp_path, bad_id):
    # A non-charset-valid id must never reach glob(): "1*" would otherwise glob
    # `1*-*.md` and wrongly match the real `1-x.md`. The ID_RE guard makes every
    # such id a clean PENDING ("no resolvable spec") instead of an injected match.
    write_story_spec(tmp_path, "1-x.md", status="done")
    assert stories.resolve_story_spec(tmp_path, bad_id).kind == stories.KIND_PENDING
