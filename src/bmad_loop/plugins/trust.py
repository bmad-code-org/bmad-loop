"""Plugin trust + api_version compatibility.

Two tiers, per the design:

  * Data-only / declarative plugins (no ``[python]``) carry no executable Python
    — they load on folder-drop. Their shell hooks are the same risk surface as
    today's ``engine.toml *_cmd`` and run through the bus like any command.
  * A plugin that declares a ``[python]`` module is **never imported or
    executed** unless its name is in ``policy.toml [plugins] enabled = [...]``.
    Dropping a folder must never auto-run code — the registry calls
    ``require_enabled`` before ``exec_module``.

Compatibility is separate from trust: a manifest's ``api_version`` must be in
the framework's ``SUPPORTED_API``. The loader decides the *consequence* of a
mismatch (hard error for a builtin we ship, skip-with-warning for third-party).
"""

from __future__ import annotations

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
