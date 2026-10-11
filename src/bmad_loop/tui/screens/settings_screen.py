"""Settings editor for .bmad-loop/policy.toml.

The form is populated from the raw tomlkit document, so unset keys show
default placeholders instead of baked-in values, and saving only touches keys
the user actually changed — tomlkit keeps everything else, comments included,
byte-identical. Validation is policy.loads() itself via PolicyDoc.validate(),
plus — on save — each enabled in-process plugin's coupling check (e.g. unity
editor_mode↔scm.isolation), the same check the engine runs at startup; errors
block the save and land in the strip above the buttons. A dedicated Plugins
section is a roster of discovered plugins with enable toggles; a plugin's
settings appear as a sub-collapsible only once it is enabled. A builtin's toggle
is its name in [plugins] enabled. A project plugin's toggle is its out-of-tree
content grant (#496/#497): toggling it on records a grant of the plugin dir as
it is at save, toggling it off revokes it, and its row shows the grant status.
The grant is recorded only after the policy validated — against the candidate
grants — and saved, so a blocked save records nothing. A plugin that changed
since its grant is never re-pinned here, by a toggle or by an unrelated save:
that is `bmad-loop plugins accept`, which its row names. Cleared fields
delete their keys (unset = default / inherit); the adapter extra_args fields
distinguish "use profile defaults" (override off → key absent) from "replace
with this list" (override on, possibly empty — None ≠ []).
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.validation import Number
from textual.widget import Widget
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    Switch,
    TextArea,
)

from ... import bmadconfig
from ... import policy as policy_mod
from ... import runs
from ...plugins import store
from ...plugins.grants import TRUSTED_SOURCES, GrantRecord, GrantSet
from ...plugins.model import PluginError
from ...plugins.registry import preflight, read_bound_tree
from ...policy import POLICY_FILE
from ...settings_schema import PluginInfo, SettingSpec, build_registry
from ..settings import PolicyDoc

# collect() sentinel for a field whose widget holds an unusable value
_INVALID = object()


def plugin_wid(name: str) -> str:
    """A plugin name as a widget-id fragment. A manifest name is any non-empty
    string, but a Textual id takes only ``[A-Za-z0-9_-]``, so an unsafe name
    would stop the screen from opening. ASCII letters, digits and ``-`` pass
    through; every other character's UTF-8 bytes (``_`` included, which keeps the
    encoding injective) become ``_xx`` hex.
    ABLATION (test_a_plugin_name_unsafe_as_a_widget_id_still_opens_and_grants):
    return ``name`` unchanged and the screen fails to open."""
    return "".join(
        c if c.isascii() and (c.isalnum() or c == "-") else "".join(f"_{b:02x}" for b in c.encode())
        for c in name
    )


class SettingsScreen(Screen[None]):
    DEFAULT_CSS = """
    SettingsScreen #form {
        padding: 0 2;
    }
    SettingsScreen #note {
        padding: 1 1 0 1;
        color: $text-muted;
    }
    SettingsScreen .field {
        height: auto;
    }
    SettingsScreen .fieldname {
        width: 30;
        padding: 1 1;
        color: $text-muted;
    }
    SettingsScreen .fielddesc {
        padding: 0 1 1 32;
        color: $text-muted;
        text-style: italic;
    }
    SettingsScreen .field Input, SettingsScreen .field Select {
        width: 1fr;
    }
    SettingsScreen .field Switch {
        margin: 0 1;
    }
    SettingsScreen .field TextArea {
        width: 1fr;
        height: 5;
    }
    SettingsScreen .field TextArea.-editing {
        border: tall $accent;
    }
    SettingsScreen .plugin-row {
        height: auto;
    }
    SettingsScreen .plugin-row Switch {
        margin: 0 1;
    }
    SettingsScreen .plugin-always {
        width: 8;
        padding: 1 1;
        color: $text-muted;
    }
    SettingsScreen .plugin-name {
        padding: 1 1;
    }
    SettingsScreen .plugin-status {
        padding: 1 1;
        color: $text-muted;
    }
    SettingsScreen .plugin-status.-attention {
        color: $warning;
    }
    SettingsScreen .plugin-divider {
        padding: 1 1 0 1;
        color: $text-muted;
        text-style: italic;
    }
    SettingsScreen .plugin-cfg {
        margin-left: 2;
    }
    SettingsScreen #errors {
        display: none;
        margin-top: 1;
        padding: 0 1;
        background: $error 20%;
    }
    SettingsScreen #errors.-visible {
        display: block;
    }
    SettingsScreen .buttons {
        height: auto;
        align-horizontal: right;
        padding: 1;
    }
    SettingsScreen .buttons Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("ctrl+s", "save", "save"),
        Binding("ctrl+e", "toggle_all", "expand/collapse all"),
        Binding("up", "nav_prev", "prev", show=False, priority=True),
        Binding("down", "nav_next", "next", show=False, priority=True),
        Binding("enter", "edit_field", "edit", show=False, priority=True),
    ]

    def __init__(self, project: Path, doc: PolicyDoc):
        super().__init__()
        self._project = project
        self._path = project / POLICY_FILE
        self._doc = doc
        # The field/section list is generated from the TOML schema registry, not
        # hardcoded: core sections plus a PluginInfo for every discovered plugin
        # (its enable state, whether it is trust-gated, and its settings fields).
        # Enable state comes from the policy under edit; an invalid doc still
        # renders the core schema (on_mount then surfaces the validation error).
        try:
            pol = policy_mod.loads(doc.dumps())
        except policy_mod.PolicyError:
            pol = policy_mod.Policy()
        # Plugins are discovered from the code root a run loads, as `bmad-loop
        # plugins` and a launch do, while the grant store stays keyed by the
        # project. A project whose BMAD config can't be read still edits its
        # policy, discovering from the project itself.
        try:
            self._root = bmadconfig.load_paths(project).repo_root
        except bmadconfig.BmadConfigError:
            self._root = project
        self._grants = store.load_grants(project)
        self._registry = build_registry(project, pol, root=self._root, grants=self._grants)
        # Toggle states at open: a project plugin's grant changes only on a flip.
        self._initial_on = {p.name: self._initial_toggle(p) for p in self._registry.plugins}
        # The TextArea currently in cursor-edit mode (None = navigation mode).
        self._editing: Widget | None = None

    # ------------------------------------------------------------- compose

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="form"):
            yield Static(
                Text(
                    f"{self._path}\nrunning engines snapshot policy at start — "
                    "changes apply to new runs and resumes",
                    style="dim",
                ),
                id="note",
            )
            for section in self._registry.sections:
                with Collapsible(title=section.title, collapsed=True):
                    for spec in section.fields:
                        yield from self._compose_field(spec)
            if self._registry.plugins:
                with Collapsible(title="Plugins", collapsed=True, id="plugins"):
                    yield from self._compose_plugins()
            yield Static(id="errors")
            with Horizontal(classes="buttons"):
                yield Button("save", variant="primary", id="save")
                yield Button("cancel", id="cancel")
        yield Footer()

    def _compose_field(self, spec: SettingSpec) -> ComposeResult:
        raw = self._doc.get(spec.section, spec.key)
        with Horizontal(classes="field"):
            yield Label(spec.label or spec.key, classes="fieldname")
            if spec.kind == "select":
                yield Select(
                    [(o, o) for o in spec.options],
                    prompt=f"default: {spec.default or 'auto'}",
                    value=str(raw) if raw in spec.options else Select.NULL,
                    id=spec.widget_id,
                )
            elif spec.kind in ("int", "float"):
                yield Input(
                    value=None if raw is None else str(raw),
                    placeholder=spec.placeholder or f"default: {spec.default}",
                    type="integer" if spec.kind == "int" else "number",
                    validators=[Number(minimum=spec.minimum, maximum=spec.maximum)],
                    valid_empty=True,
                    id=spec.widget_id,
                )
            elif spec.kind == "str":
                yield Input(
                    value=None if raw is None else str(raw),
                    placeholder=spec.placeholder,
                    id=spec.widget_id,
                )
            elif spec.kind == "switch":
                yield Switch(
                    value=bool(spec.default) if raw is None else bool(raw),
                    id=spec.widget_id,
                )
            elif spec.kind == "lines":
                yield TextArea(
                    "\n".join(str(c) for c in raw) if raw else "",
                    soft_wrap=False,
                    id=spec.widget_id,
                )
            elif spec.kind == "args":
                override = raw is not None
                yield Switch(value=override, id=f"{spec.widget_id}-override", classes="argswitch")
                yield Input(
                    value=shlex.join(str(a) for a in raw) if override else None,
                    placeholder="profile default flags — toggle to override",
                    disabled=not override,
                    id=spec.widget_id,
                )
        if spec.description:
            yield Static(spec.description, classes="fielddesc")

    def _compose_plugins(self) -> ComposeResult:
        """The Plugins section: a scannable roster of every discovered plugin with
        an enable toggle (trust-gated) or an always-on note (data-only), then —
        below the roster — a settings sub-collapsible per plugin that contributes
        settings. A plugin's settings are revealed only while it is active (data-
        only always; trust-gated once enabled); inactive ones are hidden by
        on_mount and toggled live by on_switch_changed, so the roster stays clean."""
        if self._grants.fault is not None:
            yield Static(
                Text(
                    f"plugin grants could not be read ({self._grants.fault}); "
                    "project plugins show as not granted"
                ),
                classes="plugin-status -attention",
                id="plugin-grants-fault",
            )
        for info in self._registry.plugins:
            with Horizontal(classes="plugin-row"):
                if info.trust_needed:
                    yield Switch(
                        value=self._initial_on[info.name],
                        id=f"plugin-enabled-{plugin_wid(info.name)}",
                        classes="plugin-enable",
                    )
                else:
                    yield Static("on", classes="plugin-always")
                label = f"{info.name} — {info.description}" if info.description else info.name
                yield Static(label, classes="plugin-name")
                if info.grant_required:
                    status, attention = self._grant_label(info)
                    yield Static(
                        Text(status),
                        classes="plugin-status -attention" if attention else "plugin-status",
                        id=f"plugin-status-{plugin_wid(info.name)}",
                    )
        configurable = [p for p in self._registry.plugins if p.fields]
        if configurable:
            yield Static("configure", classes="plugin-divider")
            for info in configurable:
                title = f"{info.name} — {info.description}" if info.description else info.name
                with Collapsible(
                    title=title,
                    collapsed=True,
                    id=f"plugin-cfg-{plugin_wid(info.name)}",
                    classes="plugin-cfg",
                ):
                    for spec in info.fields:
                        yield from self._compose_field(spec)

    @staticmethod
    def _initial_toggle(info: PluginInfo) -> bool:
        """A builtin's toggle is its [plugins] enabled membership; a project
        plugin's is whether a grant is recorded (matching or not, so a changed
        plugin can still be toggled off to revoke it) or its name is still in
        [plugins] enabled — the upgrade case with no grant starts on too, so
        toggling it off drops the legacy entry; left off, that fatal state could
        never be cleared and would block every save.
        ABLATION (test_toggle_off_clears_an_ungranted_legacy_entry): drop the
        ``info.enabled`` arm and the toggle can't be turned off."""
        if info.grant_required:
            return info.has_grant or info.enabled
        return info.enabled

    @staticmethod
    def _grant_label(info: PluginInfo) -> tuple[str, bool]:
        """A project plugin's grant status for its roster row, and whether it
        needs the operator's attention. Re-pinning is the CLI's: the row names it."""
        accept = f"run `bmad-loop plugins accept {info.name}`"
        if info.grant_status == "granted":
            return "granted", False
        if info.grant_status == "ungranted" and info.enabled:
            return (
                f"enabled, not granted — run `bmad-loop plugins enable {info.name}` "
                "or toggle off",
                True,
            )
        if info.grant_status == "mismatch":
            return f"changed — {accept}", True
        if info.grant_status == "unreadable":
            return f"unreadable — fix the plugin dir, then {accept}", True
        return "not granted", False

    def _plugin_active(self, info: PluginInfo) -> bool:
        """A data-only plugin is always active; a trust-gated one is active only
        while its enable toggle is on (its module is what trust gates)."""
        if not info.trust_needed:
            return True
        return self.query_one(f"#plugin-enabled-{plugin_wid(info.name)}", Switch).value

    def on_mount(self) -> None:
        # Hide the settings of every plugin that isn't active yet, so a fresh open
        # shows only the roster plus the active plugins' settings.
        for info in self._registry.plugins:
            if info.fields and not self._plugin_active(info):
                self.query_one(f"#plugin-cfg-{plugin_wid(info.name)}", Collapsible).display = False
        self._show_errors(self._doc.validate(self._registry.plugin_schemas))

    # ------------------------------------------------------------ keyboard

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # While editing a TextArea, let its own up/down move the cursor instead
        # of moving focus, and let escape exit edit mode rather than the screen.
        if action in ("nav_next", "nav_prev"):
            if self._editing is not None and self.focused is self._editing:
                return False  # TextArea cursor owns up/down in edit mode
            if any(select.expanded for select in self.query(Select)):
                return False  # an open dropdown owns up/down to pick an option
            return True
        if action == "edit_field":
            # Only intercept Enter to start cursor-editing a TextArea; for every
            # other widget Enter keeps its native meaning (open Select, toggle
            # Switch, submit Input) and for a TextArea already editing it inserts
            # a newline.
            return isinstance(self.focused, TextArea) and self._editing is None
        if action == "back":
            return self._editing is None
        return True

    def action_nav_next(self) -> None:
        self._exit_edit()
        self.focus_next()

    def action_nav_prev(self) -> None:
        self._exit_edit()
        self.focus_previous()

    def action_edit_field(self) -> None:
        area = self.focused
        if isinstance(area, TextArea):
            self._editing = area
            area.add_class("-editing")

    def action_toggle_all(self) -> None:
        self._exit_edit()
        sections = list(self.query(Collapsible))
        target = any(not c.collapsed for c in sections)  # something open -> collapse all
        for c in sections:
            c.collapsed = target

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._editing is not None:
            self._exit_edit()
            event.stop()

    def _exit_edit(self) -> None:
        if self._editing is not None:
            self._editing.remove_class("-editing")
            self._editing = None

    # -------------------------------------------------------------- events

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.has_class("argswitch") and event.switch.id:
            target = event.switch.id.removesuffix("-override")
            self.query_one(f"#{target}", Input).disabled = not event.value
        elif event.switch.has_class("plugin-enable") and event.switch.id:
            # Reveal/hide the plugin's settings live (a plugin with no settings has
            # no config collapsible). Expand on reveal so they're immediately seen.
            wid = event.switch.id.removeprefix("plugin-enabled-")
            if self.query(f"#plugin-cfg-{wid}"):
                cfg = self.query_one(f"#plugin-cfg-{wid}", Collapsible)
                cfg.display = event.value
                if event.value:
                    cfg.collapsed = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()

    # ---------------------------------------------------------------- save

    def _collect(self, spec: SettingSpec, problems: list[str]) -> object:
        """Widget state -> raw policy value; None means unset (delete key)."""
        ident = f"{spec.section}.{spec.key}"
        if spec.kind == "select":
            select = self.query_one(f"#{spec.widget_id}", Select)
            return None if select.is_blank() else str(select.value)
        if spec.kind in ("int", "float"):
            box = self.query_one(f"#{spec.widget_id}", Input)
            value = box.value.strip()
            if not value:
                return None
            result = box.validate(value)
            if result is not None and not result.is_valid:
                problems.append(f"{ident}: {'; '.join(result.failure_descriptions)}")
                return _INVALID
            try:
                return int(value) if spec.kind == "int" else float(value)
            except ValueError:
                problems.append(f"{ident}: not a valid number")
                return _INVALID
        if spec.kind == "str":
            return self.query_one(f"#{spec.widget_id}", Input).value.strip() or None
        if spec.kind == "switch":
            return self.query_one(f"#{spec.widget_id}", Switch).value
        if spec.kind == "lines":
            text = self.query_one(f"#{spec.widget_id}", TextArea).text
            return [line.strip() for line in text.splitlines() if line.strip()]
        if spec.kind == "args":
            if not self.query_one(f"#{spec.widget_id}-override", Switch).value:
                return None
            try:
                return shlex.split(self.query_one(f"#{spec.widget_id}", Input).value)
            except ValueError as e:
                problems.append(f"{ident}: {e}")
                return _INVALID
        raise AssertionError(spec.kind)

    @staticmethod
    def _changed(spec: SettingSpec, current: object, desired: object) -> bool:
        if desired is None:
            # adapter.model = "" means "CLI default" just like an absent key;
            # don't rewrite the template line for an untouched empty input
            return current is not None and not (spec.kind == "str" and current == "")
        if current is None:
            # an unset key already behaves like the default — don't bake it in
            if spec.kind == "switch" and desired == spec.default:
                return False
            if spec.kind == "lines" and desired == []:
                return False
            return True
        if isinstance(desired, list):
            # `current` pairs with a list `desired` here; its static type is the
            # heterogeneous `object` setting supertype, but at runtime it is a list.
            return [
                str(item) for item in current  # pyright: ignore[reportGeneralTypeIssues]
            ] != desired
        return current != desired

    def _builtin_toggles(self) -> list[PluginInfo]:
        return [p for p in self._registry.plugins if p.trust_needed and p.origin in TRUSTED_SOURCES]

    def _save_enabled(self, revoked: list[str]) -> None:
        """Reconcile [plugins] enabled with the builtin enable toggles. Only a
        builtin's in-process module is gated by that list; a project plugin's
        toggle is its grant, so its name is never written here. Any other name
        already in the list (a data-only plugin, a project plugin, or a plugin
        this checkout can't discover) is preserved so the save never silently
        drops trust the UI doesn't manage — except a project plugin toggled off
        (``revoked``): a legacy entry left behind would make its revoked grant a
        fatal "enabled but never granted", so the toggle-off could never save.
        An absent key stays absent when nothing is enabled (unset = nothing
        trusted)."""
        managed = self._builtin_toggles()
        toggles = {p.name for p in managed}
        on = [
            p.name
            for p in managed
            if self.query_one(f"#plugin-enabled-{plugin_wid(p.name)}", Switch).value
        ]
        current = self._doc.get("plugins", "enabled")
        current_names = None if current is None else [str(x) for x in current]
        dropped = toggles | set(revoked)
        preserved = [n for n in (current_names or []) if n not in dropped]
        desired = preserved + on
        if current_names is None:
            if desired:
                self._doc.set("plugins", "enabled", desired)
        elif current_names != desired:
            self._doc.set("plugins", "enabled", desired)

    def _save_specs(self) -> list[SettingSpec]:
        """Core fields plus only the *active* plugins' fields — a disabled plugin's
        settings are never collected, so its defaults are not baked in and any
        existing [plugins.<name>] table is left untouched (preserved as data)."""
        specs = [f for s in self._registry.sections for f in s.fields]
        for info in self._registry.plugins:
            if self._plugin_active(info):
                specs.extend(info.fields)
        return specs

    def action_save(self) -> None:
        problems: list[str] = []
        desired = {spec: self._collect(spec, problems) for spec in self._save_specs()}
        if problems:
            self._show_errors("\n".join(problems))
            return
        for spec, value in desired.items():
            if self._changed(spec, self._doc.get(spec.section, spec.key), value):
                self._doc.set(spec.section, spec.key, value)
        to_grant, to_revoke = self._grant_flips()
        self._save_enabled(to_revoke)
        # Re-read, so a `bmad-loop plugins` change made while the screen was open
        # survives; the grant write below is refused if the store moves off it.
        base = store.load_grants(self._project)
        candidate = self._candidate_grants(base, to_grant, to_revoke)
        if isinstance(candidate, str):
            self._show_errors(candidate)
            return
        # Pass the code root so the round-trip also runs every enabled in-process
        # plugin's coupling check (e.g. unity editor_mode↔scm.isolation), blocking
        # an incompatible save the same way the engine fails fast at startup — and
        # judge plugin trust against the grants this save would leave, so a plugin
        # a launch would refuse blocks it too. Nothing is written before this:
        # ABLATION (test_failed_validate_records_no_grant) — record the grant
        # first and a blocked save leaves it behind.
        error = self._doc.validate(
            self._registry.plugin_schemas, project=self._root, grants=candidate
        )
        if error:
            self._show_errors(error)
            return
        try:
            self._doc.save(self._path, confine_root=self._project)
        except OSError as e:
            self._show_errors(f"save failed: {e}")
            return
        if to_grant or to_revoke:
            try:
                store.save_grants(self._project, candidate.records, expected=base)
            except (OSError, runs.StateRootError, store.GrantStoreChanged) as e:
                self._show_errors(f"policy saved, but the plugin grant was not recorded: {e}")
                return
        if candidate.fault is not None:
            # Only reachable with no toggle flipped (a flip refuses a faulted store
            # above), so the save is fine, but the store may have gone bad since the
            # open-time banner: say so, or every project plugin silently goes inert.
            # ABLATION (test_save_surfaces_a_grant_store_that_went_bad): drop this
            # arm and the save reports plain success.
            self.app.notify(
                f"policy saved: {self._path} — but plugin grants could not be read "
                f"({candidate.fault}); a launch treats this as no grants and refuses "
                "every project plugin that needs one",
                severity="warning",
            )
        else:
            self.app.notify(f"policy saved: {self._path}")
        self.app.pop_screen()

    def _grant_flips(self) -> tuple[list[str], list[str]]:
        """Project plugins toggled on since open (to grant) and off (to revoke).
        An untouched toggle changes nothing — so an unrelated save never
        re-blesses a plugin whose contents moved since its grant."""
        to_grant: list[str] = []
        to_revoke: list[str] = []
        for info in self._registry.plugins:
            if not info.grant_required:
                continue
            now = self.query_one(f"#plugin-enabled-{plugin_wid(info.name)}", Switch).value
            was = self._initial_on[info.name]
            if now and not was and info.grant_status == "ungranted":
                to_grant.append(info.name)
            elif was and not now:
                to_revoke.append(info.name)
        return to_grant, to_revoke

    def _candidate_grants(
        self, grants: GrantSet, to_grant: list[str], to_revoke: list[str]
    ) -> GrantSet | str:
        """The grants this save would leave: ``grants`` (the store as it is now)
        plus a fresh record of each newly toggled plugin's dir, minus the revoked.
        A string is the error that blocks the save. Hashes only — nothing runs."""
        if not (to_grant or to_revoke):
            return grants
        if grants.fault is not None:
            # never overwrite a store that exists but can't be read: its grants would be lost
            return (
                f"plugin grants could not be read ({grants.fault}); not changing them — "
                "fix or remove the file, or use `bmad-loop plugins`"
            )
        if to_grant:
            try:
                found = {
                    m.name: (m, rel)
                    for m, _verdict, rel in preflight(self._root, None, GrantSet.empty())
                }
            except PluginError as e:
                return str(e)
            granted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for name in to_grant:
                if name not in found:
                    return f'plugin "{name}" is no longer discovered under {self._root}'
                manifest, rel = found[name]
                try:
                    tree = read_bound_tree(manifest)
                except PluginError as e:
                    return str(e)
                grants = store.upsert(grants, GrantRecord.from_tree(name, rel, tree, granted_at))
        for name in to_revoke:
            grants = store.remove(grants, name)
        return grants

    def _show_errors(self, message: str | None) -> None:
        strip = self.query_one("#errors", Static)
        if message:
            strip.update(Text(f"✗ {message}"))
            strip.add_class("-visible")
        else:
            strip.update("")
            strip.remove_class("-visible")
