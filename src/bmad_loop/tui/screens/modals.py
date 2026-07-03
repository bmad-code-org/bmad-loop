"""Run-control modal dialogs.

Results come back through ModalScreen.dismiss(): a dict of options from the
start modals, True from confirmations, None on cancel/escape. Pause reasons
and captured command output are arbitrary engine text and are rendered as
rich Text, never markup.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from ...model import RunState
from .. import data


def _int_or_none(value: str) -> int | None:
    value = value.strip()
    return int(value) if value else None


class BaseDialog(ModalScreen):
    """Shared chrome: centered bordered box, escape cancels."""

    DEFAULT_CSS = """
    BaseDialog {
        align: center middle;
    }
    BaseDialog #dialog {
        width: 64;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $surface;
        border: thick $primary-darken-2;
    }
    BaseDialog .title {
        text-style: bold;
        margin-bottom: 1;
    }
    BaseDialog .buttons {
        height: auto;
        align-horizontal: right;
        margin-top: 1;
    }
    BaseDialog .buttons Button {
        margin-left: 2;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class StartRunModal(BaseDialog):
    """Options for `bmad-loop run` → {epic, story, max_stories, dry_run}."""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("start run", classes="title")
            yield Input(
                placeholder="epic — blank for all",
                type="integer",
                valid_empty=True,
                id="epic",
            )
            yield Input(
                placeholder="story — 3-1, 3.1, slug, or full key (blank for all)",
                id="story",
            )
            yield Input(
                placeholder="max stories — blank for no limit",
                type="integer",
                valid_empty=True,
                id="max-stories",
            )
            yield Checkbox("dry run (print the plan, spawn nothing)", id="dry-run")
            with Horizontal(classes="buttons"):
                yield Button("start", variant="primary", id="ok")
                yield Button("cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "ok":
            self.dismiss(None)
            return
        self.dismiss(
            {
                "epic": _int_or_none(self.query_one("#epic", Input).value),
                "story": self.query_one("#story", Input).value.strip() or None,
                "max_stories": _int_or_none(self.query_one("#max-stories", Input).value),
                "dry_run": self.query_one("#dry-run", Checkbox).value,
            }
        )


class StartSweepModal(BaseDialog):
    """Options for `bmad-loop sweep` → {no_prompt, decisions_only,
    max_bundles, dry_run}."""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("start sweep", classes="title")
            yield Checkbox("unattended (--no-prompt): skip decisions", id="no-prompt")
            yield Checkbox("decisions only: triage + answer, no bundles", id="decisions-only")
            yield Input(
                placeholder="max bundles — blank for policy default",
                type="integer",
                valid_empty=True,
                id="max-bundles",
            )
            yield Checkbox("dry run (list open entries, spawn nothing)", id="dry-run")
            with Horizontal(classes="buttons"):
                yield Button("start", variant="primary", id="ok")
                yield Button("cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "ok":
            self.dismiss(None)
            return
        self.dismiss(
            {
                "no_prompt": self.query_one("#no-prompt", Checkbox).value,
                "decisions_only": self.query_one("#decisions-only", Checkbox).value,
                "max_bundles": _int_or_none(self.query_one("#max-bundles", Input).value),
                "dry_run": self.query_one("#dry-run", Checkbox).value,
            }
        )


class ConfirmModal(BaseDialog):
    """Generic confirmation → dismiss(True) on confirm, None otherwise."""

    def __init__(
        self,
        title: str,
        body: str | Text,
        *,
        confirm_label: str = "confirm",
        warning: str | None = None,
    ):
        super().__init__()
        self._title = title
        self._body = body if isinstance(body, Text) else Text(body)
        self._confirm_label = confirm_label
        self._warning = warning

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, classes="title")
            yield Static(self._body)
            if self._warning:
                yield Static(Text(f"⚠ {self._warning}", style="bold red"))
            with Horizontal(classes="buttons"):
                yield Button(self._confirm_label, variant="warning", id="ok")
                yield Button("cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(True if event.button.id == "ok" else None)


class ConfirmResumeModal(ConfirmModal):
    """Resume confirmation with pause details and a double-drive warning when
    the recorded engine pid may still be live."""

    def __init__(self, run_id: str, state: RunState, engine_alive: bool):
        body = Text()
        body.append("resume run ")
        body.append(run_id, style="bold")
        body.append("?\n")
        if state.paused:
            body.append(f"paused at {state.paused_stage or '?'}", style="yellow")
            if state.paused_reason:
                body.append(f" — {state.paused_reason}", style="yellow")
        else:
            body.append("run is not paused — it looks interrupted", style="dim")
        warning = (
            "engine.pid may still be live — resuming could double-drive this run"
            if engine_alive
            else None
        )
        super().__init__("resume run", body, confirm_label="resume", warning=warning)


class DeferredEntryModal(BaseDialog):
    """Full body of one deferred-work entry. The ledger is LLM-written
    markdown, so the body renders as plain Text, never markup."""

    DEFAULT_CSS = """
    DeferredEntryModal #dialog {
        width: 96;
        height: 80%;
    }
    DeferredEntryModal #entry {
        height: 1fr;
    }
    """

    def __init__(self, item: data.DeferredItem):
        super().__init__()
        self._item = item

    def compose(self) -> ComposeResult:
        item = self._item
        title = Text()
        title.append(f"{item.id} — {item.title}", style="bold")
        if item.done:
            title.append("  ✓ done", style="green")
        if item.legacy:
            title.append("  · legacy — converted to DW format on next sweep", style="dim")
        with Vertical(id="dialog"):
            yield Static(title, classes="title")
            with VerticalScroll(id="entry"):
                body = item.body.strip()
                if body:
                    yield Static(Text(body))
                else:
                    yield Static(Text("(empty entry)", style="dim"))
            with Horizontal(classes="buttons"):
                yield Button("close", variant="primary", id="ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class DecisionModal(BaseDialog):
    """Answer one deferred-work decision a past sweep left unanswered. Dismisses
    with the chosen sweep.DecisionOption, or None on skip/cancel. Question,
    option labels and details are LLM-written, so they render as plain Text."""

    DEFAULT_CSS = """
    DecisionModal #dialog {
        width: 86;
        height: auto;
        max-height: 90%;
    }
    DecisionModal #context {
        height: auto;
        max-height: 40%;
        margin-bottom: 1;
    }
    DecisionModal .opt {
        margin-top: 1;
    }
    DecisionModal .opt-detail {
        margin-bottom: 1;
    }
    """

    def __init__(self, decision: Any):
        super().__init__()
        self._decision = decision

    def compose(self) -> ComposeResult:
        d = self._decision
        title = Text()
        title.append(f"{d.id} — answer this decision", style="bold")
        with Vertical(id="dialog"):
            yield Static(title, classes="title")
            yield Static(Text(d.question))
            if d.context:
                with VerticalScroll(id="context"):
                    yield Static(Text(d.context, style="dim"))
            for opt in d.options:
                head = Text()
                head.append(f"[{opt.key}] ", style="bold")
                head.append(opt.label)
                head.append(f"  · {opt.effect}", style="cyan")
                if opt.key == d.recommendation:
                    head.append("  (recommended)", style="green")
                yield Static(head, classes="opt")
                detail = opt.intent or opt.resolution
                if detail:
                    yield Static(Text(f"    {detail}", style="dim"), classes="opt-detail")
                yield Button(f"choose {opt.key}", id=f"opt-{opt.key}")
            with Horizontal(classes="buttons"):
                yield Button("skip", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("opt-"):
            self.dismiss(self._decision.option(bid[len("opt-") :]))
        else:
            self.dismiss(None)


class TextOutputModal(BaseDialog):
    """Scrollable captured command output (validate, dry runs)."""

    DEFAULT_CSS = """
    TextOutputModal #dialog {
        width: 96;
        height: 80%;
    }
    TextOutputModal #output {
        height: 1fr;
    }
    """

    def __init__(self, title: str, returncode: int, output: str):
        super().__init__()
        self._title = title
        self._returncode = returncode
        self._output = output

    def compose(self) -> ComposeResult:
        status = "ok" if self._returncode == 0 else f"exit {self._returncode}"
        with Vertical(id="dialog"):
            yield Label(f"{self._title} — {status}", classes="title")
            with VerticalScroll(id="output"):
                if self._output.strip():
                    yield Static(Text.from_ansi(self._output))
                else:
                    yield Static(Text("(no output)", style="dim"))
            with Horizontal(classes="buttons"):
                yield Button("close", variant="primary", id="ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)
