"""Shared zero-token availability checks for the OpenCode test suites."""

from __future__ import annotations

import shutil
import subprocess
import sys


def _opencode_runs() -> bool:
    """Whether a usable POSIX ``opencode`` binary is available."""
    if sys.platform == "win32":
        return False
    if (binary := shutil.which("opencode")) is None:
        return False
    try:
        probe = subprocess.run([binary, "--version"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0
