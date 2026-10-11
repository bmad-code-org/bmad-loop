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
policy, root=, grants=)`` returns it plus a ``PluginInfo`` for every *discovered*
plugin — its origin, its enable state (membership in ``[plugins] enabled``),
whether it carries a toggle (a builtin's ``[python]`` module, or a project plugin
that needs a content grant), its grant status, and its ``[[settings]]`` fields.
The screen groups these under a dedicated Plugins area; reading manifests and
hashing plugin dirs never imports plugin Python. The registry is what the
settings screen consumes — it never reaches back into this module's internals.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any

from . import policy as policy_mod
from .plugins.grants import TRUSTED_SOURCES, GrantSet, grant_status, needs_grant
from .plugins.model import SettingSpec as PluginSettingSpec
from .plugins.registry import preflight
from .policy import STAGES

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

    ``trust_needed`` is True when the plugin carries a toggle. For a builtin or
    entry-point plugin that is an in-process ``[python]`` module — enabling it
    puts its name in ``[plugins] enabled``. For a project plugin it is any
    executable surface (``grant_required``) — enabling it records an out-of-tree
    content grant (#496/#497), and its name never enters ``enabled``. Other
    plugins are always active and carry no toggle. ``grant_status`` is
    ``granted``/``ungranted``/``mismatch``/``unreadable``, or ``n/a`` when no
    grant applies. ``fields`` are this plugin's settings under
    ``plugins.<name>``; they render whether or not the plugin is enabled (the
    screen greys them until enabled)."""

    name: str
    description: str
    enabled: bool
    trust_needed: bool
    fields: tuple[SettingSpec, ...] = ()
    origin: str = "builtin"
    grant_required: bool = False
    grant_status: str = "n/a"

    @property
    def has_grant(self) -> bool:
        """A grant record exists (whether or not the plugin still matches it)."""
        return self.grant_status in ("granted", "mismatch", "unreadable")


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


def _inject_mux_backends(sections: tuple[SectionSpec, ...], policy: Any) -> tuple[SectionSpec, ...]:
    """Populate the ``mux.backend`` select with the backends registered on THIS host.

    Registered backends are a runtime fact (not a policy enum set), so the option
    list is built per screen-open from ``detect_multiplexers()`` rather than baked
    into ``core.toml``. Any value already present in policy is unioned in, so a
    backend forced via ``bmad-loop mux set --force <name>`` on a host where it is not
    registered is never silently dropped when the settings screen saves (a blank
    Select would collect to ``None`` = delete the key). The empty string (auto-select)
    stays the implicit blank choice and is not listed."""
    from .adapters.multiplexer import detect_multiplexers

    current = getattr(getattr(policy, "mux", None), "backend", "") or ""
    names = [info.name for info in detect_multiplexers()]
    options = tuple(name for name in dict.fromkeys([*names, current]) if name)
    out: list[SectionSpec] = []
    for section in sections:
        if section.name != "mux":
            out.append(section)
            continue
        fields = tuple(
            replace(f, options=options) if f.key == "backend" else f for f in section.fields
        )
        out.append(replace(section, fields=fields))
    return tuple(out)


def build_registry(
    project: Path | None = None,
    policy: Any = None,
    *,
    root: Path | None = None,
    grants: GrantSet | None = None,
) -> SettingsRegistry:
    """Core schema, plus a PluginInfo for every *discovered* plugin.

    Every plugin found by discovery (under ``root``, the code root a run loads,
    else ``project``) is surfaced so the screen can offer an enable toggle and
    group its settings. Each plugin's grant status is the verdict a launch would
    reach under ``grants`` (`registry.preflight`; none given = no grants):
    manifests and hashing only, never an import or exec of plugin Python. A
    plugin's settings are data that render whether or not it is enabled (the
    screen greys them until enabled), so ``plugin_schemas`` covers every plugin
    that contributes settings, making each ``[plugins.<name>]`` table typed on
    save regardless of toggle state."""
    sections = _inject_mux_backends(load_core_schema(), policy)
    enabled = set(getattr(getattr(policy, "plugins", None), "enabled", ()) or ())
    judged = preflight(
        root if root is not None else project,
        policy,
        grants if grants is not None else GrantSet.empty(),
    )
    plugins: list[PluginInfo] = []
    plugin_schemas: dict[str, tuple[PluginSettingSpec, ...]] = {}
    for manifest, verdict, _rel in sorted(judged, key=lambda j: j[0].name):
        name = manifest.name
        if manifest.source in TRUSTED_SOURCES:
            toggled = manifest.python is not None
        else:
            toggled = needs_grant(manifest)
        plugins.append(
            PluginInfo(
                name=name,
                description=manifest.description,
                enabled=name in enabled,
                trust_needed=toggled,
                fields=_plugin_fields(name, manifest.settings),
                origin=manifest.source,
                grant_required=needs_grant(manifest),
                grant_status=grant_status(manifest, verdict),
            )
        )
        if manifest.settings:
            plugin_schemas[name] = manifest.settings
    return SettingsRegistry(
        sections=sections, plugins=tuple(plugins), plugin_schemas=plugin_schemas
    )
