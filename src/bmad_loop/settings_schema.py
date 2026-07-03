"""Schema-driven settings: the presentation layer over policy.toml.

The settings UI used to carry a hand-written list of field descriptors. That
list now lives in ``data/settings/core.toml`` as a *schema* — widget kind,
label, help text, options, bounds — while defaults and select options are
*referenced* from the ``policy.py`` dataclasses/enum-sets rather than copied, so
``policy.py`` stays the single source of truth for the runtime model and a sync
test guarantees the schema can never drift from it.

Two shapes mirror the old screen vocabulary:

  * ``SettingSpec`` — one field (the old ``_Field``): section, key, widget kind,
    options, resolved default, placeholder, numeric bounds, label, description.
    ``widget_id`` is unchanged so every existing widget id (``#limits-max_review_cycles``,
    ``#adapter-review-model`` …) is byte-identical.
  * ``SectionSpec`` — a collapsible group: name (the TOML section key), display
    label, description, its fields, and the owning plugin ("" for core).

``load_core_schema()`` parses the bundled core schema; ``build_registry(project,
policy)`` returns it plus a ``PluginInfo`` for every *discovered* plugin — its
enable state (membership in ``[plugins] enabled``), whether enabling it grants
trust (a ``[python]`` module), and its ``[[settings]]`` fields. The screen groups
these under a dedicated Plugins area; reading manifests never imports plugin
Python. The registry is what the settings screen consumes — it never reaches
back into this module's internals.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from . import policy as policy_mod
from .plugins import load_plugins
from .plugins.model import SettingSpec as PluginSettingSpec
from .tui.settings import STAGES

# plugin [[settings]] types -> settings-screen widget kinds. The screen knows
# switch/int/float/str/select; a plugin declares bool/int/float/str/select.
_PLUGIN_KIND = {"bool": "switch", "int": "int", "float": "float", "str": "str", "select": "select"}


@dataclass(frozen=True)
class SettingSpec:
    """One settings field. Same vocabulary as the screen's former ``_Field`` so
    the generic compose/collect/save logic is untouched."""

    section: str
    key: str
    kind: str  # select | int | float | str | switch | lines | args
    options: tuple[str, ...] = ()
    default: Any = None
    placeholder: str = ""
    minimum: float | None = None
    maximum: float | None = None
    label: str = ""  # display override; falls back to key when empty
    description: str = ""  # muted caption shown below the field row

    @property
    def widget_id(self) -> str:
        return f"{self.section}-{self.key}".replace(".", "-")


@dataclass(frozen=True)
class SectionSpec:
    """A collapsible settings group. ``name`` is the policy.toml section key
    (may be dotted, e.g. ``adapter.dev``). Core only — plugin settings are
    carried by ``PluginInfo``."""

    name: str
    fields: tuple[SettingSpec, ...]
    label: str = ""  # display label; falls back to name
    description: str = ""

    @property
    def title(self) -> str:
        base = self.label or self.name
        return f"{base} — {self.description}" if self.description else base


@dataclass(frozen=True)
class PluginInfo:
    """One discovered plugin as the settings screen sees it: an enable toggle
    (when trust-gated) plus its settings fields.

    ``trust_needed`` is True for a plugin that declares an in-process ``[python]``
    module — enabling it grants trust (its name enters ``[plugins] enabled``).
    Data-only plugins are always active and carry no toggle. ``fields`` are this
    plugin's settings under ``plugins.<name>``; they render whether or not the
    plugin is enabled (the screen greys them until enabled)."""

    name: str
    description: str
    enabled: bool
    trust_needed: bool
    fields: tuple[SettingSpec, ...] = ()


@dataclass(frozen=True)
class SettingsRegistry:
    """Core schema plus a PluginInfo per discovered plugin — the settings
    screen's source.

    ``plugin_schemas`` maps each plugin that contributes settings to its declared
    specs so the screen can pass them to ``policy.loads`` for typed validation of
    the ``[plugins.<name>]`` tables on save (every discovered plugin, since
    settings are data that may be set whether or not the plugin is enabled).
    """

    sections: tuple[SectionSpec, ...]
    plugins: tuple[PluginInfo, ...] = ()
    plugin_schemas: dict[str, tuple[PluginSettingSpec, ...]] = field(default_factory=dict)

    def fields(self) -> tuple[SettingSpec, ...]:
        core = tuple(f for s in self.sections for f in s.fields)
        plugin = tuple(f for p in self.plugins for f in p.fields)
        return core + plugin


# --------------------------------------------------------------- ref resolution


def _resolve_ref(ref: str) -> Any:
    """``"ScmPolicy.merge_strategy"`` -> the dataclass default value;
    ``"GATE_MODES"`` -> the module-level enum set (caller sorts it)."""
    cls_name, _, attr = ref.partition(".")
    target = getattr(policy_mod, cls_name)
    return getattr(target, attr) if attr else target


def _resolve_field(section: str, raw: dict) -> SettingSpec:
    if "options_ref" in raw:
        options = tuple(sorted(_resolve_ref(raw["options_ref"])))
    else:
        options = tuple(str(o) for o in raw.get("options", ()))
    if "default_ref" in raw:
        default = _resolve_ref(raw["default_ref"])
    else:
        default = raw.get("default")
    return SettingSpec(
        section=section,
        key=str(raw["key"]),
        kind=str(raw["kind"]),
        options=options,
        default=default,
        placeholder=str(raw.get("placeholder", "")),
        minimum=raw.get("minimum"),
        maximum=raw.get("maximum"),
        label=str(raw.get("label", "")),
        description=str(raw.get("description", "")),
    )


def _expand_section(raw: dict) -> list[SectionSpec]:
    """One core.toml ``[[section]]`` -> one SectionSpec, except a stage template
    (``expand_stages``) fans out to one section per STAGES entry, substituting
    ``{stage}`` in the name/label/description and reusing the same field set."""
    fields_raw = raw.get("field", [])
    if raw.get("expand_stages"):
        sections = []
        for stage in STAGES:
            name = str(raw["name"]).format(stage=stage)
            sections.append(
                SectionSpec(
                    name=name,
                    fields=tuple(_resolve_field(name, f) for f in fields_raw),
                    label=str(raw.get("label", "")).format(stage=stage),
                    description=str(raw.get("description", "")).format(stage=stage),
                )
            )
        return sections
    name = str(raw["name"])
    return [
        SectionSpec(
            name=name,
            fields=tuple(_resolve_field(name, f) for f in fields_raw),
            label=str(raw.get("label", "")),
            description=str(raw.get("description", "")),
        )
    ]


def load_core_schema() -> tuple[SectionSpec, ...]:
    """Parse the bundled core schema into ordered SectionSpecs (render order)."""
    text = (
        resources.files("bmad_loop.data").joinpath("settings/core.toml").read_text(encoding="utf-8")
    )
    doc = tomllib.loads(text)
    sections: list[SectionSpec] = []
    for raw in doc.get("section", []):
        sections.extend(_expand_section(raw))
    return tuple(sections)


# ------------------------------------------------------------- plugin sections


def _plugin_fields(name: str, specs: tuple[PluginSettingSpec, ...]) -> tuple[SettingSpec, ...]:
    section = f"plugins.{name}"
    return tuple(
        SettingSpec(
            section=section,
            key=s.key,
            kind=_PLUGIN_KIND.get(s.type, "str"),
            options=s.options,
            default=s.default,
            minimum=s.min,
            maximum=s.max,
            label=s.label,
            description=s.help,
        )
        for s in specs
    )


def build_registry(project: Path | None = None, policy: Any = None) -> SettingsRegistry:
    """Core schema, plus a PluginInfo for every *discovered* plugin.

    Every plugin found by discovery is surfaced so the screen can offer an enable
    toggle (membership in ``[plugins] enabled``) and group its settings. Reading
    manifests never imports/executes plugin Python — that stays trust-gated for
    hooks. A plugin's settings are data that render whether or not it is enabled
    (the screen greys them until enabled), so ``plugin_schemas`` covers every
    plugin that contributes settings, making each ``[plugins.<name>]`` table
    typed on save regardless of toggle state."""
    sections = load_core_schema()
    enabled = set(getattr(getattr(policy, "plugins", None), "enabled", ()) or ())
    manifests = load_plugins(project)
    plugins: list[PluginInfo] = []
    plugin_schemas: dict[str, tuple[PluginSettingSpec, ...]] = {}
    for name in sorted(manifests):
        manifest = manifests[name]
        plugins.append(
            PluginInfo(
                name=name,
                description=manifest.description,
                enabled=name in enabled,
                trust_needed=manifest.python is not None,
                fields=_plugin_fields(name, manifest.settings),
            )
        )
        if manifest.settings:
            plugin_schemas[name] = manifest.settings
    return SettingsRegistry(
        sections=sections, plugins=tuple(plugins), plugin_schemas=plugin_schemas
    )
