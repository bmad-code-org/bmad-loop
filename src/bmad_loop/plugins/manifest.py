"""Parse + validate a ``plugin.toml`` into an immutable PluginManifest.

Mirrors ``engines/plugin.py`` ``_parse_plugin`` / ``_load_toml``: ``tomllib``
with ``TOMLDecodeError`` wrapped into a domain error, every field coerced to its
declared type, project-relative seed paths enforced, and a single ``fail()``
helper that prefixes the source for actionable messages.

Validation here is purely structural — it does not decide trust (``trust.py``)
or whether an api_version is supported by *this* build (the loader does, so it
can hard-error on a builtin but skip a third-party plugin). A manifest that
parses is well-formed, not necessarily loadable.
"""

from __future__ import annotations

import tomllib
from typing import Any

from ..platform_util import has_parent_ref, is_absolute_path
from .model import (
    SETTING_TYPES,
    WORKFLOW_ROLES,
    WORKFLOW_STAGES,
    HookSpec,
    PluginError,
    PluginManifest,
    PythonSpec,
    SettingSpec,
    WorkflowSpec,
)


def _check_relative_paths(values: tuple[str, ...], label: str, fail) -> None:
    for value in values:
        if not value or is_absolute_path(value) or has_parent_ref(value):
            raise fail(f"{label} entries must be project-relative paths: got {value!r}")


def _parse_hooks(hooks_d: Any, fail) -> tuple[HookSpec, ...]:
    if not hooks_d:
        return ()
    if not isinstance(hooks_d, dict):
        raise fail("[hooks] must be a table of [hooks.<stage>] tables")
    hooks = []
    for stage, raw in hooks_d.items():
        if not isinstance(raw, dict):
            raise fail(f"[hooks.{stage}] must be a table")
        cmd = str(raw.get("cmd", ""))
        if not cmd:
            raise fail(f"[hooks.{stage}] requires a 'cmd'")
        timeout = int(raw.get("timeout_sec", 120))
        if timeout < 1:
            raise fail(f"[hooks.{stage}] timeout_sec must be >= 1: got {timeout}")
        hooks.append(
            HookSpec(
                stage=str(stage),
                cmd=cmd,
                timeout_sec=timeout,
                blocking=bool(raw.get("blocking", False)),
                fail_closed=bool(raw.get("fail_closed", False)),
            )
        )
    return tuple(hooks)


def _parse_settings(settings_l: Any, fail) -> tuple[SettingSpec, ...]:
    if not settings_l:
        return ()
    if not isinstance(settings_l, list):
        raise fail("[[settings]] must be an array of tables")
    specs: list[SettingSpec] = []
    seen: set[str] = set()
    for raw in settings_l:
        if not isinstance(raw, dict):
            raise fail("each [[settings]] entry must be a table")
        key = str(raw.get("key", "")).strip()
        if not key:
            raise fail("each [[settings]] entry requires a 'key'")
        if key in seen:
            raise fail(f"duplicate setting key: {key!r}")
        seen.add(key)
        kind = str(raw.get("type", "")).strip()
        if kind not in SETTING_TYPES:
            raise fail(f"setting {key!r} type must be one of {sorted(SETTING_TYPES)}: got {kind!r}")
        options = tuple(str(o) for o in raw.get("options", ()))
        if kind == "select" and not options:
            raise fail(f"select setting {key!r} requires a non-empty 'options' list")
        specs.append(
            SettingSpec(
                key=key,
                type=kind,
                default=raw.get("default"),
                help=str(raw.get("help", "")),
                options=options,
                label=str(raw.get("label", "")),
                min=raw.get("min"),
                max=raw.get("max"),
            )
        )
    return tuple(specs)


def _parse_workflows(workflows_d: Any, fail) -> tuple[WorkflowSpec, ...]:
    """Parse ``[workflows.<name>]`` tables — the ``[provides]`` surface. Each is a
    stage-bound session injection; mirrors ``_parse_hooks`` (name as the table
    key, like a hook's stage). ``stage`` and ``role`` are validated against the
    framework's small allowlists so a typo fails loudly at load rather than
    silently never firing."""
    if not workflows_d:
        return ()
    if not isinstance(workflows_d, dict):
        raise fail("[workflows] must be a table of [workflows.<name>] tables")
    specs: list[WorkflowSpec] = []
    for name, raw in workflows_d.items():
        if not isinstance(raw, dict):
            raise fail(f"[workflows.{name}] must be a table")
        stage = str(raw.get("stage", "")).strip()
        if stage not in WORKFLOW_STAGES:
            raise fail(
                f"[workflows.{name}] stage must be one of {sorted(WORKFLOW_STAGES)}: got {stage!r}"
            )
        role = str(raw.get("role", "dev")).strip() or "dev"
        if role not in WORKFLOW_ROLES:
            raise fail(
                f"[workflows.{name}] role must be one of {sorted(WORKFLOW_ROLES)}: got {role!r}"
            )
        prompt = str(raw.get("prompt", ""))
        if not prompt:
            raise fail(f"[workflows.{name}] requires a 'prompt'")
        specs.append(
            WorkflowSpec(
                name=str(name),
                stage=stage,
                role=role,
                prompt=prompt,
                blocking=bool(raw.get("blocking", False)),
            )
        )
    return tuple(specs)


def _parse_python(python_d: Any, fail) -> PythonSpec | None:
    if python_d is None:
        return None
    if not isinstance(python_d, dict):
        raise fail("[python] must be a table")
    module = str(python_d.get("module", "")).strip()
    if not module:
        raise fail("[python] requires a 'module'")
    if is_absolute_path(module) or has_parent_ref(module):
        raise fail(f"[python] module must be a plugin-relative path: got {module!r}")
    return PythonSpec(module=module, cls=str(python_d.get("class", "Plugin")) or "Plugin")


def parse_manifest(
    doc: dict, source: str, scripts_dir: str, origin: str = "project"
) -> PluginManifest:
    def fail(msg: str) -> PluginError:
        return PluginError(f"plugin {source}: {msg}")

    plugin_d = doc.get("plugin")
    if not isinstance(plugin_d, dict):
        raise fail("missing [plugin] table")

    name = str(plugin_d.get("name", "")).strip()
    if not name:
        raise fail("[plugin] 'name' is required")

    raw_api = plugin_d.get("api_version")
    if raw_api is None:
        raise fail("[plugin] 'api_version' is required")
    try:
        api_version = int(raw_api)
    except (TypeError, ValueError):
        raise fail(f"[plugin] api_version must be an integer: got {raw_api!r}") from None

    seed_files = tuple(str(s) for s in plugin_d.get("seed_files", ()))
    _check_relative_paths(seed_files, "seed_files", fail)
    seed_globs = tuple(str(s) for s in plugin_d.get("seed_globs", ()))
    _check_relative_paths(seed_globs, "seed_globs", fail)

    return PluginManifest(
        name=name,
        version=str(plugin_d.get("version", "0.0.0")),
        api_version=api_version,
        description=str(plugin_d.get("description", "")),
        author=str(plugin_d.get("author", "")),
        hooks=_parse_hooks(doc.get("hooks"), fail),
        settings=_parse_settings(doc.get("settings"), fail),
        python=_parse_python(doc.get("python"), fail),
        workflows=_parse_workflows(doc.get("workflows"), fail),
        seed_files=seed_files,
        seed_globs=seed_globs,
        priority=int(plugin_d.get("priority", 0)),
        scripts_dir=scripts_dir,
        source=origin,
    )


def load_manifest(
    text: str, source: str, scripts_dir: str, origin: str = "project"
) -> PluginManifest:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PluginError(f"plugin {source}: invalid TOML: {e}") from e
    return parse_manifest(doc, source, scripts_dir, origin)
