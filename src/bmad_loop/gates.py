"""Gate evaluation and human notification (desktop + ATTENTION file)."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from .policy import Policy

ATTENTION_FILE = "ATTENTION"


def notify(policy: Policy, run_dir: Path, title: str, message: str) -> None:
    if policy.notify.file:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (run_dir / ATTENTION_FILE).open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {title}: {message}\n")
    # portability: notify-send is Linux-only; the shutil.which guard makes this a
    # no-op on macOS/Windows (no desktop notification), so no platform branch needed.
    if policy.notify.desktop and shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", "--app-name=bmad-loop", title, message],
                timeout=10,
                capture_output=True,
            )
        except (subprocess.SubprocessError, OSError):
            pass  # desktop notification is best-effort


def pause_at_epic_boundary(policy: Policy) -> bool:
    return policy.gates.mode in ("per-epic", "per-story-spec-approval")


def pause_after_spec(policy: Policy) -> bool:
    return policy.gates.mode == "per-story-spec-approval"
