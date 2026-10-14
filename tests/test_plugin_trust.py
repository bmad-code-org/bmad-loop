"""Trust gate, api_version compatibility, and failure isolation.

The trust invariant is the most important property of the whole system: a
folder-dropped plugin must NEVER make anything run on its own say-so (#496/#497).
A project plugin with an executable surface — hooks, workflows, ``[python]``,
seeds — runs only under an operator content grant, from a verified snapshot,
with its module compiled from the bytes that were hashed. A builtin's
``[python]`` module runs only when its name is in ``[plugins] enabled``; an
un-enabled one is wholly inert. These tests prove the gate, the api_version
policy (builtin mismatch = hard error, third-party = skip-with-warning), and
that a misbehaving in-process plugin is isolated instead of crashing the run.

Every marker is an ABSOLUTE path outside both the live plugin dir and its
snapshot, baked into the plugin's bytes before the grant is taken — a
``__file__``-relative marker would land in the snapshot and prove nothing.
"""

from __future__ import annotations

import importlib.util
import marshal
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import grant_plugin

from bmad_loop import policy as policy_mod
from bmad_loop import runs
from bmad_loop.plugins import (
    GrantSet,
    HookBus,
    HookContext,
    PluginError,
    PluginRegistry,
    PluginTrustError,
    is_enabled,
    load_plugins,
)
from bmad_loop.plugins import registry as registry_mod
from bmad_loop.plugins import (
    require_enabled,
)
from bmad_loop.plugins import snapshot as snapshot_mod
from bmad_loop.plugins import (
    trust,
)
from bmad_loop.plugins.grants import TrustState
from bmad_loop.plugins.loader import USER_PLUGINS_REL
from bmad_loop.plugins.manifest import load_manifest
from bmad_loop.plugins.model import WORKFLOW_STAGES
from bmad_loop.plugins.trust import PluginUntrusted
from bmad_loop.policy import PluginsPolicy, Policy

BUILTIN_UNITY = Path(registry_mod.__file__).resolve().parents[1] / "data" / "plugins" / "unity"

# A [python] plugin whose module writes an absolute marker the instant it is
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

Path({marker!r}).write_text({tag!r})

class {cls}(Plugin):
    pass
"""

# A declarative hook script writing an absolute marker; run as `python <script>`.
HOOK_SCRIPT = "from pathlib import Path\nPath({marker!r}).write_text({tag!r})\n"


class FakeJournal:
    """Collects journal entries without touching disk."""

    def __init__(self):
        self.entries: list[dict] = []

    def append(self, kind, **fields):
        self.entries.append({"kind": kind, **fields})

    def kinds(self):
        return [e["kind"] for e in self.entries]


def marker_for(root: Path, name: str) -> Path:
    """An absolute marker beside (never inside) the plugins dir."""
    return root / f"IMPORTED-{name}"


def write_py_plugin(
    root: Path, name: str, *, cls: str = "Plugin", module: str | None = None
) -> Path:
    pdir = root / USER_PLUGINS_REL / name
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text(PY_MANIFEST.format(name=name, cls=cls))
    body = PY_MODULE.format(cls=cls, marker=str(marker_for(root, name)), tag="yes")
    (pdir / "hooks.py").write_text(module if module is not None else body)
    return pdir


def hook_cmd(script: str = "hook.py") -> str:
    # a TOML literal string: the interpreter and `{scripts}` are both quoted
    return f'\'"{sys.executable}" "{{scripts}}/{script}"\''


def write_hook_plugin(root: Path, name: str, marker: Path, *, stage: str = "pre_run", extra=""):
    pdir = root / USER_PLUGINS_REL / name
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text(
        f'[plugin]\nname = "{name}"\napi_version = 1\n{extra}'
        f"[hooks.{stage}]\ncmd = {hook_cmd()}\n"
    )
    (pdir / "hook.py").write_text(HOOK_SCRIPT.format(marker=str(marker), tag="ran"))
    return pdir


def enable(*names: str) -> Policy:
    return Policy(plugins=PluginsPolicy(enabled=tuple(names)))


def emit(reg: PluginRegistry, stage: str, root: Path) -> None:
    """Dispatch ``stage`` through a real bus with the real subprocess runner."""
    HookBus(reg).emit(stage, HookContext(stage, run_id="r", repo_root=str(root)))


def record_instantiate(monkeypatch) -> list[str]:
    """Record every plugin the registry tries to import/construct."""
    calls: list[str] = []
    real = registry_mod._instantiate

    def spy(manifest, settings, tree):
        calls.append(manifest.name)
        return real(manifest, settings, tree)

    monkeypatch.setattr(registry_mod, "_instantiate", spy)
    return calls


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
    write_py_plugin(tmp_path, "spy")
    journal = FakeJournal()
    reg = PluginRegistry.build(tmp_path, policy=Policy(), journal=journal)  # no grant
    lp = reg.get("spy")
    assert lp is not None
    assert lp.instance is None and lp.trusted is False and not lp.active
    assert lp.verdict is not None and lp.verdict.state is TrustState.REFUSED
    # the smoking gun: the module's import-time side effect never happened
    assert not marker_for(tmp_path, "spy").exists()
    refused = [e for e in journal.entries if e["kind"] == "plugin-refused"]
    assert [(e["plugin"], e["origin"], e["path"]) for e in refused] == [
        ("spy", "project", ".bmad-loop/plugins/spy")
    ]
    assert [r.name for r in reg.refusals] == ["spy"]
    assert reg.instances() == []


def test_unenabled_builtin_python_is_untrusted_not_refused(tmp_path, monkeypatch):
    # the builtin twin: an un-enabled builtin [python] plugin keeps the
    # plugin-untrusted record, and is never a refusal (a stock run journals none)
    calls = record_instantiate(monkeypatch)
    journal = FakeJournal()
    reg = PluginRegistry.build(tmp_path, policy=Policy(), journal=journal)
    lp = reg.get("unity")
    assert lp is not None and lp.instance is None and lp.trusted is False and not lp.active
    assert lp.verdict is not None and lp.verdict.state is TrustState.UNENABLED
    untrusted = [e["plugin"] for e in journal.entries if e["kind"] == "plugin-untrusted"]
    assert "unity" in untrusted
    assert "plugin-refused" not in journal.kinds() and reg.refusals == ()
    assert "unity" not in calls


def test_ungranted_project_python_never_imports(tmp_path, monkeypatch):
    write_py_plugin(tmp_path, "spy")
    calls = record_instantiate(monkeypatch)
    reg = PluginRegistry.build(tmp_path, policy=Policy())
    assert "spy" not in calls and reg.instances() == []
    assert not marker_for(tmp_path, "spy").exists()


def test_enabled_python_module_is_constructed(tmp_path):
    pdir = write_py_plugin(tmp_path, "trusted")
    journal = FakeJournal()
    grants = grant_plugin(tmp_path, "trusted")
    reg = PluginRegistry.build(tmp_path, policy=enable("trusted"), journal=journal, grants=grants)
    lp = reg.get("trusted")
    assert lp.instance is not None and lp.trusted and not lp.disabled and lp.active
    assert lp.instance.name == "trusted"
    assert marker_for(tmp_path, "trusted").exists()  # now it ran
    assert "plugin-loaded" in journal.kinds()
    assert reg.instances() == [lp.instance]
    # it ran from its verified snapshot, and the live dir is remembered
    assert lp.live_dir == str(pdir)
    assert Path(lp.manifest.scripts_dir).parent == runs.plugin_snapshots_root(tmp_path)


def test_dataonly_plugin_hooks_are_refused_without_a_grant(tmp_path):
    # no [python], but a hook is an executable surface: ungranted → inert (#497)
    (tmp_path / USER_PLUGINS_REL / "decl").mkdir(parents=True)
    (tmp_path / USER_PLUGINS_REL / "decl" / "plugin.toml").write_text(
        '[plugin]\nname = "decl"\napi_version = 1\n[hooks.pre_run]\ncmd = "true"\n'
    )
    journal = FakeJournal()
    reg = PluginRegistry.build(tmp_path, policy=Policy(), journal=journal)
    lp = reg.get("decl")
    assert lp.instance is None and not lp.trusted and not lp.active
    assert reg.hooks_for("pre_run") == []
    by_kind = {(e["kind"], e["plugin"]) for e in journal.entries}
    assert ("plugin-refused", "decl") in by_kind and ("plugin-loaded", "decl") not in by_kind


def test_granted_dataonly_plugin_offers_its_hooks(tmp_path):
    (tmp_path / USER_PLUGINS_REL / "decl").mkdir(parents=True)
    (tmp_path / USER_PLUGINS_REL / "decl" / "plugin.toml").write_text(
        '[plugin]\nname = "decl"\napi_version = 1\n[hooks.pre_run]\ncmd = "true"\n'
    )
    journal = FakeJournal()
    grants = grant_plugin(tmp_path, "decl")
    reg = PluginRegistry.build(tmp_path, policy=Policy(), journal=journal, grants=grants)
    lp = reg.get("decl")
    assert lp.instance is None and lp.trusted and lp.active  # trusted-but-codeless
    assert [h.stage for _, h in reg.hooks_for("pre_run")] == ["pre_run"]
    assert "plugin-loaded" in journal.kinds() and reg.refusals == ()


def test_settings_only_project_plugin_loads_without_grant(tmp_path):
    pdir = tmp_path / USER_PLUGINS_REL / "knobs"
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text(
        '[plugin]\nname = "knobs"\napi_version = 1\n'
        '[[settings]]\nkey = "level"\ntype = "int"\ndefault = 3\n'
    )
    journal = FakeJournal()
    reg = PluginRegistry.build(tmp_path, policy=Policy(), journal=journal)
    lp = reg.get("knobs")
    assert lp is not None and lp.trusted and lp.active
    assert lp.verdict is not None and lp.verdict.state is TrustState.NO_SURFACE
    assert lp.manifest.scripts_dir == str(pdir)  # nothing to snapshot
    assert ("knobs", lp.manifest.settings) in reg.settings_schema()
    assert reg.refusals == () and "plugin-refused" not in journal.kinds()


# ------------------------------------------- refused surfaces stay inert


def test_ungranted_project_declarative_hook_never_runs(tmp_path):
    # #497: a stock policy plus a dropped-in declarative plugin. The REAL bus
    # with the REAL subprocess runner must never dispatch its hook.
    marker = tmp_path / "ROGUE-RAN"
    write_hook_plugin(tmp_path, "rogue", marker)
    reg = PluginRegistry.build(tmp_path, policy=Policy())
    bus = HookBus(reg)
    assert not bus.active("pre_run") and bus.active_plugins() == []
    emit(reg, "pre_run", tmp_path)
    assert not marker.exists()


def test_ungranted_project_seeds_never_apply(tmp_path):
    pdir = tmp_path / USER_PLUGINS_REL / "seeder"
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text(
        '[plugin]\nname = "seeder"\napi_version = 1\n'
        'seed_files = [".env.local"]\nseed_globs = [".claude/skills/*"]\n'
    )
    reg = PluginRegistry.build(tmp_path, policy=Policy())
    assert reg.seed_files() == [] and reg.seed_globs() == []
    assert [r.name for r in reg.refusals] == ["seeder"]
    # granted, the same seeds apply
    reg = PluginRegistry.build(tmp_path, policy=Policy(), grants=grant_plugin(tmp_path, "seeder"))
    assert reg.seed_files() == [".env.local"] and reg.seed_globs() == [".claude/skills/*"]


def _fake_builtin(tmp_path: Path, monkeypatch, marker: Path) -> Path:
    """A builtin-origin [python] plugin that ALSO declares a hook, a workflow and
    seeds — the shape whose hooks the bus used to run while un-enabled."""
    bdir = tmp_path / "builtin" / "shipped"
    bdir.mkdir(parents=True)
    (bdir / "hook.py").write_text(HOOK_SCRIPT.format(marker=str(marker), tag="ran"))
    (bdir / "mod.py").write_text("from bmad_loop.plugins import Plugin\n")
    text = (
        '[plugin]\nname = "shipped"\napi_version = 1\nseed_globs = [".shipped/*"]\n'
        f"[hooks.pre_run]\ncmd = {hook_cmd()}\n"
        '[workflows.doc]\nstage = "post_dev_phase"\nprompt = "/doc"\n'
        '[python]\nmodule = "mod.py"\n'
    )
    m = load_manifest(text, "shipped/plugin.toml", str(bdir), origin="builtin")
    monkeypatch.setattr(registry_mod, "load_plugins", lambda project, journal=None: {m.name: m})
    return bdir


def test_unenabled_builtin_python_is_wholly_inert(tmp_path, monkeypatch):
    marker = tmp_path / "SHIPPED-HOOK-RAN"
    _fake_builtin(tmp_path, monkeypatch, marker)
    calls = record_instantiate(monkeypatch)
    reg = PluginRegistry.build(tmp_path, policy=Policy())
    lp = reg.get("shipped")
    assert lp is not None and not lp.active and calls == []
    assert reg.seed_globs() == [] and reg.workflow_stages() == frozenset()
    assert reg.hooks_for("pre_run") == []
    emit(reg, "pre_run", tmp_path)
    assert not marker.exists()  # the third variant: its hook never runs either
    # enabled, the same builtin is live — hooks, seeds and workflows alike
    reg = PluginRegistry.build(tmp_path, policy=enable("shipped"))
    assert reg.get("shipped").instance is not None
    assert reg.seed_globs() == [".shipped/*"] and reg.workflow_stages() == {"post_dev_phase"}
    emit(reg, "pre_run", tmp_path)
    assert marker.read_text() == "ran"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_builtin_module_symlinked_out_of_the_plugin_is_never_executed(tmp_path, monkeypatch):
    # `_parse_python` rejects an absolute, parent-climbing or root-naming module
    # lexically, at manifest load. A perfectly ordinary relative name that happens
    # to be a LINK out of the (builtin, imported-in-place) plugin dir passes all
    # three, so the in-place path re-checks containment after resolution.
    marker = tmp_path / "ESCAPED"
    bdir = _fake_builtin(tmp_path, monkeypatch, tmp_path / "unused")
    stray = tmp_path / "outside" / "evil.py"
    stray.parent.mkdir()
    stray.write_text(PY_MODULE.format(cls="Plugin", marker=str(marker), tag="yes"))
    (bdir / "mod.py").unlink()
    (bdir / "mod.py").symlink_to(stray)
    journal = FakeJournal()
    reg = PluginRegistry.build(tmp_path, policy=enable("shipped"), journal=journal)
    lp = reg.get("shipped")
    assert lp.instance is None and lp.disabled and "escapes" in lp.error
    assert not marker.exists()
    assert "plugin-error" in journal.kinds()


# ------------------------------------------------------- fatal refusals


def test_shadowing_an_enabled_builtin_name_is_fatal(tmp_path, monkeypatch):
    # #496: a project dir named like an enabled builtin used to be exec'd on the
    # builtin's say-so. Now it is a project plugin like any other: no grant → fatal.
    pdir = tmp_path / USER_PLUGINS_REL / "unity"
    pdir.mkdir(parents=True)
    shutil.copy(BUILTIN_UNITY / "plugin.toml", pdir / "plugin.toml")
    marker = marker_for(tmp_path, "unity")
    (pdir / "unity_plugin.py").write_text(
        PY_MODULE.format(cls="UnityPlugin", marker=str(marker), tag="yes")
    )
    calls = record_instantiate(monkeypatch)
    with pytest.raises(PluginTrustError) as exc:
        PluginRegistry.build(tmp_path, policy=enable("unity"))
    assert "bmad-loop plugins enable unity" in str(exc.value)
    assert [(r.name, r.origin, r.path) for r in exc.value.refusals] == [
        ("unity", "project", ".bmad-loop/plugins/unity")
    ]
    assert not marker.exists()
    assert calls == []  # the shadowed builtin's module was never imported either


def test_enabled_but_not_yet_existing_then_dropped_in_is_fatal(tmp_path):
    pol = enable("futureplug")
    reg = PluginRegistry.build(tmp_path, policy=pol)  # no plugin yet: not fatal
    assert reg.get("futureplug") is None
    write_py_plugin(tmp_path, "futureplug")
    with pytest.raises(PluginTrustError, match="plugins enable futureplug"):
        PluginRegistry.build(tmp_path, policy=pol)
    assert not marker_for(tmp_path, "futureplug").exists()


def test_granted_then_edited_is_fatal_and_names_accept(tmp_path):
    pdir = write_py_plugin(tmp_path, "spy")
    grants = grant_plugin(tmp_path, "spy")
    with (pdir / "hooks.py").open("a") as fh:
        fh.write("# edited after the grant\n")
    with pytest.raises(PluginTrustError) as exc:
        PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)
    assert "bmad-loop plugins accept spy" in str(exc.value)
    assert "1 changed" in str(exc.value)
    assert not marker_for(tmp_path, "spy").exists()


def test_granted_then_renamed_in_place_is_fatal_not_silently_dropped(tmp_path):
    """plugin.toml is granted bytes: renaming a granted plugin in its own dir is a
    changed grant, so the launch refuses with the accept pointer for the new name
    rather than dropping the plugin as merely never granted.

    ABLATION: look grants up by name only in `registry._judge` (`grants.get`) and
    the build succeeds with the plugin inert."""
    marker = tmp_path / "RAN-spy"
    pdir = write_hook_plugin(tmp_path, "spy", marker)
    grants = grant_plugin(tmp_path, "spy")
    manifest = pdir / "plugin.toml"
    manifest.write_text(manifest.read_text().replace('name = "spy"', 'name = "spy2"'))
    with pytest.raises(PluginTrustError) as exc:
        PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)
    assert 'granted as "spy", now named "spy2"' in str(exc.value)
    assert "bmad-loop plugins accept spy2" in str(exc.value)
    assert not marker.exists()


def test_granted_override_renamed_while_its_builtin_reappears_is_fatal(tmp_path):
    """Renaming a granted project override of a builtin brings the builtin back
    under the old name. The builtin never reads grants, so it must not hide the
    override's record: the rename is still a changed grant, not a silent drop.

    ABLATION: build the `_judge` claimants from every discovered name (drop the
    `TRUSTED_SOURCES` filter) and the build succeeds with the renamed override inert."""
    marker = tmp_path / "RAN-unity"
    pdir = write_hook_plugin(tmp_path, "unity", marker)
    grants = grant_plugin(tmp_path, "unity")
    manifest = pdir / "plugin.toml"
    manifest.write_text(manifest.read_text().replace('name = "unity"', 'name = "custom-unity"'))
    with pytest.raises(PluginTrustError) as exc:
        PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)
    assert 'granted as "unity", now named "custom-unity"' in str(exc.value)
    assert "bmad-loop plugins accept custom-unity" in str(exc.value)
    assert not marker.exists()


def test_granted_plugin_stripped_of_its_last_hook_is_fatal(tmp_path):
    """Dropping a granted plugin's only hook leaves it with no executable surface.
    That is a changed grant like dropping one of two hooks, never a plugin that
    quietly loads with its gates gone.

    ABLATION: check `exec_surface` before the record in `grants.classify` and the
    build succeeds with the plugin NO_SURFACE."""
    pdir = write_hook_plugin(tmp_path, "gate", tmp_path / "RAN-gate")
    grants = grant_plugin(tmp_path, "gate")
    (pdir / "plugin.toml").write_text('[plugin]\nname = "gate"\napi_version = 1\n')
    with pytest.raises(PluginTrustError) as exc:
        PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)
    assert "no longer has any hooks" in str(exc.value)
    assert "bmad-loop plugins revoke gate" in str(exc.value)


@pytest.mark.parametrize("name", ["gate", "unity"], ids=["project-only", "builtin-override"])
def test_granted_plugin_dir_deleted_is_fatal_not_silently_dropped(tmp_path, name):
    """A granted plugin whose dir is gone leaves a grant nothing claims. The launch
    refuses until it is restored or revoked; deleting a project override of a
    builtin must not quietly hand the name back to the builtin either.

    ABLATION: drop the `unclaimed` refusals from `PluginRegistry.build` and both
    rows build, the plugin gone without a word."""
    marker = tmp_path / f"RAN-{name}"
    pdir = write_hook_plugin(tmp_path, name, marker)
    grants = grant_plugin(tmp_path, name)
    shutil.rmtree(pdir)
    with pytest.raises(PluginTrustError) as exc:
        PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)
    assert f"{USER_PLUGINS_REL.as_posix()}/{name}" in str(exc.value)
    assert f"bmad-loop plugins revoke {name}" in str(exc.value)
    [refusal] = exc.value.refusals
    assert (refusal.name, refusal.origin) == (name, "project")
    # once revoked the launch builds again
    PluginRegistry.build(tmp_path, policy=Policy(), grants=GrantSet.empty())


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_granted_module_swapped_for_a_link_is_fatal_and_never_executed(tmp_path):
    # a link anywhere in a granted tree makes it unreadable: fatal, nothing runs
    pdir = write_py_plugin(tmp_path, "escapee")
    grants = grant_plugin(tmp_path, "escapee")
    stray = tmp_path / "outside" / "evil.py"
    stray.parent.mkdir()
    stray.write_text(PY_MODULE.format(cls="Plugin", marker=str(tmp_path / "EVIL"), tag="yes"))
    (pdir / "hooks.py").unlink()
    (pdir / "hooks.py").symlink_to(stray)
    with pytest.raises(PluginTrustError, match="cannot be verified"):
        PluginRegistry.build(tmp_path, policy=enable("escapee"), grants=grants)
    assert not (tmp_path / "EVIL").exists()
    assert not marker_for(tmp_path, "escapee").exists()


def test_manifest_swapped_back_before_the_hash_is_fatal_and_never_runs(tmp_path, monkeypatch):
    # discovery parses an ungranted hook, then the granted manifest is restored
    # before the tree is hashed: the digest matches, but the manifest the run
    # would execute is not the one that was hashed.
    # ABLATION: drop `_bind_manifest` from `_memo_tree` and the evil hook runs.
    marker = tmp_path / "HOOK-RAN"
    pdir = write_hook_plugin(tmp_path, "hooked", marker)
    granted = (pdir / "plugin.toml").read_text()
    (pdir / "evil.py").write_text(HOOK_SCRIPT.format(marker=str(marker), tag="evil"))
    grants = grant_plugin(tmp_path, "hooked")  # evil.py is granted inert bytes
    (pdir / "plugin.toml").write_text(granted.replace("hook.py", "evil.py"))
    real = registry_mod.read_tree

    def restore_then_read(root, **kw):
        (pdir / "plugin.toml").write_text(granted)
        return real(root, **kw)

    monkeypatch.setattr(registry_mod, "read_tree", restore_then_read)
    with pytest.raises(PluginTrustError, match="changed while it was being verified"):
        reg = PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)
        emit(reg, "pre_run", tmp_path)
    assert not marker.exists()


def test_legacy_engine_fold_does_not_grant_a_project_unity_override(tmp_path):
    # `[engine] name = "unity"` folds into [plugins] enabled — a name, which must
    # not vouch for a project dir that happens to carry it.
    pdir = tmp_path / USER_PLUGINS_REL / "unity"
    pdir.mkdir(parents=True)
    shutil.copy(BUILTIN_UNITY / "plugin.toml", pdir / "plugin.toml")
    marker = marker_for(tmp_path, "unity")
    (pdir / "unity_plugin.py").write_text(
        PY_MODULE.format(cls="UnityPlugin", marker=str(marker), tag="yes")
    )
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text('[engine]\nname = "unity"\n')
    with pytest.warns(DeprecationWarning, match=r"\[engine\]"):
        pol = policy_mod.load(policy_path)
    assert "unity" in pol.plugins.enabled
    with pytest.raises(PluginTrustError, match="plugins enable unity"):
        PluginRegistry.build(tmp_path, policy=pol)
    assert not marker.exists()


def test_build_refusal_raises_before_any_exec(tmp_path, monkeypatch):
    # one fatal verdict stops the build before ANY plugin executes — a granted
    # project module and an enabled builtin alike
    write_py_plugin(tmp_path, "early")
    grants = grant_plugin(tmp_path, "early")
    write_py_plugin(tmp_path, "late")  # enabled, never granted → fatal
    calls = record_instantiate(monkeypatch)
    with pytest.raises(PluginTrustError, match="plugins enable late"):
        PluginRegistry.build(tmp_path, policy=enable("early", "late", "tea"), grants=grants)
    assert calls == []
    assert not marker_for(tmp_path, "early").exists()


def test_granted_plugin_without_a_snapshot_root_is_refused(tmp_path):
    write_py_plugin(tmp_path, "spy")
    grants = replace(grant_plugin(tmp_path, "spy"), snapshot_root=None)
    with pytest.raises(PluginError, match="no snapshot root"):
        PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)
    assert not marker_for(tmp_path, "spy").exists()


def test_build_defaults_to_no_grants(tmp_path):
    write_py_plugin(tmp_path, "spy")
    reg = PluginRegistry.build(tmp_path, policy=Policy(), grants=GrantSet.empty())
    assert [r.name for r in reg.refusals] == ["spy"]


# ------------------------------------------ granted plugins run the snapshot


def test_granted_hook_runs_from_the_snapshot_not_the_live_dir(tmp_path):
    marker = tmp_path / "HOOK-RAN"
    pdir = write_hook_plugin(tmp_path, "hooked", marker)
    grants = grant_plugin(tmp_path, "hooked")
    reg = PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)
    # the agent rewrites the live script after the launch verified it
    (pdir / "hook.py").write_text(HOOK_SCRIPT.format(marker=str(marker), tag="live"))
    emit(reg, "pre_run", tmp_path)
    assert marker.read_text() == "ran"  # the granted bytes ran, not the edit
    lp = reg.get("hooked")
    assert lp.live_dir == str(pdir) and lp.manifest.scripts_dir != str(pdir)


def test_granted_module_executes_the_hashed_bytes(tmp_path, monkeypatch):
    # the bytes compiled are the ones that were hashed: a swap after the hash —
    # of the live module AND the published snapshot copy — never reaches exec
    pdir = write_py_plugin(tmp_path, "spy")
    grants = grant_plugin(tmp_path, "spy")
    evil = PY_MODULE.format(cls="Plugin", marker=str(tmp_path / "EVIL"), tag="evil")
    real = snapshot_mod.materialize

    def materialize_then_swap(tree, root):
        snap = real(tree, root)
        (pdir / "hooks.py").write_text(evil)
        (snap / "hooks.py").write_text(evil)
        return snap

    monkeypatch.setattr(snapshot_mod, "materialize", materialize_then_swap)
    reg = PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)
    assert not (tmp_path / "EVIL").exists()
    assert reg.get("spy").instance is not None
    assert marker_for(tmp_path, "spy").read_text() == "yes"


def test_planted_pycache_never_executes(tmp_path, monkeypatch):
    # SourceFileLoader trusts a __pycache__ .pyc whose recorded source mtime and
    # size match. Plant one in the snapshot after it verified: compile-from-bytes
    # never consults it.
    write_py_plugin(tmp_path, "spy")
    grants = grant_plugin(tmp_path, "spy")
    evil = PY_MODULE.format(cls="Plugin", marker=str(tmp_path / "EVIL"), tag="evil")
    real = snapshot_mod.materialize

    def materialize_then_plant(tree, root):
        snap = real(tree, root)
        source = snap / "hooks.py"
        st = source.stat()
        header = (
            importlib.util.MAGIC_NUMBER
            + (0).to_bytes(4, "little")
            + (int(st.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
            + (st.st_size & 0xFFFFFFFF).to_bytes(4, "little")
        )
        pyc = Path(importlib.util.cache_from_source(str(source)))
        pyc.parent.mkdir(parents=True, exist_ok=True)
        pyc.write_bytes(header + marshal.dumps(compile(evil, str(source), "exec")))
        return snap

    monkeypatch.setattr(snapshot_mod, "materialize", materialize_then_plant)
    reg = PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)
    assert not (tmp_path / "EVIL").exists()
    assert reg.get("spy").instance is not None
    assert marker_for(tmp_path, "spy").read_text() == "yes"


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
    grants = grant_plugin(tmp_path, "broken")
    reg = PluginRegistry.build(tmp_path, policy=Policy(), journal=journal, grants=grants)
    lp = reg.get("broken")
    assert lp.instance is None and lp.disabled and "boom on construct" in lp.error
    assert "plugin-error" in journal.kinds()
    assert reg.instances() == []


def test_import_exception_disables_instance_run_survives(tmp_path):
    write_py_plugin(tmp_path, "badimport", module=IMPORT_RAISES)
    journal = FakeJournal()
    grants = grant_plugin(tmp_path, "badimport")
    reg = PluginRegistry.build(tmp_path, policy=Policy(), journal=journal, grants=grants)
    lp = reg.get("badimport")
    assert lp.instance is None and lp.disabled
    assert "plugin-error" in journal.kinds()


def test_baseexception_propagates(tmp_path):
    # RunStopped/SIGTERM (BaseException) must NEVER be swallowed by the isolation
    write_py_plugin(tmp_path, "sig", module=BASEEXC_ON_IMPORT)
    grants = grant_plugin(tmp_path, "sig")
    with pytest.raises(KeyboardInterrupt):
        PluginRegistry.build(tmp_path, policy=Policy(), grants=grants)


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
    reg = PluginRegistry.build(tmp_path, policy=Policy(), grants=grant_plugin(tmp_path, "mixed"))
    assert reg.get("mixed").disabled
    assert [h.stage for _, h in reg.hooks_for("pre_run")] == ["pre_run"]


def test_construct_error_keeps_declarative_hooks(tmp_path):
    # the runtime half: the real bus still dispatches a disabled plugin's shell
    # hook, while its workflows and seeds follow the failed module (inert)
    marker = tmp_path / "MIXED-HOOK-RAN"
    pdir = write_hook_plugin(
        tmp_path,
        "mixed",
        marker,
        extra='seed_globs = [".mixed/*"]\n[python]\nmodule = "mod.py"\n',
    )
    (pdir / "mod.py").write_text(CONSTRUCT_RAISES)
    reg = PluginRegistry.build(tmp_path, policy=Policy(), grants=grant_plugin(tmp_path, "mixed"))
    lp = reg.get("mixed")
    assert lp.disabled and lp.active and not lp.live_instance_or_declarative
    assert reg.seed_globs() == []
    emit(reg, "pre_run", tmp_path)
    assert marker.read_text() == "ran"


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


# ------------------------------------------- static workflow roles (DW-380)

# A workflow-bearing project plugin; `[python]` optional. A workflow is an
# executable surface, so the plugin runs only under a grant.
WF_PLUGIN = (
    '[plugin]\nname = "{name}"\napi_version = 1\n{python}'
    '[workflows.{name}_wf]\nstage = "pre_commit_gate"\nrole = "{role}"\nprompt = "/{name}"\n'
)
WF_PYTHON = '[python]\nmodule = "mod.py"\nclass = "Plugin"\n'
WF_MODULE_OK = "from bmad_loop.plugins import Plugin as _Base\nclass Plugin(_Base):\n    pass\n"


def _write_wf_plugin(root: Path, name: str, role: str, *, module: str | None = None) -> None:
    pdir = root / USER_PLUGINS_REL / name
    pdir.mkdir(parents=True)
    python = WF_PYTHON if module is not None else ""
    (pdir / "plugin.toml").write_text(WF_PLUGIN.format(name=name, python=python, role=role))
    if module is not None:
        (pdir / "mod.py").write_text(module)


def _live_workflow_roles(reg: PluginRegistry) -> frozenset[str]:
    """The roles the live registry would actually inject, read through its own
    public consumer (`workflows_for`) at every stage — not a re-derivation."""
    return frozenset(wf.role for stage in WORKFLOW_STAGES for _lp, wf in reg.workflows_for(stage))


@pytest.mark.parametrize(
    ("tea_steps_off", "py_module", "live", "static"),
    [
        # tea UNENABLED, granted declarative review, never-granted dev: only the
        # grant binds a role, so a side admitting REFUSED/UNENABLED shows "dev".
        (False, None, {"review"}, {"review"}),
        # tea enabled with its review-role steps switched off by the overlay and
        # no granted review plugin: only tea's dev-role steps bind.
        (True, None, {"dev"}, {"dev"}),
        # a granted [python] project plugin with a dev workflow, constructed
        (False, WF_MODULE_OK, {"review", "dev"}, {"review", "dev"}),
        # the same plugin whose module fails to construct: live, its workflow goes
        # inert; the static walk still counts it — the one, safe, gap.
        (False, CONSTRUCT_RAISES, {"review"}, {"review", "dev"}),
    ],
    ids=["mixed-trust", "tea-review-steps-off", "granted-python", "python-construct-fails"],
)
def test_static_workflow_roles_match_the_live_registry(
    tmp_path, tea_steps_off, py_module, live, static
):
    """`static_workflow_roles` (preflight verdicts, no exec) is the live registry's
    active-workflow role set exactly whenever every module constructs, and a
    superset only when a `[python]` module fails to — the drift pin for the
    shared `verdict_admits` / `_workflow_enabled` rules the skills preflight reads
    (DW-380)."""
    _write_wf_plugin(tmp_path, "rogue", "dev")  # never granted: REFUSED
    records = []
    if not tea_steps_off:
        _write_wf_plugin(tmp_path, "gate", "review")
        records += grant_plugin(tmp_path, "gate").records
    if py_module is not None:
        _write_wf_plugin(tmp_path, "pyflow", "dev", module=py_module)
        records += grant_plugin(tmp_path, "pyflow").records
    if tea_steps_off:
        off = {"trace_enabled": False, "nfr_enabled": False, "review_enabled": False}
        pol = Policy(plugins=PluginsPolicy(enabled=("tea",), settings={"tea": off}))
    else:
        pol = Policy()  # tea UNENABLED: its dev and review workflows both inert
    grants = GrantSet(records=tuple(records), snapshot_root=runs.plugin_snapshots_root(tmp_path))

    reg = PluginRegistry.build(tmp_path, policy=pol, grants=grants)
    live_roles = _live_workflow_roles(reg)
    static_roles = registry_mod.static_workflow_roles(tmp_path, pol, grants)
    assert live_roles == live
    assert static_roles == static
    assert live_roles <= static_roles
    assert [r.name for r in reg.refusals] == ["rogue"]
