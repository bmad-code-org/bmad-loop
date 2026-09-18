"""Deterministic reading and editing of the deferred-work ledger.

The ledger (`{implementation_artifacts}/deferred-work.md`) is append-only
markdown in the canonical form documented at
bmad-loop-sweep/deferred-work-format.md: `### DW-<seq>: <title>` headings with
`origin:`/`location:`/`reason:`/`status:` field lines. The one sanctioned
rewrite is :func:`archive_closed`, which moves closed entries verbatim to a
sibling archive file and leaves id-preserving stubs. Pre-#2651 dev primitives
and the attended `bmad-build` append flatter entries here directly, which the
orchestrator normalizes on sweep; the current unattended primitive records its
findings in the spec's frontmatter instead and the engine harvests them into
canonical entries. The
orchestrator never trusts an LLM to have edited it — status flips and decision
records happen here, and gates re-read the file from disk.

Concurrency (#286/#469): every mutator below is a read->edit->write of the whole
file, so two orchestrator processes — a second `bmad-loop run`, a run plus a
sweep, a run plus the TUI decision modal, a run plus `sweep --archive` — would
otherwise both read, both edit, and let the last atomic write win. Each leaf
mutator therefore runs its whole read->edit->write under :func:`ledger_lock`, a
cross-process mutex on an out-of-repo sidecar. Readers stay lock-free on
purpose: every writer replaces the file atomically, so a reader already sees one
whole version or another, and taking the lock to read would buy nothing while
adding a way to deadlock. Out of scope by #286's own non-goals: the dev/review
LLM session writes this file directly and does NOT take the lock — orchestrator
writes are sequenced against sessions today, so the exposure this closes is
orchestrator-vs-orchestrator.

What the hold covers is every read that decides the PUBLISHED BYTES, which is
not quite every read (#736). A mutator handed work that turns out to be a no-op
— ids that are all already done, a decision on an entry that is not there,
specs that all dedupe, nothing eligible to archive — may answer from ONE
advisory read taken before the lock, running the same pure decision helper the
locked pass runs so the two cannot drift. Only a "would write nothing" answer
is acted on, and such a call linearizes at the probe read: it publishes no
bytes, so there is nothing for a rival to interleave with. Every other answer,
and any fault during the probe, falls through to the hold, which re-reads and
decides authoritatively. This is what keeps a no-op from failing on a lock it
never needed — an `OSError` from acquisition, or a
:class:`~bmad_loop.runs.StateRootError` from deriving the sidecar path where no
state root exists — which a replayed rollback, a re-run sweep and
``sweep --archive`` all reach routinely.

Ledger-read contract (DW-146). Every deferred-work ledger read in
``src/bmad_loop`` belongs to one of exactly three cases, and the reader's NAME is
the classification recorded at the site:

* REPAIR/WRITE — :func:`read_for_write`. The text decides published bytes: it is
  edited and written back, or it becomes an artifact a session is dispatched on.
  Absence answers ``None``; OS metadata and text-read faults raise
  :class:`LedgerReadFault`, a :class:`LedgerReadError` subclass (DW-279), with
  the original ``OSError`` chained as ``__cause__``. Undecodable bytes still
  raise :class:`LedgerReadError` with the ``UnicodeDecodeError`` chained.
  A repair write must never proceed from bytes nobody could read. The probe
  uses ``Path.stat`` + ``S_ISREG`` (DW-221) so a refused metadata call cannot
  silently become absence on Python 3.14. Pre-lock presence probes, lock
  acquisition and writes retain their raw ``OSError`` behavior.
* OBSERVATION — :func:`read_for_observation`. The text informs a report, a hint,
  or a flag, and nothing is written from it. Absence answers ``("", None)``; both
  ``OSError`` and ``UnicodeDecodeError`` degrade to ``("", "<Class>: <msg>")`` so
  the caller can journal the attributed fault and carry on. It never raises.
  :func:`observe_ledger` is the same arm with presence kept: ``None`` text for
  absence, ``""`` for a present 0-byte ledger — for the sites whose sentence
  says whether the file is there rather than what it holds.
  The helper never raising does not oblige its CALLER to stay quiet: the arm is
  about who WRITES, not about how loud the response is. An observation caller may
  legitimately answer louder than a silent degrade — ``cli._sweep_dry_run`` fails
  the command rather than print a fabricated listing,
  ``cli._validate_deferred_ledger`` grades a problem, and
  ``Engine._refuse_gated_story`` notifies the human and refuses the story — and
  three such sites keep their read inline for exactly that reason.
* ADVISORY — the pre-lock probes above, which keep their ``except Exception`` and
  decide nothing. Neither arm: the locked read below each one is the repair/write
  site, and a fault in a probe simply falls through to it.

THE DISCRIMINATOR, for a read that is not obviously one or the other: the arm is
decided by whose text THIS site edits and publishes — never by whether a write
happens downstream of it. A read whose bytes this site mutates and writes back
(or hands to a session as an artifact) is REPAIR/WRITE. A read that only
classifies, reports, or NARROWS a later write performed by a locked mutator that
re-reads the ledger for itself is OBSERVATION. That is the only property a site
can be held to locally: a data-flow rule ("a write follows, therefore
repair/write") would reclassify every read that feeds any mutator, including the
reads the mutator's own locked :func:`read_for_write` already covers, and would
leave no read anywhere in the tree on the observation arm.

The worked example, because it is the one that looks like a counterexample:
``Engine._close_declared_deferred`` reads the ledger, runs :func:`classify` over
it, and arms the commit's rollback set from the result — and it is OBSERVATION.
It never edits or publishes that text. The close itself is
:func:`mark_done_many_reopenable`, whose own locked :func:`read_for_write` is the
repair/write read for it, and which deliberately never re-uses the caller's
snapshot. So a degraded read there journals ``deferred-close-ledger-unavailable``,
writes nothing, and leaves the entries ``open`` — it must NOT raise
:class:`LedgerReadError`, because nothing at that site was about to be published.

DW-146 originally retyped only the undecodable-bytes case, deliberately.
``UnicodeDecodeError`` is a ``ValueError``, so a handler spelled ``except
OSError`` never caught it. Two sites were spelled that way — ``verify``'s
``verify_review_bundle`` and ``tui.data.deferred_entries`` — and both had a
degrade arm sitting right there (a retryable outcome, an unavailable pane) that
undecodable bytes flew straight past; each now catches both. The other two
inline observation sites, ``Engine._refuse_gated_story`` and
``cli._validate_deferred_ledger``, already caught the pair and were unchanged by
DW-146; since DW-266/267 their presence probe is ``stat`` + ``S_ISREG`` inside
that same ``try`` (as is ``verify_review_bundle``'s), so on Python 3.14 the pair
is reached for a refused probe too, where ``is_file()`` had answered False and
read the refusal as an empty ledger. All three now spell the tuple ``(OSError,
ValueError)``: ``Path.stat`` raises a plain ``ValueError`` for an embedded NUL
in the configured path and a ``UnicodeEncodeError`` for a lone surrogate — the
faults :func:`probe_absence` classifies as absence for the WRITE arm — and an
observation arm attributes them as a fault instead, which is where
``UnicodeDecodeError`` alone had let them escape the ``try`` uncaught.
DW-146 left repair/write ``OSError`` propagation untouched; DW-279 now wraps
metadata and text-read faults as :class:`LedgerReadFault`, so existing locked
read-refusal handlers can distinguish them from lock and write failures. (DW-221 later made that
propagation actually HAPPEN on Python 3.14, where the ``is_file()`` probe had
been reporting a refusal as absence. Eleven call sites that read the reader's
answer directly — ``append_entries_published``'s preimage read and
``archive_closed``'s two, five in ``sweep``, four in ``engine`` — do newly see a
raise there, the conservative direction at each and the point of the fix rather
than a side effect of it. Five mutators — ``_mark_done_many``,
``mark_seen_again_many``, ``mark_open_many``, ``record_decision`` and
``archive_closed`` — were NOT fixed by it: each carried its own bare ``if not
path.is_file()`` guard both before and under :func:`ledger_lock`, directly above
an ``or ""`` read, so on 3.14 those guards answered False for a refused ledger
and returned the mutator's no-op value before :func:`read_for_write` was ever
reached — ``sweep --archive`` reported success having archived nothing, and
``record_decision`` answered "no such entry" for a ledger that was right there.
DW-255 closed that: the pre-lock guard is :func:`_ledger_present`, which shares
the reader's ``stat`` + ``S_ISREG`` classification and PROPAGATES every
``OSError`` that is not absence, and the under-lock guard is the reader's own
``None`` answer, so a refused ledger raises out of every mutator on every
interpreter and an absent one still takes the no-op return without a lock
(#736). DW-279 preserves this absence classification and the pre-lock raw
``OSError``, wrapping only the authoritative reader's OS faults.)
:class:`LedgerReadError` derives from ``Exception`` rather than ``OSError`` or
``ValueError`` so that neither those two widened handlers nor any future
``except OSError`` silently swallows the one fault this contract exists to
attribute. Consumers that distinguish OS refusal from decode failure must
handle its :class:`LedgerReadFault` subclass before the parent class.

The WRITE half of every mutator is typed too. Each locked read->edit->write ends in
:func:`_publish`, and an ``OSError`` raised there — ``ENOSPC``, ``EROFS``, a failed
fsync or rename, a permission the atomic writer's temp file lacks — leaves as
:class:`LedgerWriteError`. It IS an ``OSError`` (a subclass), so every caller that
already degrades on ``OSError`` is unchanged: the CLI and the TUI decision modal
keep answering the human rather than dying. What the subclass buys is the split a
caller that WANTS to fail loud on a lost publish needs, and cannot make from the
outside: before it, a failed lock (``EDEADLK``, a contended sidecar) and a failed
write reached a handler as the same bare ``OSError``, and ``sweep._close_resolved``
/ ``sweep._decisions_phase`` — which degrade the lock and read faults DW-166 named
so a bookkeeping phase cannot crash a sweep — swallowed the write fault with them,
reporting a phase that closed nothing where a repair write had failed. Both now
re-raise this type ahead of that arm. The rule they follow is the one in
``AGENTS.md``: observation may degrade, repair writes must raise.

Its sibling is :class:`LedgerLockReleaseError`, for the fault on the far side of
the publish: the body of :func:`ledger_lock` completed and the lock's release
then raised. The bytes ARE on disk there, so a degrade arm that read the bare
``OSError`` as "nothing was written" reported a landed close or decision as one
that never happened. Same ``OSError``-subclass reasoning, same two re-raise
sites.
"""

from __future__ import annotations

import hashlib
import re
import sys
import threading
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date as calendar_date
from pathlib import Path
from stat import S_ISREG

from . import sprintstatus
from .fences import fenced_spans
from .platform_util import atomic_write_text, file_lock, neutralize_surrogates


class LedgerReadError(Exception):
    """A repair/write site could not read the deferred-work ledger.

    A plain ``Exception`` on purpose (DW-146): an ``OSError`` or ``ValueError``
    subclass would be swallowed by the very ``except`` arms this fault escaped.
    """


class LedgerReadFault(LedgerReadError):
    """The OS refused a repair/write read; the original OSError is chained.

    Kept separate from lock and publication failures, which remain raw OSError.
    """


ABSENCE_WINERRORS: tuple[int, ...] = (21, 123, 1921)
"""Windows error codes a metadata probe reports for a target that is as good as
absent — ``ERROR_NOT_READY`` (a disconnected mapped drive), ``ERROR_INVALID_NAME``
(a lexically invalid path) and ``ERROR_CANT_RESOLVE_FILENAME``. This is EXACTLY
pathlib's ``_IGNORED_WINERRORS`` (CPython ``pathlib._abc`` on 3.13, ``pathlib`` on
3.11–3.12), the set ``Path.is_file()`` absorbed before DW-221 replaced it with
``stat()`` + ``S_ISREG`` at the ledger's readers. ``install._ABSENCE_WINERRORS``
is the identical tuple, deliberately NOT shared across the layering: the errno
half of that classification differs (``install._ABSENCE_ERRNOS`` also absorbs
``EBADF``/``ELOOP``, which stay faults here), so the two stay independent."""


def probe_absence(exc: BaseException) -> bool:
    """Does this fault out of a ``stat()``/``lstat()`` PROBE mean absence?

    The ONE classification behind :func:`read_for_write`, :func:`_ledger_present`
    and both legs of ``verify.unpublishable_target`` (DW-256/DW-268), so the
    absorbed set cannot drift between the reader and the guard. True for:

    - ``FileNotFoundError`` / ``NotADirectoryError`` — ``ENOENT``/``ENOTDIR``,
      the absence DW-221 kept;
    - an ``OSError`` whose ``winerror`` is in :data:`ABSENCE_WINERRORS` — the
      test is on ``.winerror`` alone and the subclass is irrelevant (CPython's
      errmap makes 21 a ``PermissionError`` and 123/1921 ``EINVAL``), which is
      why this takes the exception rather than being a class tuple: no tuple
      can select it;
    - any ``ValueError`` except ``UnicodeDecodeError`` — ``Path.stat`` raises a
      plain ``ValueError`` for an embedded NUL and a ``UnicodeEncodeError`` for
      a lone surrogate, both paths the OS cannot encode, which ``is_file()``
      answered False for on every interpreter.

    Everything else stays a FAULT: ``EACCES``, ``EIO``, ``ESTALE``, ``EBADF``,
    ``ELOOP`` and an ``OSError`` carrying any other ``winerror`` (5, say) — DW-221's
    deliberate call for ``EBADF``/``ELOOP``, unchanged. PROBE faults only: a
    ``UnicodeDecodeError`` out of ``read_text`` is itself a ``ValueError`` and
    is the ONE ``ValueError`` deliberately NOT absence, so the write arm's
    decode arm stays :class:`LedgerReadError`. Classification, not disposition — the observation
    arm and ``decisions.load_pre_answers`` never call this: they attribute or
    degrade the same ``ValueError`` as a fault, which is their contract.
    """
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return True
    if isinstance(exc, OSError):
        return getattr(exc, "winerror", None) in ABSENCE_WINERRORS
    return isinstance(exc, ValueError) and not isinstance(exc, UnicodeDecodeError)


class LedgerWriteError(OSError):
    """A mutator's atomic publish of the deferred-work ledger failed.

    An ``OSError`` SUBCLASS on purpose, the opposite choice from
    :class:`LedgerReadError`: the read fault had to escape the ``except OSError``
    arms it kept flying past, where this one must keep being CAUGHT by every
    caller that already degrades a ledger ``OSError`` (``cli.cmd_decisions``,
    ``tui.app._record_decision``, ``decisions.apply_pre_answer``'s callers). The
    subclass exists for the callers that must NOT — a sweep phase whose degrade
    arm was written for lock and decode faults, and which a lost publish reached
    looking exactly like a lost lock. Raised only by :func:`_publish`, with the
    original ``OSError`` chained as ``__cause__``.
    """


class LedgerLockReleaseError(OSError):
    """:func:`ledger_lock` published its body and then could not RELEASE the lock.

    The publish landed — that is the whole point of the type. Acquisition faults
    are already typed (:class:`~bmad_loop.platform_util.LockUnavailableError`),
    and a fault on the way OUT — Windows' ``msvcrt.locking(LK_UNLCK)`` or the
    ``lseek`` ahead of it, ``os.close`` on any platform — reached callers as the
    same bare ``OSError`` a failed ``os.open`` does, with the bytes already on
    disk. A sweep degrade arm reading that as "nothing was written" then reported
    a landed close or decision as one that never happened. An ``OSError`` subclass
    for the same reason :class:`LedgerWriteError` is: every ``except OSError``
    caller is unchanged, and the one that must fail loud can name it. Raised only
    when the BODY completed — a body that raised keeps its own exception even
    when the release faults on top of it (the release fault is chained as
    ``__context__`` and added as a note), so a :class:`LedgerWriteError` is never
    downgraded to a bare ``OSError`` by the cleanup that followed it.
    """


def _publish(path: Path, text: str) -> None:
    """The one write every ledger mutator ends in: :func:`atomic_write_text`,
    retyped. The atomic writer's failure modes — the temp file, its fsync, the
    replace — all surface as ``OSError``; each leaves here as
    :class:`LedgerWriteError` so a caller can tell a publish that failed from a
    lock it never got. Anything that is not an ``OSError`` (the strict encoder's
    ``UnicodeEncodeError``, a ``ValueError`` precondition) is not a publish fault
    and passes through untouched.
    """
    try:
        atomic_write_text(path, text)
    except OSError as e:
        raise LedgerWriteError(f"could not publish {path}: {type(e).__name__}: {e}") from e


def read_for_write(path: Path) -> str | None:
    """REPAIR/WRITE arm of the ledger-read contract (DW-146).

    Absence answers ``None`` — callers that treat an absent ledger like an empty
    one spell that ``or ""`` at the site, which is exact because ``open_ids("")``
    and ``parse_ledger("")`` already answer identically for both. Nonabsence OS
    metadata faults and all text-read ``OSError`` failures become
    :class:`LedgerReadFault` with the original exception as ``__cause__``
    (DW-279). This includes disappearance or type-change races after the probe.
    Undecodable bytes remain :class:`LedgerReadError` with a
    ``UnicodeDecodeError`` cause. A refused read publishes nothing; pre-lock
    probes, lock acquisition and writes retain their raw ``OSError`` behavior.

    Use ``Path.stat`` plus ``S_ISREG`` because ``Path.is_file`` suppresses all
    OS errors on Python 3.14. What the probe's fault MEANS is decided by
    :func:`probe_absence`, the classification shared with :func:`_ledger_present`
    and ``verify.unpublishable_target`` (DW-256/DW-268): ENOENT, ENOTDIR,
    non-regular files, pathlib's ignored winerrors (:data:`ABSENCE_WINERRORS` —
    21/123/1921, CPython ``pathlib._abc`` on 3.13, ``pathlib`` on 3.11–3.12) and
    the ``ValueError`` a non-encodable path raises are absence — the set
    ``is_file()`` absorbed before DW-221 — and every other OS error is wrapped.
    ELOOP and EBADF deliberately become errors on older interpreters too, where
    ``is_file`` suppressed them. The regular-file check also prevents blocking
    on a FIFO.

    ``resolve.build_context`` shares the errno/``S_ISREG`` half of this
    classification (``ENOENT``/``ENOTDIR``/non-regular are absence there too)
    and keeps the narrow tuple: the winerror/``ValueError`` widening is the
    ledger readers' own. Its disposition differs as well: that observation path
    degrades other OS errors, whereas this repair/write reader raises
    :class:`LedgerReadFault` from them.
    """
    try:
        st = path.stat()
    except (OSError, ValueError) as e:
        if probe_absence(e):
            return None
        if isinstance(e, OSError):
            raise LedgerReadFault(f"{path} could not be read ({type(e).__name__}: {e})") from e
        raise
    if not S_ISREG(st.st_mode):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise LedgerReadFault(f"{path} could not be read ({type(e).__name__}: {e})") from e
    except UnicodeDecodeError as e:
        raise LedgerReadError(f"{path} is not valid UTF-8: {e}") from e


def observe_ledger(path: Path) -> tuple[str | None, str | None]:
    """OBSERVATION arm of the ledger-read contract (DW-146), presence-aware.

    Returns ``(text, fault)``. Absence is not a fault: it answers ``(None, None)``.
    A present, readable ledger answers its text — ``""`` for a 0-byte file, which
    is a ledger that EXISTS and holds no entry, not a missing one. Both ``OSError``
    and ``UnicodeDecodeError`` degrade to ``(None, "<Class>: <msg>")`` —
    attributed, so a caller holding a journal can record WHICH fault it degraded
    on rather than reporting an empty ledger. Never raises.

    This is the reader for a site whose SENTENCE turns on presence — "the ledger
    file is gone" against "the ledger holds no entry for this id" — because
    :func:`read_for_observation` folds absence and a 0-byte file into the one
    empty text, and testing that text's truthiness reported a present, empty
    ledger as gone (PR #794 review). Presence is taken from the same ``stat`` the
    read is gated on, never from a second probe beside it: two probes could
    disagree across a rival's unlink, and a second ``is_file()`` would bring back
    the Python 3.14 suppression DW-254 retired.

    Both arms share the ``ENOENT``/``ENOTDIR``/non-regular classification and
    differ in DISPOSITION. The probe is ``Path.stat`` + ``S_ISREG``, the same as
    :func:`read_for_write`'s (DW-221 there, DW-254 here):
    ``FileNotFoundError``/``NotADirectoryError`` and a present non-regular file
    are absence at both. Since DW-256 the write arm ADDITIONALLY absorbs
    :func:`probe_absence`'s wider set — pathlib's ignored winerrors and a
    non-encodable path — as absence; this arm keeps attributing those as faults,
    and every other ``OSError`` is a fault at both. The write arm RAISES its
    faults; this arm attributes and degrades them.
    Until DW-254 this arm probed with ``is_file()``, which raises ``EACCES`` on
    Python 3.11–3.13 (attributed here, as promised) but suppresses every OS
    error on 3.14 and answers False — so a refused ledger degraded to
    ``("", None)`` there, silent absence where the contract promised an
    attributed fault. The ``stat`` probe suppresses nothing on any interpreter,
    so the degrade sentence above is now true everywhere, and it sits INSIDE
    the ``try`` so a fault out of the probe itself lands in the same arm as a
    fault out of the read. ELOOP and EBADF change sides here exactly as they
    did at the write arm: a symlink cycle at the ledger's name is a path that
    EXISTS and cannot be read, so it is an attributed ``OSError:`` fault now,
    where ``is_file()`` absorbed errno 40 (and EBADF) and called it a clean
    empty read.
    """
    try:
        try:
            st = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            return None, None
        if not S_ISREG(st.st_mode):
            return None, None
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        # `ValueError`: `Path.stat` raises it for a path the OS cannot encode (an
        # embedded NUL). `probe_absence` ABSORBS that class as absence at the
        # write arm (DW-256); this arm never calls it — never-raises keeps the
        # `ValueError` an attributed fault rather than classifying it as absence.
        return None, f"{e.__class__.__name__}: {e}"


def read_for_observation(path: Path) -> tuple[str, str | None]:
    """OBSERVATION arm of the ledger-read contract (DW-146).

    Returns ``(text, fault)``. Absence is not a fault: it answers ``("", None)``,
    the empty text every observation site already read for a missing ledger. Both
    ``OSError`` and ``UnicodeDecodeError`` degrade to ``("", "<Class>: <msg>")``
    — attributed, so a caller holding a journal can record WHICH fault it
    degraded on rather than reporting an empty ledger. Never raises.

    The text-only projection of :func:`observe_ledger`: a 0-byte ledger and a
    missing one answer the same ``""`` here, which is right for every site that
    parses the text (nothing to parse either way) and wrong for a site whose
    sentence says whether the FILE is there — those call :func:`observe_ledger`.
    """
    text, fault = observe_ledger(path)
    return text or "", fault


def _ledger_present(path: Path) -> bool:
    """Pre-lock presence guard shared by the five write-bearing mutators (DW-255).

    True for a regular file at ``path``; False for absence — whatever
    :func:`probe_absence` says the probe's fault means (``ENOENT``, ``ENOTDIR``,
    pathlib's ignored winerrors, a non-encodable path), or a present non-regular
    file (a directory, a FIFO) — which is exactly the classification
    :func:`read_for_write` answers ``None`` for. Every other ``OSError``
    PROPAGATES: a refused ledger is a fault the caller must see, never "nothing
    to do".

    ``Path.is_file()`` was the wrong probe here for the reason it was the wrong
    probe in the reader: on Python 3.14 its body is ``os.path.isfile``, which
    suppresses every OS error and answers False, so an EACCES ledger took each
    mutator's no-op return — ``sweep --archive`` reported success having archived
    nothing, ``record_decision`` answered "no such entry" — and the DW-221-fixed
    reader under the lock was never reached. ``stat`` reports the errno on every
    interpreter. ELOOP and EBADF change sides on 3.11–3.13 too, the same call
    DW-221 made: a path that exists and cannot be read is not an absent one.

    Only the PRE-LOCK guard uses this; under the lock each mutator branches on
    the reader's own ``None``. The classification is SHARED with the reader by
    construction — both ask :func:`probe_absence` (DW-256/DW-268) — so the guard
    cannot drift from the reader: a ``ValueError`` for a non-encodable path and
    pathlib's Windows-only absorbed winerrors (21/123/1921) are absence here
    exactly as they are at :func:`read_for_write`. Until DW-256 this helper
    carried its own copy of the narrow tuple and both raised out of it.
    """
    try:
        st = path.stat()
    except (OSError, ValueError) as e:
        if probe_absence(e):
            return False
        raise
    return S_ISREG(st.st_mode)


HEADING_RE = re.compile(r"^### (DW-\d+): (.+?)\s*$", re.MULTILINE)
# Where a canonical entry ENDS, in every shape CommonMark spells an ATX heading:
# up to three spaces of indent, a space OR a tab after the hashes, and an empty
# heading (`##` alone — the separator may be the end of the line). A fourth space
# of indent is an indented code block rather than a heading, so those lines
# deliberately keep absorbing, and the indent class is spaces only for that same
# reason — a leading tab is four columns, so `\t## Notes` is a code block too.
# Read as permissively as the syntax is, because a missed boundary here does not
# merely lose a section header: the span runs on and the next section's
# `status:`/`gate:` lines are read as this entry's, so an open entry that never
# declared a gate takes a story hostage and the operator finds no gate in the
# entry the refusal names (#516). Where a miss would WRITE, the strict
# column-zero reading is the right one (`devcontract`'s destructive edits pin it
# deliberately); this only decides how far a read reaches.
# A lookahead rather than a consuming group: `parse_ledger` reads `.start()`, and
# holding the match to the opener leaves nothing free to grow a dependency on
# where the separator ended.
ANY_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?=[ \t]|$)", re.MULTILINE)
# The flat appender's opening line, in the two forms this module needs it: as a
# bullet in the raw ledger (FLAT_ENTRY_RE, the canonical-span boundary in
# parse_ledger) and as bullet *content* after `_BULLET_RE` has stripped the
# marker (`_FLAT_SOURCE_RE`, legacy section). One shape, two anchors — they have
# to agree, or a block the legacy parser recognizes stays invisible to it (#304).
# Keyed on the opening line alone, deliberately: also requiring the block's
# `summary:`/`evidence:` lines would narrow the boundary below the parser's own
# recognition, leaving the bug in place for every partial shape it accepts.
_FLAT_SOURCE_BODY = r"source_spec:[ \t]"
FLAT_ENTRY_RE = re.compile(rf"^[-*][ \t]+{_FLAT_SOURCE_BODY}", re.IGNORECASE | re.MULTILINE)
STATUS_RE = re.compile(r"^status:[ \t]*(.*)$", re.MULTILINE)
# The mechanical half of a hard gate. An entry could always *say* it blocked a
# story — `HARD GATE: must land before 3-2` in the reason line — and saying it
# stopped nothing: the queue picked the story up anyway, and the gate surfaced
# afterwards in the diff of work built on a leg nobody had wired. `gate:` names
# the blocked story keys in a form a check can match, so the claim can refuse.
# Parsed exactly like `status:`: a field line, read inside `parse_ledger`'s
# canonical span, so a line under a flat-append bullet belongs to that block and
# not to the entry above it.
GATE_RE = re.compile(r"^gate:[ \t]*(.*)$", re.MULTILINE)
# A story key as either queue spells one: a sprint key (`3-2-invite-link`), the
# stories-mode id it starts with (`3-2`), or a bare slug. Whitespace and
# separators are deliberately out — a token nothing can match is the same silent
# no-op the field exists to end, so it is surfaced rather than dropped.
GATE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# The second half of "can this token gate anything", and a different miss from the
# one above: `GATE_TOKEN_RE` rejects the spellings a *line* cannot carry (a space,
# a bare separator), this rejects the ones no *key* can carry. `gate: 3.2` passes
# the first and can never match `gates_story` against any legal key, so it used to
# report a green `ok` while gating nothing — the field's own silent no-op, one
# keystroke away from the shape that works.
#
# Two arms, because BMAD spells a story key two ways and they are NOT
# interchangeable. A stories-mode id is alphanumeric segments joined by single
# dashes (`_STORIES_ID_RE`), so `3.2` and `3_2` are out. A sprint key's slug is
# unconstrained (`sprintstatus.STORY_RE`'s trailing group), so `3-2-foo.bar` and
# `3-2-a_b` are LEGAL keys that gate correctly — which is why this is a
# whole-token shape test and not a ban on `.`/`_`. Only those characters in the
# *number* prefix are unmatchable; banning them outright would refuse real gates.
#
# Sound in the direction that matters: a token matching either arm is itself a
# legal key, so a story it could gate can exist. `gates_story`'s prefix and split
# arms only ever extend a key rightward past a `-`, and every such prefix of a
# legal key matches one of these arms too.
#
# `sprintstatus` is imported for its regex; `stories.ID_RE` is copied rather than
# imported because `stories` imports *this* module (a cycle). The copy is pinned
# to the original by a drift test rather than to a comment.
_STORIES_ID_RE = re.compile(r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$")
# The tokens `gates_story`'s split arm may fire for: a bare `<epic>-<story>`, both
# numeric. `sprintstatus.STORY_RE` attaches the split letter straight after the
# story *number*, so that is the only token a split can extend. "Ends in a digit"
# is a weaker test that reads the same shape into a slug — `3-2-v2` would take the
# arm and refuse `3-2-v2a-followup`, a different and legal key.
_SPLITTABLE_TOKEN_RE = re.compile(r"^\d+-\d+$")
# A `gate:` line the strict field pattern above will never see. `GATE_RE` is
# anchored to a lowercase `gate:` in column 0, exactly like `status:`, and that
# strictness fails in opposite directions for the two fields: a missed `status:`
# leaves an entry unresolved, which now gates conservatively, while a missed
# `gate:` leaves no gate at all. `Gate: 3-2` and an indented `  gate: 3-2` are
# therefore surfaced as unenforceable rather than silently absent — and surfaced
# rather than *accepted*, because accepting an indented line would read a fenced
# example inside an entry as a live gate and refuse a story nobody meant to block.
_GATE_NEAR_RE = re.compile(r"^[ \t]*gate[ \t]*:", re.IGNORECASE | re.MULTILINE)
# The prose convention `gate:` replaces, matched anywhere on a line rather than
# at its start: real ledgers hard-wrap their `reason:` prose, so the declaration
# routinely lands mid-line and a line-anchored pattern misses exactly the entries
# that have one. The quote lookbehind is what keeps that from over-firing — an
# entry *citing* the phrase (`names a "HARD GATE: ..."`) is discussion, not a
# declaration — and the colon does the rest of the work, since a sentence about
# "this HARD GATE is textual only" never reaches the pattern at all.
# The class covers the backtick and the curly quotes as well as the ASCII pair:
# a ledger is markdown, so `HARD GATE:` is the citation form an author reaches for
# first, and an LLM-written entry curls its quotes. Missing them made the warning
# fire on entries documenting the convention — including this repo's own docs.
# The lookbehind only reaches an *inline* citation, though; the block form of the
# same quoting is a fence, and no character precedes a line inside one. Callers
# read this through `declares_prose_gate`, which masks those out.
HARD_GATE_PROSE_RE = re.compile(r"""(?<!["'`«“”‘’])HARD GATE:""")
# Everything `str.splitlines()` splits on, not `\n` alone (#305). The writers
# below interpolate their arguments into a line-oriented file, so a break in a
# value injects ledger lines. The C1/Unicode members are load-bearing rather
# than decorative: `parse_legacy` scans with `splitlines()` while `parse_ledger`
# matches with `re.MULTILINE`, so a U+2028 splits an entry for one reader and is
# invisible to the other — the two then disagree about what the ledger says.
LINE_BREAK_RE = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]+")
# The writers' date shape. Deliberately a separate literal from the legacy
# parser's `_DATE_TOKEN_RE`, which happens to look similar today: that one
# decides whether a freeform heading is a dated section, and tightening what the
# orchestrator will *write* must never quietly retune what `parse_legacy` reads.
# Spelled `[0-9]` rather than `\d`, which also matches Arabic-Indic, fullwidth
# and mathematical digit forms — the ledger's readers understand none of them.
_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class DWEntry:
    id: str
    title: str
    status: str  # the status field value, "" when the line is missing
    severity: str | None  # normalized critical/high/medium/low, None unknown
    body: str  # full entry text including the heading
    span: tuple[int, int]  # char offsets of the entry in the ledger text
    # Body-relative offsets of the line `status` was read from; None when the
    # entry has no status line. Carried rather than re-derived because the reader
    # picks the status with a fence-aware lookup at file scope, and a writer that
    # ran `STATUS_RE.search(body)` again would pick the *first* raw match instead.
    # Those differ exactly when an entry quotes an example above its live status:
    # the writer rewrote the quoted line, the reader kept reporting the real
    # `status: open`, and the close reported success while the entry — and any
    # `gate:` it carries — stayed open forever. No default: `parse_ledger` is the
    # only constructor, and a fallback here would silently restore that split.
    status_span: tuple[int, int] | None
    # The whole-file fence index the entry was carved with, so the gate scans can
    # ask the question the heading and status reads already ask at file scope. A
    # body slice cannot see a fence opened above the heading, and the two views
    # disagree: under a stray unclosed ``` above the heading, whole-file scope
    # treats the opener as text (so this entry EXISTS) while the body sees a later
    # matched `~~~` pair as a real fence and reads a live `gate:` as an example.
    # That direction loses a gate in silence, which is what the field exists to
    # end. No default, for `status_span`'s reason: the fallback IS the bug.
    examples: _Examples

    @property
    def open(self) -> bool:
        return self.status.split()[0] == "open" if self.status else False

    @property
    def done(self) -> bool:
        """Whether the entry has landed.

        Deliberately NOT ``not open``. A status line the format does not
        understand — ``status: opne``, or no status line at all — is neither open
        nor done, and the readers that ask want *opposite* answers about it:
        :func:`open_ids` drops it (it may already be finished), while a gate on it
        has to hold (it may not be). Deriving one from the other is what let
        ``gate:`` fail open on a one-character typo — the entry read as closed, so
        the gate was skipped and ``validate`` reported an all-clear naming it.
        """
        return self.status.split()[0] == "done" if self.status else False


@dataclass(frozen=True)
class _Examples:
    """The ledger's fenced worked examples, indexed for repeated offset queries.

    ``fenced_spans`` returns its ranges in increasing order and non-overlapping (a
    fence cannot open inside an open one), so a query is a binary search for the
    last span starting at or before the offset. Kept as an index rather than a bare
    list because both scales are in play at once: a parse asks once per heading and
    several times per entry, so a linear membership test would leave the parse
    quadratic whenever a ledger's examples grow with its entries.
    """

    spans: tuple[tuple[int, int], ...]
    starts: tuple[int, ...]

    def covers(self, offset: int) -> bool:
        i = bisect_right(self.starts, offset)
        return i > 0 and offset < self.spans[i - 1][1]


def _example_spans(text: str) -> _Examples:
    """The ledger's fenced worked examples, as offset ranges.

    Read at WHOLE-FILE scope, which is the whole point. A fence that opens above
    a quoted ``### DW-n:`` heading is stranded in the *previous* entry once spans
    are carved, so an entry-local query reads the example as live — a phantom
    entry whose ``gate:`` refuses a story nobody deferred. `deferred-work-format.md`
    ships exactly that shape (a complete entry inside a ```markdown fence), so
    quoting it into a ledger is the expected trigger, not a corner case.

    ``unclosed_hides_rest=False`` repeats the answer `gates()` gives one level
    down, and here for a stronger reason: under ``True`` a single stray opener
    would erase every heading below it, dropping real open work out of
    ``open_ids()`` in silence. A phantom entry from an unterminated fence is
    today's behaviour and is visible; a vanished ledger is neither.

    Walked once per :func:`parse_ledger` and passed down to the offset checks. The
    walk covers the whole file, so recomputing it per offset made the parse
    quadratic in the number of entries — and `Engine._refuse_gated_story` re-parses
    before every story dispatch, so a mature ledger paid it on the dispatch path.
    """
    spans = tuple(fenced_spans(text, unclosed_hides_rest=False))
    return _Examples(spans=spans, starts=tuple(s for s, _ in spans))


def _example(examples: _Examples, offset: int) -> bool:
    """Whether ``offset`` sits in a fenced worked example rather than the ledger.

    Takes the index rather than the text: the answer must come from the same
    whole-file walk for every offset in one parse, and a signature that re-derived
    it per call is what made that expensive enough to matter.
    """
    return examples.covers(offset)


def _unfenced(
    pattern: re.Pattern[str],
    text: str,
    start: int,
    end: int,
    examples: _Examples,
) -> re.Match[str] | None:
    """First match of ``pattern`` within ``text[start:end]`` that is not quoted.

    Not `search()` plus a check: the first match may be the quoted one, and the
    real boundary sits after it. Bounded by ``endpos`` so a match beyond the span
    cannot claim it, while ``examples`` still describes fence state from offset 0.
    """
    for m in pattern.finditer(text, start, end):
        if not _example(examples, m.start()):
            return m
    return None


def parse_ledger(text: str) -> list[DWEntry]:
    """Extract DW entries; non-conforming sections are skipped, an entry
    without a status line parses with status "" (not open).

    Fenced matches are skipped by every scan below, not just the heading one: a
    heading or flat bullet quoted inside an example must not start an entry, end
    one, or bound a block out of one. Filtering only the headings would trade the
    phantom entry for a truncation — a fenced ``## heading`` would still cut a
    real entry short at its own boundary, and a `gate:` line below the example
    would fall outside the span and stop gating, which is the failure this field
    exists to end.
    """
    entries = []
    examples = _example_spans(text)
    headings = [m for m in HEADING_RE.finditer(text) if not _example(examples, m.start())]
    for i, m in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        # an entry also ends at any intervening heading (e.g. a "## Deferred
        # from:" section header between freeform and DW-format content)
        other = _unfenced(ANY_HEADING_RE, text, m.end(), end, examples)
        if other:
            end = other.start()
        # ...and at a flat appender block, which belongs to no canonical entry
        # (#304). This span is what parse_legacy() masks out before scanning, so
        # absorbing the block hides the finding from every reader of the ledger.
        # Searched from the entry's own `status:` line, never from above it:
        # truncating over the status leaves the entry reading as neither open nor
        # done (open_ids() drops it, classify() calls it malformed), which trades
        # one lost flat block for one lost tracked entry. An entry with no status
        # line has nothing to protect, so the whole span is fair game.
        status_m = _unfenced(STATUS_RE, text, m.end(), end, examples)
        flat = _unfenced(
            FLAT_ENTRY_RE, text, status_m.end() if status_m else m.end(), end, examples
        )
        if flat:
            end = flat.start()
        body = text[m.start() : end]
        # Re-read rather than reuse the probe above: `end` may have moved, and the
        # status must be the one inside the final span. Searched over `text` at
        # absolute offsets because `_example` reads fence state from the top of the
        # file — a body slice cannot see an opener that sits above the heading.
        status_m = _unfenced(STATUS_RE, text, m.start(), end, examples)
        severity_m = _unfenced(SEVERITY_FIELD_RE, text, m.start(), end, examples)
        entries.append(
            DWEntry(
                id=m.group(1),
                title=m.group(2),
                status=status_m.group(1).strip() if status_m else "",
                severity=(
                    _normalize_severity(severity_m.group(1)) if severity_m is not None else None
                ),
                body=body,
                span=(m.start(), end),
                status_span=(
                    (status_m.start() - m.start(), status_m.end() - m.start()) if status_m else None
                ),
                examples=examples,
            )
        )
    return entries


def open_ids(text: str) -> set[str]:
    return {e.id for e in parse_ledger(text) if e.open}


@dataclass(frozen=True)
class EntryGates:
    """One entry's ``gate:`` declaration, split by what a check can act on.

    Every shape that is not an enforceable token is reported by ``validate``,
    because none of them is a *weaker* gate than a valid one — each is the prose
    gate again wearing the field's clothes, and silence about it is what let the
    story run. ``lines`` is what distinguishes "declared nothing usable" from
    "declared nothing at all": an entry with no ``gate:`` line has made no claim,
    while ``gate:`` with an empty value has made one and inertly.

    ``empty`` counts those inert lines individually rather than folding them into
    an entry-wide verdict, because the two coexist: ``gate: 3-2`` followed by a
    bare ``gate:`` has both a gate in force and a line that names nothing, and an
    aggregate answer can only report one of them. Reporting the tokens and
    swallowing the empty line is the worse half to lose — the operator who wrote
    it believes a second story is held back.
    """

    tokens: tuple[str, ...] = ()
    malformed: tuple[str, ...] = ()
    lines: int = 0
    empty: int = 0
    near_miss: int = 0

    @property
    def inert(self) -> bool:
        """Every ``gate:`` line named nothing — ``gate:`` or ``gate: ,`` and no others."""
        return self.lines > 0 and not self.tokens and not self.malformed


def _quoted(entry: DWEntry, offset: int) -> bool:
    """Whether a BODY-relative ``offset`` sits in a fenced example.

    The single rule every gate scan in this module reads through, so that a fence
    means the same thing to all of them: an entry documenting the field quotes it,
    and a quoted example is not a declaration. Sharing it is the point — the prose
    scan was left on the raw body once, on the reasoning that a warning is cheap
    and its quote lookbehind was guard enough. It is not: that lookbehind reaches
    an inline citation only, so an entry explaining the old convention in a fenced
    block was told to convert a gate it was not declaring.

    Asked at FILE scope, like the heading and status reads in :func:`parse_ledger`
    and for the same reason: a body slice cannot see a fence opened above the
    heading, so the two views can disagree about the same line. They disagree in
    the direction that matters — a stray unclosed ``` above the heading leaves the
    entry standing at file scope while the body reads a later matched ``~~~`` pair
    as a real fence, masking a live ``gate:`` into an example. A gate lost in
    silence is the failure this field exists to end; a spurious refusal in an entry
    whose markdown is already malformed is the cheaper wrong answer.
    """
    return entry.examples.covers(entry.span[0] + offset)


def declares_prose_gate(entry: DWEntry) -> bool:
    """Whether the entry declares a gate in the pre-``gate:`` prose convention.

    :data:`HARD_GATE_PROSE_RE` filtered the way every other gate scan here is
    filtered. Lives beside them rather than at the caller so the fence rule has
    one implementation: ``validate`` is the only reader today, and a second one
    reaching for the bare pattern would reintroduce exactly the half-applied rule
    this replaced.
    """
    return any(not _quoted(entry, m.start()) for m in HARD_GATE_PROSE_RE.finditer(entry.body))


def gates(entry: DWEntry) -> EntryGates:
    """Every ``gate:`` token in one entry's canonical span, order-preserving.

    Multiple ``gate:`` lines union: an entry blocking three stories may list them
    on one line or on three, and to a line-oriented file neither spelling is the
    wrong one. Within a line the separator is a comma, and only a comma — a
    space-separated ``gate: 3-2 3-3`` lands in ``malformed`` rather than being
    read leniently, so the operator is told the spelling gated nothing instead of
    finding out from a story that ran.

    Duplicates collapse (an id repeated across lines is one claim, not two);
    empty items drop, so a trailing separator is not a token — but the *line* is
    still counted, which is how an all-empty declaration stays reportable.

    ``near_miss`` counts the lines this function deliberately did NOT read as a
    declaration: a `gate:` the strict field anchor misses (see
    :data:`_GATE_NEAR_RE`). They are counted rather than parsed so the operator is
    told the spelling gated nothing — the same trade the space-separated token
    makes, one level up.
    """
    tokens: list[str] = []
    malformed: list[str] = []
    lines = 0
    empty = 0

    # Both scans below skip fenced matches: an entry documenting this field quotes
    # it, and a quoted example is not a declaration — a fenced `gate: 3-2` sits in
    # column 0, right where the anchor looks, and the answer here is a *refusal*.
    for m in GATE_RE.finditer(entry.body):
        if _quoted(entry, m.start()):
            continue
        lines += 1
        named = False
        for raw in m.group(1).split(","):
            token = raw.strip()
            if not token:
                continue
            named = True
            bucket = tokens if _matchable_token(token) else malformed
            if token not in bucket:
                bucket.append(token)
        if not named:
            empty += 1
    near_miss = sum(
        # `^` puts every match at a line start, so this asks whether the same line
        # would have satisfied `GATE_RE` — i.e. whether it is the canonical spelling
        # already counted above — without re-running the anchor against a slice.
        not entry.body.startswith("gate:", m.start())
        for m in _GATE_NEAR_RE.finditer(entry.body)
        if not _quoted(entry, m.start())
    )
    return EntryGates(
        tokens=tuple(tokens),
        malformed=tuple(malformed),
        lines=lines,
        empty=empty,
        near_miss=near_miss,
    )


def _matchable_token(token: str) -> bool:
    """Whether ``token`` could gate any legal story key — the test that decides
    :attr:`EntryGates.tokens` vs :attr:`EntryGates.malformed`.

    Both halves are required and neither implies the other: ``GATE_TOKEN_RE``
    alone admits ``3.2``, which nothing can match, and the key shapes alone admit
    ``3-2 3-3`` via the sprint slug, which is one token pretending to be two.
    """
    if not GATE_TOKEN_RE.match(token):
        return False
    return bool(_STORIES_ID_RE.match(token) or sprintstatus.STORY_RE.match(token))


def gates_story(token: str, story_key: str) -> bool:
    """Whether ``token`` gates ``story_key``: equal, or its prefix at a key boundary.

    The prefix arm is what lets one token reach both queues — stories mode keys on
    the bare id (``3-2``) while sprint mode keys on the full ``3-2-invite-link``,
    and an author gating "story 3-2" means the story, not the spelling. The
    boundary is required rather than a bare ``startswith`` so ``3-2`` cannot sweep
    in its numeric neighbours: ``3-20-later`` is a different story.

    Two boundaries count, because BMAD spells a story key two ways. The plain one
    is ``-``. The other is a **split**: ``sprintstatus.STORY_RE`` lets an oversized
    story become ``3-2a-...`` / ``3-2b-...`` at breakdown time, and a token that
    only knew ``-`` would lose its gate the moment the gated story was split —
    silently, which is the worst thing a gate can do. One lowercase ASCII letter
    followed by ``-`` is therefore also a boundary. Exactly one letter, and the
    ``-`` after it is required, so ``3-2ab-x`` and a bare ``3-2a`` are not swept in.

    The split arm applies only to a token that *is* a bare ``<epic>-<story>``,
    because that is the only place a split letter can attach: ``STORY_RE`` puts it
    straight after the story *number*. Without that guard the arm reads any
    trailing letter as a split and gates a story nobody named — ``stories.ID_RE``
    admits word ids, so ``gate: auth`` refused ``authz-login``, and a hard failure
    on an unrelated story is the one way this check can be worse than the prose it
    replaced. "Ends in a digit" is the same guard written too loosely: the digit
    can belong to a *slug*, so ``gate: 3-2-v2`` took the arm and refused
    ``3-2-v2a-followup`` — a different, legal key — and ``gate: 3`` refused the
    distinct stories id ``3a-task``.
    """
    if story_key == token or story_key.startswith(f"{token}-"):
        return True
    # The `startswith` guard is load-bearing, not redundant with the slice below:
    # `story_key[len(token):]` says nothing about what preceded it, so without it
    # `3-2` would gate `9-9a-x` on the tail alone.
    if not story_key.startswith(token) or not _SPLITTABLE_TOKEN_RE.match(token):
        return False
    rest = story_key[len(token) :]
    return len(rest) >= 2 and "a" <= rest[0] <= "z" and rest[1] == "-"


def parse_declaration(raw: object) -> tuple[tuple[str, ...], str | None]:
    """The single reading of a ``closes_deferred:`` declaration (#234), shared by
    the ``stories.yaml`` parser, the engine's close hook, and ``validate``.

    Returns the normalized ids plus an error describing a wrong *container*.
    Missing / YAML-null is an empty declaration, not an error.

    Strict about the container, lenient about each item. A bare
    ``closes_deferred: DW-1`` is a schema error rather than a silently-wrapped
    single id — a string is iterable, so a lenient reading would quietly turn one
    id into a list of characters — while items are ``str()``-normalized and
    stripped, because an LLM-authored manifest may emit an unquoted ``DW-1`` as a
    string but a bare ``5`` as an int. Blanks drop and duplicates collapse
    (order-preserving): both are noise, not a contradiction.

    Callers decide the severity: the manifest parser raises, the engine journals,
    ``validate`` warns. What they must NOT do is disagree — before this, a wrong
    container was a hard schema error in ``stories.yaml`` and a silent empty
    declaration in frontmatter, so the same mistake either failed the parse or
    vanished depending on which file it was made in.

    Whether an id names a real entry is not decided here; that needs the ledger
    (:func:`classify`).
    """
    if raw is None:
        return (), None
    if not isinstance(raw, list):
        return (), f"must be a list of deferred-work ids (got {type(raw).__name__})"
    return tuple(dict.fromkeys(item for item in (str(x).strip() for x in raw) if item)), None


@dataclass(frozen=True)
class Declared:
    """How declared ids line up against one ledger snapshot (#234).

    Four outcomes, not two, because "not open" hides two very different cases.
    ``already_done`` is a satisfied declaration — a resume re-driving a close that
    already landed — and must stay silent. ``malformed`` is an entry that exists
    but carries neither an ``open`` nor a ``done`` status: nothing can be marked,
    and saying nothing would leave the operator believing it was.

    ``duplicates`` cross-cuts the other four: it names the declared ids the ledger
    carries more than once, whichever bucket they landed in. A duplicate id is a
    corrupt ledger (#286), and the entry this classification describes is only one
    of them — so the close is reported, never silent.
    """

    open_ids: tuple[str, ...] = ()
    already_done: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    malformed: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()


def classify(text: str, ids: Sequence[str]) -> Declared:
    """Partition `ids` against a single ledger snapshot, preserving order.

    Classifying from a snapshot rather than from :func:`mark_done`'s return value
    is deliberate: that return conflates "already done" with "absent from the
    ledger", and those need opposite treatment (silence vs. a warning).

    **The FIRST entry of a duplicated id wins**, because that is the one
    :func:`_find_entry` — and so every mutation in this module — acts on. Indexing
    last-wins instead made the two disagree, and a ledger carrying one `DW-1` open
    and another done then closed nothing while saying nothing, in either order: a
    done-first ledger classified the id `open`, sent it to
    :func:`mark_done_many`, and had :func:`_apply_done` refuse the done copy it
    found first (marked nothing, so not even an unmatched warning); an open-first
    ledger classified it `already_done` and never attempted the write at all
    (#284 round-6 review, finding 4). The duplicate itself is reported through
    ``duplicates`` rather than swallowed — one id naming two entries is a fault
    about the ledger, not an answer about the work."""
    by_id: dict[str, DWEntry] = {}
    duplicated: set[str] = set()
    for e in parse_ledger(text):
        if e.id in by_id:
            duplicated.add(e.id)
            continue  # first wins: `_find_entry` mutates that one
        by_id[e.id] = e
    buckets: dict[str, list[str]] = {"open": [], "done": [], "unknown": [], "malformed": []}
    for dw_id in ids:
        entry = by_id.get(dw_id)
        if entry is None:
            buckets["unknown"].append(dw_id)
            continue
        word = entry.status.split()[0] if entry.status else ""
        buckets[word if word in ("open", "done") else "malformed"].append(dw_id)
    return Declared(
        open_ids=tuple(buckets["open"]),
        already_done=tuple(buckets["done"]),
        unknown=tuple(buckets["unknown"]),
        malformed=tuple(buckets["malformed"]),
        duplicates=tuple(dw_id for dw_id in dict.fromkeys(ids) if dw_id in duplicated),
    )


def _find_entry(text: str, dw_id: str) -> DWEntry | None:
    for entry in parse_ledger(text):
        if entry.id == dw_id:
            return entry
    return None


def _insert_after_status(text: str, entry: DWEntry, line: str) -> str:
    """Insert a field line right after the entry's status line (or at the end
    of the entry when no status line exists)."""
    if entry.status_span:
        pos = entry.span[0] + entry.status_span[1]
        return text[:pos] + "\n" + line + text[pos:]
    insert_at = entry.span[0] + len(entry.body.rstrip())
    return text[:insert_at] + "\n" + line + text[insert_at:]


def _one_line(value: str) -> str:
    """Collapse every run of line-break characters in `value` to a single space.

    The whole of the #305 fix. These writers interpolate their arguments into a
    line-oriented file, so a value carrying a break mints a phantom
    `### DW-<n>` entry, truncates the entry's span at :data:`FLAT_ENTRY_RE` and
    re-surfaces the tail as a legacy item, or leaves the entry carrying two
    `status:` lines.

    Note what the last shape does *not* do: `STATUS_RE` takes the first match, so
    an injected `status:` never changes what `parse_ledger` reports. A test that
    asserts on `entry.status` therefore passes with this guard deleted — the
    observable is the line structure.

    Sanitizes; never raises, and nothing upstream rejects on a break either. The
    close paths do not want a `ValueError` out of these writers: it used to end
    the sweep as crashed from `sweep._close_resolved`, which since DW-166 catches
    it and degrades instead — losing the batch's closures for the cycle, which is
    better than the crash but still a lost cycle of bookkeeping, and
    `decisions.apply_pre_answer` still calls them bare (its `cli` and TUI callers
    hold the handler). Refusing the same text back at `validate_triage` only
    moved the stoppage to a pause. Collapsing is lossless enough — the ledger
    wants one line anyway — so this is the fix, and the skill docs are guidance
    that reduces occurrences without gating on them.

    That contract covers one hazard more than the break collapse alone, which is
    what the `neutralize_surrogates` pass in front of it buys (#329). A lone
    surrogate is not a line break, so it sailed through untouched — but it has
    no UTF-8 encoding, and `atomic_write_text`'s strict encode raises
    `UnicodeEncodeError` (a `ValueError` subclass) on it, from inside those same
    close-path calls. It arrives the way the break did: a triage
    `result.json` is cached with `json.dumps`, whose `ensure_ascii` keeps the
    code point a harmless `\\ud800` escape, and the reload's `json.loads` revives
    the real thing into `ResolvedEntry.evidence` and on into the `mark_done`
    note. Refusing it upstream would only move the stoppage again — same
    doctrine, same answer.

    A value with neither a break nor a surrogate is returned **untouched**, so an
    existing ledger is never reformatted and a clean write is byte-identical to
    before the guard; each pass keeps its own fast path, so the common value is
    scanned twice and copied never. The trailing `.strip()` removes all
    surrounding whitespace, not merely the space a leading or trailing break left
    behind — which is why it must stay on the far side of that fast path.

    A break-only value therefore sanitizes to `""`. Keeping it non-empty *here*
    could only yield bare whitespace, which trades an unfindable entry for an
    unidentifiable one, so each caller handles its own empties — and by two
    different strategies, which is why neither belongs in this helper.
    :func:`append_entry` **substitutes**, naming a vanished title
    `(untitled DW-<n>)` so the id it just burned stays findable.
    :func:`append_decision` **drops**, shedding the ` — ` separator along with an
    empty detail rather than promising one that is not there. Its `label` needs
    neither: every member of :data:`LINE_BREAK_RE` is `str.isspace()`, and
    `validate_triage` builds each `DecisionOption` with `.strip() or key`, so a
    break-only label has already become the option key before it arrives.

    A surrogate-only value, by contrast, sanitizes to a truthy `"�"`, so neither
    caller's empty-handling fires for it. That is the point of replacing rather
    than stripping: a title reading `�` still says *something unencodable was
    here*, where a vanished one would silently become `(untitled DW-<n>)`."""
    value = neutralize_surrogates(value)
    if not LINE_BREAK_RE.search(value):
        return value
    return LINE_BREAK_RE.sub(" ", value).strip()


def _iso_date_or_none(value: str) -> str | None:
    """`value` when it is a strict ISO ``YYYY-MM-DD`` calendar date, else None.

    The shared shape of the ledger's two date checks, so a skip-not-raise caller
    (:func:`_close_date`) and a raise caller (:func:`_require_iso_date`) cannot
    drift apart on what counts as a close date. The regex is not redundant with
    ``date.fromisoformat``: since 3.11 that also accepts ``20260611`` and ISO
    week dates, neither of which the ledger's own readers recognize, and it is
    the regex — via ``[0-9]`` — that pins the digits to ASCII. ``fromisoformat``
    in turn rejects the well-shaped impossible day (``2026-02-30``) that no
    pattern can catch."""
    if not _ISO_DATE_RE.fullmatch(value):
        return None
    try:
        calendar_date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _require_iso_date(value: str) -> None:
    """Raise unless `value` is a strict ISO `YYYY-MM-DD` calendar date.

    Raising is right here and wrong for free text: `date` is orchestrator-owned
    (`Engine._today()`), never model-authored, so a bad value is a programmer
    bug. Letting it through writes a `status:` line that reads as neither open
    nor done, which `classify` reports as malformed and `open_ids` drops — the
    entry silently leaves the sweep's world."""
    if _iso_date_or_none(value) is None:
        raise ValueError(f"date must be YYYY-MM-DD: {value!r}")


def _require_canonical_status(status: str) -> None:
    """Raise unless `status` is exactly `open` or `done YYYY-MM-DD`.

    Two halves with two different dependents. The *first word* is what
    :attr:`DWEntry.open` and :func:`classify` branch on, so anything but `open`
    or `done` makes an entry unreadable to both. The *date* is invisible to them
    — they read `status.split()[0]` and cannot tell `done 2026-02-30` from a real
    day — but it is not invisible downstream: the whole status value is carried
    verbatim to readers (the TUI's deferred pane, the `--json` projections), so a
    malformed date is rendered to a human as though it were one."""
    if status == "open":
        return
    if status.startswith("done "):
        _require_iso_date(status.removeprefix("done "))
        return
    raise ValueError(f"status must be 'open' or 'done YYYY-MM-DD': {status!r}")


def _operation_digest(operation_id: str) -> str:
    """Encode a stable close-operation id as one ledger-safe token."""
    if not operation_id:
        raise ValueError("operation_id must not be empty")
    return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()


# Per-thread reentrancy guard for :func:`ledger_lock`. `file_lock` is per open
# fd, so a second acquisition from the same process does not merely queue — on
# POSIX `flock` it blocks forever against a lock this very thread holds, with no
# timeout and no traceback. Thread-local rather than a plain module global
# because the state being tracked is "does THIS thread already hold it", and two
# threads legitimately contend through the OS lock.
_LOCK_STATE = threading.local()


@contextmanager
def ledger_lock(path: Path) -> Iterator[None]:
    """Cross-process mutual exclusion for one ledger (#286/#469).

    Held only around a single read->edit->write of `path` — never across a
    subprocess, a coding-CLI session, or an operator pause. That is an acceptance
    criterion of #286 rather than a style preference: `file_lock`'s Windows
    branch gives up after ~10 s and raises, so a holder that waits on anything
    slower converts a contended run into a failed one. It is also why the
    engine's rollback/restore windows, which span git spawns, get compare-and-set
    semantics instead of a lock around the window.

    Acquired in exactly three strata: the leaf mutators in this module, the
    engine's CAS restores, which do pure in-memory text work under the hold, and
    — keyed on a different file entirely — the pre-answer store's three writers
    in :mod:`~bmad_loop.decisions` (DW-161). Never call a mutator while holding
    it — every mutator takes this lock itself, and the nested acquisition would
    deadlock.

    Nesting raises :class:`RuntimeError` rather than deadlocking. The guard is
    deliberately path-agnostic: two *different* ledgers would not self-deadlock
    on the OS lock, but nesting is still a lock-ordering hazard, and no caller
    has a reason to hold two ledgers at once. That path-agnosticism is also why
    the store reuses this helper rather than minting a twin. What the two files
    share is ONLY this nesting guard — never an OS lock:
    :func:`~bmad_loop.runs.lock_path_for` keys each sidecar on
    ``sha256(resolved path)[:16]``, so the ledger and the store take different
    locks and exclude nobody from each other. The guard's consequence is the
    benefit: no caller may hold both at once, in either order. Two independent
    guards would let a caller hold the ledger and the store simultaneously with
    neither one noticing. The lock file itself lives out of
    the repository — see :func:`~bmad_loop.runs.lock_path_for` for why a sidecar
    beside the tracked ledger would be committed by the engine's own `git add
    -A`. Propagates `OSError` from acquisition and
    :class:`~bmad_loop.runs.StateRootError` when no state root can be derived: a
    write that could not be serialized must fail loudly, not proceed unlocked.
    """
    # Lazy, and it has to stay lazy: `runs` imports `verify`, which imports this
    # module, so a top-level import here closes the cycle.
    from . import runs

    if getattr(_LOCK_STATE, "held", False):
        raise RuntimeError("ledger lock is not reentrant")
    lock_path = runs.lock_path_for(path)
    _LOCK_STATE.held = True
    try:
        # Spelled out rather than `with file_lock(...)`: the one thing the
        # statement cannot express is "the body completed, and THEN the release
        # faulted", which is the case `LedgerLockReleaseError` names. Acquisition
        # faults leave `__enter__` untouched (`LockUnavailableError`, or a bare
        # `OSError` from the sidecar's `mkdir`/`open`), and a body that raised
        # keeps its own fault ahead of any release fault — see the except arm.
        lock = file_lock(lock_path)
        lock.__enter__()
        try:
            yield
        except BaseException as body_fault:
            # The body's fault is the news, and it stays the exception that leaves.
            # A `with` statement would let a release fault on top of it REPLACE it,
            # and for a body that raised `LedgerWriteError` that replacement is a
            # bare `OSError` — exactly the type the sweep's degrade arms read as a
            # lock never acquired, so a failed repair write would be degraded
            # after all. The release fault rides along as `__context__` and as a
            # note; it is not lost, it just does not get to speak first.
            try:
                suppressed = lock.__exit__(*sys.exc_info())
            except OSError as release_fault:
                body_fault.add_note(
                    "and then the ledger lock's release faulted: "
                    f"{type(release_fault).__name__}: {release_fault}"
                )
                raise body_fault  # noqa: B904 — `__context__` IS the release fault
            if not suppressed:
                raise
        else:
            try:
                lock.__exit__(None, None, None)
            except OSError as e:
                raise LedgerLockReleaseError(
                    f"published, then could not release the ledger lock: "
                    f"{type(e).__name__}: {e}"
                ) from e
    finally:
        _LOCK_STATE.held = False


def _apply_done(
    text: str,
    dw_id: str,
    date: str,
    note: str,
    *,
    undo_owner: str | None = None,
) -> str | None:
    """Flip one entry to `status: done <date>` + a resolution note *within* `text`.
    None when the entry is missing or not open. The entry is re-located after the
    status rewrite because that edit shifts every later span offset.

    The note is sanitized here, at the point of interpolation, rather than on
    :func:`mark_done`: that is a one-id wrapper over :func:`mark_done_many`, which
    `Engine._apply_deferred_closes` calls directly, so a wrapper-side guard would
    never see a story close (#305). `date` is validated by the sole caller, at its
    entry, so the check does not depend on a ledger existing."""
    note = _one_line(note)
    entry = _find_entry(text, dw_id)
    if entry is None or not entry.open:
        return None
    assert entry.status_span is not None  # open implies a status line
    start = entry.span[0] + entry.status_span[0]
    end = entry.span[0] + entry.status_span[1]
    previous_status_line = entry.body[entry.status_span[0] : entry.status_span[1]]
    if undo_owner is not None and LINE_BREAK_RE.search(previous_status_line):
        # An undo marker must never preserve a value that becomes more than one line
        # under the ledger readers' shared splitlines semantics. Standard closes
        # retain their existing behavior; the undo-capable path refuses the mark.
        return None
    done_status_line = f"status: done {date}"
    text = text[:start] + done_status_line + text[end:]
    entry = _find_entry(text, dw_id)
    assert entry is not None
    tail = f"resolution: {note}"
    if undo_owner is not None:
        # The owner digest makes this close distinguishable from an earlier run
        # that reused its human-readable note. The encoded prior line makes the
        # undo lossless for parser-accepted spacing and annotations. Hex keeps
        # every payload on one ASCII line, including Unicode annotations.
        previous_status_hex = previous_status_line.encode("utf-8").hex()
        tail += f"\nresolution-undo: {undo_owner} {date} {previous_status_hex}"
    return _insert_after_status(text, entry, tail)


def _apply_done_many(
    text: str,
    dw_ids: Sequence[str],
    date: str,
    note: str,
    notes: Sequence[str] | None,
    undo_owner: str | None,
) -> tuple[str, list[str]]:
    """Fold every id in `dw_ids` through :func:`_apply_done` *within* `text`,
    returning the new text and the ids actually flipped, in the order given.

    Pure — text in, text out, no `Path` and no I/O — and it is ONE body for the
    advisory pre-lock probe and the locked pass, so the two cannot drift: the
    argument :func:`_apply_append`'s extraction already makes for the batched
    appender. The whole decision lives here, `undo_owner` included, because the
    reopenable arm's LINE_BREAK refusal (:func:`_apply_done`) can be the only
    reason a batch flips nothing — a probe that scanned for open entries by hand
    would answer "would write" where this answers "would not"."""
    marked: list[str] = []
    for index, dw_id in enumerate(dw_ids):
        entry_note = note if notes is None else notes[index]
        updated = _apply_done(text, dw_id, date, entry_note, undo_owner=undo_owner)
        if updated is None:
            continue
        text = updated
        marked.append(dw_id)
    return text, marked


def _mark_done_many(
    path: Path,
    dw_ids: Sequence[str],
    date: str,
    note: str,
    *,
    operation_id: str | None = None,
    notes: Sequence[str] | None = None,
) -> list[str]:
    """Shared atomic implementation for the public close operations.

    ONE locked read->edit->write: the whole cycle runs under the cross-process
    ledger lock (#286/#469), so concurrent mutators — a second run, a sweep, the
    TUI decision modal, ``sweep --archive`` — serialize here rather than trading
    last-write-wins. Validation stays ABOVE the lock, so a programmer bug reports
    itself without first waiting on another process.

    A batch that would flip nothing — every id missing, already done, or refused
    by the reopenable arm's line-break guard — is answered from the advisory
    pre-lock probe instead, with no acquisition at all (#736). The probe folds
    the ids through :func:`_apply_done_many`, the same helper the locked pass
    uses, so it cannot answer "no write" where the authority would write.

    ``notes`` supplies a per-id resolution note, positionally paired with
    ``dw_ids``; ``note`` is the fallback for every id when it is None. A length
    mismatch raises before any I/O rather than closing a prefix under the wrong
    evidence — the pairing is positional, so a short list is a caller bug that
    would otherwise mis-attribute notes silently.
    """
    _require_iso_date(date)
    if notes is not None and len(notes) != len(dw_ids):
        raise ValueError(f"notes must be one per dw_id: {len(notes)} for {len(dw_ids)} ids")
    undo_owner = _operation_digest(operation_id) if operation_id is not None else None
    if not dw_ids:
        # Nothing to serialize against, so nothing to take a lock for — the same
        # early return `append_entries` makes, for the same reason. Below the
        # validation above, so an empty batch still reports a bad date or a bad
        # operation id; above the lock, so a caller that batches an empty set
        # cannot start failing on a lock it never needed. The per-id loop this
        # primitive replaced took no lock at all when handed nothing, and that
        # identity is part of what "byte-identical to the serial sequence" buys.
        return []
    if not _ledger_present(path):
        # No ledger, no entry to flip, so no write and no lock — the order
        # `archive_closed` already keeps for its own missing-ledger case. A
        # REFUSED ledger raises out of the guard instead (DW-255). The recheck
        # under the hold below stays: creation can race this answer.
        return []
    try:
        # ADVISORY pre-lock probe (#736): one read, and the same pure decision
        # the locked pass makes. Only a "would write nothing" answer is acted on
        # — the call then serializes at this read. Anything else, including any
        # fault here, falls through to the hold, which re-reads and decides.
        probe = path.read_text(encoding="utf-8")
        if not _apply_done_many(probe, dw_ids, date, note, notes, undo_owner)[1]:
            return []
    except Exception:  # nosec B110 - ADVISORY probe: a fault here must decide nothing
        # Neither arm of the DW-146 ledger-read contract, and deliberately
        # broader than both: this probe decides nothing on a fault, and the
        # locked read below is the REPAIR/WRITE site that does.
        pass
    with ledger_lock(path):
        # REPAIR/WRITE (DW-146): this text is edited and published below. `None`
        # is absence alone; a refusal raises out of the reader (DW-221/255).
        text = read_for_write(path)
        if text is None:
            return []
        text, marked = _apply_done_many(text, dw_ids, date, note, notes, undo_owner)
        if not marked:
            return []
        _publish(path, text)
        return marked


def mark_done_many(
    path: Path,
    dw_ids: Sequence[str],
    date: str,
    note: str,
    *,
    notes: Sequence[str] | None = None,
) -> list[str]:
    """Flip every entry in `dw_ids` to `status: done <date>` + a resolution note,
    in ONE read and ONE atomic write. Returns the ids actually flipped (missing
    and already-done ids are skipped), in the order given.

    ``notes[i]`` overrides `note` for ``dw_ids[i]`` — the shape a caller closing
    several entries under per-entry evidence needs, which otherwise costs one
    read-modify-write cycle per id. A length mismatch raises `ValueError` before
    any I/O.

    All-or-nothing on purpose. A per-id read-modify-write loop leaves marks on
    disk when it raises partway through several ids — a half-applied closure the
    caller never gets to journal, so the ledger claims resolutions the run has no
    record of. Here a failure writes nothing, and the returned list is exactly
    what landed.

    The write goes through :func:`~bmad_loop.platform_util.atomic_write_text`
    rather than a bare tmp+replace: swapping a fresh inode over the ledger
    otherwise resets its mode (a ``0600`` ledger silently becoming world-readable)
    and turns a symlinked ledger into a regular file.

    ``date`` is validated before the presence short-circuit so a programmer bug
    fails the same way whether or not a ledger happens to exist — a guard that
    only fires when the file is present is one an absent fixture hides."""
    return _mark_done_many(path, dw_ids, date, note, notes=notes)


def mark_done_many_reopenable(
    path: Path,
    dw_ids: Sequence[str],
    date: str,
    note: str,
    operation_id: str,
) -> list[str]:
    """Close entries atomically with a durable, operation-specific undo marker.

    ``operation_id`` must be stable and recomputable across crash/replay from
    already-persisted identity (for example ``run_id`` + ``story_key``), never an
    ephemeral random value. Only entries actually flipped receive its marker;
    skipped, already-done ids therefore cannot be reopened by this operation.

    The ordinary :func:`mark_done_many` deliberately emits no marker and retains
    its existing ledger format. Use this variant only for a transaction with a
    later rollback leg.
    """
    return _mark_done_many(path, dw_ids, date, note, operation_id=operation_id)


def mark_done(path: Path, dw_id: str, date: str, note: str) -> bool:
    """Flip one entry to `status: done <date>` and record a resolution note.
    Returns False (no write) when the entry is missing or already done."""
    return bool(mark_done_many(path, [dw_id], date, note))


def mark_seen_again_many(
    path: Path, dw_ids: Sequence[str], date: str, note: str
) -> tuple[list[bool], str | None, list[str], str | None]:
    """Stamp `seen-again: <date> (<note>)` under each entry's status line, in ONE
    read and ONE atomic write. Returns one applied flag per id, the text it
    published — None when it wrote nothing — the ids whose match went STALE
    inside the hold, and the PREIMAGE this call read under that hold (None when
    no locked read happened at all).

    The writer half of the format doc's dedupe rule (deferred-work-format.md): a
    finding that matches an existing entry is recorded as a sighting on that
    entry, never as a duplicate entry. Idempotent per (id, date, note): a replay
    stamping the same line is skipped, so a flag reads "this call inserted it",
    not "the line is there".

    ⚠️ The two ways a flag can be False are NOT interchangeable, which is why the
    stale ids come back separately. A replay whose line is already present has a
    live sighting on a live entry and must file nothing. An id that is missing or
    NO LONGER OPEN has no sighting anywhere: the caller matched it against a
    snapshot read before this lock and a rival closed or archived it in between,
    so its finding is recorded work again rather than a duplicate and the caller
    must file it. Stamping a done entry instead — while the caller, having already
    excluded the finding from its append, files nothing — drops the recurrence in
    silence, which is the whole reason the recheck happens inside the hold and not
    against the caller's snapshot.

    ``entry.open``, not ``not entry.done``: a status the format cannot parse is
    neither, and the only question here is whether this is still the open entry
    the caller matched — so the predicate has to be the one the caller used.

    A missing ledger applies nothing, takes no lock, and reports every id stale,
    like :func:`mark_done_many`'s absent-file arm; a REFUSED ledger raises out of
    the same :func:`_ledger_present` guard instead (DW-255), on every interpreter.

    ONE locked read->edit->write (#286/#469): each insert lands on the text the
    previous one produced (:func:`_find_entry` re-parses the evolving text, so
    spans stay honest), and the published text is handed back from inside the
    hold for the same ``post_engine_ledger_digest`` reasons as
    :func:`append_entries_published`. `date` is orchestrator-owned and raises
    when malformed; `note` is sanitized to one line (#305).

    The PREIMAGE comes back for the other half of that anchor question. The
    published text says WHAT WAS WRITTEN; only the preimage says WHAT IT WAS
    WRITTEN OVER, and an anchor may claim the published bytes as the caller's own
    solely when the preimage is still the bytes the caller last knew the file to
    hold. A rival that lands between the caller's snapshot and this locked read
    is folded into the preimage — and therefore re-published — so without that
    comparison the caller's anchor would authorize retracting the rival's work.
    """
    _require_iso_date(date)
    line = f"seen-again: {date} ({_one_line(note)})"
    if not dw_ids:
        return [], None, [], None
    if not _ledger_present(path):
        return [False for _ in dw_ids], None, list(dw_ids), None
    with ledger_lock(path):
        # REPAIR/WRITE (DW-146): the preimage this call anchors its write on.
        # `None` is absence alone; a refusal raises out of the reader.
        preimage = read_for_write(path)
        if preimage is None:
            return [False for _ in dw_ids], None, list(dw_ids), None
        text = preimage
        applied: list[bool] = []
        stale: list[str] = []
        for dw_id in dw_ids:
            entry = _find_entry(text, dw_id)
            if entry is None or not entry.open:
                applied.append(False)
                stale.append(dw_id)
                continue
            if line in entry.body:
                applied.append(False)
                continue
            text = _insert_after_status(text, entry, line)
            applied.append(True)
        if not any(applied):
            return applied, None, stale, preimage
        _publish(path, text)
        # Both returned from INSIDE the hold: the published text by
        # construction, and the preimage it replaced — neither a read-back that a
        # rival could have moved.
        return applied, text, stale, preimage


_MARK_DONE_TAIL_RE = re.compile(
    r"\nresolution:[ \t]*(.*)"
    r"\nresolution-undo:[ \t]*([0-9a-f]{64})[ \t]+"
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})[ \t]+([0-9a-f]+)$",
    re.MULTILINE,
)


def _apply_open(text: str, dw_id: str, note: str, undo_owner: str) -> str | None:
    """Undo one reopenable close *within* `text`. None when the entry is missing,
    already open, or does not carry this operation's adjacent resolution and
    undo-marker lines.

    Pure by construction — text in, text out, no `Path` and no I/O — which is
    what keeps :func:`mark_open_many` able to run it several times inside a
    single :func:`ledger_lock` hold. A version of this that touched the file
    would have to take the lock itself, and the nested acquisition is exactly the
    self-deadlock the guard on `ledger_lock` exists to convert into an error.

    A standard or earlier close has no matching marker and cannot be reopened
    merely because it reused the same human-readable note.

    A live ``archived:`` stamp is demoted to :data:`_ARCHIVED_BODY_FIELD` rather
    than dropped: the reopened entry is no longer archived, but the body its
    close moved out still is, and that line is the only thing a later triage has
    to find it with."""
    entry = _find_entry(text, dw_id)
    if entry is None or entry.open:
        return None
    if entry.status_span is None:
        # parse_ledger deliberately tolerates status-less entries. This primitive
        # is later called from _defer, where an AttributeError would crash the run
        # instead of completing the deferral.
        return None
    status_line = entry.body[entry.status_span[0] : entry.status_span[1]]
    try:
        _require_canonical_status(entry.status)
    except ValueError:
        # Only a canonical status written by mark_done is eligible for undo.
        # Preserve malformed or human-authored statuses for validation/reporting.
        return None
    res_m = _MARK_DONE_TAIL_RE.match(entry.body, entry.status_span[1])
    if res_m is None:
        return None
    if res_m.group(1).strip() != _one_line(note).strip() or res_m.group(2) != undo_owner:
        return None
    if status_line != f"status: done {res_m.group(3)}":
        return None
    try:
        previous_status_line = bytes.fromhex(res_m.group(4)).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    if LINE_BREAK_RE.search(previous_status_line):
        return None
    previous_status_m = STATUS_RE.fullmatch(previous_status_line)
    previous_status = previous_status_m.group(1).strip() if previous_status_m else ""
    if not previous_status or previous_status.split()[0] != "open":
        return None
    start = entry.span[0] + entry.status_span[0]
    end = entry.span[0] + res_m.end()
    # Demote the entry's live `archived:` stamps along with the close they
    # describe, rather than deleting them. A stub's stamp says "this body lives
    # in the archive file"; once the close is undone the body is here and the
    # line is a lie, and leaving it standing is not merely untidy — status +
    # undo tail + stamp is the exact `_STUB_BODY_RE` shape, so the next
    # reopenable close reconstitutes a stub `archive_closed` skips forever,
    # stranding the entry outside every future archive (#711).
    #
    # Cutting the line outright strands the entry a second way: a stub keeps
    # neither `location:` nor `reason:` (`_PRESERVED_FIELD_RE`), so the stamp is
    # the reopened entry's ONLY route back to the body, and triage arrives with
    # a heading and nothing to triage (#711 review). Renaming the field keeps
    # both properties — the value still narrows to the archive block, an id
    # owning several once a re-closure is archived too, while the renamed line
    # matches neither `_ARCHIVED_FIELD_RE` nor `_STUB_BODY_RE`, so the entry
    # reads as live and re-archives normally. Rehydrating the body here
    # instead was the alternative and is worse: several blocks per id is by
    # design, so a rollback's reopen would have to guess which one, and a wrong
    # guess overwrites live content with a stale body.
    #
    # Cuts are disjoint (an `^archived:` line cannot start inside the status
    # line or its adjacent tail) and applied back-to-front so earlier offsets
    # stay valid.
    cuts = [(start, end, previous_status_line)]
    for cut_start, cut_end in _archived_line_spans(entry):
        # Everything after the field name — value, spacing and the terminating
        # newline — carries over verbatim; the span starts at the anchor, so
        # the first colon is the field's own.
        stamp = entry.body[cut_start:cut_end].split(":", 1)[1]
        cuts.append(
            (
                entry.span[0] + cut_start,
                entry.span[0] + cut_end,
                f"{_ARCHIVED_BODY_FIELD}{stamp}",
            )
        )
    for cut_start, cut_end, replacement in sorted(cuts, reverse=True):
        text = text[:cut_start] + replacement + text[cut_end:]
    return text


def _apply_open_many(
    text: str, dw_ids: Sequence[str], note: str, undo_owner: str
) -> tuple[str, list[str]]:
    """Fold every id in `dw_ids` through :func:`_apply_open` *within* `text`,
    returning the new text and the ids actually reopened, in the order given.

    Pure — text in, text out, no `Path` and no I/O — and ONE body for the
    advisory pre-lock probe and the locked pass, so the two cannot drift. The
    `undo_owner` match is part of the decision: an entry closed by a different
    operation is skipped here, which is what makes "no id was eligible" a
    question only this fold can answer."""
    reopened: list[str] = []
    for dw_id in dw_ids:
        updated = _apply_open(text, dw_id, note, undo_owner)
        if updated is None:
            continue
        text = updated
        reopened.append(dw_id)
    return text, reopened


def mark_open_many(path: Path, dw_ids: Sequence[str], note: str, operation_id: str) -> list[str]:
    """Undo every close in `dw_ids` written by :func:`mark_done_many_reopenable`
    under `operation_id`, in ONE read and ONE atomic write. Returns the ids
    actually reopened, in the order given; missing and ineligible ids are
    skipped, and an entry whose marker does not match this operation is left
    exactly as it was.

    ONE locked read->edit->write: the whole cycle runs under the cross-process
    ledger lock (#286/#469), so concurrent mutators — a second run, a sweep, the
    TUI decision modal, ``sweep --archive`` — serialize here rather than trading
    last-write-wins. A per-id loop over :func:`mark_open` would instead take the
    lock once per id, leaving a rival writer a window between every pair of
    undos in what a rollback needs to be one step.

    Nothing is written when no id was eligible, and no lock is taken either
    (#736): a replayed rollback over already-reopened entries is answered from
    one advisory read, so it leaves the file untouched rather than rewriting it
    byte-for-byte, and cannot fail on a lock it had no write to serialize."""
    undo_owner = _operation_digest(operation_id)
    if not dw_ids:
        # No ids, no lock — see `_mark_done_many`. The `operation_id` above is
        # still validated, so an empty reopen cannot smuggle a bad one through.
        return []
    if not _ledger_present(path):
        # No ledger, no close to undo — see `_mark_done_many`; a refused one
        # raises here (DW-255). Rechecked under the hold below.
        return []
    try:
        # ADVISORY pre-lock probe (#736): one read, and the same pure decision
        # the locked pass makes. Only a "would write nothing" answer is acted on
        # — the call then serializes at this read. Anything else, including any
        # fault here, falls through to the hold, which re-reads and decides.
        probe = path.read_text(encoding="utf-8")
        if not _apply_open_many(probe, dw_ids, note, undo_owner)[1]:
            return []
    except Exception:  # nosec B110 - ADVISORY probe: a fault here must decide nothing
        # Neither arm of the DW-146 ledger-read contract, and deliberately
        # broader than both: this probe decides nothing on a fault, and the
        # locked read below is the REPAIR/WRITE site that does.
        pass
    with ledger_lock(path):
        # REPAIR/WRITE (DW-146): this text is edited and published below. `None`
        # is absence alone; a refusal raises out of the reader (DW-221/255).
        text = read_for_write(path)
        if text is None:
            return []
        text, reopened = _apply_open_many(text, dw_ids, note, undo_owner)
        if not reopened:
            return []
        _publish(path, text)
        return reopened


def mark_open(path: Path, dw_id: str, note: str, operation_id: str) -> bool:
    """Undo one close written by :func:`mark_done_many_reopenable`.

    A one-id wrapper over :func:`mark_open_many`, which is where the lock is
    taken and the contract documented. It delegates rather than duplicating the
    read->edit->write so that one public call is exactly one acquisition — a
    wrapper that took the lock itself and then called the batch would nest, and
    `ledger_lock` raises on that rather than deadlocking."""
    return bool(mark_open_many(path, [dw_id], note, operation_id))


def _entry_is_open(text: str, dw_id: str) -> bool:
    """Whether `text` carries `dw_id` with a status :attr:`DWEntry.open` accepts.

    Pure, and locating the entry with :func:`_find_entry` — the same first-wins
    locator :func:`_apply_decision` and :func:`_apply_done` edit through — so
    :func:`record_decision`'s `require_open` check and its write cannot disagree
    about which entry they mean. False for a missing entry and for a status the
    format does not understand — `.open` is deliberately not ``not done``, and a
    caller that asked for an open entry gets a refusal for both."""
    entry = _find_entry(text, dw_id)
    return entry is not None and entry.open


def _apply_decision(text: str, dw_id: str, date: str, label: str, detail: str) -> str | None:
    """Insert one `decision: <date> <label> — <detail>` line *within* `text`,
    right after the entry's status line. None when the entry is missing.

    Pure — text in, text out, no `Path` and no I/O — so :func:`record_decision`
    can run it and :func:`_apply_done` against the same in-memory text inside a
    single :func:`ledger_lock` hold. Applies to a done entry as readily as an
    open one: a decision is a record of what a human chose, not a status change.

    `label` and `detail` come from a triage session's `DecisionOption`, so they
    are sanitized to one line rather than refused — see :func:`_one_line`. This
    is also where a build option's `intent` gets flattened, since it reaches the
    ledger only as `detail = option.resolution or option.intent`."""
    entry = _find_entry(text, dw_id)
    if entry is None:
        return None
    label = _one_line(label)
    # Sanitize before the emptiness test, never after: a break-only detail
    # collapses to "" and must then drop the separator with it, or the entry
    # carries a dangling `— ` promising a detail that is not there.
    detail = _one_line(detail)
    detail_part = f" — {detail}" if detail else ""
    return _insert_after_status(text, entry, f"decision: {date} {label}{detail_part}")


def record_decision(
    path: Path,
    dw_id: str,
    date: str,
    label: str,
    detail: str,
    *,
    close_note: str | None = None,
    require_open: bool = False,
) -> bool:
    """Record a human decision on one entry and, when `close_note` is given, act
    on it by flipping the entry to `status: done <date>` — both in ONE read and
    ONE atomic write. Returns True when a decision line was written, False when
    the ledger or entry is absent, or `require_open` refuses a non-open entry.

    ONE locked read->edit->write: the whole cycle runs under the cross-process
    ledger lock (#286/#469), so concurrent mutators — a second run, a sweep, the
    TUI decision modal, ``sweep --archive`` — serialize here rather than trading
    last-write-wins. That is the reason the pair is one primitive at all: as
    separate :func:`append_decision` and :func:`mark_done` calls it is two
    acquisitions with a window between them, and a rival writer landing in that
    window sees an entry whose decision says "close it" and whose status still
    says open.

    The decision line is inserted BEFORE the close is applied, which is not a
    preference: :func:`_apply_done` writes its `resolution:` line immediately
    after the status line, and :data:`_MARK_DONE_TAIL_RE` — what
    :func:`_apply_open` matches an undo marker with — anchors on exactly that
    adjacency. Applying the close first would leave the decision line between
    status and resolution and make a reopenable close unreopenable. Ordered this
    way the bytes are identical to the serial pair's.

    An already-done (or missing-status) entry skips only the close half: the
    decision line still lands, because a decision recorded on an entry someone
    else already closed is still what the human chose. `close_note` is the
    resolution note for the flip, distinct from `detail`, which is the decision's
    own rationale.

    `require_open` is the ONE exception to that rule, and it defaults to False so
    every existing caller — ``cli.cmd_decisions``, the TUI decision modal,
    :func:`~bmad_loop.decisions.apply_pre_answer`, the sweep's interactive
    decision arm — keeps exactly the behaviour above: a decision recorded on an
    entry someone else already closed still lands. Passed True by one caller,
    the sweep's DW-167 replay walk, which re-applies a `close` the run that
    answered it never got onto the ledger. That walk's premise is that the entry
    is STILL OPEN — a done entry means the effect already landed — and it takes
    an open-set snapshot before it writes; but a snapshot read outside this lock
    cannot enforce a promise the lock exists to hold, so a rival writer closing
    the entry in between produced a second `decision:` line on an entry whose
    close had already been recorded. With `require_open` the predicate and the
    mutation are ONE critical section: a third refusal state joins the two
    documented above — the entry is present but no longer open — and it returns
    False having written nothing, exactly as the other two do. The caller
    distinguishes the three for its journal row; nothing here reports which. That
    third state is the one non-write that always PAYS a lock acquisition: the
    advisory probe below knows nothing about `require_open` (it asks only what
    :func:`_apply_decision` would do), so it cannot short-circuit a still-open
    refusal the way it does a missing entry — which is correct, since the answer
    it would be short-circuiting on is exactly the one a rival writer can change.

    Precondition: `date` is ISO `YYYY-MM-DD` — one check for both halves, since
    the decision line and the close share it; anything else raises `ValueError`,
    checked before the presence short-circuit so an absent ledger cannot hide
    the bug.

    A missing ledger, and a `dw_id` no entry carries, are both answered False
    without taking the lock (#736) — there is no write to serialize, and the
    TUI decision modal reaching a stale id should not fail on an acquisition.
    A REFUSED ledger is neither: :func:`_ledger_present` raises the ``OSError``
    (DW-255), so the caller sees the refusal rather than a "no such entry" False.
    The probe runs :func:`_apply_decision`, the same helper the locked pass
    runs, which is None exactly when the entry is missing.

    The write goes through :func:`~bmad_loop.platform_util.atomic_write_text` for
    the reasons documented on :func:`mark_done_many`, plus one this sibling shares
    with it: a bare ``Path.write_text`` truncates *before* it encodes, so any
    failure between the two — an unencodable value, ``ENOSPC``, ``EIO`` — leaves a
    zero-byte ledger where every entry used to be (#328).
    """
    _require_iso_date(date)
    if not _ledger_present(path):
        # No ledger, no entry to record against — see `_mark_done_many`; a
        # refused one raises here (DW-255). Rechecked under the hold below.
        return False
    try:
        # ADVISORY pre-lock probe (#736): one read, and the same pure decision
        # the locked pass makes. Only a "would write nothing" answer is acted on
        # — the call then serializes at this read. Anything else, including any
        # fault here, falls through to the hold, which re-reads and decides.
        probe = path.read_text(encoding="utf-8")
        if _apply_decision(probe, dw_id, date, label, detail) is None:
            return False
    except Exception:  # nosec B110 - ADVISORY probe: a fault here must decide nothing
        # Neither arm of the DW-146 ledger-read contract, and deliberately
        # broader than both: this probe decides nothing on a fault, and the
        # locked read below is the REPAIR/WRITE site that does.
        pass
    with ledger_lock(path):
        # REPAIR/WRITE (DW-146): this text is edited and published below. `None`
        # is absence alone; a refusal raises out of the reader (DW-221/255).
        text = read_for_write(path)
        if text is None:
            return False
        if require_open and not _entry_is_open(text, dw_id):
            # UNDER the lock, deliberately: this is the caller's still-open
            # premise being enforced at the mutation boundary rather than by its
            # own earlier read. Refuse the way the other two states do — return
            # False, write nothing — so the caller's existing non-write arm
            # covers it without a new return shape.
            return False
        updated = _apply_decision(text, dw_id, date, label, detail)
        if updated is None:
            return False
        text = updated
        if close_note is not None:
            closed = _apply_done(text, dw_id, date, close_note)
            if closed is not None:
                text = closed
        _publish(path, text)
        return True


def append_decision(path: Path, dw_id: str, date: str, label: str, detail: str) -> bool:
    """Record a human decision on an entry without changing its status.

    The no-close case of :func:`record_decision`, which is where the lock is
    taken and the contract documented. It delegates rather than duplicating the
    read->edit->write so that one public call is exactly one acquisition."""
    return record_decision(path, dw_id, date, label, detail)


DW_ID_RE = re.compile(r"\bDW-(\d+)\b")


def next_seq(text: str) -> int:
    """The next free DW sequence number — one past the highest DW-<n> anywhere
    in the ledger (malformed entries included, so a number is never reused and
    the sweep numbering check stays satisfied)."""
    nums = [int(m.group(1)) for m in DW_ID_RE.finditer(text)]
    return (max(nums) + 1) if nums else 1


def field_line_present(body: str, field: str, value: str) -> bool:
    """True when `body` has a `field:` line whose value is exactly `value`,
    matching the shapes append_entry writes (plain, or backtick-wrapped as for
    `source_spec:`). Anchored per-line so an incidental substring elsewhere in
    the body (e.g. inside `reason:`) never counts as a match."""
    v = re.escape(value)
    return re.search(rf"(?m)^{re.escape(field)}:[ \t]*`?{v}`?[ \t]*$", body) is not None


@dataclass(frozen=True)
class EntrySpec:
    """One :func:`append_entry` call's arguments as data, for the batched writer.

    The defaults are that function's defaults, so a spec built from the same
    values produces the same entry. Frozen because :func:`append_entries`
    validates the whole sequence before it takes the lock and then trusts what it
    validated — a spec mutated in between would be written unchecked.

    ``cross_spec_dedupe`` widens the open-entry idempotence scan to match
    ``origin`` alone. It is opt-in because only producers whose origin is already
    a complete, spec-independent work identity may safely collapse rows from
    different source specs. The scan remains open-only so resolved work can be
    filed again when it recurs."""

    title: str
    origin: str
    source_spec: str
    reason: str
    location: str = "n/a"
    status: str = "open"
    severity: str | None = None
    cross_spec_dedupe: bool = False


def _apply_append(text: str, spec: EntrySpec) -> tuple[str, str | None]:
    """Append one canonical `### DW-<seq>` entry *within* `text`, returning the
    new text and the id minted — or `text` unchanged and None when an open entry
    already carries the same `origin:` marker and either the same `source_spec:`
    or an opted-in cross-spec match.

    Pure — text in, text out, no `Path` and no I/O — which is what lets
    :func:`append_entries` run it once per spec against the text as it evolves,
    inside a single :func:`ledger_lock` hold. Both halves that make a batch
    differ from a loop read that evolving text: `next_seq` mints past the entry
    the previous spec just added, so ids are sequential rather than colliding,
    and the idempotence scan sees it too, so two identical specs in one call
    dedupe against each other exactly as a serial pair would.

    Free text is sanitized (:func:`_one_line`) **before** the idempotence scan,
    which compares the caller's value against the stored one via
    :func:`field_line_present`: sanitizing afterwards would compare a raw value
    against a sanitized line, so every replay of the same multiline defer would
    miss its own entry and append another.

    The scan is deliberately open-only for both match arms: a closed entry with
    the same marker does not suppress the append, because the work has come
    back. The origin-only arm is opt-in so non-harvest producers keep the
    released exact-pair semantics."""
    given_title = bool(spec.title)
    title = _one_line(spec.title)
    origin = _one_line(spec.origin)
    source_spec = _one_line(spec.source_spec)
    reason = _one_line(spec.reason)
    location = _one_line(spec.location)
    for entry in parse_ledger(text):
        if not entry.open or not field_line_present(entry.body, "origin", origin):
            continue
        if spec.cross_spec_dedupe or field_line_present(entry.body, "source_spec", source_spec):
            return text, None
    dw_id = f"DW-{next_seq(text)}"
    if given_title and not title.strip():
        # A break-only title sanitizes to nothing, and `### DW-<n>: ` is a
        # heading `HEADING_RE`'s `(.+?)` does not match: the caller is handed an
        # id no reader can find while `next_seq` has already burned it.
        #
        # Tested with `.strip()`, not `not title`: a title of `"  "` carries no
        # break at all, so `_one_line` returns it unchanged by the byte-identity
        # fast path and it stays truthy. It parses, but renders blank in
        # `status`, `--json` and the TUI — the unidentifiable half of the same
        # problem, reached without ever touching the sanitizer.
        #
        # Scoped to a title that *had* content: an already-empty one keeps its
        # long-standing behavior, and the invariant is about non-empty values.
        title = f"(untitled {dw_id})"
    lines = [
        f"### {dw_id}: {title}",
        f"origin: {origin}",
        f"location: {location}",
        f"source_spec: `{source_spec}`",
    ]
    if spec.severity:
        lines.append(f"severity: {spec.severity}")
    lines.append(f"reason: {reason}")
    lines.append(f"status: {spec.status}")
    block = "\n".join(lines) + "\n"
    # exactly one blank line between the previous content and the new entry
    if text == "" or text.endswith("\n\n"):
        sep = ""
    elif text.endswith("\n"):
        sep = "\n"
    else:
        sep = "\n\n"
    return text + sep + block, dw_id


def _apply_appends(text: str, specs: Sequence[EntrySpec]) -> tuple[str, list[str | None]]:
    """Fold every spec through :func:`_apply_append` *within* `text`, returning
    the new text and one minted id per spec — None where the spec deduped
    against an open entry that already carries its marker.

    Pure — text in, text out, no `Path` and no I/O — and ONE body for the
    advisory pre-lock probe and the locked pass, so the two cannot drift. Each
    spec sees the text the previous one produced, which is what makes ids
    sequential and lets two identical specs in one call dedupe against each
    other; see :func:`_apply_append` for why that evolution is load-bearing."""
    minted: list[str | None] = []
    for spec in specs:
        text, dw_id = _apply_append(text, spec)
        minted.append(dw_id)
    return text, minted


def append_entries(path: Path, specs: Sequence[EntrySpec]) -> list[str | None]:
    """Append every entry in `specs` in ONE read and ONE atomic write, returning
    each spec's minted id — or None in its position when that spec deduped
    against an already-open entry. Creates the ledger (and parent dir) if it does
    not yet exist.

    A thin wrapper over :func:`append_entries_published`, for the callers that
    only need the ids. One acquisition, in the leaf.
    """
    return append_entries_published(path, specs)[0]


def append_entries_published(
    path: Path, specs: Sequence[EntrySpec]
) -> tuple[list[str | None], str | None, str | None]:
    """:func:`append_entries`, additionally handing back the text it published —
    or None when it wrote nothing, because every spec deduped or `specs` was
    empty — and the PREIMAGE it read under the lock. For an existing ledger, a
    None preimage means no locked read happened; an opted-in all-deduped batch
    can write nothing yet return the locked preimage. An absent ledger has no
    textual preimage, so it is also represented by None after a locked read.

    For a caller that has to record WHAT IT WROTE rather than what the file holds
    afterwards. Reading the ledger back after this returns is a different
    question with the same answer only when nobody else wrote in between: the
    lock is released before the read, so a concurrent mutator's bytes would be
    folded into the caller's own anchor. That matters for
    ``post_engine_ledger_digest``, whose whole job is to say "these bytes are
    ours" — counting a rival's write as ours would have the pre-harvest restore
    retract it, which is the loss this module exists to prevent (#286). Taking
    the text from inside the hold removes the window rather than narrowing it.

    That closes the window AFTER the locked read; the preimage closes the one
    BEFORE it. A rival that appended between the caller's snapshot and this
    locked read is already in the text this call re-publishes, so the published
    bytes carry it and an anchor set from them would still authorize retracting
    it. Handing the preimage back lets the caller answer the only question that
    makes the anchor safe — "is what I wrote over still what I last knew this
    file to be?" — and decline to move the anchor when it is not.

    The returned text is what was handed to
    :func:`~bmad_loop.platform_util.atomic_write_text`, so a digest of it equals
    a digest of a later ``read_text`` of the file: the writer's text mode
    translates the newlines on the way out and ``read_text`` normalizes them back
    on the way in.

    ONE locked read->edit->write: the whole cycle runs under the cross-process
    ledger lock (#286/#469), so concurrent mutators — a second run, a sweep, the
    TUI decision modal, ``sweep --archive`` — serialize here rather than trading
    last-write-wins. The hold spans every `next_seq` mint as well as every
    idempotence scan, which is what stops two concurrent appenders reading the
    same highest id and both minting it (#469).

    With every spec using the default narrow semantics, byte-identical to a
    serial :func:`append_entry` loop over the same specs, because each spec is
    applied to the text the previous one produced rather than to the text this
    call read. Opted-in specs have no serial ``append_entry`` equivalent; their
    evolving batch text additionally lets a later source spec dedupe against an
    earlier same-origin row. A naive batch gets both properties wrong.

    ALL specs are validated — the `status` and `severity` enumerations, which are
    orchestrator-owned and so raise rather than sanitize — before the lock is
    taken and before anything is written. All-or-nothing: a bad spec anywhere in
    the sequence leaves the ledger exactly as it was, rather than committing the
    prefix that happened to precede it. Validating above the lock also means a
    programmer bug reports itself without first waiting on another process.

    Nothing is written when every spec dedupes. For default narrow semantics no
    lock is taken either (#736): a replayed defer is answered from one advisory
    read that runs :func:`_apply_appends`, the same helper the locked pass runs.
    An opted-in cross-spec batch always reaches the lock, even when the advisory
    fold suppresses every spec, because an observed open twin may close before
    the authoritative decision; the locked re-read must then file the recurrence
    fresh. Deliberately NO missing-ledger guard, unlike its sibling mutators: an
    absent ledger here means CREATE, which is a write, and a write must take the
    lock.

    The write goes through :func:`~bmad_loop.platform_util.atomic_write_text` for
    the reasons documented on :func:`mark_done_many`, plus one this sibling shares
    with it: a bare ``Path.write_text`` truncates *before* it encodes, so any
    failure between the two — an unencodable value, ``ENOSPC``, ``EIO`` — leaves a
    zero-byte ledger where every entry used to be (#328).
    """
    for spec in specs:
        _require_canonical_status(spec.status)
        # The whitelist is derived from the legacy parser's alias table (defined
        # below; resolved at call time) so what this writer emits and what
        # `field_severity` normalizes to cannot drift apart.
        if spec.severity and spec.severity not in _CANONICAL_SEVERITIES:
            raise ValueError(
                f"severity must be one of {sorted(_CANONICAL_SEVERITIES)}: {spec.severity!r}"
            )
    if not specs:
        # Nothing to serialize against, so nothing to take a lock for.
        return [], None, None
    try:
        # ADVISORY pre-lock probe (#736): one read — shaped exactly like the
        # locked one, absence included — and the same pure decision the locked
        # pass makes. Only a "would write nothing" answer is acted on, and here
        # that is every spec deduping, which is also the only case where the
        # published text is the text already on disk. Anything else, including
        # any fault here, falls through to the hold, which re-reads and decides.
        probe = path.read_text(encoding="utf-8") if path.is_file() else ""
        minted = _apply_appends(probe, specs)[1]
        if all(dw_id is None for dw_id in minted) and not any(
            spec.cross_spec_dedupe for spec in specs
        ):
            # No lock was taken, so there is no locked read to report a preimage
            # from — and nothing was written for an anchor to claim either.
            return minted, None, None
    except Exception:  # nosec B110 - ADVISORY probe: a fault here must decide nothing
        # Neither arm of the DW-146 ledger-read contract, and deliberately
        # broader than both: this probe decides nothing on a fault, and the
        # locked read below is the REPAIR/WRITE site that does.
        pass
    with ledger_lock(path):
        # REPAIR/WRITE (DW-146), absence preserved: `None` still means "no
        # ledger", which the returned preimage carries to the caller's anchor.
        preimage = read_for_write(path)
        text, minted = _apply_appends(preimage or "", specs)
        if all(dw_id is None for dw_id in minted):
            return minted, None, preimage
        path.parent.mkdir(parents=True, exist_ok=True)
        _publish(path, text)
        # Both returned from INSIDE the hold: the published text by
        # construction, and the preimage it replaced — neither a read-back that a
        # rival could have moved.
        return minted, text, preimage


def append_entry(
    path: Path,
    *,
    title: str,
    origin: str,
    source_spec: str,
    reason: str,
    location: str = "n/a",
    status: str = "open",
    severity: str | None = None,
) -> str | None:
    """Append a new canonical `### DW-<seq>` entry numbered past the highest
    existing DW id, returning the new id (e.g. "DW-42").

    Idempotent: returns None without writing when an open entry already carries
    the same `origin:` marker and `source_spec:` — so re-running the same defer
    (e.g. a second sweep of the same story) never duplicates the entry. Creates
    the ledger (and parent dir) if it does not yet exist.

    The one-spec case of :func:`append_entries`, which is where the lock is taken
    and the contract documented. It delegates rather than duplicating the
    read->edit->write so that one public call is exactly one acquisition."""
    return append_entries(
        path,
        [
            EntrySpec(
                title=title,
                origin=origin,
                source_spec=source_spec,
                reason=reason,
                location=location,
                status=status,
                severity=severity,
            )
        ],
    )[0]


ARCHIVE_REL = "deferred-work-archive.md"
# The archive sibling is never locked in its own right: :func:`archive_closed`
# is the only writer, and it holds the LEDGER's :func:`ledger_lock` across both
# writes (#286/#469). Any future writer of this file must take that same lock.
# A stub left by a prior archive_closed run carries this field. The next run
# reads it to skip entries whose body has already been moved — without it,
# every run would re-archive the stub (a heading + status line) and the
# archive would accumulate duplicates.
_ARCHIVED_FIELD_RE = re.compile(r"^archived:", re.MULTILINE)

# What :func:`mark_open` leaves where that stamp was. A reopened entry is not
# archived — its body is back in the ledger — but the body the undone close
# moved out still is, and this line is what a triage session follows to it.
# Deliberately a different field name: `archived:` means "the body is
# elsewhere", which a reopened entry must not claim, and a line matching
# `_ARCHIVED_FIELD_RE` here would rebuild the exact `_STUB_BODY_RE` shape on
# the next reopenable close.
_ARCHIVED_BODY_FIELD = "archived-body:"


def _archived_line_spans(entry: DWEntry) -> list[tuple[int, int]]:
    """Body-relative spans of the entry's live ``archived:`` field lines, each
    covering the whole line including its terminating newline.

    Reads through :func:`_quoted` for the same reason every gate scan does:
    an entry documenting the archive field in a fenced example carries the
    line in column 0, right where the anchor looks, and without the fence
    check a quoted ``archived:`` would be mistaken for the real thing. The one
    place that rule is written, so the three questions asked about the field —
    is this entry archived, what does its body say apart from the stamp, and
    which bytes must a reopen rename — cannot answer it differently.

    Whole lines rather than match starts because both cutting callers remove
    the line, and a span ending at the anchor would leave the stamp's value
    behind as orphaned text.
    """
    spans: list[tuple[int, int]] = []
    for m in _ARCHIVED_FIELD_RE.finditer(entry.body):
        if _quoted(entry, m.start()):
            continue
        line_end = entry.body.find("\n", m.end())
        spans.append((m.start(), len(entry.body) if line_end == -1 else line_end + 1))
    return spans


def _is_archived(entry: DWEntry) -> bool:
    """Whether the entry carries a live ``archived:`` field line (not a quoted
    example), marking it as touched by :func:`archive_closed` — a stub in the
    live ledger, or an archived body in the archive file.
    """
    return bool(_archived_line_spans(entry))


def _body_without_archived(entry: DWEntry) -> str:
    """The entry's body with its live ``archived:`` stamps and trailing blank
    lines removed — the comparison key for :func:`archive_closed`'s
    crash-recovery skip.

    An archived twin is its ledger entry plus exactly one ``archived:`` line,
    so the two are the same content only once that line is discounted; trailing
    newlines go with it because they record where the entry sat in its file,
    not what it says. Everything else is compared verbatim, deliberately: the
    cheap wrong answer is archiving a body twice, and the expensive one is
    deciding a divergent re-closure was already saved and dropping it (#711).
    """
    body = entry.body
    for start, end in reversed(_archived_line_spans(entry)):
        body = body[:start] + body[end:]
    return body.rstrip("\n")


def _archived_stamp(entry: DWEntry) -> str | None:
    """The value of the entry's first live ``archived:`` field line, or None
    when it carries none.

    Read from an *archive* twin, this is what a stub pointing at that block
    must carry — and what :func:`mark_open` demotes into an `archived-body:`
    pointer. The archive holds several blocks per id by design, so the stamp
    narrows rather than identifies: two closures archived on one day share it,
    and the append-only file's order is the tie-break (later block, later
    closure).
    """
    spans = _archived_line_spans(entry)
    if not spans:
        return None
    start, end = spans[0]
    return entry.body[start:end].split(":", 1)[1].strip()


# Field lines a stub must carry when the archived body had them, because
# downstream readers key on them regardless of status: `gate:` (validate's
# closed-entry gate report deliberately keeps speaking), `origin:` +
# `source_spec:` (the engine's status-agnostic harvest-replay dedupe), live
# `severity:`/`priority:` metadata (a reopened stub must remain selectable by a
# severity floor), and the reopenable-close undo tail (`mark_open`'s adjacency
# requirement). The severity arm mirrors SEVERITY_FIELD_RE's accepted prefixes
# and is copied byte-for-byte; `_quoted` below excludes fenced examples.
_PRESERVED_SEVERITY_LINE = (
    r"(?i:[ \t]*(?:[-*][ \t]+)?(?:\*\*)?(?:severity|priority)[ \t]*:[ \t]*(?:\*\*)?[^\n]*)"
)
_PRESERVED_FIELD_RE = re.compile(
    rf"^(?:gate:.*|origin:.*|source_spec:.*|{_PRESERVED_SEVERITY_LINE})$", re.MULTILINE
)

# The exact stub shape :func:`archive_closed` leaves in the live ledger.
# A done entry that merely carries a hand-written `archived:` line does NOT
# match — it is a real entry, not a stub, and must still be archived.
_STUB_BODY_RE = re.compile(
    r"### .*: .*\n\n"
    r"status: done [0-9]{4}-[0-9]{2}-[0-9]{2}\n"
    # Separators mirror `_MARK_DONE_TAIL_RE`, which tolerates tabs: that regex
    # decides what `_preserved_stub_lines` copies into the stub verbatim, so a
    # stricter shape here reads a stub this module just wrote as a live entry
    # and re-archives it on every run, forever, appending nothing (#711).
    r"(?:resolution:[ \t]*[^\n]*\nresolution-undo:[ \t]*[0-9a-f]{64}[ \t]+[^\n]*\n)?"
    rf"(?:(?:(?:gate:|origin:|source_spec:)[^\n]*|{_PRESERVED_SEVERITY_LINE})\n)*"
    r"archived: [^\n]*\n"
    r"\n?"
)


def _is_stub(entry: DWEntry) -> bool:
    """Whether the entry is a stub left by a prior :func:`archive_closed` run.

    Shape-based rather than `archived:`-line-based: a done entry a human
    annotated with a stray unfenced ``archived:`` line is a real entry whose
    body still belongs in the live ledger — skipping it forever on the strength
    of one line would silently exclude it from every future archive.
    """
    return entry.done and _STUB_BODY_RE.fullmatch(entry.body.rstrip("\n") + "\n") is not None


def _preserved_stub_lines(entry: DWEntry) -> list[str]:
    """The load-bearing field lines a stub must keep from the archived body.

    Scanned fence-aware like every field read in this module: a fenced example
    documenting `origin:` is not a declaration. The undo tail is read with the
    same adjacency regex :func:`mark_open` will later use against the stub, so
    what qualifies here is exactly what remains undoable there.
    """
    lines = [
        entry.body[m.start() : m.end()]
        for m in _PRESERVED_FIELD_RE.finditer(entry.body)
        if not _quoted(entry, m.start())
    ]
    if entry.status_span is not None:
        tail = _MARK_DONE_TAIL_RE.match(entry.body, entry.status_span[1])
        if tail is not None:
            lines = [tail.group(0).lstrip("\n")] + lines
    return lines


def _close_date(entry: DWEntry) -> str | None:
    """The ISO close date from a ``done <date>`` status, or None when the
    entry is not done, is done without a date suffix, or carries a date
    that does not match the ISO ``YYYY-MM-DD`` shape.

    Entries closed with a bare ``status: done`` (no date) or a hand-edited
    non-ISO date are skipped by :func:`archive_closed`: there is no close
    date to compare against a ``--before`` cutoff, and the stub the function
    leaves in the ledger needs one to stay readable as done.
    """
    if not entry.done:
        return None
    parts = entry.status.split()
    if len(parts) != 2:  # exactly `done YYYY-MM-DD` — extra tokens are not a close date
        return None
    # Same shape check as `_require_iso_date` (well-formed regex AND a real
    # calendar day), skip-not-raise: a hand-edited close is data, not a bug.
    return _iso_date_or_none(parts[1])


def _eligible_for_archive(text: str, before: str | None) -> list[tuple[DWEntry, str]]:
    """Every entry in `text` :func:`archive_closed` would move, paired with its
    close date, in ledger order.

    Pure — text in, entries out, no `Path` and no I/O — and ONE body for the
    advisory pre-lock probe and the locked pass, so the two cannot drift. Three
    skips make up the decision: an entry that is not done, or done without a
    date, has nothing to compare or to stamp a stub with; `before` excludes
    entries closed on or after the cutoff; and a stub from a prior run is
    already archived."""
    to_archive: list[tuple[DWEntry, str]] = []
    for entry in parse_ledger(text):
        close_date = _close_date(entry)
        if close_date is None:
            continue  # not done, or done without a date
        if before is not None and close_date >= before:
            continue  # closed on or after the cutoff
        if _is_stub(entry):
            continue  # stub from a prior archive_closed run
        to_archive.append((entry, close_date))
    return to_archive


def archive_closed(
    path: Path,
    *,
    before: str | None = None,
    archive_date: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Move closed (``status: done <date>``) ledger entries to a sibling
    archive file (:data:`ARCHIVE_REL`), replacing each with a minimal stub
    that preserves the DW- id for grep and ``closes_deferred``
    cross-references.

    Returns the list of archived ids, in ledger order. ``dry_run=True``
    returns the ids that *would* be archived without writing anything.

    Each archived entry's body is preserved verbatim in the archive file,
    with an ``archived: <date>`` field line appended after the entry's status
    line. The stub left in the live ledger keeps the heading, a ``status:
    done <date>`` line (so :func:`parse_ledger` reads it as done and
    :func:`open_ids` drops it), an ``archived: <date>`` line (so a subsequent
    run skips it rather than re-archiving the stub), and the entry's
    load-bearing field lines — ``gate:``, ``origin:``/``source_spec:``, live
    ``severity:``/``priority:`` metadata, and the reopenable-close undo tail —
    because downstream readers key on those regardless of status (validate's
    closed-gate report, the engine's harvest-replay dedupe, severity selection,
    and sweep bundle rollback respectively).

    ``before`` (ISO ``YYYY-MM-DD``) archives only entries closed strictly
    *before* that date. Entries with ``status: done`` (no date) are always
    skipped — there is no close date to compare against a cutoff or to stamp
    the stub with. Open and legacy entries are never touched.

    Dates are validated with :func:`_require_iso_date` (same validation as
    the existing close-path writers), ahead of the presence short-circuit
    so a programmer bug fails the same way whether or not a ledger exists.
    Both writes — the trimmed ledger and the appended archive — go through
    :func:`atomic_write_text`, the same primitive every ledger writer uses.
    The archive file accumulates on repeat runs: new entries are appended to
    the existing file, never overwritten, and stubs from a prior run are
    skipped by their exact stub shape. A stub's ``archived:`` date names the
    archive block holding its body, so an entry recovered from a crashed run
    is stamped with the date already on that block rather than with this run's.

    The whole read->edit->write runs under the cross-process ledger lock
    (#286/#469): concurrent mutators — a second run, a sweep, the TUI decision
    modal, ``sweep --archive`` — serialize here rather than trading
    last-write-wins. ONE acquisition spans BOTH writes — the
    archive sibling has no lock of its own precisely because it is only ever
    written under its ledger's lock — and an ELIGIBLE ``dry_run`` runs inside
    the hold too, so there is one code path rather than a locked and an unlocked
    one. A run with nothing eligible is the exception, and only because it is
    not a code path at all: the advisory pre-lock probe (#736) answers it with
    the empty list before either branch is reached, so ``sweep --archive`` over
    a ledger holding nothing closed keeps reporting success where the state root
    cannot be derived or the lock cannot be taken.
    """
    if before is not None:
        _require_iso_date(before)
    if archive_date is not None:
        _require_iso_date(archive_date)
    if not _ledger_present(path):
        # No ledger means no write, and so no lock — the order
        # `sprintstatus.advance` already keeps for its own missing-board case.
        # Acquiring first would turn "there is nothing to archive", which
        # `bmad-loop sweep --archive` reports as SUCCESS, into a failure wherever
        # the state root cannot be derived: a released behavior, changed by a lock
        # taken for a file that is not there. A REFUSED ledger is not "nothing
        # to archive": the guard raises (DW-255) rather than letting `--archive`
        # report success over a file it could not read. Rechecked under the hold
        # below, deletion being able to race this answer.
        return []
    try:
        # ADVISORY pre-lock probe (#736): one read, and the same pure decision
        # the locked pass makes. Only a "would write nothing" answer is acted on
        # — the call then serializes at this read. Above the `dry_run` branch on
        # purpose, so a nothing-eligible dry run skips the lock too; an ELIGIBLE
        # dry run still runs under the hold, where the one code path is. Anything
        # else, including any fault here, falls through to that hold.
        probe = path.read_text(encoding="utf-8")
        if not _eligible_for_archive(probe, before):
            return []
    except Exception:  # nosec B110 - ADVISORY probe: a fault here must decide nothing
        # Neither arm of the DW-146 ledger-read contract, and deliberately
        # broader than both: this probe decides nothing on a fault, and the
        # locked read below is the REPAIR/WRITE site that does.
        pass
    with ledger_lock(path):
        # REPAIR/WRITE (DW-146): this text is stubbed and published below. `None`
        # is absence alone; a refusal raises out of the reader (DW-221/255).
        text = read_for_write(path)
        if text is None:
            return []
        to_archive = _eligible_for_archive(text, before)
        if not to_archive:
            return []
        archived_ids = [e.id for e, _ in to_archive]
        if dry_run:
            return archived_ids
        stamp = archive_date or calendar_date.today().isoformat()
        archive_path = path.parent / ARCHIVE_REL
        # REPAIR/WRITE (DW-146): the archive sidecar is republished with these
        # entries appended, so undecodable bytes here must escalate rather than
        # publish an archive rebuilt from "" — the same class as the ledger read
        # above, applied to the other file this mutator writes. The contract is
        # otherwise ledger-only; this sidecar is IN scope by name because it is the
        # second half of ONE locked repair/write cycle over ledger contents (the
        # entries appended here are the entries the read above found), so leaving it
        # bare would park an unattributed `UnicodeDecodeError` inside a converted
        # region. Nothing else outside the ledger is in scope.
        existing = read_for_write(archive_path) or ""
        # Append an `archived:` line after each entry's status line. The status
        # span is body-relative, so the insertion works within the body slice —
        # same offset math as `_insert_after_status`, applied to the body.
        #
        # Crash recovery: the archive is written BEFORE the ledger (see below), so
        # a crash between the two writes leaves the ledger with full entries whose
        # bodies are already in the archive. A retry must still stub those ledger
        # entries (completing the interrupted operation) but must NOT append their
        # bodies again — an append-only archive accumulating duplicates. Entries
        # whose parsed archive twin carries a live (non-fenced) ``archived:``
        # field are therefore skipped here and only replaced with stubs below.
        #
        # The twin must match in BODY, not merely in id and close date. A DW id is
        # reusable across closures (`mark_open` reopens, a re-close follows) and a
        # closed entry still accepts writes (`append_decision` does not read
        # status), so id + date names a *closure slot*, not its content: reopened
        # and re-closed the same day with a new resolution, or annotated with a
        # decision after its body was archived, the ledger entry and its twin
        # differ. Skipping on the slot alone stubbed that entry over its own
        # content while reporting the id as archived — the body reached neither
        # file (#711). A body that differs is appended instead; the archive holds
        # several blocks per id by design, and over-archiving is recoverable where
        # a silent drop is not.
        archive_blocks: list[str] = []
        already_archived = {
            e.id: ((_close_date(e), _body_without_archived(e)), _archived_stamp(e))
            for e in parse_ledger(existing)
            if _is_archived(e)
        }  # fence-aware: a quoted example in the archive is not a real body
        # A recovered entry's stub is stamped with the date already on its archived
        # body, not with this run's. The two diverge whenever the retry lands on a
        # later day than the crashed run, and the stamp is not decoration: it is
        # what picks one of an id's several archive blocks — for a reader following
        # the stub, and for the `archived-body:` pointer `mark_open` demotes that
        # stamp into, which is a reopened entry's only route back to its body
        # (#711 review). A stub naming a date no block carries resolves to nothing.
        recovered_stamps: dict[str, str] = {}
        for entry, close_date in to_archive:
            twin = already_archived.get(entry.id)
            if twin is not None and twin[0] == (close_date, _body_without_archived(entry)):
                # this closure's body is already archived (crashed prior run)
                if twin[1] is not None:
                    recovered_stamps[entry.id] = twin[1]
                continue
            body = entry.body
            assert entry.status_span is not None  # done with a date implies a status line
            pos = entry.status_span[1]
            body = body[:pos] + f"\narchived: {stamp}" + body[pos:]
            archive_blocks.append(body)
        # Appended, never prepended: for one id the file's order is closure order,
        # which is the documented tie-break when two closures were archived on the
        # same day and so carry the same stamp (#711 review).
        if archive_blocks:
            if existing == "" or existing.endswith("\n\n"):
                sep = ""
            elif existing.endswith("\n"):
                sep = "\n"
            else:
                sep = "\n\n"
            archive_content = existing + sep + "".join(archive_blocks)
        else:
            archive_content = existing  # pure crash-recovery pass: only stub the ledger
        # Replace each archived entry's span with a stub, working backwards so
        # earlier spans are unaffected by later replacements — the same
        # text-surgery pattern as `_apply_done`, applied to multiple entries.
        for entry, close_date in reversed(to_archive):
            preserved = "".join(f"{line}\n" for line in _preserved_stub_lines(entry))
            stub = (
                f"### {entry.id}: {entry.title}\n\n"
                f"status: done {close_date}\n"
                f"{preserved}"
                f"archived: {recovered_stamps.get(entry.id, stamp)}\n\n"
            )
            start, end = entry.span
            text = text[:start] + stub + text[end:]
        # Write the archive BEFORE the ledger: a crash between writes leaves the
        # archive with extra content (harmless — the archive is append-only) and
        # the ledger unchanged (safe — the bodies are still in the live file).
        # Writing the ledger first would leave stubs in the ledger with no bodies
        # in the archive — content lost.
        _publish(archive_path, archive_content)
        _publish(path, text)
        return archived_ids


# ------------------------------------------------------------------- legacy
#
# Ledgers written before the DW format (older BMAD-method projects) are
# freeform markdown: "## Deferred from: ..." sections holding id'd or
# strikethrough bullets, "### D-1.2-003: title — RESOLVED" entry headings,
# topic sections closed with "(... — DONE)". parse_legacy() reads them
# tolerantly so the TUI can display them and a sweep can migrate them; the
# strict DW contract above is untouched — legacy items have no status line
# to flip, so mark_done/open_ids never see them.

# Severity is extracted forgivingly (the ledger is LLM-written): a
# `severity:`/`priority:` field line in any case, plain or bold-bulleted
# ("- **Severity:** high"), common synonyms accepted.
SEVERITY_ALIASES = {
    "critical": "critical",
    "blocker": "critical",
    "high": "high",
    "major": "high",
    "medium": "medium",
    "med": "medium",
    "moderate": "medium",
    "low": "low",
    "minor": "low",
    "trivial": "low",
}
# What every alias above normalizes to, and so the only values `append_entry` may
# write. Derived rather than restated: a hand-copied whitelist drifts the moment
# an alias is added for a new canonical level.
_CANONICAL_SEVERITIES = frozenset(SEVERITY_ALIASES.values())

SEVERITY_FIELD_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?(?:severity|priority)[ \t]*:[ \t]*(?:\*\*)?[ \t]*"
    r"([A-Za-z][\w-]*)",
    re.IGNORECASE | re.MULTILINE,
)


def field_severity(body: str) -> str | None:
    m = SEVERITY_FIELD_RE.search(body)
    return _normalize_severity(m.group(1)) if m else None


def _normalize_severity(value: str) -> str | None:
    """Normalize one severity token for canonical and tolerant readers alike."""
    return SEVERITY_ALIASES.get(value.lower())


@dataclass(frozen=True)
class LegacyEntry:
    key: str  # stable content-derived identity, unique within the file
    id: str  # native id ("W2", "D-CAP-001", "0-1"), "" when the item has none
    title: str  # cleaned one-line title (markers/strikethrough stripped)
    done: bool
    severity: str | None  # normalized critical/high/medium/low, None unknown
    body: str  # the bullet/heading block verbatim
    section: str  # enclosing ##/### heading text, "" at top level
    span: tuple[int, int]  # char offsets in the ledger text


_DONE_WORDS = r"(?:DONE|RESOLVED|CLOSED|VERIFIED|DOCUMENTED|FIXED)"
_LINE_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
# a single whitespace-free digit-bearing token before ":" or "—" makes a
# heading an entry ("### D-CAP-001: title", "## D-8.6-001 — title");
# "## Epic 0: ..." has a space and "## 2026-06-09 — ..." is a date, so: section
_ENTRY_HEADING_RE = re.compile(r"^(~~)?([^\s:*~]*\d[^\s:*~]*)(?::[ \t]+|[ \t]+[—–][ \t]+)(.+)$")
_DATE_TOKEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SECTION_DONE_RE = re.compile(rf"(?:—|–|-|\()[ \t]*{_DONE_WORDS}\b[^)]*\)?[ \t]*$")
_TITLE_DONE_SUFFIX_RE = re.compile(rf"[ \t]*(?:—|–|-)[ \t]*{_DONE_WORDS}[ \t]*$")
_BARE_DONE_SUFFIX_RE = re.compile(rf"[ \t]*{_DONE_WORDS}\b.*$")
_BOLD_DONE_RE = re.compile(rf"\*\*{_DONE_WORDS}\b[^*]*\*\*")
_BRACKET_DONE_RE = re.compile(rf"\[{_DONE_WORDS}\]")
_DONE_PREFIX_RE = re.compile(rf"^\*\*{_DONE_WORDS}\b[^*]*\*\*:?[ \t]*")
# "- W-1.2-c — CLOSED: ..." / "CLOSED 2026-06-11 (story 1.11). ..."
_LEAD_DONE_RE = re.compile(rf"^{_DONE_WORDS}\b")
_LEAD_DONE_STRIP_RE = re.compile(
    rf"^{_DONE_WORDS}\b(?:[ \t]+\d{{4}}-\d{{2}}-\d{{2}})?(?:[ \t]*\([^)]*\))?[ \t]*[:.—–-]?[ \t]*"
)
# The flat review appender — pre-#2651 dev primitives and the attended
# `bmad-build` — writes a flat block per finding:
#   - source_spec: `spec-foo.md`
#     summary: <one sentence>
#     evidence: <why this is real>
# We recognize it so the `summary` becomes the title (not the source_spec path)
# and the entry migrates cleanly into the canonical `### DW-<seq>` shape. The
# opening line comes from the same `_FLAT_SOURCE_BODY` as FLAT_ENTRY_RE, which
# bounds canonical spans on it — see that constant for why they must not drift.
_FLAT_SOURCE_RE = re.compile(rf"^{_FLAT_SOURCE_BODY}", re.IGNORECASE)
_FLAT_SUMMARY_RE = re.compile(r"^[ \t]*summary:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)
_BULLET_RE = re.compile(r"^[-*][ \t]+(.*)$")
_ITEM_ID_RE = re.compile(
    r"^(?:\*\*)?([^\s:*~]*\d[^\s:*~]*)(?:\*\*)?(?:[ \t]*[—–][ \t]+|:[ \t]+|[ \t]+-[ \t]+)"
)
_BRACKET_TOKEN_RE = re.compile(r"\[([A-Za-z]+)[^\]]*\]")
_LEAD_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*")
_STRUCK_LINE_RE = re.compile(r"^~~(.*)~~")
_TRAIL_BRACKET_RE = re.compile(r"[ \t]*\[[^\]]+\][ \t.]*$")


def _bracket_severity(s: str) -> str | None:
    for m in _BRACKET_TOKEN_RE.finditer(s):
        sev = SEVERITY_ALIASES.get(m.group(1).lower())
        if sev:
            return sev
    return None


def _clean_title(s: str) -> str:
    return " ".join(s.replace("**", "").split())


def _item_entry(first: str, body: str, section: str, section_done: bool) -> dict:
    """Interpret one bullet item; returns the pre-key entry fields."""
    if _FLAT_SOURCE_RE.match(first):
        # flat appender block (legacy/attended era): title is the `summary`
        sm = _FLAT_SUMMARY_RE.search(body)
        summary = sm.group(1).strip() if sm else ""
        return {
            "id": "",
            "title": _clean_title(summary) if summary else _clean_title(first),
            "done": section_done,
            "severity": field_severity(body),
            "section": section,
        }
    content = first
    struck = False
    m = _STRUCK_LINE_RE.match(content)
    if m:  # "~~text~~ DONE" / "~~text~~ → resolution" on the first line
        struck = True
        content = m.group(1)
    elif content.startswith("~~") and "~~" in body[2:]:
        struck = True  # strikethrough closes on a later line
        content = content[2:]
    item_id = ""
    m = _ITEM_ID_RE.match(content)
    if m:
        item_id = m.group(1)
        content = content[m.end() :]
    done = (
        struck
        or section_done
        or bool(_LEAD_DONE_RE.match(content))
        or bool(_BOLD_DONE_RE.search(body))
        or bool(_BRACKET_DONE_RE.search(body))
    )
    content = _DONE_PREFIX_RE.sub("", content)
    content = _LEAD_DONE_STRIP_RE.sub("", content)
    while True:  # trailing "[MINOR]" / "[CLOSED]" tokens are not title text
        trimmed = _TRAIL_BRACKET_RE.sub("", content)
        if trimmed == content:
            break
        content = trimmed
    bold = _LEAD_BOLD_RE.match(content)
    if bold and len(bold.group(1).split()) >= 3:
        title = bold.group(1)  # notey: the bold phrase is the title
    else:
        title = content
    return {
        "id": item_id,
        "title": _clean_title(title),
        "done": done,
        "severity": _bracket_severity(body) or field_severity(body),
        "section": section,
    }


def _heading_entry(struck: bool, hid: str, rest: str, body: str, section: str) -> dict:
    """Interpret one '### D-1: title' entry heading (story-maker shape)."""
    title = rest
    done = struck
    m = _TITLE_DONE_SUFFIX_RE.search(title)
    if m:
        done = True
        title = title[: m.start()]
    if struck:
        title = _BARE_DONE_SUFFIX_RE.sub("", title.replace("~~", ""))
    return {
        "id": hid,
        "title": _clean_title(title),
        "done": done,
        "severity": field_severity(body) or _bracket_severity(body),
        "section": section,
    }


def parse_legacy(text: str) -> list[LegacyEntry]:
    """Extract legacy (non-DW) deferred items. Canonical DW entries and fenced
    examples are masked out first, so mixed ledgers parse both ways without
    overlap and a quoted example contributes nothing to either reading.

    The fenced half is not symmetry for its own sake. `parse_ledger` used to hand
    a quoted example over as a phantom canonical entry, whose span masked the
    example here by accident; once it stopped doing that, the same quotation
    surfaced on this side instead — a bullet or `### DW-n:` heading inside a fence
    read as a legacy finding (#514).
    """
    masked = text
    # `unclosed_hides_rest=False` for the reason the canonical side uses it: one
    # stray opener must not blank every legacy finding below it out of view. The
    # delimiter lines survive as a lone backtick or tilde plus spaces, which no
    # pattern below can start an item on — masking them too made no test disagree.
    spans = [e.span for e in parse_ledger(text)] + fenced_spans(text, unclosed_hides_rest=False)
    for s, t in spans:
        masked = masked[:s] + re.sub(r"[^\n]", " ", masked[s:t]) + masked[t:]

    found: list[tuple[dict, tuple[int, int]]] = []
    section = ""
    section_done = False
    # a done section with no items yet: emitted as its own done entry unless
    # bullets, an entry heading, or a deeper child heading claim it first
    pending: dict | None = None  # {"level", "fields", "span"}
    item: dict | None = None  # accumulating bullet or entry heading

    def close_item(end: int) -> None:
        nonlocal item
        if item is None:
            return
        body = text[item["start"] : end].rstrip()
        span = (item["start"], item["start"] + len(body))
        if item["kind"] == "item":
            fields = _item_entry(item["first"], body, item["section"], item["section_done"])
        else:
            fields = _heading_entry(
                item["struck"], item["hid"], item["rest"], body, item["section"]
            )
        found.append((fields, span))
        item = None

    def emit_pending() -> None:
        nonlocal pending
        if pending is not None:
            found.append((pending["fields"], pending["span"]))
            pending = None

    offset = 0
    for line in text.splitlines(keepends=True):
        masked_line = masked[offset : offset + len(line)].rstrip("\n")
        hm = _LINE_HEADING_RE.match(masked_line)
        if hm:
            level = len(hm.group(1))
            close_item(offset)
            if level == 1:
                emit_pending()
                section, section_done = "", False
            elif level in (2, 3):
                if pending is not None and level > pending["level"]:
                    pending = None  # a child heading: the parent is structure
                else:
                    emit_pending()
                em = _ENTRY_HEADING_RE.match(hm.group(2))
                if em and _DATE_TOKEN_RE.fullmatch(em.group(2)):
                    em = None  # "## 2026-06-09 — ..." is a dated section
                if em:
                    pending = None
                    item = {
                        "kind": "heading",
                        "start": offset,
                        "struck": bool(em.group(1)),
                        "hid": em.group(2),
                        "rest": em.group(3),
                        "section": section,
                        "section_done": section_done,
                    }
                else:
                    htext = hm.group(2)
                    struck = htext.startswith("~~") and "~~" in htext[2:]
                    section = _clean_title(htext.replace("~~", ""))
                    section_done = struck or bool(_SECTION_DONE_RE.search(htext))
                    if section_done:
                        pending = {
                            "level": level,
                            "span": (offset, offset + len(line.rstrip("\n"))),
                            "fields": {
                                "id": "",
                                "title": section,
                                "done": True,
                                "severity": None,
                                "section": "",
                            },
                        }
            offset += len(line)
            continue
        if item is not None and item["kind"] == "heading":
            if masked_line.strip() == "---" or (masked_line.strip() == "" and line.strip() != ""):
                close_item(offset)  # rule, or a masked canonical entry
            offset += len(line)
            continue
        bm = _BULLET_RE.match(masked_line)
        if bm:
            close_item(offset)
            pending = None
            item = {
                "kind": "item",
                "start": offset,
                "first": bm.group(1),
                "section": section,
                "section_done": section_done,
            }
        elif masked_line.strip() in ("", "---"):
            # a masked canonical entry reads as blank: it still bounds the item
            if masked_line.strip() == "---" or line.strip() != masked_line.strip():
                close_item(offset)
        elif masked_line[0] in " \t":
            pass  # indented continuation of the current item
        else:
            close_item(offset)  # column-0 prose ends an item, emits nothing
        offset += len(line)
    close_item(len(text))
    emit_pending()

    entries: list[LegacyEntry] = []
    counts: dict[str, int] = {}
    for fields, span in found:
        base = hashlib.sha1(
            f"{fields['section']}\0{fields['id'] or fields['title']}".encode(),
            usedforsecurity=False,  # display/identity key, not a credential
        ).hexdigest()[:10]
        n = counts.get(base, 0) + 1
        counts[base] = n
        entries.append(
            LegacyEntry(
                key=base if n == 1 else f"{base}-{n}",
                id=fields["id"],
                title=fields["title"],
                done=fields["done"],
                severity=fields["severity"],
                body=text[span[0] : span[1]],
                section=fields["section"],
                span=span,
            )
        )
    return entries


def has_legacy(text: str) -> bool:
    return bool(parse_legacy(text))
