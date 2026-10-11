"""Plugin trust + api_version compatibility.

Trust is by ORIGIN, then by CONTENT (#496/#497; the verdict itself is
`grants.classify`, enforced once in `PluginRegistry.build`):

  * builtin and entry-point plugins ship with the installed package and are
    trusted by provenance. A builtin's in-process ``[python]`` module is
    imported only when its name is in ``policy.toml [plugins] enabled``; an
    un-enabled one is wholly inert — its hooks, workflows and seeds too.
  * a project-origin plugin (``.bmad-loop/plugins/``) with any executable
    surface — hooks, workflows, ``[python]``, seeds — runs only under an
    operator content GRANT recorded out of the tree. Never granted and not in
    ``enabled``: inert, and the run goes on. Granted but changed since, or
    named in ``enabled`` without a grant: the build raises `PluginTrustError`
    before any plugin executes. A granted plugin runs from a verified snapshot.
    A settings-only project plugin loads freely.

Dropping a folder never auto-runs anything, and a name in ``enabled`` no longer
vouches for whatever dir happens to carry it.

Compatibility is separate from trust: a manifest's ``api_version`` must be in
the framework's ``SUPPORTED_API``. The loader decides the *consequence* of a
mismatch (hard error for a builtin we ship, skip-with-warning for third-party).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .model import SUPPORTED_API, PluginError, PluginManifest


class _HasPlugins(Protocol):
    plugins: "_PluginsPolicyLike"


class _PluginsPolicyLike(Protocol):
    enabled: tuple[str, ...]


class PluginUntrusted(PluginError):
    """Raised when in-process Python is requested for a plugin that is not in
    ``[plugins] enabled``. Caught by the registry, which records the instance as
    untrusted (not constructed) rather than crashing the run."""


@dataclass(frozen=True)
class Refusal:
    """One plugin the registry gate refused: its name, why, its discovery origin
    (``manifest.source``) and its dir (POSIX relpath from the discovery root)."""

    name: str
    reason: str
    origin: str
    path: str


class PluginTrustError(PluginError):
    """Raised by `PluginRegistry.build` when any plugin's verdict is fatal (a
    granted plugin that changed, an ``enabled`` name with no grant, a granted dir
    that can't be read) — before any plugin, builtins included, executes.
    ``refusals`` carries every fatal one; the message carries their fixes."""

    def __init__(self, refusals: tuple[Refusal, ...]):
        self.refusals = refusals
        super().__init__("\n".join(r.reason for r in refusals))


def enabled_names(policy: _HasPlugins | None) -> frozenset[str]:
    """The allowlist from ``[plugins] enabled``; empty when absent."""
    if policy is None:
        return frozenset()
    plugins = getattr(policy, "plugins", None)
    if plugins is None:
        return frozenset()
    return frozenset(getattr(plugins, "enabled", ()) or ())


def is_enabled(policy: _HasPlugins | None, name: str) -> bool:
    return name in enabled_names(policy)


def require_enabled(policy: _HasPlugins | None, name: str) -> None:
    """Gate in-process execution. Raises PluginUntrusted if not allowlisted."""
    if not is_enabled(policy, name):
        raise PluginUntrusted(
            f"plugin {name!r} declares an in-process [python] module but is not in "
            f"[plugins] enabled — its code will not run"
        )


def api_supported(api_version: int) -> bool:
    return api_version in SUPPORTED_API


def check_api(manifest: PluginManifest) -> str | None:
    """Return None if the manifest's api_version is supported, else a human
    message describing the mismatch. The loader maps this to hard-error vs skip
    based on the manifest's source."""
    if api_supported(manifest.api_version):
        return None
    return (
        f"plugin {manifest.name!r} declares api_version {manifest.api_version}, "
        f"unsupported by this build (supports {sorted(SUPPORTED_API)})"
    )
