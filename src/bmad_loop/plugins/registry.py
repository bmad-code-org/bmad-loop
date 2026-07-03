"""PluginRegistry: the inter-pillar contract.

The registry collapses discovery + trust + in-process resolution into one
read-only object the rest of the system consumes:

  * the settings pillar reads ``settings_schema()``;
  * the hook bus reads ``hooks_for(stage)``;
  * custom orchestration reads ``provided_workflows()``.

Neither consumer reaches into loader internals. Building the registry is the
single place trust is enforced and failure is isolated:

  * a plugin with no ``[python]`` loads as a data-only/declarative LoadedPlugin
    (instance None) — its shell hooks are available to the bus;
  * a ``[python]`` plugin is constructed only if it is in ``[plugins] enabled``
    (``trust.require_enabled`` gates ``exec_module``); otherwise it is recorded
    untrusted and its module is never imported;
  * any exception while importing/constructing a trusted instance is caught
    (``except Exception`` — never ``BaseException``, so RunStopped/SIGTERM
    propagate), journalled, and the instance disabled. The run survives.
"""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

from . import trust
from .loader import load_plugins
from .model import (
    HookSpec,
    LoadedPlugin,
    Plugin,
    PluginManifest,
    SettingSpec,
    WorkflowSpec,
)


def _resolve_settings(manifest: PluginManifest, policy) -> dict:
    """Manifest defaults overlaid by the ``[plugins.<name>]`` policy table. The
    single resolved view the instance is built with and the bus reads for env."""
    resolved = dict(manifest.setting_defaults())
    plugins_pol = getattr(policy, "plugins", None) if policy is not None else None
    overrides = getattr(plugins_pol, "settings", {}).get(manifest.name, {}) if plugins_pol else {}
    resolved.update(overrides)
    return resolved


def _instantiate(manifest: PluginManifest, settings: dict) -> Plugin:
    """Import the plugin's module and construct its Plugin subclass with its
    resolved settings.

    Caller is responsible for the trust gate; this performs the actual
    ``exec_module``. Kept tiny so the registry's try/except wraps exactly the
    import + construct surface.
    """
    module_path = Path(manifest.scripts_dir) / manifest.python.module  # type: ignore[union-attr]
    if not module_path.is_file():
        raise FileNotFoundError(f"plugin module not found: {module_path}")
    spec = importlib.util.spec_from_file_location(f"bmad_loop_plugin_{manifest.name}", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls_name = manifest.python.cls  # type: ignore[union-attr]
    cls = getattr(module, cls_name, None)
    if cls is None:
        raise AttributeError(f"plugin module {manifest.python.module!r} has no {cls_name!r}")
    if not (isinstance(cls, type) and issubclass(cls, Plugin)):
        raise TypeError(f"{cls_name!r} must subclass plugins.Plugin")
    return cls(manifest, settings)


def _resolve(manifest: PluginManifest, policy, journal) -> LoadedPlugin:
    settings = _resolve_settings(manifest, policy)
    if manifest.python is None:
        if journal is not None:
            journal.append("plugin-loaded", plugin=manifest.name, mode="declarative")
        return LoadedPlugin(manifest=manifest, settings=settings)

    if not trust.is_enabled(policy, manifest.name):
        if journal is not None:
            journal.append(
                "plugin-untrusted",
                plugin=manifest.name,
                reason="[python] module requires [plugins] enabled",
            )
        return LoadedPlugin(manifest=manifest, trusted=False, settings=settings)

    try:
        instance = _instantiate(manifest, settings)
    except Exception as e:  # noqa: BLE001 - isolate plugin failures; never BaseException
        if journal is not None:
            journal.append("plugin-error", plugin=manifest.name, error=f"{type(e).__name__}: {e}")
        return LoadedPlugin(manifest=manifest, disabled=True, error=str(e), settings=settings)

    if journal is not None:
        journal.append("plugin-loaded", plugin=manifest.name, mode="python")
    return LoadedPlugin(manifest=manifest, instance=instance, settings=settings)


class PluginRegistry:
    """Read-only view over the loaded plugins. Build once per run."""

    def __init__(self, loaded: list[LoadedPlugin]):
        # stable order: manifest priority then load (discovery/overlay) order.
        self._loaded = sorted(loaded, key=lambda lp: lp.manifest.priority)

    @classmethod
    def build(cls, project: Path | None = None, policy=None, journal=None) -> PluginRegistry:
        manifests = load_plugins(project, journal=journal)
        loaded = [_resolve(m, policy, journal) for m in manifests.values()]
        return cls(loaded)

    # ----------------------------------------------------------- consumers

    def plugins(self) -> list[LoadedPlugin]:
        return list(self._loaded)

    def get(self, name: str) -> LoadedPlugin | None:
        for lp in self._loaded:
            if lp.manifest.name == name:
                return lp
        return None

    def hooks_for(self, stage: str) -> list[tuple[LoadedPlugin, HookSpec]]:
        """Every (plugin, hook) bound to ``stage``, in registry order. A
        disabled instance still contributes its *declarative* hooks (those are
        out-of-process and independent of the in-process module that failed)."""
        out: list[tuple[LoadedPlugin, HookSpec]] = []
        for lp in self._loaded:
            hook = lp.manifest.hook_for(stage)
            if hook is not None:
                out.append((lp, hook))
        return out

    def settings_schema(self) -> list[tuple[str, tuple[SettingSpec, ...]]]:
        """(plugin name, setting specs) for every plugin that contributes
        settings, in registry order. Consumed by the settings pillar."""
        return [
            (lp.manifest.name, lp.manifest.settings) for lp in self._loaded if lp.manifest.settings
        ]

    def provided_workflows(self) -> dict[str, tuple[str, ...]]:
        """plugin name -> declared workflow names (for introspection / docs)."""
        return {
            lp.manifest.name: tuple(w.name for w in lp.manifest.workflows)
            for lp in self._loaded
            if lp.manifest.workflows
        }

    def workflow_stages(self) -> frozenset[str]:
        """Every stage some loaded plugin binds a *still-enabled* workflow to. The
        engine reads this once to precompute an O(1) injection guard — a run whose
        plugins provide no workflows pays nothing at the per-stage check.

        A workflow a setting has disabled (``<name>_enabled = false``) is dropped
        here too, so the guard stays exact: when every step at a stage is turned
        off the stage falls out of the set and the engine skips it entirely. This
        mirrors the same skip in ``workflows_for`` (the settings-overlay
        convention; see ``WorkflowSpec``)."""
        return frozenset(
            w.stage
            for lp in self._loaded
            for w in lp.manifest.workflows
            if lp.settings.get(f"{w.name}_enabled") is not False
        )

    def workflows_for(self, stage: str) -> list[tuple[LoadedPlugin, WorkflowSpec]]:
        """Every (plugin, workflow) injected at ``stage``, in registry order.

        Only *active* plugins contribute: a data-only/declarative plugin always,
        an in-process plugin only once enabled+constructed (instance built). An
        un-enabled or errored ``[python]`` plugin is inert — its workflows must
        not fire any more than its module runs (same gate as ``_active_for_seeds``).

        Settings overlay: a plugin's resolved settings can tune a workflow per
        the ``<workflow-name>_enabled`` / ``<workflow-name>_blocking`` convention
        (see ``WorkflowSpec``). ``<name>_enabled = false`` drops the step;
        ``<name>_blocking`` overrides the manifest's ``blocking`` flag. Absent
        settings preserve the manifest values exactly, so a plugin that declares
        no such settings is byte-identical to the pre-overlay behaviour."""
        out: list[tuple[LoadedPlugin, WorkflowSpec]] = []
        for lp in self._active_for_seeds():
            for wf in lp.manifest.workflows_for(stage):
                if lp.settings.get(f"{wf.name}_enabled") is False:
                    continue  # a setting can disable a step
                blocking = bool(lp.settings.get(f"{wf.name}_blocking", wf.blocking))
                out.append((lp, wf if blocking == wf.blocking else replace(wf, blocking=blocking)))
        return out

    def instances(self) -> list[Plugin]:
        """Constructed, trusted, non-disabled in-process plugins."""
        return [lp.instance for lp in self._loaded if lp.instance is not None]

    def _active_for_seeds(self) -> list[LoadedPlugin]:
        """Plugins whose declared seeds apply: data-only/declarative plugins
        (always active) and enabled in-process plugins (instance built). An
        un-enabled or errored ``[python]`` plugin is inert — its module never ran,
        so its seeds must not leak into a worktree (e.g. the Unity plugin's skill
        tree when unity isn't enabled)."""
        return [lp for lp in self._loaded if lp.manifest.python is None or lp.instance is not None]

    def seed_files(self) -> list[str]:
        """Union of every active plugin's ``seed_files`` (literal project-relative
        paths), order-preserving + deduped. Consumed by worktree provisioning so a
        plugin can prime an isolated checkout with gitignored paths it needs."""
        return list(
            dict.fromkeys(f for lp in self._active_for_seeds() for f in lp.manifest.seed_files)
        )

    def seed_globs(self) -> list[str]:
        """Union of every active plugin's ``seed_globs`` (patterns expanded against
        the main repo), order-preserving + deduped."""
        return list(
            dict.fromkeys(g for lp in self._active_for_seeds() for g in lp.manifest.seed_globs)
        )

    def validate(self, policy) -> None:
        """Let every constructed in-process plugin self-validate against the run
        policy (``Plugin.validate``). A plugin raises to reject an incompatible
        config; the registry lets it propagate so the run fails fast at startup."""
        for lp in self._loaded:
            if lp.instance is not None:
                lp.instance.validate(policy)
