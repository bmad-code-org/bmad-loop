"""Command-palette entries (tui.commands): the PALETTE/EXCLUDED tables stay total
over every TUI binding, each screen's palette lists only the commands whose key
is live there (modal blocking included), and a palette pick runs the real
action through BmadLoopApp.get_system_commands."""

from __future__ import annotations

from test_tui_app import until
from textual.binding import Binding
from textual.command import CommandInput, CommandList, CommandPalette
from textual.dom import DOMNode

from bmad_loop.tui import commands
from bmad_loop.tui.app import BmadLoopApp
from bmad_loop.tui.screens import modals
from bmad_loop.tui.screens.dashboard import DashboardScreen
from bmad_loop.tui.screens.settings_screen import SettingsScreen

APP_ACTIONS = [
    "start_run",
    "start_sweep",
    "resume_run",
    "review_pause",
    "resolve_run",
    "reverify_run",
    "answer_decisions",
    "attach",
    "stop_run",
    "graceful_stop_run",
    "delete_run",
    "archive_run",
    "cleanup_sessions",
    "validate",
    "settings",
    "toggle_dark",
]
DASHBOARD_ACTIONS = ["copy_pane", "resize_mode", "unpin_log"]
SETTINGS_ACTIONS = ["save", "toggle_all", "back"]
MODAL_ACTIONS = ["cancel", "toggle_detail"]


def _title(action: str) -> str:
    return commands.PALETTE[action][0]


def _subclasses(cls: type) -> list[type]:
    out: list[type] = []
    for sub in cls.__subclasses__():
        out += [sub, *_subclasses(sub)]
    return out


def _bound_actions() -> set[str]:
    classes: list[type[DOMNode]] = [BmadLoopApp, DashboardScreen, SettingsScreen]
    classes += [modals.BaseDialog, *_subclasses(modals.BaseDialog)]
    actions: set[str] = set()
    for cls in classes:
        # Textual's own bases (App, Screen, ModalScreen) carry framework
        # bindings — focus movement, built-in quit/copy — that are not ours.
        for base in (b for b in cls.__mro__ if b.__module__.startswith("bmad_loop.")):
            for binding in base.__dict__.get("BINDINGS", []):
                assert isinstance(binding, Binding), (cls, binding)
                actions.add(binding.action.split("(", 1)[0])
    return actions


def _titles(app: BmadLoopApp) -> set[str]:
    return {command.title for command in app.get_system_commands(app.screen)}


def test_every_binding_is_in_palette_or_excluded():
    bound = _bound_actions()
    assert not set(commands.PALETTE) & set(commands.EXCLUDED)
    assert sorted(bound - set(commands.PALETTE) - set(commands.EXCLUDED)) == []
    assert sorted(set(commands.PALETTE) - bound) == []
    assert sorted(set(commands.EXCLUDED) - bound) == []
    # The tables cover exactly the planned commands, so a PALETTE entry for a
    # (still bound) key cannot silently move to EXCLUDED.
    assert sorted(commands.PALETTE) == sorted(
        APP_ACTIONS + DASHBOARD_ACTIONS + SETTINGS_ACTIONS + MODAL_ACTIONS
    )


async def test_dashboard_palette_lists_app_and_dashboard_commands(project):
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        titles = _titles(app)
        for action in APP_ACTIONS + DASHBOARD_ACTIONS:
            assert _title(action) in titles, action
        assert {"Quit", "Theme"} <= titles  # Textual's built-ins stay
        for action in SETTINGS_ACTIONS + MODAL_ACTIONS:
            assert _title(action) not in titles, action
        # One entry per action even when it has several live keys.
        listed = [c.title for c in app.get_system_commands(app.screen)]
        assert len(listed) == len(set(listed))


async def test_settings_palette_lists_settings_commands(project):
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("g")
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
        titles = _titles(app)
        for action in SETTINGS_ACTIONS + APP_ACTIONS:
            assert _title(action) in titles, action
        for action in DASHBOARD_ACTIONS + MODAL_ACTIONS:
            assert _title(action) not in titles, action


async def test_modal_palette_hides_app_commands(project):
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        app.push_screen(modals.ConfirmModal("probe", "a modal over the dashboard"))
        await until(pilot, lambda: isinstance(app.screen, modals.ConfirmModal))
        titles = _titles(app)
        assert _title("cancel") in titles
        for action in APP_ACTIONS + DASHBOARD_ACTIONS + SETTINGS_ACTIONS:
            assert _title(action) not in titles, action
        assert _title("toggle_detail") not in titles  # ValidateFindingsModal only


async def test_palette_runs_command_end_to_end(project):
    app = BmadLoopApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("ctrl+p")
        await until(pilot, lambda: isinstance(app.screen, CommandPalette))
        app.screen.query_one(CommandInput).value = _title("settings")

        def first_hit() -> str:
            options = app.screen.query_one(CommandList)
            return str(options.get_option_at_index(0).prompt) if options.option_count else ""

        await until(
            pilot, lambda: _title("settings") in first_hit(), what="settings is the top hit"
        )
        # The first enter highlights the top hit, the next selects it, and the
        # last runs it (command.py _select_or_command).
        for _ in range(3):
            if not isinstance(app.screen, CommandPalette):
                break
            await pilot.press("enter")
            await pilot.pause()
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))


class _TwoKeySettingsApp(BmadLoopApp):
    # Textual merges BINDINGS across the MRO: g and G both open settings.
    BINDINGS = [Binding("G", "settings", "settings")]


async def test_palette_lists_an_action_once_across_keys(project):
    app = _TwoKeySettingsApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        keys = [k for k, b in app.screen.active_bindings.items() if b.binding.action == "settings"]
        assert sorted(keys) == ["G", "g"]
        listed = [c.title for c in app.get_system_commands(app.screen)]
        assert listed.count(_title("settings")) == 1
