"""PluginRegistry: the inter-pillar contract.

The registry collapses discovery + trust + in-process resolution into one
read-only object the rest of the system consumes:

  * the settings pillar reads ``settings_schema()``;
  * the hook bus iterates ``plugins()`` and dispatches only ``LoadedPlugin.active``
    ones (``hooks_for`` mirrors that filter for introspection);
  * the engine reads ``workflow_stages()``/``workflows_for()`` and worktree
    provisioning ``seed_files()``/``seed_globs()``, both gated on
    ``LoadedPlugin.live_instance_or_declarative``.

Neither consumer reaches into loader internals or re-derives trust from a
manifest's source. ``build`` is the single place trust is enforced (#496/#497)
and failure is isolated. It runs in two passes:

  1. **Verdicts, no exec.** Every manifest is classified (`grants.classify`)
     against the launch's frozen `GrantSet`. Any fatal verdict — a granted
     project plugin whose bytes changed, an ``enabled`` name with no grant, a
     granted dir that can't be read — raises `PluginTrustError` before ANY
     plugin executes, builtins included. A granted plugin's verified tree is
     copied to an out-of-tree snapshot (`snapshot.materialize`) and its
     ``scripts_dir`` repointed there, so ``{scripts}`` and its module come from
     the bytes that were hashed, not the agent-writable live dir.
  2. **Resolve.** A never-granted project plugin with an executable surface is
     refused: inert, ``plugin-refused`` journalled, kept in ``refusals``. An
     un-enabled builtin ``[python]`` plugin is untrusted and wholly inert. A
     granted ``[python]`` module is compiled from the hashed bytes (no
     ``.pyc`` is read or written); an enabled builtin's is imported in place,
     re-checked for containment. Any exception while importing/constructing an
     instance is caught (``except Exception`` — never ``BaseException``, so
     RunStopped/SIGTERM propagate), journalled, and the instance disabled; its
     declarative hooks stay offered. The run survives.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path, PurePath

from . import snapshot, trust
from .digest import VerifiedTree, read_tree
from .grants import GrantSet, TrustState, Verdict, classify, fatal_message, needs_grant
from .loader import PLUGIN_FILE, load_plugins
from .manifest import load_manifest
from .model import (
    HookSpec,
    LoadedPlugin,
    Plugin,
    PluginError,
    PluginManifest,
    SettingSpec,
    WorkflowSpec,
)
from .trust import PluginTrustError, Refusal


def _resolve_settings(manifest: PluginManifest, policy) -> dict:
    """Manifest defaults overlaid by the ``[plugins.<name>]`` policy table. The
    single resolved view the instance is built with and the bus reads for env."""
    resolved = dict(manifest.setting_defaults())
    plugins_pol = getattr(policy, "plugins", None) if policy is not None else None
    overrides = getattr(plugins_pol, "settings", {}).get(manifest.name, {}) if plugins_pol else {}
    resolved.update(overrides)
    return resolved


def _load_module(manifest: PluginManifest, tree: VerifiedTree | None):
    """The executed plugin module: compiled from the verified tree's bytes when
    there is one, else imported from the (builtin) plugin dir in place."""
    scripts_dir = Path(manifest.scripts_dir)
    module_name = manifest.python.module  # type: ignore[union-attr]
    spec_name = f"bmad_loop_plugin_{manifest.name}"
    if tree is not None:
        # The snapshot under scripts_dir was verified against `tree`, but the bytes
        # executed are the ones that were hashed — never re-read from disk, so a
        # swap after the hash can't reach exec. compile+exec, not exec_module:
        # SourceFileLoader would prefer a planted __pycache__ .pyc whose recorded
        # mtime/size match, and would write one back.
        rel = PurePath(module_name).as_posix()
        data = next((f.data for f in tree.files if f.relpath == rel), None)
        if data is None:
            raise ImportError(f"plugin module not in verified tree: {module_name}")
        module_path = scripts_dir / rel
        spec = importlib.util.spec_from_file_location(spec_name, module_path)
        if spec is None:
            raise ImportError(f"cannot load plugin module: {module_path}")
        module = importlib.util.module_from_spec(spec)
        code = compile(data, str(module_path), "exec", dont_inherit=True)
        exec(code, module.__dict__)  # nosec B102 - the granted, hashed plugin bytes
        return module
    module_path = scripts_dir / module_name
    # `_parse_python` rejects an absolute, parent-climbing or root-naming module at
    # manifest load, but lexically: a project-relative name that is a symlink out of
    # the plugin resolves fine and would be exec'd. Re-check after resolution, since
    # what happens below is arbitrary code execution, not a copy.
    # Strictly below, not merely inside: `is_relative_to` is true for equal paths,
    # so a module resolving to the plugin dir itself would pass an inside-check.
    try:
        resolved, base = module_path.resolve(), scripts_dir.resolve()
        contained = resolved != base and resolved.is_relative_to(base)
    except (OSError, RuntimeError):
        contained = False
    if not contained:
        raise ImportError(f"plugin module escapes its plugin directory: {module_path}")
    if not module_path.is_file():
        raise FileNotFoundError(f"plugin module not found: {module_path}")
    spec = importlib.util.spec_from_file_location(spec_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instantiate(manifest: PluginManifest, settings: dict, tree: VerifiedTree | None) -> Plugin:
    """Load the plugin's module and construct its Plugin subclass with its
    resolved settings.

    Caller is responsible for the trust gate; this performs the actual exec.
    ``tree`` is the verified tree of a granted plugin (its module is compiled
    from those bytes); ``None`` for an enabled builtin. Kept tiny so the
    registry's try/except wraps exactly the import + construct surface.
    """
    module = _load_module(manifest, tree)
    cls_name = manifest.python.cls  # type: ignore[union-attr]
    cls = getattr(module, cls_name, None)
    if cls is None:
        # manifest.python is validated non-None before this loader runs (mirrors
        # the manifest.python.cls access above).
        raise AttributeError(
            f"plugin module {manifest.python.module!r} has no {cls_name!r}"  # pyright: ignore[reportOptionalMemberAccess]
        )
    if not (isinstance(cls, type) and issubclass(cls, Plugin)):
        raise TypeError(f"{cls_name!r} must subclass plugins.Plugin")
    return cls(manifest, settings)


def _rel_path(manifest: PluginManifest, project: Path | None) -> str:
    """The plugin dir as a POSIX relpath from the discovery root — the key a
    grant records. Lexical: discovery builds ``scripts_dir`` under ``project``.
    Falls back to the absolute POSIX path (never equal to a recorded relpath, so
    a grant can't match) when there is no common root."""
    if project is None:
        return PurePath(manifest.scripts_dir).as_posix()
    try:
        return PurePath(os.path.relpath(manifest.scripts_dir, project)).as_posix()
    except ValueError:  # win32: another drive
        return PurePath(manifest.scripts_dir).as_posix()


def _bind_manifest(m: PluginManifest, tree: VerifiedTree) -> None:
    """Raise `PluginError` unless ``m`` is what the tree's own ``plugin.toml``
    parses to.

    Discovery parsed ``m`` from an earlier read than the one `read_tree` hashed,
    and the run executes ``m``'s hooks, not the tree's: a manifest swapped in for
    discovery and restored before the hash would pass the digest check and run
    hooks nobody granted. Re-parsing the hashed bytes closes that window."""
    data = next((f.data for f in tree.files if f.relpath == PLUGIN_FILE), None)
    if data is None:
        raise PluginError(f"plugin dir {tree.root}: no {PLUGIN_FILE} in the hashed tree")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PluginError(f"plugin dir {tree.root}: {PLUGIN_FILE} is not valid UTF-8") from e
    hashed = load_manifest(text, str(tree.root / PLUGIN_FILE), m.scripts_dir, origin=m.source)
    if hashed != m:
        raise PluginError(
            f"plugin dir {tree.root}: {PLUGIN_FILE} changed while it was being verified"
        )


def read_bound_tree(m: PluginManifest) -> VerifiedTree:
    """`read_tree` of ``m``'s dir, refused unless its hashed ``plugin.toml`` is
    ``m`` (`_bind_manifest`). The one read for every path that grants a tree or
    runs one: a grant written from an unbound read would pin a manifest swapped
    in after the operator's discovery, and the launch-side bind would then only
    compare that manifest with itself."""
    tree = read_tree(Path(m.scripts_dir))
    _bind_manifest(m, tree)
    return tree


def _memo_tree(m: PluginManifest) -> Callable[[], VerifiedTree]:
    """A `read_bound_tree` thunk that reads the dir at most once, so the tree the
    verdict compared is the very tree the snapshot copies and exec compiles."""
    cache: list[VerifiedTree] = []

    def live() -> VerifiedTree:
        if not cache:
            cache.append(read_bound_tree(m))
        return cache[0]

    return live


def _judge(
    manifests: Iterable[PluginManifest],
    project: Path | None,
    enabled: frozenset[str],
    grants: GrantSet,
) -> list[tuple[PluginManifest, Verdict, Callable[[], VerifiedTree], str]]:
    """Pass 1's verdicts: (manifest, verdict, memoized tree thunk, grant relpath)
    per plugin. Hashes at most; never imports or runs anything."""
    judged: list[tuple[PluginManifest, Verdict, Callable[[], VerifiedTree], str]] = []
    manifests = list(manifests)
    claimants = [m.name for m in manifests if needs_grant(m)]
    for m in manifests:
        rel = _rel_path(m, project)
        live = _memo_tree(m)
        record = grants.match(m.name, rel, claimants)
        verdict = classify(m, enabled=enabled, record=record, rel_path=rel, live=live)
        judged.append((m, verdict, live, rel))
    return judged


def preflight(
    project: Path | None, policy, grants: GrantSet
) -> list[tuple[PluginManifest, Verdict, str]]:
    """The verdict `PluginRegistry.build` would reach for every discovered plugin,
    without building: (manifest, verdict, grant relpath). A launch calls this to
    refuse before its first side effect; ``build`` re-judges the same way and stays
    the gate. Manifest discovery and hashing only — no plugin code runs. Raises
    `PluginError` on a discovery fault."""
    manifests = load_plugins(project)
    enabled = trust.enabled_names(policy)
    return [
        (m, v, rel) for m, v, _live, rel in _judge(manifests.values(), project, enabled, grants)
    ]


def _resolve(
    manifest: PluginManifest,
    policy,
    journal,
    verdict: Verdict,
    tree: VerifiedTree | None,
    live_dir: str,
    refusal: Refusal | None,
) -> LoadedPlugin:
    settings = _resolve_settings(manifest, policy)
    if refusal is not None:
        if journal is not None:
            journal.append(
                "plugin-refused",
                plugin=refusal.name,
                reason=refusal.reason,
                origin=refusal.origin,
                path=refusal.path,
            )
        return LoadedPlugin(manifest=manifest, trusted=False, settings=settings, verdict=verdict)

    if manifest.python is None:
        if journal is not None:
            journal.append("plugin-loaded", plugin=manifest.name, mode="declarative")
        return LoadedPlugin(
            manifest=manifest, settings=settings, verdict=verdict, live_dir=live_dir
        )

    if verdict.state is TrustState.UNENABLED:
        if journal is not None:
            journal.append(
                "plugin-untrusted",
                plugin=manifest.name,
                reason="[python] module requires [plugins] enabled",
            )
        return LoadedPlugin(manifest=manifest, trusted=False, settings=settings, verdict=verdict)

    try:
        instance = _instantiate(manifest, settings, tree)
    except Exception as e:  # isolate plugin failures; never BaseException
        if journal is not None:
            journal.append("plugin-error", plugin=manifest.name, error=f"{type(e).__name__}: {e}")
        return LoadedPlugin(
            manifest=manifest,
            disabled=True,
            error=str(e),
            settings=settings,
            verdict=verdict,
            live_dir=live_dir,
        )

    if journal is not None:
        journal.append("plugin-loaded", plugin=manifest.name, mode="python")
    return LoadedPlugin(
        manifest=manifest, instance=instance, settings=settings, verdict=verdict, live_dir=live_dir
    )


class PluginRegistry:
    """Read-only view over the loaded plugins. Build once per run."""

    def __init__(self, loaded: list[LoadedPlugin], refusals: Iterable[Refusal] = ()):
        # stable order: manifest priority then load (discovery/overlay) order.
        self._loaded = sorted(loaded, key=lambda lp: lp.manifest.priority)
        # the non-fatal refusals `build` made (never-granted project plugins);
        # the engine journals them. Storage only — the gate lives in `build`.
        self.refusals: tuple[Refusal, ...] = tuple(refusals)

    @classmethod
    def build(
        cls,
        project: Path | None = None,
        policy=None,
        journal=None,
        *,
        grants: GrantSet = GrantSet.empty(),
    ) -> PluginRegistry:
        """Discover, gate and resolve every plugin (see module doc).

        ``grants`` is the launch's frozen grant set; the default, none, refuses
        every project plugin with an executable surface (fail closed). Raises
        `PluginTrustError` on any fatal verdict, before anything executes."""
        manifests = load_plugins(project, journal=journal)
        enabled = trust.enabled_names(policy)
        # pass 1: verdicts only — nothing is imported or run until all are in.
        judged = _judge(manifests.values(), project, enabled, grants)
        fatal = [
            Refusal(m.name, fatal_message(m.name, verdict), m.source, rel)
            for m, verdict, _live, rel in judged
            if verdict.fatal
        ]
        if fatal:
            raise PluginTrustError(tuple(fatal))

        resolved: list[tuple[PluginManifest, Verdict, VerifiedTree | None, str, Refusal | None]]
        resolved = []
        for m, verdict, live, rel in judged:
            tree: VerifiedTree | None = None
            live_dir = ""
            refusal: Refusal | None = None
            if verdict.state is TrustState.GRANTED:
                if grants.snapshot_root is None:
                    raise PluginError(
                        f"plugin {m.name!r} is granted but no snapshot root was given"
                    )
                tree = live()  # the memoized tree classify compared
                live_dir = m.scripts_dir
                m = replace(m, scripts_dir=str(snapshot.materialize(tree, grants.snapshot_root)))
            elif verdict.state is TrustState.REFUSED:
                refusal = Refusal(m.name, verdict.reason, m.source, rel)
            resolved.append((m, verdict, tree, live_dir, refusal))

        # pass 2: resolve — the only place a module can execute.
        loaded = [
            _resolve(m, policy, journal, verdict, tree, live_dir, refusal)
            for m, verdict, tree, live_dir, refusal in resolved
        ]
        refusals = [refusal for *_, refusal in resolved if refusal is not None]
        return cls(loaded, refusals)

    # ----------------------------------------------------------- consumers

    def plugins(self) -> list[LoadedPlugin]:
        return list(self._loaded)

    def get(self, name: str) -> LoadedPlugin | None:
        for lp in self._loaded:
            if lp.manifest.name == name:
                return lp
        return None

    def hooks_for(self, stage: str) -> list[tuple[LoadedPlugin, HookSpec]]:
        """Every (plugin, hook) bound to ``stage``, in registry order — the same
        ``LoadedPlugin.active`` filter the bus dispatches by (introspection; the
        bus iterates ``plugins()`` itself). A disabled instance still contributes
        its *declarative* hooks (those are out-of-process and independent of the
        in-process module that failed); a refused or un-enabled plugin none."""
        out: list[tuple[LoadedPlugin, HookSpec]] = []
        for lp in self._loaded:
            if not lp.active:
                continue
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
        """Every stage some live plugin (see ``_active_for_seeds``) binds a
        *still-enabled* workflow to. The engine reads this once to precompute an O(1) injection guard — a run whose
        plugins provide no workflows pays nothing at the per-stage check.

        A workflow a setting has disabled (``<name>_enabled = false``) is dropped
        here too, so the guard stays exact: when every step at a stage is turned
        off the stage falls out of the set and the engine skips it entirely. This
        mirrors the same skip in ``workflows_for`` (the settings-overlay
        convention; see ``WorkflowSpec``)."""
        return frozenset(
            w.stage
            for lp in self._active_for_seeds()
            for w in lp.manifest.workflows
            if lp.settings.get(f"{w.name}_enabled") is not False
        )

    def workflows_for(self, stage: str) -> list[tuple[LoadedPlugin, WorkflowSpec]]:
        """Every (plugin, workflow) injected at ``stage``, in registry order.

        Only *live* plugins contribute: an active data-only/declarative plugin,
        an in-process plugin only once trusted+constructed (instance built). A
        refused project plugin, or an un-enabled or errored ``[python]`` one, is
        inert — its workflows must
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
        """Plugins whose declared seeds and workflows apply
        (``LoadedPlugin.live_instance_or_declarative``): active declarative
        plugins, and active in-process plugins whose instance was built. A
        refused project plugin, or an un-enabled or errored ``[python]`` one, is
        inert — its module never ran, so its seeds must not leak into a worktree
        (e.g. the Unity plugin's skill tree when unity isn't enabled)."""
        return [lp for lp in self._loaded if lp.live_instance_or_declarative]

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
