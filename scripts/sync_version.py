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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INIT = ROOT / "src" / "bmad_loop" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
CANONICAL_MODULE_YAML = (
    ROOT / "src" / "bmad_loop" / "data" / "skills" / "bmad-loop-setup" / "assets" / "module.yaml"
)
ROOT_MODULE_YAML = ROOT / "module.yaml"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.-]+)?$")

_INIT_PAT = re.compile(r'(?m)^(__version__\s*=\s*")[^"]*(")')
_PYPROJECT_PAT = re.compile(r'(?m)^(version\s*=\s*")[^"]*(")')
_MODULE_VERSION_PAT = re.compile(r"(?m)^(module_version:\s*)\S+")
# Targeted replace so we touch only the version value and leave marketplace.json
# formatting (compact arrays etc.) for the JSON formatter to own.
_MARKET_PAT = re.compile(r'(?m)^(\s*"version"\s*:\s*")[^"]*(")')


def read_canonical() -> str:
    m = _INIT_PAT.search(INIT.read_text())
    if not m:
        sys.exit(f"error: could not find __version__ in {INIT}")
    return m.group(0).split('"')[1]


def _sub_once(pat: re.Pattern[str], repl: str, text: str, path: Path) -> str:
    new, n = pat.subn(repl, text, count=1)
    if n != 1:
        sys.exit(f"error: expected exactly one match in {path}, found {n}")
    return new


def stamp(version: str) -> None:
    if not _VERSION_RE.match(version):
        sys.exit(f"error: {version!r} is not a valid X.Y.Z version")

    INIT.write_text(_sub_once(_INIT_PAT, rf"\g<1>{version}\g<2>", INIT.read_text(), INIT))
    PYPROJECT.write_text(
        _sub_once(_PYPROJECT_PAT, rf"\g<1>{version}\g<2>", PYPROJECT.read_text(), PYPROJECT)
    )
    CANONICAL_MODULE_YAML.write_text(
        _sub_once(
            _MODULE_VERSION_PAT,
            rf"\g<1>{version}",
            CANONICAL_MODULE_YAML.read_text(),
            CANONICAL_MODULE_YAML,
        )
    )

    market_text = MARKETPLACE.read_text()
    market_new, n = _MARKET_PAT.subn(rf"\g<1>{version}\g<2>", market_text)
    if n == 0:
        sys.exit(f"error: found no version field in {MARKETPLACE}")
    MARKETPLACE.write_text(market_new)

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

    py = _field(_PYPROJECT_PAT, PYPROJECT.read_text())
    if py != canonical:
        problems.append(f"pyproject.toml [project].version = {py!r} (expected {canonical!r})")

    mv = _MODULE_VERSION_PAT.search(CANONICAL_MODULE_YAML.read_text())
    mv_val = mv.group(0).split(":", 1)[1].strip() if mv else None
    if mv_val != canonical:
        problems.append(f"module.yaml module_version = {mv_val!r} (expected {canonical!r})")

    market = json.loads(MARKETPLACE.read_text())
    for i, plugin in enumerate(market.get("plugins", [])):
        if plugin.get("version") != canonical:
            problems.append(
                f"marketplace.json plugins[{i}].version = {plugin.get('version')!r} "
                f"(expected {canonical!r})"
            )

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
