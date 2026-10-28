"""Answer a parked permission prompt from the TUI (#855).

The adapter journals `session-parked` when a session's CLI is waiting on a
permission prompt, and :mod:`bmad_loop.parked` projects it back. This module is
the one path that presses keys into that pane: an operator's approve or deny,
sent as the profile's ``[parked_answers]`` keys.

Pressing a key into a live coding CLI is the riskiest thing the dashboard does,
so every step must pass before anything is sent, and any doubt refuses with a
reason instead:

- Any keypress cancels Claude Code's auto-deny countdown, after which the
  prompt waits forever, so a key sent to the wrong screen strands the session.
- The same keys answer other dialogs differently: Enter on the bypass startup
  dialog chooses its focused "No, exit", and Escape there exits too.
- Elicitation dialogs latch the same PermissionPrompt kind.

Hence the guards: a FRESH capture of the visible pane must match every pattern
of the chosen answer's guard list. A refusal is an :class:`AnswerOutcome` with
``sent=False``; nothing here raises on a transport or read fault.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import regex

from .adapters.multiplexer import MultiplexerError, TerminalMultiplexer
from .adapters.panematch import Hit, match_rows, screen_rows
from .adapters.profile import ProfileError, get_profile
from .journal import Journal, load_state
from .parked import open_session_start_indexed, parked_prompt
from .runs import run_dir_for, session_name

# Per-guard search budget; the same bound the adapter's parked-prompt look uses.
GUARD_MATCH_TIMEOUT_S = 2.0

Choice = Literal["approve", "deny"]


@dataclass(frozen=True)
class AnswerOutcome:
    """``sent`` is True only when the keys went out; ``reason`` says why not,
    or what was sent."""

    sent: bool
    reason: str


def _window_of(pane: str) -> str:
    """The window id a recorded pane target names: ``<window>.%N`` (see
    :meth:`~.adapters.multiplexer.TerminalMultiplexer.pane_target`) or the bare
    window id a backend without pane targets records."""
    window, dot, tail = pane.rpartition(".")
    return window if dot and tail.startswith("%") else pane


def _unmatched_guard(screen: str, guards: tuple[str, ...]) -> str | None:
    """The first guard ``screen`` does not plainly match (a search timeout counts
    as no match), or None when every guard matched."""
    rows = screen_rows(screen)
    for guard in guards:
        found = match_rows(rows, [regex.compile(guard)], timeout=GUARD_MATCH_TIMEOUT_S)
        if not isinstance(found, Hit):
            return guard
    return None


def answer(
    project: Path, run_id: str, choice: Choice, *, mux: TerminalMultiplexer
) -> AnswerOutcome:
    """Press the ``choice`` keys of the parked session's profile into its pane,
    then journal ``parked-answered``, or refuse with a reason when any check
    fails: the journal reads, the open session's prompt is still live, its
    window is alive, its profile has ``[parked_answers]``, and a fresh capture
    matches every guard of ``choice``."""
    run_dir = run_dir_for(project, run_id)
    try:
        state = load_state(run_dir)
        journal = Journal(run_dir, pin=state)
        entries = journal.entries()
    except (OSError, ValueError) as e:
        return AnswerOutcome(False, f"run {run_id} unreadable: {e}")

    start_entry, start = open_session_start_indexed(entries)
    if start_entry is None:
        return AnswerOutcome(False, "no open session")
    task_id = str(start_entry["task_id"])
    prompt = parked_prompt(entries, start, task_id)
    if prompt is None:
        return AnswerOutcome(False, f"{task_id} is not parked on a permission prompt")
    if prompt.pane is None:
        return AnswerOutcome(False, f"{task_id}'s parked pane was not recorded")
    pane = prompt.pane

    try:
        alive = _window_of(pane) in mux.list_window_ids(session_name(run_id))
    except MultiplexerError as e:
        return AnswerOutcome(False, f"window liveness unknown: {e}")
    if not alive:
        return AnswerOutcome(False, f"{task_id}'s window is gone")

    adapter = start_entry.get("adapter")
    if not isinstance(adapter, str) or not adapter:
        return AnswerOutcome(False, f"{task_id}'s session recorded no adapter")
    try:
        profile = get_profile(adapter, project)
    except (ProfileError, OSError) as e:
        return AnswerOutcome(False, f"profile {adapter!r} unresolved: {e}")
    answers = profile.parked_answers
    if answers is None:
        return AnswerOutcome(False, f"profile {adapter!r} has no [parked_answers]")
    keys, guards = (
        (answers.approve, answers.approve_guard)
        if choice == "approve"
        else (answers.deny, answers.deny_guard)
    )

    try:
        screen = mux.capture_pane(pane)
    except MultiplexerError as e:
        return AnswerOutcome(False, f"pane capture failed: {e}")
    if (missing := _unmatched_guard(screen, guards)) is not None:
        return AnswerOutcome(False, f"the pane does not show the prompt (guard {missing!r})")

    try:
        mux.send_keys(pane, *keys)
    except MultiplexerError as e:
        return AnswerOutcome(False, f"keys not sent: {e}")
    try:
        journal.append("parked-answered", task_id=task_id, choice=choice, by="tui")
    except OSError as e:
        # The keys are already in the pane, so this is still a send; say the
        # record is missing rather than hide it.
        return AnswerOutcome(True, f"sent {choice}; journal write failed: {e}")
    return AnswerOutcome(True, f"sent {choice} ({' '.join(keys)}) to {task_id}")
