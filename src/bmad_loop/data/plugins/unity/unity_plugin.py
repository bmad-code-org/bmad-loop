"""In-process Unity engine plugin — the proof the framework carries an engine.

Everything that used to be bespoke ``Engine._engine_*`` code now lives here, on
top of the generic plugin framework:

  * the **readiness gate** (``on_pre_ready_gate``) blocks until the Editor + MCP
    report ready before any session runs, in both editor modes;
  * **per_worktree setup/teardown** (``on_pre_worktree_setup`` /
    ``on_pre_worktree_teardown``) launch and reap a managed Editor per worktree;
  * a failure **vetoes (defers)** the unit through the bus — the engine's generic
    ``_vetoed`` routing turns that into a deferral + notification, with no
    Unity-specific branch in the loop;
  * **MCP agent routing** reads ``ctx.agents`` (the dev + review CLIs in the
    worktree) so every agent's MCP config is pointed at the worktree's Editor;
  * **editor_mode↔scm.isolation coupling** is validated in ``validate`` at startup
    (it moved out of core ``policy.loads`` — a flat per-key schema can't express a
    cross-section coupling).

The helper scripts (``unity_ready.py`` / ``unity_setup.py`` / ``unity_teardown.py``)
are unchanged; they read the same ``BMAD_LOOP_*`` environment this module injects,
so the env contract a Unity operator relies on is identical to the engine layer it
replaces.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from bmad_loop.plugins.model import Plugin, PluginError

# editor modes this plugin supports; the operator picks one via [plugins.unity].
EDITOR_MODES = ("shared", "per_worktree")
# best-effort teardown bound so a hung Editor-quit can't stall the loop for the
# full readiness budget on every unit (was Engine._ENGINE_TEARDOWN_TIMEOUT).
_TEARDOWN_TIMEOUT = 120


class UnityPlugin(Plugin):
    """Trust-gated in-process plugin (loads only when "unity" is in
    ``[plugins] enabled``). Its lifecycle hooks gate and manage the Editor."""

    # Whether to let the post-run cleanup reclaim the IvanMurzak MCP server's
    # /tmp scratch + log. Mirrors [cleanup] clean_tmp, captured at validate()
    # since plugin hooks don't otherwise see the run policy. Default True.
    _clean_tmp = True

    def _editor_mode(self) -> str:
        return str(self.settings.get("editor_mode", "shared"))

    # ----------------------------------------------------------- validation

    def validate(self, policy: Any) -> None:
        """editor_mode and scm.isolation are coupled: a live Editor MCP must act
        on the same folder the agent edits. shared = the agent's warm Editor on
        the checkout in place (no worktree); per_worktree = one Editor per
        isolated worktree."""
        self._clean_tmp = bool(getattr(getattr(policy, "cleanup", None), "clean_tmp", True))
        mode = self._editor_mode()
        if mode not in EDITOR_MODES:
            raise PluginError(
                f"plugin 'unity': editor_mode must be one of {sorted(EDITOR_MODES)}: got {mode!r}"
            )
        isolation = getattr(getattr(policy, "scm", None), "isolation", "none")
        if mode == "shared" and isolation != "none":
            raise PluginError(
                "plugin 'unity': editor_mode = 'shared' requires scm.isolation = 'none' "
                f"(the agent works in place on the live Editor's checkout); got "
                f"scm.isolation = {isolation!r}"
            )
        if mode == "per_worktree" and isolation != "worktree":
            raise PluginError(
                "plugin 'unity': editor_mode = 'per_worktree' requires scm.isolation = 'worktree'; "
                f"got scm.isolation = {isolation!r}"
            )

    # --------------------------------------------------------------- hooks

    def on_pre_ready_gate(self, ctx) -> None:
        """Block until the Editor + MCP report ready before a unit runs (both
        modes). A non-zero exit vetoes (defers) the unit."""
        rc, tail = self._run_script("unity_ready.py", ctx, timeout=self._ready_timeout())
        if rc != 0:
            ctx.veto("defer", f"Unity Editor not ready (rc={rc}): {tail}".rstrip())

    def on_pre_worktree_setup(self, ctx) -> None:
        """per_worktree: make the fresh worktree a usable Unity project + launch
        its managed Editor before the agent runs. A failure defers the unit."""
        if self._editor_mode() != "per_worktree":
            return
        rc, tail = self._run_script("unity_setup.py", ctx, timeout=self._ready_timeout())
        if rc != 0:
            ctx.veto("defer", f"Unity worktree setup failed (rc={rc}): {tail}".rstrip())

    def on_pre_worktree_teardown(self, ctx) -> None:
        """per_worktree: quit the unit's managed Editor + undo its setup. Best
        effort — observe-only (teardown stages forbid veto); a failure is left to
        the bus journal, the unit's outcome stands."""
        if self._editor_mode() != "per_worktree":
            return
        self._run_script("unity_teardown.py", ctx, timeout=_TEARDOWN_TIMEOUT)

    def on_post_run(self, ctx) -> None:
        """Run finished cleanly: reclaim the IvanMurzak MCP server's /tmp scratch
        (downloaded server zips) and truncate its unbounded editor log. Best
        effort — observe-only, runs once per run in both editor modes, after the
        loop so it never races an in-flight setup-mcp download. CoplayDev uses a
        shared server with no per-project /tmp download, so it is skipped."""
        if not self._clean_tmp:
            return
        if str(self.settings.get("mcp", "ivanmurzak")) != "ivanmurzak":
            return
        self._run_script("unity_cleanup.py", ctx, timeout=_TEARDOWN_TIMEOUT)

    # -------------------------------------------------------------- helpers

    def _ready_timeout(self) -> int:
        try:
            return max(1, int(self.settings.get("ready_timeout_sec", 600)))
        except (TypeError, ValueError):
            return 600

    def engine_env(self, ctx) -> dict[str, str]:
        """The ``BMAD_LOOP_*`` environment the helper scripts read — identity +
        worktree from the context, the Editor knobs from this plugin's settings,
        and the MCP agent ids from ``ctx.agents`` (dev + review CLIs). Identical
        to the contract the bespoke ``Engine._run_engine_hook`` used to inject."""
        import os

        worktree = ctx.worktree or ctx.repo_root or ""
        env = dict(os.environ)
        env.update(
            {
                "BMAD_LOOP_REPO_ROOT": ctx.repo_root or "",
                "BMAD_LOOP_WORKTREE": worktree,
                "BMAD_LOOP_RUN_DIR": ctx.run_dir or "",
                "BMAD_LOOP_STORY_KEY": ctx.story_key or "",
                "BMAD_LOOP_ENGINE_MCP": str(self.settings.get("mcp", "ivanmurzak")),
                "BMAD_LOOP_ENGINE_EDITOR_MODE": self._editor_mode(),
                "BMAD_LOOP_ENGINE_READY_TIMEOUT": str(self.settings.get("ready_timeout_sec", 600)),
                "BMAD_LOOP_ENGINE_READY_GRACE": str(self.settings.get("ready_grace_sec", -1)),
                "BMAD_LOOP_UNITY_PATH": str(self.settings.get("unity_path", "")),
                "BMAD_LOOP_CLEAN_TMP": "1" if self._clean_tmp else "0",
            }
        )
        # Tell the per_worktree setup which agent MCP configs to point at the
        # worktree's Editor (dev + review may be different CLIs, each with its own
        # config file). Omitted when no real profile is loaded so the script keeps
        # its claude-code default.
        if ctx.agents:
            env["BMAD_LOOP_ENGINE_AGENTS"] = ",".join(ctx.agents)
        return env

    def _run_script(self, name: str, ctx, *, timeout: int) -> tuple[int, str]:
        """Run one helper script with the engine env, returning (rc, output-tail).
        Never raises: a launch failure / timeout maps to a non-zero rc so the
        readiness gate defers rather than crashing the run."""
        script = Path(self.manifest.scripts_dir) / name
        env = self.engine_env(ctx)
        cwd = ctx.worktree or ctx.repo_root or None
        try:
            proc = subprocess.run(  # nosec B603 - operator-enabled engine plugin script
                # Our own interpreter, not a PATH-resolved `python3`: the helper now
                # imports `bmad_loop.process_host`, which must be importable here even
                # under a pipx-style install where PATH `python3` lacks the package.
                [sys.executable, str(script)],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return -1, f"timed out after {timeout}s"
        except OSError as e:
            return -1, str(e)
        return proc.returncode, (proc.stdout + proc.stderr)[-2000:].strip()
