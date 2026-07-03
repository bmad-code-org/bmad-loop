#!/usr/bin/env python3
"""per_worktree teardown for the bmad-loop Unity engine plugin.

Runs once per unit when the worktree is finished — on success, on a deferral, and
on a pause/escalation — so the unit's managed Unity Editor never outlives its
worktree. Best effort: the engine logs a non-zero exit but the unit's outcome
stands (it does not re-defer a done/paused unit just because Editor-quit failed).

  1. Gracefully quit the worktree's Editor (`unity-mcp-cli close`, then --force).
  2. Fallback hard-kill: if an Editor *or its MCP server* whose argv references this
     worktree is still alive afterwards, SIGTERM→SIGKILL it (Linux). ``close``
     reports success even when it can't find the Editor — which happens precisely
     when readiness failed and the Editor never registered with the MCP — so without
     this a failed unit would leak a live Editor. The Unity plugin also spawns a
     child ``gamedev-mcp-server`` (the local MCP HTTP server) that ``close`` does
     NOT reap; a leaked server holds its port and poisons later runs (the plugin
     declines to start a fresh server when a stale one lingers in the name-keyed
     Library cache), so we sweep it up here too.
  3. Drop the ``<worktree>/Library`` if setup left a *symlink* (the empty-cache
     fallback), leaving the persistent cache it pointed at intact for the next run.
     A real Library — the common case now that setup *primes* a warm reflink/CoW copy
     in — is left untouched; it is removed cheaply when bmad-loop deletes the worktree
     (CoW-shared extents cost almost nothing to drop).

Verified against unity-mcp-cli v0.81.1 (`close <path>` keys off the project path).
Only the IvanMurzak MCP launches a managed per-worktree Editor, so only it is quit
here; for CoplayDev (shared :8080 server) override engine.worktree_teardown_cmd.

Env (injected by the engine):
  BMAD_LOOP_WORKTREE     the worktree whose Editor to quit
  BMAD_LOOP_ENGINE_MCP   ivanmurzak | coplaydev               (default ivanmurzak)
  UNITY_MCP_CLI          IvanMurzak CLI binary                (default unity-mcp-cli)
  BMAD_LOOP_UNITY_CLOSE_TIMEOUT  polite-quit seconds before --force (default 30)

Exit 0 = Editor quit + symlink dropped; non-zero = something failed (logged).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from bmad_loop.process_host import get_process_host


def _psutil():
    """Lazily import psutil (the optional ``non-linux`` extra), used only for
    non-Linux process discovery (macOS and Windows). The dep-free core never imports
    it; raise a clear, actionable error if it's missing on a platform that needs it."""
    try:
        import psutil  # noqa: PLC0415  (intentional lazy import — keeps the core dep-free)
    except ImportError as exc:  # pragma: no cover - exercised only off Linux
        raise RuntimeError(
            f"unity_teardown: process discovery on {sys.platform!r} needs psutil; "
            "install the optional extra (pip install 'bmad-loop[non-linux]') or run "
            "under Linux/WSL"
        ) from exc
    return psutil


def _worktree() -> Path | None:
    wt = os.environ.get("BMAD_LOOP_WORKTREE")
    return Path(wt) if wt else None


# Process basenames we reap when bound to the worktree path: the Unity Editor
# binary and the local MCP HTTP server the plugin spawns as a child.
_TARGET_BASENAMES = ("unity", "gamedev-mcp-server")


def _exe_basename(entry: Path) -> str:
    try:
        return os.path.basename(os.readlink(entry / "exe")).lower()
    except OSError:
        return ""


def _lingering_pids(worktree: Path) -> list[int]:
    """PIDs of the Unity *Editor* or its *MCP server* bound to this worktree.

    Tight on purpose: the process must (a) reference this exact worktree path in
    argv — Unity gets ``-projectPath <path>`` and the server's binary lives under
    ``<worktree>/Library/mcp-server/`` — and (b) have an executable basename of
    exactly ``unity`` (the Editor) or ``gamedev-mcp-server`` (the MCP server). That
    excludes the launcher shell, ``unity-mcp-cli``/node, python, greps, and the
    operator's Editor/server on any other project, so we never kill the wrong one.

    Linux uses the zero-dependency ``/proc`` fast path; other platforms (no /proc)
    fall back to the same scan over psutil (the optional ``non-linux`` extra)."""
    if sys.platform.startswith("linux"):
        return _lingering_pids_proc(worktree)
    return _lingering_pids_psutil(worktree)


def _lingering_pids_proc(worktree: Path) -> list[int]:
    """Linux fast path: scan ``/proc`` for the worktree-bound Editor/server."""
    needle = str(worktree)
    pids: list[int] = []
    for entry in Path("/proc").iterdir():  # portability: Linux-only /proc scan, guarded above
        if not entry.name.isdigit():
            continue
        try:
            argv = (
                (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
            )
        except OSError:
            continue  # process gone or unreadable
        if needle not in argv:
            continue
        argv0 = os.path.basename(argv.split(" ", 1)[0]).lower() if argv.strip() else ""
        if _exe_basename(entry) in _TARGET_BASENAMES or argv0 in _TARGET_BASENAMES:
            pids.append(int(entry.name))
    return pids


def _lingering_pids_psutil(worktree: Path) -> list[int]:
    """Non-Linux equivalent of the ``/proc`` scan via psutil (no /proc available).
    Same tight match — argv references this worktree AND the process basename is a
    target — with the basename's extension stripped so Windows' ``Unity.exe`` /
    ``gamedev-mcp-server.exe`` match ``_TARGET_BASENAMES``. Not exercised on Linux."""
    psutil = _psutil()
    needle = str(worktree)
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if needle not in " ".join(cmdline):
                continue
            exe = proc.info.get("exe") or ""
            name = proc.info.get("name") or ""
            argv0 = os.path.basename(cmdline[0]).lower() if cmdline else ""
            exe_base = os.path.splitext(os.path.basename(exe))[0].lower() if exe else ""
            name_base = os.path.splitext(name)[0].lower()
            if (
                exe_base in _TARGET_BASENAMES
                or argv0 in _TARGET_BASENAMES
                or name_base in _TARGET_BASENAMES
            ):
                pids.append(int(proc.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue  # process vanished or unreadable mid-scan
    return pids


def _force_kill_lingering(worktree: Path) -> int:
    """Best-effort SIGTERM→SIGKILL of any Editor or MCP server left running for this
    worktree after ``close``. Returns the number of processes targeted."""
    pids = _lingering_pids(worktree)
    # exclude ourselves just in case (our own argv has the worktree path too)
    pids = [p for p in pids if p != os.getpid()]
    if not pids:
        return 0
    print(
        f"unity_teardown: 'close' left {len(pids)} Unity process(es) for {worktree} "
        f"running ({pids}); hard-killing",
        file=sys.stderr,
    )
    host = get_process_host()
    # Snapshot identity before signalling so the force-kill escalation can never land
    # on a pid the kernel recycled to an unrelated process (mirrors runs.stop_run's
    # guard; force_kill's own contract requires confirmed identity).
    ident = {pid: host.identity(pid) for pid in pids}
    for pid in pids:
        try:
            host.terminate(pid)
        except OSError:
            pass
    # give them a few seconds to exit politely, then hard-kill survivors
    for _ in range(20):
        if not any(host.is_alive(p) for p in pids):
            break
        time.sleep(0.5)
    for pid in pids:
        if host.is_alive(pid) and ident[pid] is not None and host.identity(pid) == ident[pid]:
            try:
                host.force_kill(pid)
            except OSError:
                pass
    return len(pids)


def _cli() -> str:
    return os.environ.get("UNITY_MCP_CLI", "unity-mcp-cli")


def _drop_library_symlink(worktree: Path) -> None:
    link = worktree / "Library"
    if link.is_symlink():
        try:
            link.unlink()
            print("unity_teardown: dropped Library symlink", file=sys.stderr)
        except OSError as exc:  # best effort
            print(f"unity_teardown: could not drop Library symlink: {exc}", file=sys.stderr)


def _close_ivanmurzak(worktree: Path) -> int:
    cli = _cli()
    if shutil.which(cli) is None:
        # nothing to quit against; not fatal — just drop the symlink below.
        print(f"unity_teardown: {cli!r} not on PATH; skipping Editor quit", file=sys.stderr)
        return 0
    timeout = os.environ.get("BMAD_LOOP_UNITY_CLOSE_TIMEOUT", "30")
    proc = subprocess.run(
        [cli, "close", str(worktree), "--timeout", timeout, "--force"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        print(f"unity_teardown: 'close' exited {proc.returncode}", file=sys.stderr)
    return proc.returncode


def main() -> int:
    worktree = _worktree()
    if worktree is None:
        print("unity_teardown: BMAD_LOOP_WORKTREE is not set", file=sys.stderr)
        return 2
    mcp = (os.environ.get("BMAD_LOOP_ENGINE_MCP") or "ivanmurzak").strip().lower()
    rc = 0
    if mcp == "ivanmurzak":
        rc = _close_ivanmurzak(worktree)
    elif mcp == "coplaydev":
        print(
            "unity_teardown: CoplayDev per_worktree teardown is not wired; override "
            "engine.worktree_teardown_cmd if you launched a managed Editor for it.",
            file=sys.stderr,
        )
    else:
        print(
            f"unity_teardown: unknown BMAD_LOOP_ENGINE_MCP={mcp!r} (expected ivanmurzak|coplaydev)",
            file=sys.stderr,
        )
        rc = 2
    # close reports success even when it can't find the Editor (e.g. readiness
    # failed so it never registered) and never reaps the plugin's child MCP server
    # — sweep up any Editor or gamedev-mcp-server still bound to this worktree so a
    # failed unit never leaks a live process (a leaked server poisons later runs).
    # Only a SURVIVING process (couldn't be killed) is a teardown failure; a leak we
    # successfully reaped is still a clean teardown.
    if _force_kill_lingering(worktree):
        survivors = _lingering_pids(worktree)
        if survivors:
            print(
                f"unity_teardown: {len(survivors)} Editor/server process(es) survived kill: "
                f"{survivors}",
                file=sys.stderr,
            )
            rc = rc or 1
    _drop_library_symlink(worktree)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
