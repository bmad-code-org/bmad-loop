"""Unit tests for the Unity scene-guard seeder (``unity_seed_assets.py``).

The seeder copies the bundled scene auto-save guard payload (``unity_assets/``:
``SceneAutoSaveGuard.cs`` + its asmdef, with pre-generated fixed-GUID ``.meta``
files) into a project's ``Assets`` tree so a chronically-dirty scene never raises
the two run-stalling modal dialogs. These drive its ``main()`` end-to-end against a
temp worktree, using the real bundled payload (so the version-header + GUID contract
is exercised, not a stand-in).

The plugin-side wiring (env plumbing + the pre_worktree_setup/pre_ready_gate hooks
that invoke this seeder) lives in ``test_engine_plugin.py``.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from bmad_loop import platform_util
from bmad_loop.plugins import get_plugin

_GUARD_DIR = "Assets/BmadLoop/Editor"


def _load_seeder():
    path = os.path.join(get_plugin("unity").scripts_dir, "unity_seed_assets.py")
    spec = importlib.util.spec_from_file_location("unity_seed_assets_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _payload(mod) -> Path:
    return Path(mod.__file__).resolve().parent / "unity_assets"


def _set_env(monkeypatch, worktree, *, install="1", guard_dir=None):
    monkeypatch.setenv("BMAD_LOOP_WORKTREE", str(worktree))
    monkeypatch.setenv("BMAD_LOOP_UNITY_INSTALL_SCENE_GUARD", install)
    if guard_dir is None:
        monkeypatch.delenv("BMAD_LOOP_UNITY_SCENE_GUARD_DIR", raising=False)
    else:
        monkeypatch.setenv("BMAD_LOOP_UNITY_SCENE_GUARD_DIR", guard_dir)


# ---------------------------------------------------------------- fresh install


def test_seed_fresh_install_lays_all_payload_files(tmp_path, monkeypatch):
    mod = _load_seeder()
    _set_env(monkeypatch, tmp_path)
    (tmp_path / "Assets").mkdir()

    assert mod.main() == 0

    payload = _payload(mod)
    editor = tmp_path / _GUARD_DIR
    # content files land byte-identical to the payload (robust to a version bump)
    for name in (
        "SceneAutoSaveGuard.cs",
        "SceneAutoSaveGuard.cs.meta",
        "BmadLoop.Unity.Editor.asmdef",
        "BmadLoop.Unity.Editor.asmdef.meta",
    ):
        assert (editor / name).read_bytes() == (payload / name).read_bytes(), name
    # parent-folder metas carry the payload's fixed GUIDs (no per-worktree churn)
    assert (tmp_path / "Assets" / "BmadLoop.meta").read_bytes() == (
        payload / "_folders" / "BmadLoop.meta"
    ).read_bytes()
    assert (tmp_path / "Assets" / "BmadLoop" / "Editor.meta").read_bytes() == (
        payload / "_folders" / "Editor.meta"
    ).read_bytes()
    # Unity owns Assets/ itself — the seeder never lays an Assets.meta over it
    assert not (tmp_path / "Assets.meta").exists()


# ------------------------------------------------------------------ idempotency


def test_seed_idempotent_reinstall_is_noop(tmp_path, monkeypatch):
    mod = _load_seeder()
    _set_env(monkeypatch, tmp_path)
    (tmp_path / "Assets").mkdir()
    assert mod.main() == 0

    # a local edit that keeps the (matching) version header must survive a re-run:
    # an equal version is left untouched.
    cs = tmp_path / _GUARD_DIR / "SceneAutoSaveGuard.cs"
    cs.write_text(cs.read_text() + "\n// local edit marker\n", encoding="utf-8")

    assert mod.main() == 0
    assert "// local edit marker" in cs.read_text()  # not overwritten


# --------------------------------------------------------------- version bump


def test_seed_version_bump_reinstalls(tmp_path, monkeypatch):
    mod = _load_seeder()
    _set_env(monkeypatch, tmp_path)
    editor = tmp_path / _GUARD_DIR
    editor.mkdir(parents=True)
    cs = editor / "SceneAutoSaveGuard.cs"
    cs.write_text("// bmad-loop-scene-guard-version: 0.9.0\n// stale guard\n", encoding="utf-8")

    assert mod.main() == 0

    payload = _payload(mod)
    assert "0.9.0" not in cs.read_text()  # the stale guard was replaced
    assert cs.read_bytes() == (payload / "SceneAutoSaveGuard.cs").read_bytes()
    # the reinstall also brought the meta companions along
    assert (editor / "SceneAutoSaveGuard.cs.meta").is_file()
    assert (editor / "BmadLoop.Unity.Editor.asmdef").is_file()


def test_seed_newer_target_version_is_left_alone(tmp_path, monkeypatch):
    mod = _load_seeder()
    _set_env(monkeypatch, tmp_path)
    editor = tmp_path / _GUARD_DIR
    editor.mkdir(parents=True)
    cs = editor / "SceneAutoSaveGuard.cs"
    # a hypothetical future guard version must never be downgraded
    cs.write_text("// bmad-loop-scene-guard-version: 99.0.0\n// future guard\n", encoding="utf-8")

    assert mod.main() == 0
    assert "99.0.0" in cs.read_text()  # untouched


# ------------------------------------------------------------ graceful skips


def test_seed_skips_when_no_asset_root(tmp_path, monkeypatch):
    mod = _load_seeder()
    _set_env(monkeypatch, tmp_path)  # no Assets/ created

    assert mod.main() == 0  # benign skip, not an error
    assert not (tmp_path / "Assets").exists()  # nothing scattered into a non-Unity tree


def test_seed_disabled_via_env(tmp_path, monkeypatch):
    mod = _load_seeder()
    _set_env(monkeypatch, tmp_path, install="0")
    (tmp_path / "Assets").mkdir()

    assert mod.main() == 0
    assert not (tmp_path / "Assets" / "BmadLoop").exists()  # seeding did not run


def test_seed_errors_without_worktree(tmp_path, monkeypatch):
    mod = _load_seeder()
    monkeypatch.delenv("BMAD_LOOP_WORKTREE", raising=False)
    monkeypatch.setenv("BMAD_LOOP_UNITY_INSTALL_SCENE_GUARD", "1")
    assert mod.main() == 2  # a real error (no project to seed)


# ---------------------------------------------------------------- custom dir


def test_seed_custom_dir_keys_folder_metas_by_name(tmp_path, monkeypatch):
    mod = _load_seeder()
    _set_env(monkeypatch, tmp_path, guard_dir="Assets/Vendor/BmadLoop/Editor")
    (tmp_path / "Assets").mkdir()

    assert mod.main() == 0

    editor = tmp_path / "Assets" / "Vendor" / "BmadLoop" / "Editor"
    assert (editor / "SceneAutoSaveGuard.cs").is_file()
    # the payload keys folder metas by folder NAME, so BmadLoop/ + Editor/ still get
    # their fixed-GUID metas even under a custom parent...
    assert (tmp_path / "Assets" / "Vendor" / "BmadLoop.meta").is_file()
    assert (editor.parent / "Editor.meta").is_file()
    # ...but Vendor/ has no payload meta, so Unity auto-generates that one (we ship none)
    assert not (tmp_path / "Assets" / "Vendor.meta").exists()


# ------------------------------------------------------------- invalid dirs


def test_seed_rejects_absolute_guard_dir(tmp_path, monkeypatch):
    mod = _load_seeder()
    outside = tmp_path / "outside"
    outside.mkdir()
    worktree = tmp_path / "wt"
    (worktree / "Assets").mkdir(parents=True)
    _set_env(monkeypatch, worktree, guard_dir=str(outside / "Editor"))

    assert mod.main() == 2  # a real error, not a crash (relative_to would raise)
    assert not (outside / "Editor").exists()  # nothing written at the absolute target


def test_seed_rejects_parent_traversal_guard_dir(tmp_path, monkeypatch):
    mod = _load_seeder()
    # the escape target's parent (tmp_path) exists, so an unguarded seeder would
    # really create escaped/ out there — the mutate-check is meaningful
    worktree = tmp_path / "wt"
    (worktree / "Assets").mkdir(parents=True)
    _set_env(monkeypatch, worktree, guard_dir="Assets/../../escaped/Editor")

    assert mod.main() == 2
    assert not (tmp_path / "escaped").exists()  # no write outside the worktree


def test_seed_rejects_root_naming_guard_dir(tmp_path, monkeypatch):
    mod = _load_seeder()
    worktree = tmp_path / "wt"
    (worktree / "Assets").mkdir(parents=True)
    # `main()` `.strip()`s the env var, so the trailing-SPACE spellings collapse to
    # "." and the pre-existing `not rel.parts` arm already caught them. These are
    # the ones that survive that strip: on Windows each names the worktree itself,
    # and the asset-root probe below the guard would then find the worktree (a real
    # directory) and pass, scattering the payload across the worktree root.
    for evil in ("...", "....", ". .", ".\\"):
        _set_env(monkeypatch, worktree, guard_dir=evil)
        assert mod.main() == 2, evil
    assert not (worktree / mod._GUARD_CS).exists()  # payload never hit the root
    assert not (worktree / "BmadLoop").exists()


def test_seed_rejects_windows_flavored_escapes_on_any_platform(tmp_path, monkeypatch):
    mod = _load_seeder()
    (tmp_path / "Assets").mkdir()
    for evil in ("C:\\evil", "C:evil", "\\evil", "..\\evil"):
        _set_env(monkeypatch, tmp_path, guard_dir=evil)
        assert mod.main() == 2, evil
    assert not (tmp_path / "Assets" / "BmadLoop").exists()  # nothing seeded


def test_seed_rejects_a_win32_alias_guard_dir_on_any_platform(tmp_path, monkeypatch):
    """The fourth family member, wired into the same guard chain: a guard dir that
    names a Windows device, or whose trailing periods/spaces Win32 trims, installs
    somewhere other than the path it spells. Refused on every platform for the same
    reason the drive-qualified rows above are — a value must not mean one thing here
    and another on Windows. `Editor.` is the shape that matters in practice: nothing
    upstream catches it, since the caller `.strip()`s only the whole env var."""
    mod = _load_seeder()
    (tmp_path / "Assets").mkdir()
    for evil in ("Assets/NUL", "Assets/BmadLoop/Editor.", "Assets/BmadLoop /Editor", "NUL"):
        _set_env(monkeypatch, tmp_path, guard_dir=evil)
        assert mod.main() == 2, evil
    assert not (tmp_path / "Assets" / "BmadLoop").exists()  # nothing seeded


# --------------------------------------------- the hand-mirrored win32 predicate

# The phase-1 truth table from `tests/test_platform_util.py`, carried over verbatim:
# rule 1 (reserved device basenames, per component, both separators), rule 2 (the
# trailing period/space trim), the over-refusal tripwires (`com10`, `nulls`,
# `auxiliary`), and the root/parent spellings the predicate must leave to its three
# siblings. Any divergence between core and the clone shows up on one of these rows.
_WIN32_ALIAS_ROWS = (
    "NUL",
    "nul",
    "NUL.txt",
    "PRN  ",
    "sub/NUL",
    "sub/NUL.txt",
    "CONIN$",
    "COM1",
    "COM0",
    "LPT9",
    "AUX",
    "aux.json",
    "CON.",
    "sub\\NUL",
    ".claude/skills.",
    ".claude/skills ",
    "skills. ",
    "sub./x",
    "a/b ",
    ".claude/skills",
    "normal/path",
    "com10",
    "nulls",
    "auxiliary",
    "a.b/c.d",
    "..",
    ".",
    "",
    "...",
    "   ",
    ".. ",
)


@pytest.mark.parametrize("value", _WIN32_ALIAS_ROWS)
def test_seeder_win32_alias_clone_agrees_with_the_core_predicate(value):
    """This script is stdlib-only (it is deployed into a consumer project and cannot
    import bmad_loop), so `_names_win32_alias` is a hand-written mirror of
    `platform_util.names_win32_alias`. The file is also excluded from pyright and has
    no other guard against drift (#546) — so the mirror is pinned behaviorally, on
    the core predicate's own truth table, rather than trusted to stay in sync."""
    mod = _load_seeder()
    assert mod._names_win32_alias(value) is platform_util.names_win32_alias(value)


def test_seeder_reserved_basename_set_mirrors_core_member_for_member():
    """The truth table above cannot reach every member of the set, and a device name
    dropped from the clone would simply be seeded. Pin the set itself — including the
    deliberate over-refusals (`COM0`/`LPT0`, the `CONIN$`/`CONOUT$` pair) that neither
    Microsoft's published list nor Wine's `RtlIsDosDeviceName_U` backs on its own."""
    mod = _load_seeder()
    assert mod._RESERVED_BASENAMES == platform_util._RESERVED_BASENAMES


# ------------------------------------------------------------ version parsing


def test_parse_version_tuple_and_missing():
    mod = _load_seeder()
    assert mod._parse_version("// bmad-loop-scene-guard-version: 1.2.3\n") == (1, 2, 3)
    assert mod._parse_version("no header here") is None
