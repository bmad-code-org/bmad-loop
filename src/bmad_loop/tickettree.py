"""Typed access to a BMAD v7 project's own ``tickets.py``.

A v7 project drives its initiative ticket tree through
``_bmad/method/scripts/tickets.py`` (``next`` / ``status`` / ``find`` /
``mark``). bmad-loop never reimplements that script: :class:`TicketTree` runs
the project's installed copy as a bounded subprocess and parses its JSON into
:mod:`bmad_loop.ticketmodel` views. Plans are read back natively with
:mod:`bmad_loop.frontmatter`, never by parsing the script's output a second way.

``tickets.py`` is deterministic, stdlib-only Python — not an LLM — but it is
*project code*: it ``exec``s the project's ``_bmad/scripts/config_utils.py`` to
merge the BMad config layers. Running it is the same trust step as install's
render probe running the project's ``render_skill.py``.

Spawning. :meth:`TicketTree._call` is the single spawn point (a portability
guard holds every other module off the script). It goes through
:func:`childrun.run_argv` — no shell, stdin closed, stop-aware, whole-tree kill
on :data:`TICKETS_TIMEOUT_S` — with ``encoding="utf-8"``, because the script
writes UTF-8 on every platform and the locale codec would garble it on a
Windows cp1252 console. The launcher (:meth:`TicketTree.launcher`) is the
running interpreter as ``python -I -B -X utf8`` when the script's PEP 723
``requires-python`` is a ``>=X.Y`` this interpreter satisfies (no uv cold start
on the per-pick path, no ``__pycache__`` in the store); otherwise
``uv run --no-project`` with ``PYTHONUTF8=1``.

Errors. Every failure is a :class:`TicketsError` subclass carrying a ``kind``
token for journals and pauses:

- :class:`TicketsPinMismatch` — the live ``[core] active_initiative`` is not the
  run's pinned initiative; raised *before* anything is spawned.
- :class:`TicketsUnavailable` — the script is missing, or could not be launched
  (spawn ``OSError``, no ``uv`` on the fallback path).
- :class:`TicketsTimeout` / :class:`TicketsInterrupted` — the child was killed
  at the timeout, or a pending hard stop kept it from spawning.
- :class:`TicketsFault` — exit 1 with the script's JSON error object on stderr
  (its ``TicketError``, ``OSError`` or ``ValueError``); ``data`` carries the
  object's extra keys. :class:`TicketsNoMatch` is the subclass for "no ticket
  matches": upstream has no distinct exit code or error key for it, so it is
  recognised by the message prefix ``no ticket matches `` — a best effort that
  degrades to a plain :class:`TicketsFault` if upstream rewords it.
- :class:`TicketsStoreRefusal` — exit 2 with a JSON error object: the store is
  a tracker (or ``next`` needs ``--synced``).
- :class:`TicketsUsageError` — exit 2 with plain-text stderr: argparse refused
  the command line, i.e. the installed script does not speak this contract.
- :class:`TicketsCrashed` — any other shape (a traceback, an unexpected exit
  code, stdout that is not one JSON object).
- :class:`TicketsSchemaError` — the JSON parsed but lacks or mistypes a pinned
  field (:class:`ticketmodel.TicketsSchemaError`, re-raised as a
  :class:`TicketsError` so one ``except`` catches every tickets failure).

Writes. :meth:`TicketTree.mark` is bmad-loop's sole write path to a plan's
``status`` and ``blocked_*`` fields, and :meth:`TicketTree.restore_preimage`
the sole byte-exact restore of a pre-image the same transaction captured
(:meth:`TicketTree.discard_unbuilt_plan` deletes a plan that holds only
frontmatter, for a rollback). All three hold the plan's advisory sidecar lock
(``runs.lock_path_for(plan)``) for their whole read-decide-write-read-back,
and every status write is decided by ``statemachine.check_plan_transition``
under a :data:`~bmad_loop.statemachine.PlanWriteReason`. The lock is held across
bounded child processes only (one ``find`` and at most one ``mark``, each under
:data:`TICKETS_TIMEOUT_S`) and never across a session or a pause: the build
skill writes plan status itself, so bmad-loop never marks while a session for
that ticket is live. Two more errors belong to writes:

- :class:`TicketsLockBusy` — the plan lock could not be acquired (Windows gives
  up after about 10 s; POSIX waits).
- :class:`TicketsPlanMismatch` — ``find`` names a different plan than the one
  the caller expected, so the write could land on the wrong file.
- :class:`TicketsTransitionRefused` — the transition table refused the write
  (also a ``statemachine.PlanTransitionRefused``).
- :class:`TicketsReadBackMismatch` — the plan read back after a write does not
  hold what was written.
- :class:`TicketsPlanIOError` — bmad-loop's own read, write or delete of the
  plan file raised ``OSError``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol, TypeVar, runtime_checkable

from . import bmadconfig, childrun, frontmatter, platform_util, ticketmodel
from .process_host import ProcessHostError
from .statemachine import PlanTransitionRefused, PlanWriteReason, check_plan_transition
from .ticketmodel import Capability, FindResult, NextView, StatusView

if TYPE_CHECKING:
    from .journal import Journal

TICKETS_TIMEOUT_S = 60.0
_T = TypeVar("_T")
# How much of a child's stderr an error message quotes (its tail).
_STDERR_TAIL = 2000
_NO_MATCH_PREFIX = "no ticket matches "


class TicketsError(Exception):
    """Base of every ``tickets.py`` failure. ``kind`` is a stable token."""

    kind: ClassVar[str] = "tickets-error"


class TicketsUnavailable(TicketsError):
    kind = "unavailable"


class TicketsTimeout(TicketsError):
    kind = "timeout"


class TicketsInterrupted(TicketsError):
    kind = "interrupted"


class TicketsCrashed(TicketsError):
    kind = "crashed"


class TicketsUsageError(TicketsError):
    kind = "usage"


class TicketsStoreRefusal(TicketsError):
    kind = "store-refusal"


class TicketsFault(TicketsError):
    """Exit 1 with the script's JSON error object on stderr."""

    kind = "fault"

    def __init__(self, message: str, data: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data: dict[str, object] = dict(data or {})


class TicketsNoMatch(TicketsFault):
    """A :class:`TicketsFault` whose message starts ``no ticket matches `` (by
    prefix only — see the module docstring)."""

    kind = "no-match"


class TicketsPinMismatch(TicketsError):
    """The live active initiative is not the pinned one. ``live`` is None when no
    initiative is active (or the config value is unusable; ``detail`` says)."""

    kind = "pin-mismatch"

    def __init__(self, pinned: str, live: str | None, detail: str = "") -> None:
        shown = "none" if live is None else repr(live)
        super().__init__(
            f"active initiative is {shown}, but this run is pinned to {pinned!r}"
            + (f" ({detail})" if detail else "")
        )
        self.pinned = pinned
        self.live = live
        self.detail = detail


class TicketsSchemaError(TicketsError, ticketmodel.TicketsSchemaError):
    """A pinned field is missing or mistyped: version skew. Also an instance of
    :class:`ticketmodel.TicketsSchemaError`."""

    kind = "schema"


class TicketsLockBusy(TicketsError):
    kind = "lock-busy"


class TicketsPlanMismatch(TicketsError):
    """``find`` resolved the ticket to a plan other than the expected one."""

    kind = "plan-mismatch"


class TicketsTransitionRefused(TicketsError, PlanTransitionRefused):
    """The plan transition table refused a write; nothing was written. Also an
    instance of :class:`statemachine.PlanTransitionRefused`."""

    kind = "refused"


class TicketsReadBackMismatch(TicketsError):
    """The plan read back after a write does not hold what was written."""

    kind = "read-back"


class TicketsPlanIOError(TicketsError):
    """bmad-loop's own native read, write or delete of a plan raised ``OSError``."""

    kind = "plan-io"


@dataclass(frozen=True)
class PlanMark:
    """What one :meth:`TicketTree.mark` or :meth:`TicketTree.discard_unbuilt_plan`
    did. ``before``/``after`` are the plan's normalized ``status`` (``""`` when
    unset or absent); ``outcome`` is ``written``, ``noop`` or ``discarded``;
    ``created`` is the script's own report (always False: bmad-loop never marks a
    ticket without a plan)."""

    ref: str
    plan: Path
    before: str
    after: str
    outcome: str
    created: bool = False


@runtime_checkable
class TicketTreeLike(Protocol):
    """What the engine needs from a ticket tree; :class:`TicketTree` is the real
    one and ``tests/conftest.py::FakeTicketTree`` the in-memory one every engine
    test injects."""

    @property
    def project_root(self) -> Path: ...

    @property
    def pinned_initiative(self) -> str | None: ...

    def capability(self) -> Capability: ...

    def next(self) -> NextView: ...

    def status(self, folder: Path | None = None) -> StatusView: ...

    def find(self, ref: str) -> FindResult: ...

    def mark(
        self,
        ref: str,
        target: str,
        *,
        reason: PlanWriteReason,
        expected_plan: Path,
        blocked: str | None = None,
        journal: Journal,
    ) -> PlanMark: ...

    def discard_unbuilt_plan(self, ref: str, expected_plan: Path, journal: Journal) -> PlanMark: ...

    def restore_preimage(self, plan: Path, data: bytes | None, journal: Journal) -> None: ...


# (resolved script path, st_size, st_mtime_ns) -> what the script offers. Shared
# by every TicketTree so a fresh instance per pick does not re-probe `--help`.
_CAPABILITIES: dict[tuple[str, int, int], Capability] = {}


def _running_version() -> tuple[int, int, int]:
    return (sys.version_info[0], sys.version_info[1], sys.version_info[2])


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _STDERR_TAIL else "…" + text[-_STDERR_TAIL:]


def _json_object(text: str) -> dict[str, object] | None:
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


class TicketTree:
    """One project's ticket tree, read through its installed ``tickets.py``.

    ``project_root`` is passed to the script as ``--project-root`` (the main
    checkout, or a worktree mount's project). ``script`` defaults to the
    installed path; tests inject a fake. ``pinned_initiative`` is the run's
    pinned ``RunState.initiative``: when set, every tree read first checks the
    live ``[core] active_initiative`` and refuses on a difference, so a run never
    reads (or later writes) another initiative's tree. None means no pin
    (validate, preflight)."""

    def __init__(
        self,
        project_root: Path,
        script: Path | None = None,
        pinned_initiative: str | None = None,
        *,
        timeout: float = TICKETS_TIMEOUT_S,
    ) -> None:
        self._project_root = Path(project_root)
        self.script = (
            Path(script) if script is not None else self._project_root / bmadconfig.V7_MARKER_REL
        )
        self._pinned_initiative = pinned_initiative
        self.timeout = timeout

    @property
    def project_root(self) -> Path:
        return self._project_root

    @property
    def pinned_initiative(self) -> str | None:
        return self._pinned_initiative

    # ----------------------------------------------------------- launch

    def _script_stat(self) -> os.stat_result:
        """The script's stat, every failure typed :class:`TicketsUnavailable` —
        ``Path.is_file`` would raise a non-"not there" ``OSError`` raw before 3.14
        and past callers that catch only :class:`TicketsError`."""
        try:
            st = self.script.stat()
        except OSError as e:
            raise TicketsUnavailable(f"{self.script} is missing: {e}") from e
        if not stat.S_ISREG(st.st_mode):
            raise TicketsUnavailable(f"{self.script} is missing")
        return st

    def _script_text(self) -> str:
        try:
            return self.script.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise TicketsUnavailable(f"cannot read {self.script}: {e}") from e

    def launcher(self) -> tuple[list[str], dict[str, str] | None]:
        """The argv prefix that runs the script, and the child environment (None
        inherits). The running interpreter when the script's ``requires-python``
        allows it, else ``uv run --no-project`` (uv reads the header itself)."""
        spec = ticketmodel.parse_requires_python(self._script_text())
        if ticketmodel.requires_python_satisfied(spec, _running_version()):
            return [sys.executable, "-I", "-B", "-X", "utf8"], None
        uv = shutil.which("uv")
        if uv is None:
            raise TicketsUnavailable(
                f"{self.script.name} needs Python {spec or '(unspecified)'} and `uv` is not on PATH"
            )
        return [uv, "run", "--no-project"], {**os.environ, "PYTHONUTF8": "1"}

    def _check_pin(self) -> None:
        pinned = self._pinned_initiative
        if pinned is None:
            return
        try:
            live = bmadconfig.active_initiative(self._project_root)
        except bmadconfig.BmadConfigError as e:
            raise TicketsPinMismatch(pinned, None, str(e)) from e
        if live != pinned:
            raise TicketsPinMismatch(pinned, live)

    def _call(
        self, tail: Sequence[str], *, verb: str, json_out: bool = True
    ) -> dict[str, object] | str:
        """Run ``tickets.py --project-root <root> <tail…>`` — the module's single
        spawn point — and return its stdout JSON object (``json_out``) or text.
        Tree reads (``json_out``) check the initiative pin first."""
        if json_out:
            self._check_pin()
        self._script_stat()
        prefix, env = self.launcher()
        argv = [*prefix, str(self.script), "--project-root", str(self._project_root), *tail]
        try:
            run = childrun.run_argv(
                argv, cwd=self._project_root, timeout=self.timeout, env=env, encoding="utf-8"
            )
        except (OSError, ValueError, subprocess.SubprocessError, ProcessHostError) as e:
            raise TicketsUnavailable(f"cannot run {self.script.name} {verb}: {e}") from e
        return _result(run, verb, json_out=json_out)

    # ----------------------------------------------------------- reads

    def capability(self) -> Capability:
        """The verbs and ``mark`` statuses the installed script offers, from its
        ``--help`` output. Cached on the script's (path, size, mtime), so a
        reinstalled script is probed again and an unchanged one costs no spawn."""
        st = self._script_stat()
        key = (str(self.script.resolve()), st.st_size, st.st_mtime_ns)
        cached = _CAPABILITIES.get(key)
        if cached is not None:
            return cached
        top = self._call(["--help"], verb="--help", json_out=False)
        mark = self._call(["mark", "--help"], verb="mark --help", json_out=False)
        assert isinstance(top, str) and isinstance(mark, str)
        cap = ticketmodel.parse_help(
            top, mark, requires_python=ticketmodel.parse_requires_python(self._script_text())
        )
        _CAPABILITIES[key] = cap
        return cap

    def next(self) -> NextView:
        """``next`` on the active initiative (plus the backlog), as build-auto runs it."""
        return _parsed(ticketmodel.parse_next, self._call(["next"], verb="next"))

    def status(self, folder: Path | None = None) -> StatusView:
        """``status`` on ``folder``, or on the active initiative (plus the backlog)."""
        tail = ["status"] if folder is None else ["status", str(folder)]
        return _parsed(ticketmodel.parse_status, self._call(tail, verb="status"))

    def find(self, ref: str) -> FindResult:
        """``find <ref>`` on the active initiative, then the backlog."""
        # `--` keeps a ref that starts with `-` from reading as an option.
        return _parsed(ticketmodel.parse_find, self._call(["find", "--", ref], verb="find"))

    # ----------------------------------------------------------- writes

    @contextmanager
    def _plan_lock(self, plan: Path) -> Iterator[None]:
        """The plan's advisory sidecar lock. Only the ACQUISITION maps to
        :class:`TicketsLockBusy`; a fault inside the locked body is never
        reported as contention."""
        from . import runs  # lazy: runs imports most of the package

        with ExitStack() as stack:
            try:
                stack.enter_context(platform_util.file_lock(runs.lock_path_for(plan)))
            except platform_util.LockUnavailableError as e:
                raise TicketsLockBusy(f"the lock on {plan} is held elsewhere: {e}") from e
            yield

    def _locate(self, ref: str, expected_plan: Path) -> None:
        """``find`` the ticket under the lock and refuse unless it names
        ``expected_plan``: the lock and the write must be on one file."""
        found = Path(self.find(ref).plan)
        if platform_util.resolve_or_lexical(found) != platform_util.resolve_or_lexical(
            expected_plan
        ):
            raise TicketsPlanMismatch(f"{ref}: find names plan {found}, expected {expected_plan}")

    def mark(
        self,
        ref: str,
        target: str,
        *,
        reason: PlanWriteReason,
        expected_plan: Path,
        blocked: str | None = None,
        journal: Journal,
    ) -> PlanMark:
        """Set ``ref``'s plan ``status`` to ``target`` (``blocked`` with the park
        reason ``blocked``) through ``tickets.py mark`` — bmad-loop's sole write
        path to a plan's ``status``/``blocked_*``.

        Under the plan lock: ``find`` and the ``expected_plan`` cross-check, the
        plan's own fields read natively, ``check_plan_transition``, the ``mark``
        child (skipped for a no-op), and a native read-back that must hold
        ``target`` and the requested reason. Every call journals one
        ``ticket-mark`` (outcome ``written``, ``noop``, ``refused`` — the table or
        the cross-check said no — or ``failed``) and every failure raises a
        :class:`TicketsError`."""
        plan = Path(expected_plan)
        before = after = ""
        try:
            with self._plan_lock(plan):
                self._locate(ref, plan)
                has_plan, before, blocked_now, blocked_at = _plan_fields(plan)
                after = before
                try:
                    write = check_plan_transition(
                        ref,
                        before,
                        target,
                        reason,
                        has_plan=has_plan,
                        blocked_reason=blocked_now,
                        blocked=blocked,
                        blocked_at=blocked_at,
                    )
                except PlanTransitionRefused as e:
                    raise TicketsTransitionRefused(
                        e.ref, e.current, e.target, e.reason, e.why
                    ) from e
                if not write:
                    result = PlanMark(ref, plan, before, before, "noop")
                else:
                    flags = ["--blocked", blocked] if blocked is not None else []
                    doc = self._call(["mark", *flags, "--", ref, target], verb="mark")
                    report = _parsed(ticketmodel.parse_mark, doc)
                    _, after, blocked_after, blocked_at_after = _plan_fields(plan)
                    if (
                        after != target
                        or blocked_after != (blocked or "")
                        or (blocked is None and blocked_at_after)
                    ):
                        raise TicketsReadBackMismatch(
                            f"{ref}: {plan} reads back status {after!r} (reason "
                            f"{blocked_after!r}, blocked_at {blocked_at_after!r}) "
                            f"after marking {target!r}"
                        )
                    result = PlanMark(ref, plan, before, after, "written", report.created)
        except TicketsError as e:
            if not isinstance(e, TicketsTransitionRefused | TicketsPlanMismatch):
                after = _status_or_unknown(plan)
            _journal_mark(
                journal,
                ref=ref,
                reason=reason,
                before=before,
                after=after,
                outcome=_failure_outcome(e),
                plan=plan,
            )
            raise
        _journal_mark(
            journal,
            ref=ref,
            reason=reason,
            before=result.before,
            after=result.after,
            outcome=result.outcome,
            plan=plan,
            created=result.created,
        )
        return result

    def discard_unbuilt_plan(self, ref: str, expected_plan: Path, journal: Journal) -> PlanMark:
        """Delete ``ref``'s plan when it holds only frontmatter — a rollback of a
        ticket whose build never wrote a body. Same lock and cross-check as
        :meth:`mark`; the plan's status must be one ``rollback`` may move. No plan
        is a no-op; a plan with a body is refused (re-arm it with
        ``mark(..., reason="rollback")`` instead). Journals ``ticket-mark`` with
        outcome ``discarded``."""
        plan = Path(expected_plan)
        before = ""
        try:
            with self._plan_lock(plan):
                self._locate(ref, plan)
                has_plan, before, blocked_now, _ = _plan_fields(plan)
                if not has_plan:
                    result = PlanMark(ref, plan, "", "", "noop")
                else:
                    why = ""
                    try:
                        check_plan_transition(
                            ref,
                            before,
                            "ready-for-dev",
                            "rollback",
                            has_plan=True,
                            blocked_reason=blocked_now,
                        )
                        # `utf-8-sig`, as `_plan_fields` reads it: a BOM ahead of
                        # `---` would otherwise read as a body.
                        if not frontmatter.is_frontmatter_only(
                            plan.read_bytes().decode("utf-8-sig")
                        ):
                            why = "the plan holds more than frontmatter"
                    except PlanTransitionRefused as e:
                        why = e.why
                    except (OSError, UnicodeDecodeError) as e:
                        why = f"the plan is unreadable: {e}"
                    if why:
                        raise TicketsTransitionRefused(ref, before, "", "rollback", why)
                    try:
                        plan.unlink()
                    except OSError as e:
                        raise TicketsPlanIOError(f"{ref}: cannot delete {plan}: {e}") from e
                    result = PlanMark(ref, plan, before, "", "discarded")
        except TicketsError as e:
            _journal_mark(
                journal,
                ref=ref,
                reason="rollback",
                before=before,
                after=before,
                outcome=_failure_outcome(e),
                plan=plan,
            )
            raise
        _journal_mark(
            journal,
            ref=ref,
            reason="rollback",
            before=result.before,
            after=result.after,
            outcome=result.outcome,
            plan=plan,
        )
        return result

    def restore_preimage(self, plan: Path, data: bytes | None, journal: Journal) -> None:
        """Put ``plan`` back to the exact bytes a transaction captured before
        writing it (``None``: the plan did not exist, so remove it). The sole
        byte-exact restore of a plan, under the same lock as :meth:`mark`, for
        undoing a write the same transaction made — never for a status change of
        its own. No child is spawned. The bytes are read back and must match;
        journals ``ticket-plan-restored``."""
        plan = Path(plan)
        before = after = "?"
        restored = False
        try:
            with self._plan_lock(plan):
                before = _status_or_unknown(plan)
                try:
                    if data is None:
                        plan.unlink(missing_ok=True)
                        restored = not plan.exists()
                    else:
                        platform_util.atomic_write_bytes(plan, data)
                        restored = plan.read_bytes() == data
                except OSError as e:
                    raise TicketsPlanIOError(f"cannot restore {plan}: {e}") from e
                after = _status_or_unknown(plan)
        finally:
            journal.append(
                "ticket-plan-restored",
                plan=str(plan),
                before=before,
                after=after,
                outcome="restored" if restored else "failed",
            )
        if not restored:
            raise TicketsReadBackMismatch(f"{plan} does not read back as the restored pre-image")


def _plan_fields(plan: Path) -> tuple[bool, str, str, str]:
    """``(exists, status, blocked_reason, blocked_at)`` of a plan, read natively.
    ``status`` is ``frontmatter.status_of``'s normalized token — states are
    compared that way, never as bytes (the script's quoting of values may
    change). A leading UTF-8 byte-order mark is skipped, as the script keeps one
    it finds. An ``OSError`` from the existence probe or the read (other than
    the plan, or a parent, not existing) raises :class:`TicketsPlanIOError`, so
    it reaches the callers' ``ticket-mark`` failure record — ``Path.is_file``
    would raise it raw before 3.14 and answer "no plan" from 3.14 on."""
    try:
        is_file = stat.S_ISREG(plan.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        is_file = False
    except OSError as e:
        raise TicketsPlanIOError(f"cannot stat {plan}: {e}") from e
    if not is_file:
        return False, "", "", ""
    try:
        text = plan.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        text = ""  # no readable frontmatter, as `frontmatter.read_frontmatter` degrades
    except OSError as e:
        raise TicketsPlanIOError(f"cannot read {plan}: {e}") from e
    fm = frontmatter.parse_frontmatter(text)
    raw, at = fm.get("blocked_reason", ""), fm.get("blocked_at", "")
    return (
        True,
        frontmatter.status_of(fm),
        "" if raw is None else str(raw),
        "" if at is None else str(at),
    )


def _status_or_unknown(plan: Path) -> str:
    """The plan's status for a journal record; ``"?"`` when it cannot be read."""
    try:
        return _plan_fields(plan)[1]
    except TicketsPlanIOError:
        return "?"


def _failure_outcome(error: TicketsError) -> str:
    return (
        "refused" if isinstance(error, TicketsTransitionRefused | TicketsPlanMismatch) else "failed"
    )


def _journal_mark(
    journal: Journal,
    *,
    ref: str,
    reason: str,
    before: str,
    after: str,
    outcome: str,
    plan: Path,
    created: bool = False,
) -> None:
    journal.append(
        "ticket-mark",
        ref=ref,
        reason=reason,
        before=before,
        after=after,
        outcome=outcome,
        created=created,
        plan=str(plan),
    )


def _parsed(parse: Callable[[object], _T], doc: dict[str, object] | str) -> _T:
    try:
        return parse(doc)
    except ticketmodel.TicketsSchemaError as e:
        raise TicketsSchemaError(e.command, e.field, e.detail) from e


def _result(run: childrun.ChildRun, verb: str, *, json_out: bool) -> dict[str, object] | str:
    """Map one finished child onto the module's error contract."""
    if run.interrupted:
        raise TicketsInterrupted(f"tickets.py {verb}: interrupted by a stop request")
    if run.timed_out:
        raise TicketsTimeout(f"tickets.py {verb}: no answer within the timeout")
    rc = run.returncode
    if rc == 0:
        if not json_out:
            return run.stdout
        doc = _json_object(run.stdout)
        if doc is None:
            raise TicketsCrashed(
                f"tickets.py {verb}: stdout is not one JSON object: {_tail(run.stdout)}"
            )
        return doc
    err = _json_object(run.stderr)
    message = err.get("error") if err is not None else None
    if rc == 2:
        if isinstance(message, str):
            raise TicketsStoreRefusal(f"tickets.py {verb}: {message}")
        raise TicketsUsageError(f"tickets.py {verb}: refused the command line: {_tail(run.stderr)}")
    if rc == 1 and isinstance(message, str) and err is not None:
        data = {k: v for k, v in err.items() if k != "error"}
        cls = TicketsNoMatch if message.startswith(_NO_MATCH_PREFIX) else TicketsFault
        raise cls(message, data)
    raise TicketsCrashed(f"tickets.py {verb}: exit {rc}: {_tail(run.stderr)}")


if TYPE_CHECKING:
    _conforms: TicketTreeLike = TicketTree(Path("."))
