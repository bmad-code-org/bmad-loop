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

This module reads only; bmad-loop's write path to a plan is added beside it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol, TypeVar, runtime_checkable

from . import bmadconfig, childrun, ticketmodel
from .process_host import ProcessHostError
from .ticketmodel import Capability, FindResult, NextView, StatusView

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


# (resolved script path, st_size, st_mtime_ns) -> what the script offers. Shared
# by every TicketTree so a fresh instance per pick does not re-probe `--help`.
_CAPABILITIES: dict[tuple[str, int, int], Capability] = {}


def _running_version() -> tuple[int, int]:
    return (sys.version_info[0], sys.version_info[1])


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
        if not self.script.is_file():
            raise TicketsUnavailable(f"{self.script} is missing")
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
        try:
            st = self.script.stat()
        except OSError as e:
            raise TicketsUnavailable(f"{self.script} is missing: {e}") from e
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
