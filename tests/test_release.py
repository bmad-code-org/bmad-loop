"""Unit tests for the pure helpers in scripts/release.py.

These cover the logic that decides versions, parses/extracts CHANGELOG sections,
inserts link references, groups commits, and short-circuits a publish when the tag
already exists. Anything touching real git/gh is exercised via monkeypatch.
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
        ("0.5.0.dev3", (0, 5, 0), "dev3"),
    ],
)
def test_parse_version(v, core, suffix):
    assert release.parse_version(v) == (core, suffix)


@pytest.mark.parametrize("bad", ["", "1.2", "1.2.x", "v1.2.3", "x.y.z"])
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
def test_publish_noop_when_tag_exists(monkeypatch, capsys):
    monkeypatch.setattr(release.sync_version, "read_canonical", lambda: "0.5.0")
    monkeypatch.setattr(release, "tag_exists", lambda tag: True)
    rc = release.cmd_publish(SimpleNamespace(dry_run=False))
    assert rc == 0
    assert "already exists" in capsys.readouterr().out


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
def _publish_with_gh(monkeypatch, tmp_path, *, returncode, stderr, seen=None):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(SAMPLE)
    monkeypatch.setattr(release, "CHANGELOG", cl)
    monkeypatch.setattr(release.sync_version, "read_canonical", lambda: "0.5.0")
    monkeypatch.setattr(release, "tag_exists", lambda tag: False)
    monkeypatch.setattr(release, "_git_out", lambda *a: "deadbeef" * 5)
    monkeypatch.setattr(release.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(*a, **kw):
        if seen is not None:
            seen.update(kw)
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    return release.cmd_publish(SimpleNamespace(dry_run=False))


def test_publish_treats_a_lost_race_as_success(monkeypatch, capsys, tmp_path):
    rc = _publish_with_gh(
        monkeypatch,
        tmp_path,
        returncode=1,
        stderr="HTTP 422: Validation Failed\nRelease.tag_name already exists",
    )
    assert rc == 0
    assert "created concurrently" in capsys.readouterr().out


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


# --- prepare refuses an unpromoted changelog -------------------------------- #
# `--dry-run` still runs every precondition before returning, so it drives the guard
# without mutating anything; `no_assets` + an absent `trunk` keep the whole path
# subprocess-free.
def _prepare_dry_run(monkeypatch, tmp_path, changelog_text, *, version="0.5.0"):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(changelog_text)
    monkeypatch.setattr(release, "CHANGELOG", cl)
    monkeypatch.setattr(release.sync_version, "read_canonical", lambda: "0.4.3")
    monkeypatch.setattr(release, "repo_url", lambda: REPO_URL)
    monkeypatch.setattr(release, "current_branch", lambda: "release/0.5.0")
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
