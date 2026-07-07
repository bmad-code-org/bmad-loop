"""Run-control modal dialogs.

Results come back through ModalScreen.dismiss(): a dict of options from the
start modals, True from confirmations, None on cancel/escape. Pause reasons
and captured command output are arbitrary engine text and are rendered as
rich Text, never markup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from ... import stories
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
    """Options for `bmad-loop run`.

    Dual-flow: a source select (prefilled from ``[stories]``) picks sprint mode
    vs. stories mode; the spec-folder input feeds a live schedule preview that
    validates ``stories.yaml`` (parses + rules pass, SPEC.md present) and lists
    the linear schedule with independent spec/done checkpoint markers — the same
    projection `run --dry-run` prints. Returns
    ``{source, spec_folder, epic, story, max_stories, dry_run}``."""

    DEFAULT_CSS = """
    StartRunModal #dialog {
        height: 90%;
    }
    StartRunModal #fields {
        height: 1fr;
    }
    StartRunModal #preview {
        height: auto;
        max-height: 14;
        border: solid $primary-darken-2;
        padding: 0 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        project: Path,
        *,
        default_source: str = "sprint-status",
        default_spec_folder: str = "",
    ):
        super().__init__()
        self._project = project
        self._default_source = default_source
        self._default_spec_folder = default_spec_folder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("start run", classes="title")
            # fields scroll; the button row is docked below so it stays clickable
            # in any terminal size (the modal is tall in stories mode).
            with VerticalScroll(id="fields"):
                yield Select(
                    [
                        ("sprint mode — sprint-status.yaml", "sprint-status"),
                        ("stories mode — folder+id dispatch", "stories"),
                    ],
                    value=self._default_source,
                    allow_blank=False,
                    id="source",
                )
                yield Input(
                    value=self._default_spec_folder,
                    placeholder="stories mode: spec folder holding stories.yaml + SPEC.md",
                    id="spec-folder",
                )
                yield Input(
                    placeholder="epic — blank for all (sprint mode)",
                    type="integer",
                    valid_empty=True,
                    id="epic",
                )
                yield Input(
                    placeholder="story — 3-1 / slug / full key (sprint), or story id (stories)",
                    id="story",
                )
                yield Input(
                    placeholder="max stories — blank for no limit",
                    type="integer",
                    valid_empty=True,
                    id="max-stories",
                )
                yield Checkbox("dry run (print the plan, spawn nothing)", id="dry-run")
                with VerticalScroll(id="preview"):
                    yield Static(id="preview-body")
            with Horizontal(classes="buttons"):
                yield Button("start", variant="primary", id="ok")
                yield Button("cancel", id="cancel")

    def on_mount(self) -> None:
        self._refresh_preview()

    def on_select_changed(self, event: Select.Changed) -> None:
        self._refresh_preview()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "spec-folder":
            self._refresh_preview()

    def _refresh_preview(self) -> None:
        try:
            body = self.query_one("#preview-body", Static)
            source = self.query_one("#source", Select).value
            spec_folder = self.query_one("#spec-folder", Input).value.strip()
        except NoMatches:
            return  # a Changed message during mount, before the tree is built
        if source != "stories":
            body.update(Text("sprint mode — walks sprint-status.yaml", style="dim"))
            return
        if not spec_folder:
            body.update(
                Text("stories mode needs a spec folder (stories.yaml + SPEC.md)", style="yellow")
            )
            return
        folder = stories.resolve_spec_folder(self._project, spec_folder)
        try:
            rows = stories.story_rows(folder)
        except stories.StoriesError as e:
            body.update(Text(f"⚠ {e}", style="red"))
            return
        text = Text()
        text.append(f"{len(rows)} stories · linear order", style="bold")
        if not (folder / "SPEC.md").is_file():
            text.append("  ⚠ SPEC.md missing", style="red")
        for r in rows:
            text.append(f"\n  {r.position}. {r.id} ({r.label})")
            marks = [
                m for m, on in (("spec", r.spec_checkpoint), ("done", r.done_checkpoint)) if on
            ]
            if marks:
                text.append(f" [{'/'.join(marks)}]", style="magenta")
            text.append(f"  {r.title}", style="dim")
        body.update(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "ok":
            self.dismiss(None)
            return
        self.dismiss(
            {
                "source": self.query_one("#source", Select).value,
                "spec_folder": self.query_one("#spec-folder", Input).value.strip(),
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


class SpecReviewModal(BaseDialog):
    """Read-only story-spec viewer with a configurable action row.

    Shared by the plan-checkpoint viewer (Approve & resume / Request replan) and
    the spec-approval / epic gate viewer (Approve & resume). Dismisses with the
    chosen action verb, or None on close/escape. The spec path is shown
    prominently with a copy-path action; the spec body is LLM-written markdown so
    it renders as plain Text, never markup. The modal owns no logic — the caller
    maps each verb to the exact CLI code path (resume / reset-to-draft + resume)."""

    DEFAULT_CSS = """
    SpecReviewModal #dialog {
        width: 100;
        height: 85%;
    }
    SpecReviewModal #spec {
        height: 1fr;
        border: solid $primary-darken-2;
        padding: 0 1;
    }
    SpecReviewModal .path {
        color: $text-muted;
        margin-bottom: 1;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        subtitle: str | Text,
        spec_path: Path | None,
        spec_text: str,
        actions: list[tuple[str, str, str]],
    ):
        super().__init__()
        self._title = title
        self._subtitle = subtitle if isinstance(subtitle, Text) else Text(subtitle)
        self._spec_path = spec_path
        self._spec_text = spec_text
        self._actions = actions

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, classes="title")
            yield Static(self._subtitle)
            path_line = Text()
            if self._spec_path is not None:
                path_line.append(str(self._spec_path))
            else:
                path_line.append("(no spec file resolved)", style="dim")
            yield Static(path_line, classes="path")
            with VerticalScroll(id="spec"):
                body = self._spec_text.strip()
                yield Static(Text(body) if body else Text("(empty spec)", style="dim"))
            with Horizontal(classes="buttons"):
                if self._spec_path is not None:
                    yield Button("copy path", id="copy-path")
                for verb, label, variant in self._actions:
                    yield Button(label, variant=variant, id=f"act-{verb}")  # type: ignore[arg-type]
                yield Button("close", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "copy-path" and self._spec_path is not None:
            self.app.copy_to_clipboard(str(self._spec_path))
            self.app.notify("spec path copied to clipboard")
            return
        if bid.startswith("act-"):
            self.dismiss(bid[len("act-") :])
            return
        self.dismiss(None)


class StoryCheckpointModal(BaseDialog):
    """done_checkpoint summary card shown after a story commits: id/title, the
    commit subject + short hash, a gate line derived from real task state (the
    verify + review gates the commit cleared, plus the follow-up review-cycle
    count) and token totals. Dismisses with 'continue' (resume the schedule) or
    'stop' (mark the run stopped), None on close/escape."""

    def __init__(
        self,
        *,
        story_key: str,
        title: str,
        commit: str,
        verify_line: str,
        tokens: str,
    ):
        super().__init__()
        self._story_key = story_key
        self._title = title
        self._commit = commit
        self._verify_line = verify_line
        self._tokens = tokens

    def compose(self) -> ComposeResult:
        head = Text()
        head.append(f"story checkpoint — {self._story_key}", style="bold")
        with Vertical(id="dialog"):
            yield Label(head, classes="title")
            if self._title:
                yield Static(Text(self._title))
            card = Text()
            card.append("\ncommit  ", style="dim")
            card.append(self._commit or "(none)", style="green")
            card.append("\nverify  ", style="dim")
            card.append(self._verify_line)
            card.append("\ntokens  ", style="dim")
            card.append(self._tokens, style="dim")
            yield Static(card)
            with Horizontal(classes="buttons"):
                yield Button("Continue run", variant="primary", id="act-continue")
                yield Button("Stop run", variant="warning", id="act-stop")
                yield Button("close", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        self.dismiss(bid[len("act-") :] if bid.startswith("act-") else None)


class EscalationModal(BaseDialog):
    """Blocked-story escalation view with story context: the story entry's
    title/description (stories mode), the blocking condition parsed from the
    spec's ``## Auto Run Result``, and a sentinel indicator when the matched spec
    is a fixed-slug pre-planning-halt sentinel. Dismisses with 'resolve' (launch
    the interactive resolve agent) or 'rearm' (re-arm + resume — only offered once
    the resolution marker exists), None on close/escape."""

    DEFAULT_CSS = """
    EscalationModal #dialog {
        width: 90;
        height: auto;
        max-height: 90%;
    }
    EscalationModal #blocking {
        height: auto;
        max-height: 40%;
        margin-top: 1;
        border: solid $primary-darken-2;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        *,
        story_key: str,
        title: str,
        description: str,
        blocking: str,
        sentinel_kind: str,
        resolution_ready: bool,
        engine_live: bool,
    ):
        super().__init__()
        self._story_key = story_key
        self._title = title
        self._description = description
        self._blocking = blocking
        self._sentinel_kind = sentinel_kind
        self._resolution_ready = resolution_ready
        self._engine_live = engine_live

    def compose(self) -> ComposeResult:
        head = Text()
        head.append(f"escalation — {self._story_key}", style="bold red")
        with Vertical(id="dialog"):
            yield Label(head, classes="title")
            if self._title:
                yield Static(Text(self._title, style="bold"))
            if self._description:
                yield Static(Text(self._description, style="dim"))
            if self._sentinel_kind:
                yield Static(
                    Text(
                        f"⚠ pre-planning-halt sentinel ({self._sentinel_kind}) — "
                        "re-arm deletes it (a copy is preserved) for a clean re-dispatch",
                        style="yellow",
                    )
                )
            with VerticalScroll(id="blocking"):
                body = self._blocking.strip()
                yield Static(
                    Text(body) if body else Text("(no blocking condition recorded)", style="dim")
                )
            if self._engine_live:
                yield Static(
                    Text("engine may still be live — stop it before resolving", style="yellow")
                )
            hint = Text()
            if self._resolution_ready:
                hint.append("resolution recorded — re-arm & resume when ready", style="green")
            else:
                hint.append(
                    "resolve opens an interactive agent to fix the frozen spec; "
                    "re-arm unlocks once it records a resolution",
                    style="dim",
                )
            yield Static(hint)
            with Horizontal(classes="buttons"):
                yield Button(
                    "Resolve", variant="primary", id="act-resolve", disabled=self._engine_live
                )
                yield Button(
                    "Re-arm & resume",
                    variant="warning",
                    id="act-rearm",
                    disabled=not self._resolution_ready or self._engine_live,
                )
                yield Button("close", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        self.dismiss(bid[len("act-") :] if bid.startswith("act-") else None)


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
