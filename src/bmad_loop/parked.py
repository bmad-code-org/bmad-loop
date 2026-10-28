"""Journal projection of a live parked permission prompt (#855).

The adapter journals `session-parked` on each PermissionPrompt latch (with the
pane the prompt waits in, the hook Notification subtype, and the CLI's parsed
`auto_deny_at` deadline when its countdown was on screen) and one
`session-unparked` when the prompt closes. This module folds those events back
into "is the open session parked right now, and where" — plain stdlib, so the
TUI header and Log pane and the core answer path read the same answer without
the core path importing the TUI's pyte/rich dependencies.

The open-session scoping lives here too: a park belongs to a session only from
its `session-start` onward, so a park left behind by an earlier session with a
reused task id never reads as the current one's.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Journal kinds that close an open park for the same task: the adapter's own
# `session-unparked` and an answer sent from the TUI. Not `session-active`: the
# transcript write that raised the prompt can close an idle stretch on the next
# heartbeat, after the latch. The adapter measures activity from a baseline
# stat'ed at the latch and journals `session-unparked` (`how="activity"`) itself.
PARK_CLOSERS = frozenset({"session-unparked", "parked-answered"})


@dataclass(frozen=True)
class ParkedPrompt:
    """The open session's live permission prompt: the journal `ts` of the
    newest unclosed `session-parked` (`since`), the CLI's epoch auto-deny
    deadline (`auto_deny_at`, None when the countdown was not parsed), the mux
    pane target it waits in (`pane`, None when unrecorded) and the hook
    Notification subtype that latched it (`subtype`, None for a direct parked
    event)."""

    since: float
    auto_deny_at: float | None
    pane: str | None
    subtype: str | None


def open_session_start_indexed(
    journal_entries: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, int]:
    """The session-start entry whose agent session is currently open — the last
    session-start with no later matching session-end — plus its index in
    `journal_entries` (``(None, -1)`` when every started session has ended or
    none started), so a caller can scan the entries that FOLLOW the open start.
    The task_id is tracked as a string so the session-end match is
    byte-identical to what the TUI's `active_task_id` compares."""
    open_entry: dict[str, Any] | None = None
    open_index = -1
    active: str | None = None
    for index, entry in enumerate(journal_entries):
        kind = entry.get("kind")
        if kind == "session-start" and entry.get("task_id") is not None:
            active = str(entry["task_id"])
            open_entry = entry
            open_index = index
        elif kind == "session-end" and str(entry.get("task_id")) == active:
            active = None
            open_entry = None
            open_index = -1
    return open_entry, open_index


def _finite(raw: object) -> float | None:
    """`raw` as a finite float, or None for a non-number, a bool, an overflow or
    a NaN/inf."""
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def parked_prompt(
    journal_entries: list[dict[str, Any]], start: int, task_id: str
) -> ParkedPrompt | None:
    """The live parked prompt of `task_id`'s session opened at index `start`,
    or None: the newest `session-parked` for the task after `start`, unless a
    later `session-unparked` or `parked-answered` for the same task closed it.
    The newest park wins — the adapter re-journals one with the deadline a tick
    after the first, and a re-latch re-emits without an unpark between. A `session-parked` without a finite numeric `ts` is skipped without
    erasing an earlier open park; an ill-typed `pane`/`subtype`/`auto_deny_at`
    reads as unrecorded. Never raises on a malformed entry."""
    parked: ParkedPrompt | None = None
    for entry in journal_entries[start + 1 :]:
        if not isinstance(entry, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            continue
        if str(entry.get("task_id")) != task_id:
            continue
        kind = entry.get("kind")
        if kind == "session-parked":
            since = _finite(entry.get("ts"))
            if since is None:
                continue
            pane = entry.get("pane")
            subtype = entry.get("subtype")
            parked = ParkedPrompt(
                since=since,
                auto_deny_at=_finite(entry.get("auto_deny_at")),
                pane=pane if isinstance(pane, str) and pane else None,
                subtype=subtype if isinstance(subtype, str) and subtype else None,
            )
        elif kind in PARK_CLOSERS:
            parked = None
    return parked
