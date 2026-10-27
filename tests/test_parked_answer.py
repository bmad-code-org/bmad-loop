"""Answering a parked permission prompt from the TUI (parked_answer, #855): keys
go out only when every check passes on a fresh capture, and every refusal sends
nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

from bmad_loop import parked_answer, platform_util
from bmad_loop.adapters.multiplexer import MultiplexerError
from bmad_loop.journal import Journal, load_state, save_state
from bmad_loop.model import RunState
from bmad_loop.runs import RUNS_DIR, session_name

RUN_ID = "20261027-100000"
TASK = "1-1-dev-1"
PANE = "@3.%5"

# The #855 report's screen (its countdown line split so this file never holds it).
PROMPT_SCREEN = (
    "⚠ Claude Code will automatically deny this reques"
    + "t in 2:00, to avoid blocking progress on an unattended session\n"
    "\n"
    "Do you want to proceed?\n"
    "❯ 1. Yes\n"
    "  2. No\n"
)
# Claude Code's bypass startup dialog: Enter confirms the focused "No, exit", and
# Escape exits too. The heading is split: claude's parked pattern matches it whole.
BYPASS_SCREEN = (
    "WARNING: Claude Code running in Bypass Permissions mod" + "e\n"
    "\n"
    "❯ 1. No, exit\n"
    "  2. Yes, I accept\n"
)
# An MCP elicitation form, which also latches PermissionPrompt.
ELICITATION_SCREEN = (
    "MCP server tracker requests information\n" "\n" "❯ Project key: \n" "  Submit\n"
)


class RecordingMux:
    """Duck-typed mux: answers a fixed window list and screen, records every
    capture and send, and raises a scripted MultiplexerError per op."""

    def __init__(self, screen: str = PROMPT_SCREEN, windows: tuple[str, ...] = ("@3",)):
        self.screen = screen
        self.windows = list(windows)
        self.captures: list[str] = []
        self.sends: list[tuple[str, tuple[str, ...]]] = []
        self.listed: list[str] = []
        self.fail: dict[str, MultiplexerError] = {}

    def list_window_ids(self, session: str) -> list[str]:
        self.listed.append(session)
        if "list" in self.fail:
            raise self.fail["list"]
        return self.windows

    def capture_pane(self, target: str) -> str:
        self.captures.append(target)
        if "capture" in self.fail:
            raise self.fail["capture"]
        return self.screen

    def send_keys(self, target: str, *keys: str) -> None:
        if "send" in self.fail:
            raise self.fail["send"]
        self.sends.append((target, keys))


def _seed(project: Path, *, adapter: str = "claude", closers: tuple[str, ...] = ()) -> Path:
    """A run whose open dev session is parked on a permission prompt in PANE,
    with the state pinned to its run-dir identity; each of `closers` is then
    journaled for the task, in order."""
    run_dir = project / RUNS_DIR / RUN_ID
    run_dir.mkdir(parents=True)
    state = RunState(
        run_id=RUN_ID,
        project=str(project),
        started_at="2026-10-27T10:00:00",
        run_dir_identity=platform_util.root_identity_record(run_dir),
    )
    save_state(run_dir, state)
    journal = Journal(run_dir, pin=state)
    journal.append("session-start", task_id=TASK, role="dev", adapter=adapter, model="")
    journal.append("session-parked", task_id=TASK, pane=PANE, subtype="permission_prompt")
    for kind in closers:
        journal.append(kind, task_id=TASK)
    return run_dir


def _entries(run_dir: Path) -> list[dict]:
    return Journal(run_dir, pin=load_state(run_dir)).entries()


def _answered(run_dir: Path) -> list[dict]:
    return [e for e in _entries(run_dir) if e["kind"] == "parked-answered"]


@pytest.mark.parametrize(("choice", "keys"), [("approve", ("Enter",)), ("deny", ("Escape",))])
def test_a_live_prompt_gets_its_keys_and_a_pinned_parked_answered(tmp_path, choice, keys):
    run_dir = _seed(tmp_path)
    mux = RecordingMux()
    outcome = parked_answer.answer(tmp_path, RUN_ID, choice, mux=mux)
    assert outcome.sent, outcome.reason
    assert mux.listed == [session_name(RUN_ID)]
    assert mux.captures == [PANE]
    assert mux.sends == [(PANE, keys)]
    answered = _answered(run_dir)
    assert [(e["task_id"], e["choice"], e["by"]) for e in answered] == [(TASK, choice, "tui")]
    # written through the pinned writer: no unpinned-write notice
    assert [e for e in _entries(run_dir) if e["kind"] == "journal-unpinned"] == []


def test_a_screen_missing_one_guard_sends_nothing(tmp_path):
    """Focus moved to "2. No": the question is still on screen, but approve's
    Enter would now deny, so approve refuses.

    ABLATION: drop the `_unmatched_guard` check in `answer` and Enter is sent."""
    run_dir = _seed(tmp_path)
    moved = PROMPT_SCREEN.replace("❯ 1. Yes", "  1. Yes").replace("  2. No", "❯ 2. No")
    mux = RecordingMux(screen=moved)
    outcome = parked_answer.answer(tmp_path, RUN_ID, "approve", mux=mux)
    assert not outcome.sent
    assert "guard" in outcome.reason
    assert mux.captures == [PANE]
    assert mux.sends == []
    assert _answered(run_dir) == []


@pytest.mark.parametrize("choice", ["approve", "deny"])
@pytest.mark.parametrize("screen", [BYPASS_SCREEN, ELICITATION_SCREEN], ids=["bypass", "elicit"])
def test_other_dialogs_refuse_both_answers(tmp_path, choice, screen):
    """The bypass startup dialog (Enter = "No, exit", Escape exits) and an
    elicitation form both latch as a parked prompt; neither shows the question,
    so neither key is pressed."""
    _seed(tmp_path)
    mux = RecordingMux(screen=screen)
    outcome = parked_answer.answer(tmp_path, RUN_ID, choice, mux=mux)
    assert not outcome.sent
    assert mux.sends == []


@pytest.mark.parametrize("closer", ["session-unparked", "session-active", "parked-answered"])
def test_a_closed_park_refuses_before_touching_the_pane(tmp_path, closer):
    """A newer unpark (or activity, or an earlier answer) closes the park, so
    nothing is captured or sent even though the screen would pass.

    ABLATION: skip the `parked_prompt` check and the keys are sent."""
    _seed(tmp_path, closers=(closer,))
    mux = RecordingMux()
    outcome = parked_answer.answer(tmp_path, RUN_ID, "approve", mux=mux)
    assert not outcome.sent
    assert "not parked" in outcome.reason
    assert (mux.captures, mux.sends) == ([], [])


def test_a_dead_window_refuses(tmp_path):
    """ABLATION: drop the window-liveness check and the keys are sent."""
    _seed(tmp_path)
    mux = RecordingMux(windows=("@9",))
    outcome = parked_answer.answer(tmp_path, RUN_ID, "deny", mux=mux)
    assert not outcome.sent
    assert "window is gone" in outcome.reason
    assert (mux.captures, mux.sends) == ([], [])


def test_a_profile_without_parked_answers_refuses(tmp_path):
    """gemini ships no `[parked_answers]`: no keys to send, whatever the screen.

    ABLATION: drop the `parked_answers is None` refusal and this raises instead
    of refusing."""
    _seed(tmp_path, adapter="gemini")
    mux = RecordingMux()
    outcome = parked_answer.answer(tmp_path, RUN_ID, "approve", mux=mux)
    assert not outcome.sent
    assert "[parked_answers]" in outcome.reason
    assert (mux.captures, mux.sends) == ([], [])


def test_a_project_overlay_profile_is_resolved(tmp_path):
    """The session's profile comes from the same resolver launch uses, project
    overlays included: an overlay `claude` without the table offers no keys."""
    _seed(tmp_path)
    overlay = tmp_path / ".bmad-loop" / "profiles"
    overlay.mkdir(parents=True)
    (overlay / "claude.toml").write_text(
        'name = "claude"\nbinary = "claude"\n\n[hooks]\ndialect = "none"\n', encoding="utf-8"
    )
    mux = RecordingMux()
    outcome = parked_answer.answer(tmp_path, RUN_ID, "approve", mux=mux)
    assert not outcome.sent
    assert "[parked_answers]" in outcome.reason
    assert mux.sends == []


@pytest.mark.parametrize("op", ["list", "capture", "send"])
def test_a_mux_fault_is_a_refusal_not_a_raise(tmp_path, op):
    """A transport fault at any step answers `sent=False` with its message, and
    nothing is journaled as answered."""
    run_dir = _seed(tmp_path)
    mux = RecordingMux()
    mux.fail[op] = MultiplexerError(f"{op} broke")
    outcome = parked_answer.answer(tmp_path, RUN_ID, "approve", mux=mux)
    assert not outcome.sent
    assert f"{op} broke" in outcome.reason
    assert mux.sends == []
    assert _answered(run_dir) == []


def test_no_run_and_no_open_session_refuse(tmp_path):
    mux = RecordingMux()
    assert not parked_answer.answer(tmp_path, "missing", "approve", mux=mux).sent
    _seed(tmp_path, closers=())
    run_dir = tmp_path / RUNS_DIR / RUN_ID
    Journal(run_dir, pin=load_state(run_dir)).append("session-end", task_id=TASK)
    outcome = parked_answer.answer(tmp_path, RUN_ID, "approve", mux=mux)
    assert (outcome.sent, outcome.reason) == (False, "no open session")
    assert mux.sends == []
