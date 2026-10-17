#!/usr/bin/env python3
"""Single source of truth for the project + BMAD-module version.

The version number lives in several files across three formats and one
external consumer (the BMAD-method installer). They drifted once already
(package ``0.1.0`` vs module metadata ``1.0.0``), so this script is the only
sanctioned way to change the version, and CI runs ``--check`` to fail on drift.

The canonical value is ``bmad_loop.__version__`` in ``src/bmad_loop/__init__.py``.
Every other field is derived from it:

* ``pyproject.toml``                              -> ``[project] version``
* ``src/.../bmad-loop-setup/assets/module.yaml``  -> ``module_version``
* ``.claude-plugin/marketplace.json``             -> ``plugins[*].version``
* ``<repo-root>/module.yaml``                     -> byte-identical copy of the
  canonical module.yaml above. The BMAD installer's ``resolveInstalledModuleYaml``
  only discovers a descriptor at shallow paths (``skills/``, ``src/``, a
  ``*-setup/assets/`` directly under those, or the repo root) — the canonical
  copy under ``src/bmad_loop/data/skills/...`` is too deep, so this root mirror
  is what lets the installer locate the ``bmad-loop`` module.
* ``src/.../bmod-bmad-loop/bmod.toml``            -> ``[bmod] version``, the
  module record BMAD v7 ``bmad setup`` discovers. Only the ``[bmod]`` table's own
  ``version`` key is stamped: the ``required_skills`` entries carry minimum
  versions of *other* modules' skills, which a release never touches.

The version must be SemVer with an optional ``-`` pre-release. Upstream
``bmad setup`` orders module versions by SemVer and reports anything else as
unordered, so ``0.14.0.dev1`` or a ``-dev`` pre-release is refused here.

Stamping also runs ``uv lock`` to refresh ``uv.lock`` (which pins the project
version); CI's ``uv sync --locked`` fails the install step on a stale lock, so
the relock is part of the stamp rather than a manual follow-up.

Usage::

    uv run python scripts/sync_version.py 0.2.0   # stamp a new version everywhere (+ uv lock)
    uv run python scripts/sync_version.py --check  # verify all fields agree (CI)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INIT = ROOT / "src" / "bmad_loop" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
CANONICAL_MODULE_YAML = (
    ROOT / "src" / "bmad_loop" / "data" / "skills" / "bmad-loop-setup" / "assets" / "module.yaml"
)
ROOT_MODULE_YAML = ROOT / "module.yaml"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
BMOD_TOML = ROOT / "src" / "bmad_loop" / "data" / "skills" / "bmod-bmad-loop" / "bmod.toml"

# Upstream `bmad setup`'s SEMVER (skills/bmad/scripts/setup.py) minus build metadata,
# which its manifest validator refuses on a record version: no leading zeros, and an
# optional `-` pre-release of dot-separated identifiers. The lookahead mirrors
# `parse_orderable_semver`, which treats any version containing `-dev` (any case) as
# unorderable, so `bmad setup` could never report it as current or outdated.
_NUM = r"(?:0|[1-9][0-9]*)"
_PRE_ID = rf"(?:{_NUM}|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
_VERSION_RE = re.compile(
    rf"^(?!.*-(?i:dev)){_NUM}\.{_NUM}\.{_NUM}(?:-{_PRE_ID}(?:\.{_PRE_ID})*)?\Z"
)

_INIT_PAT = re.compile(r'(?m)^(__version__\s*=\s*")[^"]*(")')
_PYPROJECT_PAT = re.compile(r'(?m)^(version\s*=\s*")[^"]*(")')
_MODULE_VERSION_PAT = re.compile(r"(?m)^(module_version:\s*)\S+")
# Targeted replace so we touch only the version value and leave marketplace.json
# formatting (compact arrays etc.) for the JSON formatter to own.
_MARKET_PAT = re.compile(r'(?m)^(\s*"version"\s*:\s*")[^"]*(")')
# The `[bmod]` table runs from its header to the next table header. Inside it, only a
# line that STARTS with `version =` is the module's own version: the inline
# `{ skill = ..., version = ... }` requirement entries sit on their own indented lines
# (bmod.toml writes those arrays multi-line), so they never match. The header and
# version-line shapes follow upstream's `BMOD_HEADER`/`VERSION_LINE`
# (skills/bmad/scripts/validate_manifests.py).
_BMOD_TABLE_PAT = re.compile(
    r"(?ms)^[ \t]*\[[ \t]*bmod[ \t]*\][ \t]*(?:#[^\n]*)?$(.*?)(?=^[ \t]*\[|\Z)"
)
_BMOD_VERSION_PAT = re.compile(r'(?m)^([ \t]*version[ \t]*=[ \t]*")[^"\n]*(")')


def parse_canonical(text: str) -> str | None:
    """The ``__version__`` value in ``__init__.py`` source ``text``, or ``None``.

    The single owner of that parse: :func:`read_canonical` reads the checkout with
    it, and ``release.py`` reads a ``git show`` blob of another commit with it.
    """
    m = _INIT_PAT.search(text)
    return m.group(0).split('"')[1] if m else None


def read_canonical() -> str:
    version = parse_canonical(INIT.read_text(encoding="utf-8"))
    if version is None:
        sys.exit(f"error: could not find __version__ in {INIT}")
    return version


def _sub_once(pat: re.Pattern[str], repl: str, text: str, path: Path) -> str:
    new, n = pat.subn(repl, text, count=1)
    if n != 1:
        sys.exit(f"error: expected exactly one match in {path}, found {n}")
    return new


def _stamp_bmod(text: str, version: str, path: Path) -> str:
    """``text`` with the ``[bmod]`` table's own ``version`` set to ``version``."""
    table = _BMOD_TABLE_PAT.search(text)
    if table is None:
        sys.exit(f"error: no [bmod] table in {path}")
    body = _sub_once(_BMOD_VERSION_PAT, rf"\g<1>{version}\g<2>", table.group(1), path)
    return text[: table.start(1)] + body + text[table.end(1) :]


def _bmod_version(text: str) -> str | None:
    """The ``[bmod] version`` of a bmod.toml, parsed as TOML; ``None`` when absent.

    Raises ``tomllib.TOMLDecodeError`` on a file that is not TOML at all.
    """
    table = tomllib.loads(text).get("bmod")
    if not isinstance(table, dict):
        return None
    version = table.get("version")
    return version if isinstance(version, str) else None


def stamp(version: str) -> None:
    if not _VERSION_RE.match(version):
        sys.exit(f"error: {version!r} is not a SemVer X.Y.Z[-pre] version (no `-dev`)")

    INIT.write_text(
        _sub_once(_INIT_PAT, rf"\g<1>{version}\g<2>", INIT.read_text(encoding="utf-8"), INIT),
        encoding="utf-8",
        newline="\n",
    )
    PYPROJECT.write_text(
        _sub_once(
            _PYPROJECT_PAT,
            rf"\g<1>{version}\g<2>",
            PYPROJECT.read_text(encoding="utf-8"),
            PYPROJECT,
        ),
        encoding="utf-8",
        newline="\n",
    )
    CANONICAL_MODULE_YAML.write_text(
        _sub_once(
            _MODULE_VERSION_PAT,
            rf"\g<1>{version}",
            CANONICAL_MODULE_YAML.read_text(encoding="utf-8"),
            CANONICAL_MODULE_YAML,
        ),
        encoding="utf-8",
        newline="\n",
    )

    market_text = MARKETPLACE.read_text(encoding="utf-8")
    market_new, n = _MARKET_PAT.subn(rf"\g<1>{version}\g<2>", market_text)
    if n == 0:
        sys.exit(f"error: found no version field in {MARKETPLACE}")
    MARKETPLACE.write_text(market_new, encoding="utf-8", newline="\n")
    BMOD_TOML.write_text(
        _stamp_bmod(BMOD_TOML.read_text(encoding="utf-8"), version, BMOD_TOML),
        encoding="utf-8",
        newline="\n",
    )

    # Regenerate the installer-discoverable repo-root mirror from the canonical copy.
    shutil.copyfile(CANONICAL_MODULE_YAML, ROOT_MODULE_YAML)

    print(f"stamped version {version} across all files + regenerated {ROOT_MODULE_YAML.name}")
    _relock()


def _relock() -> None:
    """Refresh uv.lock so the pinned project version tracks the bump. CI runs
    `uv sync --locked`, which fails the install step on a stale lock — so this is
    part of the stamp, not an optional follow-up. Loud non-zero exit (the files
    are already stamped; re-running is idempotent) beats a silent drift."""
    try:
        subprocess.run(["uv", "lock"], cwd=ROOT, check=True)
    except FileNotFoundError:
        sys.exit("error: `uv` not found — run `uv lock` manually before committing")
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: `uv lock` failed (exit {e.returncode}) — fix and commit uv.lock")
    print("regenerated uv.lock")


def _field(pat: re.Pattern[str], text: str, group_split: str = '"') -> str | None:
    m = pat.search(text)
    if not m:
        return None
    return m.group(0).split(group_split)[1] if group_split else m.group(0)


def check() -> int:
    canonical = read_canonical()
    problems: list[str] = []

    py = _field(_PYPROJECT_PAT, PYPROJECT.read_text(encoding="utf-8"))
    if py != canonical:
        problems.append(f"pyproject.toml [project].version = {py!r} (expected {canonical!r})")

    mv = _MODULE_VERSION_PAT.search(CANONICAL_MODULE_YAML.read_text(encoding="utf-8"))
    mv_val = mv.group(0).split(":", 1)[1].strip() if mv else None
    if mv_val != canonical:
        problems.append(f"module.yaml module_version = {mv_val!r} (expected {canonical!r})")

    market = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    # Refuse to pass vacuously. `market.get("plugins", [])` iterates zero times when the
    # key is renamed, dropped or emptied, so the loop below would report agreement it
    # never checked — green on exactly the corruption this gate exists to catch. Same
    # shape the tracked-files preflight in .github/workflows/ci.yml already guards.
    plugins = market.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        problems.append(
            f"marketplace.json has no non-empty `plugins` list (got {plugins!r}) — "
            "this check would pass vacuously"
        )
        plugins = []
    for i, plugin in enumerate(plugins):
        if not isinstance(plugin, dict):
            problems.append(f"marketplace.json plugins[{i}] is not an object (got {plugin!r})")
            continue
        if plugin.get("version") != canonical:
            problems.append(
                f"marketplace.json plugins[{i}].version = {plugin.get('version')!r} "
                f"(expected {canonical!r})"
            )

    try:
        bmod_version = _bmod_version(BMOD_TOML.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        problems.append(f"{BMOD_TOML.name} unreadable ({e})")
    else:
        if bmod_version != canonical:
            problems.append(f"bmod.toml [bmod].version = {bmod_version!r} (expected {canonical!r})")

    if not ROOT_MODULE_YAML.exists():
        problems.append(f"missing repo-root mirror {ROOT_MODULE_YAML} (run sync_version.py)")
    elif ROOT_MODULE_YAML.read_bytes() != CANONICAL_MODULE_YAML.read_bytes():
        problems.append(
            f"{ROOT_MODULE_YAML.name} differs from canonical "
            f"{CANONICAL_MODULE_YAML.relative_to(ROOT)} (run sync_version.py to regenerate)"
        )

    if problems:
        print(f"version drift detected (canonical __version__ = {canonical}):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"ok: every version field agrees on {canonical} and the root mirror matches")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.exit("usage: sync_version.py <X.Y.Z> | --check")
    if argv[0] == "--check":
        return check()
    stamp(argv[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
