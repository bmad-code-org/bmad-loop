"""Post-mortem environment-fault classification, shared across transports (#194).

After a session's verdict and reconcile have settled, a single tail read of the
session log can reclassify a non-completed verdict as an *environment fault* —
the CLI never got usable work out of the provider, so the attempt proved nothing
about the story. The engine routes that to a PAUSE (``env_fault_pause_reason``)
instead of charging a dev attempt, and re-arming resets the budget.

This lives in its own module rather than on one adapter because the signal is
**transport-agnostic**: every adapter that writes a per-task diagnostic log can
be classified from it, whatever produced the bytes. Which file that is per
adapter is named by ``ENV_FAULT_LOG_SUFFIX``: the tmux adapters tee a pane
capture to ``logs/<task_id>.log`` (``mux.pipe_pane``); the opencode HTTP adapter
redirects the ``opencode serve`` process's own stdout/stderr to
``logs/<task_id>.server.out`` — NOT its ``.log``, which is a model-written
conversation transcript. Keeping the classifier attached to a single adapter is
what let #194 ship covering only half the adapters, so the next sibling adapter
inherits this instead of re-omitting it.

Host-class contract: ``self.profile`` (a ``CLIProfile``), ``self.logs_dir``, and
``_note_lifecycle`` (from ``_ResultFileMixin``). Mix in alongside that mixin.

Note the two log flavors differ in how *dirty* they are, which is why the
shipped patterns are anchored the way they are. A tmux pane capture contains the
model's own output — a story that implements rate limiting will print "429" and
"quota" in ordinary healthy work — whereas the opencode server log carries only
server and provider lines. That difference sets the bar per profile, and the two
bars are NOT the same. On a pane capture a pattern must reproduce a COMPLETE
captured CLI error sentence: an error-shaped token plus a cause on the same line
is precisely the shape a story writing ABOUT the error emits, so that weaker rule
classified 44 of this repo's own tracked lines and had to be withdrawn (#507).
The opencode server log tolerates the anchor-plus-cause form only because the
model cannot write to it. Seed either from a captured line, never a plausible
one; the profiles' own comments carry the provenance and
tests/test_env_fault_patterns.py pins both halves.

A pattern match is a claim that the CLI never reached the API, and measured
usage disproves it (#764): a late transient 5xx in the tail of a session that
billed tokens for its whole clock is a real timeout, not an outage. So after a
match the classifier reads the session's usage, and a tracked nonzero total
overrules the verdict — ``env_fault`` stays False, ``env_fault_overruled`` keeps
the evidence, and the session is charged like any other failure. It is the
rationale that already excludes ``over_budget``. Untracked usage (``None``), a
tracked zero, or a fault reading usage keep today's verdict: only a measurement
can overrule.
"""

from __future__ import annotations

import dataclasses
import re
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

import regex

from .base import SessionHandle, SessionResult, SessionSpec
from .profile import CLIProfile

if TYPE_CHECKING:
    from ..model import TokenUsage

# Post-mortem transport-failure classification (#194): how much of the tee'd
# session log's tail to scan, how long an evidence excerpt to keep, and which
# non-completed statuses are eligible. over_budget is excluded — a budget
# crossing proves real API traffic — and completed never reaches the scan.
ENV_FAULT_TAIL_BYTES = 64 * 1024
ENV_FAULT_EVIDENCE_MAX = 240
# How much of the line *before* the match to keep when the line is too long to
# quote whole. Truncating from the start instead loses the error on any log whose
# lines lead with metadata: an opencode `level=ERROR message="stream error" …`
# line carries ~250 characters of timestamp/provider/session fields before
# `error.error=`, so a head-truncated excerpt showed the operator every field
# except the failure. The evidence string is what lands in the pause reason and
# the ATTENTION file — it has to contain the thing that matched.
ENV_FAULT_EVIDENCE_LEAD = 40
ENV_FAULT_STATUSES = frozenset({"timeout", "stalled", "crashed"})
# Wall-clock bound on EACH pattern match (run via the `regex` module, not stdlib
# `re`, whose `search` has no timeout), so a pathological profile regex cannot hang
# run() teardown indefinitely. Note what this does and does not bound: it is
# per-search, so the worst case for a scan is lines × patterns × this, not this.
# A sane pattern over the ≤64 KiB tail matches in microseconds, and the first
# search to blow the bound aborts the whole scan (TimeoutError → decline to
# classify), so the realistic ceiling is one timeout — but an operator writing a
# pattern that backtracks on many lines without exceeding the per-line bound can
# still make teardown slow. Keep patterns anchored and non-nested.
ENV_FAULT_MATCH_TIMEOUT_S = 2.0
# Self-contained ANSI/terminal-control stripper for the log tail: CSI, OSC (BEL-
# or ST-terminated), other two-char ESC sequences, and raw C1 bytes. Deliberately
# NOT the TUI/pyte machinery — the classifier reads raw pane bytes best-effort and
# must not pull a terminal emulator into the adapter. Inert on a log that was
# never a terminal capture (the opencode server log), which costs one pass.
_ANSI_RE = re.compile(
    r"""
    \x1b\[ [0-?]* [ -/]* [@-~]        # CSI ... final byte
    | \x1b\] .*? (?: \x07 | \x1b\\ )   # OSC ... BEL or ST
    | \x1b [@-Z\\-_]                   # 2-char ESC sequences (incl. C1 via ESC)
    | [\x80-\x9f]                      # raw C1 control bytes
    """,
    re.VERBOSE,
)


class EnvFaultMixin:
    """Classify a dead session as an environment fault from its log tail (#194).

    Mixed into every adapter that writes a per-task diagnostic log; which file
    is scanned is named by ``ENV_FAULT_LOG_SUFFIX``. Inert for a profile with no
    ``env_fault_patterns``, so mixing it in is always safe."""

    # Set by the concrete adapter's __init__; bare annotations (no runtime effect)
    # tell the type checker which host attributes this mixin reads.
    profile: CLIProfile
    logs_dir: Path

    # Which per-task file in logs_dir carries the CLI's DIAGNOSTIC output. Default
    # ".log" is the tmux adapters' pane capture. Overridden by any adapter that
    # writes its diagnostics elsewhere.
    #
    # This is a suffix and not a hardcoded path because the two are not the same
    # file for every transport, and getting it wrong is silent: the scan simply
    # finds nothing and every provider outage reads as a failed story. The opencode
    # adapter split them (its ".log" became a curated conversation transcript,
    # diagnostics moved to ".server.out"), and the classifier kept scanning ".log"
    # — passing every unit test, because they write the fixture to whatever path
    # this resolves to, and failing only end to end.
    #
    # It also carries the SAFETY property the shipped patterns depend on. A file
    # containing the model's own output cannot be scanned with content-based
    # patterns: a story that quotes a provider error verbatim is byte-identical to
    # the real thing (see tests/test_env_fault_patterns.py). The tmux adapters
    # accept that risk knowingly and their profile patterns are anchored for it;
    # opencode's are anchored on the assumption of a model-free log, so pointing
    # this at a transcript would make them unsound.
    ENV_FAULT_LOG_SUFFIX = ".log"

    def _env_fault_log_path(self, task_id: str) -> Path:
        return self.logs_dir / f"{task_id}{self.ENV_FAULT_LOG_SUFFIX}"

    if TYPE_CHECKING:
        # Supplied by _ResultFileMixin, which every host of this mixin already
        # uses. Declared rather than inherited: generic.py imports this module,
        # so depending on _ResultFileMixin here would be a cycle.
        def _note_lifecycle(self, task_id: str, event: str, **fields: object) -> None: ...

        # Supplied by CodingCLIAdapter, the base of every host of this mixin.
        def read_usage(self, result: SessionResult) -> TokenUsage | None: ...

    @cached_property
    def _env_fault_patterns(self) -> tuple[regex.Pattern[str], ...]:
        """Compiled once per adapter, on first classification. The profile
        validated each pattern at parse time with this same engine, so
        ``regex.compile`` cannot raise here; empty tuple = classification inert.

        A ``cached_property`` rather than an ``__init__`` line so every adapter
        gains the behavior by mixing the class in, with nothing to remember to
        wire — the omission that caused this bug. Tests may still assign
        ``adapter._env_fault_patterns`` directly; that shadows the property in
        the instance ``__dict__``, as it did when this was a plain attribute.

        CONSTRAINT: the cache is filled on first classification and never
        invalidated, so reassigning ``self.profile`` afterwards leaves stale
        patterns. Nothing in ``src/`` mutates ``profile`` after ``__init__`` (it
        is constructor state), so this holds in production; a test that swaps the
        profile must do so BEFORE the first classification, or assign
        ``_env_fault_patterns`` directly instead."""
        return tuple(regex.compile(p) for p in self.profile.env_fault_patterns)

    def _classify_env_fault(
        self, handle: SessionHandle, spec: SessionSpec, result: SessionResult
    ) -> SessionResult:
        """Post-mortem transport-failure classification (#194).

        Runs last in ``run()`` (after ``_post_kill_reconcile``): only a
        non-completed verdict (``result.status`` in ``ENV_FAULT_STATUSES``,
        ``result_json is None``) with configured patterns is inspected, so a
        reconcile upgrade to ``completed`` is never re-classified and adapters
        without patterns stay inert. On a matching log-tail line, stamp
        ``env_fault`` / ``env_fault_evidence`` and drop an ``env-fault-classified``
        lifecycle breadcrumb. No match, no patterns, or an unreadable log leaves
        the verdict untouched — best-effort, like ``_write_heartbeat``.

        A match is overruled when the session's tracked usage is nonzero (#764):
        ``env_fault`` stays False, ``env_fault_overruled`` carries the evidence,
        and an ``env-fault-overruled`` breadcrumb (with ``tokens``) replaces the
        classified one. Usage is read only after a match, so a healthy log costs
        no extra read."""
        if (
            not self._env_fault_patterns
            or result.status not in ENV_FAULT_STATUSES
            or result.result_json is not None
        ):
            return result
        evidence = self._env_fault_evidence(handle.task_id)
        if evidence is None:
            return result
        tokens = self._overruling_usage_total(result)
        if tokens is not None:
            self._note_lifecycle(
                handle.task_id,
                "env-fault-overruled",
                status=result.status,
                evidence=evidence,
                tokens=tokens,
            )
            return dataclasses.replace(result, env_fault_overruled=evidence)
        self._note_lifecycle(
            handle.task_id, "env-fault-classified", status=result.status, evidence=evidence
        )
        return dataclasses.replace(result, env_fault=True, env_fault_evidence=evidence)

    def _overruling_usage_total(self, result: SessionResult) -> int | None:
        """The session's tracked token total when it is nonzero, else None (#764).

        None covers untracked usage, a tracked zero and a ``read_usage`` fault,
        all of which keep the env-fault verdict. A fault is swallowed exactly as
        ``_classify_zero_token_timeout`` swallows it — usage is metadata, never a
        gate, and an escape would unwind ``run()`` before the engine records the
        session; the engine's own ``read_usage`` right after ``run()`` re-raises
        it on its journaled path."""
        try:
            usage = self.read_usage(result)
        except Exception:  # usage is metadata, never a gate; the engine re-raises it
            return None
        if usage is None or usage.total <= 0:
            return None
        return usage.total

    def _env_fault_evidence(self, task_id: str) -> str | None:
        """Scan the tail of the tee'd session log for a transport-failure pattern.

        Reads the last ``ENV_FAULT_TAIL_BYTES`` (binary, decoded with
        ``errors="replace"``, ``\\r``→``\\n``), strips ANSI, and matches each line
        against the precompiled patterns under a per-match ``ENV_FAULT_MATCH_TIMEOUT_S``
        bound. Returns the ANSI-stripped matching line (last match winning, windowed
        to ``ENV_FAULT_EVIDENCE_MAX`` around the match), or None when nothing matches,
        the log can't be read (any ``OSError``), or a pattern exceeds the match timeout
        (``TimeoutError``) — no classification, the best-effort doctrine.

        The last two Nones are folds, not answers: the scan never looked, so an
        outage in that log reads as an ordinary failure and spends a dev attempt.
        Each drops an ``env-fault-scan-failed`` lifecycle crumb (``stage`` =
        ``read`` or ``match``, ``log``, ``error``; ``match`` adds ``pattern``) so
        the post-mortem can tell "no provider error" from "never checked" (DW-460).
        A MISSING log is a read fault too, not an answer: both hosts create it
        before the session starts (the pane tee pre-touched, ``.server.out`` at
        spawn), so its absence here means it went away."""
        log_path = self._env_fault_log_path(task_id)
        try:
            with log_path.open("rb") as fh:
                fh.seek(0, 2)  # SEEK_END
                size = fh.tell()
                offset = max(0, size - ENV_FAULT_TAIL_BYTES)
                if offset:
                    # The byte immediately BEFORE the window, read on its own so it
                    # enters neither `raw` nor the ENV_FAULT_TAIL_BYTES budget — it
                    # only answers whether the window opens mid-line. Inspected raw,
                    # ahead of the \r→\n normalization below: a pane capture is
                    # CR-terminated, so a \r here ends a line just as a \n does.
                    fh.seek(offset - 1)
                    boundary = fh.read(1)
                else:
                    fh.seek(0)
                    boundary = b"\n"  # not truncated: the window is the whole file
                raw = fh.read()
        except OSError as exc:
            self._note_scan_failed(task_id, "read", log_path, exc)
            return None
        straddles = boundary not in (b"\n", b"\r")
        text = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace").replace("\r", "\n"))
        lines = text.split("\n")
        if straddles and any(lines[1:]):
            # The window opened mid-line, so the first element is a fragment of
            # whatever line straddled the edge — not a line. Matching it would quote
            # a half-line as evidence, and (because the cut can land mid-codepoint)
            # its head may be a U+FFFD from the errors="replace" decode. Drop it:
            # one lost line at the far end of a 64 KiB tail cannot matter, and a
            # fabricated line can.
            #
            # Both halves of the guard are load-bearing, and each was once wrong:
            # * `straddles`, not "the read truncated": an offset that happens to
            #   land on the first byte after a newline yields a COMPLETE first line.
            #   Discarding that on truncation alone loses a whole line, and if it
            #   was the only provider-error match in the tail the outage goes
            #   unclassified and the run burns a story attempt for nothing.
            # * `any(lines[1:])`, not `len(lines) > 1`: the guard means "do not
            #   discard if that leaves nothing to scan", and the length test does
            #   not say that. `split("\n")` always yields a trailing "", so a window
            #   holding one >64 KiB newline-terminated line splits to
            #   [fragment, ""] — length 2, dropped, leaving only "". Degenerate,
            #   but silently classifying nothing is exactly the failure this
            #   classifier exists to prevent.
            lines = lines[1:]
        match_line: str | None = None
        match_pos = 0
        for line in lines:
            for pat in self._env_fault_patterns:
                try:
                    hit = pat.search(line, timeout=ENV_FAULT_MATCH_TIMEOUT_S)
                except TimeoutError as exc:
                    # runaway pattern → decline to classify (best-effort, like OSError)
                    self._note_scan_failed(task_id, "match", log_path, exc, pattern=pat.pattern)
                    return None
                if hit is not None:
                    match_line, match_pos = line, hit.start()  # last match wins
                    break
        if match_line is None:
            return None
        return _excerpt(match_line, match_pos)

    def _note_scan_failed(
        self, task_id: str, stage: str, log_path: Path, exc: BaseException, **fields: object
    ) -> None:
        # Through the host's _note_lifecycle, the channel env-fault-classified
        # already takes — every in-tree host (GenericAdapter, OpencodeHttpAdapter
        # and their Dev subclasses) gets it from _ResultFileMixin, whose writer
        # swallows its own I/O faults, so the crumb cannot turn a declined
        # classification into a raise out of run() teardown.
        self._note_lifecycle(
            task_id,
            "env-fault-scan-failed",
            stage=stage,
            log=str(log_path),
            error=f"{type(exc).__name__}: {exc}",
            **fields,
        )


def _excerpt(line: str, match_pos: int) -> str:
    """Quote at most ``ENV_FAULT_EVIDENCE_MAX`` characters of ``line``, keeping the
    matched region rather than the line's head. Short lines are returned whole.

    BOTH cut ends are marked with an ellipsis. Marking only the head (as this did
    at first) makes a window that dropped a suffix read as a complete line that
    simply ended there — which, for a string an operator reads out of a pause
    reason to decide whether their run died of a provider outage, is the one thing
    it must not do."""
    stripped = line.strip()
    if len(stripped) <= ENV_FAULT_EVIDENCE_MAX:
        return stripped
    usable = len(line.rstrip())
    # The markers are spent FROM the budget, never added on top of it: this string
    # is journalled and rendered in a pause reason, so ENV_FAULT_EVIDENCE_MAX has
    # to be a real ceiling on what callers receive. But how many markers there are
    # depends on the window, and the window depends on the budget, which depends on
    # the markers. Resolve by fixed point over the only three possibilities — no
    # marker, one, or two — taking the first that is self-consistent. Terminates in
    # at most three passes; the final fallback (two markers) is always valid
    # because a line long enough to reach here cannot need fewer than it reserves.
    start, head, tail, budget = 0, False, False, ENV_FAULT_EVIDENCE_MAX
    for reserved in (0, 1, 2):
        budget = ENV_FAULT_EVIDENCE_MAX - reserved
        # Prefer the match with LEAD chars of left context, but slide LEFT rather
        # than return a short excerpt when the match sits near the end of the line.
        # That is the common case here, not a corner: opencode's logfmt puts ~250
        # characters of metadata before `error.error=`, so the match is near the
        # end of almost every line this classifier quotes.
        start = max(0, min(match_pos - ENV_FAULT_EVIDENCE_LEAD, usable - budget))
        head = start > 0
        tail = start + budget < usable
        if (1 if head else 0) + (1 if tail else 0) == reserved:
            break
    window = line[start : start + budget].strip()
    if head:
        window = f"…{window}"
    if tail:
        window = f"{window}…"
    return window
