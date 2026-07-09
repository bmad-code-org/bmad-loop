"""Regression guard for bmad-loop#64: the bmad-loop-setup cleanup scripts must
never delete live, manifest-tracked BMAD config/state.

On a BMAD v6 install ``_bmad/core/``, ``_bmad/<module>/`` and ``_bmad/_config/``
hold live config + installer manifests (no staged ``SKILL.md``). The setup scripts
used to hardcode ``core`` and ``--also-remove _config`` into their delete lists,
destroying that shared state. These tests run the real scripts as the SKILL.md
documents and assert only genuine redundant skill-payload dirs are ever removed.

Root cause is shared with upstream ``bmad-code-org/bmad-builder#96``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "src" / "bmad_loop" / "data" / "skills" / "bmad-loop-setup" / "scripts"
ASSETS = REPO / "src" / "bmad_loop" / "data" / "skills" / "bmad-loop-setup" / "assets"


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
    )


def _run_json(script: str, *args: str) -> dict:
    proc = _run(script, *args)
    assert (
        proc.returncode == 0
    ), f"{script} exit {proc.returncode}\nOUT:{proc.stdout}\nERR:{proc.stderr}"
    return json.loads(proc.stdout)


def _v6_bmad(tmp_path: Path) -> Path:
    """A realistic BMAD v6 `_bmad/` tree: live config + manifest, no staged skills."""
    bmad = tmp_path / "_bmad"
    (bmad / "core").mkdir(parents=True)
    (bmad / "core" / "config.yaml").write_text("user_name: BMad\noutput_folder: out\n")
    (bmad / "core" / "module-help.csv").write_text("module,skill\ncore,x\n")
    (bmad / "bmm").mkdir()
    (bmad / "bmm" / "config.yaml").write_text("dev_story_location: docs\n")
    (bmad / "bmad-loop").mkdir()
    (bmad / "bmad-loop" / "config.yaml").write_text("cadence: fast\n")
    cfg = bmad / "_config"
    cfg.mkdir()
    (cfg / "manifest.yaml").write_text("installation:\n  version: 6.10.0\n")
    (cfg / "files-manifest.csv").write_text("path,hash\n_bmad/core/config.yaml,abc\n")
    (cfg / "bmad-help.csv").write_text("module,skill\ncore,x\n")
    return bmad


def _skills_dir(tmp_path: Path, *installed: str) -> Path:
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    for name in installed:
        d = skills / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name}\n")
    return skills


# ---------------------------------------------------------------- cleanup-legacy


def test_cleanup_preserves_core_and_config_on_v6(tmp_path):
    """The documented invocation must be a no-op on a v6 install."""
    bmad = _v6_bmad(tmp_path)
    skills = _skills_dir(tmp_path)  # nothing installed — bmad-loop dir is config-bearing

    result = _run_json(
        "cleanup-legacy.py",
        "--bmad-dir",
        str(bmad),
        "--module-code",
        "bmad-loop",
        "--skills-dir",
        str(skills),
    )

    assert result["directories_removed"] == []
    assert (bmad / "bmad-loop" / "config.yaml").exists()
    assert (bmad / "core" / "config.yaml").exists()
    assert (bmad / "_config" / "manifest.yaml").exists()
    protected = {p["dir"] for p in result["directories_protected"]}
    assert "bmad-loop" in protected


def test_cleanup_refuses_explicit_core_and_config(tmp_path):
    """Even an explicit --module-code core / --also-remove _config must not delete them."""
    bmad = _v6_bmad(tmp_path)
    skills = _skills_dir(tmp_path)

    result = _run_json(
        "cleanup-legacy.py",
        "--bmad-dir",
        str(bmad),
        "--module-code",
        "core",
        "--also-remove",
        "_config",
        "--skills-dir",
        str(skills),
    )

    assert result["directories_removed"] == []
    assert (bmad / "core" / "config.yaml").exists()
    assert (bmad / "_config" / "manifest.yaml").exists()
    assert (bmad / "_config" / "files-manifest.csv").exists()


def test_cleanup_removes_genuine_redundant_payload(tmp_path):
    """A dir with an installed SKILL.md payload and no config IS cleaned."""
    bmad = _v6_bmad(tmp_path)
    payload = bmad / "legacy-mod"
    payload.mkdir()
    (payload / "SKILL.md").write_text("# legacy-mod\n")  # skill name == 'legacy-mod'
    skills = _skills_dir(tmp_path, "legacy-mod")

    result = _run_json(
        "cleanup-legacy.py",
        "--bmad-dir",
        str(bmad),
        "--module-code",
        "legacy-mod",
        "--skills-dir",
        str(skills),
    )

    assert result["directories_removed"] == ["legacy-mod"]
    assert not payload.exists()
    # protecting v6 state is unaffected
    assert (bmad / "core" / "config.yaml").exists()


def test_cleanup_errors_when_payload_skill_not_installed(tmp_path):
    """A payload dir whose skill is missing from skills-dir is an error, not a delete."""
    bmad = _v6_bmad(tmp_path)
    payload = bmad / "legacy-mod"
    payload.mkdir()
    (payload / "SKILL.md").write_text("# legacy-mod\n")
    skills = _skills_dir(tmp_path)  # legacy-mod NOT installed

    proc = _run(
        "cleanup-legacy.py",
        "--bmad-dir",
        str(bmad),
        "--module-code",
        "legacy-mod",
        "--skills-dir",
        str(skills),
    )
    assert proc.returncode == 1
    assert payload.exists()
    err = json.loads(proc.stdout)
    assert err["status"] == "error"
    assert "legacy-mod" in err["missing_skills"]


def test_cleanup_reports_removable_dir_absent_at_removal_time(tmp_path):
    """A removable dir gone by removal time must surface in directories_not_found.

    Regression for bmad-loop#73 review: cleanup_directories()'s not_found used to be
    discarded, so such a dir silently vanished from every JSON list. Exercised
    deterministically via a nested --also-remove target whose parent is removed first.
    """
    bmad = _v6_bmad(tmp_path)
    # 'legacy/' has no SKILL.md of its own; its only payload is 'legacy/child'.
    (bmad / "legacy" / "child").mkdir(parents=True)
    (bmad / "legacy" / "child" / "SKILL.md").write_text("# child\n")
    skills = _skills_dir(tmp_path, "child")

    result = _run_json(
        "cleanup-legacy.py",
        "--bmad-dir",
        str(bmad),
        "--module-code",
        "legacy",
        "--also-remove",
        "legacy/child",  # removed together with its parent 'legacy' earlier in the run
        "--skills-dir",
        str(skills),
    )

    # Parent is removed; the nested child is already gone by its turn but must still
    # be reported rather than silently dropped from all output lists.
    assert result["directories_removed"] == ["legacy"]
    assert "legacy/child" in result["directories_not_found"]
    assert not (bmad / "legacy").exists()


def test_cleanup_protects_core_by_name_even_as_pure_payload(tmp_path):
    """core/ is never removed — even holding only a SKILL.md payload and no config.

    Regression for bmad-loop#73 review: the docstring promised 'core' is never
    removed, but only _config was protected by name; a marker-less core payload
    slipped through the config-bearing check.
    """
    bmad = tmp_path / "_bmad"
    core = bmad / "core"
    core.mkdir(parents=True)
    (core / "SKILL.md").write_text("# core\n")  # no config markers at all
    skills = _skills_dir(tmp_path, "core")

    result = _run_json(
        "cleanup-legacy.py",
        "--bmad-dir",
        str(bmad),
        "--module-code",
        "core",
        "--skills-dir",
        str(skills),
    )

    assert result["directories_removed"] == []
    assert (core / "SKILL.md").exists()
    protected = {p["dir"] for p in result["directories_protected"]}
    assert "core" in protected


def test_cleanup_protects_dir_with_nested_live_config(tmp_path):
    """A config marker nested below the top level still protects the whole dir.

    Regression for bmad-loop#73 review: is_config_bearing() only looked at the
    candidate's top level while skill discovery was recursive, so a dir with a
    SKILL.md and deeper live config was misclassified as a removable payload.
    """
    bmad = _v6_bmad(tmp_path)
    legacy = bmad / "legacy-mod"
    (legacy / "sub").mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# legacy-mod\n")
    (legacy / "sub" / "config.yaml").write_text("live: true\n")  # nested live state
    skills = _skills_dir(tmp_path, "legacy-mod")

    result = _run_json(
        "cleanup-legacy.py",
        "--bmad-dir",
        str(bmad),
        "--module-code",
        "legacy-mod",
        "--skills-dir",
        str(skills),
    )

    assert result["directories_removed"] == []
    assert (legacy / "sub" / "config.yaml").exists()
    protected = {p["dir"] for p in result["directories_protected"]}
    assert "legacy-mod" in protected


def test_cleanup_removes_payload_whose_skill_ships_marker_named_assets(tmp_path):
    """Marker-named files inside a SKILL.md-bearing subtree are payload, not live state.

    Staged skill payloads legitimately ship files like assets/module-help.csv
    (bmad-loop-setup itself does); the recursive config scan must not
    false-protect such a dir or the documented bauto rename-cleanup would no-op.
    """
    bmad = _v6_bmad(tmp_path)
    legacy = bmad / "legacy-mod"
    foo = legacy / "skills" / "foo"
    (foo / "assets").mkdir(parents=True)
    (foo / "SKILL.md").write_text("# foo\n")
    (foo / "assets" / "module-help.csv").write_text("module,skill\nfoo,x\n")
    skills = _skills_dir(tmp_path, "foo")

    result = _run_json(
        "cleanup-legacy.py",
        "--bmad-dir",
        str(bmad),
        "--module-code",
        "legacy-mod",
        "--skills-dir",
        str(skills),
    )

    assert result["directories_removed"] == ["legacy-mod"]
    assert not legacy.exists()
    # live v6 state is untouched
    assert (bmad / "core" / "config.yaml").exists()


# ---------------------------------------------------------------- merge-config


def test_merge_config_preserves_legacy_configs(tmp_path):
    bmad = _v6_bmad(tmp_path)
    answers = tmp_path / "answers.json"
    answers.write_text("{}")

    result = _run_json(
        "merge-config.py",
        "--config-path",
        str(bmad / "config.yaml"),
        "--user-config-path",
        str(bmad / "config.user.yaml"),
        "--module-yaml",
        str(ASSETS / "module.yaml"),
        "--answers",
        str(answers),
        "--legacy-dir",
        str(bmad),
    )

    assert result["legacy_configs_deleted"] == []
    # live per-module + core config survive
    assert (bmad / "core" / "config.yaml").exists()
    assert (bmad / "bmad-loop" / "config.yaml").exists()
    # and the consolidated config was still written
    assert (bmad / "config.yaml").exists()


# ---------------------------------------------------------------- merge-help-csv


def test_merge_help_csv_preserves_legacy_csvs(tmp_path):
    bmad = _v6_bmad(tmp_path)

    result = _run_json(
        "merge-help-csv.py",
        "--target",
        str(bmad / "module-help.csv"),
        "--source",
        str(ASSETS / "module-help.csv"),
        "--legacy-dir",
        str(bmad),
        "--module-code",
        "bmad-loop",
    )

    assert result["legacy_csvs_deleted"] == []
    assert (bmad / "core" / "module-help.csv").exists()
    assert (bmad / "module-help.csv").exists()
