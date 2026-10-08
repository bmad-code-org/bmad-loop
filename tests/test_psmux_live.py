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

The `test_adopted_*` probes are the ordinary-direction complement: each
exercises a behavior the 3.3.8 floor let the backend assume (several through
the backend verb itself, deliberately), so a red probe there means psmux
regressed, not that a workaround became droppable.

Windows-local by construction (psmux registers for win32 only); skipped
everywhere else, and when psmux is absent or an unsupported version.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psmux_teardown
import pytest

from bmad_loop import runs
from bmad_loop.adapters import psmux_backend, tmux_base
from bmad_loop.adapters.psmux_backend import PsmuxMultiplexer
from bmad_loop.tui import launch

HAVE_PSMUX = sys.platform == "win32" and shutil.which("psmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_PSMUX, reason="requires Windows with psmux on PATH")

PARKED_ARGV = ["pwsh", "-NoProfile", "-Command", "exit 0"]  # zero tokens, parks on read

# The real resolver, captured before conftest pins it for every test.
_REAL_PWSH_PATH = PsmuxMultiplexer._pwsh_path


@pytest.fixture(autouse=True)
def _probe_the_real_pwsh(monkeypatch):
    """Drop conftest's pinned pwsh path and seeded PowerShell answer, so every
    window launch here resolves the installed pwsh and runs the backend's real
    version probe against it."""
    monkeypatch.setattr(PsmuxMultiplexer, "_pwsh_path", _REAL_PWSH_PATH)
    monkeypatch.setattr(psmux_backend, "_PWSH_VERSIONS", {})


def test_prune_kills_only_the_owning_projects_window(tmp_path: Path, monkeypatch, psmux_data_root):
    mux = PsmuxMultiplexer()
    if not mux.available():
        pytest.skip("psmux present but not an admitted version")
    # Same registry isolation the `probe` fixture gives every other test here.
    # Without it this test alone ran against the default registry while its
    # siblings spawned and killed servers in private ones — under `-n logical`
    # that contention made the mint below hand back "" and fail at the
    # degraded-mint assertion. Isolation is what makes the module xdist-safe,
    # not the uuid session names, which never collided.
    monkeypatch.setenv("PSMUX_DATA_DIR", str(psmux_data_root))
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

        # The ctl-session NAME is resolved per (project, transport) since the
        # per-registry rename (runs.ctl_session_for); pin the resolver to this
        # test's own session so the prune sweeps the windows minted above.
        monkeypatch.setattr(runs, "ctl_session_for", lambda _p, _m=None: session)
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
        # The registry-scoped teardown, not a name-scoped kill_session: every
        # non-warm psmux server spawns a replacement `__warm__` server AT
        # STARTUP (`server/mod.rs:1196` / `spawn_warm_server`, source-read at
        # v3.3.8), and that warm server inherits this test's PSMUX_DATA_DIR and
        # registers its own `.port` under the same private root — so a
        # `kill-session -t <name>` leaves it running forever (measured: this
        # test alone, from zero psmux processes, left one `psmux.exe server -s
        # __warm__` behind). `_teardown_probe_session`'s `kill-server` read_dirs
        # the root and reaps both. `_new_session_env()` carries the
        # monkeypatched PSMUX_DATA_DIR, so it addresses this test's registry.
        try:
            _teardown_probe_session(mux, session, _new_session_env())
        except AssertionError as exc:
            # Only raise when the body passed. Raising here on an
            # already-failing run replaces the real diagnostic — the leak takes
            # over the summary line and the assertion drops to a chained
            # "during handling" frame — so that run keeps the warning instead.
            if body_ok:
                raise
            print(f"warning: {exc}", file=sys.stderr)


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


_plain_has_session = psmux_teardown.plain_has_session


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
    """The session's focused window, in the qualified form the seam mints."""
    proc = mux._run(
        ["list-windows", "-t", session, "-F", "#{window_id} #{window_active}"], check=False
    )
    assert proc.returncode == 0, f"probe setup: active-window probe failed: {proc.stderr.strip()!r}"
    active = [line.split()[0] for line in proc.stdout.splitlines() if line.endswith(" 1")]
    assert len(active) == 1, f"probe setup: expected one active window, got {active!r}"
    return f"{session}:{active[0]}"


_CMDLINE_TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def _server_session_of(cmdline: str) -> str | None:
    """The session a psmux server process was started for, or None.

    `psmux` builds its server argv as `["server", "-s", <name>, ...]`
    (`src/main.rs:1421-1423`, source-read at v3.3.8), so the name is the whole
    token after the FIRST `-s` — never a substring of the command line, which is
    the distinction that matters when the killer below acts on the answer. The
    first is taken deliberately: a later `-c "<command>"` can hold anything,
    including another `-s`, and it arrives as one quoted token here.
    """
    tokens = [quoted if quoted else bare for quoted, bare in _CMDLINE_TOKEN.findall(cmdline)]
    for index, token in enumerate(tokens[:-1]):
        if token == "-s":
            return tokens[index + 1]
    return None


def _kill_unregistered_servers(session: str) -> list[str]:
    """Last resort: kill psmux server processes for ``session`` that no psmux verb
    can reach, and return the pids killed.

    Sustained absence is not death. A server whose registry root cannot be
    written **binds its port and runs anyway** — `ensure_session_registry_files`
    (`server/mod.rs:104-177`, source-read at v3.3.8) swallows every failure with
    `let _ = create_dir_all(...)` / `let _ = fs::write(...)` — so it publishes no
    `.port` file. `has-session` resolves through that file and `kill-server`
    enumerates the ones under the root (`main.rs:746`, source-read), which means
    both verbs report a running server as absent and neither can address it.
    Absence for any length of time is then indistinguishable from death, and the
    teardown below would return clean over a live process.

    Reachable in exactly the world the self-heal probe's control mint makes red:
    psmux no longer creating a missing root. That path is where the leak was
    observed — twice, killed by hand — so it is the path this closes.

    The process table is the only remaining witness, and it is read in two steps
    on purpose. **Selection is an exact match on the session token**
    (:func:`_server_session_of`), not a substring of the command line: a
    substring also selects `foreign-<session>-tail`, and the second step
    `Stop-Process -Force`s what the first chose. Measured — two servers so named,
    in separate private registries, were both selected by a substring predicate
    and only the right one by this. A backstop that force-kills has to be exactly
    scoped, or the operator's own session pays for a probe's naming.

    Filtering on `Name='psmux.exe'` keeps the pwsh doing the asking, whose own
    command line quotes the session name, out of the answer. Windows-only, like
    the whole module.
    """
    listing = _powershell(
        "Get-CimInstance Win32_Process -Filter \"Name='psmux.exe'\" | "
        'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
    )
    doomed: list[str] = []
    for line in listing.splitlines():
        pid, _, cmdline = line.partition("\t")
        if pid.strip().isdigit() and _server_session_of(cmdline) == session:
            doomed.append(pid.strip())
    if doomed:
        _powershell(
            "; ".join(
                f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue" for pid in doomed
            ),
            check=False,
        )
    return doomed


def _powershell(script: str, *, check: bool = True) -> str:
    """Run one PowerShell command and return its stdout; raise if it could not
    run, or (``check``) exited nonzero. An empty listing is the teardown's proof
    that no invisible server is left, so a failed probe must never read as one.
    The kill passes ``check=False``: `-Command` exits 1 when a pid is already
    gone, and the teardown re-confirms after every kill anyway."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            timeout=tmux_base.TMUX_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError(
            f"probe setup: process-table witness failed ({type(exc).__name__}: {exc})"
        ) from exc
    if check and proc.returncode != 0:
        raise AssertionError(
            f"probe setup: process-table witness exited {proc.returncode}: "
            f"{proc.stderr.strip()[:500]}"
        )
    return proc.stdout


def _teardown_probe_session(
    mux: PsmuxMultiplexer, session: str, env: dict[str, str], *, known_created: bool = False
) -> None:
    """Kill a probe session and its server; refuse to return until it is provably
    gone in both registries. The policy — why it is retried, registry-scoped and
    how long absence must hold — is `psmux_teardown.teardown_probe_session`'s
    docstring. ``known_created`` is the caller's positive post-mint observation
    and nothing weaker; the process-table witness is this module's own."""
    psmux_teardown.teardown_probe_session(
        mux,
        session,
        env,
        known_created=known_created,
        kill_unregistered=_kill_unregistered_servers,
    )


@pytest.fixture(scope="module")
def psmux_data_root(tmp_path_factory):
    """Return an isolated registry root, or fail loudly if one cannot be had.

    Isolation is a precondition here, not a nicety: without it every test in this
    module shares the developer's real registry, and under xdist that contention
    is what made the prune test's window mint hand back "". Returning a degraded
    ``None`` would restore that flake silently — and worst under load, where the
    probe below is itself most likely to fail — so both failure directions raise
    instead. On the 3.3.8 floor the ignored-variable branch is unreachable
    anyway; what stays live is the probe failing as an instrument.
    """
    mux = PsmuxMultiplexer()
    if not mux.available():
        pytest.skip("psmux present but not an admitted version")
    # Pre-created, and that is not incidental. An earlier revision handed out an
    # UNCREATED path so this fixture's own probe session would double as the proof
    # that psmux create_dir_all's a missing root. Measured cost of that: psmux has
    # to build the directory before the server can write its port file, and under
    # a full-suite `-n logical` load that was enough to push past the CLIENT's
    # readiness deadline — `psmux: failed to create session` while the server came
    # up anyway (`src/main.rs`, source-read at v3.3.8). One full suite: 13 fixture
    # errors with the uncreated root, 0 with this line. The self-heal claim is
    # measured on its own root instead, by
    # `test_adopted_a_missing_registry_root_self_heals`, so one test pays that cost
    # rather than every test in the module.
    root = tmp_path_factory.mktemp("psmux-data") / "registry"
    root.mkdir()
    session = f"bmad-loop-data-probe-{uuid.uuid4().hex[:8]}"
    env = _new_session_env()
    env["PSMUX_DATA_DIR"] = str(root)
    isolated = False
    try:
        created = mux._run(
            ["new-session", "-d", "-s", session, "-c", str(root)], check=False, env=env
        )
        if created.returncode != 0:
            pytest.fail(
                "probe setup: could not mint the isolation probe session: "
                f"{created.stderr.strip()!r}"
            )
        # These two reads are also the live half of the registry-namespace claim
        # the cleanup sweep rests on: the second passes no env, so it inherits a
        # process with PSMUX_DATA_DIR unset — byte-identical to what a
        # `PsmuxMultiplexer(default_registry=True)` instance spawns with (that
        # strip is unit-asserted in test_psmux_backend). A session in one
        # registry being invisible from the other is what makes the sweep's
        # second pass address anything at all.
        isolated = _plain_has_session(mux, session, env=env)
        default = _plain_has_session(mux, session)
        if not isolated or default:
            pytest.fail(
                "probe setup: the installed psmux ignored PSMUX_DATA_DIR "
                f"(visible in the isolated registry={isolated}, "
                f"visible in the default one={default})"
            )
        return root
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
        #
        # `isolated` is the positive read above; a failed mint never set it.
        _teardown_probe_session(mux, session, env, known_created=isolated)


@pytest.fixture
def probe(tmp_path, monkeypatch, psmux_data_root):
    """Yield a throwaway session with two parked windows."""
    mux = PsmuxMultiplexer()
    if not mux.available():
        pytest.skip("psmux present but not an admitted version")
    monkeypatch.setenv("PSMUX_DATA_DIR", str(psmux_data_root))
    session = f"bmad-loop-test-{uuid.uuid4().hex[:8]}"
    env = _new_session_env()
    env["PSMUX_DATA_DIR"] = str(psmux_data_root)
    # The SAME teardown the data-root fixture uses, and it has to be: a
    # single-shot kill plus one read is a clean-looking teardown over a real
    # leak whenever the server is mid-start, and every leaked server slows the
    # next mint and makes its own timeout likelier. This is the fixture that
    # runs fifteen times, so it is the one that compounds. See
    # `psmux_teardown.teardown_probe_session` for why absence has to hold, and
    # for how long.
    #
    # `minted_session` hands the teardown `known_created=True` only once
    # `_raw_new_session` has RETURNED, and that returns only after a plain
    # `has-session` answered — the positive observation the teardown may carry.
    # A test that kills the session itself
    # (`test_adopted_kill_session_honors_the_exact_match_target`) otherwise
    # reached teardown with nothing left to see and held the full never-seen
    # vigil; a mint that raised hands over False and keeps that vigil.
    #
    # `kill-server` is registry-wide, and this root is shared with the module
    # fixture — which is safe by construction: that fixture's own probe session
    # is torn down inside its setup, so this session is the only one in the
    # root while a test runs.
    #
    # The teardown's second read passes no env, but this fixture has
    # PSMUX_DATA_DIR monkeypatched into the process for the duration, so that
    # read lands in the isolated registry too rather than in the default one.
    # It is a duplicate here, not a default-registry check; the module fixture
    # is where that check has teeth.
    with psmux_teardown.minted_session(
        lambda: _raw_new_session(mux, session, tmp_path),
        lambda known_created: _teardown_probe_session(
            mux, session, env, known_created=known_created
        ),
    ):
        windows = [_mint_probe_window(mux, session, f"probe-{n}", tmp_path) for n in (1, 2)]
        yield mux, session, windows


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
    # On the supported build the `-t` target routes server-side (psmux/psmux#483),
    # so an inherited client would be dragged into the throwaway session and the
    # probe would stay green through the very premise flip — the scrub is what
    # keeps "no client to move" true. A local scrub, not _new_session_env: this
    # call needs neither the CLAUDE_CODE strip nor PSMUX_ALLOW_NESTING.
    # PSMUX_DATA_DIR is kept, so the raw verbs still address the registry the
    # probe session lives in.
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
        "psmux now reports effect rather than dispatch for switch-client -t — the "
        "attached-count gate in switch_client is droppable and rc alone can answer"
    )


def test_premise_option_values_survive_the_control_line(probe):
    # The surviving half of the transportable premise: _transportable's refusals
    # are now bounded by this backend's OWN read parse, not the wire, so only
    # the permitted shapes are still a premise about psmux. A red here means a
    # shape the gate admits has started corrupting — silently unprunable
    # windows — which is the direction that must never pass unnoticed.
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

    # The permitted shapes: a refusal here would silently make windows unprunable.
    for value in (
        "\\\\srv\\share",
        "C:/Program Files/x",
        "a ; b",
        "a'b",
        "x\u00a0y",
        "\\\\srv\\share My Proj",
        "C:\\dir with space\\",
    ):
        accepted, got = roundtrip(value)
        assert accepted and got == value, (
            f"psmux no longer carries {value!r} verbatim — _transportable permits a "
            "shape that now corrupts, so a tag reads back different from the prune's"
        )
    # Interior tab specifically: the wire's own fix (#536) widened the client's
    # quoting test to char::is_whitespace(), and a tab is ASCII, so it rides the
    # same branch as the NBSP above by reading of the source. Measure it rather
    # than reason it — that is what this file is for.
    accepted, got = roundtrip("a\tb")
    assert accepted and got == "a\tb", (
        "psmux no longer carries an interior tab verbatim — _transportable stopped "
        "refusing interior whitespace when the 3.3.8 floor landed"
    )


def test_premise_target_less_display_message_ignores_tmux_pane(probe):
    mux, session, windows = probe
    # The founding divergence of the _display_message pin (gh-669): psmux
    # resolves a target-less display-message against the server's ACTIVE
    # window, ignoring the caller's TMUX_PANE.
    mux.select_window(windows[1])
    assert (
        _active_window(mux, session) == windows[1]
    ), "probe setup: could not focus the probe's other window"
    panes = mux._run(["list-panes", "-t", windows[0], "-F", "#{pane_id}"], check=False)
    assert panes.returncode == 0, f"probe setup: list-panes failed: {panes.stderr.strip()!r}"
    pane = panes.stdout.split()[0] if panes.stdout.split() else ""
    assert re.fullmatch(r"%\d+", pane), f"probe setup: unexpected pane id {pane!r}"
    env = _new_session_env()
    for var in ("TMUX", "PSMUX_TARGET_SESSION", "PSMUX_TARGET_FULL"):
        env.pop(var, None)
    env["TMUX_PANE"] = pane  # a caller in a NON-active window, as psmux sets it
    answered = mux._run(["display-message", "-p", "#{window_id}"], check=False, env=env)
    assert (
        answered.returncode == 0
    ), f"probe setup: target-less display-message failed: {answered.stderr.strip()!r}"
    active = windows[1].rsplit(":", 1)[1]
    assert answered.stdout.strip() == active, (
        "psmux now resolves a target-less display-message via the caller's "
        "TMUX_PANE instead of the active window — the `-t $TMUX_PANE` pin in "
        "psmux_backend._display_message (gh-669) has become droppable"
    )


def test_premise_a_dash_leading_option_value_unsets_the_key(probe):
    """`set-option -t S @k -u` reads the value as the unset flag: rc 0, and the
    key is gone. That silent data loss is why `_transportable` refuses every
    `-`-leading value (psmux/psmux#583).

    Red means the installed psmux stores the value verbatim, and that refusal is
    droppable — but only behind the `_LAST_UNSUPPORTED` gate, set to the last
    release WITHOUT the fix (the gate is exclusive: ``available()`` admits only
    versions above it), since an older admitted build still unsets.
    """
    mux, session, _ = probe
    seeded = mux._run(["set-option", "-t", session, "@k", "v"], check=False)
    assert seeded.returncode == 0, f"probe setup: seeding @k failed: {seeded.stderr.strip()!r}"
    seen = mux._run(["show-options", "-qv", "-t", session, "@k"], check=False)
    assert seen.stdout.strip() == "v", f"probe setup: @k did not read back: {seen.stdout!r}"
    written = mux._run(["set-option", "-t", session, "@k", "-u"], check=False)
    assert written.returncode == 0, (
        "psmux now refuses a `-`-leading option value outright instead of silently "
        f"unsetting the key (psmux/psmux#583): {written.stderr.strip()!r}"
    )
    got = mux._run(["show-options", "-qv", "-t", session, "@k"], check=False)
    assert got.returncode == 0, f"probe setup: reading @k back failed: {got.stderr.strip()!r}"
    assert got.stdout.strip() == "", (
        "psmux now stores a `-`-leading option value verbatim (psmux/psmux#583, read "
        f"back {got.stdout.strip()!r}) — the `-` refusal in psmux_backend._transportable "
        "is droppable once _LAST_UNSUPPORTED is the last release without the fix"
    )


def test_premise_one_session_name_is_held_across_registries(tmp_path):
    """psmux's one-server-per-name guard is keyed on the session name alone, not
    the registry root, so while root A holds a session a second root cannot
    create one of the same name. That collision is why `runs.ctl_session_for`
    suffixes the control-session name per registry (psmux/psmux#599).

    Red means the guard follows `PSMUX_DATA_DIR` and the per-registry suffix is
    no longer forced by psmux — droppable only behind the `_LAST_UNSUPPORTED`
    gate, set to the last release WITHOUT the fix (the gate is exclusive).

    Only a refusal with nothing registered in B counts as the collision. A
    failed create whose server registers anyway is the client's readiness
    deadline under load, not the guard, and a fixed build can produce it too.
    Ceiling: B is read once, so a server registering after that read passes
    as the collision. That errs green, never red: on a fixed release it delays
    the flip to the next unloaded run instead of inventing one.

    Root A is torn down before root B, and that order is load-bearing: B's
    never-seen teardown ends in a process-table kill by session token, which
    would take A's live server of the same name if A were still standing.
    """
    mux = PsmuxMultiplexer()
    if not mux.available():
        pytest.skip("psmux present but not an admitted version")
    session = f"bmad-loop-dup-probe-{uuid.uuid4().hex[:8]}"
    envs = []
    for name in ("root-a", "root-b"):
        root = tmp_path / name
        root.mkdir()  # pre-created: see psmux_data_root for the measured cost
        env = _new_session_env()
        env["PSMUX_DATA_DIR"] = str(root)
        envs.append(env)
    env_a, env_b = envs
    a_seen = b_seen = False
    try:
        try:
            created_a = mux._run(
                ["new-session", "-d", "-s", session, "-c", str(tmp_path)], check=False, env=env_a
            )
            assert (
                created_a.returncode == 0
            ), f"probe setup: root A's session creation failed: {created_a.stderr.strip()!r}"
            a_seen = _plain_has_session(mux, session, env=env_a)
            assert a_seen, "probe setup: root A's session was not observable"
            created_b = mux._run(
                ["new-session", "-d", "-s", session, "-c", str(tmp_path)], check=False, env=env_b
            )
            b_seen = _plain_has_session(mux, session, env=env_b)
            # A dead A would let B's create succeed on any build: not a flip.
            assert _plain_has_session(
                mux, session, env=env_a
            ), "probe setup: root A's session died while root B created its own"
        finally:
            _teardown_probe_session(mux, session, env_a, known_created=a_seen)
    finally:
        _teardown_probe_session(mux, session, env_b, known_created=b_seen)
    assert created_b.returncode == 0 or not b_seen, (
        "probe setup: root B's create reported failure but its session registered "
        f"anyway — a readiness timeout, not the name guard: {created_b.stderr.strip()!r}"
    )
    assert created_b.returncode != 0, (
        "psmux now lets a second registry hold a session name another registry's live "
        "server owns (psmux/psmux#599) — the per-registry control-session suffix in "
        "runs.ctl_session_for (#733) retires, but only through a _LAST_UNSUPPORTED bump "
        "to the last release without the fix, never a deletion: an older admitted build "
        "still collides"
    )


def test_premise_has_session_reaps_a_live_registry_entry_on_a_connect_timeout(
    probe, psmux_data_root
):
    """A `has-session` whose 500 ms connect times out deletes the session's
    `.port` even though its server is alive — the reap window the accepted
    ceiling in `runs.live_session_may_be_ours` documents (psmux/psmux#622).

    Red means a timeout no longer reaps, and that ceiling's psmux half retires —
    only behind the `_LAST_UNSUPPORTED` gate, set to the last release WITHOUT
    the fix (the gate is exclusive), since an older admitted build still reaps.

    Determinism: the forged port is one this process has bound but never
    listens on, which on Windows times out rather than refuses (checked first:
    a refusal is reaped on every build and would prove nothing). The server's
    registry tick rewrites a differing `.port` at most every 5 s, so the probe
    forges once, waits for the tick to restore the real port, then forges again
    and asks `has-session` well inside that interval.
    """
    mux, session, _ = probe
    port_file = Path(psmux_data_root) / f"{session}.port"
    real = port_file.read_text(encoding="utf-8").strip()
    assert real.isdigit(), f"probe setup: unexpected .port contents {real!r}"
    dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        dead.bind(("127.0.0.1", 0))  # bound, never listening
        forged = str(dead.getsockname()[1])
        try:
            socket.create_connection(("127.0.0.1", int(forged)), timeout=0.5).close()
            outcome = "connected"
        except TimeoutError:
            outcome = "timeout"
        except OSError as exc:
            outcome = type(exc).__name__
        assert outcome == "timeout", (
            f"probe setup: a bound-but-unlistened loopback port answered {outcome}, "
            "not a timeout — a refusal is reaped on every build"
        )
        port_file.write_text(forged, encoding="utf-8")
        deadline = time.monotonic() + 15
        restored = False
        while time.monotonic() < deadline:
            try:
                restored = port_file.read_text(encoding="utf-8").strip() == real
            except OSError:
                restored = False
            if restored:
                break
            time.sleep(0.05)
        assert restored, "probe setup: the server's registry tick never restored .port"
        ticked = time.monotonic()
        port_file.write_text(forged, encoding="utf-8")
        asked = mux._run(["has-session", "-t", session], check=False)
        reaped = not port_file.exists()
        left = "" if reaped else port_file.read_text(encoding="utf-8").strip()
        elapsed = time.monotonic() - ticked
        assert elapsed < 4.0, (
            f"probe setup: has-session landed {elapsed:.1f}s after the registry tick, "
            "close enough to the next one that it may have rewritten .port"
        )
        # The tick restores the REAL port, which reads as neither outcome.
        assert reaped or left == forged, (
            f"probe setup: .port reads {left!r}, neither reaped nor the forged "
            f"{forged!r} — a registry tick raced the observation"
        )
        assert (
            asked.returncode != 0
        ), "probe setup: has-session answered through a port nothing listens on"
        assert reaped, (
            "psmux has-session no longer reaps a live session's .port on a connect "
            f"timeout (psmux/psmux#622, .port kept as {left!r}) — the reap-window ceiling "
            "prose in runs.live_session_may_be_ours (#754) narrows once _LAST_UNSUPPORTED "
            "is the last release without the fix"
        )
    finally:
        dead.close()
        # Re-publish the real port: the teardown addresses the server through it,
        # and an absent .port would read as a dead session until the next tick.
        port_file.write_text(real, encoding="utf-8")


# ------------------------------------------------- adopted-behavior probes
#
# The mirror image of the premise probes above. Those guarded workarounds and
# went red when upstream made one droppable; these guard the four behaviors the
# 3.3.8 floor let this backend START assuming, and go red if a later psmux takes
# one back. Without them the seam's only evidence for those four is a mocked
# argv shape, which cannot notice a regression in the binary at all. Ordinary
# semantics: red means broken.


def test_adopted_pipe_pane_delivers_pane_bytes_through_the_flag_transport(probe, tmp_path):
    mux, _, windows = probe
    # The real backend verb, not raw argv: the `-EncodedCommand` transport, the
    # base64 join and the _pwsh_quote'd sink path are all under test together.
    # A spaced path deliberately — it is what the sidecar could not carry.
    log = tmp_path / "run log.txt"
    text = ""
    # A fresh path per attempt: the sink holds the file open for Write sharing
    # only Read, so a retry against the same path would be denied the open and
    # die at once — reporting a file-sharing collision as "no pane bytes".
    for attempt, window in enumerate(windows):  # psmux/psmux#482's spawn race
        log = tmp_path / f"run log {attempt}.txt"
        mux.pipe_pane(window, log)
        for argv in (
            ["send-keys", "-t", window, "-l", "echo pipeprobe"],
            ["send-keys", "-t", window, "Enter"],
        ):
            sent = mux._run(argv, check=False)
            assert (
                sent.returncode == 0
            ), f"probe setup: {argv[0]} could not drive pipe-pane output: {sent.stderr.strip()!r}"
        deadline = time.monotonic() + 7.5
        while time.monotonic() < deadline:
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if "pipeprobe" in text:
                break
            time.sleep(0.25)
        if "pipeprobe" in text:
            break
    assert "pipeprobe" in text, (
        "the pipe-pane flag transport delivered no pane bytes — psmux stopped "
        "carrying dash-flag tokens or quoting through pipe-pane (psmux/psmux#482, "
        f"psmux/psmux#563), so the run log is silently empty. Captured: {text[:200]!r}"
    )
    assert not log.with_name(log.name + ".sink.ps1").exists()  # no sidecar, ever again


def test_a_pinned_pwsh_path_with_shell_syntax_launches_and_pipes(probe, tmp_path, monkeypatch):
    """The window and the pipe-pane sink run the absolute pwsh path the version
    probe read (#863), even when that path holds what psmux's own command
    tokenizer cannot carry: a space, `( )` as in `Program Files (x86)`, an
    apostrophe and a doubled space. A junction so named stands in for such an
    install, so the row holds wherever pwsh is installed; Windows reports a
    process launched through it under the junction path, so counting images
    there counts only the pinned pwsh itself, never a wrapper shell that merely
    names it in its arguments.

    A red here means psmux stopped running a `&`-led command under its own
    PowerShell verbatim, so the call-operator line no longer reaches pwsh."""
    mux, session, _windows = probe
    installed = Path(_REAL_PWSH_PATH(mux))
    link = tmp_path / "pinned (x86) pwsh's  dir"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(installed.parent)],
        capture_output=True,
        text=True,
        timeout=tmux_base.TMUX_TIMEOUT_S,
    )
    assert made.returncode == 0, f"probe setup: junction failed: {made.stderr.strip()!r}"
    pinned_images = (
        "@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "
        f"{psmux_backend._pwsh_quote(str(link) + chr(92) + '*')} }}).Count"
    )

    def await_images(at_least: int) -> int:
        deadline = time.monotonic() + 7.5
        count = int(_powershell(pinned_images).strip())
        while count < at_least and time.monotonic() < deadline:
            time.sleep(0.25)
            count = int(_powershell(pinned_images).strip())
        return count

    try:
        monkeypatch.setattr(mux, "_PWSH", str(link / installed.name))
        assert mux._pwsh_path() == str(link / installed.name)
        text = ""
        for attempt in range(2):  # psmux/psmux#482's spawn race
            before = await_images(0)
            window = _mint_probe_window(mux, session, f"pinned-{attempt}", tmp_path)
            # The parked window runs the pinned pwsh, before any sink exists.
            assert await_images(before + 1) >= before + 1, "the window is not the pinned pwsh"
            log = tmp_path / f"pinned log {attempt}.txt"
            mux.pipe_pane(window, log)
            # The sink is a second pinned image — read before the keystrokes
            # below answer the park and close the window.
            assert await_images(before + 2) >= before + 2, "the sink is not the pinned pwsh"
            for argv in (
                ["send-keys", "-t", window, "-l", "echo pinnedprobe"],
                ["send-keys", "-t", window, "Enter"],
            ):
                sent = mux._run(argv, check=False)
                assert sent.returncode == 0, f"probe setup: {argv[0]}: {sent.stderr.strip()!r}"
            deadline = time.monotonic() + 7.5
            while time.monotonic() < deadline:
                text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
                if "pinnedprobe" in text:
                    break
                time.sleep(0.25)
            if "pinnedprobe" in text:
                break
        assert "pinnedprobe" in text, f"no pane bytes through the pinned sink: {text[:200]!r}"
    finally:
        os.rmdir(link)  # the junction only, never the install it points at


def test_adopted_select_window_focuses_a_qualified_window_id(probe):
    mux, session, windows = probe
    # Start focused on the OTHER window, so a green here is a move rather than a
    # window that happened to be current already.
    mux.select_window(windows[1])
    assert (
        _active_window(mux, session) == windows[1]
    ), "probe setup: could not establish the select-window probe's starting focus"
    mux.select_window(windows[0])
    assert _active_window(mux, session) == windows[0], (
        "psmux no longer focuses `select-window -t session:@N` — the override "
        "dropped on the 3.3.8 floor (psmux/psmux#497) is needed again"
    )


def test_adopted_switch_client_rejects_an_unresolvable_target(probe):
    mux, session, _ = probe
    # The half of switch_client's verdict that needs no attached client: rc is
    # honest in the FAILURE direction, which is what lets the `-t` leg read it at
    # all — and what its `-l` fallback hangs on. Green with zero clients here and
    # green in the premise probe above (rc 0 with nothing to move) are what bound
    # rc between them — neither alone would justify trusting it (#659).
    #
    # Same scrub as that premise probe, and the same reason: pytest may itself be
    # running inside psmux, and a raw client verb issued with an inherited $TMUX
    # addresses the developer's server. Here it would also decide the rc — a
    # target unresolvable on THAT server proves nothing about this one.
    clientless = {
        key: value for key, value in os.environ.items() if key not in ("TMUX", "TMUX_PANE")
    }
    # `=session:%N` is what current_return_target composes and switch_client is
    # handed, so probe that grammar rather than the bare one (psmux strips the
    # leading `=`, but the rc under test belongs to the argv production emits).
    target = mux.target(session, "%9999")
    proc = mux._run(["switch-client", "-t", target], check=False, env=clientless)
    assert proc.returncode != 0, (
        "psmux now exits 0 for a switch-client target that cannot resolve — rc no "
        "longer separates a failed switch from a real one, so switch_client's "
        "verdict and its `-l` fallback both lose their source"
    )


def test_adopted_kill_session_honors_the_exact_match_target(probe):
    mux, session, _ = probe
    # The inherited base verb end-to-end — its `=name` argv included — once the
    # psmux override is gone. A red here means every session this seam creates
    # outlives its own teardown.
    mux.kill_session(session)
    assert not _plain_has_session(mux, session), (
        "psmux no longer honors the `=name` form for kill-session (psmux/psmux#558) "
        "— the plain-name override dropped on the 3.3.8 floor is needed again"
    )


def test_adopted_unresolvable_kill_target_exits_nonzero_and_spares_live_windows(probe):
    mux, session, _ = probe
    # Both halves matter and neither implies the other: the non-zero exit is what
    # triggers kill_window's survivor probe, and sparing the live windows is what
    # makes the old destructive workaround unnecessary (psmux/psmux#545).
    before = set(mux.list_window_ids(session))
    assert len(before) >= 2, f"probe setup: probe session should hold the parked windows: {before}"
    proc = mux._run(["kill-window", "-t", f"{session}:@9999"], check=False)
    assert proc.returncode != 0, (
        "psmux is back to exiting 0 for an unresolvable kill-window target — "
        "kill_window's survivor probe never triggers and a real failure stays silent"
    )
    after = set(mux.list_window_ids(session))
    assert after == before, (
        "an unresolvable kill-window target destroyed a live window again "
        f"(psmux/psmux#545): {sorted(before)} -> {sorted(after)}"
    )


def test_adopted_display_message_pane_target_resolves_globally(probe):
    mux, session, windows = probe
    # The premise the psmux _display_message override rests on (gh-669): a bare
    # `%N` display-message target resolves globally across windows
    # (DisplayMessageById, psmux/psmux#332), so a probe pinned to a pane of a
    # NON-active window answers that window — while a target-less probe answers
    # whichever window has focus. Focus the other window first, so a green here
    # is genuine cross-window resolution, not the active window by accident.
    mux.select_window(windows[1])
    assert (
        _active_window(mux, session) == windows[1]
    ), "probe setup: could not focus the probe's other window"
    panes = mux._run(["list-panes", "-t", windows[0], "-F", "#{pane_id}"], check=False)
    assert panes.returncode == 0, f"probe setup: list-panes failed: {panes.stderr.strip()!r}"
    pane = panes.stdout.split()[0] if panes.stdout.split() else ""
    assert re.fullmatch(r"%\d+", pane), f"probe setup: unexpected pane id {pane!r}"
    # Route by registry (the probe session is the isolated registry's only — and
    # otherwise its most recent — session): a bare `%N` target carries no session,
    # and this process's own $TMUX, when running inside a mux, would route the
    # probe to the wrong server entirely.
    env = _new_session_env()
    for var in ("TMUX", "PSMUX_TARGET_SESSION", "PSMUX_TARGET_FULL"):
        env.pop(var, None)
    answered = mux._run(["display-message", "-p", "-t", pane, "#{window_id}"], check=False, env=env)
    assert answered.returncode == 0, (
        "psmux no longer answers a bare `%N` display-message target "
        f"(psmux/psmux#332): {answered.stderr.strip()!r}"
    )
    expected = windows[0].rsplit(":", 1)[1]
    assert answered.stdout.strip() == expected, (
        "a pane-pinned display-message no longer resolves the pane's own window "
        "across window focus (DisplayMessageById, psmux/psmux#332) — the "
        "TMUX_PANE pin in psmux_backend._display_message answers for the "
        f"active window again: expected {expected!r}, got {answered.stdout.strip()!r}"
    )


def test_premise_a_port_and_key_pair_is_answered_by_whichever_server_it_names(
    probe, psmux_data_root
):
    """A `-t <session>` read resolves through that name's port and key files,
    and psmux does not check that the server behind them is the session asked
    for. Copying BOTH files under a second name is the reachable shape of that
    — a duplicated registry entry. Both are required: the server rejects a key
    mismatch before running anything, so a recycled port alone fails instead of
    answering. The read then lands at rc 0 with a real count belonging to the
    WRONG session, which is why _attached_clients compares the name it got back.

    The two assertions below carry OPPOSITE meanings, which is why neither says
    simply "droppable". A red on the rc means psmux started refusing the
    misroute, and the identity compare is then redundant. A red on the answered
    NAME means psmux started echoing the name that was asked for instead of the
    answering server's own — the compare would then be inert rather than
    redundant, and the guard needs replacing, not removing.

    Note what this probe cannot reach: a foreign server whose session genuinely
    carries the same name, or one whose name merely collides after the seam's
    own `.strip()`, is indistinguishable by name and stays #531's subject.
    """
    mux, session, _ = probe
    root = Path(psmux_data_root)
    forged = f"{session}-forged"
    for suffix in ("port", "key"):
        source = root / f"{session}.{suffix}"
        assert source.exists(), f"probe setup: no {source.name} to forge from"
        shutil.copyfile(source, root / f"{forged}.{suffix}")
    try:
        read = mux._run(
            ["display-message", "-p", "-t", forged, "#{session_attached}|#{session_name}"],
            check=False,
        )
        assert read.returncode == 0, (
            "psmux now REFUSES a port and key pair whose server does not own the name — the "
            f"identity compare in _attached_clients is redundant: {read.stderr.strip()!r}"
        )
        count, _, answered = read.stdout.strip().partition("|")
        assert answered == session, (
            "psmux now answers with the name that was ASKED for rather than the "
            "answering server's own — the identity compare in _attached_clients is "
            f"INERT, not redundant, and needs replacing rather than removing "
            f"(asked {forged!r}, got {answered!r})"
        )
        assert count.isdigit(), (
            "probe setup: the forged read gave no attached count "
            f"({read.stdout.strip()!r}) — it cannot show what a misroute hands back"
        )
    finally:
        for suffix in ("port", "key"):
            (root / f"{forged}.{suffix}").unlink(missing_ok=True)


def test_adopted_a_missing_registry_root_self_heals(tmp_path):
    """The derived root is a path under the state root that nothing creates in
    advance, so the design leans on psmux ``create_dir_all``-ing it itself
    (``server/mod.rs``, source-read at v3.3.8). Measured rather than trusted:
    without it every first run on a fresh project would fail at session creation.

    Its own root and its own session, deliberately, and not the module fixture's.
    Folding this into that fixture is what made every test here pay for the extra
    work on psmux's session-creation path — see the comment there for the measured
    cost. One test paying it is the right trade; the whole module paying it is not.
    """
    mux = PsmuxMultiplexer()
    if not mux.available():
        pytest.skip("psmux present but not an admitted version")
    root = tmp_path / "derived" / "never-created"
    assert not root.exists(), "probe setup: the root under test must not exist yet"
    env = _new_session_env()
    env["PSMUX_DATA_DIR"] = str(root)
    session = f"bmad-loop-heal-probe-{uuid.uuid4().hex[:8]}"
    try:
        created = mux._run(
            ["new-session", "-d", "-s", session, "-c", str(tmp_path)], check=False, env=env
        )
        # The DIRECTORY is the claim, not the exit code.
        #
        # `psmux: failed to create session` is the client's readiness poll hitting
        # `ready_deadline` (`src/main.rs`, source-read at v3.3.8), and building
        # this very directory is part of what it is waiting on. Under a loaded box
        # that fires while the server is still coming up — or before it comes up at
        # all. Neither says anything about `create_dir_all`, so neither may be read
        # as a premise flip; a server that never started is an INSTRUMENT failure,
        # the same class every `probe setup:` message in this module names.
        for _ in range(60):
            if root.is_dir():
                break
            time.sleep(0.25)
    finally:
        _teardown_probe_session(mux, session, env)

    if root.is_dir():
        return  # the claim holds

    # It does not, and the two reasons mean opposite things. An earlier revision
    # separated them by asking whether the SUBJECT's server was observable — which
    # it can never be while the root is absent: psmux creates the directory before
    # writing the registry files it publishes a server through (`server/mod.rs`,
    # source-read at v3.3.8), so no root means no port file means `has-session`
    # says no. The regression arm was unreachable and the load arm swallowed
    # everything, including the regression. A probe that cannot fail when its
    # premise flips is not a probe.
    #
    # What does separate them is a CONTROL: the identical mint against a
    # PRE-CREATED root, on this box, at this moment, under this load. If psmux can
    # start a server when the directory is already there and cannot when it is
    # not, the missing directory is the difference — that is the regression, and
    # every first run on a fresh project would fail at session creation. If it
    # cannot start one either way, the box is the difference and nothing was
    # measured.
    #
    # Minted only on this path, so the passing run still costs exactly one server.
    control_root = tmp_path / "control" / "pre-created"
    control_root.mkdir(parents=True)
    control_env = _new_session_env()
    control_env["PSMUX_DATA_DIR"] = str(control_root)
    control = f"bmad-loop-heal-control-{uuid.uuid4().hex[:8]}"
    try:
        control_made = mux._run(
            ["new-session", "-d", "-s", control, "-c", str(tmp_path)], check=False, env=control_env
        )
        control_up = False
        for _ in range(60):
            if _plain_has_session(mux, control, env=control_env):
                control_up = True
                break
            time.sleep(0.25)
    finally:
        _teardown_probe_session(mux, control, control_env)

    if control_up:
        pytest.fail(
            "psmux no longer creates a missing PSMUX_DATA_DIR: it started a server "
            f"under a pre-created root but left {str(root)!r} absent "
            f"(mint rc={created.returncode}, stderr={created.stderr.strip()!r}). The "
            "derived root must be mkdir'd before the first spawn, or every first run "
            "on a fresh project fails at session creation."
        )
    pytest.skip(
        "probe setup: psmux could not start a server on this box under either root "
        f"(missing-root rc={created.returncode}, stderr={created.stderr.strip()!r}; "
        f"pre-created-root rc={control_made.returncode}, "
        f"stderr={control_made.stderr.strip()!r}) — nothing was measured about "
        "create_dir_all"
    )


def test_adopted_a_relative_registry_root_is_refused_before_the_spawn(monkeypatch, tmp_path):
    """psmux ``assert!``s the root absolute and non-empty and panics otherwise
    (``src/paths.rs``, source-read at v3.3.8). The backend refuses first, so the
    operator gets one bmad-named error instead of a Rust panic whose nonzero exit
    ``has_session`` would report as an ordinary "no session".

    A red here means psmux started tolerating a relative root, and the gate in
    ``PsmuxMultiplexer._run`` became a refusal psmux itself no longer needs.
    """
    mux = PsmuxMultiplexer()
    if not mux.available():
        pytest.skip("psmux present but not an admitted version")
    monkeypatch.chdir(tmp_path)
    env = _new_session_env()
    env["PSMUX_DATA_DIR"] = "relative-root"
    # Bypass the backend's own gate deliberately: this probe is about psmux.
    raw = subprocess.run(
        ["psmux", "has-session", "-t", "no-such-session"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        env=env,
        timeout=tmux_base.TMUX_TIMEOUT_S,
    )
    assert raw.returncode != 0 and "PSMUX_DATA_DIR" in (raw.stderr or ""), (
        "psmux no longer refuses a relative PSMUX_DATA_DIR — the absoluteness gate "
        "in PsmuxMultiplexer._run is no longer standing in for a panic: "
        f"rc={raw.returncode} stderr={raw.stderr.strip()!r}"
    )


def test_a_powershell_older_than_7_3_refuses_the_launch_before_any_window(probe, monkeypatch):
    """The runtime half of the PowerShell floor (#861): the backend's version
    probe, pointed at the Windows PowerShell 5.1 that ships with Windows, reads
    a version below 7.3, and a parked-window launch then refuses before psmux
    mints anything. 5.1 is the stand-in for the pwsh 7.0-7.2 builds the floor
    exists for: its native-command builder corrupts argv the same way (an
    argument with a space and a trailing backslash swallows the next one).

    A red here means the refusal stopped gating the launch, or 5.1 stopped
    answering the probe's query."""
    if shutil.which("powershell.exe") is None:
        pytest.skip("requires Windows PowerShell 5.1 (powershell.exe)")
    mux, session, _windows = probe
    before = sorted(mux.list_window_ids(session))
    monkeypatch.setattr(mux, "_PWSH", "powershell.exe")
    with pytest.raises(tmux_base.TmuxError, match=r"PowerShell 7\.3 or later.*'5\.1\.") as refused:
        mux.new_parked_window(session, "legacy-shell", Path.cwd(), PARKED_ARGV, "@r")
    assert "upgrade pwsh to 7.3 or later" in str(refused.value)
    assert psmux_backend._PWSH_VERSIONS[mux._pwsh_path()].startswith("5.1.")
    assert sorted(mux.list_window_ids(session)) == before, "a window was minted despite the refusal"


# Argument shapes a window's argv must deliver byte-for-byte to its target on a
# supported pwsh, whatever kind of launcher the target is.
_ARGV_SHAPES = [
    "--state-root=C:\\dir with space\\",
    "--x=C:\\a b\\\\",
    'say "hi"',
    'a\\"b c',
    "",
    "žćč dir ü — 日本",
    "Use the $bmad-dev-auto skill",
    "spec at `x.md` now",
    "next",
]
_ARGV_PRINTER = (
    "import json, os, sys\n"
    "open(os.environ['ARGV_OUT'], 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
)


@pytest.mark.parametrize("launcher", ["exe", "ps1", "cmd"])
def test_a_window_delivers_argv_intact_to_every_launcher_kind(tmp_path, launcher):
    """The backend's own encoded source, run by the installed pwsh, hands the
    target its argv byte-for-byte: a native .exe, an npm-style .ps1 shim, and
    an npm-style .cmd batch launcher. Under the default Windows argument mode
    pwsh 7.3+ still builds a batch launcher's command line the legacy way, so
    the .cmd row fails on an embedded quote, a backslash-quote with a space
    and an empty argument unless the source opts into Standard mode first
    (`_shell_wrap`). No psmux server is involved: the window's shell command
    is run under pwsh `-Command`, exactly as psmux would spawn it."""
    mux = PsmuxMultiplexer()
    if not mux.available():
        pytest.skip("psmux present but not an admitted version")
    try:
        mux._require_pwsh_floor()
    except tmux_base.TmuxError as exc:
        # A pre-7.3 pwsh is the refusal test's host, not this scenario's.
        pytest.skip(f"pwsh below the supported floor: {exc}")
    printer = tmp_path / "printer.py"
    printer.write_text(_ARGV_PRINTER, encoding="utf-8")
    if launcher == "exe":
        target = [sys.executable, str(printer)]
    elif launcher == "ps1":
        policy = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", "Get-ExecutionPolicy"],
            capture_output=True,
            text=True,
            timeout=tmux_base.TMUX_TIMEOUT_S,
        ).stdout.strip()
        if policy in ("AllSigned", "Restricted"):
            pytest.skip(f"execution policy {policy} does not run a local .ps1")
        shim = tmp_path / "shim.ps1"
        shim.write_text(
            f"& {psmux_backend._pwsh_quote(sys.executable)} "
            '"$PSScriptRoot/printer.py" $args\nexit $LASTEXITCODE\n',
            encoding="utf-8",
        )
        target = [str(shim)]
    else:
        shim = tmp_path / "shim.cmd"
        shim.write_text(
            f'@ECHO off\r\n"{sys.executable}" "%~dp0printer.py" %*\r\n', encoding="utf-8"
        )
        target = [str(shim)]
    out = tmp_path / "argv.json"
    command = mux._shell_wrap(
        f"$env:ARGV_OUT = {psmux_backend._pwsh_quote(str(out))}; "
        + mux._join_argv([*target, *_ARGV_SHAPES])
    )
    # psmux runs the window's one call-operator line under its own PowerShell.
    ran = subprocess.run(
        [mux._pwsh_path(), "-NoLogo", "-Command", *command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=tmux_base.TMUX_TIMEOUT_S,
    )
    assert out.exists(), f"the {launcher} target never ran: {ran.stderr.strip()!r}"
    assert json.loads(out.read_text(encoding="utf-8")) == _ARGV_SHAPES
