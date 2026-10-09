"""Seed and read a coding CLI's home-level workspace-trust allowlist (DW-390).

agy (Antigravity CLI) gates every workspace on ``trustedWorkspaces`` in
``~/.gemini/antigravity-cli/settings.json`` with EXACT-path matching, and blocks
on an interactive trust dialog until it is answered. Under
``isolation = "worktree"`` every unit runs in a fresh path, so without a grant
every session hangs on that dialog until the session timeout. A profile that
declares ``[workspace_trust]`` (:class:`~.adapters.profile.WorkspaceTrustSpec`)
lets provisioning append the worktree to that list before any session launches.

**Confinement.** The one file touched is the declared ``~/``-anchored path (the
profile validator refuses anything else), and within it only the one declared
TOP-LEVEL key; every other key, and key order, is preserved. The write is atomic
(:func:`~.platform_util.atomic_write_text` defaults: permission bits kept, a
symlinked settings file followed) and leaves no lock or temp file beside it.

**Root-trust rule.** ``.bmad-loop/profiles/*.toml`` arrives with a clone, so a
declared home path is untrusted input. A worktree's mount project (its session
cwd, DW-484) is seeded ONLY when the main project (the in-place session cwd) is
already in the same list, under its as-passed or resolved spelling: the worktree
inherits trust the operator granted and never creates trust from nothing. Root not trusted, or file/key missing, is
a reported outcome (the caller journals it), not a fault.

**Faults.** A malformed file (not JSON, not an object, the key present but not a
list of strings), a read fault other than absence, or a write fault raises
:class:`WorkspaceTrustError` from :func:`seed` — repair writes must raise.
:func:`trust_status` is the read-only observation twin and folds the same faults
into an explicit ``unverifiable`` status. Neither returns a raw path in a reason,
so the probe can render reasons without leaking the workspace location.

Read-modify-write is serialized in-process by a module lock and the entry is
re-read after the write to confirm it landed, narrowing (not closing) the window
for a concurrent agy write. Entries accumulate one per unit: removal on worktree
teardown is out of scope.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Literal

from .adapters.profile import WorkspaceTrustSpec
from .platform_util import atomic_write_text

SeedOutcome = Literal["seeded", "present", "root-untrusted"]
TrustStatus = Literal["trusted", "untrusted", "unverifiable"]

_LOCK = threading.Lock()


class WorkspaceTrustError(Exception):
    """The trust settings file cannot be safely read or updated."""


def settings_file(spec: WorkspaceTrustSpec) -> Path:
    """The absolute settings file ``spec`` names. ``settings_path`` is validated
    ``~/``-prefixed at profile load, so this is always a path under home.
    Raises :class:`WorkspaceTrustError` when no home directory can be determined
    (``Path.home()`` raises RuntimeError with HOME/USERPROFILE unset)."""
    try:
        home = Path.home()
    except RuntimeError as e:
        raise WorkspaceTrustError(
            f"cannot determine the home directory for {spec.settings_path}"
        ) from e
    return home / spec.settings_path[2:]


def _spellings(path: Path) -> set[str]:
    """The as-passed (absolutized) and resolved spellings of ``path``; a path the
    OS cannot resolve contributes only its lexical form."""
    out = {str(path.absolute())}
    try:
        out.add(str(path.resolve()))
    except (OSError, RuntimeError, ValueError):
        pass
    return out


def _read_entries(spec: WorkspaceTrustSpec) -> tuple[dict[str, object], list[str]] | str:
    """The parsed document and the key's entries, or a path-free reason string
    when the file or key is absent. Raises :class:`WorkspaceTrustError` on a
    malformed file or a read fault."""
    path = settings_file(spec)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"{spec.settings_path} does not exist"
    except UnicodeDecodeError as e:
        raise WorkspaceTrustError(f"{spec.settings_path} is not valid UTF-8") from e
    except OSError as e:
        raise WorkspaceTrustError(f"{spec.settings_path} is unreadable ({type(e).__name__})") from e
    try:
        doc = json.loads(text)
    # ValueError, not just JSONDecodeError: an integer past the int-str digit
    # limit raises a plain ValueError.
    except (ValueError, RecursionError) as e:
        raise WorkspaceTrustError(f"{spec.settings_path} is not valid JSON") from e
    if not isinstance(doc, dict):
        raise WorkspaceTrustError(f"{spec.settings_path} is not a JSON object")
    if spec.key not in doc:
        return f"{spec.settings_path} has no {spec.key!r} key"
    entries = doc[spec.key]
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise WorkspaceTrustError(f"{spec.settings_path} key {spec.key!r} is not a list of strings")
    return doc, entries


def trust_status(spec: WorkspaceTrustSpec, workspace: Path) -> tuple[TrustStatus, str]:
    """Read-only: is ``workspace`` (as-passed or resolved) in the declared list?

    Returns ``(status, reason)`` — ``trusted``, ``untrusted`` (file, key or entry
    absent) or ``unverifiable`` (malformed or unreadable file). The reason never
    carries the workspace path."""
    try:
        read = _read_entries(spec)
    except WorkspaceTrustError as e:
        return "unverifiable", str(e)
    if isinstance(read, str):
        return "untrusted", read
    _doc, entries = read
    if _spellings(workspace) & set(entries):
        return "trusted", f"listed in {spec.settings_path} {spec.key!r}"
    return "untrusted", f"not listed in {spec.settings_path} {spec.key!r}"


def seed(
    spec: WorkspaceTrustSpec, workspace: Path, *, trusted_root: Path
) -> tuple[SeedOutcome, str]:
    """Append ``workspace`` (resolved spelling) to the declared list, only when
    ``trusted_root`` is already in it. Idempotent: already present (either
    spelling) writes nothing.

    Returns ``(outcome, reason)``: ``seeded``, ``present``, or ``root-untrusted``
    (root absent, or file/key missing — nothing written). Raises
    :class:`WorkspaceTrustError` on a malformed file, a read or write fault, or a
    post-write re-read that does not show the entry."""
    try:
        target = str(workspace.resolve())
    except (OSError, RuntimeError, ValueError) as e:
        raise WorkspaceTrustError(f"cannot resolve the workspace path: {e}") from e
    with _LOCK:
        read = _read_entries(spec)
        if isinstance(read, str):
            return "root-untrusted", read
        doc, entries = read
        if not _spellings(trusted_root) & set(entries):
            return (
                "root-untrusted",
                f"the project root is not listed in {spec.settings_path} {spec.key!r}",
            )
        if _spellings(workspace) & set(entries):
            return "present", f"already listed in {spec.settings_path} {spec.key!r}"
        doc[spec.key] = [*entries, target]
        try:
            atomic_write_text(
                settings_file(spec), json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
            )
        except (OSError, ValueError) as e:
            raise WorkspaceTrustError(f"cannot write {spec.settings_path}: {e}") from e
        confirm = _read_entries(spec)
        if isinstance(confirm, str) or target not in confirm[1]:
            raise WorkspaceTrustError(
                f"the worktree entry did not persist in {spec.settings_path} {spec.key!r}"
            )
    return "seeded", f"appended to {spec.settings_path} {spec.key!r}"
