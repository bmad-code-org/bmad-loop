"""Windows tmux backend for the terminal-multiplexer seam.

This is an interim native-Windows transport for the tmux-windows port. It keeps
the generic adapter on the existing tmux contract while quarantining the
Windows-specific behavior that differs from POSIX tmux:

* tmux-windows rejects drive-qualified ``-c C:\\...`` paths, so tmux is spawned
  from the requested cwd and receives ``-c .``.
* tmux-windows panes do not reliably inherit the parent PATH, so agent windows
  explicitly receive the Windows environment needed to resolve CLI binaries.
* parked windows use PowerShell syntax instead of POSIX sh.
* pane logging is disabled because tmux-windows can terminate the server when
  ``pipe-pane`` spawns a Windows command.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path, PureWindowsPath

from .tmux_base import TMUX_TIMEOUT_S, BaseTmuxBackend, TmuxError

_WINDOWS_ENV_NAMES = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
}


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell_exe() -> str:
    system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _is_absolute_path(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _merge_env(base: dict[str, str], override: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    actual_key_by_upper: dict[str, str] = {}

    for source in (base, override):
        for key, value in source.items():
            if not value:
                continue
            upper = key.upper()
            old_key = actual_key_by_upper.get(upper)
            if old_key is not None and old_key != key:
                merged.pop(old_key, None)
            merged[key] = value
            actual_key_by_upper[upper] = key
    return merged


class WindowsTmuxMultiplexer(BaseTmuxBackend):
    """tmux-windows backend.

    The tmux argv contract remains inherited from :class:`BaseTmuxBackend`; this
    leaf only adapts the spawn cwd, environment injection, and shell dialect.
    """

    _ENCODING = "utf-8"

    def _inherited_env(self) -> dict[str, str]:
        inherited = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _WINDOWS_ENV_NAMES and value
        }
        if not any(key.upper() == "SYSTEMROOT" for key in inherited):
            inherited["SystemRoot"] = r"C:\Windows"
        if not any(key.upper() == "COMSPEC" for key in inherited):
            system_root = next(
                value for key, value in inherited.items() if key.upper() == "SYSTEMROOT"
            )
            inherited["ComSpec"] = str(Path(system_root) / "System32" / "cmd.exe")
        return inherited

    def _normalize_tmux_argv(self, argv: list[str]) -> tuple[list[str], Path | None]:
        rewritten = list(argv)
        run_cwd: Path | None = None
        for index, value in enumerate(rewritten[:-1]):
            if value != "-c":
                continue
            raw_cwd = rewritten[index + 1]
            if _is_absolute_path(raw_cwd):
                run_cwd = Path(raw_cwd)
                rewritten[index + 1] = "."
        return rewritten, run_cwd

    def _run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        run_argv, run_cwd = self._normalize_tmux_argv(argv)
        proc = subprocess.run(
            ["tmux", *run_argv],
            capture_output=True,
            text=True,
            encoding=self._ENCODING,
            env=env,
            cwd=str(run_cwd) if run_cwd is not None else None,
            timeout=TMUX_TIMEOUT_S,
        )
        if check and proc.returncode != 0:
            raise TmuxError(f"tmux {' '.join(argv[:2])} failed: {proc.stderr.strip()}")
        return proc

    def _join_argv(self, argv: list[str]) -> str:
        return subprocess.list2cmdline(argv)

    def _source_prefix(self) -> str:
        assignments = [
            f"$env:{key} = {_ps_quote(value)}; "
            for key, value in self._inherited_env().items()
        ]
        return "".join(assignments)

    _EXIT_CAPTURE = (
        "$ec = if ($global:LASTEXITCODE -ne $null) { $global:LASTEXITCODE } "
        "else { if ($?) { 0 } else { 1 } }"
    )
    _ECHO = "Write-Host"
    _PARK = "[void][Console]::ReadLine()"

    def _shell_wrap(self, source: str) -> list[str]:
        return [_powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", source]

    def _parked_trailer(self, return_opt: str) -> str:
        quoted_return_opt = _ps_quote(return_opt)
        detach = _ps_quote("detach")
        return (
            f"$ret = (& tmux show-options -wqv {quoted_return_opt} 2>$null); "
            f"if ($ret -eq {detach}) {{ & tmux detach-client 2>$null }} "
            "elseif ($ret) { "
            "& tmux switch-client -t $ret 2>$null; "
            "if ($global:LASTEXITCODE -ne 0) { & tmux switch-client -l 2>$null } "
            "}"
        )

    def _window_launch(self, env: dict[str, str], command: str) -> list[str]:
        launch_env = _merge_env(self._inherited_env(), env)
        env_args: list[str] = []
        for key, value in launch_env.items():
            env_args += ["-e", f"{key}={value}"]
        return [*env_args, command]

    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        # tmux-windows 3.6a can exit the server when pipe-pane spawns either a
        # PowerShell or cmd.exe pipe command. A missing pane log is less damaging
        # than taking down the live agent session; hooks/window liveness still
        # drive completion.
        return None

    def attach_target_argv(self, target: str) -> list[str]:
        if os.environ.get("TMUX"):
            return ["tmux", "switch-client", "-t", target]
        return ["tmux", "attach", "-t", target]
