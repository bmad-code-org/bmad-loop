"""tomlkit-backed editing model for .bmad-loop/policy.toml.

Zero textual imports: this is the testable core of the settings editor. The
form never re-implements policy rules — validate() round-trips the document
through policy.loads(), so policy.py stays the single source of truth.
tomlkit preserves comments and key order; a missing file starts from
POLICY_TEMPLATE so the first save carries the full inline documentation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomlkit

from .. import policy as policy_mod

STAGES = ("dev", "review", "triage")


class PolicyDoc:
    """A policy.toml document under edit. Sections may be dotted
    ("adapter.dev") to address the per-stage override tables."""

    def __init__(self, doc: tomlkit.TOMLDocument):
        self._doc = doc

    @classmethod
    def load(cls, path: Path) -> PolicyDoc:
        text = path.read_text(encoding="utf-8") if path.is_file() else policy_mod.POLICY_TEMPLATE
        return cls(tomlkit.parse(text))

    def _table(self, section: str, create: bool) -> Any | None:
        node: Any = self._doc
        for part in section.split("."):
            if part not in node:
                if not create:
                    return None
                node[part] = tomlkit.table()
            node = node[part]
        return node

    def get(self, section: str, key: str) -> Any | None:
        """Raw value from the document, or None when the key is unset."""
        table = self._table(section, create=False)
        if table is None or key not in table:
            return None
        return table[key]

    def set(self, section: str, key: str, value: Any | None) -> None:
        """Set a key; None deletes it. A per-stage adapter table emptied by a
        delete is dropped entirely, restoring 'unset = inherit'."""
        if value is None:
            table = self._table(section, create=False)
            if table is not None and key in table:
                del table[key]
            parent, _, stage = section.partition(".")
            if stage and table is not None and len(table) == 0:
                del self._doc[parent][stage]
            return
        self._table(section, create=True)[key] = value

    def validate(
        self,
        plugin_schemas: dict[str, Any] | None = None,
        project: Path | None = None,
    ) -> str | None:
        """Authoritative validation via policy.loads(); None when valid.

        ``plugin_schemas`` (plugin name -> setting specs) lets the round-trip
        also type-check any [plugins.<name>] tables the screen rendered. When
        ``project`` is given, every enabled in-process plugin additionally
        self-validates against the parsed policy (Plugin.validate) — the same
        coupling check the engine runs at startup (e.g. the Unity plugin's
        editor_mode↔scm.isolation rule) — so an incompatible combination is
        caught at save time rather than mid-run. Building the registry imports
        only the plugins already trusted in [plugins] enabled."""
        try:
            pol = policy_mod.loads(self.dumps(), plugin_schemas=plugin_schemas)
        except policy_mod.PolicyError as e:
            return str(e)
        if project is not None:
            from ..plugins.model import PluginError
            from ..plugins.registry import PluginRegistry

            try:
                PluginRegistry.build(project, pol).validate(pol)
            except (policy_mod.PolicyError, PluginError) as e:
                return str(e)
        return None

    def dumps(self) -> str:
        return tomlkit.dumps(self._doc)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(self.dumps(), encoding="utf-8")
        os.replace(tmp, path)
