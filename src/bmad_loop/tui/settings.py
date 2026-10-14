"""tomlkit-backed editing model for .bmad-loop/policy.toml.

Zero textual imports: this is the testable core of the settings editor. The
form never re-implements policy rules — validate() round-trips the document
through policy.loads(), so policy.py stays the single source of truth.
tomlkit preserves comments and key order; a missing file starts from
POLICY_TEMPLATE so the first save carries the full inline documentation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit

from .. import policy as policy_mod
from ..platform_util import atomic_write_text_confined
from ..plugins.grants import GrantSet

# STAGES now lives in policy.py (#679) so the settings schema can reach it
# without importing this tomlkit-backed module. Re-exported here because
# `settings.STAGES` is this module's historical name for it; the pin is
# load-bearing — without it ruff F401 autofix deletes the re-export.
from ..policy import STAGES  # noqa: F401 — re-export


class PolicyDoc:
    """A policy.toml document under edit. Sections may be dotted
    ("adapter.dev") to address the per-stage override tables; a
    "plugins.<name>" section is always the two-part path (see `_path`)."""

    def __init__(self, doc: tomlkit.TOMLDocument):
        self._doc = doc

    @classmethod
    def load(cls, path: Path, *, template_when_missing: bool = True) -> PolicyDoc:
        """``template_when_missing=False`` starts a missing file from an empty
        document instead of POLICY_TEMPLATE — for callers that write a single
        section (e.g. dashboard geometry) and must not materialise every
        default setting into a fresh policy.toml."""
        if path.is_file():
            text = path.read_text(encoding="utf-8")
        else:
            text = policy_mod.POLICY_TEMPLATE if template_when_missing else ""
        return cls(tomlkit.parse(text))

    @staticmethod
    def _path(section: str) -> list[str]:
        """Table path for a section. A ``plugins.<name>`` section splits only
        once: a manifest name is any non-empty string, so ``acme.tools`` is the
        one table ``[plugins."acme.tools"]`` that policy.py's per-plugin parse
        reads, never nested ``[plugins.acme.tools]`` tables."""
        head, dot, name = section.partition(".")
        if head == "plugins" and dot:
            return [head, name]
        return section.split(".")

    def _table(self, section: str, create: bool) -> Any | None:
        return self._walk(self._path(section), create)

    def _walk(self, path: list[str], create: bool) -> Any | None:
        node: Any = self._doc
        for part in path:
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
        """Set a key; None deletes it. A sub-table (per-stage adapter or
        per-plugin) emptied by a delete is dropped entirely, restoring
        'unset = inherit'."""
        if value is None:
            table = self._table(section, create=False)
            if table is not None and key in table:
                del table[key]
            path = self._path(section)
            if len(path) > 1 and table is not None and len(table) == 0:
                # The parent exists: the emptied table was just found under it.
                parent = self._walk(path[:-1], create=False)
                del parent[path[-1]]  # pyright: ignore[reportOptionalSubscript]
            return
        # _table(create=True) never returns None (it creates missing tables), but
        # its return type is Any | None.
        self._table(section, create=True)[key] = value  # pyright: ignore[reportOptionalSubscript]

    def validate(
        self,
        plugin_schemas: dict[str, Any] | None = None,
        project: Path | None = None,
        grants: GrantSet | None = None,
    ) -> str | None:
        """Authoritative validation via policy.loads(); None when valid.

        ``plugin_schemas`` (plugin name -> setting specs) lets the round-trip
        also type-check any [plugins.<name>] tables the screen rendered. When
        ``project`` is given, every enabled in-process plugin additionally
        self-validates against the parsed policy (Plugin.validate) — the same
        coupling check the engine runs at startup (e.g. the Unity plugin's
        editor_mode↔scm.isolation rule) — so an incompatible combination is
        caught at save time rather than mid-run. ``project`` is the discovery
        root (the code root a run loads) and ``grants`` the plugin grants the
        build is judged against (none given = no grants, fail closed), so a
        plugin a launch would refuse — an ungranted project plugin named in
        ``enabled``, or one changed since its grant — fails here with the same
        message (`PluginTrustError` is a `PluginError`) before anything executes.
        Building the registry imports only builtins trusted in [plugins] enabled
        and project plugins granted in ``grants``."""
        try:
            pol = policy_mod.loads(self.dumps(), plugin_schemas=plugin_schemas)
        except policy_mod.PolicyError as e:
            return str(e)
        if project is not None:
            from ..plugins.model import PluginError
            from ..plugins.registry import PluginRegistry

            try:
                PluginRegistry.build(
                    project, pol, grants=grants if grants is not None else GrantSet.empty()
                ).validate(pol)
            except (policy_mod.PolicyError, PluginError) as e:
                return str(e)
        return None

    def dumps(self) -> str:
        return tomlkit.dumps(self._doc)

    def save(self, path: Path, *, confine_root: Path) -> None:
        """#363: via the helper, which removes its temp on any raise. The
        hand-rolled temp was the fixed name `.bmad-loop/policy.toml.tmp` —
        gitignored by nothing, and byte-identical to the one
        `policy.write_mux_backend` built, so the settings editor and the mux
        writer raced on one name. Raises rather than degrades: the screen catches
        OSError to show "save failed", which requires the raise to reach it —
        and `UnconfinedWriteError` is an OSError, so a refusal arrives there too.

        `confine_root` is a REQUIRED keyword rather than a defaulted one, and
        this document does not remember one: a `PolicyDoc` is parsed text with no
        path of its own — `load` and `save` are each handed one per call — so
        there is nothing here to derive a root from. Requiring it makes a caller
        that has not decided what tree this policy file belongs to a type error
        rather than an unconfined write (#593). `runsetup` documents
        `.bmad-loop/policy.toml` as a path a driven session may write, so the
        directories above it are exactly the ones worth anchoring.

        `require_writable_target=True` (#597): policy.toml is hand-edited config,
        and an operator who marks it read-only gets the `PermissionError` a bare
        `Path.write_text` raised before this write went atomic — surfaced by the
        same "save failed" the screen already shows."""
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text_confined(
            path, self.dumps(), confine_root=confine_root, require_writable_target=True
        )
