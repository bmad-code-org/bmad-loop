"""Discover + load plugin manifests (folder-drop now; entry-points later).

Discovery walks three sources in overlay precedence — exactly the
builtin-then-project pattern of ``load_profiles`` (adapters/profile.py), with an
entry-point source wedged in the middle as a locked future-additive seam:

    builtin (bmad_loop.data/plugins/*)         lowest precedence
    entry_point (bmad_loop.plugins group)      written, returns nothing today
    project (<project>/.bmad-loop/plugins/*)   highest precedence (same-name override)

Each plugin is a directory holding ``plugin.toml`` plus any helper scripts; the
directory is its ``{scripts}`` dir. Resolving bundled plugins to a real
filesystem path assumes a regular (non-zipped) install — the same assumption the
rest of the package makes for packaged skills/profiles/engines.

api_version mismatch handling lives here because it is source-dependent: a
builtin we ship with the wrong version is a packaging bug (hard error); a
third-party plugin written for a newer/older API is skipped with a warning so a
stale drop-in can never take a run down.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

from . import trust
from .manifest import load_manifest
from .model import PluginError, PluginManifest

PLUGIN_FILE = "plugin.toml"
USER_PLUGINS_REL = Path(".bmad-loop") / "plugins"
ENTRY_POINT_GROUP = "bmad_loop.plugins"


def _discover_builtin() -> Iterator[PluginManifest]:
    packaged = resources.files("bmad_loop.data").joinpath("plugins")
    if not packaged.is_dir():
        return
    for entry in sorted(packaged.iterdir(), key=lambda e: e.name):
        toml = entry.joinpath(PLUGIN_FILE)
        if entry.is_dir() and toml.is_file():
            yield load_manifest(
                toml.read_text(encoding="utf-8"),
                f"{entry.name}/{PLUGIN_FILE}",
                str(entry),
                origin="builtin",
            )


def _discover_entry_points() -> Iterator[PluginManifest]:
    """Future-additive seam for ``importlib.metadata`` entry points (group
    ``bmad_loop.plugins``, the modern selectable API on Python >= 3.11). Locked
    shut for now: folder-drop is the only distribution path, so this yields
    nothing. Wiring it later needs no changes to callers — discovery order and
    overlay precedence already account for this source.
    """
    return
    yield  # pragma: no cover - marks this a generator without emitting


def _discover_project(project: Path) -> Iterator[PluginManifest]:
    user_dir = project / USER_PLUGINS_REL
    if not user_dir.is_dir():
        return
    for entry in sorted(user_dir.iterdir()):
        toml = entry / PLUGIN_FILE
        if entry.is_dir() and toml.is_file():
            yield load_manifest(
                toml.read_text(encoding="utf-8"), str(toml), str(entry), origin="project"
            )


def discover(project: Path | None = None) -> Iterator[PluginManifest]:
    """Yield manifests in overlay order (builtin < entry_point < project).

    Later same-name manifests override earlier ones; ``load_plugins`` collapses
    the stream into a name->manifest dict honoring that precedence.
    """
    yield from _discover_builtin()
    yield from _discover_entry_points()
    if project is not None:
        yield from _discover_project(project)


def load_plugins(project: Path | None = None, *, journal=None) -> dict[str, PluginManifest]:
    """Packaged built-ins overlaid by project-local plugins, api-checked.

    A builtin with an unsupported api_version is a hard error (we shipped it); a
    third-party one is skipped with a warning (and journalled when a journal is
    given) so it can never crash a run.
    """
    plugins: dict[str, PluginManifest] = {}
    for manifest in discover(project):
        problem = trust.check_api(manifest)
        if problem is not None:
            if manifest.source == "builtin":
                raise PluginError(problem)
            warnings.warn(problem, stacklevel=2)
            if journal is not None:
                journal.append("plugin-skipped", plugin=manifest.name, reason=problem)
            continue
        plugins[manifest.name] = manifest
    return plugins


def get_plugin(name: str, project: Path | None = None) -> PluginManifest:
    plugins = load_plugins(project)
    manifest = plugins.get(name)
    if manifest is None:
        raise PluginError(f"unknown plugin: {name!r} (available: {sorted(plugins)})")
    return manifest
