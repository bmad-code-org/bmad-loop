"""Trust gate, api_version compatibility, and failure isolation.

The trust invariant is the most important property of the whole system: a
folder-dropped plugin that declares an in-process [python] module must NEVER
have that module imported or executed unless its name is in [plugins] enabled.
These tests prove the gate (no allowlist -> no import), the api_version policy
(builtin mismatch = hard error, third-party = skip-with-warning), and that a
misbehaving in-process plugin is isolated instead of crashing the run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bmad_loop.plugins import (
    PluginError,
    PluginRegistry,
    is_enabled,
    load_plugins,
    require_enabled,
    trust,
)
from bmad_loop.plugins.loader import USER_PLUGINS_REL
from bmad_loop.plugins.trust import PluginUntrusted
from bmad_loop.policy import PluginsPolicy, Policy

# A [python] plugin whose module touches a marker file the instant it is
# imported, so a test can assert it was (or was never) executed. Its Plugin
# subclass is constructed by the registry only when trusted.
PY_MANIFEST = """
[plugin]
name = "{name}"
api_version = 1
[python]
module = "hooks.py"
class = "{cls}"
"""

PY_MODULE = """
from pathlib import Path
from bmad_loop.plugins import Plugin

Path(__file__).with_name("IMPORTED").write_text("yes")

class {cls}(Plugin):
    pass
"""


class FakeJournal:
    """Collects journal entries without touching disk."""

    def __init__(self):
        self.entries: list[dict] = []

    def append(self, kind, **fields):
        self.entries.append({"kind": kind, **fields})

    def kinds(self):
        return [e["kind"] for e in self.entries]


def write_py_plugin(
    root: Path, name: str, *, cls: str = "Plugin", module: str | None = None
) -> Path:
    pdir = root / USER_PLUGINS_REL / name
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text(PY_MANIFEST.format(name=name, cls=cls))
    (pdir / "hooks.py").write_text(module if module is not None else PY_MODULE.format(cls=cls))
    return pdir


def enable(*names: str) -> Policy:
    return Policy(plugins=PluginsPolicy(enabled=tuple(names)))


# ------------------------------------------------------------ allowlist


def test_enabled_helpers():
    pol = enable("a", "b")
    assert is_enabled(pol, "a") and not is_enabled(pol, "c")
    require_enabled(pol, "a")  # no raise
    with pytest.raises(PluginUntrusted, match="not in"):
        require_enabled(pol, "c")
    # None policy / absent table => nothing trusted
    assert not is_enabled(None, "a")
    assert not is_enabled(Policy(), "a")


# ----------------------------------------------- THE trust invariant


def test_untrusted_python_module_is_never_imported(tmp_path):
    pdir = write_py_plugin(tmp_path, "spy")
    journal = FakeJournal()
    reg = PluginRegistry.build(tmp_path, policy=Policy(), journal=journal)  # not enabled
    lp = reg.get("spy")
    assert lp is not None
    assert lp.instance is None and lp.trusted is False
    # the smoking gun: the module's import-time side effect never happened
    assert not (pdir / "IMPORTED").exists()
    assert "plugin-untrusted" in journal.kinds()
    assert reg.instances() == []


def test_enabled_python_module_is_constructed(tmp_path):
    pdir = write_py_plugin(tmp_path, "trusted")
    journal = FakeJournal()
    reg = PluginRegistry.build(tmp_path, policy=enable("trusted"), journal=journal)
    lp = reg.get("trusted")
    assert lp.instance is not None and lp.trusted and not lp.disabled
    assert lp.instance.name == "trusted"
    assert (pdir / "IMPORTED").exists()  # now it ran
    assert "plugin-loaded" in journal.kinds()
    assert reg.instances() == [lp.instance]


def test_dataonly_plugin_loads_without_enable(tmp_path):
    # no [python] => declarative; loads + is available with no allowlist entry
    (tmp_path / USER_PLUGINS_REL / "decl").mkdir(parents=True)
    (tmp_path / USER_PLUGINS_REL / "decl" / "plugin.toml").write_text(
        '[plugin]\nname = "decl"\napi_version = 1\n[hooks.pre_run]\ncmd = "true"\n'
    )
    journal = FakeJournal()
    reg = PluginRegistry.build(tmp_path, policy=Policy(), journal=journal)
    lp = reg.get("decl")
    assert lp.instance is None and lp.trusted  # trusted-but-codeless
    assert [h.stage for _, h in reg.hooks_for("pre_run")] == ["pre_run"]
    assert "plugin-loaded" in journal.kinds()


# --------------------------------------------------- api_version policy


def test_builtin_api_mismatch_is_hard_error(tmp_path, monkeypatch):
    # pretend this build supports only api 2: the shipped builtin (api 1) is a
    # packaging bug we must surface loudly, not skip.
    monkeypatch.setattr(trust, "SUPPORTED_API", frozenset({2}))
    with pytest.raises(PluginError, match="unsupported by this build"):
        load_plugins()


def test_thirdparty_api_mismatch_is_skipped_with_warning(tmp_path):
    pdir = tmp_path / USER_PLUGINS_REL / "future"
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text('[plugin]\nname = "future"\napi_version = 999\n')
    journal = FakeJournal()
    with pytest.warns(UserWarning, match="unsupported by this build"):
        plugins = load_plugins(tmp_path, journal=journal)
    assert "future" not in plugins  # skipped, run survives
    assert "example" in plugins  # the compatible builtin still loaded
    assert "plugin-skipped" in journal.kinds()


# ----------------------------------------------- failure isolation


CONSTRUCT_RAISES = """
from bmad_loop.plugins import Plugin as _Base
class Plugin(_Base):
    def __init__(self, manifest, settings):
        raise RuntimeError("boom on construct")
"""

IMPORT_RAISES = "raise RuntimeError('boom on import')\n"

BASEEXC_ON_IMPORT = "raise KeyboardInterrupt('sigint-like')\n"


def test_construct_exception_disables_instance_run_survives(tmp_path):
    write_py_plugin(tmp_path, "broken", module=CONSTRUCT_RAISES)
    journal = FakeJournal()
    reg = PluginRegistry.build(tmp_path, policy=enable("broken"), journal=journal)  # no raise
    lp = reg.get("broken")
    assert lp.instance is None and lp.disabled and "boom on construct" in lp.error
    assert "plugin-error" in journal.kinds()
    assert reg.instances() == []


def test_import_exception_disables_instance_run_survives(tmp_path):
    write_py_plugin(tmp_path, "badimport", module=IMPORT_RAISES)
    journal = FakeJournal()
    reg = PluginRegistry.build(tmp_path, policy=enable("badimport"), journal=journal)
    lp = reg.get("badimport")
    assert lp.instance is None and lp.disabled
    assert "plugin-error" in journal.kinds()


def test_baseexception_propagates(tmp_path):
    # RunStopped/SIGTERM (BaseException) must NEVER be swallowed by the isolation
    write_py_plugin(tmp_path, "sig", module=BASEEXC_ON_IMPORT)
    with pytest.raises(KeyboardInterrupt):
        PluginRegistry.build(tmp_path, policy=enable("sig"))


def test_disabled_python_still_offers_declarative_hooks(tmp_path):
    # a [python] plugin that also declares a shell hook: even if its module
    # fails to construct, the out-of-process hook stays available to the bus.
    pdir = tmp_path / USER_PLUGINS_REL / "mixed"
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text(
        '[plugin]\nname = "mixed"\napi_version = 1\n'
        '[hooks.pre_run]\ncmd = "true"\n'
        '[python]\nmodule = "hooks.py"\n'
    )
    (pdir / "hooks.py").write_text(CONSTRUCT_RAISES)
    reg = PluginRegistry.build(tmp_path, policy=enable("mixed"))
    assert reg.get("mixed").disabled
    assert [h.stage for _, h in reg.hooks_for("pre_run")] == ["pre_run"]
