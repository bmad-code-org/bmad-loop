"""Unit tests for the pure helpers in scripts/release.py.

These cover the logic that decides versions, parses/extracts CHANGELOG sections,
inserts link references, groups commits, and short-circuits a publish when the tag
already exists. Anything touching real git/gh is exercised via monkeypatch. The
`cmd_check` / `cmd_prepare` rows drive the release contract over fixture changelogs
with `sync_version` stubbed; `sync_version.check()` itself is covered in
test_sync_version.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import release  # noqa: E402

REPO_URL = "https://github.com/bmad-code-org/bmad-loop"


# --- version parsing / ordering ------------------------------------------- #
@pytest.mark.parametrize(
    "v,core,suffix",
    [
        ("0.5.0", (0, 5, 0), ""),
        ("1.10.2", (1, 10, 2), ""),
        ("0.5.0-rc1", (0, 5, 0), "rc1"),
        ("0.5.0-rc.2", (0, 5, 0), "rc.2"),
    ],
)
def test_parse_version(v, core, suffix):
    assert release.parse_version(v) == (core, suffix)


# `0.5.0.dev3` and `-dev` pre-releases are not SemVer `bmad setup` can order.
@pytest.mark.parametrize(
    "bad", ["", "1.2", "1.2.x", "v1.2.3", "x.y.z", "0.5.0.dev3", "0.5.0-dev3", "01.2.3"]
)
def test_parse_version_rejects_garbage(bad):
    with pytest.raises(ValueError):
        release.parse_version(bad)


@pytest.mark.parametrize(
    "new,old,expected",
    [
        ("0.5.0", "0.4.3", True),
        ("0.4.3", "0.4.3", False),
        ("0.4.2", "0.4.3", False),
        ("1.0.0", "0.9.9", True),
        ("0.5.0", "0.5.0-rc1", True),  # final beats its own pre-release
        ("0.5.0-rc1", "0.5.0", False),
        ("0.5.0-rc2", "0.5.0-rc1", True),
    ],
)
def test_version_gt(new, old, expected):
    assert release.version_gt(new, old) is expected


# --- changelog section extraction ----------------------------------------- #
SAMPLE = """# Changelog

## [0.5.0] — 2026-07-01

### Fixed

- **A thing.** It no longer breaks.

## [0.4.3] — 2026-06-17

### Added

- **Older thing.** Context here.

[0.4.3]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.4.3
[0.4.2]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.4.2
"""


def test_extract_section_returns_body():
    body = release.extract_section(SAMPLE, "0.5.0")
    assert body is not None
    assert "**A thing.**" in body
    assert "Older thing" not in body  # stops at the next heading


def test_extract_section_last_section_stops_before_link_refs():
    body = release.extract_section(SAMPLE, "0.4.3")
    assert body is not None
    assert "Older thing" in body
    assert "releases/tag" not in body  # link-ref block not swallowed


def test_extract_section_missing():
    assert release.extract_section(SAMPLE, "9.9.9") is None
    assert release.has_curated_section(SAMPLE, "9.9.9") is False
    assert release.has_curated_section(SAMPLE, "0.5.0") is True


def test_has_curated_section_false_when_empty():
    text = "## [0.6.0] — 2026-08-01\n\n## [0.5.0] — 2026-07-01\n\n- something\n"
    assert release.has_curated_section(text, "0.6.0") is False


# --- promote-and-reopen fixtures ------------------------------------------- #
# The state a release leaves behind: the notes moved out of `## [Unreleased]` into
# the version section, an empty Unreleased was reopened above it, and the compare
# link tracks the release just cut. `check` must pass on exactly this.
PROMOTED = """# Changelog

## [Unreleased]

## [0.5.0] — 2026-07-01

### Fixed

- **A thing.** It no longer breaks.

[Unreleased]: https://github.com/bmad-code-org/bmad-loop/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.5.0
[0.4.3]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.4.3
"""

# The drift the prepare guard exists to catch: a `## [0.5.0]` section authored
# *beside* a still-populated `## [Unreleased]` rather than promoted from it. Both
# sections are non-empty, so the Unreleased guard is the only precondition that can
# fire — ablate it and `prepare` sails through.
DRIFTED = """# Changelog

## [Unreleased]

### Added

- **Something newer.** Filed after the section below was authored.

## [0.5.0] — 2026-07-01

### Fixed

- **A thing.** It no longer breaks.

[Unreleased]: https://github.com/bmad-code-org/bmad-loop/compare/v0.4.3...HEAD
[0.4.3]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.4.3
"""

# Each derives from PROMOTED by breaking exactly one thing, so exactly one `check`
# arm reports — a shared "rc == 1" would pass for the wrong reason.
NO_UNRELEASED_HEADING = PROMOTED.replace("## [Unreleased]\n\n", "", 1)
NO_UNRELEASED_REF = PROMOTED.replace(
    "[Unreleased]: https://github.com/bmad-code-org/bmad-loop/compare/v0.5.0...HEAD\n", "", 1
)
STALE_UNRELEASED_REF = PROMOTED.replace("/compare/v0.5.0...HEAD", "/compare/v0.4.3...HEAD", 1)

# Renamed but never reopened. `has_curated_section` reports False here for the same
# reason it does for a correctly emptied one, so `prepare` needs the missing/empty
# distinction that `extract_section`'s None makes.
# Renamed and emptied correctly, but the release date never got stamped on. `section_re`
# accepts any suffix after `]`, so every other guard reads this as a clean promotion.
UNDATED_RELEASE_HEADING = PROMOTED.replace("## [0.5.0] — 2026-07-01", "## [0.5.0]", 1)

# A half-finished rename: the fresh empty heading went in, the old populated one was
# never renamed. Guards that `search` for the first Unreleased see only the empty one.
DUPLICATE_UNRELEASED = """# Changelog

## [Unreleased]

## [0.5.0] — 2026-07-01

### Fixed

- **A thing.** It no longer breaks.

## [Unreleased]

### Added

- **Never promoted.** Left behind by the half-finished rename.

[Unreleased]: https://github.com/bmad-code-org/bmad-loop/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.5.0
"""

UNRELEASED_REOPENED_BELOW = """# Changelog

## [0.5.0] — 2026-07-01

### Fixed

- **A thing.** It no longer breaks.

## [Unreleased]

[Unreleased]: https://github.com/bmad-code-org/bmad-loop/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/bmad-code-org/bmad-loop/releases/tag/v0.5.0
"""


# `section_re` already escapes its argument, so the promotion guards reuse it for the
# non-numeric "Unreleased" heading rather than adding a second section regex.
def test_extract_section_reads_an_empty_unreleased_heading():
    assert release.extract_section(PROMOTED, "Unreleased") == ""


def test_has_curated_section_distinguishes_empty_from_populated_unreleased():
    assert release.has_curated_section(PROMOTED, "Unreleased") is False
    assert release.has_curated_section(DRIFTED, "Unreleased") is True


# --- link-ref insertion ---------------------------------------------------- #
def test_ensure_link_ref_inserts_on_top_of_block():
    out = release.ensure_link_ref(SAMPLE, "0.5.0", REPO_URL)
    assert f"[0.5.0]: {REPO_URL}/releases/tag/v0.5.0" in out
    # newest ref sits above the previous newest
    assert out.index("[0.5.0]:") < out.index("[0.4.3]:")


def test_ensure_link_ref_idempotent():
    once = release.ensure_link_ref(SAMPLE, "0.5.0", REPO_URL)
    twice = release.ensure_link_ref(once, "0.5.0", REPO_URL)
    assert once == twice
    assert once.count("[0.5.0]:") == 1


def test_ensure_link_ref_appends_when_no_block():
    text = "# Changelog\n\n## [0.1.0] — 2026-01-01\n\n- first\n"
    out = release.ensure_link_ref(text, "0.1.0", REPO_URL)
    assert out.rstrip().endswith(f"[0.1.0]: {REPO_URL}/releases/tag/v0.1.0")


# --- the `[Unreleased]:` compare link -------------------------------------- #
# Its base has to advance with every bump, or it silently keeps comparing against a
# release two cuts back.
def test_ensure_link_ref_repoints_a_stale_unreleased_compare_link():
    out = release.ensure_link_ref(DRIFTED, "0.5.0", REPO_URL)
    assert f"[Unreleased]: {REPO_URL}/compare/v0.5.0...HEAD" in out


def test_ensure_link_ref_repoints_unreleased_even_when_the_version_ref_exists():
    # STALE_UNRELEASED_REF already carries `[0.5.0]:`. The version-ref insert is
    # therefore a no-op, and a shared early return would skip the rewrite below —
    # which is exactly what a re-run of `prepare` looks like.
    out = release.ensure_link_ref(STALE_UNRELEASED_REF, "0.5.0", REPO_URL)
    assert f"[Unreleased]: {REPO_URL}/compare/v0.5.0...HEAD" in out


def test_ensure_link_ref_inserts_unreleased_above_the_version_refs():
    out = release.ensure_link_ref(SAMPLE, "0.5.0", REPO_URL)
    assert f"[Unreleased]: {REPO_URL}/compare/v0.5.0...HEAD" in out
    assert out.index("[Unreleased]:") < out.index("[0.5.0]:")


def test_ensure_link_ref_repairs_a_malformed_unreleased_ref_in_place():
    # Rewriting is shape-blind on purpose: matching only the well-formed
    # `compare/vX...HEAD` shape would leave a mangled line behind *and* insert a
    # second one.
    text = PROMOTED.replace(f"{REPO_URL}/compare/v0.5.0...HEAD", f"{REPO_URL}/compare/HEAD", 1)
    out = release.ensure_link_ref(text, "0.5.0", REPO_URL)
    assert out.count("[Unreleased]:") == 1


def test_ensure_link_ref_idempotent_with_an_unreleased_ref():
    once = release.ensure_link_ref(DRIFTED, "0.5.0", REPO_URL)
    twice = release.ensure_link_ref(once, "0.5.0", REPO_URL)
    assert once == twice


# --- commit grouping ------------------------------------------------------- #
def test_group_commits_by_type():
    lines = [
        "feat(tui): add panel\x00abc123",
        "fix: stop the crash\x00def456",
        "fix(scm): worktree path\x00aaa111",
        "random unprefixed subject\x00bbb222",
        "",
    ]
    groups = release.group_commits(lines)
    assert groups["feat"] == ["feat(tui): add panel"]
    assert groups["fix"] == ["fix: stop the crash", "fix(scm): worktree path"]
    assert groups["other"] == ["random unprefixed subject"]


# --- commit summary derivation --------------------------------------------- #
def test_commit_summary_strips_bold_and_truncates(monkeypatch, tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## [0.5.0] — 2026-07-01\n\n### Fixed\n\n- **A short lead.** rest\n")
    monkeypatch.setattr(release, "CHANGELOG", cl)
    assert release._commit_summary("0.5.0") == "A short lead. rest"


# --- publish idempotency --------------------------------------------------- #
def test_publish_dry_run_prints_notes(monkeypatch, capsys, tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(SAMPLE)
    monkeypatch.setattr(release, "CHANGELOG", cl)
    monkeypatch.setattr(release.sync_version, "read_canonical", lambda: "0.5.0")
    monkeypatch.setattr(release, "tag_exists", lambda tag: False)
    monkeypatch.setattr(release, "_git_out", lambda *a: "deadbeef" * 5)
    rc = release.cmd_publish(SimpleNamespace(dry_run=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "would create release v0.5.0" in out
    assert "**A thing.**" in out


# --- publish under a concurrent publisher ---------------------------------- #
# `tag_exists` reads the checkout's refs, so a run whose checkout predates another
# runner's tag push reaches `gh release create` and loses. That is the only failure
# the command may swallow; every other one still has to be loud.
HEAD_SHA = "deadbeef" * 5
LOST_RACE = "HTTP 422: Validation Failed\nRelease.tag_name already exists"


def _publish_with_gh(
    monkeypatch,
    tmp_path,
    *,
    returncode=0,
    stderr="",
    seen=None,
    tag_exists_locally=False,
    remote_target=HEAD_SHA,
    version_at_target="0.5.0",
    gh_calls=None,
    release_view_rc=0,
    release_view_stderr="",
    gh_available=True,
    dry_run=False,
    changelog=SAMPLE,
    call_kwargs=None,
):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(changelog)
    monkeypatch.setattr(release, "CHANGELOG", cl)
    monkeypatch.setattr(release.sync_version, "read_canonical", lambda: "0.5.0")
    monkeypatch.setattr(release, "tag_exists", lambda tag: tag_exists_locally)
    monkeypatch.setattr(release, "_git_out", lambda *a: HEAD_SHA)
    monkeypatch.setattr(
        release.shutil, "which", lambda name: f"/usr/bin/{name}" if gh_available else None
    )
    # Argument-dependent, so probing the wrong tag or reading the version at the
    # wrong commit (HEAD, the local tag) falls through to None and goes red.
    monkeypatch.setattr(
        release, "remote_tag_target", lambda tag: remote_target if tag == "v0.5.0" else None
    )
    monkeypatch.setattr(
        release,
        "version_at",
        lambda commit: version_at_target if commit == remote_target else None,
    )

    def fake_run(*a, **kw):
        cmd = a[0] if a else kw.get("args")
        if gh_calls is not None:
            gh_calls.append(cmd)
        if seen is not None:
            seen.update(kw)
        if call_kwargs is not None:
            call_kwargs.append((cmd, kw))
        if cmd[:3] == ["gh", "release", "view"]:
            return SimpleNamespace(
                returncode=release_view_rc, stdout="", stderr=release_view_stderr
            )
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    return release.cmd_publish(SimpleNamespace(dry_run=dry_run))


def test_publish_treats_a_lost_race_as_success(monkeypatch, capsys, tmp_path):
    rc = _publish_with_gh(monkeypatch, tmp_path, returncode=1, stderr=LOST_RACE)
    assert rc == 0
    assert "v0.5.0 was created concurrently — nothing to publish" in capsys.readouterr().out


def test_publish_lost_race_on_an_annotated_tag_checks_the_peeled_commit(
    monkeypatch, capsys, tmp_path
):
    # Drives the real `remote_tag_target` + `peeled_tag_target`: the tag-object SHA
    # differs from HEAD, so only the peeled `^{}` line can make this a success.
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(SAMPLE)
    monkeypatch.setattr(release, "CHANGELOG", cl)
    monkeypatch.setattr(release.sync_version, "read_canonical", lambda: "0.5.0")
    monkeypatch.setattr(release, "tag_exists", lambda tag: False)
    monkeypatch.setattr(release, "_git_out", lambda *a: HEAD_SHA)
    monkeypatch.setattr(release.shutil, "which", lambda name: f"/usr/bin/{name}")
    ls_remote = f"{'ab' * 20}\trefs/tags/v0.5.0\n{HEAD_SHA}\trefs/tags/v0.5.0^{{}}\n"

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "ls-remote"]:
            return SimpleNamespace(returncode=0, stdout=ls_remote, stderr="")
        if cmd[:3] == ["gh", "release", "create"]:
            return SimpleNamespace(returncode=1, stdout="", stderr=LOST_RACE)
        raise AssertionError(f"unexpected subprocess: {cmd}")

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    rc = release.cmd_publish(SimpleNamespace(dry_run=False))
    assert rc == 0
    assert "v0.5.0 was created concurrently — nothing to publish" in capsys.readouterr().out


def test_publish_dies_when_a_lost_race_tagged_another_commit(monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(
            monkeypatch, tmp_path, returncode=1, stderr=LOST_RACE, remote_target="f00d" * 10
        )
    msg = str(exc.value)
    assert msg.startswith("release: ")
    assert "v0.5.0" in msg and "f00d" * 10 in msg and HEAD_SHA in msg
    assert "created concurrently — nothing" not in capsys.readouterr().out


def test_publish_dies_when_a_lost_race_leaves_no_remote_tag(monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(monkeypatch, tmp_path, returncode=1, stderr=LOST_RACE, remote_target=None)
    assert "origin has no v0.5.0 tag" in str(exc.value)
    assert "nothing to publish" not in capsys.readouterr().out


# --- publish verifies a pre-existing tag (DW-352) -------------------------- #
# A local tag is only the cheap pre-check: the commit the tag points to on origin
# must carry the version being published — compared by version, never against HEAD,
# so a non-bump push (or a maintenance tag off `release/*`) stays a no-op.
def test_publish_noop_when_existing_tag_target_carries_the_version(monkeypatch, capsys, tmp_path):
    gh_calls: list = []
    rc = _publish_with_gh(
        monkeypatch,
        tmp_path,
        tag_exists_locally=True,
        remote_target="cafe" * 10,  # not HEAD: version-at-target is the test
        gh_calls=gh_calls,
    )
    assert rc == 0
    assert "v0.5.0 already exists — nothing to publish" in capsys.readouterr().out
    assert gh_calls == [["gh", "release", "view", "v0.5.0"]]


def test_publish_dies_when_existing_tag_target_carries_another_version(monkeypatch, tmp_path):
    gh_calls: list = []
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(
            monkeypatch,
            tmp_path,
            tag_exists_locally=True,
            remote_target="cafe" * 10,
            version_at_target="0.4.9",
            gh_calls=gh_calls,
        )
    msg = str(exc.value)
    assert msg.startswith("release: ")
    assert "v0.5.0" in msg and "cafe" * 10 in msg and "0.4.9" in msg and "not 0.5.0" in msg
    assert gh_calls == []


def test_publish_dies_when_existing_tag_target_is_unreadable(monkeypatch, tmp_path):
    gh_calls: list = []
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(
            monkeypatch,
            tmp_path,
            tag_exists_locally=True,
            version_at_target=None,
            gh_calls=gh_calls,
        )
    assert "cannot read __version__" in str(exc.value)
    assert gh_calls == []


def test_publish_dies_when_the_tag_exists_only_locally(monkeypatch, tmp_path):
    gh_calls: list = []
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(
            monkeypatch, tmp_path, tag_exists_locally=True, remote_target=None, gh_calls=gh_calls
        )
    assert "not on origin" in str(exc.value)
    assert gh_calls == []


# --- publish creates the missing release on a verified tag (DW-411) -------- #
# A verified tag is not a release: a hand-pushed tag, or a `gh release create` that
# cut the tag then failed, must get its release rather than being skipped forever.
RELEASE_NOT_FOUND = "release not found"
EXISTING_TARGET = "cafe" * 10


def test_publish_creates_the_missing_release_on_a_verified_tag(monkeypatch, capsys, tmp_path):
    gh_calls: list = []
    seen: dict[str, object] = {}
    rc = _publish_with_gh(
        monkeypatch,
        tmp_path,
        tag_exists_locally=True,
        remote_target=EXISTING_TARGET,
        release_view_rc=1,
        release_view_stderr=RELEASE_NOT_FOUND,
        gh_calls=gh_calls,
        seen=seen,
    )
    assert rc == 0
    assert gh_calls[0] == ["gh", "release", "view", "v0.5.0"]
    assert gh_calls[1] == [
        "gh",
        "release",
        "create",
        "v0.5.0",
        "--verify-tag",
        "--title",
        "v0.5.0",
        "--notes-file",
        "-",
    ]
    assert len(gh_calls) == 2
    assert seen["cwd"] == release.REPO
    assert "**A thing.**" in str(seen["input"])
    out = capsys.readouterr().out
    assert f"published v0.5.0 for the existing tag at {EXISTING_TARGET[:12]}" in out
    assert "nothing to publish" not in out


def test_publish_bounds_the_body_on_the_existing_tag_path(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(release, "repo_url", lambda: REPO_URL)
    seen: dict[str, object] = {}
    rc = _publish_with_gh(
        monkeypatch,
        tmp_path,
        tag_exists_locally=True,
        remote_target=EXISTING_TARGET,
        release_view_rc=1,
        release_view_stderr=RELEASE_NOT_FOUND,
        seen=seen,
        changelog=SAMPLE.replace("- **A thing.** It no longer breaks.", _long_section(1_000)),
    )
    assert rc == 0
    sent = seen["input"]
    assert isinstance(sent, str) and len(sent) <= release.GITHUB_NOTES_LIMIT
    assert f"{REPO_URL}/blob/v0.5.0/CHANGELOG.md" in sent
    assert "over GitHub's 125,000 limit" in capsys.readouterr().out


def test_publish_dry_run_reports_the_missing_release_without_creating(
    monkeypatch, capsys, tmp_path
):
    gh_calls: list = []
    rc = _publish_with_gh(
        monkeypatch,
        tmp_path,
        tag_exists_locally=True,
        remote_target=EXISTING_TARGET,
        release_view_rc=1,
        release_view_stderr=RELEASE_NOT_FOUND,
        gh_calls=gh_calls,
        dry_run=True,
    )
    assert rc == 0
    assert gh_calls == [["gh", "release", "view", "v0.5.0"]]
    out = capsys.readouterr().out
    assert (
        f"[dry-run] would create release v0.5.0 on the existing tag at {EXISTING_TARGET[:12]}"
        in out
    )
    assert "**A thing.**" in out


def test_publish_dies_when_the_release_view_errors(monkeypatch, capsys, tmp_path):
    gh_calls: list = []
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(
            monkeypatch,
            tmp_path,
            tag_exists_locally=True,
            remote_target=EXISTING_TARGET,
            release_view_rc=1,
            release_view_stderr="HTTP 401: Bad credentials",
            gh_calls=gh_calls,
        )
    msg = str(exc.value)
    assert msg.startswith("release: ")
    assert "gh release view v0.5.0" in msg and "rc 1" in msg and "Bad credentials" in msg
    assert gh_calls == [["gh", "release", "view", "v0.5.0"]]  # no create
    assert "nothing to publish" not in capsys.readouterr().out


def test_publish_dies_when_the_existing_tag_create_fails(monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(
            monkeypatch,
            tmp_path,
            tag_exists_locally=True,
            remote_target=EXISTING_TARGET,
            release_view_rc=1,
            release_view_stderr=RELEASE_NOT_FOUND,
            returncode=1,
            stderr="tag v0.5.0 doesn't exist in the repo",
        )
    msg = str(exc.value)
    assert msg.startswith("release: ") and "gh release create v0.5.0" in msg and "rc 1" in msg
    captured = capsys.readouterr()
    assert "doesn't exist in the repo" in captured.err
    assert "published" not in captured.out


def test_publish_dies_without_gh_on_the_existing_tag_path(monkeypatch, tmp_path):
    gh_calls: list = []
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(
            monkeypatch,
            tmp_path,
            tag_exists_locally=True,
            remote_target=EXISTING_TARGET,
            gh_available=False,
            gh_calls=gh_calls,
        )
    assert "`gh` CLI not found" in str(exc.value)
    assert gh_calls == []


# --- a fresh create re-verifies where the tag landed (DW-412) --------------- #
# `gh` ignores `--target` when the tag already exists on origin, so rc 0 alone does
# not say the release is attached to the commit this run targeted.
def test_publish_dies_when_a_fresh_create_lands_on_another_commit(monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(monkeypatch, tmp_path, returncode=0, remote_target="f00d" * 10)
    msg = str(exc.value)
    assert msg.startswith("release: ")
    assert "v0.5.0" in msg and "f00d" * 10 in msg and HEAD_SHA in msg
    assert "pre-existing tag" in msg
    assert "published" not in capsys.readouterr().out


def test_publish_dies_when_a_fresh_create_leaves_no_remote_tag(monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(monkeypatch, tmp_path, returncode=0, remote_target=None)
    msg = str(exc.value)
    assert msg.startswith("release: ") and "origin has no v0.5.0 tag" in msg
    assert "published" not in capsys.readouterr().out


def test_publish_reports_published_when_the_fresh_tag_lands_on_head(monkeypatch, capsys, tmp_path):
    rc = _publish_with_gh(monkeypatch, tmp_path, returncode=0)
    assert rc == 0
    assert "published v0.5.0" in capsys.readouterr().out


ANNOTATED_LS_REMOTE = (
    "a776dfd9bfdbbbee76eb8cba6779a48d0116914c\trefs/tags/v1.0.0\n"
    "aa3e24f7b13975613badab9074c95616055ef9da\trefs/tags/v1.0.0^{}\n"
)


def test_peeled_tag_target_prefers_the_peeled_commit_of_an_annotated_tag():
    target = release.peeled_tag_target(ANNOTATED_LS_REMOTE, "v1.0.0")
    assert target == "aa3e24f7b13975613badab9074c95616055ef9da"


def test_peeled_tag_target_reads_a_lightweight_tag():
    out = "aa3e24f7b13975613badab9074c95616055ef9da\trefs/tags/v1.0.1\n"
    assert release.peeled_tag_target(out, "v1.0.1") == "aa3e24f7b13975613badab9074c95616055ef9da"


def test_peeled_tag_target_ignores_a_tail_matching_foreign_ref():
    # ls-remote's pattern tail-matches, so this ref really comes back for `refs/tags/v1.0.1`.
    out = "1111111111111111111111111111111111111111\trefs/tags/x/refs/tags/v1.0.1\n"
    assert release.peeled_tag_target(out, "v1.0.1") is None


def test_peeled_tag_target_is_none_on_empty_output():
    assert release.peeled_tag_target("", "v1.0.0") is None


def test_remote_tag_target_probes_exact_and_peeled_refs(monkeypatch):
    seen: list = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return SimpleNamespace(returncode=0, stdout=ANNOTATED_LS_REMOTE, stderr="")

    monkeypatch.setattr(release, "_run", fake_run)
    assert release.remote_tag_target("v1.0.0") == "aa3e24f7b13975613badab9074c95616055ef9da"
    assert seen == [
        ["git", "ls-remote", "--tags", "origin", "refs/tags/v1.0.0", "refs/tags/v1.0.0^{}"]
    ]


def test_remote_tag_target_is_none_when_origin_has_no_such_tag(monkeypatch):
    monkeypatch.setattr(
        release, "_run", lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    assert release.remote_tag_target("v9.9.9") is None


def test_remote_tag_target_dies_when_ls_remote_fails(monkeypatch):
    monkeypatch.setattr(
        release,
        "_run",
        lambda cmd, **kw: SimpleNamespace(
            returncode=128, stdout="", stderr="fatal: unable to access origin"
        ),
    )
    with pytest.raises(SystemExit) as exc:
        release.remote_tag_target("v1.0.0")
    assert "rc 128" in str(exc.value)
    assert "unable to access origin" in str(exc.value)


def test_version_at_parses_the_init_blob_at_the_commit(monkeypatch):
    seen: list = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        blob = '"""bmad-loop."""\n\n__version__ = "0.4.2"\n'
        return SimpleNamespace(returncode=0, stdout=blob, stderr="")

    monkeypatch.setattr(release, "_run", fake_run)
    assert release.version_at("abc123") == "0.4.2"
    assert seen == [["git", "show", "abc123:src/bmad_loop/__init__.py"]]


def test_version_at_is_none_when_git_show_fails_or_has_no_version(monkeypatch):
    monkeypatch.setattr(
        release,
        "_run",
        lambda cmd, **kw: SimpleNamespace(returncode=128, stdout="", stderr="fatal: bad object"),
    )
    assert release.version_at("abc123") is None
    monkeypatch.setattr(
        release,
        "_run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="x = 1\n", stderr=""),
    )
    assert release.version_at("abc123") is None


def test_publish_still_dies_on_a_genuine_gh_failure(monkeypatch, capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _publish_with_gh(
            monkeypatch,
            tmp_path,
            returncode=1,
            stderr="HTTP 401: Bad credentials",
        )
    assert "gh release create v0.5.0" in str(exc.value)
    assert "Bad credentials" in capsys.readouterr().err


def test_publish_passes_check_false_so_the_swallow_inspects_the_rc(monkeypatch, tmp_path):
    seen: dict[str, object] = {}
    _publish_with_gh(monkeypatch, tmp_path, returncode=0, stderr="", seen=seen)
    assert seen["check"] is False


# --- release.py's subprocess text is UTF-8 on every platform (DW-518) ------- #
# `text=True` with no `encoding=` uses the locale's code page — the ANSI one on
# Windows — so a manual Windows publish would mis-encode the notes' em dashes.
def test_run_decodes_child_output_as_utf8(monkeypatch):
    seen: dict[str, object] = {}

    def fake_run(*a, **kw):
        seen.update(kw)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    release._run(["git", "status"], capture=True)
    assert seen["text"] is True
    assert seen["encoding"] == "utf-8"


@pytest.mark.parametrize(
    "path_kwargs",
    [
        pytest.param({}, id="fresh-tag"),
        pytest.param(
            {
                "tag_exists_locally": True,
                "remote_target": EXISTING_TARGET,
                "release_view_rc": 1,
                "release_view_stderr": RELEASE_NOT_FOUND,
            },
            id="existing-tag",
        ),
    ],
)
def test_publish_sends_the_notes_to_gh_as_utf8(monkeypatch, tmp_path, path_kwargs):
    # Per-call kwargs, not the merged `seen`: on the existing-tag path `_run` also
    # carries encoding=, which would mask a create call that lacks it.
    calls: list = []
    changelog = SAMPLE.replace("It no longer breaks.", "It no longer breaks — anywhere.")
    rc = _publish_with_gh(
        monkeypatch, tmp_path, call_kwargs=calls, changelog=changelog, **path_kwargs
    )
    assert rc == 0
    creates = [kw for cmd, kw in calls if cmd[:3] == ["gh", "release", "create"]]
    assert len(creates) == 1
    assert "—" in str(creates[0]["input"])  # the em dash the encoding protects
    assert creates[0]["encoding"] == "utf-8"


# --- publish bounds the release body -------------------------------------- #
# GitHub rejects a body over 125,000 chars with HTTP 422 *after* `gh` has created the
# tag, which strands a tag with no release (v0.12.0's first publish). The body is a
# view of the CHANGELOG, so it is the body that yields, at an entry boundary.
def _long_section(entries: int, *, width: int = 200) -> str:
    return "### Fixed\n\n" + "\n".join(f"- Entry {i:05d}. " + "x" * width for i in range(entries))


def test_bound_release_notes_returns_short_notes_untouched():
    notes = "### Fixed\n\n- **A thing.** It no longer breaks."
    assert release.bound_release_notes(notes, "0.5.0", REPO_URL) is notes


def test_bound_release_notes_cuts_at_an_entry_boundary_and_links_the_changelog():
    notes = _long_section(40)
    out = release.bound_release_notes(notes, "0.5.0", REPO_URL, limit=2_000)
    assert len(out) <= 2_000
    body, _, footer = out.partition("\n\n---\n\n")
    # Every surviving line is a whole entry — none sliced mid-sentence.
    assert all(line.startswith("- Entry ") and line.endswith("x") for line in body.splitlines()[2:])
    assert "- Entry 00000." in body
    assert f"{REPO_URL}/blob/v0.5.0/CHANGELOG.md" in footer
    assert "truncated at GitHub's 2,000-character limit" in footer


def test_bound_release_notes_drops_a_heading_left_with_no_entries():
    notes = "### Added\n\n- " + "a" * 400 + "\n\n### Fixed\n\n- " + "b" * 400
    out = release.bound_release_notes(notes, "0.5.0", REPO_URL, limit=700)
    body = out.partition("\n\n---\n\n")[0]
    assert "### Added" in body
    assert "### Fixed" not in body  # its only entry did not fit, so the heading goes too


def test_bound_release_notes_hard_cuts_when_even_the_first_entry_overflows():
    notes = "- " + "z" * 5_000
    out = release.bound_release_notes(notes, "0.5.0", REPO_URL, limit=600)
    assert len(out) <= 600
    assert out.startswith("- zzz")


def test_publish_sends_a_bounded_body_and_says_so(monkeypatch, capsys, tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(SAMPLE.replace("- **A thing.** It no longer breaks.", _long_section(1_000)))
    monkeypatch.setattr(release, "CHANGELOG", cl)
    monkeypatch.setattr(release.sync_version, "read_canonical", lambda: "0.5.0")
    monkeypatch.setattr(release, "tag_exists", lambda tag: False)
    monkeypatch.setattr(release, "repo_url", lambda: REPO_URL)
    monkeypatch.setattr(release, "_git_out", lambda *a: "deadbeef" * 5)
    monkeypatch.setattr(release, "remote_tag_target", lambda tag: "deadbeef" * 5)
    monkeypatch.setattr(release.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen: dict[str, object] = {}

    def fake_run(*a, **kw):
        seen.update(kw)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    rc = release.cmd_publish(SimpleNamespace(dry_run=False))
    assert rc == 0
    sent = seen["input"]
    assert isinstance(sent, str) and len(sent) <= release.GITHUB_NOTES_LIMIT
    assert f"{REPO_URL}/blob/v0.5.0/CHANGELOG.md" in sent
    assert "over GitHub's 125,000 limit" in capsys.readouterr().out


def test_prepare_warns_on_a_release_star_branch(monkeypatch, capsys, tmp_path):
    # `_prepare_dry_run` pins the branch to `release/0.5.0`, the shape release.yml publishes
    # from on push — the warning is what tells a PR author they are about to self-publish.
    assert _prepare_dry_run(monkeypatch, tmp_path, PROMOTED) == 0
    out = capsys.readouterr().out
    assert "matches release.yml's `release/*` trigger" in out


def test_prepare_stays_quiet_on_a_chore_branch(monkeypatch, capsys, tmp_path):
    assert _prepare_dry_run(monkeypatch, tmp_path, PROMOTED, branch="chore/release-0.5.0") == 0
    assert "release/*" not in capsys.readouterr().out


def test_prepare_warns_when_the_section_will_be_truncated(monkeypatch, capsys, tmp_path):
    long_promoted = PROMOTED.replace("- **A thing.** It no longer breaks.", _long_section(1_000))
    assert _prepare_dry_run(monkeypatch, tmp_path, long_promoted) == 0
    out = capsys.readouterr().out
    assert "warning:" in out and "will truncate the release body" in out


# --- prepare refuses an unpromoted changelog -------------------------------- #
# `--dry-run` still runs every precondition before returning, so it drives the guard
# without mutating anything; `no_assets` + an absent `trunk` keep the whole path
# subprocess-free.
def _prepare_dry_run(
    monkeypatch, tmp_path, changelog_text, *, version="0.5.0", branch="release/0.5.0"
):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(changelog_text)
    monkeypatch.setattr(release, "CHANGELOG", cl)
    monkeypatch.setattr(release.sync_version, "read_canonical", lambda: "0.4.3")
    monkeypatch.setattr(release, "repo_url", lambda: REPO_URL)
    monkeypatch.setattr(release, "current_branch", lambda: branch)
    monkeypatch.setattr(release, "last_release_tag", lambda: "v0.4.3")
    monkeypatch.setattr(release, "tag_exists", lambda tag: False)
    monkeypatch.setattr(release, "dirty_paths", lambda: ["CHANGELOG.md"])
    monkeypatch.setattr(release, "tui_changed_since", lambda tag: False)
    monkeypatch.setattr(release.shutil, "which", lambda name: None)
    return release.cmd_prepare(
        SimpleNamespace(
            version=version, dry_run=True, force_assets=False, no_assets=True, allow_dirty=False
        )
    )


def test_prepare_refuses_a_still_populated_unreleased(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _prepare_dry_run(monkeypatch, tmp_path, DRIFTED)
    assert "`## [Unreleased]` still has content" in str(exc.value)


def test_prepare_refuses_a_never_reopened_unreleased(monkeypatch, tmp_path):
    # Renaming the heading without reopening one leaves `has_curated_section` False,
    # exactly as a correct promotion does — and `release.yml` publishes on push without
    # waiting for the CI check that would catch it, so `prepare` has to.
    with pytest.raises(SystemExit) as exc:
        _prepare_dry_run(monkeypatch, tmp_path, NO_UNRELEASED_HEADING)
    assert "no `## [Unreleased]` heading" in str(exc.value)


def test_prepare_refuses_a_release_heading_without_a_date(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _prepare_dry_run(monkeypatch, tmp_path, UNDATED_RELEASE_HEADING)
    assert "is not `## [0.5.0] — <ISO date>`" in str(exc.value)


@pytest.mark.parametrize("bad", ["2026-02-31", "2026-99-99", "2026-13-05"])
def test_prepare_refuses_an_impossible_release_date(monkeypatch, tmp_path, bad):
    text = PROMOTED.replace("2026-07-01", bad, 1)
    with pytest.raises(SystemExit) as exc:
        _prepare_dry_run(monkeypatch, tmp_path, text)
    assert "real calendar date" in str(exc.value)


def test_prepare_refuses_a_leftover_second_unreleased(monkeypatch, tmp_path):
    # The empty heading is first, so anything that `search`es rather than scanning
    # every match reads it and passes while the real entries sit below, unshipped.
    with pytest.raises(SystemExit) as exc:
        _prepare_dry_run(monkeypatch, tmp_path, DUPLICATE_UNRELEASED)
    assert "2 `## [Unreleased]` headings" in str(exc.value)


def test_prepare_refuses_an_unreleased_reopened_below_the_release(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _prepare_dry_run(monkeypatch, tmp_path, UNRELEASED_REOPENED_BELOW)
    assert "sits below `## [0.5.0]`" in str(exc.value)


def test_prepare_accepts_a_promoted_changelog(monkeypatch, tmp_path):
    # The positive control: without it the guard above passes for a version bump
    # that `prepare` was refusing for some entirely different precondition.
    assert _prepare_dry_run(monkeypatch, tmp_path, PROMOTED) == 0


# --- check holds the promote-and-reopen result ------------------------------ #
def _check(monkeypatch, tmp_path, changelog_text, *, canonical="0.5.0", sync_rc=0):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(changelog_text)
    monkeypatch.setattr(release, "CHANGELOG", cl)
    monkeypatch.setattr(release.sync_version, "read_canonical", lambda: canonical)
    monkeypatch.setattr(release.sync_version, "check", lambda: sync_rc)
    monkeypatch.setattr(release, "repo_url", lambda: REPO_URL)
    return release.cmd_check(SimpleNamespace())


def test_check_passes_on_a_promoted_changelog(monkeypatch, tmp_path):
    assert _check(monkeypatch, tmp_path, PROMOTED) == 0


def test_check_flags_a_consumed_unreleased_heading(monkeypatch, capsys, tmp_path):
    rc = _check(monkeypatch, tmp_path, NO_UNRELEASED_HEADING)
    assert rc == 1
    assert "MISSING `## [Unreleased]` heading" in capsys.readouterr().err


def test_check_flags_a_missing_unreleased_compare_ref(monkeypatch, capsys, tmp_path):
    rc = _check(monkeypatch, tmp_path, NO_UNRELEASED_REF)
    assert rc == 1
    assert "MISSING `[Unreleased]:` compare link ref" in capsys.readouterr().err


def test_check_flags_an_unreleased_compare_link_to_another_repo(monkeypatch, capsys, tmp_path):
    # Correct version, wrong repository: the base alone cannot tell these apart.
    text = PROMOTED.replace(f"{REPO_URL}/compare", "https://github.com/other/repo/compare", 1)
    rc = _check(monkeypatch, tmp_path, text)
    assert rc == 1
    assert "compares against https://github.com/other/repo" in capsys.readouterr().err


def test_check_flags_a_stale_unreleased_compare_base(monkeypatch, capsys, tmp_path):
    rc = _check(monkeypatch, tmp_path, STALE_UNRELEASED_REF)
    assert rc == 1
    assert "STALE `[Unreleased]:` compare base v0.4.3" in capsys.readouterr().err


def test_check_still_reports_a_version_field_mismatch(monkeypatch, tmp_path):
    # `sync_version.check()` moved in-process so CI can run this under
    # `--no-project`; it still has to gate the exit code.
    assert _check(monkeypatch, tmp_path, PROMOTED, sync_rc=1) == 1


def test_check_flags_a_missing_section_for_the_canonical_version(monkeypatch, capsys, tmp_path):
    rc = _check(monkeypatch, tmp_path, PROMOTED, canonical="9.9.9")
    assert rc == 1
    assert "MISSING `## [9.9.9]` section" in capsys.readouterr().err


@pytest.mark.parametrize(
    "stderr,lost",
    [
        ("Release.tag_name already exists", True),
        ("release already exists", True),
        ("ALREADY EXISTS", True),
        ("HTTP 401: Bad credentials", False),
        ("HTTP 422: Validation Failed\ntarget_commitish is invalid", False),
        ("", False),
    ],
)
def test_already_exists_matches_only_the_duplicate_tag_error(stderr, lost):
    assert release._already_exists(stderr) is lost


# --- prepare writes the CHANGELOG as LF on every platform (DW-517) ----------- #
def test_prepare_writes_the_changelog_as_lf_under_windows_newline_translation(
    monkeypatch, tmp_path, emulate_windows_newlines
):
    """A Windows release cut must not rewrite CHANGELOG.md as CRLF —
    `.gitattributes` has no `text=auto` rule to normalize it back."""
    # Drop the version's link ref so prepare's write visibly changes the file.
    link_ref = f"[0.5.0]: {REPO_URL}/releases/tag/v0.5.0\n"
    assert link_ref in PROMOTED
    cl = tmp_path / "CHANGELOG.md"
    cl.write_bytes(PROMOTED.replace(link_ref, "").encode("utf-8"))
    monkeypatch.setattr(release, "CHANGELOG", cl)
    monkeypatch.setattr(release.sync_version, "read_canonical", lambda: "0.4.3")
    monkeypatch.setattr(release, "repo_url", lambda: REPO_URL)
    monkeypatch.setattr(release, "current_branch", lambda: "chore/release-0.5.0")
    monkeypatch.setattr(release, "last_release_tag", lambda: "v0.4.3")
    monkeypatch.setattr(release, "tag_exists", lambda tag: False)
    monkeypatch.setattr(release, "dirty_paths", lambda: ["CHANGELOG.md"])
    monkeypatch.setattr(release, "_reseed_skills", lambda dry_run: None)
    monkeypatch.setattr(release, "_run_trunk_fmt", lambda dry_run: None)
    ran: list = []
    monkeypatch.setattr(release, "_run", lambda cmd, **kw: ran.append(cmd))
    emulate_windows_newlines()

    rc = release.cmd_prepare(
        SimpleNamespace(
            version="0.5.0", dry_run=False, force_assets=False, no_assets=True, allow_dirty=False
        )
    )

    assert rc == 0
    assert ran[-1][:2] == ["git", "commit"]  # reached the end of the mutate phase
    data = cl.read_bytes()
    assert link_ref.encode("utf-8") in data  # control: the write happened
    assert b"\r\n" not in data
