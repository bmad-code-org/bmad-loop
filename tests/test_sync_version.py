"""Unit tests for `scripts/sync_version.py`'s `check()` — the `version-sync` CI gate.

The gate's job is to refuse a release whose version fields disagree. What these cover
is the failure mode that is *invisible* when it happens: `check()` reading a
marketplace.json whose `plugins` list it cannot find, iterating zero times, and
printing "ok: every version field agrees" — announcing agreement it never verified.
A gate that is green for both "correct" and "unreadable" carries no information.

Every negative case here is paired with a positive control (a genuinely stale version
must still redden), because a `check() != 0` assertion passes for every reason a
non-zero could arise — including a bug in the fixture.
"""

from __future__ import annotations

import json
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sync_version  # noqa: E402


@pytest.fixture
def marketplace(tmp_path, monkeypatch):
    """Point `check()` at a throwaway marketplace.json, leaving every other file real.

    Returns a writer taking the parsed-JSON object, so a test names the corruption it
    is injecting rather than a blob of literal JSON.
    """
    target = tmp_path / "marketplace.json"
    monkeypatch.setattr(sync_version, "MARKETPLACE", target)
    canonical = sync_version.read_canonical()

    def write(obj) -> None:
        target.write_text(json.dumps(obj), encoding="utf-8")

    write({"plugins": [{"name": "bmad-loop", "version": canonical}]})
    return write


def test_agreeing_versions_pass(marketplace):
    """Baseline: the shape the repo actually ships is green."""
    assert sync_version.check() == 0


def test_stale_plugin_version_is_caught(marketplace):
    """Positive control. Without this, every assertion below could be passing
    because the gate is broken outright rather than because the guard works."""
    marketplace({"plugins": [{"name": "bmad-loop", "version": "0.0.1-stale"}]})
    assert sync_version.check() == 1


@pytest.mark.parametrize(
    ("label", "doc"),
    [
        ("renamed key", {"plugin": [{"name": "bmad-loop", "version": "0.0.1-stale"}]}),
        ("missing key", {"name": "bmad marketplace"}),
        ("empty list", {"plugins": []}),
        ("wrong type", {"plugins": {}}),
        ("null", {"plugins": None}),
    ],
)
def test_unusable_plugins_list_fails_instead_of_passing_vacuously(marketplace, label, doc):
    """The defect this file exists for.

    Measured against the pre-guard code, these split into two failure modes, and the
    guard's job is to turn both into one clean drift report:

    * ``renamed key`` / ``missing key`` / ``empty list`` / ``wrong type`` returned **0**
      while printing "ok: every version field agrees on <v>" — a silent vacuous pass.
    * ``null`` raised ``TypeError`` from ``enumerate(None)`` — fail-closed, but as a
      crashed job rather than the drift report this gate exists to emit.

    ``renamed key`` is deliberately loaded with a *stale* version too, so a green result
    would mean the gate waved through real drift, not merely an empty plugin list.
    """
    marketplace(doc)
    assert sync_version.check() == 1, f"{label}: check() passed with no plugins to inspect"


def test_non_object_plugin_entry_is_reported_not_raised(marketplace):
    """A string where an object belongs used to hit `.get` on a str and raise
    AttributeError. Exiting non-zero via traceback is technically fail-closed, but it
    reads as a crashed job rather than the drift report this gate is supposed to emit."""
    marketplace({"plugins": ["bmad-loop"]})
    assert sync_version.check() == 1


def test_second_plugin_stale_is_caught(marketplace):
    """The loop must not stop at the first agreeing entry."""
    canonical = sync_version.read_canonical()
    marketplace(
        {
            "plugins": [
                {"name": "bmad-loop", "version": canonical},
                {"name": "other", "version": "0.0.1-stale"},
            ]
        }
    )
    assert sync_version.check() == 1


# --- parse_canonical: the one owner of the __version__ parse ---------------- #
# release.py reads `git show <commit>:__init__.py` blobs through it, so it must
# agree with read_canonical on the checkout and answer None, not exit, on a miss.
def test_parse_canonical_agrees_with_read_canonical():
    assert sync_version.parse_canonical(sync_version.INIT.read_text()) == (
        sync_version.read_canonical()
    )


def test_parse_canonical_is_none_without_a_version():
    assert sync_version.parse_canonical('"""no version here."""\n') is None


# --- stamp() writes LF on every platform (DW-517) --------------------------- #
def test_stamp_writes_lf_under_windows_newline_translation(
    tmp_path, monkeypatch, emulate_windows_newlines
):
    """A Windows release cut must not rewrite the stamped LF files as CRLF —
    `.gitattributes` has no `text=auto` rule to normalize them back."""
    stamped = {}
    for name in ("INIT", "PYPROJECT", "CANONICAL_MODULE_YAML", "MARKETPLACE", "BMOD_TOML"):
        src = getattr(sync_version, name)
        dst = tmp_path / f"{name.lower()}{src.suffix}"
        shutil.copyfile(src, dst)
        monkeypatch.setattr(sync_version, name, dst)
        stamped[name] = dst
    monkeypatch.setattr(sync_version, "_relock", lambda: None)
    emulate_windows_newlines()

    sync_version.stamp("99.0.0")

    for name, path in stamped.items():
        data = path.read_bytes()
        # Control: the file was actually rewritten, so "no CRLF" is not vacuous.
        assert b"99.0.0" in data, name
        assert b"\r\n" not in data, name


# --- bmod.toml: the BMAD v7 module record ------------------------------------ #
@pytest.fixture
def bmod_toml(tmp_path, monkeypatch):
    """Point `check()`/`stamp()` at a throwaway copy of the shipped module record."""
    target = tmp_path / "bmod.toml"
    shutil.copyfile(sync_version.BMOD_TOML, target)
    monkeypatch.setattr(sync_version, "BMOD_TOML", target)
    return target


def test_bmod_version_drift_is_caught(bmod_toml):
    """A record whose `[bmod] version` lags `__version__` reddens the gate.

    The unmodified copy is the positive control: green before the edit, so the red
    after it is the drift and not a broken fixture. Delete the bmod arm of `check()`
    and this fails.
    """
    assert sync_version.check() == 0
    text = bmod_toml.read_text(encoding="utf-8")
    canonical = sync_version.read_canonical()
    bmod_toml.write_text(
        text.replace(f'version = "{canonical}"', 'version = "0.0.1-stale"', 1), encoding="utf-8"
    )
    assert sync_version.check() == 1


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("no [bmod] table", '[skill]\nbmod = "bmod-bmad-loop"\n'),
        ("no version key", '[bmod]\ncode = "bmad-loop"\n'),
        ("not TOML", "[bmod\nversion = \n"),
    ],
)
def test_unreadable_bmod_record_fails_instead_of_passing(bmod_toml, label, body):
    """A record the check cannot read is drift, never a vacuous pass."""
    bmod_toml.write_text(body, encoding="utf-8")
    assert sync_version.check() == 1, label


def test_missing_bmod_record_is_reported(bmod_toml):
    bmod_toml.unlink()
    assert sync_version.check() == 1


def test_bmod_stamp_leaves_requirement_minimums(bmod_toml):
    """Only the `[bmod]` table's own `version` moves.

    `required_skills` minimums are other modules' versions, and a `version` key in
    another table (here a `[skill]` table placed first) is not the module's. An
    unscoped `version = "…"` substitution — the marketplace stamper's shape — would
    rewrite them, and this fails.
    """
    original = bmod_toml.read_text(encoding="utf-8")
    text = '[skill]\nversion = "1.2.3"\n\n' + original
    stamped = sync_version._stamp_bmod(text, "99.0.0", bmod_toml)

    before, after = tomllib.loads(text), tomllib.loads(stamped)
    assert after["bmod"]["version"] == "99.0.0"
    assert after["skill"] == before["skill"]
    assert after["bmod"]["required_skills"] == before["bmod"]["required_skills"]
    # The minimums are real data, so "unchanged" is not vacuous.
    assert {r["version"] for r in after["bmod"]["required_skills"]} == {"6.13.0"}
    # Exactly one line differs.
    changed = [
        (a, b) for a, b in zip(text.splitlines(), stamped.splitlines(), strict=True) if a != b
    ]
    assert len(changed) == 1 and changed[0][1] == 'version = "99.0.0"'


def test_bmod_stamp_refuses_a_record_without_a_version_line(bmod_toml):
    with pytest.raises(SystemExit):
        sync_version._stamp_bmod('[bmod]\ncode = "x"\n', "1.0.0", bmod_toml)


@pytest.mark.parametrize(
    "version",
    [
        "0.14.0.dev1",  # PEP 440 dev release: four dot parts
        "0.14.0-dev1",  # upstream parse_orderable_semver refuses any `-dev`
        "0.14.0-rc-DEV",
        "01.2.3",  # leading zero
        "1.2.03",
        "1.2.3-01",  # numeric pre-release identifier with a leading zero
        "1.2.3+build.5",  # build metadata: upstream's validator refuses it on a record
        "1.2.3-",
        "1.2.3-rc..1",
        "1.2",
        "v1.2.3",
    ],
)
def test_version_regex_refuses_non_semver(version, tmp_path, monkeypatch):
    """Every shape `bmad setup` cannot order is refused, and `stamp()` writes nothing."""
    assert not sync_version._VERSION_RE.match(version)
    target = tmp_path / "__init__.py"
    target.write_text('__version__ = "0.13.2"\n', encoding="utf-8")
    monkeypatch.setattr(sync_version, "INIT", target)
    with pytest.raises(SystemExit):
        sync_version.stamp(version)
    assert target.read_text(encoding="utf-8") == '__version__ = "0.13.2"\n'


@pytest.mark.parametrize("version", ["0.13.2", "1.0.0-rc1", "1.0.0-rc.1", "6.13.0-next", "0.0.0"])
def test_version_regex_accepts_semver(version):
    """Positive control for the refusals above."""
    assert sync_version._VERSION_RE.match(version)
