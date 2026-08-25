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
from conftest import fault_read_text

from bmad_loop.plugins import (
    PluginError,
    PluginRegistry,
    discover,
    get_plugin,
    load_plugins,
)
from bmad_loop.plugins.loader import PLUGIN_FILE, USER_PLUGINS_REL
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
        # root-naming python module path. The value is `.strip()`ed before the guard,
        # so the trailing-space spellings arrive as "." — but the trailing-DOT ones
        # arrive intact, and Win32 trims those to the plugin dir just the same. What
        # gets exec'd matters more than what gets copied, hence the whole family.
        ('[plugin]\nname = "e"\napi_version = 1\n[python]\nmodule = "."\n', "plugin-relative"),
        ('[plugin]\nname = "e"\napi_version = 1\n[python]\nmodule = "..."\n', "plugin-relative"),
        ('[plugin]\nname = "e"\napi_version = 1\n[python]\nmodule = ". ."\n', "plugin-relative"),
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


# The one substring every #480 refusal shares, across all seven guarded config
# sites — a single matcher for the whole family.
_WIN32_ALIAS_MATCH = "must not name a Windows device or end a component in a period or space"


@pytest.mark.parametrize("key", ["seed_files", "seed_globs"])
@pytest.mark.parametrize(
    "value",
    [
        "NUL",  # the bare device — project-relative by every measure the arm above applies
        "sub/CON",  # a non-final component: `_is_reserved_basename` alone reads this False
        "aux.json",  # lowercase and extensioned, the shape a seed entry actually takes
        ".claude/skills.",  # rule 2: Win32 creates `skills`, the manifest still spells `skills.`
        "a/b ",  # the trailing space, identical in shape to the dot since PR #708
    ],
)
def test_manifest_rejects_win32_alias_seed_paths(key, value):
    """Both seed fields refuse a value that names a Windows device or ends a
    component in a period or space.

    The `key` axis is the assertion, not scenery: `seed_files` and `seed_globs` are
    each guarded by one `_check_relative_paths` call, so this proves the single
    shared helper covers both rather than assuming it. Ablation: delete that
    helper's `names_win32_alias` arm and every row of BOTH parametrizations reddens
    together — one helper, two fields — while the `[python] module` test below stays
    green.
    """
    body = f'[plugin]\nname = "e"\napi_version = 1\n{key} = ["{value}"]\n'
    with pytest.raises(PluginError, match=_WIN32_ALIAS_MATCH) as excinfo:
        load_manifest(body, "e/plugin.toml", "e")
    assert key in str(excinfo.value)  # the message names the field it refused


@pytest.mark.parametrize(
    "value",
    [
        "NUL",
        "hooks.",  # Win32 trims to `hooks`, so the import resolves past the file named
        "sub/CON.py",
        "pkg /hooks.py",  # an interior component, which the caller's .strip() cannot reach
    ],
)
def test_manifest_rejects_win32_alias_python_module(value):
    """`[python] module` refuses the same family, and it bites harder here than at
    the seed fields: this value is imported rather than copied, so a spelling Win32
    resolves elsewhere is what gets exec'd. Its own arm — ablate it and only these
    rows redden, while both seed-field parametrizations above stay green."""
    body = f'[plugin]\nname = "e"\napi_version = 1\n[python]\nmodule = "{value}"\n'
    with pytest.raises(PluginError, match=_WIN32_ALIAS_MATCH) as excinfo:
        load_manifest(body, "e/plugin.toml", "e")
    assert "[python] module" in str(excinfo.value)


# ------------------------------------------------------------ manifest reads


def test_non_utf8_plugin_manifest_raises_plugin_error(tmp_path):
    """The READ, not a value coercion. `load_manifest`'s CONVERSION_FAULTS funnel
    cannot reach this one — it wraps the parse, and the decode happens in the
    argument expression at the discovery call site, before `load_manifest` is
    entered — so a non-UTF-8 `plugin.toml` escaped as a raw `UnicodeDecodeError`.
    Asserting the TYPE is the point: `tui/settings.py` degrades on
    `except (PolicyError, PluginError)`, and a `ValueError` escape walked through
    it and took the settings surface down at construction. Sibling guard:
    `adapters/profile.py`'s `_read_profile_text` (#473).

    ABLATION: route the project read back through
    `load_manifest(toml.read_text(encoding="utf-8"), ...)` and this raises
    UnicodeDecodeError instead."""
    pdir = write_plugin(tmp_path, "bad", MINIMAL.format(name="bad"))
    manifest = pdir / PLUGIN_FILE
    manifest.write_bytes(b'[plugin]\nname = "b\xffad"\napi_version = 1\n')
    # Self-verify the fixture before trusting what it proves: a file that decoded
    # fine would make the assertion below pass for the wrong reason.
    with pytest.raises(UnicodeDecodeError):
        manifest.read_text(encoding="utf-8")
    with pytest.raises(PluginError, match="not valid UTF-8") as excinfo:
        load_plugins(tmp_path)
    assert str(manifest) in str(excinfo.value)  # the fault names the file at fault


def test_unreadable_plugin_manifest_raises_plugin_error(tmp_path, monkeypatch):
    """The OSError arm of the same guard: a manifest that is present but cannot be
    read — permissions, an I/O error, a dead mount. Discovery's `is_file()` rules
    out ABSENCE and nothing else, so this escaped as a bare OSError, which no
    consumer of `load_plugins` catches.

    `fault_read_text` rather than chmod for the reason its docstring gives, and
    its targeting matters twice over: a blanket `Path.read_text` patch is answered
    by the BUILTIN loop first, which would redden with this call site untouched.

    ABLATION: route the project read back through `toml.read_text(...)` and this
    raises PermissionError instead."""
    pdir = write_plugin(tmp_path, "proj", MINIMAL.format(name="proj"))
    manifest = pdir / PLUGIN_FILE
    # Precondition: readable, this plugin loads — so the raise below is the fault
    # being converted, not a manifest that was malformed all along.
    assert load_plugins(tmp_path)["proj"].source == "project"
    fault_read_text(monkeypatch, manifest)
    with pytest.raises(PluginError, match="unreadable") as excinfo:
        load_plugins(tmp_path)
    assert str(manifest) in str(excinfo.value)


def test_unreadable_builtin_plugin_manifest_raises_plugin_error(monkeypatch):
    """The packaged built-ins are read through the same guard. They are trusted
    content, so this is not the user-authored fault class the two tests above
    cover — but a corrupt or unreadable install is a PACKAGING bug, and the loader
    owes its callers the typed error that says so rather than a traceback:
    `PluginRegistry.build` and the TUI settings screen both key on PluginError and
    neither catches OSError.

    Here because the two read sites are separate wiring axes, not one — the same
    measurement `adapters/profile.py`'s packaged site forced. Reverting the
    BUILTIN read to `toml.read_text(...)` leaves both project tests green; this is
    the only test that reddens for it, and the project ablation leaves this one
    green. Disjoint, which is the proof that neither site stands in for the
    other."""
    packaged = resources.files("bmad_loop.data").joinpath("plugins")
    names = sorted(e.name for e in packaged.iterdir() if e.is_dir())
    # Real path, not a zip member: `fault_read_text` targets `Path.read_text`, so a
    # zipimported install would leave the fault unarmed. Asserted, not assumed.
    victim = Path(str(packaged.joinpath(names[0], PLUGIN_FILE)))
    assert victim.is_file()
    fault_read_text(monkeypatch, victim)
    with pytest.raises(PluginError, match="unreadable") as excinfo:
        load_plugins()
    assert f"{names[0]}/{PLUGIN_FILE}" in str(excinfo.value)


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
