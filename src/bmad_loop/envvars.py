"""Central registry of the ``BMAD_LOOP_*`` runtime environment variables.

These operator/test override knobs used to be read inline at scattered call
sites (`engine`, `adapters.multiplexer`, `cli`, `process_host`), which left them
undiscoverable and undocumented. This module is the one place each is named,
typed, and given a reader; the operator-facing table lives in the README under
"Environment variables".

Each reader preserves its call site's exact semantics — same parse, same
fallback — so routing a site through here changes nothing observable. Only the
core operator/test knobs belong here (the count is deliberately not stated — it
has already changed once); the `BMAD_LOOP_UNITY_*` / `BMAD_LOOP_ENGINE_*` family
read by the bundled Unity plugin's stand-alone helper scripts is that plugin's
own contract (documented in the game-engine guide) and stays with it. Nor do the
session-protocol vars the engine *injects* into a child session
(`BMAD_LOOP_RUN_DIR`, `BMAD_LOOP_TASK_ID`, …): those have a producing side inside
the orchestrator, and the stdlib-only relays that read them back cannot import
this module at all.

Note what the carve-out is and is not: "has a producing side" does not qualify a
name, "is read only by a stdlib-only relay that cannot import this module" does.
Anything core orchestration reads back belongs here, and
`test_portability_guard.test_bmad_loop_env_reads_only_in_the_registry` is the
enforced form of that rule.
"""

from __future__ import annotations

import math
import os

#: Overrides the per-session wall-clock budget, in seconds (test / E2E hook).
SESSION_TIMEOUT_S = "BMAD_LOOP_SESSION_TIMEOUT_S"
#: Forces the terminal-multiplexer backend by registered name.
MUX_BACKEND = "BMAD_LOOP_MUX_BACKEND"
#: Forces the process-host implementation by registered name (test / override).
PROCESS_HOST = "BMAD_LOOP_PROCESS_HOST"
#: Overrides the user-scoped state root that per-run control-plane state lives
#: under (see :func:`runs.state_root`), replacing the whole platform cascade.
STATE_DIR = "BMAD_LOOP_STATE_DIR"


def session_timeout_s() -> float | None:
    """The per-session wall-clock override in seconds, or ``None`` when unset.

    Anything that is not a finite positive number of seconds reads as ``None``
    (ignored), and the guard is deliberately two-sided. Rejecting zero and
    negatives keeps a fat-fingered override from silently *shortening* a real
    run's budget; rejecting non-finite values keeps ``inf`` / ``1e999`` /
    ``Infinity`` — all of which ``float()`` accepts and all of which pass a bare
    ``> 0`` — from silently *removing* it. That second half matters more than it
    looks: both adapters build their monotonic and wall-clock deadlines by
    adding this to the current time, so a non-finite budget yields a deadline
    that can never expire, and this is the outer bound every stall-grace and
    wake-nudge window defers to. Losing it means an unattended run can wedge
    with no backstop left.

    A very large *finite* value is still honoured. It is a duration, however
    unwise, and an operator asking for one is expressing intent; ``inf`` is not
    a duration at all.
    """
    raw = os.environ.get(SESSION_TIMEOUT_S)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def mux_backend() -> str | None:
    """The forced terminal-multiplexer backend name, or ``None`` when unset.

    Returned verbatim (callers test truthiness and resolve the name), so the
    forced-selection semantics match the raw env read exactly.
    """
    return os.environ.get(MUX_BACKEND)


def process_host() -> str | None:
    """The forced process-host name, or ``None`` when unset."""
    return os.environ.get(PROCESS_HOST)


def state_dir() -> str | None:
    """The overriding bmad-loop state root, or ``None`` when unset.

    Verbatim like the two name readers above — :func:`runs.state_root` uses the
    value as the state root itself, so an operator who names a directory gets that
    directory. Silently ignoring a stated override in favour of the platform
    cascade would be the same failure :func:`mux_backend` refuses: a loud
    misconfiguration turned into a quiet auto-select, discoverable only by
    noticing where a run's events did *not* appear.

    Reading verbatim is not the same as accepting anything: this reader reports
    what is set, and :func:`runs.state_root` judges it. A **relative** value is
    refused there rather than resolved, because the root is read by two processes
    with different working directories — see that function for the full reason.
    The split is deliberate; a reader that silently rewrote its variable would
    make the refusal impossible to state.

    The one value not passed through is the empty string, which reads as unset.
    ``export BMAD_LOOP_STATE_DIR=`` is what an unset-looking export leaves behind,
    and it is not a directory an operator can have meant: ``Path("")`` is the
    *current directory*, so honouring it would silently root the control plane at
    whatever cwd the loop happened to be launched from.
    """
    return os.environ.get(STATE_DIR) or None
