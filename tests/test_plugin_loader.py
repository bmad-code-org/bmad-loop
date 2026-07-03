"""Plugin manifest parsing, discovery/overlay precedence, and packaging.

The trust + in-process-execution surface lives in test_plugin_trust.py; this
file covers the data path: a folder-dropped plugin.toml parses to an immutable
manifest, builtins are overlaid by project-local plugins, and the builtin
plugins dir ships in an installed context.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from bmad_loop.plugins import (
    PluginError,
    PluginRegistry,
    discover,
    get_plugin,
    load_plugins,
)
from bmad_loop.plugins.loader import USER_PLUGINS_REL

# --------------------------------------------------------------- helpers


def write_plugin(root: Path, name: str, body: str, *, files: dict[str, str] | None = None) -> Path:
    """Drop a project-local plugin directory under <root>/.bmad-loop/plugins."""
    pdir = root / USER_PLUGINS_REL / name
    pdir.mkdir(parents=True)
    (pdir / "plugin.toml").write_text(body)
    for rel, text in (files or {}).items():
        (pdir / rel).write_text(text)
    return pdir


MINIMAL = """
[plugin]
name = "{name}"
api_version = 1
"""

FULL = """
[plugin]
name = "full"
version = "2.1.0"
api_version = 1
description = "everything"
author = "me"
seed_files = [".mcp.json"]
seed_globs = [".claude/skills/*"]
priority = 5

[hooks.pre_session]
cmd = 'python3 "{scripts}/probe.py"'
timeout_sec = 30
blocking = true

[hooks.post_commit]
cmd = "true"

[python]
module = "hooks.py"
class = "MyPlugin"

[[settings]]
key = "strict"
type = "bool"
default = false
help = "be strict"

[[settings]]
key = "mode"
type = "select"
options = ["a", "b"]
default = "a"

[workflows.lint-sweep]
stage = "post_dev_phase"
role = "dev"
prompt = "/lint-sweep {story_key}"
blocking = true
"""


# ------------------------------------------------------------ builtins


def test_builtin_example_plugin_loads():
    plugins = load_plugins()
    assert "example" in plugins
    ex = plugins["example"]
    assert ex.source == "builtin"
    assert ex.python is None  # data-only: no executable code
    assert ex.hooks == ()
    assert [s.key for s in ex.settings] == ["greeting"]
    # scripts_dir points at the bundled plugin dir (for {scripts} substitution)
    assert ex.scripts_dir.replace("\\", "/").endswith("data/plugins/example")


def test_get_plugin_unknown_raises():
    with pytest.raises(PluginError, match="unknown plugin"):
        get_plugin("nope")


def test_packaging_smoke_plugins_dir_present():
    # the builtins dir must ship in an installed context (hatch wheel)
    packaged = resources.files("bmad_loop.data").joinpath("plugins")
    assert packaged.is_dir()
    assert any(e.joinpath("plugin.toml").is_file() for e in packaged.iterdir() if e.is_dir())


# --------------------------------------------------------- parse happy path


def test_full_manifest_parses(tmp_path):
    write_plugin(tmp_path, "full", FULL)
    full = load_plugins(tmp_path)["full"]
    assert (full.version, full.description, full.author, full.priority) == (
        "2.1.0",
        "everything",
        "me",
        5,
    )
    assert full.seed_files == (".mcp.json",)
    assert full.seed_globs == (".claude/skills/*",)
    # hooks keyed by stage; placeholder + flags carried through
    pre = full.hook_for("pre_session")
    assert pre is not None and pre.blocking is True and pre.timeout_sec == 30
    assert "{scripts}" in pre.cmd and "{scripts}" not in full.render(pre.cmd)
    assert full.hook_for("post_commit").blocking is False
    # settings, incl. a select with options
    assert {s.key: s.type for s in full.settings} == {"strict": "bool", "mode": "select"}
    assert next(s for s in full.settings if s.key == "mode").options == ("a", "b")
    # python + provides
    assert full.python.module == "hooks.py" and full.python.cls == "MyPlugin"
    # [workflows.<name>] -> a stage-bound session injection
    assert [w.name for w in full.workflows] == ["lint-sweep"]
    wf = full.workflows[0]
    assert (wf.stage, wf.role, wf.blocking) == ("post_dev_phase", "dev", True)
    assert wf.prompt == "/lint-sweep {story_key}"


# --------------------------------------------------------- rejections


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("[plugin]\napi_version = 1\n", "name"),  # missing name
        ('[plugin]\nname = "e"\n', "api_version"),  # missing api_version
        ('[plugin]\nname = "e"\napi_version = "x"\n', "api_version must be an integer"),
        # duplicate setting key
        (
            '[plugin]\nname = "e"\napi_version = 1\n'
            '[[settings]]\nkey = "k"\ntype = "str"\n'
            '[[settings]]\nkey = "k"\ntype = "int"\n',
            "duplicate setting key",
        ),
        # bad setting type
        (
            '[plugin]\nname = "e"\napi_version = 1\n[[settings]]\nkey = "k"\ntype = "blob"\n',
            "type must be one of",
        ),
        # select with no options
        (
            '[plugin]\nname = "e"\napi_version = 1\n[[settings]]\nkey = "k"\ntype = "select"\n',
            "requires a non-empty",
        ),
        # absolute seed path
        ('[plugin]\nname = "e"\napi_version = 1\nseed_files = ["/etc/passwd"]\n', "seed_files"),
        ('[plugin]\nname = "e"\napi_version = 1\nseed_globs = ["/abs/*"]\n', "seed_globs"),
        # absolute python module path
        ('[plugin]\nname = "e"\napi_version = 1\n[python]\nmodule = "/x.py"\n', "plugin-relative"),
        # hook with no cmd
        (
            '[plugin]\nname = "e"\napi_version = 1\n[hooks.pre_run]\nblocking = true\n',
            "requires a 'cmd'",
        ),
        # workflow bound to a non-injection stage
        (
            '[plugin]\nname = "e"\napi_version = 1\n'
            '[workflows.w]\nstage = "pre_run"\nprompt = "x"\n',
            "stage must be one of",
        ),
        # workflow with an unknown role
        (
            '[plugin]\nname = "e"\napi_version = 1\n'
            '[workflows.w]\nstage = "post_dev_phase"\nrole = "triage"\nprompt = "x"\n',
            "role must be one of",
        ),
        # workflow with no prompt
        (
            '[plugin]\nname = "e"\napi_version = 1\n' '[workflows.w]\nstage = "post_dev_phase"\n',
            "requires a 'prompt'",
        ),
        # missing [plugin] table
        ("[other]\nx = 1\n", "missing \\[plugin\\] table"),
    ],
)
def test_invalid_manifest_rejected(tmp_path, body, match):
    write_plugin(tmp_path, "bad", body)
    with pytest.raises(PluginError, match=match):
        load_plugins(tmp_path)


def test_invalid_toml_rejected(tmp_path):
    write_plugin(tmp_path, "broken", "[plugin]\nname = \n")
    with pytest.raises(PluginError, match="invalid TOML"):
        load_plugins(tmp_path)


# ----------------------------------------------------- discovery / overlay


def test_project_overlay_extends_builtins(tmp_path):
    write_plugin(tmp_path, "proj", MINIMAL.format(name="proj"))
    plugins = load_plugins(tmp_path)
    assert "proj" in plugins and "example" in plugins  # overlay extends, doesn't replace
    assert plugins["proj"].source == "project"
    assert plugins["proj"].scripts_dir == str(tmp_path / USER_PLUGINS_REL / "proj")


def test_project_same_name_overrides_builtin(tmp_path):
    # a project plugin named "example" wins over the builtin (highest precedence)
    write_plugin(
        tmp_path, "example", '[plugin]\nname = "example"\nversion = "9.9.9"\napi_version = 1\n'
    )
    ex = load_plugins(tmp_path)["example"]
    assert ex.source == "project" and ex.version == "9.9.9"


def test_discover_order_is_builtin_then_project(tmp_path):
    write_plugin(tmp_path, "zeta", MINIMAL.format(name="zeta"))
    sources = [m.source for m in discover(tmp_path)]
    # every builtin precedes every project plugin (entry-point seam is empty)
    assert sources == sorted(sources, key=lambda s: 0 if s == "builtin" else 1)
    assert "builtin" in sources and sources[-1] == "project"


def test_registry_orders_by_priority(tmp_path):
    write_plugin(tmp_path, "hi", '[plugin]\nname = "hi"\napi_version = 1\npriority = 10\n')
    write_plugin(tmp_path, "lo", '[plugin]\nname = "lo"\napi_version = 1\npriority = -5\n')
    reg = PluginRegistry.build(tmp_path)
    names = [lp.name for lp in reg.plugins()]
    assert names.index("lo") < names.index("hi")  # lower priority first
