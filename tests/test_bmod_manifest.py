"""The shipped `bmod.toml` files parse under upstream `bmad setup`'s manifest rules.

BMAD v7 installs and update-checks modules from `bmod.toml` records:
`bmod-bmad-loop/bmod.toml` is bmad-loop's module record, and each member skill
carries a `[skill]`-only file pointing back at it. A record `bmad setup` rejects
makes the module invisible to it, and nothing in bmad-loop reads these files at
runtime, so only this test keeps them honest.

The rules are RE-IMPLEMENTED here from upstream BMAD-METHOD at 8f0d50e1 (the
program's pin), never imported: a harness must not depend on the artifact it
validates. Sources:

* `skills/bmad/scripts/setup.py` — `parse_bmod_file` :1379-1399,
  `parse_bmod_table` :1402-1421, `parse_skill_table` :1465-1480,
  `validate_source` :1483-1495, `parse_skill_names` :1498, requirement entries
  :1561-1596, `parse_orderable_semver` :1927-1941, `MODULE_NAME`/`SKILL_NAME`/
  `RESERVED_MODULE_DIRS`/`SEMVER` :31-48, membership in `discover_installation`
  :1111-1210 and `member_names` :1350.
* `skills/bmad/scripts/validate_manifests.py` — every skill folder ships a
  `bmod.toml` (:70); record version is SemVer, no `-dev`, no build metadata
  (:112-133); both install-message keys present (:163); a record folder ships
  `SKILL.md` (:208) and is named `bmod-<code>` (:210); a member's `[skill].source`
  equals its record's `update_source` (:226-267); `help/help.md` exists (:321).
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from bmad_loop.install import MODULE_SKILLS

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
RECORD = "bmod-bmad-loop"

# setup.py :31-48.
SOURCE_PREFIXES = ("github:", "https://", "file:", "plugin:")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
RESERVED_MODULE_DIRS = frozenset({"_config", "custom", "modules", "scripts"})
_NUM = r"(?:0|[1-9][0-9]*)"
_PRE = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
_BUILD = r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"
SEMVER = re.compile(rf"{_NUM}\.{_NUM}\.{_NUM}(?:-{_PRE}(?:\.{_PRE})*)?(?:\+{_BUILD})?\Z")


def _orderable(version: object) -> bool:
    """`parse_orderable_semver`: SEMVER, and no `-dev` in any case."""
    return (
        isinstance(version, str)
        and SEMVER.match(version) is not None
        and "-dev" not in version.casefold()
    )


def _source_problems(source: object) -> list[str]:
    """`validate_source`: a known prefix, a non-empty rest, owner/repo for github:."""
    if not isinstance(source, str) or not source:
        return [f"source must be a non-empty string, got {source!r}"]
    prefix = next((p for p in SOURCE_PREFIXES if source.startswith(p)), None)
    if prefix is None:
        return [f"source {source!r} has no known prefix {SOURCE_PREFIXES}"]
    rest = source[len(prefix) :]
    if not rest:
        return [f"source {source!r} is empty after its prefix"]
    if prefix == "github:":
        parts = rest.split("/")
        if len(parts) < 2 or any(not part for part in parts):
            return [f"github source {source!r} needs owner/repo with no empty parts"]
    if prefix == "https://" and any(ch.isspace() for ch in source):
        return [f"https source {source!r} contains whitespace"]
    return []


def _name_list_problems(field: str, value: object) -> list[str]:
    if not isinstance(value, list):
        return [f"{field} must be a list"]
    problems = [f"{field}: {v!r} is not a skill name" for v in value if not _is_name(v)]
    if len(set(map(str, value))) != len(value):
        problems.append(f"{field} repeats a name")
    return problems


def _is_name(value: object) -> bool:
    return isinstance(value, str) and NAME.match(value) is not None


def _requirement_problems(field: str, value: object) -> list[str]:
    """Requirement entries: a plain name, or `{skill, source, version?}`."""
    if not isinstance(value, list):
        return [f"{field} must be a list"]
    problems: list[str] = []
    names: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("skill")
            problems += [f"{field} {name!r}: {p}" for p in _source_problems(entry.get("source"))]
            if "version" in entry and not _orderable(entry["version"]):
                problems.append(f"{field} {name!r}: version {entry['version']!r} is unorderable")
            if "+" in str(entry.get("version", "")):
                problems.append(f"{field} {name!r}: version carries build metadata")
        else:
            problems.append(f"{field} entry {entry!r} is neither a name nor a table")
            continue
        if not _is_name(name):
            problems.append(f"{field}: {name!r} is not a skill name")
        names.append(str(name))
    if len(set(names)) != len(names):
        problems.append(f"{field} repeats a skill")
    return problems


def _load(folder: str) -> dict[str, object]:
    path = SKILLS / folder / "bmod.toml"
    assert path.is_file(), f"{folder} ships no bmod.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "bmod" in data or "skill" in data, f"{path} has neither [bmod] nor [skill]"
    return data


def _record_problems(folder: str, bmod: object) -> list[str]:
    """`parse_bmod_table` plus the validator's record-folder rules."""
    if not isinstance(bmod, dict):
        return ["[bmod] is not a table"]
    problems: list[str] = []
    code = bmod.get("code")
    if not _is_name(code) or str(code).casefold() in RESERVED_MODULE_DIRS:
        problems.append(f"code {code!r} is not a usable module code")
    if folder != f"bmod-{code}":
        problems.append(f"record folder {folder!r} is not named bmod-<code>")
    version = bmod.get("version")
    if not _orderable(version) or "+" in str(version):
        problems.append(f"version {version!r} is not orderable SemVer without build metadata")
    problems += _source_problems(bmod.get("update_source"))
    if "skills" in bmod:
        problems += _name_list_problems("skills", bmod["skills"])
    for field in ("required_skills", "recommended_skills"):
        if field in bmod:
            problems += _requirement_problems(field, bmod[field])
    for field in ("pre_install_message", "post_install_message"):
        if not isinstance(bmod.get(field), str):
            problems.append(f"{field} must be present as a string")
    if not (SKILLS / folder / "SKILL.md").is_file():
        problems.append("record folder ships no SKILL.md")
    if not (SKILLS / folder / "help" / "help.md").is_file():
        problems.append("record folder ships no help/help.md")
    return problems


def test_bmod_manifests_parse_under_upstream_rules():
    """The record and every member parse, and membership closes both ways."""
    assert RECORD in MODULE_SKILLS
    record = _load(RECORD)
    assert "skill" not in record, "the record is [bmod]-only, as upstream's records are"
    bmod = record["bmod"]
    assert not _record_problems(RECORD, bmod), _record_problems(RECORD, bmod)
    assert isinstance(bmod, dict)

    members = bmod["skills"]
    assert isinstance(members, list)
    # The record plus its listed members is exactly what `bmad-loop init` lays down.
    assert {RECORD, *members} == set(MODULE_SKILLS)

    for folder in members:
        data = _load(folder)
        assert "bmod" not in data, f"{folder} must be a member, not a second record"
        skill = data["skill"]
        assert isinstance(skill, dict), f"{folder}: [skill] is not a table"
        # `parse_skill_table`, standalone: `bmod` names the record, `source` is valid.
        assert skill.get("bmod") == RECORD, f"{folder} does not point back at {RECORD}"
        assert not _source_problems(skill.get("source")), folder
        # validate_manifests :226-267: a member's source is its record's update_source.
        assert skill["source"] == bmod["update_source"], folder
        assert "scripts" not in skill, f"{folder} declares scripts it does not ship"


def test_record_skill_md_never_invoked():
    """The record's SKILL.md names the folder and says it is metadata, never a skill."""
    text = (SKILLS / RECORD / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("---")[1]
    assert re.search(rf"(?m)^name:\s*{RECORD}\s*$", front)
    assert "Never invoke this skill." in front


def test_marketplace_lists_every_module_skill():
    """`npx skills add` finds a folder only through marketplace `skills`, so the
    record must be listed, and the list must match what `init` ships."""
    market = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    listed = market["plugins"][0]["skills"]
    assert len(listed) == len(set(listed))
    assert set(listed) == {f"./skills/{s}" for s in MODULE_SKILLS}


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("no prefix", "bmad-code-org/bmad-loop"),
        ("github without repo", "github:bmad-code-org"),
        ("github empty part", "github:bmad-code-org//skills"),
        ("empty after prefix", "file:"),
    ],
)
def test_source_rule_rejects_bad_sources(label, source):
    """Positive controls for the re-implemented `validate_source`: the shipped
    sources pass only because the rule can fail."""
    assert _source_problems(source), label
