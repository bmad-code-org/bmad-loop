"""psmux backend unit tests.

Deterministic: the single subprocess seam (``tmux_base.subprocess.run``) is
mocked, so these run on any OS. Shell source shipped as ``-EncodedCommand`` is
decoded back (base64 → UTF-16LE) to assert its composition.
"""

import base64
import os
import subprocess

import pytest

from bmad_loop.adapters import psmux_backend, tmux_base
from bmad_loop.adapters.multiplexer import MultiplexerError, get_multiplexer
from bmad_loop.adapters.psmux_backend import PsmuxMultiplexer
from bmad_loop.adapters.tmux_backend import TmuxMultiplexer
from bmad_loop.adapters.tmux_base import TmuxError


class _RecordRun:
    """Stand-in for subprocess.run that records every spawn's argv and kwargs."""

    def __init__(self, returncode: int = 0, stderr: str = "", stdout: str = ""):
        self.calls: list[tuple[list, dict]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout

    @property
    def argv(self):
        return self.calls[-1][0]

    @property
    def kwargs(self):
        return self.calls[-1][1]

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=self.stdout, stderr=self.stderr
        )


@pytest.fixture
def rec(monkeypatch):
    recorder = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", recorder)
    return recorder


def _decode(encoded: str) -> str:
    return base64.b64decode(encoded).decode("utf-16-le")


def _pwsh_payload(argv: list) -> str:
    """Assert the trailing args are a pwsh -EncodedCommand launch; return the
    decoded shell source."""
    assert argv[-4:-1] == ["pwsh", "-NoProfile", "-EncodedCommand"]
    return _decode(argv[-1])


# ------------------------------------------------------------------ decoding


def test_run_decodes_utf8_with_backslashreplace(rec):
    PsmuxMultiplexer()._run(["list-windows"])
    assert rec.kwargs["encoding"] == "utf-8"
    assert rec.kwargs["errors"] == "backslashreplace"


# ---------------------------------------------------------------- new_window


def test_new_window_ships_env_and_command_as_encoded_pwsh(rec, tmp_path):
    PsmuxMultiplexer().new_window(
        "s", "n", tmp_path, {"A": "x y", "B": "it's"}, "claude -p 'hi there'"
    )

    # the tmux-family scaffolding is the base's, spawned via the psmux binary,
    # with no -e flags — the env rides the encoded source's prelude instead
    assert rec.argv[:12] == [
        "psmux",
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
        "pwsh",
    ]
    assert "-e" not in rec.argv

    source = _pwsh_payload(rec.argv)
    # teammate-clear prelude, then env prelude, then the call-operator command
    assert source.index("Remove-Item") < source.index("$env:A")
    assert "'CLAUDE_CODE_*'" in source
    assert "'CLAUDECODE*'" in source
    assert "'PSMUX_CLAUDE_TEAMMATE_MODE'" in source
    assert "$env:A = 'x y'; " in source
    assert "$env:B = 'it''s'; " in source
    assert source.endswith("& 'claude' '-p' 'hi there'")


def test_new_window_rejects_invalid_env_name(rec, tmp_path):
    mux = PsmuxMultiplexer()
    for bad in ("A-B", "1X", "A B", "", "SAFE\n"):
        with pytest.raises(MultiplexerError):
            mux.new_window("s", "n", tmp_path, {bad: "v"}, "cmd")
    assert rec.calls == []  # rejected before any spawn


def test_new_window_rejects_malformed_command(rec, tmp_path):
    mux = PsmuxMultiplexer()
    # unbalanced quote (shlex can't split it) and an empty command (`& ` alone
    # is a pwsh parse error) both fail as the seam type, before any spawn
    for bad in ("claude -p 'x", "", "   "):
        with pytest.raises(MultiplexerError):
            mux.new_window("s", "n", tmp_path, {}, bad)
    assert rec.calls == []


def test_new_parked_window_rejects_empty_argv(rec, tmp_path):
    with pytest.raises(MultiplexerError):
        PsmuxMultiplexer().new_parked_window("s", "n", tmp_path, [], "")
    assert rec.calls == []


def test_new_window_literalizes_shell_operators(rec, tmp_path):
    # the seam's `command` is a POSIX-quoted argv join, not a shell line: pwsh
    # re-quoting turns would-be operators into literal arguments
    PsmuxMultiplexer().new_window("s", "n", tmp_path, {}, "a && b | c")
    source = _pwsh_payload(rec.argv)
    assert source.endswith("& 'a' '&&' 'b' '|' 'c'")


def test_new_window_env_values_stay_inert_literals(rec, tmp_path):
    # Env values are attacker-shaped strings from the caller's perspective:
    # pwsh must receive each one as a single-quoted literal with no room for
    # interpolation, subexpression, or quote breakout.
    hostile = {
        "A": "it's",
        "B": "line1\nline2",
        "C": "$(Remove-Item x)",
        "D": "`; Write-Host pwned",
        "E": "",
        "F": "'; Remove-Item -Recurse 'C:\\ #",
    }
    PsmuxMultiplexer().new_window("s", "n", tmp_path, hostile, "prog")
    source = _pwsh_payload(rec.argv)
    for key, value in hostile.items():
        assert f"$env:{key} = '{value.replace(chr(39), chr(39) * 2)}'; " in source
    # with doubled quotes collapsed, every remaining quote must pair up — an
    # odd count means some value broke out of its literal
    assert source.replace("''", "").count("'") % 2 == 0


# ------------------------------------------- session-qualified window ids (#254)
# psmux mints window ids per server (one server per session), so a bare `@N`
# replayed as a `-t` target routes by the caller's $TMUX — the wrong server
# from a ctl pane. new_window and list_window_ids must therefore emit the
# `session:@N` form symmetrically (psmux/psmux#483), or window_alive's
# membership check reads every window as dead.


def _window_fake(monkeypatch, new_window_id: str = "@2\n", listed: str = "@1\n@2\n"):
    """Script new-window to print an id and list-windows to list ids."""

    def fake(argv, **kwargs):
        out = new_window_id if argv[1] == "new-window" else listed
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)


def test_new_window_returns_session_qualified_id(monkeypatch, tmp_path):
    _window_fake(monkeypatch)
    assert PsmuxMultiplexer().new_window("s", "n", tmp_path, {}, "prog") == "s:@2"


def test_list_window_ids_returns_session_qualified_ids(monkeypatch):
    _window_fake(monkeypatch)
    assert PsmuxMultiplexer().list_window_ids("s") == ["s:@1", "s:@2"]


def test_list_windows_id_column_is_findable_in_list_window_ids(monkeypatch):
    """The ctl prune's kill verdict is a set membership ACROSS these two methods
    (#435): candidates carry list_windows' `window_id` column, the post-kill
    liveness check answers list_window_ids. Qualify one side only and every
    candidate reads as removed — the optimism the verdict exists to remove, with
    no error anywhere. The live gate (test_psmux_live) pins this too but needs
    Windows + psmux and is a manual CI gate; this one runs everywhere."""
    _window_fake(monkeypatch, listed="@1\t0\n@2\trun-x\n")
    rows = PsmuxMultiplexer().list_windows("s", ["window_id", "window_name"])
    assert [r[0] for r in rows] == ["s:@1", "s:@2"]  # guard: the column is populated

    _window_fake(monkeypatch, listed="@1\n@2\n")
    live = PsmuxMultiplexer().list_window_ids("s")
    assert all(row[0] in live for row in rows)


def test_qualification_degrades_to_bare_on_colon_session(monkeypatch, tmp_path):
    # A `:` in the session name would split the target at the wrong colon on
    # replay — both methods degrade to the bare id identically (the #221 rule).
    _window_fake(monkeypatch)
    mux = PsmuxMultiplexer()
    assert mux.new_window("a:b", "n", tmp_path, {}, "prog") == "@2"
    assert mux.list_window_ids("a:b") == ["@1", "@2"]


def test_new_window_falsy_id_passes_through_unqualified(monkeypatch, tmp_path):
    # An empty minted id is a failure sentinel, not a target — qualifying it
    # would forge "s:" out of nothing.
    _window_fake(monkeypatch, new_window_id="")
    assert PsmuxMultiplexer().new_window("s", "n", tmp_path, {}, "prog") == ""


def test_window_alive_accepts_new_window_id(monkeypatch, tmp_path):
    # Symmetry is the whole contract: the id new_window mints must be found by
    # list_window_ids, or the engine's liveness probe declares an instant crash.
    _window_fake(monkeypatch)
    mux = PsmuxMultiplexer()
    assert mux.window_alive("s", mux.new_window("s", "n", tmp_path, {}, "prog")) is True


def test_qualified_id_reaches_the_pipe_pane_target(rec, tmp_path):
    # #254 is about the `-t` argv, not the return value: the consumer must replay
    # the minted id verbatim, and this backend's pipe_pane override (the one verb
    # it reimplements) must not re-derive a bare target of its own.
    rec.stdout = "@2\n"
    mux = PsmuxMultiplexer()
    mux.pipe_pane(mux.new_window("s", "n", tmp_path, {}, "prog"), tmp_path / "run.log")
    assert rec.argv[1:4] == ["pipe-pane", "-t", "s:@2"]


def test_list_window_ids_transport_failure_still_raises(monkeypatch):
    # Qualification must not soften the liveness contract: a transport failure
    # raises rather than answering [] (which would read as "session crashed").
    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(["psmux"], 30)

    monkeypatch.setattr(tmux_base.subprocess, "run", timeout)
    with pytest.raises(MultiplexerError):
        PsmuxMultiplexer().list_window_ids("s")


# --------------------------------------------------------------- new_session


def test_new_session_bypasses_nesting_guard(rec, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", "1234")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("PSMUX_CLAUDE_TEAMMATE_MODE", "tmux")
    monkeypatch.setenv("Claude_Code_Mixed", "mixed")
    before = dict(os.environ)
    PsmuxMultiplexer().new_session("s", tmp_path, cols=80, lines=24)

    create_argv, create_kwargs = rec.calls[0]
    assert create_argv == [
        "psmux",
        "new-session",
        "-d",
        "-s",
        "s",
        "-c",
        str(tmp_path),
        "-x",
        "80",
        "-y",
        "24",
    ]
    # the no-op belt: create is verified by a has-session probe afterwards
    assert rec.argv == ["psmux", "has-session", "-t", "=s"]
    assert create_kwargs["env"]["PSMUX_ALLOW_NESTING"] == "1"
    # the claude session vars are scrubbed from the create env (the psmux server
    # this call may cold-start would otherwise hand them to every window)
    assert "CLAUDE_CODE_SSE_PORT" not in create_kwargs["env"]
    assert "CLAUDECODE" not in create_kwargs["env"]
    assert "PSMUX_CLAUDE_TEAMMATE_MODE" not in create_kwargs["env"]
    assert "Claude_Code_Mixed" not in create_kwargs["env"]
    # the bypass var and the scrub are confined to the child spawn
    assert dict(os.environ) == before


def test_new_session_omits_geometry_when_unset(rec, tmp_path):
    PsmuxMultiplexer().new_session("s", tmp_path)
    create_argv = rec.calls[0][0]
    assert "-x" not in create_argv
    assert "-y" not in create_argv


def test_new_session_exit_zero_noop_raises(monkeypatch, tmp_path):
    # The nesting guard's historical failure mode: new-session exits 0 having
    # created nothing. The belt verifies and blames session creation directly.
    def fake(argv, **kwargs):
        rc = 1 if argv[1] == "has-session" else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    with pytest.raises(MultiplexerError, match="was not created"):
        PsmuxMultiplexer().new_session("s", tmp_path)


def test_new_session_failure_raises_multiplexer_error(monkeypatch, tmp_path):
    monkeypatch.setattr(tmux_base.subprocess, "run", _RecordRun(returncode=1, stderr="boom"))
    with pytest.raises(MultiplexerError):
        PsmuxMultiplexer().new_session("s", tmp_path)

    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(["tmux"], 30)

    monkeypatch.setattr(tmux_base.subprocess, "run", timeout)
    with pytest.raises(MultiplexerError):
        PsmuxMultiplexer().new_session("s", tmp_path)


# --------------------------------------------------------------- kill_session


def test_kill_session_uses_the_inherited_exact_match_target(rec, monkeypatch):
    # 3.3.8 honors the `=name` exact-match form (psmux/psmux#558), so the base's
    # argv is correct here and the override is gone. strict which-stub: the
    # base's guard must probe the psmux binary, not a copy-pasted "tmux".
    monkeypatch.setattr(
        psmux_backend.shutil,
        "which",
        lambda name: "C:\\bin\\psmux.exe" if name == "psmux" else None,
    )
    PsmuxMultiplexer().kill_session("s")
    assert rec.argv == ["psmux", "kill-session", "-t", "=s"]


def test_kill_session_no_binary_no_spawn(rec, monkeypatch):
    monkeypatch.setattr(psmux_backend.shutil, "which", lambda _name: None)
    PsmuxMultiplexer().kill_session("s")
    assert rec.calls == []


# ------------------------------------------------------- return target (#221)
# psmux runs one server per session, so the parked-window return target must be
# session-qualified: a bare %N replayed from the control session is at best
# unresolvable, at worst collides with a real control-session pane
# (psmux/psmux#483). The seam default (bare pane id) stays correct for tmux —
# whose switch-client rejects the qualified form — so the composition lives in
# this backend's override.


def _probe_fake(monkeypatch, answers: dict[str, tuple[int, str]]):
    """Script the display-message probes: fmt -> (returncode, stdout)."""

    def fake(argv, **kwargs):
        rc, out = answers[argv[-1]]
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    monkeypatch.setenv("TMUX", "/tmp/psmux-1000/default,123,0")  # inside psmux
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)


def test_return_target_session_qualified(monkeypatch):
    _probe_fake(monkeypatch, {"#{pane_id}": (0, "%9\n"), "#{session_name}": (0, "main\n")})
    assert PsmuxMultiplexer().current_return_target() == "=main:%9"


def test_return_target_none_outside_mux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tmux_base.subprocess, "run", _RecordRun())
    assert PsmuxMultiplexer().current_return_target() is None


def test_return_target_none_on_empty_pane(monkeypatch):
    _probe_fake(monkeypatch, {"#{pane_id}": (0, "\n"), "#{session_name}": (0, "main\n")})
    assert PsmuxMultiplexer().current_return_target() is None


def test_return_target_bare_pane_when_session_probe_fails(monkeypatch):
    # A resolvable own pane means we ARE inside the multiplexer; a failed
    # session-name probe degrades to the bare pane id, never to None (which
    # callers would record as "detach" and strand the client).
    _probe_fake(monkeypatch, {"#{pane_id}": (0, "%9\n"), "#{session_name}": (1, "")})
    assert PsmuxMultiplexer().current_return_target() == "%9"


def test_return_target_bare_pane_on_empty_session_name(monkeypatch):
    # rc-0 empty stdout from the session probe must degrade the same way a
    # failed probe does — a "=:%9" target would misparse at replay.
    _probe_fake(monkeypatch, {"#{pane_id}": (0, "%9\n"), "#{session_name}": (0, "\n")})
    assert PsmuxMultiplexer().current_return_target() == "%9"


def test_return_target_bare_pane_on_unqualifiable_session_name(monkeypatch):
    # A session name the `=session:%N` grammar cannot carry (a `:` would split
    # at the wrong colon on replay) degrades to the bare id too.
    _probe_fake(monkeypatch, {"#{pane_id}": (0, "%9\n"), "#{session_name}": (0, "a:b\n")})
    assert PsmuxMultiplexer().current_return_target() == "%9"


# ------------------------------------------------------------- parked window


def test_new_parked_window_composes_pwsh_source(rec, tmp_path):
    PsmuxMultiplexer().new_parked_window("s", "n", tmp_path, ["claude", "--resume"], "%3")

    # calls[0]: the mint; the orphan-key sweep spawns after it (own test below)
    source = _pwsh_payload(rec.calls[0][0])
    prefix_end = source.index("& 'claude' '--resume'")
    assert "Remove-Item" in source[:prefix_end]  # teammate-clear prelude first
    # A not-recognized command leaves $LASTEXITCODE unset but the source keeps
    # running, so the banner needs a fallback code that also works before pwsh 7.
    assert (
        "& 'claude' '--resume'; "
        "$ec = if ($null -eq $LASTEXITCODE) { 1 } else { $LASTEXITCODE }; "
        'Write-Host "[bmad-loop exited $ec — press enter]"; Read-Host; ' in source
    )
    # trailer: same tmux-family verbs as the POSIX one, pwsh control flow,
    # issued through the psmux binary. The read is session-scoped and keyed by
    # the window id the trailer probes for itself (#310) — `-wqv` is dead here.
    assert "-wqv" not in source
    assert (
        '$wid = "$(psmux display-message -p -t $env:TMUX_PANE'
        " '#{window_id}' 2>$null)\".Trim(); " in source
    )
    assert "if ($wid) { " in source
    # Derived, not hardcoded: the trailer must compose the same key
    # _scoped_option_key mints, and the splice only works because $wid (`@N`)
    # supplies the marker's trailing `@` — pin both halves of that coupling.
    marker = PsmuxMultiplexer._SCOPE_MARKER
    assert marker.endswith("@")
    assert f"$key = '%3{marker[:-1]}' + $wid; " in source
    assert '$ret = "$(psmux show-options -qv $key 2>$null)".Trim(); ' in source
    assert "if ($ret -eq 'detach') { psmux detach-client 2>$null }" in source
    assert "psmux switch-client -t $ret 2>$null" in source
    assert "psmux switch-client -l 2>$null" in source
    assert "psmux set-option -u $key 2>$null" in source


# ------------------------------------------------------------------ pipe_pane


# The path shapes the sidecar could not carry: an apostrophe (doubled by
# _pwsh_quote), a space and `$`/backtick interpolation syntax. All three ride
# inside the base64 now, so they are one parametrization rather than a refusal.
@pytest.mark.parametrize("name", ["win's.log", "my run.log", "$name`x.log"])
def test_pipe_pane_ships_the_sink_as_an_encoded_flag_transport(rec, tmp_path, name):
    log = tmp_path / name
    PsmuxMultiplexer().pipe_pane("@1", log)

    assert len(rec.calls) == 1
    assert rec.argv[:5] == ["psmux", "pipe-pane", "-t", "@1", "-o"]
    # One `-o` string, space-joined: base64 is [A-Za-z0-9+/=], so nothing in the
    # composed command needs quoting against psmux's re-parse.
    piped = rec.argv[5].split(" ")
    assert piped[:3] == ["pwsh", "-NoProfile", "-EncodedCommand"]
    sink = _decode(piped[3])
    # byte-exact raw stream copy (no console decode / re-encode / CRLF mangling),
    # flushed per chunk so the live tail sees bytes incrementally
    quoted = str(log).replace(chr(39), chr(39) * 2)
    assert f"[System.IO.File]::Open('{quoted}', 'Append', 'Write', 'Read')" in sink
    assert "$in.Read($buf, 0, $buf.Length)" in sink
    assert "$out.Flush()" in sink
    # nothing is written beside the log any more
    assert list(tmp_path.iterdir()) == []


def test_pipe_pane_swallows_failure_with_warning(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(tmux_base.subprocess, "run", _RecordRun(returncode=1, stderr="gone"))
    assert PsmuxMultiplexer().pipe_pane("@1", tmp_path / "log") is None
    assert "pipe-pane log capture failed" in capsys.readouterr().err


def test_pipe_pane_warns_without_spawning_on_an_unencodable_log_path(rec, capsys, tmp_path):
    # A lone surrogate (surrogateescape filesystem decoding) fails the UTF-16LE
    # encode inside _shell_wrap BEFORE _tmux ever runs — warn-never-raise must
    # hold there too, not only for the TmuxError arm.
    assert PsmuxMultiplexer().pipe_pane("@1", tmp_path / "x\ud800.log") is None
    assert rec.calls == []
    assert "pipe-pane log capture failed" in capsys.readouterr().err


# ------------------------------------------------------------------ selection


def test_available_requires_psmux_pwsh_and_supported_version(monkeypatch):
    # Only psmux + pwsh may be probed — a tmux drop-in is deliberately not
    # required, so a which() stub answering for anything else must not matter.
    monkeypatch.setattr(
        psmux_backend.shutil,
        "which",
        lambda name: f"C:\\bin\\{name}.exe" if name in ("psmux", "pwsh") else None,
    )
    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 3.3.8")
    assert PsmuxMultiplexer().available() is True

    # 3.3.7 and older are refused: 3.3.6 force-kills recycled PIDs on teardown,
    # and 3.3.7 lacks the fixes this backend's verbs now assume.
    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 3.3.7")
    assert PsmuxMultiplexer().available() is False

    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 3.3.6")
    assert PsmuxMultiplexer().available() is False

    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 3.4.0")
    assert PsmuxMultiplexer().available() is True

    # multi-digit segments compare numerically, not lexicographically
    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 3.10.0")
    assert PsmuxMultiplexer().available() is True
    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 10.0")
    assert PsmuxMultiplexer().available() is True

    # a suffixed newer release still clears the strictly-greater gate; a
    # suffixed refused one still fails it (the suffix is not read at all)
    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 3.3.8-rc0")
    assert PsmuxMultiplexer().available() is True

    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 3.3.7-rc0")
    assert PsmuxMultiplexer().available() is False

    # a two-part compat version (tmux's own format) reads as patch 0
    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 3.4")
    assert PsmuxMultiplexer().available() is True

    monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self: "tmux 3.3")
    assert PsmuxMultiplexer().available() is False

    # unidentifiable version fails closed
    for garbled in (None, "", "tmux next-3.4", "psmux 9.9.9"):
        monkeypatch.setattr(PsmuxMultiplexer, "version", lambda self, v=garbled: v)
        assert PsmuxMultiplexer().available() is False


def test_available_composes_real_version_probe(monkeypatch):
    # End-to-end through the real version() seam (no version() stub): the gate
    # must survive `psmux -V` composition, including trailing-newline stripping.
    monkeypatch.setattr(
        psmux_backend.shutil,
        "which",
        lambda name: f"C:\\bin\\{name}.exe" if name in ("psmux", "pwsh") else None,
    )
    monkeypatch.setattr(tmux_base.shutil, "which", lambda name: f"C:\\bin\\{name}.exe")
    rec = _RecordRun(stdout="tmux 3.3.8\n")
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)
    assert PsmuxMultiplexer().available() is True
    assert rec.argv == ["psmux", "-V"]


def test_available_parses_the_gate_out_of_a_real_two_line_probe(monkeypatch):
    # psmux -V really prints two lines. version() folds them (#321), and the
    # gate reads the compat segment out of the fold — if a future change ever
    # puts psmux's own line first, this fails instead of silently reporting the
    # backend unavailable and taking psmux off Windows with no diagnostic.
    # One stub, not two: psmux_backend.shutil IS tmux_base.shutil (both plain
    # `import shutil`), so a second setattr would silently replace the first and
    # leave available()'s psmux+pwsh probe unexercised. This one answers for the
    # three binaries both layers ask about and refuses everything else.
    monkeypatch.setattr(
        psmux_backend.shutil,
        "which",
        lambda name: f"C:\\bin\\{name}.exe" if name in ("psmux", "pwsh", "tmux") else None,
    )

    for release, expected in (("3.3.8", True), ("3.3.7", False)):
        rec = _RecordRun(stdout=f"tmux {release}\npsmux {release} (66cf613 2026-08-18)\n")
        monkeypatch.setattr(tmux_base.subprocess, "run", rec)
        mux = PsmuxMultiplexer()
        assert mux.available() is expected
        # The fold reached the gate whole — both segments, one line.
        assert mux.version() == f"tmux {release}; psmux {release} (66cf613 2026-08-18)"


def test_available_caches_version_gate_per_instance(monkeypatch):
    monkeypatch.setattr(
        psmux_backend.shutil,
        "which",
        lambda name: f"C:\\bin\\{name}.exe" if name in ("psmux", "pwsh") else None,
    )
    calls = 0

    def probe(self):
        nonlocal calls
        calls += 1
        return "tmux 3.3.8"

    monkeypatch.setattr(PsmuxMultiplexer, "version", probe)
    mux = PsmuxMultiplexer()
    assert mux.available() is True
    assert mux.available() is True
    assert calls == 1  # repeated polls must not respawn the version query


def test_available_missing_binary_short_circuits_version_probe(monkeypatch):
    def no_probe(self):
        raise AssertionError("version() must not spawn when a binary is missing")

    monkeypatch.setattr(PsmuxMultiplexer, "version", no_probe)
    for absent in ("pwsh", "psmux"):
        monkeypatch.setattr(
            psmux_backend.shutil, "which", lambda name, a=absent: None if name == a else "x"
        )
        assert PsmuxMultiplexer().available() is False


def test_registry_selects_psmux_when_forced(monkeypatch):
    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "psmux")
    get_multiplexer.cache_clear()
    try:
        assert isinstance(get_multiplexer(), PsmuxMultiplexer)
    finally:
        get_multiplexer.cache_clear()  # don't leak the forced pick to other tests


# ------------------------------------------ TUI-side qualified window ids (#291)
# The launcher/prune surfaces hand ids around from a process that is usually
# OUTSIDE any pane, where a bare `@N` resolves through the most-recent-session
# fallback instead of the session that minted it. kill-window on such an id is
# destructive against the wrong server, so new_parked_window, the `window_id`
# columns of list_windows, and current_window_id all carry `session:@N`, and
# every `-t` consumer replays that form verbatim.


def _rows_fake(monkeypatch, rows: str, *, new_window_id: str = "@2\n"):
    """Script list-windows to emit tab-separated -F rows (and new-window an id)."""

    def fake(argv, **kwargs):
        out = new_window_id if argv[1] == "new-window" else rows
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)


def test_new_parked_window_returns_session_qualified_id(monkeypatch, tmp_path):
    _window_fake(monkeypatch)
    win = PsmuxMultiplexer().new_parked_window("ctl", "run-x", tmp_path, ["prog"], "@ret")
    assert win == "ctl:@2"


def test_new_parked_window_degrades_on_colon_session(monkeypatch, tmp_path):
    # Same #221 rule the engine-side mint follows: `a:b:@2` would split at the
    # wrong colon, so the id stays bare rather than becoming a wrong target.
    _window_fake(monkeypatch)
    win = PsmuxMultiplexer().new_parked_window("a:b", "run-x", tmp_path, ["prog"], "@ret")
    assert win == "@2"


def test_qualified_window_id_degrades_on_empty_session():
    # An empty session name would compose ":@2", which current_window_id parses
    # back to the bare "@2" — the two sides of the prune comparison must degrade
    # together, so the compose side degrades too.
    assert PsmuxMultiplexer()._qualified_window_id("", "@2") == "@2"


def test_new_parked_window_falsy_id_passes_through(monkeypatch, tmp_path):
    # An empty id is start_detached's "window id not captured" sentinel; forging
    # "ctl:" out of it would turn a detected failure into a plausible target.
    _window_fake(monkeypatch, new_window_id="")
    assert PsmuxMultiplexer().new_parked_window("ctl", "r", tmp_path, ["p"], "@ret") == ""


def test_list_windows_qualifies_only_the_window_id_column(monkeypatch):
    _rows_fake(monkeypatch, "@1\tshell\n@2\trun-x\n")
    rows = PsmuxMultiplexer().list_windows("ctl", ["window_id", "window_name"])
    assert rows == [("ctl:@1", "shell"), ("ctl:@2", "run-x")]


def test_list_windows_without_id_column_is_untouched(monkeypatch):
    # A names-only listing must pass through unrewritten.
    _rows_fake(monkeypatch, "shell\nrun-x\n")
    assert PsmuxMultiplexer().list_windows("ctl", ["window_name"]) == [("shell",), ("run-x",)]


def test_list_windows_degrades_on_colon_session(monkeypatch):
    _rows_fake(monkeypatch, "@1\tshell\n")
    assert PsmuxMultiplexer().list_windows("a:b", ["window_id", "window_name"]) == [("@1", "shell")]


_CURRENT_FMT = "#{session_name}:#{window_id}"


def test_current_window_id_is_session_qualified(monkeypatch):
    _probe_fake(monkeypatch, {_CURRENT_FMT: (0, "ctl:@2\n")})
    assert PsmuxMultiplexer().current_window_id() == "ctl:@2"


def test_current_window_id_matches_list_windows_form(monkeypatch):
    # The load-bearing symmetry: the prune candidate scan skips its own window by
    # comparing these two values. Qualify one side only and the scan stops
    # recognizing itself — a prune from inside a ctl window kills that window.
    _probe_fake(monkeypatch, {_CURRENT_FMT: (0, "ctl:@2\n")})
    mux = PsmuxMultiplexer()
    current = mux.current_window_id()
    _rows_fake(monkeypatch, "@1\tshell\n@2\trun-x\n")
    assert current in [row[0] for row in mux.list_windows("ctl", ["window_id", "window_name"])]


def test_current_window_id_resolves_session_and_id_in_one_probe(monkeypatch):
    # Two probes would open a gap where the id resolves and the session does not.
    # There is no safe answer in that gap — list_windows qualifies its rows from
    # the session it was PASSED, so they stay qualified whatever a probe here
    # says, and a bare id can never equal one: the prune would stop excluding its
    # own window and kill it. One expansion yields both parts or neither.
    recorder = _RecordRun(stdout="ctl:@2\n")
    monkeypatch.setenv("TMUX", "/tmp/psmux-1000/default,123,0")
    monkeypatch.setattr(tmux_base.subprocess, "run", recorder)
    assert PsmuxMultiplexer().current_window_id() == "ctl:@2"
    assert len(recorder.calls) == 1
    assert recorder.argv[1:] == ["display-message", "-p", _CURRENT_FMT]


def test_current_window_id_none_outside_mux(monkeypatch):
    # Not inside psmux: the probe is skipped entirely rather than answering for
    # some other client's session.
    monkeypatch.delenv("TMUX", raising=False)
    rec = _RecordRun()
    monkeypatch.setattr(tmux_base.subprocess, "run", rec)
    assert PsmuxMultiplexer().current_window_id() is None
    assert rec.calls == []


def test_current_window_id_bare_on_colon_session(monkeypatch):
    # `a:b:@2` cannot be split back at the right colon, so it degrades to the
    # bare id — and list_windows("a:b", ...) degrades its rows identically, so
    # the prune comparison still lines up.
    _probe_fake(monkeypatch, {_CURRENT_FMT: (0, "a:b:@2\n")})
    mux = PsmuxMultiplexer()
    assert mux.current_window_id() == "@2"
    _rows_fake(monkeypatch, "@2\trun-x\n")
    assert mux.list_windows("a:b", ["window_id", "window_name"]) == [("@2", "run-x")]


def test_current_window_id_none_on_unparseable_probe(monkeypatch):
    # A probe that did not answer a window id is not a target; composing one from
    # the fragment would aim a later kill somewhere unintended.
    for answer in ("ctl:\n", "ctl:notanid\n", ":\n", "ctl:@\n"):
        _probe_fake(monkeypatch, {_CURRENT_FMT: (0, answer)})
        assert PsmuxMultiplexer().current_window_id() is None, answer


@pytest.mark.parametrize("target", ["ctl:@3", "=ctl:@3", "=ctl:run-abc"])
def test_select_window_sends_every_target_form_unrewritten(rec, target):
    # psmux 3.3.8 resolves a scoped window-id target server-side (psmux/psmux#497),
    # so the backend inherits the base verb: no index resolve, no extra listing
    # round-trip, and the qualified id this backend mints is sent verbatim.
    PsmuxMultiplexer().select_window(target)
    assert rec.argv[1:] == ["select-window", "-t", target]
    assert len(rec.calls) == 1


def test_tmux_backend_keeps_bare_tui_ids(monkeypatch, tmp_path):
    # The divergence is psmux-only: tmux ids are server-global, so qualifying
    # them would produce targets its own verbs do not accept.
    _rows_fake(monkeypatch, "@1\tshell\n")
    mux = TmuxMultiplexer()
    assert mux.new_parked_window("ctl", "run-x", tmp_path, ["prog"], "@ret") == "@2"
    assert mux.list_windows("ctl", ["window_id", "window_name"]) == [("@1", "shell")]


# ------------------------------------- per-window option channel (#310)


def _option_fake(monkeypatch, *, rows: str = "", value: str = "", rc: int = 0):
    """Record every spawn; answer list-windows with `rows` and show-options with
    `value`."""
    recorder = _RecordRun()

    def fake(argv, **kwargs):
        recorder.calls.append((argv, kwargs))
        out = rows if argv[1] == "list-windows" else value
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    return recorder


def test_set_window_option_writes_id_keyed_session_option(monkeypatch):
    # The whole point: `-w` is dead on psmux, so the write goes to session scope
    # with the window id in the KEY, routed by an explicit `-t <session>`.
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().set_window_option("ctl:@3", "@bmad_project", "C:/p")
    assert rec_.argv[1:] == ["set-option", "-t", "ctl", "@bmad_project__blw@3", "C:/p"]
    assert "-w" not in rec_.argv


def test_set_window_option_resolves_a_name_token(monkeypatch):
    # set_return_pane passes `=session:<window-name>`, not an id — the channel
    # has to resolve it or the return target is recorded under no window at all.
    rec_ = _option_fake(monkeypatch, rows="@1\tshell\n@4\trun-abc\n")
    PsmuxMultiplexer().set_window_option("=ctl:run-abc", "@bmad_return_pane", "=ctl:%7")
    assert rec_.calls[0][0][1] == "list-windows"
    assert rec_.argv[1:] == ["set-option", "-t", "ctl", "@bmad_return_pane__blw@4", "=ctl:%7"]


def test_set_window_option_value_with_spaces_stays_one_argv_element(monkeypatch):
    # The transport gate is the general contract for every `@` option value: a
    # spaced value clears it via client quoting, and the channel must still pass
    # it as one argv element.
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().set_window_option("ctl:@2", "@bmad_project", r"C:\Users\Some User\p")
    assert rec_.argv[-1] == r"C:\Users\Some User\p"


def test_show_window_option_reads_the_id_keyed_session_option(monkeypatch):
    rec_ = _option_fake(monkeypatch, value="C:/p\n")
    assert PsmuxMultiplexer().show_window_option("ctl:@3", "@bmad_project") == "C:/p"
    assert rec_.argv[1:] == ["show-options", "-qv", "-t", "ctl", "@bmad_project__blw@3"]


def test_show_window_option_miss_reads_empty(monkeypatch, capsys):
    # A miss on live psmux is rc 0 + empty stdout (`-q` suppresses the line);
    # it is a real answer, so no warning.
    _option_fake(monkeypatch, value="", rc=0)
    assert PsmuxMultiplexer().show_window_option("ctl:@3", "@bmad_return_pane") == ""
    assert capsys.readouterr().err == ""


def test_show_window_option_transport_failure_warns(monkeypatch, capsys):
    # rc≠0 (dead server / bad session) is NOT a miss. The ABC read can only
    # degrade to "", but the failure must not be indistinguishable from unset.
    _option_fake(monkeypatch, value="", rc=1)
    assert PsmuxMultiplexer().show_window_option("ctl:@3", "@bmad_return_pane") == ""
    assert "failed" in capsys.readouterr().err


def test_unset_window_option_failure_warns(monkeypatch, capsys):
    # `-u` is a write: a silently failed unset leaves a live return key that
    # replays the return move when the parked window's command exits.
    rec_ = _option_fake(monkeypatch, rc=1)
    PsmuxMultiplexer().unset_window_option("ctl:@3", "@bmad_return_pane")
    assert rec_.calls  # the unset was attempted
    assert "failed" in capsys.readouterr().err


def test_unset_window_option_frees_the_key(monkeypatch):
    # `-u` genuinely removes it (user_options.remove), so a later read is
    # "unset" rather than an empty value that still occupies the map.
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().unset_window_option("ctl:@3", "@bmad_return_pane")
    assert rec_.argv[1:] == ["set-option", "-u", "-t", "ctl", "@bmad_return_pane__blw@3"]


@pytest.mark.parametrize("target", ["@3", "", "run-abc"])
def test_option_verbs_refuse_an_unroutable_target(monkeypatch, capsys, target):
    # No session means no `-t`, and without `-t` psmux routes by the
    # most-recent-session fallback — the misrouting this change exists to stop.
    # Declining loudly beats writing into an arbitrary server. Both mutating
    # verbs take the same gate.
    rec_ = _option_fake(monkeypatch)
    mux = PsmuxMultiplexer()
    mux.set_window_option(target, "@bmad_project", "C:/p")
    mux.unset_window_option(target, "@bmad_project")
    assert rec_.calls == []
    assert capsys.readouterr().err.count("does not resolve") == 2
    # An unroutable target with an untransportable value warns ONCE: the scope
    # gate runs first, so the refusal never re-enters the unset verb and
    # reports a `set-option -u` the caller never issued.
    mux.set_window_option(target, "@bmad_project", "a ; b")
    err = capsys.readouterr().err
    assert err.count("does not resolve") == 1
    assert "transport" not in err
    assert rec_.calls == []


def test_set_window_option_unresolvable_name_token_warns(monkeypatch, capsys):
    # A routable session but a name no window carries (died between listing and
    # targeting): the resolve comes back empty and the write must decline, not
    # invent a key.
    rec_ = _option_fake(monkeypatch, rows="@1\tshell\n")
    PsmuxMultiplexer().set_window_option("=ctl:run-gone", "@bmad_project", "C:/p")
    assert [c[0][1] for c in rec_.calls] == ["list-windows"]
    assert "does not resolve" in capsys.readouterr().err


def test_show_window_option_unroutable_target_reads_empty_with_warning(monkeypatch, capsys):
    # "" already means "unset" to every caller, but the miss is still said out
    # loud — an unroutable target sends no verb and warns on stderr, for reads
    # the same as for writes.
    rec_ = _option_fake(monkeypatch)
    assert PsmuxMultiplexer().show_window_option("@3", "@bmad_project") == ""
    assert rec_.calls == []
    assert "does not resolve" in capsys.readouterr().err


def test_builtin_window_option_still_takes_the_w_path(monkeypatch):
    # The 14 real window options are NOT broken on psmux; only `@` names are.
    # Rewriting `automatic-rename` to `automatic-rename_3` would break a
    # working verb.
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().set_window_option("ctl:@3", "automatic-rename", "off")
    assert rec_.argv[1:] == ["set-option", "-w", "-t", "ctl:@3", "automatic-rename", "off"]


def test_list_windows_fills_option_columns_per_window(monkeypatch):
    # `#{@bmad_project}` expands from the single per-server map, so asking psmux
    # for it hands every row the same value and the prune cannot discriminate.
    # The column is filled per window id instead, from ONE full option listing
    # (a flat extra call however many rows or `@` columns there are).
    listing = '@bmad_project__blw@2 "proj-b"\n@bmad_project__blw@3 "proj-a"\n'
    seen = []

    def fake(argv, **kwargs):
        seen.append(argv)
        if argv[1] == "list-windows":
            out = "@1\tshell\t@1\n@2\trun-x\t@2\n@3\trun-y\t@3\n"
        else:
            out = listing
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    rows = PsmuxMultiplexer().list_windows("ctl", ["window_id", "window_name", "@bmad_project"])
    assert rows == [
        ("ctl:@1", "shell", ""),  # no key in the listing = unset
        ("ctl:@2", "run-x", "proj-b"),
        ("ctl:@3", "run-y", "proj-a"),
    ]
    # the `@` field never reaches psmux's format string — that expansion is the bug
    assert "#{@bmad_project}" not in seen[0][-1]
    assert seen[0][-1] == "#{window_id}\t#{window_name}\t#{window_id}"
    assert len(seen) == 2  # the row listing + one option listing, nothing per-row


def test_list_windows_without_option_column_spawns_one_call(monkeypatch):
    # No `@` field means no per-row fill: the prune's common path must not pay
    # N extra round-trips for nothing.
    rec_ = _option_fake(monkeypatch, rows="@1\tshell\n")
    PsmuxMultiplexer().list_windows("ctl", ["window_id", "window_name"])
    assert len(rec_.calls) == 1


def test_tmux_backend_keeps_real_window_options(monkeypatch):
    # The divergence is psmux-only. tmux has genuine per-window user options, so
    # rewriting the key there would move state to a place nothing reads.
    rec_ = _option_fake(monkeypatch, value="C:/p\n")
    mux = TmuxMultiplexer()
    mux.set_window_option("ctl:@3", "@bmad_project", "C:/p")
    assert rec_.argv[1:] == ["set-option", "-w", "-t", "ctl:@3", "@bmad_project", "C:/p"]
    assert mux.show_window_option("ctl:@3", "@bmad_project") == "C:/p"
    assert rec_.argv[1:] == ["show-options", "-wqv", "-t", "ctl:@3", "@bmad_project"]


def test_kill_window_frees_only_that_windows_keys(monkeypatch):
    # Generic by the seam's `__blw@<digits>` marker: both bmad keys of @3 go;
    # @13's key (a suffix near-miss), @1's key, a foreign config option
    # (`@color_3`) and even a hand-written old-convention `@theme_@3` stay.
    # Order is kill-then-verify-then-clean: the kill fires first, the liveness
    # listing (without @3) proves it landed, then the keys are freed.
    listing = (
        '@bmad_project__blw@3 "proj-a"\n'
        '@bmad_return_pane__blw@3 "=ctl:%7"\n'
        '@bmad_project__blw@13 "proj-b"\n'
        '@bmad_project__blw@1 "proj-c"\n'
        '@color_3 "cfg"\n'
        '@theme_@3 "user"\n'
        "mouse on\n"
    )
    recorder = _RecordRun()

    def fake(argv, **kwargs):
        recorder.calls.append((argv, kwargs))
        out = {"show-options": listing, "list-windows": "@1\n@13\n"}.get(argv[1], "")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().kill_window("ctl:@3")
    assert recorder.calls[0][0][1:] == ["kill-window", "-t", "ctl:@3"]
    unset = [c[0][5] for c in recorder.calls if c[0][1] == "set-option"]
    assert sorted(unset) == ["@bmad_project__blw@3", "@bmad_return_pane__blw@3"]


def test_kill_window_name_target_resolves_scope_before_the_kill(monkeypatch):
    # A name token is only resolvable while the window lives — the scope
    # lookup must precede the kill, and the freed keys must be the resolved
    # id's, not the name's.
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "list-windows":
            # name-resolve asks for id+name; the liveness probe for ids only
            out = "@3\trun-abc\n@1\tshell\n" if "#{window_name}" in argv[-1] else "@1\n"
        elif argv[1] == "show-options":
            out = '@bmad_project__blw@3 "proj"\n'
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().kill_window("=ctl:run-abc")
    verbs = [c[1] for c in calls]
    assert verbs[0] == "list-windows"  # the name resolve, before the kill
    assert verbs[1] == "kill-window"
    assert [c[5] for c in calls if c[1] == "set-option"] == ["@bmad_project__blw@3"]


def test_kill_window_sends_the_kill_when_the_scope_lookup_dies(monkeypatch):
    # Resolving a name target costs a listing round-trip, and it happens BEFORE
    # the kill (a name is unresolvable once the window is dead) — so a dead
    # probe there must not cost the kill. It cannot: the base list_windows
    # swallows transport failures to [], so the scope degrades to None and the
    # kill goes out unscoped, leaving the keys to the orphan sweep. Pinned
    # because the ordering makes the opposite reading plausible on sight.
    sent = []

    def fake(argv, **kwargs):
        if argv[1] == "list-windows":
            raise subprocess.TimeoutExpired(argv, 1)
        sent.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().kill_window("=ctl:run-abc")  # must not raise
    assert [c[1] for c in sent] == ["kill-window"]


def test_kill_window_failed_kill_retains_the_keys(monkeypatch):
    # The containment flip: a kill that did not land (the window is still in
    # the liveness listing) must leave the live window its keys — a key-less
    # live window loses its project tag (prune retry degrades to the run-dir
    # fallback) and its return key (an attached client parks with no way back).
    recorder = _RecordRun()

    def fake(argv, **kwargs):
        recorder.calls.append((argv, kwargs))
        out = "@1\n@3\n" if argv[1] == "list-windows" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().kill_window("ctl:@3")
    assert not [c for c in recorder.calls if c[0][1] in ("set-option", "show-options")]


def test_kill_window_failure_warning_reads_the_qualified_listing(monkeypatch, capsys):
    # The base's survivor probe checks membership against list_window_ids,
    # which this backend qualifies (`ctl:@3`, not the base's bare `@3`); the
    # probe must recognize the surviving window through that shape or a failed
    # psmux kill would never warn.
    def fake(argv, **kwargs):
        if argv[1] == "kill-window":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom\n")
        out = "@1\n@3\n" if argv[1] == "list-windows" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().kill_window("ctl:@3")
    err = capsys.readouterr().err
    assert "kill-window ctl:@3 exited 1" in err
    assert "still alive" in err


def test_kill_window_unverifiable_liveness_retains_the_keys(monkeypatch):
    # An empty liveness listing is a failed probe, not proof of death (the ctl
    # session always keeps its shell window) — retaining beats freeing a live
    # window's keys; the launch-time orphan sweep reclaims real orphans later.
    recorder = _RecordRun()

    def fake(argv, **kwargs):
        recorder.calls.append((argv, kwargs))
        rc = 1 if argv[1] == "list-windows" else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().kill_window("ctl:@3")
    assert [c[0][1] for c in recorder.calls] == ["kill-window", "list-windows"]


def test_failed_kill_leaves_ownership_readable_for_the_prune_retry(monkeypatch):
    # The retained key is not just unswept — it must still answer a read,
    # because the prune retry re-identifies ownership through it. The fake is
    # STATEFUL on purpose: answering the read from a constant would pass even
    # with the liveness guard deleted, since a listing that never offers the key
    # leaves the cleanup nothing to free. `-qv` is the targeted read, bare `-q`
    # the full listing the cleanup sweeps.
    key = "@bmad_project__blw@3"
    store = {key: "tag-a"}

    def fake(argv, **kwargs):
        out = ""
        if argv[1] == "list-windows":
            out = "@1\n@3\n"  # the kill did not land
        elif argv[1] == "set-option" and "-u" in argv:
            store.pop(argv[-1], None)
        elif argv[1] == "show-options" and "-qv" in argv:
            out = store.get(argv[-1], "")
        elif argv[1] == "show-options":
            out = "".join(f'{k} "{v}"\n' for k, v in store.items())
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    mux = PsmuxMultiplexer()
    mux.kill_window("ctl:@3")
    assert mux.show_window_option("ctl:@3", "@bmad_project") == "tag-a"


def test_stranded_keys_after_cleanup_crash_are_reclaimed_by_the_sweep(monkeypatch):
    # The two halves of the strand-then-reclaim contract: a landed kill whose
    # key-free step dies strands the keys, and a later sweep claims them. The
    # sweep is driven directly here; new_parked_window's wiring to it is pinned
    # by test_new_parked_window_sweeps_orphan_keys.
    key = "@bmad_project__blw@3"
    state = {"healed": False}
    freed = []

    def fake(argv, **kwargs):
        if argv[1] == "show-options":
            if not state["healed"]:
                raise subprocess.TimeoutExpired(argv, 1)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{key} tag-a\n", stderr="")
        if argv[1] == "list-windows":
            return subprocess.CompletedProcess(argv, 0, stdout="@1\n", stderr="")
        if argv[1] == "set-option" and "-u" in argv:
            freed.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    mux = PsmuxMultiplexer()
    mux.kill_window("ctl:@3")  # kill lands, then the key listing dies
    assert not freed
    state["healed"] = True
    mux._sweep_orphan_keys("ctl")
    assert freed == [key]


def test_kill_window_cleanup_failure_never_fails_the_kill(monkeypatch):
    # The key listing dying after a landed kill must not raise; the orphan
    # sweep reclaims the stranded keys at the next parked-window launch.
    sent = []

    def fake(argv, **kwargs):
        if argv[1] == "show-options":
            raise subprocess.TimeoutExpired(argv, 1)
        sent.append(argv)
        out = "@1\n" if argv[1] == "list-windows" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().kill_window("ctl:@3")  # must not raise
    assert sent[0][1:] == ["kill-window", "-t", "ctl:@3"]


def test_kill_window_unlistable_options_warns_and_strands_the_keys(monkeypatch, capsys):
    # show-options rc!=0 after a verified kill is a transport failure, not "no
    # keys": nothing is freed, nothing raises, and the strand is visible.
    recorder = _RecordRun()

    def fake(argv, **kwargs):
        recorder.calls.append((argv, kwargs))
        rc = 1 if argv[1] == "show-options" else 0
        out = "@1\n" if argv[1] == "list-windows" else ""
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().kill_window("ctl:@3")  # must not raise
    assert not [c for c in recorder.calls if c[0][1] == "set-option"]
    assert "stranded keys await the orphan sweep" in capsys.readouterr().err


def test_kill_window_unfreeable_key_warns(monkeypatch, capsys):
    # A nonzero rc from `set-option -u` IS proof the key was not freed —
    # silence would leak it for the server's life with no signal.
    def fake(argv, **kwargs):
        rc = 1 if argv[1] == "set-option" else 0
        out = {
            "list-windows": "@1\n",
            "show-options": '@bmad_project__blw@3 "proj"\n',
        }.get(argv[1], "")
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="denied")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().kill_window("ctl:@3")  # must not raise
    assert "set-option -u @bmad_project__blw@3 on ctl failed" in capsys.readouterr().err


def test_a_dead_free_round_trip_does_not_abandon_the_rest_of_the_batch(monkeypatch, capsys):
    # Every free is contained to its own key. A transport failure on key N used
    # to escape to the sweep's outer guard, which warned once and left keys
    # N+1..M stranded — one intermittent round-trip costing the whole batch.
    attempted = []

    def fake(argv, **kwargs):
        if argv[1] == "show-options":
            out = '@bmad_project__blw@7 "gone"\n@bmad_return_pane__blw@7 "%9"\n'
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        if argv[1] == "list-windows":
            return subprocess.CompletedProcess(argv, 0, stdout="@1\n", stderr="")
        if argv[1] == "set-option":
            attempted.append(argv[-1])
            if len(attempted) == 1:
                raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer()._sweep_orphan_keys("ctl")
    assert attempted == ["@bmad_project__blw@7", "@bmad_return_pane__blw@7"]
    assert "set-option -u @bmad_project__blw@7 on ctl failed" in capsys.readouterr().err


def test_kill_window_unroutable_target_skips_cleanup_but_kills(monkeypatch):
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().kill_window("@3")
    assert [c[0][1] for c in rec_.calls] == ["kill-window"]


@pytest.mark.parametrize(
    "value",
    [
        'x"y',  # `"`: _scoped_options strips ONE surrounding pair, so it corrupts the read
        'x " y',  # same, spaced
        "bad\nline",  # the listing is parsed line-by-line — a break splits the value
        "bad\rline",  # splitlines() cuts on `\r` too, not just `\n`
        "",  # empty write is a silent server-side no-op; unset exists for this
        "-flag",  # dropped as a flag server-side, or flips to unset (psmux/psmux#583)
        " C:\\p x",  # leading space survives the wire but this backend's reads strip
        "C:\\p x ",  # trailing space, same round-trip loss
    ],
)
def test_set_window_option_refuses_untransportable_values(monkeypatch, capsys, value):
    # What survives the wire on 3.3.8 is not the whole story: the READ half is
    # ours, and a `"`, a line break or edge whitespace still cannot come back
    # verbatim. A corrupted stored tag never equals project_tag again — the
    # window turns silently unprunable — so refuse loudly instead of storing
    # garbage. The refusal also frees any prior value: a refused REwrite must
    # read as unset, not replay the stale value (e.g. an old parked return
    # target).
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().set_window_option("ctl:@3", "@bmad_project", value)
    assert [c[0][1:4] for c in rec_.calls] == [["set-option", "-u", "-t"]]
    assert "transport" in capsys.readouterr().err


@pytest.mark.parametrize(
    "value",
    [
        "a; b",
        "C:\\Users\\O'Brien Files\\proj",
        "C:\\Users\\O'Brien\\dev",  # spaceless `'` — no longer a quote opener
        "a;b",  # unspaced `;` (psmux/psmux#499)
        "a ; b",  # standalone `;` token — the chain splitter is quote-aware now
        "a \\; b",  # and its `\;` sibling
        "\\\\srv\\share My Proj",  # spaced UNC — `\\` no longer collapses (#547)
        "C:\\dir with space\\",  # trailing `\` no longer eats the closing quote
        "x\u00a0y",  # non-ASCII whitespace no longer splits server-side (#536)
    ],
)
def test_set_window_option_accepts_wire_safe_values(monkeypatch, value):
    # Every ordinary Windows path shape 3.3.8 carries verbatim MUST pass: a
    # gate still refusing them would silently untag apostrophed, UNC, spaced
    # and trailing-separator project directories.
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().set_window_option("ctl:@3", "@bmad_project", value)
    assert rec_.argv[-1] == value


def test_set_window_option_write_failure_warns(monkeypatch, capsys):
    # A tag that silently failed to land re-opens the mis-scoped prune: the
    # window reads as untagged and falls into the run-dir fallback.
    rec_ = _option_fake(monkeypatch, rc=1)
    PsmuxMultiplexer().set_window_option("ctl:@3", "@bmad_project", "C:/p")
    assert rec_.calls  # the write was attempted
    assert "failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "value",
    [
        'C:\\a"b\\proj',  # `"` cannot survive the listing's one-quote-pair strip
        "bad\nline",  # the listing is parsed line-by-line
        "",  # an empty write is a silent server-side no-op
        "-flag",  # dropped as a flag server-side (psmux/psmux#583)
    ],
)
def test_set_session_option_refuses_untransportable_values(monkeypatch, capsys, value):
    # Same gate as the window channel, on a write that was ungated before #320.
    # Refusing leaves the option unset, which the prune's run-dir fallback
    # handles correctly — a corrupted tag would strand the session forever.
    # The refusal FREES the key instead of just returning: the server loads the
    # user's psmux config, so the name can arrive pre-seeded, and a surviving
    # foreign value would read back as a real non-matching tag.
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().set_session_option("bmad-loop-x", "@bmad_project", value)
    assert [c[0][1:5] for c in rec_.calls] == [["set-option", "-u", "-t", "bmad-loop-x"]]
    assert rec_.argv[-1] == "@bmad_project"  # the key, never the rejected value
    assert "transport" in capsys.readouterr().err


@pytest.mark.parametrize(
    "value",
    [
        "C:\\my proj\\app",  # spaced
        "C:\\app",  # unspaced
        "\\\\srv\\share\\app",  # unspaced UNC
        "C:\\Users\\O'Brien Files\\proj",  # spaced `'`
        "C:\\a ; b\\proj",  # `;` tokens no longer split the value (psmux/psmux#499)
        "C:\\projects\\my proj\\",  # trailing `\` no longer eats the closing quote
        "\\\\srv\\share name\\proj",  # spaced UNC — `\\` no longer collapses (#547)
        "C:\\Users\\O'Brien\\proj",  # unspaced `'` no longer opens a quote
    ],
)
def test_set_session_option_accepts_transportable_values(monkeypatch, value):
    # All eight round-trip unchanged on psmux 3.3.8; a gate that refused them
    # would untag ordinary Windows project paths.
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().set_session_option("bmad-loop-x", "@bmad_project", value)
    assert rec_.argv[1:] == ["set-option", "-t", "bmad-loop-x", "@bmad_project", value]


def test_set_session_option_passes_builtin_options_through(monkeypatch):
    # The gate is for `@` user options only. A builtin keeps the base path even
    # with a value the `@` branch would refuse — narrowing it here would change
    # a verb this backend has no evidence about.
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().set_session_option("bmad-loop-x", "status-left", "-flag")
    assert rec_.argv[1:] == ["set-option", "-t", "bmad-loop-x", "status-left", "-flag"]


def test_set_session_option_refusal_free_failure_warns_not_raises(monkeypatch, capsys):
    # The free is best-effort like every other write in this channel: a dead
    # multiplexer must not abort session creation over a tag that is only an
    # optimization — but it must not pass silently either.
    _option_fake(monkeypatch, rc=1)
    PsmuxMultiplexer().set_session_option("bmad-loop-x", "@bmad_project", 'C:\\a"b')
    err = capsys.readouterr().err
    assert "transport" in err and "failed" in err


def test_set_session_option_write_failure_still_raises(monkeypatch):
    # An accepted value keeps the base's strict contract: session tagging runs
    # at session creation, where a dead multiplexer must not pass silently.
    _option_fake(monkeypatch, rc=1)
    with pytest.raises(TmuxError):
        PsmuxMultiplexer().set_session_option("bmad-loop-x", "@bmad_project", "C:\\app")


def test_list_windows_option_read_failure_degrades_to_unset_with_a_warning(monkeypatch, capsys):
    # A failed listing reads as "untagged", the same answer a genuinely untagged
    # window gives — safe because _ctl_window_candidates only claims an untagged
    # window whose run dir exists under THIS project, and run ids are unique. The
    # failure still warns: without it a prune --dry-run reports "nothing
    # prunable" with no trace of why. The other columns must survive intact.
    def fake(argv, **kwargs):
        if argv[1] == "show-options":
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, stdout="@1\tshell\t@1\n", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    rows = PsmuxMultiplexer().list_windows("ctl", ["window_id", "window_name", "@bmad_project"])
    assert rows == [("ctl:@1", "shell", "")]  # id column still qualified
    assert "listing failed" in capsys.readouterr().err


def test_list_windows_malformed_id_probe_degrades_to_unset(monkeypatch):
    # A probe value that is not a bare `@N` yields no key to look up, so the
    # column reads unset — same degrade as a dead listing.
    rec_ = _option_fake(monkeypatch, rows="weird\tshell\tweird\n", value="")
    rows = PsmuxMultiplexer().list_windows("ctl", ["window_id", "window_name", "@bmad_project"])
    assert rows[0][2] == ""
    assert rec_.calls  # the listing was still attempted


def test_list_windows_option_fill_declines_on_unroutable_session(monkeypatch):
    # The #221 degrade: `-t a:b` would route to server `a`. The id columns
    # degrade to bare ids; the option fill must decline the same way — "" with
    # no reads issued — rather than present another server's value as a tag.
    rec_ = _option_fake(monkeypatch, rows="@1\tshell\t@1\n")
    rows = PsmuxMultiplexer().list_windows("a:b", ["window_id", "window_name", "@bmad_project"])
    assert rows == [("@1", "shell", "")]
    assert len(rec_.calls) == 1  # the listing only — no show-options spawned


def test_new_parked_window_sweeps_orphan_keys(monkeypatch, tmp_path):
    # Enter-dismissing a parked window closes it without kill_window, so its
    # keys outlive it; launch reconciles. `__blw@7` has no window → freed.
    # `__blw@2` is live → kept. Foreign options are untouched — `@color_3`,
    # and even a hand-written `@theme_@3`, whose window `@3` is long gone: the
    # sweep matches the seam's own marker, not a naming convention.
    listing = (
        '@bmad_project__blw@7 "gone"\n'
        '@bmad_project__blw@2 "live"\n'
        '@color_3 "cfg"\n'
        '@theme_@3 "user"\n'
    )
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        out = {"new-window": "@2\n", "list-windows": "@1\n@2\n", "show-options": listing}.get(
            argv[1], ""
        )
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    win = PsmuxMultiplexer().new_parked_window("ctl", "run-x", tmp_path, ["prog"], "@ret")
    assert win == "ctl:@2"
    unset = [c[5] for c in calls if c[1] == "set-option"]
    assert unset == ["@bmad_project__blw@7"]


def test_tmux_backend_forwards_option_columns_to_format(monkeypatch):
    # The tmux prune path depends on `#{@bmad_project}` reaching the -F format
    # string — real per-window options expand per row there. Only psmux
    # synthesizes the column.
    recorder = _RecordRun(stdout="@1\tshell\tproj\n")
    monkeypatch.setattr(tmux_base.subprocess, "run", recorder)
    rows = TmuxMultiplexer().list_windows("ctl", ["window_id", "window_name", "@bmad_project"])
    assert "#{@bmad_project}" in recorder.argv[-1]
    assert rows == [("@1", "shell", "proj")]


@pytest.mark.parametrize(
    "value",
    ["\u00a0lead", "trail\u00a0", "\tlead", "trail\t", " lead", "trail ", "   "],
)
def test_set_window_option_refuses_edge_whitespace_of_any_kind(monkeypatch, capsys, value):
    # Not a wire property: this backend's own reads `.strip()`/`.Trim()`, and
    # both strip Unicode whitespace, so an edge NBSP or tab reads back short
    # exactly as an edge ASCII space does. Interior whitespace is fine. An
    # all-whitespace value is the degenerate case \u2014 it strips to "", i.e. the
    # empty write the gate refuses outright.
    rec_ = _option_fake(monkeypatch)
    PsmuxMultiplexer().set_window_option("ctl:@3", "@bmad_project", value)
    assert [c[0][1:4] for c in rec_.calls] == [["set-option", "-u", "-t"]]
    assert "transport" in capsys.readouterr().err


# `splitlines()` is the refusal, not a `\n` check: it also cuts on \v, \f, the
# file/group/record separators, NEL and the Unicode line/paragraph separators.
# Every one of them splits _scoped_options' line-by-line parse, so every one has
# to be refused \u2014 an `in "\r\n"` rewrite would look equivalent and quietly admit
# eight corrupting shapes.
@pytest.mark.parametrize(
    "sep",
    ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
    ids=lambda s: f"U+{ord(s):04X}",
)
def test_transportable_refuses_every_splitlines_separator(sep):
    assert not PsmuxMultiplexer._transportable(f"a{sep}b")


def test_sweep_snapshots_keys_before_live_windows(monkeypatch, tmp_path):
    # Order is the race guard: a window minted-and-tagged between the two
    # listings must have its key OUTSIDE the snapshot, so it can never read as
    # a false orphan. Pin the call order.
    order = []

    def fake(argv, **kwargs):
        order.append(argv[1])
        out = {"new-window": "@2\n", "list-windows": "@1\n@2\n", "show-options": ""}.get(
            argv[1], ""
        )
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().new_parked_window("ctl", "run-x", tmp_path, ["prog"], "@ret")
    sweep = [v for v in order if v in ("show-options", "list-windows")]
    assert sweep == ["show-options", "list-windows"]


def test_sweep_unlistable_options_warns(monkeypatch, capsys):
    # show-options rc!=0 aborts the sweep: a sweep that silently fails every
    # launch leaks keys for the server's whole life with no signal anywhere.
    def fake(argv, **kwargs):
        rc = 1 if argv[1] == "show-options" else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer()._sweep_orphan_keys("ctl")
    assert "orphan-key sweep on ctl could not list options" in capsys.readouterr().err


def test_sweep_treats_empty_live_list_as_failed_probe(monkeypatch, tmp_path, capsys):
    # We just minted a window in this session, so an empty live listing is a
    # failed probe, not an empty session — believing it would sweep every key,
    # live windows included. It is also said out loud: list_window_ids raises on
    # a transport fault and answers [] only on rc != 0, so this branch is a
    # server failing every launch, and silence would leak keys with no signal.
    listing = '@bmad_project__blw@2 "live"\n@bmad_project__blw@7 "gone"\n'
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "list-windows":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no server")
        out = {"new-window": "@2\n", "show-options": listing}.get(argv[1], "")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    PsmuxMultiplexer().new_parked_window("ctl", "run-x", tmp_path, ["prog"], "@ret")
    assert not [c for c in calls if c[1] == "set-option"]
    assert "orphan-key sweep on ctl could not list live windows" in capsys.readouterr().err


def test_sweep_transport_exception_never_fails_the_mint(monkeypatch, tmp_path, capsys):
    # The live-window probe can RAISE (the base converts a timeout to
    # TmuxError), not just answer empty — the sweep must swallow it (with a
    # trace) and the mint must still hand back the window id.
    def fake(argv, **kwargs):
        if argv[1] == "list-windows":
            raise subprocess.TimeoutExpired(argv, 1)
        out = {"new-window": "@2\n", "show-options": '@bmad_project__blw@7 "gone"\n'}.get(
            argv[1], ""
        )
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    win = PsmuxMultiplexer().new_parked_window("ctl", "run-x", tmp_path, ["prog"], "@ret")
    assert win == "ctl:@2"
    assert "sweep failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "value",
    [
        "a; b",
        "a;b",
        "C:\\Users\\O'Brien Files\\proj",
        "two  spaces",
        "C:/p",
        "\\\\srv\\share",
        "a ; b",
        "a \\; b",
        "C:\\dir with space\\",
        "\\\\srv\\share My Proj",
        "x\u00a0y",
    ],
)
def test_accepted_values_round_trip_through_the_listing_parse(monkeypatch, value):
    # The write gate and the read parser are two halves of one invariant:
    # every accepted value must read back IDENTICAL from the `@key "value"`
    # listing shape psmux emits — else the prune's equality compare breaks.
    # This is the bound on how far the gate may be narrowed: the wire getting
    # cleaner buys nothing for a shape this parse cannot carry.
    assert PsmuxMultiplexer._transportable(value)
    _option_fake(monkeypatch, value=f'@bmad_project__blw@3 "{value}"\n')
    options = PsmuxMultiplexer()._scoped_options("ctl")
    assert options == {"@bmad_project__blw@3": value}


def test_ctl_prune_scan_discriminates_projects_end_to_end(monkeypatch, tmp_path):
    # Acceptance-level: the real _ctl_window_candidates over the real psmux
    # backend (subprocess faked) — only this project's dead-run window is a
    # candidate; the other project's window and the shell window survive.
    from bmad_loop import runs
    from bmad_loop.tui import launch

    mine = str(tmp_path.resolve())
    listing = f'@bmad_project__blw@2 "{mine}"\n@bmad_project__blw@3 "C:/elsewhere"\n'

    def fake(argv, **kwargs):
        if argv[1] == "list-windows":
            out = "@1\tshell\t@1\n@2\trun-20260726-1\t@2\n@3\trun-20260726-2\t@3\n"
        elif argv[1] == "show-options":
            out = listing
        elif argv[1] == "has-session":
            out = ""
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    mux = PsmuxMultiplexer()
    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    monkeypatch.setattr(mux, "available", lambda: True)
    monkeypatch.setattr(mux, "current_window_id", lambda: None)  # outside any pane
    monkeypatch.setattr(launch, "get_multiplexer", lambda: mux)
    monkeypatch.setattr(launch, "mux_usable", lambda _mux: True)
    monkeypatch.setattr(runs, "is_run", lambda _dir: True)
    monkeypatch.setattr(runs, "engine_alive", lambda _dir: False)

    candidates = launch._ctl_window_candidates(tmp_path)
    assert candidates == [("bmad-loop-ctl:@2", "run-20260726-1")]


# ---------------------------------------- client verbs: observed effect (#317)


def _client_fake(monkeypatch, *, attached, session="ctl", verb_rc=0):
    """Script the two probes the client verbs measure with.

    ``attached`` is the successive answers to ``#{session_attached}``, consumed
    in call order — so a detach that worked is ["1", "0"] and psmux's rc-0 no-op
    is ["1", "1"]. Every verb exits ``verb_rc`` (0 by default: on psmux the exit
    code says nothing, which is the whole point).
    """
    monkeypatch.setenv("TMUX", "/tmp/psmux-1000/default,123,0")  # inside a pane
    counts = list(attached)
    calls: list[list] = []

    def fake(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "display-message" and argv[-1] == "#{session_name}":
            return subprocess.CompletedProcess(argv, 0, stdout=f"{session}\n", stderr="")
        if argv[1] == "display-message" and argv[-1] == "#{session_attached}":
            return subprocess.CompletedProcess(argv, 0, stdout=f"{counts.pop(0)}\n", stderr="")
        return subprocess.CompletedProcess(argv, verb_rc, stdout="", stderr="")

    monkeypatch.setattr(tmux_base.subprocess, "run", fake)
    return calls


def test_psmux_detach_reports_the_observed_drop(monkeypatch):
    calls = _client_fake(monkeypatch, attached=["1", "0"])
    assert PsmuxMultiplexer().detach_client() is True
    assert ["psmux", "detach-client"] in calls
    # both probes routed to the session, never left to the most-recent fallback
    assert calls.count(["psmux", "display-message", "-p", "-t", "ctl", "#{session_attached}"]) == 2


def test_psmux_detach_with_nothing_attached_is_false(monkeypatch):
    """psmux's detach-client exits 0 with zero clients attached, so the exit
    code cannot answer this — the count has to. Reading rc here is exactly the
    vacuous True that strands the human (#317)."""
    calls = _client_fake(monkeypatch, attached=["0", "0"])
    assert PsmuxMultiplexer().detach_client() is False
    assert ["psmux", "detach-client"] in calls  # attempted, just not effective


def test_psmux_client_verbs_never_claim_an_unobservable_effect(monkeypatch):
    """A psmux build that does not carry #{session_attached} echoes something
    non-numeric; that degrades to False, never to a vacuous True."""
    _client_fake(monkeypatch, attached=["#{session_attached}", "#{session_attached}"])
    assert PsmuxMultiplexer().detach_client() is False


def test_psmux_detach_outside_a_pane_is_false(monkeypatch):
    """No TMUX, so current_session answers None: there is no session to measure
    and no client of ours to move. Answer False without issuing a flag-less
    detach, which psmux promotes server-side to detach-all."""
    # Scripted counts a probe COULD read, so removing the guard fails this on
    # its assertions rather than on the fixture running dry.
    calls = _client_fake(monkeypatch, attached=["1", "0"])
    monkeypatch.delenv("TMUX", raising=False)
    assert PsmuxMultiplexer().detach_client() is False
    assert not any(c[1] == "detach-client" for c in calls)


def test_psmux_switch_reports_the_drop(monkeypatch):
    # `last_fallback=True` is what gives the last assertion teeth: with the
    # default False the `-l` leg is unreachable by construction, so "no fallback
    # when -t moved it" would hold even with the early return gone. Counts for
    # two probes for the same reason — a second one must reach the assertion
    # rather than run the fixture dry.
    calls = _client_fake(monkeypatch, attached=["1", "0", "1", "0"])
    assert PsmuxMultiplexer().switch_client("ctl:%9", last_fallback=True) is True
    assert ["psmux", "switch-client", "-t", "ctl:%9"] in calls
    # no fallback when -t moved it
    assert not any(c[1:3] == ["switch-client", "-l"] for c in calls)


def test_psmux_switch_falls_back_then_reports_the_drop(monkeypatch):
    calls = _client_fake(monkeypatch, attached=["1", "1", "1", "0"])
    assert PsmuxMultiplexer().switch_client("ctl:%9", last_fallback=True) is True
    assert ["psmux", "switch-client", "-l"] in calls


def test_psmux_switch_is_false_while_the_leg_is_inert(monkeypatch):
    """psmux/psmux#483: the switch moves no client, and every arm still exits 0.
    Both legs are attempted and both read as no effect, so the caller keeps
    prompting instead of being told the human was handed their terminal back."""
    calls = _client_fake(monkeypatch, attached=["1", "1", "1", "1"])
    assert PsmuxMultiplexer().switch_client("ctl:%9", last_fallback=True) is False
    assert ["psmux", "switch-client", "-t", "ctl:%9"] in calls
    assert ["psmux", "switch-client", "-l"] in calls


def test_psmux_switch_without_fallback_does_not_attempt_it(monkeypatch):
    # Scripted for two probes though one is expected: dropping the `last_fallback`
    # conjunct must fail this on its assertion, not on the counts running dry.
    calls = _client_fake(monkeypatch, attached=["1", "1", "1", "1"])
    assert PsmuxMultiplexer().switch_client("ctl:%9") is False
    assert not any(c[1:3] == ["switch-client", "-l"] for c in calls)
