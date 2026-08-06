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
    discovered_manifests,
    get_plugin,
    load_plugins,
)
from bmad_loop.plugins.loader import USER_PLUGINS_REL
from bmad_loop.plugins.manifest import load_manifest

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
        # `inf` reaches api_version's own guard as OverflowError. The load_manifest
        # funnel would catch it either way, so this row is what keeps the field's
        # specific message (rather than the funnel's generic one) — ablate the
        # guard's tuple back to (TypeError, ValueError) and only this row reddens.
        ('[plugin]\nname = "e"\napi_version = inf\n', "api_version must be an integer"),
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
        # root-naming seed path — "" is only one spelling. These feed the same
        # provision_worktree seed loop, where a root ref copies the whole repo into
        # the worktree and the copy then recurses into its own destination.
        ('[plugin]\nname = "e"\napi_version = 1\nseed_files = ["."]\n', "seed_files"),
        ('[plugin]\nname = "e"\napi_version = 1\nseed_files = ["./"]\n', "seed_files"),
        ('[plugin]\nname = "e"\napi_version = 1\nseed_globs = ["."]\n', "seed_globs"),
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


def test_discovered_manifests_union_keeps_both_sides_of_a_same_name_override(tmp_path):
    """A project plugin named after a builtin EVICTS it from the effective map —
    correct for a run, wrong for `validate`'s historical reconstruction, where
    the builtin's seed paths may be exactly what an older run wrote into the
    shared exclude. The union carries both."""
    write_plugin(
        tmp_path,
        "unity",
        '[plugin]\nname = "unity"\napi_version = 1\nseed_files = ["vendored/mine.json"]\n',
    )

    union = [m for m in discovered_manifests(tmp_path) if m.name == "unity"]

    assert {m.source for m in union} == {"builtin", "project"}
    # the contrast that makes the union necessary: the effective map collapses
    # the pair to the project one, losing the builtin's seed_globs
    assert load_plugins(tmp_path)["unity"].source == "project"


def test_discovered_manifests_skips_only_the_malformed_file(tmp_path):
    """One bad project plugin.toml skips ITSELF — not the builtins, and not its
    project siblings. `a-broken` sorts FIRST within the project dir, so a guard
    that stops that source at the first fault (a `break`, or fail-fast) also
    loses `b-good` — which is what the "b-good" assert catches.

    Two breakage shapes on purpose: `a-broken` fails at the TOML layer,
    `a2-badfield` is valid TOML whose field value hits a raw conversion — only
    the `load_manifest` funnel makes that one a PluginError the per-item guard
    can catch. Ablation: drop the funnel arm and this ERRORS (bare ValueError
    out of `discovered_manifests`) instead of skipping the file."""
    write_plugin(tmp_path, "a-broken", "[plugin]\nname = \n")
    write_plugin(
        tmp_path, "a2-badfield", '[plugin]\nname = "a2-badfield"\napi_version = 1\npriority = "x"\n'
    )
    write_plugin(tmp_path, "b-good", MINIMAL.format(name="b-good"))
    # non-vacuity: the effective loader really refuses this project dir, so the
    # union below cannot be riding on a fixture that never exercised the guards
    with pytest.raises(PluginError):
        load_plugins(tmp_path)

    names = {m.name for m in discovered_manifests(tmp_path)}

    assert {"example", "tea", "unity", "b-good"} <= names
    assert "a-broken" not in names
    assert "a2-badfield" not in names


def test_discovered_manifests_apply_no_api_version_gate(tmp_path):
    """`trust.check_api` filters by what THIS build supports — yet another
    effective-config narrowing: the polluting run may have been an older
    bmad-loop that loaded the manifest fine, and its seed lines still need a
    candidate today. The warns-context doubles as the non-vacuity pin — the
    effective loader really does gate this manifest out (pyproject sets no
    filterwarnings, so the warning surfaces; precedent: test_plugin_trust)."""
    write_plugin(tmp_path, "fromfuture", '[plugin]\nname = "fromfuture"\napi_version = 999\n')
    with pytest.warns(UserWarning, match="unsupported by this build"):
        plugins = load_plugins(tmp_path)
    assert "fromfuture" not in plugins

    assert "fromfuture" in {m.name for m in discovered_manifests(tmp_path)}


@pytest.mark.parametrize("key", ["seed_files", "seed_globs"])
@pytest.mark.parametrize(
    ("value", "match"),
    [
        ('""', "must be a list of paths"),
        ('"foo"', "must be a list of paths"),
        ("5", "must be a list of paths"),
        ("[1]", "entries must be strings"),
    ],
)
def test_seed_list_shapes_rejected(key, value, match):
    """Shape before entries, mirroring the sibling seed sources (policy.py
    `worktree_seed`, profile `str_list`): a bare string iterates into
    per-character entries that each pass the per-entry path guard, and a scalar
    used to leak a raw TypeError out of `loads` where every other malformed
    value raises PluginError."""
    body = f'[plugin]\nname = "e"\napi_version = 1\n{key} = {value}\n'
    with pytest.raises(PluginError, match=match):
        load_manifest(body, "e/plugin.toml", "e")


@pytest.mark.parametrize(
    "tail",
    [
        'priority = "x"',  # int() -> ValueError
        "priority = [1]",  # int() -> TypeError
        '[hooks.pre_session]\ncmd = "true"\ntimeout_sec = "x"',  # int() -> ValueError
        '[[settings]]\nkey = "k"\ntype = "select"\noptions = 5',  # iteration -> TypeError
        "priority = inf",  # int() -> OverflowError
        "priority = -inf",  # int() -> OverflowError
        "priority = nan",  # int() -> ValueError
        '[hooks.pre_session]\ncmd = "true"\ntimeout_sec = inf',  # int() -> OverflowError
    ],
)
def test_malformed_field_values_raise_plugin_error(tail):
    """TOML-legal values of the wrong TYPE hit raw conversions (`int()`, and
    iteration over a setting's `options`) that `api_version`'s own guard shows
    were only ever handled one field at a time. The `load_manifest` funnel turns
    those bare escapes into PluginError, which is what every consumer's fault
    handling keys on.

    `inf` is the row that shows why the funnel must be the CLOSED set rather than
    the types seen so far: `int(float('inf'))` raises OverflowError, a sibling of
    neither ValueError nor TypeError, so the first version of this funnel let it
    through. Ablation: drop the funnel arm and every row raises the bare exception;
    drop OverflowError alone and exactly the three `inf` rows do."""
    body = f'[plugin]\nname = "e"\napi_version = 1\n{tail}\n'
    with pytest.raises(PluginError, match="malformed field value"):
        load_manifest(body, "e/plugin.toml", "e")


# every type `tomllib` can yield, plus the numeric spellings that are legal TOML
# and hostile to a raw coercion
TOML_VALUE_DOMAIN = [
    '"x"',
    "1",
    "1.5",
    "true",
    "1979-05-27T07:32:00Z",
    "1979-05-27",
    "07:32:00",
    "[1]",
    "{ k = 1 }",
    "inf",
    "-inf",
    "nan",
    "9" * 400,  # tomllib keeps arbitrary precision; float() of this overflows
]


@pytest.mark.parametrize("value", TOML_VALUE_DOMAIN)
def test_every_toml_value_type_parses_or_raises_plugin_error(value):
    """The closure pin behind `CONVERSION_FAULTS`, and the reason that tuple is a
    funnel rather than one more round of enumeration: a manifest field can hold
    any of the nine types `tomllib` yields, and the contract is that each either
    parses or raises the DOMAIN error — never a bare conversion fault.

    This is what a per-type exception list cannot promise: the last two review
    rounds each added a type after a bot found a spelling nobody had tried. A new
    coercion that raises something outside the set reddens this without anyone
    having to think of the spelling first."""
    body = f'[plugin]\nname = "e"\napi_version = 1\npriority = {value}\n'
    try:
        load_manifest(body, "e/plugin.toml", "e")
    except PluginError:
        pass


def test_registry_orders_by_priority(tmp_path):
    write_plugin(tmp_path, "hi", '[plugin]\nname = "hi"\napi_version = 1\npriority = 10\n')
    write_plugin(tmp_path, "lo", '[plugin]\nname = "lo"\napi_version = 1\npriority = -5\n')
    reg = PluginRegistry.build(tmp_path)
    names = [lp.name for lp in reg.plugins()]
    assert names.index("lo") < names.index("hi")  # lower priority first
