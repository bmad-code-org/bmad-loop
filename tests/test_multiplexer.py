"""Multiplexer-seam proof.

Drives a full ``GenericAdapter`` start/wait cycle against a stub
``TerminalMultiplexer`` with **no tmux on PATH** and the tmux backend's
subprocess seam booby-trapped, proving the adapter never shells out to tmux
directly — every transport op goes through ``self.mux``. Mirrors MockAdapter's
role for the transport axis.
"""

import json
import os
import shlex
import subprocess
import sys

import pytest
from conftest import needs_strict_codec

from bmad_loop.adapters import multiplexer, tmux_base
from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.generic import GenericAdapter
from bmad_loop.adapters.multiplexer import MultiplexerError, TerminalMultiplexer, parse_target
from bmad_loop.adapters.profile import get_profile
from bmad_loop.adapters.tmux_backend import TmuxMultiplexer
from bmad_loop.policy import LimitsPolicy, Policy


class StubMux(TerminalMultiplexer):
    """Records the transport ops the adapter performs; never touches a real
    multiplexer. Unused ops raise, so the test also pins exactly which ops the
    adapter relies on."""

    def __init__(self):
        self.calls: list[str] = []
        self.window_env: dict[str, str] = {}
        self._sessions: set[str] = set()
        self._windows: dict[str, list[str]] = {}
        self._next = 0

    # ---- ops the adapter uses (record + minimal real behavior)
    def has_session(self, name):
        self.calls.append("has_session")
        return name in self._sessions

    def new_session(self, name, cwd, cols=None, lines=None):
        self.calls.append("new_session")
        self._sessions.add(name)
        self._windows[name] = []

    def set_session_option(self, name, option, value):
        self.calls.append("set_session_option")

    def new_window(self, session, name, cwd, env, command):
        self.calls.append("new_window")
        self.window_env = env
        self._next += 1
        win = f"@stub{self._next}"
        self._windows.setdefault(session, []).append(win)
        return win

    def pipe_pane(self, window_id, log_file):
        self.calls.append("pipe_pane")

    def list_window_ids(self, session):
        self.calls.append("list_window_ids")
        return list(self._windows.get(session, []))

    def send_text(self, window_id, text):
        self.calls.append("send_text")

    def kill_window(self, target):
        self.calls.append("kill_window")
        for wins in self._windows.values():
            if target in wins:
                wins.remove(target)

    def available(self):
        return True

    # ---- ops the adapter must NOT touch
    def kill_session(self, name):
        raise AssertionError("adapter must not call kill_session")

    def list_sessions(self):
        raise AssertionError("adapter must not call list_sessions")

    def session_options(self, option):
        raise AssertionError("adapter must not call session_options")

    def new_parked_window(self, session, name, cwd, argv, return_opt):
        raise AssertionError("adapter must not call new_parked_window")

    def list_windows(self, session, fields):
        raise AssertionError("adapter must not call list_windows")

    def window_alive(self, session, window_id):
        raise AssertionError("adapter must not call window_alive")

    def select_window(self, target):
        raise AssertionError("adapter must not call select_window")

    def set_window_option(self, target, option, value):
        raise AssertionError("adapter must not call set_window_option")

    def unset_window_option(self, target, option):
        raise AssertionError("adapter must not call unset_window_option")

    def show_window_option(self, target, option):
        raise AssertionError("adapter must not call show_window_option")

    def attach_target_argv(self, target):
        raise AssertionError("adapter must not call attach_target_argv")

    def current_pane_id(self):
        raise AssertionError("adapter must not call current_pane_id")

    def current_window_id(self):
        raise AssertionError("adapter must not call current_window_id")

    def current_session(self):
        raise AssertionError("adapter must not call current_session")

    def detach_client(self):
        raise AssertionError("adapter must not call detach_client")

    def switch_client(self, target, last_fallback=False):
        raise AssertionError("adapter must not call switch_client")


@pytest.fixture
def no_tmux(monkeypatch):
    """tmux off PATH, and the backend's subprocess seam booby-trapped: any direct
    shell-out to tmux fails the test loudly."""
    monkeypatch.setenv("PATH", "")

    def boom(*a, **k):
        raise AssertionError("GenericAdapter shelled out to tmux directly")

    monkeypatch.setattr(tmux_base.subprocess, "run", boom)


def _spec(tmp_path):
    task_id = "1-1-dev-1"
    return SessionSpec(
        task_id=task_id,
        role="dev",
        prompt="/bmad-dev-auto 1-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_RUN_DIR": str(tmp_path / "run"), "BMAD_LOOP_TASK_ID": task_id},
        timeout_s=10.0,
    )


def test_generic_adapter_drives_only_the_mux(tmp_path, no_tmux):
    stub = StubMux()
    adapter = GenericAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("claude"),
        mux=stub,
        # out of the project tree, as `runsetup.make_adapters` resolves it (#494),
        # so the seeded Stop below has to be observed on the PRIMARY channel
        events_dir=tmp_path / "state" / "events",
    )
    spec = _spec(tmp_path)

    handle = adapter.start_session(spec)
    assert handle.native_id == "@stub1"
    # session bootstrap + window launch + log tee all went through the mux
    assert stub.calls == [
        "has_session",
        "new_session",
        "set_session_option",
        "new_window",
        "pipe_pane",
    ]

    # Seed a fresh Stop event + result.json (ts above the launch floor) so the
    # wait observes a normal completion without any real process.
    ts = handle.launched_ns + 1
    events_dir = adapter.watcher.events_dir
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"{ts}-{spec.task_id}-Stop.json").write_text(
        json.dumps({"ts": ts, "event": "Stop", "task_id": spec.task_id, "session_id": "s1"}),
        encoding="utf-8",
    )
    (adapter.tasks_dir / spec.task_id / "result.json").write_text(
        json.dumps({"workflow": "auto-dev"}), encoding="utf-8"
    )

    result = adapter.wait_for_completion(handle, spec)
    assert result.status == "completed"
    assert result.result_json == {"workflow": "auto-dev"}
    assert result.session_id == "s1"

    adapter.kill(handle)
    assert "kill_window" in stub.calls


def test_generic_adapter_window_env_pins_the_state_root_over_profile(
    tmp_path, no_tmux, monkeypatch
):
    """The engine's window merge (`{**profile.env, **spec.env}`) rides through
    the `runs.pin_state_root` chokepoint: a profile `[env]` table declaring
    `BMAD_LOOP_STATE_DIR` is forced to this process's resolved root when one
    derives, and STRIPPED when none does — with an underivable root there is no
    pin key in `spec.env` for mere merge order to protect, and the profile's
    absolute value would otherwise aim the coding window at a registry its own
    orchestrator cannot see. `interactive_env` (the attached resolve path)
    applies the same rule.

    Ablate the `runs.pin_state_root` wrap at either merge and the matching
    assertion fails."""
    import dataclasses

    from bmad_loop import envvars, runs

    def make_adapter():
        stub = StubMux()
        adapter = GenericAdapter(
            run_dir=tmp_path / "run",
            policy=Policy(limits=LimitsPolicy()),
            profile=dataclasses.replace(
                get_profile("claude"), env={envvars.STATE_DIR: str(tmp_path / "S2")}
            ),
            mux=stub,
            events_dir=tmp_path / "state" / "events",
        )
        return stub, adapter

    spec = _spec(tmp_path)

    # derivable: the profile's S2 is overwritten with the resolved root
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path / "S1"))
    stub, adapter = make_adapter()
    adapter.start_session(spec)
    assert stub.window_env[envvars.STATE_DIR] == str(runs.state_root())
    assert adapter.interactive_env(spec)[envvars.STATE_DIR] == str(runs.state_root())

    # underivable: no pin exists, so the profile's entry is stripped outright
    monkeypatch.setenv(envvars.STATE_DIR, "relative-root")
    stub, adapter = make_adapter()
    adapter.start_session(spec)
    assert envvars.STATE_DIR not in stub.window_env
    assert envvars.STATE_DIR not in adapter.interactive_env(spec)
    # ...and the rest of the profile/spec env is untouched
    assert stub.window_env["BMAD_LOOP_TASK_ID"] == spec.task_id


# --------------------------------------------------------------- seam honesty
#
# Phase 1: no tmux contract method may leak a raw subprocess.TimeoutExpired /
# OSError. The one place a subprocess is spawned (_run) deliberately propagates
# those raw; the guarantee is enforced one level up, in the inherited contract
# methods, so it holds even for a psmux that overrides only _run.


@pytest.fixture(params=[subprocess.TimeoutExpired(["tmux"], 30), FileNotFoundError("tmux")])
def boom_run(request, monkeypatch):
    """tmux 'present' on PATH, but the single subprocess spawn always raises a raw
    transport error (parametrized over a timeout and a missing binary)."""
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: "/usr/bin/tmux")

    def boom(*_a, **_k):
        raise request.param

    monkeypatch.setattr(tmux_base.subprocess, "run", boom)
    return request.param


def test_seam_methods_never_leak_raw_subprocess_error(boom_run, tmp_path):
    mux = TmuxMultiplexer()

    # Raisers: liveness / mutating ops re-raise as the seam type. A raw timeout
    # would escape the MultiplexerError contract; a sentinel would mis-read as a
    # real (empty/absent) answer — so these MUST raise, and as MultiplexerError.
    raisers = [
        lambda: mux.list_window_ids("s"),
        lambda: mux.window_alive("s", "@1"),
        lambda: mux.new_session("s", tmp_path),
        lambda: mux.new_window("s", "n", tmp_path, {}, "cmd"),
        lambda: mux.set_session_option("s", "opt", "val"),
        lambda: mux.new_parked_window("s", "n", tmp_path, ["echo", "hi"], ""),
        lambda: mux.send_text("@1", "hi"),
        lambda: mux.has_session("s"),  # already-correct raiser — lock it in
    ]
    for call in raisers:
        with pytest.raises(MultiplexerError) as excinfo:
            call()
        # the seam type, never a raw subprocess / OS error leaking through
        assert not isinstance(excinfo.value, subprocess.SubprocessError)
        assert not isinstance(excinfo.value, OSError)

    # Sentinel returners: a transport failure degrades to the documented value
    # (never a raise, never a mis-typed answer).
    assert mux.list_windows("s", ["window_id"]) == []
    assert mux.show_window_option("@1", "opt") == ""
    assert mux.detach_client() is False
    assert mux.kill_window("@1") is None
    assert mux.select_window("@1") is None
    assert mux.set_window_option("@1", "opt", "val") is None
    assert mux.unset_window_option("@1", "opt") is None
    assert mux.pipe_pane("@1", tmp_path / "log") is None
    assert mux.window_pane_pids("@1") == []

    # switch_client is the one sentinel that splits the two faults the rest
    # collapse: a missing binary never ran the verb, so nobody moved and whoever
    # was in front of this window still is — the joint claim False stands for.
    # A timeout may have completed the switch server-side, and only None can say
    # so; a False there tells the return path to keep prompting a window the
    # client has already left (#659, one seam up).
    unvouched = None if isinstance(boom_run, subprocess.TimeoutExpired) else False
    assert mux.switch_client("s") is unvouched
    assert mux.switch_client("s", last_fallback=True) is unvouched

    # Already-correct swallowers stay swallowing (lock-in).
    assert mux.kill_session("s") is None
    assert mux.list_sessions() == []
    assert mux.session_options("opt") == {}
    assert mux.version() is None
    assert mux.current_pane_id() is None


# --------------------------------------------- proved-gone vs unknowable (#525)
#
# The seam's `[]` is a positive claim: the backend listed the session and found
# no windows, OR established that the session is gone. Every OTHER non-zero exit
# is a listing that could not be taken, and answering `[]` there reports a live
# session as empty — a death the engine acts on, a kill the prune calls verified.
#
# The rows below are a TRANSCRIPT, not invented fixtures. Measured 2026-08-31
# against the real binaries, tmux 3.4 (WSL) and psmux 3.3.8 (66cf613):
#
#   tmux  list-windows -t =ghost        rc 1  "can't find session: ghost"
#   tmux  list-windows, no server       rc 1  "no server running on /tmp/tmux-1000/default"
#   psmux list-windows -t =ghost        rc 1  "psmux: no server running on session 'ghost'"
#   psmux, tampered .key, WINDOWS ALIVE rc 1  "psmux: Invalid session key"
#   psmux, wrong .port,   WINDOWS ALIVE rc 1  "psmux: connection timed out"
#
# The last two are why an exit code cannot make this call: identical rc, live
# windows. A rejected `-F` format is NOT on the list because neither binary
# fails on one — both exit 0 (tmux prints nothing, psmux echoes the literal), so
# there is no "rejected format" arm to discriminate.

_PROVED_GONE_STDERR = [
    "can't find session: ghost",
    "no server running on /tmp/tmux-1000/default",
    "psmux: no server running on session 'ghost'",
]
_UNPROVEN_STDERR = [
    "psmux: Invalid session key",
    "psmux: connection timed out",
    "psmux: no response from server (timed out)",
    "",  # a non-zero exit that said nothing at all proves nothing at all
]


def _failing_listing(monkeypatch, stderr: str) -> None:
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda argv, **_k: subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr),
    )


@pytest.mark.parametrize("stderr", _PROVED_GONE_STDERR)
def test_list_window_ids_answers_empty_when_the_session_is_proved_gone(monkeypatch, stderr):
    """A vanished session is an ANSWER, not a fault: `[]`, no exception.

    Raising here instead would be the same dishonest report from the other
    side — prune_ctl_windows would invent a phantom survivor that every
    subsequent cleanup re-reports and nothing ever clears."""
    _failing_listing(monkeypatch, stderr)
    assert TmuxMultiplexer().list_window_ids("s") == []
    assert TmuxMultiplexer().window_alive("s", "@1") is False


@pytest.mark.parametrize("stderr", _UNPROVEN_STDERR)
def test_list_window_ids_raises_when_a_nonzero_exit_proves_nothing(monkeypatch, stderr):
    """The bug (#525): a listing that failed while the windows are alive must not
    read as an empty session.

    Both live-window rows come from a psmux server that was still serving its
    windows — one refused the auth, one was unreachable — and folding either to
    `[]` tells the engine's liveness probe the window died and the prune that
    its kills are verified.

    Ablation: replace the `_session_proved_gone` guard with a bare `return []`
    and every row here fails, while the proved-gone sibling above still passes.
    """
    _failing_listing(monkeypatch, stderr)
    with pytest.raises(MultiplexerError):
        TmuxMultiplexer().list_window_ids("s")
    with pytest.raises(MultiplexerError):
        TmuxMultiplexer().window_alive("s", "@1")


def test_gone_stderr_match_is_case_insensitive_and_substring(monkeypatch):
    """The fragments are matched inside a longer line and without regard to case:
    every measured wording embeds them in a sentence (`psmux: no server running
    on session 'x'`), and a backend that capitalizes its first word must not
    thereby turn a vanished session into an unverifiable one."""
    _failing_listing(monkeypatch, "PSMUX: No Server Running on session 'ghost'\n")
    assert TmuxMultiplexer().list_window_ids("s") == []


def test_a_leaf_may_override_the_proved_gone_wordings(monkeypatch):
    """The rule is a class attribute so a tmux-family leaf whose multiplexer words
    absence differently can replace it without touching a method body — the same
    swap-a-class-attr contract as `_BINARY` / `_ENCODING`.

    psmux deliberately does NOT override (its measured wordings are covered by
    the base tuple); this locks the seam open for the leaf that isn't."""

    class Dialect(TmuxMultiplexer):
        # Mixed case ON THE FRAGMENT, deliberately: the case-folding is a promise
        # to the leaf, and folding only the captured text keeps the base's own
        # (lower-case) tuple working while silently breaking every override that
        # spelled its fragment the way its binary prints it — a vanished session
        # read as unverifiable, i.e. a phantom survivor forever.
        _SESSION_GONE_STDERR = ("Session Vanished",)

    _failing_listing(monkeypatch, "the session vanished, sorry")
    assert Dialect().list_window_ids("s") == []
    # ...and the base's own wordings stop counting once replaced
    _failing_listing(monkeypatch, "can't find session: ghost")
    with pytest.raises(MultiplexerError):
        Dialect().list_window_ids("s")


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n"])
def test_a_blank_gone_fragment_never_matches(monkeypatch, blank):
    """A blank fragment in the tuple must be dropped, not matched.

    `"" in err` is true for every error there is, and `" " in err` for very
    nearly as many — any message with a space in it, which is all of them. So
    one stray element (a trailing comma, a leaf building its tuple from config)
    folds EVERY failed listing back to `[]`: #525 restored in full, in the
    silent direction, from a slip no reviewer would see.

    The whitespace rows are the point — an emptiness check that only rejects
    `""` leaves the more likely slip live. Ablation: weaken `if f.strip()` to
    `if f` and the space and newline rows fail. The tab row passes either way,
    because no message these binaries emit contains one; it is here to pin the
    rule, not to drive the ablation. The stderr below keeps its trailing newline
    for the same reason the wordings are verbatim — that is how a real binary
    hands it over, and it is what makes the `"\\n"` row a live threat rather than
    a hypothetical one."""

    class Sloppy(TmuxMultiplexer):
        _SESSION_GONE_STDERR = ("no server running", blank)

    _failing_listing(monkeypatch, "psmux: Invalid session key\n")
    with pytest.raises(MultiplexerError):
        Sloppy().list_window_ids("s")
    # the real sibling still counts — the filter drops the element, not the rule
    _failing_listing(monkeypatch, "no server running on /tmp/tmux-1000/default")
    assert Sloppy().list_window_ids("s") == []


@pytest.mark.parametrize("stderr", _UNPROVEN_STDERR)
def test_metadata_listings_keep_their_sentinel_but_say_so(monkeypatch, capsys, stderr):
    """Same lens, opposite conclusion (#525). `list_windows` and `session_options`
    read metadata, not liveness, and their sentinel degrades toward doing
    NOTHING — an empty candidate list prunes nothing, an unread tag reads as
    untagged and is left alone — so the documented `[]` / `{}` stays.

    What was missing is the signal: a server erroring on every call made the
    tool behave as if the sessions it manages had stopped existing, silently."""
    _failing_listing(monkeypatch, stderr)
    mux = TmuxMultiplexer()
    assert mux.list_windows("s", ["window_id"]) == []
    assert mux.session_options("@opt") == {}
    err = capsys.readouterr().err
    assert err.count("without proving the session gone") == 2
    # A non-zero exit that said NOTHING proves nothing either, and the warning has
    # to survive having no detail to quote — the row this parametrization used to
    # drop, under which a silent-on-blank-stderr regression passed.
    assert (stderr.strip() or "(no stderr)") in err


def test_metadata_listings_warn_when_the_transport_itself_failed(boom_run, capsys):
    """A timeout / a spawn that died proves no more about the session than an
    unrecognized non-zero exit, and the sentinel is identical — so the signal has
    to be too, or the seam's "say when the failure proved nothing" is only half
    true and the quietest failures are the ones that stay quiet.

    The `shutil.which` pre-gate is the deliberate exception and is covered by
    test_seam_methods_never_leak_raw_subprocess_error's no-binary case: a box
    with no multiplexer has no sessions to report on."""
    mux = TmuxMultiplexer()
    assert mux.list_windows("s", ["window_id"]) == []
    assert mux.session_options("@opt") == {}
    err = capsys.readouterr().err
    assert err.count("without proving the session gone") == 2
    assert type(boom_run).__name__ in err


def test_metadata_listings_contain_a_decode_fault(monkeypatch, capsys):
    """A strict-codec leaf must get the documented sentinel here too, not a raw
    UnicodeDecodeError out of a method the seam calls best-effort.

    `list_window_ids` has named this arm since #380 — a leaf overriding `_ERRORS`
    back to a strict handler raises a ValueError-family decode error that neither
    `SubprocessError` nor `OSError` covers. The metadata siblings promise `[]` /
    `{}` on a transport failure and a decode fault is one, so the same arm belongs
    here; without it the promise holds for every failure except this one."""
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: "/usr/bin/tmux")

    def boom(*_a, **_k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(tmux_base.subprocess, "run", boom)
    mux = TmuxMultiplexer()
    assert mux.list_windows("s", ["window_id"]) == []
    assert mux.session_options("@opt") == {}
    assert capsys.readouterr().err.count("without proving the session gone") == 2


def test_metadata_listings_are_silent_without_a_binary(monkeypatch, capsys):
    """No multiplexer installed is an ANSWER, not a fault: both metadata listings
    answer their sentinel and say nothing.

    Uniformity is the assertion. A sibling that warned here would fire on every
    call on such a box while the other stayed quiet, and a diagnostic that fires
    for one method and not its twin teaches the reader to ignore it.

    Silent, NOT spawnless, and the difference is the whole point. The silence is
    gated inside `_warn_unproven_listing`, after `_run` has been asked; gating it
    by short-circuiting ahead of `_run` would decide the RETURN VALUE from the
    ambient PATH, which makes the seam unreachable through its own spawn
    primitive — the injected transport below would never be consulted, and the
    resulting behavior would differ by platform rather than by contract. That
    was a real regression: it left `list_windows` answering `[]` for every
    psmux test on Linux, where the binary does not exist, while staying green on
    Windows, where it does.

    Ablation: drop the `shutil.which` guard in `_warn_unproven_listing` and this
    fails on the two warnings it must not print."""
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda argv, **_k: (_ for _ in ()).throw(FileNotFoundError(argv[0])),
    )
    mux = TmuxMultiplexer()
    assert mux.list_windows("s", ["window_id"]) == []
    assert mux.session_options("@opt") == {}
    assert capsys.readouterr().err == ""


def test_list_windows_answer_comes_from_run_not_from_path(monkeypatch):
    """`list_windows` must consult `_run` even when the binary is absent from PATH.

    The seam documents `_run` as the ONE place a spawn happens and the source of
    every answer. A `shutil.which` short-circuit ahead of it silently substitutes
    the ambient PATH for the transport, so a caller (or a test) that injects a
    transport is never asked — which is exactly how the same suite passed on
    Windows and failed 15 ways on Linux.

    Ablation: reinstate an early `if not shutil.which(...): return []` in
    `list_windows` and this fails on the injected rows never arriving."""
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda argv, **_k: subprocess.CompletedProcess(argv, 0, stdout="@1\tshell\n", stderr=""),
    )
    assert TmuxMultiplexer().list_windows("s", ["window_id", "window_name"]) == [("@1", "shell")]


def test_metadata_listings_stay_silent_for_a_gone_session(monkeypatch, capsys):
    """A vanished session is an answer, not a fault — warning about it would put
    noise on the ordinary path (`cleanup` after the last session ends)."""
    _failing_listing(monkeypatch, "no server running on /tmp/tmux-1000/default")
    mux = TmuxMultiplexer()
    assert mux.list_windows("s", ["window_id"]) == []
    assert mux.session_options("@opt") == {}
    assert capsys.readouterr().err == ""


def test_list_window_ids_decode_fault_raises_the_seam_type(monkeypatch):
    """A byte the codec cannot decode is a transport failure like a timeout: the
    liveness probe must answer MultiplexerError ("unknowable"), not leak the raw
    UnicodeDecodeError — prune_ctl_windows' post-kill verdict and the engine's
    window_alive catch only the seam type (#435).

    Since #380 `_run` decodes with backslashreplace on every platform, nothing
    reaches this arm from a stock capture; the fault is injected here instead.
    The arm still guards a leaf that overrides `_ERRORS` back to a strict handler
    — which is why the fault below is monkeypatched in rather than produced by a
    real child, and why this test stays green with or without that fix.
    """
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: "/usr/bin/tmux")

    def boom(*_a, **_k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(tmux_base.subprocess, "run", boom)
    mux = TmuxMultiplexer()
    with pytest.raises(MultiplexerError) as excinfo:
        mux.list_window_ids("s")
    assert not isinstance(excinfo.value, UnicodeError)


@needs_strict_codec
def test_run_decodes_an_undecodable_byte_instead_of_raising():
    """A capture carrying a byte the codec cannot decode comes back with that byte
    rendered as a visible \\xNN escape; `_run` does not raise (#380).

    POSIX left `_ERRORS` at None — the STRICT handler — so a stray byte in any
    tmux capture raised out of the one spawn primitive, and the fourteen guards
    that catch only (SubprocessError, OSError) let it through. Fixing it here, at
    the source, rather than at those catch sites is deliberate: most of them
    return a sentinel ([], None, {}), so catching a decode fault there would turn
    a crash into a WRONG ANSWER (see the seam-honesty note on list_window_ids).

    Drives a REAL child, never a monkeypatched `subprocess.run`. Only a real spawn
    executes the stdlib decode this fix changes, so a faked seam would pass
    identically with `_ERRORS` back at None — the fake-green #378 was bitten by.
    `sys.executable`, never a bare `python`: the suite runs under uv, where no
    `python` need be on PATH (see tests/test_verify.py's note on this).

    Ablation: set BaseTmuxBackend._ERRORS back to None and this fails alone, on
    the UnicodeDecodeError escaping `_run`.
    """

    class PyBackend(TmuxMultiplexer):
        # the "tmux binary" is this interpreter, so _run spawns something whose
        # bytes we choose; everything else about the primitive is untouched.
        _BINARY = sys.executable

    proc = PyBackend()._run(["-c", 'import sys; sys.stdout.buffer.write(b"ok-\\xff-tail")'])
    assert proc.stdout == "ok-\\xff-tail"


def test_seam_honesty_holds_for_psmux_style_run_override(monkeypatch):
    """The guarantee lives ABOVE _run, so a backend (like the eventual psmux) that
    overrides only _run and lets a raw TimeoutExpired escape it still gets seam
    honesty from the inherited contract methods."""
    monkeypatch.setattr(tmux_base.shutil, "which", lambda _name: "/usr/bin/tmux")

    class PsmuxStyle(TmuxMultiplexer):
        def _run(self, argv, *, check=True, env=None):
            raise subprocess.TimeoutExpired(["tmux", *argv], 30)

    mux = PsmuxStyle()
    with pytest.raises(MultiplexerError):
        mux.list_window_ids("s")
    with pytest.raises(MultiplexerError):
        mux.window_alive("s", "@1")
    # sentinel methods still degrade rather than leak the raw timeout
    assert mux.list_windows("s", ["window_id"]) == []


# ------------------------------------- window_pane_pids capability (#157)
#
# Like version(), window_pane_pids is a NON-abstract capability method: an
# out-of-tree backend implementing only the abstract set (herdr) keeps working
# with zero edits and inherits the "capability not offered" sentinel [].


def test_window_pane_pids_default_is_capability_not_offered():
    # StubMux implements only the abstract contract — it instantiates without
    # window_pane_pids and inherits the degrade sentinel from the seam base.
    assert StubMux().window_pane_pids("@1") == []


def test_tmux_window_pane_pids_parses_pane_pid_lines(monkeypatch):
    mux = TmuxMultiplexer()
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="1234\n5678\n", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake_run)
    assert mux.window_pane_pids("@7") == [1234, 5678]
    assert seen["argv"] == ["tmux", "list-panes", "-t", "@7", "-F", "#{pane_pid}"]


def test_tmux_list_windows_keeps_tabs_inside_the_trailing_field(monkeypatch):
    """The last requested field may legally contain the delimiter.

    PROJECT_OPTION carries a resolved filesystem path, and a tab is a legal
    POSIX filename byte — so an unbounded split turns one row into four parts
    and the field slice then drops the tail, handing the caller a *truncated*
    path. That does not read as "no tag", it reads as another project's tag, so
    the comparison sites discard the project's own windows. The bounded split
    keeps the remainder in the field it belongs to."""
    mux = TmuxMultiplexer()
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(
            argv, 0, stdout="@7\tresume-RID\t/home/u/my\tproj\n", stderr=""
        ),
    )
    rows = mux.list_windows("ctl", ["window_id", "window_name", "@bmad_project"])
    assert rows == [("@7", "resume-RID", "/home/u/my\tproj")]

    # Unchanged where it always held: short rows still pad trailing fields, and
    # a single-field request still takes the whole line.
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout="@7\trun-RID\n", stderr=""),
    )
    assert mux.list_windows("ctl", ["window_id", "window_name", "@bmad_project"]) == [
        ("@7", "run-RID", "")
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        lambda argv: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 30)),
        lambda argv: subprocess.CompletedProcess(argv, 1, stdout="", stderr="no window"),
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout="not-a-pid\n", stderr=""),
    ],
    ids=["timeout", "dead-window", "garbage-output"],
)
def test_tmux_window_pane_pids_degrades_to_empty(monkeypatch, outcome):
    mux = TmuxMultiplexer()
    monkeypatch.setattr(tmux_base.subprocess, "run", lambda argv, **k: outcome(argv))
    assert mux.window_pane_pids("@7") == []


def _kill_fake(monkeypatch, *, kill_rc: int, kill_err: str = "", live: str = "", probe_rc: int = 0):
    """Script kill-window's exit and the survivor probe's, recording the argv.

    ``live`` feeds the session-listing probe (qualified targets); ``probe_rc``
    feeds both it and the list-panes probe (unqualified targets).
    """
    calls: list[list[str]] = []

    def fake(argv, **k):
        calls.append(argv)
        if argv[1] == "list-windows":
            return subprocess.CompletedProcess(argv, probe_rc, stdout=live, stderr="")
        if argv[1] == "list-panes":
            return subprocess.CompletedProcess(argv, probe_rc, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, kill_rc, stdout="", stderr=kill_err)

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    return calls


def test_kill_window_warns_only_when_the_window_outlived_the_kill(monkeypatch, capsys):
    # The detectable leak class: the kill failed while the window it names is
    # still in its session's listing. The listing resolves independently of the
    # failed target (kill and list-windows are separate verbs against separate
    # targets), so this state is reachable — a same-target replay was not: a
    # target the kill could not resolve fails the replay too. The verdict is
    # unchanged — still None, still never raises — only now the target and the
    # binary's own stderr reach the operator.
    calls = _kill_fake(
        monkeypatch, kill_rc=1, kill_err="server temporarily unavailable\n", live="@1\n@3\n"
    )
    mux = TmuxMultiplexer()
    assert mux.kill_window("ctl:@3") is None
    err = capsys.readouterr().err
    assert "kill-window ctl:@3" in err
    assert "still alive" in err
    assert "server temporarily unavailable" in err  # verbatim: the reader judges it
    # The probe reads the session's own window list, not the failed target.
    assert calls[1][1:] == ["list-windows", "-t", "=ctl", "-F", "#{window_id}"]


def test_kill_window_is_silent_when_the_window_is_already_gone(monkeypatch, capsys):
    # The dominant case, not an edge one: CodingCLIAdapter.run kills in a
    # `finally` on every session, and a session that completed by window death
    # has nothing left to kill. A warning here would fire on ordinary teardown.
    # Covers the never-existed target too: not listed means nothing leaked.
    # Ablation: drop the `not self._window_survived_kill(target)` half of
    # kill_window's gate and this fails — every non-zero kill would warn.
    _kill_fake(monkeypatch, kill_rc=1, kill_err="can't find window: @7\n", live="@1\n")
    assert TmuxMultiplexer().kill_window("ctl:@7") is None
    assert capsys.readouterr().err == ""


def test_kill_window_is_silent_when_the_session_died_with_the_window(monkeypatch, capsys):
    # A session that ended when its last window died fails the listing probe —
    # ambiguous, so no warning: nothing provably survived.
    # Ablation: same mutation as the already-gone test above; this fails too.
    _kill_fake(monkeypatch, kill_rc=1, kill_err="can't find session: ctl\n", probe_rc=1)
    assert TmuxMultiplexer().kill_window("ctl:@7") is None
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("target", ["ctl:@7", "@7"])
def test_kill_window_is_silent_when_the_survivor_probe_cannot_answer(monkeypatch, capsys, target):
    # Unreadable is not "alive": the warning is a diagnostic, so a probe that
    # could not answer must not manufacture one — that would put the noise back
    # on exactly the path the probe exists to clear. Both probe shapes: the
    # session listing (qualified target) and list-panes (bare target).
    # Ablation: drop the `not self._window_survived_kill(target)` half of
    # kill_window's gate and both params fail on an unexpected warning.
    def fake(argv, **k):
        if argv[1] in ("list-windows", "list-panes"):
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom\n")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    assert TmuxMultiplexer().kill_window(target) is None  # must not raise
    assert capsys.readouterr().err == ""


def test_kill_window_unqualified_target_keeps_the_same_resolution_probe(monkeypatch, capsys):
    # A bare `@N` (tmux native_id shape) carries no session to list, so the
    # probe replays the target through list-panes: blind to a wrong-target
    # leak, but a kill that failed while the target resolves still warns.
    calls = _kill_fake(monkeypatch, kill_rc=1, kill_err="boom\n", probe_rc=0)
    assert TmuxMultiplexer().kill_window("@7") is None
    err = capsys.readouterr().err
    assert "kill-window @7" in err and "still alive" in err
    assert calls[1][1:] == ["list-panes", "-t", "@7"]


def test_kill_window_warning_omits_a_bare_colon_on_empty_stderr(monkeypatch, capsys):
    # A non-zero exit with nothing on stderr is plausible; "exited 1: " reads as
    # a truncated message rather than a complete one.
    _kill_fake(monkeypatch, kill_rc=1, kill_err="")
    TmuxMultiplexer().kill_window("@7")
    err = capsys.readouterr().err.strip()
    assert err.endswith("still alive")
    assert ": " not in err.removeprefix("warning: ")


def test_kill_window_is_silent_when_the_kill_lands(monkeypatch, capsys):
    calls = _kill_fake(monkeypatch, kill_rc=0)
    assert TmuxMultiplexer().kill_window("@7") is None
    assert capsys.readouterr().err == ""
    assert len(calls) == 1  # a landed kill never pays for the survivor probe


# -------------------------------- version() is one bounded line, always (#321)
#
# Consumers render version() inline (the `mux` table, validate's preflight
# finding, the diagnostic dump), so the seam owes them a single line — and a
# bounded one, since the table sizes its columns off the widest cell. psmux's
# `-V` prints two lines — a `tmux X.Y.Z` compat line plus its own — and the base
# folds them here so no consumer has to.


def _version_stdout(monkeypatch, stdout: str):
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=""),
    )


def test_version_folds_a_multi_line_probe_onto_one_line(monkeypatch):
    _version_stdout(monkeypatch, "tmux 3.3.7\npsmux 3.3.7 (05cc5d4 2026-07-20)\n")

    got = TmuxMultiplexer().version()

    assert got is not None and "\n" not in got
    # Folded, not truncated: dropping the tail would hide which binary runs.
    assert got == "tmux 3.3.7; psmux 3.3.7 (05cc5d4 2026-07-20)"
    # Order is load-bearing — PsmuxMultiplexer.available() anchors its version
    # match at position 0, so the compat line must stay first.
    assert got.startswith("tmux 3.3.7")


def test_version_of_a_single_line_probe_is_unchanged(monkeypatch):
    # The POSIX path must stay byte-identical: no separator, no reformatting.
    _version_stdout(monkeypatch, "tmux 3.4\n")

    assert TmuxMultiplexer().version() == "tmux 3.4"


def test_version_drops_blank_segments_and_strips_the_rest(monkeypatch):
    # A blank line would otherwise fold into a bare "; ; " run, and an indented
    # continuation line into "tmux 3.3.7;   psmux ...".
    _version_stdout(monkeypatch, "tmux 3.3.7\n\n   \n  psmux 3.3.7\n")

    assert TmuxMultiplexer().version() == "tmux 3.3.7; psmux 3.3.7"


def test_version_of_an_all_blank_probe_is_the_none_sentinel(monkeypatch):
    # A probe that exits 0 printing nothing reports "no version" — the sentinel
    # this method documents — not a version that happens to be empty.
    _version_stdout(monkeypatch, "\n  \n")

    assert TmuxMultiplexer().version() is None


def test_version_is_bounded_not_just_flattened(monkeypatch):
    # `mux` sizes every column off the widest cell, so an unbounded single line
    # breaks the table exactly as the embedded newline did — length is half the
    # seam's promise. The cut is at the tail, which the psmux gate's anchored
    # parse never reads.
    _version_stdout(monkeypatch, "tmux 3.4 " + "x" * 300)

    got = TmuxMultiplexer().version()

    assert got is not None
    assert len(got) == multiplexer.VERSION_MAX_CHARS
    assert got.startswith("tmux 3.4 ") and got.endswith("…")


def test_version_error_records_the_probe_crash_version_swallows(monkeypatch):
    """A binary on PATH that dies answering `-V` is the case version()'s None
    cannot express (#428). None stays the seam's answer; the diagnostic keeps
    the identity of the failure — including the probe's stderr, which _run
    already folds into the error text."""
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, stdout="", stderr="Access denied"),
    )
    mux = TmuxMultiplexer()

    assert mux.version() is None
    error = mux.version_error()
    assert error is not None and "Access denied" in error


def test_version_error_records_undecodable_probe_output(monkeypatch):
    """Pins the `UnicodeError` arm of version()'s catch. A strictly-decoding _run
    raises UnicodeDecodeError out of subprocess itself on a binary emitting an
    undecodable byte — a corrupt install, the very case #428 is about — and it is
    a ValueError, outside the SubprocessError/OSError family, so without the arm
    it escapes as a raw crash that every guard above turns back into an
    unexplained None. Since #380 `_run` is no longer strict on any platform
    (_ERRORS is backslashreplace), the arm now covers a leaf that overrides it
    back to a strict handler. The raise is injected rather than decoded for real:
    this owns the except clause, not the decoding (tmux_base documents that
    half) — which is also why it is unaffected by that default."""

    def boom(argv, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(tmux_base.subprocess, "run", boom)
    mux = TmuxMultiplexer()

    assert mux.version() is None
    error = mux.version_error()
    assert error is not None and "invalid start byte" in error


def test_version_error_is_none_when_the_binary_is_simply_absent(monkeypatch):
    """Nothing was asked, so there is no failure to report — a missing binary is
    already legible from AVAILABLE and must not also raise a warning."""
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: None)
    mux = TmuxMultiplexer()

    assert mux.version() is None
    assert mux.version_error() is None


def test_version_error_never_outlives_the_probe_it_describes(monkeypatch):
    """It describes the LAST call. A recovered probe that left the old failure
    standing would have `mux` warning about a backend that just answered fine."""
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"/usr/bin/{name}")
    mux = TmuxMultiplexer()
    monkeypatch.setattr(
        tmux_base.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, stdout="", stderr="transient"),
    )
    assert mux.version() is None and mux.version_error() is not None

    _version_stdout(monkeypatch, "tmux 3.4\n")

    assert mux.version() == "tmux 3.4"
    assert mux.version_error() is None


def test_version_error_of_a_backend_that_keeps_no_record_is_none():
    """The seam default: an out-of-tree backend inherits silence rather than an
    AttributeError, so the accessor is safe to call on anything registered — and
    non-abstract, so adding it does not break a backend that never heard of it.
    Called unbound because the body reads no state (an ABC subclass cannot be
    instantiated to hold any)."""
    assert "version_error" not in multiplexer.TerminalMultiplexer.__abstractmethods__
    assert multiplexer.TerminalMultiplexer.version_error(object()) is None  # type: ignore[arg-type]


def test_version_of_a_real_probe_is_never_truncated(monkeypatch):
    # The bound must clear the probes that actually exist by a wide margin —
    # otherwise it trades one unreadable cell for a useless one.
    _version_stdout(monkeypatch, "tmux 3.3.7\npsmux 3.3.7 (05cc5d4 2026-07-20)\n")

    got = TmuxMultiplexer().version()

    assert got == "tmux 3.3.7; psmux 3.3.7 (05cc5d4 2026-07-20)"
    assert len(got) < multiplexer.VERSION_MAX_CHARS


# ---------------------------------------------- _run seam: encoding + env (#40)
#
# The spawn primitive carries the two knobs its docstring promises — output
# decoding (class attr _ENCODING) and a per-call env — both defaulting to today's
# POSIX behavior, so a native-Windows leaf overrides zero lines of spawn plumbing.


class _RecordRun:
    """Stand-in for subprocess.run that records the argv and kwargs of the one spawn."""

    def __init__(self):
        self.argv: list = []
        self.kwargs: dict = {}

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_run_posix_default_passes_no_encoding_and_no_env(monkeypatch):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    TmuxMultiplexer()._run(["list-windows"])

    # The locale-default codec (encoding=None ≡ bare text=True) and the inherited
    # parent env (env=None) are both unchanged; only strictness went away. errors is
    # backslashreplace on every platform since #380, so an undecodable byte degrades
    # to a visible \xNN escape instead of raising out of the one spawn primitive.
    # This pins the KWARG; the decode it actually produces is pinned against a real
    # child by test_run_decodes_an_undecodable_byte_instead_of_raising.
    assert rec.kwargs["text"] is True
    assert rec.kwargs["encoding"] is None
    assert rec.kwargs["errors"] == "backslashreplace"
    assert rec.kwargs["env"] is None


def test_run_subclass_encoding_reaches_subprocess(monkeypatch):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    class Utf8Backend(TmuxMultiplexer):
        _ENCODING = "utf-8"  # a Windows leaf forces UTF-8 without touching _run

    Utf8Backend()._run(["list-windows"])
    assert rec.kwargs["encoding"] == "utf-8"


def test_run_custom_env_is_forwarded_without_leaking(monkeypatch):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,1234,0")  # a nesting-guard var to scrub
    before = dict(os.environ)

    # per the _run docstring: copy the parent env and REMOVE the offending var —
    # never rebuild from scratch (Windows children need SystemRoot etc.)
    scrubbed = dict(os.environ)
    del scrubbed["TMUX"]
    TmuxMultiplexer()._run(["new-session"], env=scrubbed)

    assert rec.kwargs["env"] == scrubbed
    assert "TMUX" not in rec.kwargs["env"]
    # the scrubbed env is confined to the child spawn — this process's env is untouched
    assert dict(os.environ) == before


# ------------------------------------------------------- shell-dialect seam
#
# new_window / new_parked_window keep the tmux argv construction and the
# parked-window protocol in the base; only shell-dialect fragments route
# through overridable hooks. Locked two ways: the POSIX output stays
# byte-identical to the pre-seam inline code, and a leaf that overrides only
# the hooks still gets the base's scaffolding without touching a method body.

# the exact sh source the POSIX backend produced before the hooks existed
_PARKED_SH_SOURCE = (
    'echo hi; ec=$?; echo "[bmad-loop exited $ec — press enter]"; read -r; '
    "ret=$(tmux show-options -wqv %3 2>/dev/null); "
    'if [ "$ret" = "detach" ]; then tmux detach-client 2>/dev/null; '
    'elif [ -n "$ret" ]; then '
    'tmux switch-client -t "$ret" 2>/dev/null || tmux switch-client -l 2>/dev/null; fi'
)


def test_new_parked_window_posix_argv_byte_identical(monkeypatch, tmp_path):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    TmuxMultiplexer().new_parked_window("s", "n", tmp_path, ["echo", "hi"], "%3")

    assert rec.argv == [
        "tmux",
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}",
        "-t",
        "=s:",
        "-n",
        "n",
        "-c",
        str(tmp_path),
        "sh",
        "-c",
        _PARKED_SH_SOURCE,
    ]


def test_new_session_argv_byte_identical(monkeypatch, tmp_path):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    TmuxMultiplexer().new_session("s", tmp_path)

    assert rec.argv == ["tmux", "new-session", "-d", "-s", "s", "-c", str(tmp_path)]


def test_new_window_posix_argv_byte_identical(monkeypatch, tmp_path):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    TmuxMultiplexer().new_window("s", "n", tmp_path, {"A": "1", "B": "2"}, "cmd")

    assert rec.argv == [
        "tmux",
        "new-window",
        "-t",
        "=s:",
        "-n",
        "n",
        "-c",
        str(tmp_path),
        "-P",
        "-F",
        "#{window_id}",
        "-e",
        "A=1",
        "-e",
        "B=2",
        "cmd",
    ]


def test_new_window_posix_command_reaches_tmux_verbatim(monkeypatch, tmp_path):
    # The contract says `command` is a shlex-joined argv, not a shell line.
    # The POSIX leaf must not parse or re-quote it: whatever the caller built
    # arrives at tmux as one verbatim trailing argument, so operator-looking
    # tokens the caller quoted (here a literal "&&" argument) survive intact.
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    command = shlex.join(["echo", "a b", "&&", "reboot"])
    TmuxMultiplexer().new_window("s", "n", tmp_path, {}, command)

    assert rec.argv == [
        "tmux",
        "new-window",
        "-t",
        "=s:",
        "-n",
        "n",
        "-c",
        str(tmp_path),
        "-P",
        "-F",
        "#{window_id}",
        command,
    ]


class _FakeDialect(TmuxMultiplexer):
    """A leaf that overrides ONLY the dialect hooks — no contract method bodies."""

    _EXIT_CAPTURE = "ec := EXITSTATUS"
    _ECHO = "say"
    _PARK = "pause"

    def _join_argv(self, argv):
        return "run " + " ".join(f"<{a}>" for a in argv)

    def _source_prefix(self):
        return "PRELUDE; "

    def _shell_wrap(self, source):
        return ["fakesh", "-enc", source]

    def _parked_trailer(self, return_opt):
        return f"TRAILER({return_opt})"

    def _window_launch(self, env, command):
        return [f"wrapped:{command}"]


def test_dialect_leaf_parked_window_composes_from_hooks(monkeypatch, tmp_path):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    _FakeDialect().new_parked_window("s", "n", tmp_path, ["echo", "hi"], "%3")

    # the tmux scaffolding is the base's, unchanged
    assert rec.argv[:12] == [
        "tmux",
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}",
        "-t",
        "=s:",
        "-n",
        "n",
        "-c",
        str(tmp_path),
    ]
    # the shell source is composed prefix + inner + capture + banner + park + trailer
    assert rec.argv[12:] == [
        "fakesh",
        "-enc",
        "PRELUDE; run <echo> <hi>; ec := EXITSTATUS; "
        'say "[bmad-loop exited $ec — press enter]"; '
        "pause; TRAILER(%3)",
    ]


def test_dialect_leaf_new_window_routes_launch_through_hook(monkeypatch, tmp_path):
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)

    _FakeDialect().new_window("s", "n", tmp_path, {"A": "1"}, "cmd")

    assert rec.argv == [
        "tmux",
        "new-window",
        "-t",
        "=s:",
        "-n",
        "n",
        "-c",
        str(tmp_path),
        "-P",
        "-F",
        "#{window_id}",
        "wrapped:cmd",
    ]
    assert "-e" not in rec.argv  # env strategy fully delegated to the hook


# ------------------------------------------------------------ target contract
#
# target() is the seam-canonical encoder core uses instead of hand-assembling
# "=session[:window]" strings; parse_target is the matching decoder a native-id
# backend reuses instead of re-deriving the grammar. Pure string work: no
# subprocess, no env sensitivity, safe on every CI leg. Both backends are
# constructed directly (their constructors are documented side-effect-free).


def test_target_default_grammar():
    mux = TmuxMultiplexer()
    assert mux.target("s") == "=s"
    assert mux.target("s", "w") == "=s:w"
    # falsy window collapses to the session-only form, mirroring parse_target's
    # "=s:" -> ("s", None) decode
    assert mux.target("s", None) == "=s"
    assert mux.target("s", "") == "=s"


@pytest.mark.parametrize(
    ("session", "window"),
    [("s", None), ("s", "w"), ("bmad-loop-ctl", "run-20260714-abc")],
)
def test_parse_target_round_trips_the_encoder(session, window):
    mux = TmuxMultiplexer()
    assert parse_target(mux.target(session, window)) == (session, window)


def test_parse_target_edges():
    # empty window part decodes like the session-only form
    assert parse_target("=s:") == ("s", None)
    # window is everything after the FIRST colon (minted names carry no colon,
    # but the split rule is pinned regardless)
    assert parse_target("=s:a:b") == ("s", "a:b")


@pytest.mark.parametrize("native", ["@1", "%3", "w1:p1"])
def test_parse_target_passes_native_ids_through(native):
    # non-"=" targets are backend-native ids: the decoder answers None and the
    # backend resolves them itself
    assert parse_target(native) is None
