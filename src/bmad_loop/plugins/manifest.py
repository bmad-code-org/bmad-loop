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

from ..platform_util import (
    has_parent_ref,
    is_absolute_path,
    names_tree_root,
    names_win32_alias,
)
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

# The CLOSED set of faults a raw coercion over a `tomllib` value can raise — see
# `adapters/profile.py::CONVERSION_FAULTS` for the nine-type enumeration behind it
# (the `inf`/`-inf` and oversized-int OverflowError rows are the ones a per-field
# guard keeps missing). Restated here rather than imported: the plugin layer does
# not otherwise depend on the adapter layer, and each module's own domain test
# pins its copy.
CONVERSION_FAULTS = (AttributeError, OverflowError, TypeError, ValueError)


def _str_list(plugin_d: dict, key: str, fail) -> tuple[str, ...]:
    # Shape before entries, the same rule the sibling seed sources apply to their
    # own lists (policy.py `worktree_seed`, adapters/profile.py `str_list`): a
    # bare string iterates into per-character entries that each pass the
    # per-entry guard below, and a scalar raises a bare TypeError out of `loads`
    # where every other malformed value here raises PluginError.
    raw = plugin_d.get(key, ())
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise fail(f"[plugin] {key} must be a list of paths: got {raw!r}")
    if not all(isinstance(s, str) for s in raw):
        raise fail(f"[plugin] {key} entries must be strings: got {list(raw)!r}")
    return tuple(raw)


def _check_relative_paths(values: tuple[str, ...], label: str, fail) -> None:
    # `names_tree_root` subsumes the emptiness check it replaced: "", ".", "./" and
    # ".\" all name the tree rather than anything in it, and a seed entry that names
    # the tree root makes provision_worktree copy the whole repo into the worktree.
    #
    # The second refusal is a SEPARATE arm rather than a fourth term in the first,
    # because the first one's message is false for what it catches: `NUL` and
    # `skills.` ARE project-relative. What they are not is deterministic — each names
    # a different path on Windows than the string spells, so the same manifest seeds a
    # different file (or a device) depending on where the run happens. One call site
    # guards BOTH `seed_files` and `seed_globs`; see `names_win32_alias`'s docstring
    # for the two rules and their sources.
    for value in values:
        if names_tree_root(value) or is_absolute_path(value) or has_parent_ref(value):
            raise fail(f"{label} entries must be project-relative paths: got {value!r}")
        if names_win32_alias(value):
            raise fail(
                f"{label} entries must not name a Windows device or end a component "
                f"in a period or space: got {value!r}"
            )


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
    if names_tree_root(module) or is_absolute_path(module) or has_parent_ref(module):
        raise fail(f"[python] module must be a plugin-relative path: got {module!r}")
    # Separate arm, same reason as `_check_relative_paths`, and it bites harder here:
    # this value is not copied but *imported*, so a module spelled `NUL` or `hooks.`
    # resolves on Windows to something other than the file the manifest names.
    if names_win32_alias(module):
        raise fail(
            "[python] module must not name a Windows device or end a component "
            f"in a period or space: got {module!r}"
        )
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
    except CONVERSION_FAULTS:
        raise fail(f"[plugin] api_version must be an integer: got {raw_api!r}") from None

    seed_files = _str_list(plugin_d, "seed_files", fail)
    _check_relative_paths(seed_files, "seed_files", fail)
    seed_globs = _str_list(plugin_d, "seed_globs", fail)
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
    try:
        return parse_manifest(doc, source, scripts_dir, origin)
    except PluginError:
        raise  # intent: a domain error is never re-wrapped (it is not a CONVERSION_FAULT)
    except CONVERSION_FAULTS as e:
        # A funnel, not per-field guards — the `_load_toml` arm of
        # adapters/profile.py, same reason: `parse_manifest`'s raw conversions
        # (`int()` on `priority`, `api_version` and a hook's `timeout_sec`,
        # iteration over a setting's `options`) raise bare conversion errors on
        # TOML-legal values of the wrong type, and every consumer keys its fault
        # handling on PluginError — the TUI's settings pane reports it beside a
        # PolicyError, and `settings_schema`/`PluginRegistry.build` reach it
        # through `load_plugins`. A bare escape crashed `validate` before any
        # document was printed. The tuple is that module's CLOSED set for the
        # `tomllib` value domain, shared so the two parsers cannot drift apart
        # one exception type at a time.
        raise PluginError(f"plugin {source}: malformed field value: {e}") from e
