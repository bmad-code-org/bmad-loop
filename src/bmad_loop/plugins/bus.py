"""HookBus: dispatch lifecycle stages to plugin hooks (declarative + python).

The bus is the single fan-out point between the engine's stages and the loaded
plugins. It enforces the two invariants that keep plugins safe:

  * **No-op fast path.** ``active(stage)`` is an O(1) set membership test
    precomputed at build time. A run with no plugin bound to a stage never
    builds a context or calls ``emit`` — zero-plugin runs stay byte-identical.
  * **Failure isolation.** Every hook — subprocess or in-process Python — is
    wrapped. A python hook that raises is caught (``except Exception`` only;
    ``RunStopped``/SIGTERM as ``BaseException`` propagate), journalled, and the
    offending instance disabled for the rest of the run. A declarative hook that
    errors (timeout, bad interpreter) fails open by default (the run survives);
    ``fail_closed`` turns an error into a defer veto.

Dispatch order is registry order (manifest ``priority`` then load order).
Mutations pipeline — a later plugin sees an earlier plugin's edits. Vetoes are
collected without short-circuit so order can never hide a severer objection;
the context resolves the most-conservative one.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable

from .context import MUTABLE_FIELDS, VETO_ACTIONS, HookContext, Veto
from .model import LoadedPlugin
from .registry import PluginRegistry

# (returncode, combined-output) — the declarative-hook transport. Injectable so
# tests can drive the bus without spawning real subprocesses.
HookRunner = Callable[..., "tuple[int, str]"]


class _HookError(Exception):
    """A declarative hook could not run to completion (timeout / launch failure),
    as distinct from a clean non-zero exit. Decides fail-open vs fail-closed."""


def _run_subprocess(
    cmd: str, *, cwd: str | None, env: dict[str, str], timeout: int
) -> tuple[int, str]:
    """Default declarative-hook transport: a shell command with the plugin env,
    capturing output. shell=True is intentional (mirrors the deterministic verify
    commands + the legacy engine ``*_cmd`` hooks)."""
    try:
        proc = subprocess.run(  # nosec B602 - operator-authored plugin command
            cmd,
            shell=True,  # portability: operator-authored plugin command — sanctioned shell-out (see plan out-of-scope)
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise _HookError(f"timed out after {timeout}s") from e
    except OSError as e:
        raise _HookError(str(e)) from e
    return proc.returncode, proc.stdout + proc.stderr


def _instance_stages(instance: Any) -> set[str]:
    """The stages an in-process plugin handles: every ``on_<stage>`` method it
    defines. Lets ``active()`` stay precise — a python plugin only marks the
    stages it actually implements, so the fast path holds for the rest."""
    return {
        name[3:]
        for name in dir(instance)
        if name.startswith("on_") and callable(getattr(instance, name, None))
    }


def _hook_env(ctx: HookContext, lp: LoadedPlugin) -> dict[str, str]:
    """The ``BMAD_LOOP_*`` environment a declarative hook reads — the run's
    identity fields plus the plugin's resolved settings as ``BMAD_LOOP_SETTING_*``.
    Generalizes ``engine._run_engine_hook``'s env block to any plugin/stage."""
    env = dict(os.environ)
    fields = {
        "BMAD_LOOP_STAGE": ctx.stage,
        "BMAD_LOOP_RUN_ID": ctx.run_id,
        "BMAD_LOOP_RUN_DIR": ctx.run_dir or "",
        "BMAD_LOOP_REPO_ROOT": ctx.repo_root or "",
        "BMAD_LOOP_WORKTREE": ctx.worktree or ctx.repo_root or "",
        "BMAD_LOOP_STORY_KEY": ctx.story_key or "",
        "BMAD_LOOP_ROLE": ctx.role or "",
        "BMAD_LOOP_PHASE": ctx.phase or "",
        "BMAD_LOOP_BRANCH": ctx.branch or "",
        "BMAD_LOOP_AGENTS": ",".join(ctx.agents),
        "BMAD_LOOP_PLUGIN": lp.name,
    }
    env.update({k: v for k, v in fields.items() if v != ""})
    # the plugin's *resolved* settings (defaults overlaid by [plugins.<name>]),
    # not bare manifest defaults, so a declarative hook sees the operator's config.
    for key, value in lp.settings.items():
        env[f"BMAD_LOOP_SETTING_{key.upper()}"] = str(value)
    return env


def _last_json(output: str) -> dict[str, Any] | None:
    """A declarative hook may emit a JSON object on its last non-empty stdout
    line to mutate the context. Anything else is treated as plain advisory log
    text. Parse failures are ignored (the hook still ran)."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                return None
            return payload if isinstance(payload, dict) else None
        return None
    return None


class HookBus:
    """Fan stage emits out to every loaded plugin. Build once per run from the
    registry; share with the engine."""

    def __init__(
        self,
        registry: PluginRegistry,
        journal: Any = None,
        *,
        runner: HookRunner | None = None,
    ):
        self._registry = registry
        self._journal = journal
        self._runner = runner or _run_subprocess
        # plugins disabled mid-run after an in-process hook raised (failure
        # isolation): skipped for every subsequent stage.
        self._disabled: set[str] = set()
        # precompute the active-stage set for the O(1) fast path.
        self._active: set[str] = set()
        self._py_stages: dict[str, set[str]] = {}
        for lp in registry.plugins():
            for hook in lp.manifest.hooks:
                self._active.add(hook.stage)
            if lp.instance is not None:
                stages = _instance_stages(lp.instance)
                if stages:
                    self._py_stages[lp.name] = stages
                    self._active.update(stages)

    # ----------------------------------------------------------- fast path

    def active(self, stage: str) -> bool:
        """True iff some loaded plugin binds ``stage``. The engine guards every
        emit with this so a zero-plugin run does no work."""
        return stage in self._active

    def any_active(self) -> bool:
        return bool(self._active)

    def active_plugins(self) -> list[str]:
        """Names of plugins that bind at least one stage — for a one-line
        run-start journal entry when (and only when) plugins are live."""
        out: list[str] = []
        for lp in self._registry.plugins():
            if lp.manifest.hooks or lp.name in self._py_stages:
                out.append(lp.name)
        return out

    # -------------------------------------------------------------- dispatch

    def emit(self, stage: str, ctx: HookContext) -> HookContext:
        """Dispatch ``stage`` to every plugin bound to it, in registry order.
        Returns the same context (carrying mutations + collected vetoes). A no-op
        when no plugin binds the stage."""
        if stage not in self._active:
            return ctx
        for lp in self._registry.plugins():
            if lp.name in self._disabled:
                continue
            hook = lp.manifest.hook_for(stage)
            if hook is not None:
                self._dispatch_declarative(lp, hook, ctx)
            if lp.instance is not None and stage in self._py_stages.get(lp.name, ()):
                self._dispatch_python(lp, stage, ctx)
        return ctx

    def _dispatch_declarative(self, lp: LoadedPlugin, hook: Any, ctx: HookContext) -> None:
        cmd = lp.manifest.render(hook.cmd)
        env = _hook_env(ctx, lp)
        cwd = ctx.worktree or ctx.repo_root or None
        try:
            rc, output = self._runner(cmd, cwd=cwd, env=env, timeout=hook.timeout_sec)
        except _HookError as e:
            self._log("plugin-hook-error", plugin=lp.name, stage=hook.stage, error=str(e))
            if hook.blocking and hook.fail_closed:
                ctx.add_veto(
                    Veto("defer", f"plugin {lp.name!r} hook {hook.stage} errored: {e}", lp.name)
                )
            return
        explicit_veto = self._apply_stdout(lp, ctx, output)
        if not hook.blocking:
            self._log("plugin-hook", plugin=lp.name, stage=hook.stage, rc=rc)
            return
        # blocking hook: a non-zero exit vetoes (defer) unless the hook already
        # asked for a specific action via its stdout JSON.
        if rc != 0 and not explicit_veto:
            tail = output.strip()[-500:]
            ctx.add_veto(
                Veto("defer", f"plugin {lp.name!r} hook {hook.stage} exited {rc}: {tail}", lp.name)
            )
        self._log("plugin-hook", plugin=lp.name, stage=hook.stage, rc=rc, blocking=True)

    def _apply_stdout(self, lp: LoadedPlugin, ctx: HookContext, output: str) -> bool:
        """Merge a declarative hook's optional stdout-JSON: ``shared`` updates,
        whitelisted ``mutate`` fields, and an explicit ``veto``. Returns whether
        an explicit veto was supplied (so the exit-code path doesn't double-veto)."""
        payload = _last_json(output)
        if not payload:
            return False
        shared = payload.get("shared")
        if isinstance(shared, dict):
            ctx.shared.update(shared)
        mutate = payload.get("mutate")
        if isinstance(mutate, dict):
            for key, value in mutate.items():
                if key in MUTABLE_FIELDS:
                    setattr(ctx, key, value)
        veto = payload.get("veto")
        if isinstance(veto, dict) and veto.get("action") in VETO_ACTIONS:
            ctx.add_veto(Veto(veto["action"], str(veto.get("reason", "")), lp.name))
            return True
        return False

    def _dispatch_python(self, lp: LoadedPlugin, stage: str, ctx: HookContext) -> None:
        instance = lp.instance
        ctx._current_plugin = lp.name
        try:
            instance.hook(stage, ctx)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001 - isolate plugin failures; never BaseException
            self._log("plugin-error", plugin=lp.name, stage=stage, error=f"{type(e).__name__}: {e}")
            # disable the misbehaving instance for the rest of the run; its
            # declarative hooks (if any) keep working — they are out-of-process.
            self._disabled.add(lp.name)
            if getattr(instance, "fail_closed", False):
                ctx.add_veto(Veto("defer", f"plugin {lp.name!r} hook {stage} raised: {e}", lp.name))
        finally:
            ctx._current_plugin = ""

    def _log(self, kind: str, **fields: Any) -> None:
        if self._journal is not None:
            self._journal.append(kind, **fields)
