"""Command-palette (`ctrl+p`) entries for the TUI's key-bound commands.

BmadLoopApp.get_system_commands yields palette_commands after Textual's
built-ins (Theme, Quit, Keys, Screenshot). The palette is built from the live
`screen.active_bindings` of the screen it was opened over, so a command is
offered only where its key works: a modal hides the app and dashboard
commands exactly as it blocks their keys, and an action whose check_action
returns False drops out. Every command runs through `run_action` with the
binding's own node as namespace, the same path a keypress takes.

Every bound action is either in PALETTE or in EXCLUDED with the reason it is
left out; tests/test_tui_commands.py fails on a new binding in neither, and on
a PALETTE entry that is no longer bound anywhere.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from typing import Any

from textual.app import SystemCommand
from textual.dom import DOMNode
from textual.screen import Screen

# Bare action name -> (title, help). Titles are verb-first so the fuzzy search
# finds them by what they do; help mirrors the docs/tui-guide.md key table.
PALETTE: dict[str, tuple[str, str]] = {
    # app (BmadLoopApp.BINDINGS) — live on the dashboard and settings screen
    "start_run": ("Start run", "Start a run (modal)"),
    "start_sweep": ("Start sweep", "Start a deferred-work sweep (modal)"),
    "resume_run": ("Resume run", "Resume the selected paused/interrupted run"),
    "review_pause": ("Review paused run", "Review the selected paused run in its HITL viewer"),
    "resolve_run": ("Resolve escalation", "Resolve a run paused at an escalation, then re-arm"),
    "reverify_run": ("Re-verify story", "Re-verify a story's kept work (pick target)"),
    "answer_decisions": ("Answer decisions", "Answer deferred-work decisions sweeps left open"),
    "attach": ("Attach to session", "Attach to the selected run's live session or window"),
    "stop_run": ("Stop run", "Stop the selected live run, abandoning the in-flight item"),
    "graceful_stop_run": ("Soft-stop run", "Finish the in-flight item, then finalize and stop"),
    "delete_run": ("Delete run", "Delete the selected run's directory"),
    "archive_run": ("Archive run", "Archive the selected run to .bmad-loop/archive"),
    "cleanup_sessions": ("Clean up sessions", "Clean up multiplexer sessions of finished runs"),
    "validate": ("Validate project", "Run bmad-loop validate, findings in a modal"),
    "settings": ("Open settings", "Settings editor for .bmad-loop/policy.toml"),
    "toggle_dark": ("Toggle light/dark mode", "Switch between the light and dark theme"),
    # dashboard (DashboardScreen.BINDINGS)
    "copy_pane": ("Copy pane", "Copy the active Log/Attention pane to the clipboard"),
    "resize_mode": ("Resize panes", "Enter/leave pane resize mode"),
    "unpin_log": ("Follow log", "Unpin the log view and follow new output"),
    # settings editor (SettingsScreen.BINDINGS)
    "save": ("Save settings", "Save .bmad-loop/policy.toml"),
    "toggle_all": ("Expand/collapse all sections", "Expand or collapse every settings section"),
    "back": ("Back to dashboard", "Leave the settings editor without saving"),
    # modals (BaseDialog and subclasses)
    "cancel": ("Cancel dialog", "Close this dialog without acting"),
    "toggle_detail": ("Toggle finding detail", "Show or hide validate finding detail"),
}

# Bound actions deliberately left out of the palette -> why.
EXCLUDED: dict[str, str] = {
    "quit": "Textual's built-in Quit command covers it",
    "nav_next": "settings-form arrow navigation",
    "nav_prev": "settings-form arrow navigation",
    "edit_field": "settings-form Enter-to-edit",
    "resize_up": "a step inside resize mode",
    "resize_down": "a step inside resize mode",
    "resize_left": "a step inside resize mode",
    "resize_right": "a step inside resize mode",
    "resize_done": "a step inside resize mode",
    "resize_cycle": "a step inside resize mode",
}


def palette_commands(
    screen: Screen[Any], run_action: Callable[[str, DOMNode], Any]
) -> Iterator[SystemCommand]:
    """Yield a palette command for each PALETTE action live on `screen`."""
    seen: set[str] = set()
    for key, active in screen.active_bindings.items():
        if not active.enabled:
            continue
        binding = active.binding
        action = binding.action.split("(", 1)[0]
        if action not in PALETTE or action in seen:
            continue
        seen.add(action)
        title, help_text = PALETTE[action]
        yield SystemCommand(
            title,
            f"{help_text} ({binding.key_display or key})",
            partial(run_action, binding.action, active.node),
        )
