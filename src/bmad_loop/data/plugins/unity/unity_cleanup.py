#!/usr/bin/env python3
"""Post-run scratch cleanup for the bmad-loop Unity engine plugin.

The IvanMurzak ``unity-mcp-cli`` downloads the Editor MCP server into Unity's
per-project temp dir — on Linux ``/tmp/<companyName>/<productName>/`` — as a
versioned ``unity-mcp-server-*.zip`` and never removes it, so a fresh zip
accumulates every time the pinned server version changes. The Editor also writes
an unbounded ``<project>/Temp/mcp-server/ai-editor-logs.txt``. Neither is needed
once a run is over; this script reclaims both, best-effort, at ``post_run``.

It runs once per run (both editor modes), after the loop has finished — so it
never races an in-flight ``setup-mcp`` download. Gated by ``BMAD_LOOP_CLEAN_TMP``
(the engine maps [cleanup] clean_tmp onto it); a "0" disables it.

Env (injected by the engine):
  BMAD_LOOP_WORKTREE     the Unity project path (the repo root in shared mode)
  BMAD_LOOP_REPO_ROOT    main repo root (fallback for the project path)
  BMAD_LOOP_CLEAN_TMP    "1" (default) enables; "0" disables
  BMAD_LOOP_UNITY_LOG_CAP_MB  truncate the mcp log once it exceeds this (default 5)

Exit 0 always (best-effort): a cleanup failure must never fail a finished run.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# companyName/productName are flat scalars in the otherwise-YAML asset.
_COMPANY_RE = re.compile(r"^\s*companyName:\s*(.+?)\s*$", re.MULTILINE)
_PRODUCT_RE = re.compile(r"^\s*productName:\s*(.+?)\s*$", re.MULTILINE)


def _project() -> Path | None:
    p = os.environ.get("BMAD_LOOP_WORKTREE") or os.environ.get("BMAD_LOOP_REPO_ROOT")
    return Path(p) if p else None


def read_project_identity(project: Path) -> tuple[str, str]:
    """``(companyName, productName)`` from ProjectSettings/ProjectSettings.asset.
    Falls back to ``("DefaultCompany", <dir name>)`` — Unity's own default
    companyName — when the asset is missing or a field is unset."""
    default = ("DefaultCompany", project.name)
    asset = project / "ProjectSettings" / "ProjectSettings.asset"
    try:
        text = asset.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default
    company = _COMPANY_RE.search(text)
    product = _PRODUCT_RE.search(text)
    return (
        company.group(1) if company and company.group(1) else default[0],
        product.group(1) if product and product.group(1) else default[1],
    )


def _temp_cache_root() -> Path:
    """Base of Unity's ``Application.temporaryCachePath`` (where unity-mcp-cli drops
    the server zip), under which we look for ``<company>/<product>``.

    On Linux this is exactly ``/tmp`` — keep it byte-for-byte (it *is* Unity's
    documented temporaryCachePath base, not our scratch choice). On other platforms
    (no /tmp) derive from ``tempfile.gettempdir()`` as a best-effort analogue.
    NOTE: native Windows Unity uses a *different* cache root —
    ``%USERPROFILE%\\AppData\\Local\\Temp\\<company>\\<product>`` — which
    ``gettempdir()`` does not always resolve to; getting that exactly right is a
    documented follow-up (no Windows backend ships this pass, so it isn't exercised)."""
    if sys.platform.startswith("linux"):
        return Path("/tmp")  # nosec B108 portability: Unity temporaryCachePath base (Linux)
    return Path(tempfile.gettempdir())  # portability: non-Linux temp root (see note above)


def _clean_tmp_zips(company: str, product: str) -> int:
    """Remove the downloaded unity-mcp-server zips under the temp cache dir
    ``<temp>/<company>/<product>``. Returns the number removed. Scoped to this
    project's temp dir so a sibling project's downloads are never touched."""
    # This is unity-mcp-cli's fixed download location, not our scratch choice.
    tmp = _temp_cache_root() / company / product
    if not tmp.is_dir():
        return 0
    removed = 0
    for zip_path in tmp.glob("unity-mcp-server-*.zip"):
        try:
            zip_path.unlink()
            removed += 1
            print(f"unity_cleanup: removed {zip_path}", file=sys.stderr)
        except OSError as exc:  # best effort
            print(f"unity_cleanup: could not remove {zip_path}: {exc}", file=sys.stderr)
    return removed


def _truncate_mcp_log(project: Path) -> None:
    """Reset the MCP editor log once it grows past the cap. Safe even while the
    Editor/server holds it open — truncation just resets the size."""
    try:
        cap_mb = max(0, int(os.environ.get("BMAD_LOOP_UNITY_LOG_CAP_MB", "5")))
    except ValueError:
        cap_mb = 5
    log = project / "Temp" / "mcp-server" / "ai-editor-logs.txt"
    try:
        if log.is_file() and log.stat().st_size > cap_mb * 1_048_576:
            log.write_bytes(b"")
            print(f"unity_cleanup: truncated {log}", file=sys.stderr)
    except OSError as exc:  # best effort
        print(f"unity_cleanup: could not truncate {log}: {exc}", file=sys.stderr)


def main() -> int:
    if os.environ.get("BMAD_LOOP_CLEAN_TMP", "1").strip() not in ("1", "true", "yes", "on", ""):
        return 0
    project = _project()
    if project is None:
        print("unity_cleanup: no project path (BMAD_LOOP_WORKTREE/REPO_ROOT)", file=sys.stderr)
        return 0
    company, product = read_project_identity(project)
    _clean_tmp_zips(company, product)
    _truncate_mcp_log(project)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
