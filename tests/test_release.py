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
