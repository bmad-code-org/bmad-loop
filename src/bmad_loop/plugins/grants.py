"""Operator content grants for project plugins, and the pure trust verdict.

Trust is by ORIGIN, then by CONTENT (#496/#497, maintainer decision 2026-08-08,
refined 2026-10-10):

  * builtin and entry-point plugins are trusted by provenance — they ship with
    the installed package. ``[plugins] enabled`` stays the gate for a builtin's
    in-process ``[python]`` module;
  * a project-origin plugin with any executable surface (hooks, workflows,
    ``[python]``, seeds) runs only under a GRANT: an operator's record of the
    sha256 of its whole directory (`digest.read_tree`). For project origin the
    grant subsumes ``enabled``. A plugin with no executable surface loads freely.

Only `TRUSTED_SOURCES` skips the grant. Every other source — the dataclass
default ``"project"`` and anything unknown — needs one, so a new discovery
source is fail-safe until someone decides otherwise here.

Grants live OUT of the tree (the store is P2's), because the tree and its
policy.toml are agent-writable and a grant written there could be forged. This
module is the pure half: the record and its codec, and `classify`, which maps a
manifest plus its grant record to a `Verdict` without touching the store.

`classify` refuses by intent. A plugin nobody granted and nobody named in
``enabled`` is inert (REFUSED) and the run goes on; a granted plugin whose bytes
moved, or an ``enabled`` name with no grant, is fatal — the operator asked for
it, so silently dropping it would be the surprise. A never-granted plugin is
never hashed: the `live` thunk is only called when there is a record to compare.
An ``enabled`` name with no discovered plugin has no manifest to classify and is
not fatal.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from .digest import VerifiedTree, diff_files, map_digest
from .model import PluginError, PluginManifest

TRUSTED_SOURCES: frozenset[str] = frozenset({"builtin", "entry_point"})

GRANTS_VERSION = 1

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_FILE_VALUE_RE = re.compile(r"[0-9a-f]{64}(:x)?")
_RECORD_KEYS = frozenset({"path", "digest", "files", "granted_at"})

# Grants are keyed by the project's path in the state root (#572): a moved or
# re-cloned project starts with none and fails closed until re-accepted.
PER_MACHINE_NOTE = (
    "Grants are per machine and keyed by the project path; re-accept after moving the project."
)


@dataclass(frozen=True)
class GrantRecord:
    """One operator grant: the plugin dir (POSIX relpath from repo_root) and the
    digest and file map it had when the operator accepted it."""

    name: str
    path: str
    digest: str
    files: tuple[tuple[str, str], ...]
    granted_at: str

    def file_map(self) -> dict[str, str]:
        return dict(self.files)

    @classmethod
    def from_tree(cls, name: str, path: str, tree: VerifiedTree, granted_at: str) -> GrantRecord:
        fmap = tree.file_map()
        return cls(name, path, tree.digest, tuple((r, fmap[r]) for r in fmap), granted_at)


@dataclass(frozen=True)
class GrantSet:
    """The grants a launch read, frozen for the run.

    ``snapshot_root`` is where verified copies are materialized (None: no store
    was consulted). ``fault`` names a store that existed but could not be read —
    the set is then empty, and the fault is reported rather than folded away."""

    records: tuple[GrantRecord, ...] = ()
    snapshot_root: Path | None = None
    fault: str | None = None

    def get(self, name: str) -> GrantRecord | None:
        for record in self.records:
            if record.name == name:
                return record
        return None

    @classmethod
    def empty(cls) -> GrantSet:
        return cls()


def encode_grants(records: Iterable[GrantRecord]) -> str:
    """The store text for a set of records (stable: sorted keys)."""
    plugins: dict[str, dict[str, Any]] = {}
    for r in records:
        if r.name in plugins:
            raise ValueError(f"duplicate grant for plugin {r.name!r}")
        plugins[r.name] = {
            "path": r.path,
            "digest": r.digest,
            "files": dict(r.files),
            "granted_at": r.granted_at,
        }
    doc = {"version": GRANTS_VERSION, "plugins": plugins}
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _require_str(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what}: expected a non-empty string")
    return value


def _decode_record(name: str, raw: object) -> GrantRecord:
    if not isinstance(raw, dict):
        raise ValueError(f"grant {name!r}: expected an object")
    entry = cast(dict[str, object], raw)
    if entry.keys() != _RECORD_KEYS:
        raise ValueError(f"grant {name!r}: expected keys {sorted(_RECORD_KEYS)}")
    path = _require_str(entry["path"], f"grant {name!r} path")
    digest = _require_str(entry["digest"], f"grant {name!r} digest")
    if not _SHA_RE.fullmatch(digest):
        raise ValueError(f"grant {name!r} digest: not a sha256 hex digest")
    granted_at = _require_str(entry["granted_at"], f"grant {name!r} granted_at")
    raw_files = entry["files"]
    if not isinstance(raw_files, dict):
        raise ValueError(f"grant {name!r} files: expected an object")
    files: list[tuple[str, str]] = []
    for rel, value in cast(dict[object, object], raw_files).items():
        rel = _require_str(rel, f"grant {name!r} file")
        if not isinstance(value, str) or not _FILE_VALUE_RE.fullmatch(value):
            raise ValueError(f"grant {name!r} file {rel!r}: malformed entry")
        files.append((rel, value))
    if map_digest(dict(files)) != digest:
        raise ValueError(f"grant {name!r}: digest does not match its file list")
    return GrantRecord(name, path, digest, tuple(files), granted_at)


def decode_grants(text: str) -> tuple[GrantRecord, ...]:
    """Parse store text into records sorted by name; any malformed shape is ValueError."""
    doc: object = json.loads(text)  # JSONDecodeError is a ValueError
    if not isinstance(doc, dict):
        raise ValueError("grant store: expected an object")
    top = cast(dict[str, object], doc)
    if top.keys() != {"version", "plugins"}:
        raise ValueError("grant store: expected keys ['plugins', 'version']")
    version = top["version"]
    if type(version) is not int or version != GRANTS_VERSION:
        raise ValueError(f"grant store: unsupported version {version!r}")
    plugins = top["plugins"]
    if not isinstance(plugins, dict):
        raise ValueError("grant store plugins: expected an object")
    records = [
        _decode_record(_require_str(name, "grant store plugin name"), raw)
        for name, raw in cast(dict[object, object], plugins).items()
    ]
    return tuple(sorted(records, key=lambda r: r.name))


def exec_surface(m: PluginManifest) -> bool:
    """Whether the manifest can make anything run: hooks, workflows, an
    in-process module, or seeds copied into a worktree."""
    return bool(m.hooks or m.workflows or m.python is not None or m.seed_files or m.seed_globs)


def needs_grant(m: PluginManifest) -> bool:
    return m.source not in TRUSTED_SOURCES and exec_surface(m)


class TrustState(StrEnum):
    PROVENANCE = "provenance"
    NO_SURFACE = "no-surface"
    GRANTED = "granted"
    UNENABLED = "unenabled"
    REFUSED = "refused"
    FATAL_MISMATCH = "fatal-mismatch"
    FATAL_ENABLED_UNGRANTED = "fatal-enabled-ungranted"
    FATAL_UNREADABLE = "fatal-unreadable"


_ACTIVE_STATES = frozenset({TrustState.PROVENANCE, TrustState.NO_SURFACE, TrustState.GRANTED})
_FATAL_STATES = frozenset(
    {
        TrustState.FATAL_MISMATCH,
        TrustState.FATAL_ENABLED_UNGRANTED,
        TrustState.FATAL_UNREADABLE,
    }
)


@dataclass(frozen=True)
class Verdict:
    """What the gate decided for one plugin. ``changed`` lists the relpaths that
    differ from the grant (FATAL_MISMATCH only)."""

    state: TrustState
    reason: str
    changed: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE_STATES

    @property
    def fatal(self) -> bool:
        return self.state in _FATAL_STATES


def classify(
    m: PluginManifest,
    *,
    enabled: frozenset[str],
    record: GrantRecord | None,
    rel_path: str,
    live: Callable[[], VerifiedTree],
) -> Verdict:
    """The trust verdict for one discovered plugin (see module doc).

    ``rel_path`` is the plugin dir's POSIX relpath from repo_root; ``live`` reads
    and digests that dir, and is called only when a grant must be compared."""
    if m.source in TRUSTED_SOURCES:
        if m.python is not None and m.name not in enabled:
            return Verdict(TrustState.UNENABLED, "in-process module not in [plugins] enabled")
        return Verdict(TrustState.PROVENANCE, f"{m.source} plugin")
    if not exec_surface(m):
        return Verdict(TrustState.NO_SURFACE, "no executable surface")
    if record is None:
        if m.name in enabled:
            return Verdict(
                TrustState.FATAL_ENABLED_UNGRANTED, "listed in [plugins] enabled but never granted"
            )
        return Verdict(TrustState.REFUSED, "never granted")
    try:
        tree = live()
    except PluginError as e:
        return Verdict(TrustState.FATAL_UNREADABLE, str(e))
    reasons: list[str] = []
    differing: tuple[str, ...] = ()
    if record.path != rel_path:
        reasons.append(f"granted at {record.path}, now found at {rel_path}")
    if record.digest != tree.digest:
        added, removed, changed = diff_files(record.file_map(), tree.file_map())
        reasons.append(
            f"contents changed since the grant: {len(added)} added, "
            f"{len(removed)} removed, {len(changed)} changed"
        )
        differing = tuple(sorted({*added, *removed, *changed}, key=os.fsencode))
    if reasons:
        return Verdict(TrustState.FATAL_MISMATCH, "; ".join(reasons), differing)
    return Verdict(TrustState.GRANTED, f"granted sha256:{record.digest[:12]}")


def fatal_message(name: str, verdict: Verdict) -> str:
    """The operator-facing refusal for a fatal verdict, naming the verb that clears it."""
    if verdict.state is TrustState.FATAL_MISMATCH:
        return (
            f'plugin "{name}" changed since it was granted ({verdict.reason}); '
            f"review it, then run `bmad-loop plugins accept {name}`. {PER_MACHINE_NOTE}"
        )
    if verdict.state is TrustState.FATAL_ENABLED_UNGRANTED:
        return (
            f'plugin "{name}" is listed in [plugins] enabled but has no grant; '
            f"review it, then run `bmad-loop plugins enable {name}`. {PER_MACHINE_NOTE}"
        )
    if verdict.state is TrustState.FATAL_UNREADABLE:
        return (
            f'plugin "{name}" cannot be verified against its grant: {verdict.reason}; '
            f"fix the plugin dir, then run `bmad-loop plugins accept {name}`."
        )
    raise ValueError(f"not a fatal verdict: {verdict.state}")
