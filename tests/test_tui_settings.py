"""PolicyDoc edit-model semantics (comment preservation, unset-vs-empty,
stage-table lifecycle, authoritative validation) plus Pilot tests proving the
settings screen produces a minimal diff on save and blocks invalid values.

Structural assertions parse the dumped TOML instead of substring-matching:
POLICY_TEMPLATE contains commented example lines like '# [adapter.dev]' that
make substring checks lie."""

from __future__ import annotations

import tomllib

from test_tui_app import until
from textual.widgets import Collapsible, Input, Select, Switch

from bmad_loop import policy as policy_mod
from bmad_loop.plugins import load_plugins
from bmad_loop.policy import POLICY_FILE, POLICY_TEMPLATE
from bmad_loop.tui.app import BmadLoopApp
from bmad_loop.tui.screens.dashboard import DashboardScreen
from bmad_loop.tui.screens.settings_screen import SettingsScreen
from bmad_loop.tui.settings import PolicyDoc


def fresh_doc(tmp_path) -> PolicyDoc:
    return PolicyDoc.load(tmp_path / "missing-policy.toml")  # template-backed


def test_load_missing_file_starts_from_template(tmp_path):
    doc = fresh_doc(tmp_path)
    assert doc.get("limits", "max_review_cycles") == 3
    assert "# bmad-loop orchestration policy" in doc.dumps()
    assert doc.validate() is None


def test_set_preserves_comments_with_minimal_diff(tmp_path):
    path = tmp_path / "policy.toml"
    path.write_text(POLICY_TEMPLATE, encoding="utf-8")
    doc = PolicyDoc.load(path)
    doc.set("limits", "max_review_cycles", 5)
    doc.save(path)
    new = path.read_text(encoding="utf-8")
    assert "# cache reads bill at ~0.1x" in new
    changed = set(POLICY_TEMPLATE.splitlines()) ^ set(new.splitlines())
    assert changed == {"max_review_cycles = 3", "max_review_cycles = 5"}


def test_clear_deletes_key_and_default_applies(tmp_path):
    doc = fresh_doc(tmp_path)
    doc.set("gates", "mode", None)
    assert doc.get("gates", "mode") is None
    assert "mode" not in tomllib.loads(doc.dumps())["gates"]
    assert policy_mod.loads(doc.dumps()).gates.mode == "per-epic"


def test_stage_table_set_and_clear(tmp_path):
    doc = fresh_doc(tmp_path)
    doc.set("adapter.dev", "model", "opus")
    assert policy_mod.loads(doc.dumps()).adapter.dev.model == "opus"
    doc.set("adapter.dev", "model", None)
    # the emptied stage table is dropped entirely: unset = inherit
    assert "dev" not in tomllib.loads(doc.dumps())["adapter"]
    assert policy_mod.loads(doc.dumps()).adapter.dev.model is None
    # the base [adapter] table is untouched
    assert doc.get("adapter", "name") == "claude"


def test_stage_table_with_remaining_keys_survives_clear(tmp_path):
    doc = fresh_doc(tmp_path)
    doc.set("adapter.review", "name", "codex")
    doc.set("adapter.review", "model", "gpt-5-codex")
    doc.set("adapter.review", "model", None)
    assert tomllib.loads(doc.dumps())["adapter"]["review"] == {"name": "codex"}


def test_extra_args_none_vs_empty(tmp_path):
    doc = fresh_doc(tmp_path)
    assert policy_mod.loads(doc.dumps()).adapter.extra_args is None
    doc.set("adapter", "extra_args", [])
    assert policy_mod.loads(doc.dumps()).adapter.extra_args == ()
    doc.set("adapter", "extra_args", None)
    assert policy_mod.loads(doc.dumps()).adapter.extra_args is None


def test_scalar_added_next_to_existing_stage_table_parses(tmp_path):
    # tomlkit must place a new [adapter] scalar before the [adapter.dev]
    # header, not after it (where TOML would assign it to the stage table)
    doc = fresh_doc(tmp_path)
    doc.set("adapter.dev", "model", "opus")
    doc.set("adapter", "extra_args", ["--foo"])
    pol = policy_mod.loads(doc.dumps())
    assert pol.adapter.extra_args == ("--foo",)
    assert pol.adapter.dev.model == "opus"


def test_adapter_timing_knobs_round_trip(tmp_path):
    # unset -> inherit (None on base and stages); set a number -> persisted;
    # clear -> back to inherit. Mirrors how the TUI int/float collect maps an
    # empty box to None (delete key) and a value to the parsed number.
    doc = fresh_doc(tmp_path)
    base = policy_mod.loads(doc.dumps()).adapter
    assert base.usage_grace_s is None and base.stop_without_result_nudges is None

    doc.set("adapter", "usage_grace_s", 6.0)
    doc.set("adapter.review", "stop_without_result_nudges", 5)
    pol = policy_mod.loads(doc.dumps())
    assert pol.adapter.usage_grace_s == 6.0
    assert pol.adapter.resolved("review").stop_without_result_nudges == 5
    assert doc.validate() is None

    doc.set("adapter", "usage_grace_s", None)
    assert policy_mod.loads(doc.dumps()).adapter.usage_grace_s is None


def test_validate_surfaces_policy_error(tmp_path):
    doc = fresh_doc(tmp_path)
    doc.set("gates", "mode", "bogus")
    error = doc.validate()
    assert error is not None and "gates.mode" in error
    doc.set("gates", "mode", "none")
    assert doc.validate() is None


def test_save_creates_parent_and_leaves_no_tmp(tmp_path):
    path = tmp_path / ".bmad-loop" / "policy.toml"
    doc = PolicyDoc.load(path)
    doc.set("limits", "max_dev_attempts", 3)
    doc.save(path)
    assert policy_mod.load(path).limits.max_dev_attempts == 3
    assert list(path.parent.iterdir()) == [path]


def test_validate_with_project_enforces_plugin_coupling(tmp_path):
    """Passing project= runs every enabled in-process plugin's self-validation
    (the same check the engine runs at startup): unity's editor_mode='shared'
    requires scm.isolation='none', a coupling a flat per-key schema can't express
    and which loads()-only validation therefore misses."""
    schemas = {"unity": load_plugins()["unity"].settings}
    doc = PolicyDoc.load(tmp_path / "missing.toml")  # template-backed
    doc.set("plugins", "enabled", ["unity"])
    doc.set("plugins.unity", "editor_mode", "shared")
    doc.set("scm", "isolation", "worktree")  # invalid with shared
    # per-key validation alone can't see the coupling -> passes
    assert doc.validate(schemas) is None
    # with the project, the plugin's validate() rejects it
    err = doc.validate(schemas, project=tmp_path)
    assert err is not None and "isolation" in err


def test_validate_with_project_accepts_valid_coupling(tmp_path):
    schemas = {"unity": load_plugins()["unity"].settings}
    doc = PolicyDoc.load(tmp_path / "missing.toml")
    doc.set("plugins", "enabled", ["unity"])
    doc.set("plugins.unity", "editor_mode", "per_worktree")
    doc.set("scm", "isolation", "worktree")  # valid per_worktree combo
    assert doc.validate(schemas, project=tmp_path) is None


def test_validate_with_project_skips_disabled_plugin_coupling(tmp_path):
    """A bad coupling for a plugin that is NOT enabled is not flagged — its
    in-process module is never built, so there is nothing to self-validate."""
    schemas = {"unity": load_plugins()["unity"].settings}
    doc = PolicyDoc.load(tmp_path / "missing.toml")
    doc.set("plugins.unity", "editor_mode", "shared")
    doc.set("scm", "isolation", "worktree")  # would be invalid IF unity were enabled
    assert doc.validate(schemas, project=tmp_path) is None


# ----------------------------------------------------------- settings screen


async def open_settings(app, pilot) -> SettingsScreen:
    await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
    await pilot.press("g")
    await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
    # push_screen swaps app.screen before the form's fields mount; a bare screen-type
    # wait lets the first query_one NoMatches on a slow CI runner. The fields mount as
    # one batch, so waiting for any Input gates every later query_one in the callers.
    await until(pilot, lambda: bool(app.screen.query(Input)))
    return app.screen


def expand_all(screen) -> None:
    """Sections start collapsed; expand them so inner fields are focusable."""
    for c in screen.query(Collapsible):
        c.collapsed = False


def write_policy(project) -> None:
    path = project.project / POLICY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(POLICY_TEMPLATE, encoding="utf-8")


async def test_settings_screen_saves_minimal_diff(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#limits-max_review_cycles", Input).value = "5"
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        new = (project.project / POLICY_FILE).read_text(encoding="utf-8")
        changed = set(POLICY_TEMPLATE.splitlines()) ^ set(new.splitlines())
        assert changed == {"max_review_cycles = 3", "max_review_cycles = 5"}


async def test_settings_screen_untouched_save_writes_nothing_new(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(app, pilot)
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        assert (project.project / POLICY_FILE).read_text(encoding="utf-8") == POLICY_TEMPLATE


async def test_settings_screen_blocks_invalid_value(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#limits-cache_read_weight", Input).value = "5"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        from textual.widgets import Static

        strip = screen.query_one("#errors", Static)
        assert "cache_read_weight" in str(strip.content)
        assert (project.project / POLICY_FILE).read_text(encoding="utf-8") == POLICY_TEMPLATE


async def test_settings_screen_review_toggle_roundtrip(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#review-enabled", Switch).value = False
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.review.enabled is False


async def test_settings_screen_low_frame_rate_toggle_roundtrip(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#tui-low_frame_rate", Switch).value = True
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.tui.low_frame_rate is True


# ----------------------------------------- engine-is-now-a-plugin (Unity) settings


def write_policy_enabling_unity(project) -> None:
    """A policy that turns the Unity engine plugin on, so its settings render
    under [plugins.unity] in the settings screen."""
    path = project.project / POLICY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[plugins]\nenabled = ["unity"]\n', encoding="utf-8")


async def test_settings_screen_unity_roster_reveals_settings_on_enable(project):
    """Unity (a trust-gated [python] plugin) appears in the Plugins roster with an
    enable toggle. While off, its settings sub-collapsible is hidden so the roster
    stays clean; flipping the toggle reveals it live (no save/reopen needed). With
    the policy already enabling unity, its settings are visible on open."""
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        assert screen.query_one("#plugin-enabled-unity", Switch).value is False
        cfg = screen.query_one("#plugin-cfg-unity", Collapsible)
        assert cfg.display is False  # settings hidden until enabled
        screen.query_one("#plugin-enabled-unity", Switch).value = True
        # Switch.Changed reveals the sub-collapsible on a later tick; a single
        # pause() can race that delivery on a slow CI runner, so wait it out.
        await until(pilot, lambda: cfg.display is True)  # revealed live on enable

    write_policy_enabling_unity(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        assert screen.query_one("#plugin-enabled-unity", Switch).value is True
        assert screen.query_one("#plugin-cfg-unity", Collapsible).display is True


async def test_settings_screen_enable_toggle_roundtrip(project):
    """Flipping a trust-gated plugin's enable toggle on writes [plugins] enabled;
    flipping it off again empties the list. The default editor_mode='shared' is a
    valid coupling with the default scm.isolation='none', so the save succeeds."""
    write_policy(project)  # template: enabled = []
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#plugin-enabled-unity", Switch).value = True
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        assert policy_mod.load(project.project / POLICY_FILE).plugins.enabled == ("unity",)

    write_policy_enabling_unity(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#plugin-enabled-unity", Switch).value = False
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        assert policy_mod.load(project.project / POLICY_FILE).plugins.enabled == ()


async def test_settings_screen_enable_toggle_preserves_unmanaged_names(project):
    """Saving the toggles never drops a name the UI doesn't manage (a data-only or
    undiscovered plugin already trusted): only trust-gated toggles are reconciled."""
    path = project.project / POLICY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[plugins]\nenabled = ["ghost"]\n', encoding="utf-8")
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#plugin-enabled-unity", Switch).value = True
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        assert set(policy_mod.load(path).plugins.enabled) == {"ghost", "unity"}


async def test_settings_screen_blocks_invalid_plugin_coupling(project):
    """editor_mode='shared' requires scm.isolation='none'. Pairing it with
    'worktree' is rejected at save time — the engine's startup coupling check now
    runs on the TUI save path — so the save is blocked and the screen stays open."""
    write_policy_enabling_unity(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#plugins-unity-editor_mode", Select).value = "shared"
        screen.query_one("#scm-isolation", Select).value = "worktree"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)  # save blocked
        from textual.widgets import Static

        assert "isolation" in str(screen.query_one("#errors", Static).content)


async def test_settings_screen_unity_editor_mode_roundtrip(project):
    write_policy_enabling_unity(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#plugins-unity-editor_mode", Select).value = "per_worktree"
        screen.query_one("#plugins-unity-mcp", Select).value = "coplaydev"
        screen.query_one("#scm-isolation", Select).value = "worktree"  # valid per_worktree combo
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.plugin_setting("unity", "editor_mode") == "per_worktree"
        assert pol.plugin_setting("unity", "mcp") == "coplaydev"
        assert pol.plugins.enabled == ("unity",)


async def test_settings_screen_unity_ready_fields_roundtrip(project):
    write_policy_enabling_unity(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#plugins-unity-ready_timeout_sec", Input).value = "300"
        screen.query_one("#plugins-unity-ready_grace_sec", Input).value = "30"
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.plugin_setting("unity", "ready_timeout_sec") == 300
        assert pol.plugin_setting("unity", "ready_grace_sec") == 30


async def test_settings_screen_stage_override_roundtrip(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#adapter-review-model", Input).value = "gpt-5-codex"
        override = screen.query_one("#adapter-extra_args-override", Switch)
        override.value = True
        args_box = screen.query_one("#adapter-extra_args", Input)
        # Switch.Changed enables the Input on a later tick; a single pause() can
        # race that delivery on a slow CI runner, so wait for the derived state.
        await until(pilot, lambda: not args_box.disabled)  # override enables input
        args_box.value = "--permission-mode bypassPermissions"
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.adapter.review.model == "gpt-5-codex"
        assert pol.adapter.extra_args == ("--permission-mode", "bypassPermissions")


# ------------------------------------------------------ plugin-contributed settings


def write_policy_enabling_example(project) -> None:
    """A policy that turns the builtin data-only `example` plugin on, so its
    [[settings]] render a section in the screen."""
    path = project.project / POLICY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[plugins]\nenabled = ["example"]\n', encoding="utf-8")


async def test_data_only_plugin_renders_without_toggle(project):
    """A data-only plugin (example: no [python]) is always active, so it appears in
    the Plugins roster with no enable toggle and its settings are visible (not
    hidden) by default."""
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        titles = [str(c.title) for c in screen.query(Collapsible)]
        assert any(t.startswith("example") for t in titles)
        assert screen.query("#plugins-example-greeting")  # settings render
        assert not screen.query("#plugin-enabled-example")  # data-only: no toggle
        assert screen.query_one("#plugin-cfg-example", Collapsible).display is True


async def test_enabled_plugin_section_renders_and_round_trips(project):
    """An enabled plugin's setting renders and persists under [plugins.<name>]."""
    write_policy_enabling_example(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        titles = [str(c.title) for c in screen.query(Collapsible)]
        assert any(t.startswith("example") for t in titles)
        screen.query_one("#plugins-example-greeting", Input).value = "howdy"
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.plugin_setting("example", "greeting") == "howdy"
        # the trust allowlist is untouched by the settings write
        assert pol.plugins.enabled == ("example",)


async def test_save_preserves_disabled_plugin_settings(project):
    """A disabled plugin's existing [plugins.<name>] table is data, not collected
    from its (hidden) widgets, so an unrelated save leaves it untouched."""
    path = project.project / POLICY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[plugins]\nenabled = []\n\n[plugins.unity]\nmcp = "coplaydev"\n', encoding="utf-8"
    )
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        assert screen.query_one("#plugin-cfg-unity", Collapsible).display is False  # hidden
        screen.query_one("#limits-max_review_cycles", Input).value = "5"  # unrelated edit
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(path)
        assert pol.plugin_setting("unity", "mcp") == "coplaydev"  # preserved untouched
        assert pol.plugins.enabled == ()  # still disabled


# ----------------------------------------------- section collapse / expand-all


async def test_sections_start_collapsed_and_toggle_all(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        sections = list(screen.query(Collapsible))
        assert sections  # sanity: the form is grouped into sections
        assert all(c.collapsed for c in sections)  # every section starts collapsed

        await pilot.press("ctrl+e")  # expand all
        await pilot.pause()
        assert all(not c.collapsed for c in sections)

        await pilot.press("ctrl+e")  # collapse all again
        await pilot.pause()
        assert all(c.collapsed for c in sections)


# -------------------------------------------------- arrow nav + enter-to-edit


async def test_arrow_keys_navigate_fields(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        expand_all(screen)
        await pilot.pause()
        screen.query_one("#limits-max_review_cycles", Input).focus()
        await pilot.pause()
        before = app.focused
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not before  # arrow advanced focus
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is before  # and back again


async def test_arrow_down_skips_disabled_args_input(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        expand_all(screen)
        await pilot.pause()
        # override off -> the args Input is disabled and out of the focus chain
        args_box = screen.query_one("#adapter-extra_args", Input)
        assert args_box.disabled
        screen.query_one("#adapter-extra_args-override", Switch).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not args_box


async def test_enter_opens_select_and_arrows_pick(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        from textual.widgets import Select

        screen = await open_settings(app, pilot)
        expand_all(screen)
        await pilot.pause()
        select = screen.query_one("#gates-mode", Select)
        start = select.value
        select.focus()
        await pilot.pause()
        await pilot.press("enter")  # open the dropdown
        await pilot.pause()
        assert select.expanded
        await pilot.press("down")  # dropdown owns up/down while open
        await pilot.press("enter")  # pick the highlighted option
        await pilot.pause()
        assert not select.expanded
        assert select.value != start


async def test_textarea_enter_edit_mode_and_escape(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        from textual.widgets import TextArea

        screen = await open_settings(app, pilot)
        expand_all(screen)
        await pilot.pause()
        area = screen.query_one("#verify-commands", TextArea)

        # nav mode: down leaves the TextArea
        area.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not area

        # enter -> edit mode: down keeps focus (cursor moves within the box)
        area.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert area.has_class("-editing")
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is area

        # escape exits edit mode without leaving the screen
        await pilot.press("escape")
        await pilot.pause()
        assert not area.has_class("-editing")
        assert isinstance(app.screen, SettingsScreen)


async def test_escape_in_nav_mode_pops_screen(project):
    write_policy(project)
    app = BmadLoopApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(app, pilot)
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
