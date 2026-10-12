"""Registry tests for the core `BMAD_LOOP_*` runtime overrides.

`envvars` is the one place each core var is named, typed, and given a reader, so
what is pinned here is the *contract* the call sites (`engine`,
`adapters.multiplexer`, `cli`, `process_host`, `runs`) and the "Environment variables"
table in `docs/reference/configuration.md` both depend on: the literal names, and each
reader's parse and fallback. The two name readers pass their value through
verbatim on purpose — validation lives downstream in the registry that resolves
the name — so a test asserting rejection here would be asserting the wrong
module's job. `state_dir` is verbatim for a related reason (a stated override
must not be silently swapped for a guess) with one exception, the empty string,
which is graded here because `runs.state_root` trusts what this reader returns.

Contract parity: `test_engine.py::test_session_timeout_s_env_override` pins the
same rejection set one layer up, through `Engine._session_timeout_s` (does the
policy default survive a bad override?). This file grades the reader itself
(what does the parse return?), which is why it carries the rows that only a
direct read can distinguish — `nan`, which parses, survives the `<= 0`
comparison, and is rejected only by the finiteness check — and the two name
readers the engine never touches. Deliberately
layered, not duplicated: a behavior change lands in both or records the
divergence.
"""

import pytest

from bmad_loop import envvars


def test_constants_are_the_literal_env_var_names():
    """The constants ARE the public contract: call sites import the name rather
    than spelling the string, and the configuration reference documents these literals for
    operators. Renaming one silently retires an override — the variable an
    operator exports simply stops being read, with no error anywhere — so the
    strings are pinned, not merely the readers that consume them."""
    assert envvars.SESSION_TIMEOUT_S == "BMAD_LOOP_SESSION_TIMEOUT_S"
    assert envvars.MUX_BACKEND == "BMAD_LOOP_MUX_BACKEND"
    assert envvars.PROCESS_HOST == "BMAD_LOOP_PROCESS_HOST"
    assert envvars.STATE_DIR == "BMAD_LOOP_STATE_DIR"


def test_session_timeout_s_is_none_when_unset(monkeypatch):
    """Unset is the ordinary case — `None` is what makes the engine keep its
    policy budget (`limits.session_timeout_min x 60`) instead of an override.

    Ablation target: delete the `if raw is None: return None` early-out and this
    test fails alone, on the `TypeError` `float(None)` raises — which the reader's
    `except ValueError` deliberately does not catch. The early-out is a gate in its
    own right, not a shortcut through the parse."""
    monkeypatch.delenv(envvars.SESSION_TIMEOUT_S, raising=False)
    assert envvars.session_timeout_s() is None


# The rejection is three independent gates, and every row below is held by exactly
# one of them — grade them singly, since ablating them together reddens every row
# at once and so grades none of them:
#   (a) the `try/except ValueError` holds the unparseable rows, which would
#       otherwise raise out of the reader instead of reading as None;
#   (b) the `value <= 0` half holds the zero and negative rows;
#   (c) the `not math.isfinite(value)` half holds the non-finite spellings — and
#       `nan`.
# ⚠️ (c) holding `nan` is load-bearing and easy to get backwards. Under the old
# `return value if value > 0 else None`, nan was rejected by the COMPARISON (every
# comparison against nan is False). The guard is now spelled `value <= 0`, and
# `nan <= 0` is ALSO False — so nan no longer falls out of the comparison and the
# finiteness check holds it alone. Same result, different gate: do not "simplify"
# (c) away on the theory that `> 0` already covers nan, because this form does not.
# The [inf]/[1e999]/[Infinity]/[INF] rows are why (c) exists at all: `float()`
# accepts all four and each passes a bare `> 0`, so before it they read as a real
# budget and the deadline the adapters compute from it (`time.monotonic() +
# timeout_s`) could never expire.
@pytest.mark.parametrize(
    "raw",
    [
        "not-a-number",  # unparseable: ValueError out of float()
        "",  # empty: also a ValueError, and the spelling an unset-looking export leaves
        "0",  # parses, but zero would expire every session instantly
        "0.0",  # the float spelling of the same
        "-1",  # negative: already-elapsed budget
        "-0.5",  # negative float
        "nan",  # parses; `nan <= 0` is False, so ONLY the finiteness check rejects it
        "inf",  # parses to inf and passes `> 0` — a deadline that never arrives
        "1e999",  # overflows to inf: the same hole reachable without typing "inf"
        "Infinity",  # float()'s other accepted spelling
        "INF",  # float() is case-insensitive here, so the guard must be too
    ],
)
def test_session_timeout_s_ignores_a_value_that_cannot_be_a_budget(monkeypatch, raw):
    """Anything that is not a finite positive number of seconds reads as `None`
    (ignored) rather than as a budget, and the guard is two-sided because the
    failure mode is silent in BOTH directions. A fat-fingered `0` or `-1` would
    not error — it would shorten every session to nothing and read as a run of
    instant timeouts. `inf` (or `1e999`, which overflows to it) is the opposite
    and worse: both adapters build their deadlines as `time.monotonic() +
    timeout_s`, so a non-finite budget yields a deadline that never arrives, and
    this is the outer bound every stall-grace and wake-nudge window defers to.
    An unattended run would wedge with nothing left to stop it."""
    monkeypatch.setenv(envvars.SESSION_TIMEOUT_S, raw)
    assert envvars.session_timeout_s() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3", 3.0),
        ("0.5", 0.5),
        # The deliberate boundary of the finiteness guard, pinned so it is a
        # reviewable decision rather than an accident: a huge but FINITE value is
        # still honoured. It is a duration, however unwise, and an operator asking
        # for one is expressing intent — where `inf` is not a duration at all.
        # Drawing the line anywhere else would mean inventing a ceiling, which is
        # a policy number this module has no business choosing.
        ("1e308", 1e308),
    ],
)
def test_session_timeout_s_reads_a_positive_override_as_seconds(monkeypatch, raw, expected):
    """Both spellings land as a float: the int-looking one an operator types and
    the sub-second one the E2E gates rely on (`test_stories_e2e` drives a
    3-second budget through this seam). The value is seconds, not minutes — the
    policy key it overrides is in minutes, which is exactly the confusion the
    reader's name and this assertion pin down."""
    monkeypatch.setenv(envvars.SESSION_TIMEOUT_S, raw)
    assert envvars.session_timeout_s() == expected


def test_mux_backend_is_a_verbatim_passthrough(monkeypatch):
    """The reader forces nothing and validates nothing — it hands back the raw
    string so the forced-selection semantics match the raw env read exactly.
    An unregistered name must survive the read *intact*: the multiplexer
    registry is what raises on it, and it can only do that if the bad name
    reaches it. A reader that swallowed the unknown name would turn a loud
    misconfiguration into a silent auto-select.

    Ablation target: make the reader "helpful" (`return raw if raw in {"tmux",
    "psmux"} else None`) and this test fails on the unregistered-name assertion
    alone — the unset and `tmux` assertions stay green under it, so neither pins
    the no-validation contract on its own."""
    monkeypatch.delenv(envvars.MUX_BACKEND, raising=False)
    assert envvars.mux_backend() is None

    monkeypatch.setenv(envvars.MUX_BACKEND, "tmux")
    assert envvars.mux_backend() == "tmux"

    monkeypatch.setenv(envvars.MUX_BACKEND, "no-such-backend")
    assert envvars.mux_backend() == "no-such-backend"


def test_process_host_is_a_verbatim_passthrough(monkeypatch):
    """Same contract as `mux_backend`, and for the same reason: `process_host`'s
    own registry raises `ProcessHostError` on an unregistered name rather than
    falling back to POSIX (on win32 `os.kill(pid, 0)` is destructive), so this
    reader must not filter the name on its way there."""
    monkeypatch.delenv(envvars.PROCESS_HOST, raising=False)
    assert envvars.process_host() is None

    monkeypatch.setenv(envvars.PROCESS_HOST, "posix")
    assert envvars.process_host() == "posix"

    monkeypatch.setenv(envvars.PROCESS_HOST, "bogus")
    assert envvars.process_host() == "bogus"


def test_state_dir_is_a_verbatim_passthrough_except_for_the_empty_string(monkeypatch):
    """`runs.state_root` uses this value as the state root itself, so the reader
    hands back what the operator wrote — a relative spelling included. Filtering it
    would put the loop in the position `mux_backend` refuses: an override the
    operator can see they exported, silently swapped for the platform guess, and
    detectable only by noticing where a run's events did *not* appear.

    The empty string is the exception, and it is a value this reader must hold
    rather than the caller: `export BMAD_LOOP_STATE_DIR=` is what an unset-looking
    export leaves behind, `Path("")` is the *current directory*, and rooting the
    control plane at the launch cwd is neither what was meant nor something a
    later process would resolve the same way. Reading it as unset falls through to
    the platform cascade, which is the same thing not exporting it at all does.

    Ablation target: spell the reader `os.environ.get(STATE_DIR)` and only the
    empty row fails — the unset and value rows pass under it, so neither pins the
    rule on its own."""
    monkeypatch.delenv(envvars.STATE_DIR, raising=False)
    assert envvars.state_dir() is None

    monkeypatch.setenv(envvars.STATE_DIR, "")
    assert envvars.state_dir() is None

    monkeypatch.setenv(envvars.STATE_DIR, "/var/lib/bmad-loop")
    assert envvars.state_dir() == "/var/lib/bmad-loop"

    monkeypatch.setenv(envvars.STATE_DIR, "relative/state")
    assert envvars.state_dir() == "relative/state"
