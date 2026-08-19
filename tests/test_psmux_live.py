"""Live psmux gate: cross-project prune isolation and selected workaround premises.

The unit acceptance test (test_psmux_backend) proves the candidate scan with a
faked subprocess; what no unit layer can prove is the transport half — a real
psmux server holding two projects' windows on one shared control session, a
prune in one project, and the other project's window *and its option keys*
surviving the kill. Same zero-token contract as test_opencode_live: parked
windows run a plain `exit 0`, no coding CLI is ever launched.

The `test_premise_*` probes observe upstream psmux behavior *directly* — raw argv, no
backend verb under test — so the assumptions the workarounds in psmux_backend
are built on stop being a human's reading of a changelog. These probes invert
the usual failure semantics: a red probe is the intended signal, and it means
the workaround its message names has become droppable, not that the suite
broke. Each message says which one. The scaffolding assertions inside those
probes — session/window mint, focus setup, reads whose value is not itself the
premise — carry a ``probe setup: `` prefix instead, so an instrument failure is
never read as a premise flip.

Windows-local by construction (psmux registers for win32 only); skipped
everywhere else, and when psmux is absent or an unsupported version.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from bmad_loop import runs
from bmad_loop.adapters.psmux_backend import PsmuxMultiplexer, _pwsh_quote
from bmad_loop.adapters.tmux_base import TmuxError
from bmad_loop.tui import launch

HAVE_PSMUX = sys.platform == "win32" and shutil.which("psmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_PSMUX, reason="requires Windows with psmux on PATH")

PARKED_ARGV = ["pwsh", "-NoProfile", "-Command", "exit 0"]  # zero tokens, parks on read


def test_prune_kills_only_the_owning_projects_window(tmp_path: Path, monkeypatch):
    mux = PsmuxMultiplexer()
    if not mux.available():
        pytest.skip("psmux present but not an admitted version")
    session = f"bmad-loop-test-{uuid.uuid4().hex[:8]}"
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_a.mkdir()
    proj_b.mkdir()
    body_ok = False
    try:
        mux.new_session(session, tmp_path)
        win_a = mux.new_parked_window(session, "run-20260726-1", proj_a, PARKED_ARGV, "@r")
        win_b = mux.new_parked_window(session, "run-20260726-2", proj_b, PARKED_ARGV, "@r")
        # A degraded mint hands back "" or a bare id — fail loud here rather
        # than let rsplit or a mis-scoped option write misdiagnose the prune.
        assert re.fullmatch(r"[^:]+:@\d+", win_a), win_a
        assert re.fullmatch(r"[^:]+:@\d+", win_b), win_b
        mux.set_window_option(win_a, runs.PROJECT_OPTION, runs.project_tag(proj_a))
        mux.set_window_option(win_b, runs.PROJECT_OPTION, runs.project_tag(proj_b))
        # set_window_option declines silently (warn-only): prove the tags
        # landed before asserting anything about the prune's discrimination.
        assert mux.show_window_option(win_a, runs.PROJECT_OPTION) == runs.project_tag(proj_a)
        assert mux.show_window_option(win_b, runs.PROJECT_OPTION) == runs.project_tag(proj_b)
        # A hand-written user option carrying window A's digits in the OLD
        # (pre-marker) shape: the seam's sweeps must never claim it.
        digits_a = win_a.rsplit("@", 1)[1]
        foreign = f"@theme_@{digits_a}"
        proc = mux._run(["set-option", "-t", session, foreign, "user"], check=False)
        assert proc.returncode == 0, proc.stderr

        monkeypatch.setattr(launch, "CTL_SESSION", session)
        monkeypatch.setattr(launch, "get_multiplexer", lambda: mux)
        # Pin "outside any pane": when pytest itself runs inside psmux the
        # target-less probe can resolve the test's own window and exclude it.
        monkeypatch.setattr(mux, "current_window_id", lambda: None)
        monkeypatch.setattr(runs, "engine_alive", lambda _dir: False)

        # First with the kill suppressed: the candidate is provably still alive,
        # so it must land in `survived`. This is the half that pins the id-form
        # symmetry against a real server — a removal reads "gone" whether or not
        # the candidate's qualified `session:@N` matches the liveness listing's
        # form, but a SURVIVOR only reads "still there" when they do match, and
        # psmux qualifies both sides (#254/#291). No kill is sent, so nothing
        # here disturbs the verified-removal assertions below.
        with monkeypatch.context() as no_kill:
            no_kill.setattr(mux, "kill_window", lambda _t: None)
            assert launch.prune_ctl_windows(proj_a) == ([], ["run-20260726-1"], [])
        assert mux.window_alive(session, win_a)  # the suppressed kill really was a no-op

        # Then for real: the kill lands, so the verdict is a verified removal
        # with both other arms empty.
        assert launch.prune_ctl_windows(proj_a) == (["run-20260726-1"], [], [])

        live = mux.list_window_ids(session)
        assert win_a not in live
        assert win_b in live
        options = mux._scoped_options(session) or {}
        key_a = mux._scoped_option_key(runs.PROJECT_OPTION, digits_a)
        key_b = mux._scoped_option_key(runs.PROJECT_OPTION, win_b.rsplit("@", 1)[1])
        assert key_a not in options  # freed by the verified kill
        assert options.get(key_b) == runs.project_tag(proj_b)  # untouched
        assert options.get(foreign) == "user"  # foreign key survives every sweep
        body_ok = True
    finally:
        # kill_session is a best-effort backstop; verify it worked so a real
        # server never leaks silently off a green (or already-failing) run.
        mux.kill_session(session)
        try:
            leaked = mux.has_session(session)
        except TmuxError:
            leaked = True
        if leaked:
            note = f"live-gate session {session} survived teardown; kill it manually"
            # Only raise when the body passed. pytest.fail() here on an
            # already-failing run replaces the real diagnostic — the leak takes
            # over the summary line and the assertion drops to a chained
            # "during handling" frame — so that run keeps the warning instead.
            if body_ok:
                pytest.fail(note)
            print(f"warning: {note}", file=sys.stderr)


# --------------------------------------------------------------- premise probes


def _new_session_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not (
            key.upper().startswith(("CLAUDE_CODE_", "CLAUDECODE"))
            or key.upper() == "PSMUX_CLAUDE_TEAMMATE_MODE"
        )
    }
    env["PSMUX_ALLOW_NESTING"] = "1"
    return env


def _plain_has_session(
    mux: PsmuxMultiplexer, session: str, *, env: dict[str, str] | None = None
) -> bool:
    return mux._run(["has-session", "-t", session], check=False, env=env).returncode == 0


def _raw_new_session(mux: PsmuxMultiplexer, session: str, cwd: Path) -> None:
    created = mux._run(
        ["new-session", "-d", "-s", session, "-c", str(cwd)],
        check=False,
        env=_new_session_env(),
    )
    assert (
        created.returncode == 0
    ), f"probe setup: probe session creation failed: {created.stderr.strip()!r}"
    assert _plain_has_session(
        mux, session
    ), "probe setup: probe session was not observable by plain name"


def _mint_probe_window(mux: PsmuxMultiplexer, session: str, name: str, cwd: Path) -> str:
    before = set(mux.list_window_ids(session))
    mux.new_parked_window(session, name, cwd, PARKED_ARGV, "@r")
    deadline = time.monotonic() + 5
    created: set[str] = set()
    while time.monotonic() < deadline:
        created = set(mux.list_window_ids(session)) - before
        if len(created) == 1:
            break
        time.sleep(0.1)
    assert (
        len(created) == 1
    ), f"probe setup: probe window mint was not observable: {sorted(created)}"
    window_id = created.pop()
    # Same qualified-id guard the prune test applies to its mints: a degraded id
    # would otherwise surface later as a bare rsplit IndexError or a mis-scoped
    # option write, several frames away from the mint that produced it.
    assert re.fullmatch(
        r"[^:]+:@\d+", window_id
    ), f"probe setup: probe window mint returned an unqualified id: {window_id!r}"
    return window_id


def _active_window(mux: PsmuxMultiplexer, session: str) -> str:
    proc = mux._run(
        ["list-windows", "-t", session, "-F", "#{window_id} #{window_active}"], check=False
    )
    assert proc.returncode == 0, f"probe setup: active-window probe failed: {proc.stderr.strip()!r}"
    active = [line.split()[0] for line in proc.stdout.splitlines() if line.endswith(" 1")]
    assert len(active) == 1, f"probe setup: expected one active window, got {active!r}"
    return f"{session}:{active[0]}"


@pytest.fixture(scope="module")
def psmux_data_root(tmp_path_factory):
    """Return an isolated registry root only when the installed build honors it."""
    mux = PsmuxMultiplexer()
    if not mux.available():
        pytest.skip("psmux present but not an admitted version")
    root = tmp_path_factory.mktemp("psmux-data")
    session = f"bmad-loop-data-probe-{uuid.uuid4().hex[:8]}"
    env = _new_session_env()
    env["PSMUX_DATA_DIR"] = str(root)
    try:
        created = mux._run(
            ["new-session", "-d", "-s", session, "-c", str(root)], check=False, env=env
        )
        if created.returncode != 0:
            return None
        isolated = _plain_has_session(mux, session, env=env)
        default = _plain_has_session(mux, session)
        return root if isolated and not default else None
    finally:
        # The one session here that can land in the developer's real registry —
        # a build ignoring PSMUX_DATA_DIR is the very branch this fixture exists
        # to detect — so verify the kill in BOTH views rather than trusting a
        # best-effort call whose target may not be the registry it created in.
        # The kill sits INSIDE the try, unlike the `probe` fixture's: this one
        # needs `env=`, which only raw _run takes, and raw _run propagates a
        # timeout even under check=False. A kill that hung is exactly when the
        # session is most likely still standing, so it must reach the report
        # below rather than escape with a bare TimeoutExpired.
        try:
            mux._run(["kill-session", "-t", session], check=False, env=env)
            leaked = _plain_has_session(mux, session, env=env) or _plain_has_session(mux, session)
        except (OSError, TmuxError, subprocess.TimeoutExpired):
            leaked = True
        assert (
            not leaked
        ), f"probe setup: data-probe session {session} survived teardown; kill it manually"


@pytest.fixture
def probe(tmp_path, monkeypatch, psmux_data_root):
    """Yield a throwaway session with two parked windows."""
    mux = PsmuxMultiplexer()
    if not mux.available():
        pytest.skip("psmux present but not an admitted version")
    if psmux_data_root is not None:
        monkeypatch.setenv("PSMUX_DATA_DIR", str(psmux_data_root))
    session = f"bmad-loop-test-{uuid.uuid4().hex[:8]}"
    try:
        _raw_new_session(mux, session, tmp_path)
        windows = [_mint_probe_window(mux, session, f"probe-{n}", tmp_path) for n in (1, 2)]
        yield mux, session, windows
    finally:
        mux.kill_session(session)
        try:
            leaked = _plain_has_session(mux, session)
        except (OSError, TmuxError, subprocess.TimeoutExpired):
            # TimeoutExpired too: _plain_has_session goes through raw _run, which
            # propagates a timeout even under check=False, so a hung psmux would
            # otherwise escape this teardown instead of reporting the leak. The
            # kill_session above needs no such cover — it swallows
            # SubprocessError, and TimeoutExpired is one — which is why only the
            # data-root fixture's raw-_run kill had to move inside its try.
            leaked = True
        assert (
            not leaked
        ), f"probe setup: probe session {session} survived teardown; kill it manually"


def test_premise_version_leads_with_a_tmux_triple():
    # Deliberately NOT gated on available(): that method applies this very regex
    # and would skip the test on the failure it is here to catch, making the
    # probe unable to go red. Module-level HAVE_PSMUX is the only guard.
    # Raw `-V`, not version(): the seam collapses psmux's two-line banner to one
    # line, and asserting on that would pin our normalization, not upstream's
    # output — which is the one thing no probe here may do.
    mux = PsmuxMultiplexer()
    proc = mux._run(["-V"], check=False)
    assert (
        proc.returncode == 0
    ), "psmux -V now fails — available() in psmux_backend rejects the installed build"
    reported = proc.stdout.strip()
    assert re.match(r"tmux \d+\.\d+", reported), (
        f"psmux -V no longer leads with a tmux version triple ({reported!r}) — the "
        "available() version gate in psmux_backend parses exactly that prefix and "
        "now admits nothing"
    )


def test_premise_exact_match_prefix_resolves_for_has_session_but_not_kill(probe):
    mux, session, _ = probe
    found = mux._run(["has-session", "-t", f"={session}"], check=False)
    assert found.returncode == 0, (
        "psmux no longer resolves the `=name` exact-match form for has-session — "
        "the post-create verification belt in new_session cannot confirm anything"
    )
    # Costs psmux's own 5s settle timeout: the = form routes to the right server
    # but reaches the kill handler unstripped, so its name compare never matches
    # and the client waits out its deadline (psmux/psmux#558).
    mux._run(["kill-session", "-t", f"={session}"], check=False)
    assert _plain_has_session(mux, session), (
        "psmux now honors the `=name` form for kill-session (psmux/psmux#558) — "
        "kill_session's deliberate plain-name target is no longer the only form "
        "that works"
    )


def test_premise_window_scoped_option_write_lands_at_session_scope(probe):
    mux, session, windows = probe
    written = mux._run(["set-option", "-w", "-t", windows[0], "@probe", "v"], check=False)
    assert written.returncode == 0, "set-option -w no longer even accepts a window target"
    read_back = mux._run(["show-options", "-wqv", "-t", windows[0], "@probe"], check=False)
    # rc first: an empty stdout from a FAILED read would satisfy the emptiness
    # assertion below and record the premise as observed when it was not.
    assert read_back.returncode == 0, f"the -w read itself failed: {read_back.stderr.strip()!r}"
    assert read_back.stdout.strip() == "", (
        "psmux now has real per-window option storage — the @opt_@N scoped-option "
        "channel in psmux_backend is droppable"
    )
    # Not merely unstored: the -w write silently landed in the session's single
    # map. That misrouting is why the channel encodes the window id in the KEY.
    at_session = mux._run(["show-options", "-qv", "-t", session, "@probe"], check=False)
    assert at_session.returncode == 0, f"session-scope read failed: {at_session.stderr.strip()!r}"
    assert at_session.stdout.strip() == "v", (
        "the -w write no longer lands at session scope — the option channel's "
        "premise about where a window-scoped write goes has changed"
    )


def test_premise_client_verbs_exit_zero_with_no_client_to_move(probe):
    mux, session, windows = probe
    # The premise is "no client to move", and an inherited $TMUX says otherwise:
    # when pytest itself runs inside psmux there IS a client — the developer's.
    # At 3.3.7 the verb is inert, so nothing moves and the probe reads the same
    # either way (target routing is port-file based, and rc is dispatch-only 0);
    # on the effect-reporting build this probe exists to catch, the switch would
    # drag that client into the throwaway session and stay green through the very
    # premise flip. A local scrub, not _new_session_env: this call needs neither
    # the CLAUDE_CODE strip nor PSMUX_ALLOW_NESTING. PSMUX_DATA_DIR is kept, so
    # the raw verbs still address the registry the probe session lives in.
    clientless = {
        key: value for key, value in os.environ.items() if key not in ("TMUX", "TMUX_PANE")
    }
    attached = mux._run(
        ["display-message", "-p", "-t", session, "#{session_attached}"],
        check=False,
        env=clientless,
    )
    assert (
        attached.returncode == 0
    ), f"probe setup: attached-count probe failed: {attached.stderr.strip()!r}"
    assert (
        attached.stdout.strip() == "0"
    ), "probe setup: probe session unexpectedly has an attached client"
    # `switch-client -l` has no target form and could move a developer's client
    # when pytest itself runs inside psmux, so it is deliberately unobservable.
    proc = mux._run(["switch-client", "-t", windows[0]], check=False, env=clientless)
    assert proc.returncode == 0, (
        "psmux now reports effect rather than dispatch for switch-client — the "
        "#{session_attached} delta in _client_left can go back to trusting rc"
    )


def test_premise_option_values_corrupted_by_the_control_line(probe):
    mux, session, _ = probe

    def roundtrip(value: str) -> tuple[bool, str]:
        written = mux._run(["set-option", "-t", session, "@rt", value], check=False)
        if written.returncode != 0:
            return False, ""
        got = mux._run(["show-options", "-qv", "-t", session, "@rt"], check=False)
        assert (
            got.returncode == 0
        ), f"probe setup: show-options after {value!r} failed: {got.stderr.strip()!r}"
        unset = mux._run(["set-option", "-u", "-t", session, "@rt"], check=False)
        assert (
            unset.returncode == 0
        ), f"probe setup: unset after {value!r} failed: {unset.stderr.strip()!r}"
        return True, got.stdout.strip()

    # The banned canaries may be corrupted or refused by the raw CLI.
    corrupted = {
        "a ; b": "the one-shot chain splitter is now quote-aware (psmux/psmux#499)",
        "-lead": "a dash-leading value now survives the server tokenizer",
        "a'b": "a bare apostrophe is no longer read as a quote opener",
        "x\u00a0y": "non-ASCII whitespace no longer splits the value server-side",
    }
    for value, why in corrupted.items():
        accepted, got = roundtrip(value)
        assert not accepted or got != value, (
            f"{why} — the matching branch of _transportable in psmux_backend now "
            "refuses a value the wire carries intact"
        )
    # The permitted shapes: a refusal here would silently make windows unprunable.
    for value in ("\\\\srv\\share", "C:/Program Files/x"):
        accepted, got = roundtrip(value)
        assert accepted and got == value, (
            f"psmux no longer carries {value!r} verbatim — _transportable permits a "
            "shape that now corrupts, so a tag reads back different from the prune's"
        )


def test_premise_pipe_pane_strips_dash_flag_tokens(probe, tmp_path):
    mux, _, windows = probe
    sidecar = tmp_path / "argprobe.ps1"
    record = tmp_path / "argprobe.txt"
    # The piped command is a bare double-quoted interpolation, exactly as
    # pipe_pane composes it — and pipe_pane refuses `$` and a backtick rather
    # than ship a path pwsh would expand. Match that production refusal, and add
    # a space: psmux's quote re-parse splits a spaced path (a `C:\Users\John Doe`
    # profile is enough), the sidecar never spawns, and this probe would then red
    # permanently while its message blamed a transient spawn race. pipe_pane's
    # docstring already lists a spaced path as an untested ceiling — skipping is
    # honest about that; a red here would not be.
    if any(char in str(sidecar) for char in ("$", "`", " ")):
        pytest.skip(
            "tmp path carries PowerShell interpolation syntax, or a space psmux's "
            "quote re-parse would split before the sidecar spawns"
        )
    # Records the argv psmux actually handed it, then drains stdin so the child
    # outlives the first chunk rather than racing the pane. -Encoding pins the
    # bytes the reader below assumes rather than trusting a pwsh default.
    sidecar.write_text(
        f"Set-Content -Encoding utf8 -LiteralPath {_pwsh_quote(str(record))} "
        "-Value ($args -join '|')\n"
        "$in = [System.Console]::OpenStandardInput()\n"
        "$buf = New-Object byte[] 4096\n"
        "while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) { }\n",
        encoding="utf-8",
    )
    piped = f'pwsh "{sidecar}" -flagcanary positional'
    text = ""
    for window in windows:
        attached = mux._run(["pipe-pane", "-t", window, "-o", piped], check=False)
        assert attached.returncode == 0, (
            "probe setup: raw pipe-pane failed — psmux_backend.pipe_pane's sidecar "
            f"workaround premise went unobserved: {attached.stderr.strip()!r}"
        )
        for argv in (
            ["send-keys", "-t", window, "-l", "echo hello"],
            ["send-keys", "-t", window, "Enter"],
        ):
            sent = mux._run(argv, check=False)
            assert sent.returncode == 0, (
                f"probe setup: {argv[0]} could not trigger pipe-pane output: "
                f"{sent.stderr.strip()!r}"
            )
        deadline = time.monotonic() + 7.5
        while time.monotonic() < deadline:
            text = record.read_text(encoding="utf-8") if record.exists() else ""
            if text.endswith("\n"):
                break
            time.sleep(0.25)
        if text.endswith("\n"):
            break
    # An unobserved premise must fail, never pass: no file means the sidecar
    # never spawned, which says nothing about flag stripping either way.
    assert text.endswith("\n"), (
        "probe setup: pipe-pane sidecar never ran, so the dash-flag premise went "
        "unobserved — psmux's first-pipe-after-new-window spawn race "
        "(psmux/psmux#482) is a likelier cause than a premise change; re-run "
        "before reading anything into it"
    )
    recorded = text.strip().split("|")
    assert "positional" in recorded, f"sidecar recorded an unexpected argv: {recorded!r}"
    assert "-flagcanary" not in recorded, (
        "psmux now passes dash-flag tokens through pipe-pane — pipe_pane's "
        "positional sidecar .ps1 can go back to a flag transport (psmux/psmux#482)"
    )


def test_premise_unresolvable_kill_target_exits_zero_and_kills_a_live_window(probe):
    # Destructive by construction: on this premise the kill lands on the ACTIVE
    # window (psmux/psmux#545). The safety property is per-probe session
    # ownership — the `probe` fixture mints a fresh one per test, so nothing this
    # destroys is shared. File order enforces nothing here (xdist reorders); it
    # sits last for reader flow only.
    mux, session, _ = probe
    before = set(mux.list_window_ids(session))
    assert len(before) >= 2, f"probe setup: probe session should hold the parked windows: {before}"
    active = _active_window(mux, session)
    proc = mux._run(["kill-window", "-t", f"{session}:@9999"], check=False)
    after = set(mux.list_window_ids(session))
    assert proc.returncode == 0, (
        "psmux now reports a non-zero exit for an unresolvable kill-window target — "
        "kill_window's check=False swallow in tmux_base can surface the failure"
    )
    # Exactly one window and the session still standing. `after < before` is a
    # strict-subset test that ANY number of vanished windows satisfies — a
    # collateral kill that took the session down answers [] through
    # list_window_ids' non-zero leg and would pass while claiming something
    # entirely different happened.
    assert mux.has_session(
        session
    ), f"the unresolvable kill took the whole session down, not one window: {before}"
    assert after < before and before - after == {active}, (
        "an unresolvable kill-window target no longer destroys the active live "
        f"window (psmux/psmux#545): {sorted(before)} -> {sorted(after)}"
    )
