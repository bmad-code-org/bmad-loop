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
from collections.abc import Callable, Iterator
from functools import partial
from importlib import resources
from pathlib import Path

from . import trust
from .manifest import load_manifest
from .model import PluginError, PluginManifest

PLUGIN_FILE = "plugin.toml"
USER_PLUGINS_REL = Path(".bmad-loop") / "plugins"
ENTRY_POINT_GROUP = "bmad_loop.plugins"


def _builtin_manifest_files() -> list[tuple[str, str, Callable[[], str]]]:
    """(source label, scripts dir, text reader) for every packaged plugin.toml.
    Enumeration only — nothing is read or parsed here, so callers choose their
    own fault granularity."""
    packaged = resources.files("bmad_loop.data").joinpath("plugins")
    if not packaged.is_dir():
        return []
    return [
        (
            f"{entry.name}/{PLUGIN_FILE}",
            str(entry),
            partial(entry.joinpath(PLUGIN_FILE).read_text, encoding="utf-8"),
        )
        for entry in sorted(packaged.iterdir(), key=lambda e: e.name)
        if entry.is_dir() and entry.joinpath(PLUGIN_FILE).is_file()
    ]


def _project_manifest_files(project: Path | None) -> list[tuple[str, str, Callable[[], str]]]:
    """(source label, scripts dir, text reader) for every project plugin.toml."""
    if project is None:
        return []
    user_dir = project / USER_PLUGINS_REL
    if not user_dir.is_dir():
        return []
    return [
        (
            str(entry / PLUGIN_FILE),
            str(entry),
            partial((entry / PLUGIN_FILE).read_text, encoding="utf-8"),
        )
        for entry in sorted(user_dir.iterdir())
        if entry.is_dir() and (entry / PLUGIN_FILE).is_file()
    ]


def _discover_builtin() -> Iterator[PluginManifest]:
    for source, scripts_dir, read in _builtin_manifest_files():
        yield load_manifest(read(), source, scripts_dir, origin="builtin")


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
    for source, scripts_dir, read in _project_manifest_files(project):
        yield load_manifest(read(), source, scripts_dir, origin="project")


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

    This is the EFFECTIVE map: fail-fast, last-write-wins on a name collision,
    filtered to what this build supports. All three are what a run wants and
    all three are wrong for reconstructing history — see
    :func:`discovered_manifests`.
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


def discovered_manifests(project: Path | None = None) -> list[PluginManifest]:
    """Every parseable manifest from every discovered source — a UNION, never
    the effective map, so a builtin and a same-name project plugin BOTH appear
    and no api_version filter is applied.

    Exists for historical reconstruction (`validate`'s #384 shared-exclude
    check), where :func:`load_plugins`'s semantics are each wrong in a
    different way: fail-fast lets one malformed project ``plugin.toml`` zero
    every builtin manifest already yielded; last-write-wins lets a valid
    same-name project plugin remove the builtin's seed paths from the set; and
    ``trust.check_api`` filters by what THIS build supports, while the
    polluting run may have been an older bmad-loop that supported the manifest
    fine. Consumers only ever ADD candidates, so over-inclusion is safe by
    construction.

    Never raises. Enumeration is guarded per SOURCE and read+parse per ITEM —
    one bad file skips only itself, and losing one source's directory cannot
    lose another's. Faults are silent here because the effective loader is the
    one that reports them. A manifest today's schema cannot parse contributes
    nothing — an under-report the #384 detector's docstring already records as
    its safe direction.
    """
    union: list[PluginManifest] = []
    for origin, enumerate_source in (
        ("builtin", _builtin_manifest_files),
        ("project", partial(_project_manifest_files, project)),
    ):
        try:
            files = enumerate_source()
        except OSError:
            continue
        for source, scripts_dir, read in files:
            try:
                union.append(load_manifest(read(), source, scripts_dir, origin=origin))
            except (PluginError, OSError, UnicodeError):
                continue
    # The entry-point seam yields nothing today; consumed under its own guard so
    # wiring it later cannot zero the folder sources. When it IS wired, give it
    # the same per-item granularity as the two above — a generator raise here
    # would drop that source's remaining items.
    try:
        union.extend(_discover_entry_points())
    except (PluginError, OSError, UnicodeError):
        pass
    return union


def get_plugin(name: str, project: Path | None = None) -> PluginManifest:
    plugins = load_plugins(project)
    manifest = plugins.get(name)
    if manifest is None:
        raise PluginError(f"unknown plugin: {name!r} (available: {sorted(plugins)})")
    return manifest
