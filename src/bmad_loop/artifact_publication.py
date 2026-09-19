"""Explicit bundle deliverable byte authority, independent of artifact-only receipts.

A complete pre-execution inventory grants comparison authority, never selection
authority. The accepted spec selects exact files; ignored bytes are frozen as base64
snapshots while tracked and pending-tracked bytes are bound to Git-normalized blob
identities and checked in the final index. Publication is replayable after any partial
write because each ignored destination must still equal its baseline or intended bytes.
Files are created privately (0600); modes and xattrs are not preserved. The
check after staging is not a lock or atomic compare-and-swap: a noncooperating
filesystem writer can still race the final check and replacement. On platforms
without descriptor-relative reads, source and destination reads plus destination
pathname-identity observations use the checked fallback and retain its check/read
race.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import BinaryIO, Literal, NamedTuple

from . import verify
from .bmadconfig import ProjectPaths
from .frontmatter import parse_frontmatter
from .model import StoryTask
from .platform_util import (
    DIR_FD_ANCHORED_WRITES,
    atomic_write_bytes_confined,
    has_parent_ref,
    names_tree_root,
    names_win32_alias,
    open_dir_confined,
)


class PublicationError(Exception):
    """Publication refused; the mounted source and persisted payload must survive."""


class PublicationSizeError(PublicationError):
    """A measured ignored-file payload exceeded a configured admission limit."""

    def __init__(
        self,
        cause: Literal["file-limit", "payload-limit"],
        measured_bytes: int,
        limit_bytes: int,
        *,
        path: Path | None = None,
        at_least: bool = False,
    ) -> None:
        self.cause = cause
        self.measured_bytes = measured_bytes
        self.limit_bytes = limit_bytes
        self.measurement_is_lower_bound = at_least
        measurement = f"at least {measured_bytes}" if at_least else str(measured_bytes)
        if cause == "file-limit":
            message = (
                f"artifact deliverable exceeds per-file publication limit: {path}; "
                f"measured {measurement} bytes, limit {limit_bytes} bytes"
            )
        else:
            location = f" while reading {path}" if path is not None else ""
            message = (
                f"artifact payload exceeds aggregate publication limit{location}; "
                f"measured {measurement} bytes, limit {limit_bytes} bytes"
            )
        super().__init__(message)


DEFAULT_FILE_MAX_BYTES = 5 * 1_048_576
DEFAULT_PAYLOAD_MAX_BYTES = 10 * 1_048_576
_BOUNDED_READ_CHUNK_BYTES = 64 * 1024


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or PureWindowsPath(value).drive
        or any(c in value for c in '\\*?[]<>:"|')
        or any(ord(c) < 32 for c in value)
        or names_win32_alias(value)
        or names_tree_root(value)
        or any(p in ("", ".", "..") for p in value.split("/"))
    ):
        raise PublicationError(f"invalid artifact deliverable path: {value!r}")
    if value.casefold() in ("deferred-work.md", "sprint-status.yaml"):
        raise PublicationError(f"reserved orchestrator artifact: {value}")
    return value


def _confined(root: Path, path: Path) -> None:
    """Reject links at every component, including the configured root."""
    if has_parent_ref(path):
        raise PublicationError(f"artifact path contains parent traversal: {path}")
    if not path.is_relative_to(root):
        raise PublicationError(f"artifact is outside {root}: {path}")
    for part in (path, *path.parents):
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise PublicationError(f"artifact path is a symlink: {part}")
        if part != path and not stat.S_ISDIR(mode):
            raise PublicationError(f"artifact parent is not a directory: {part}")


def _read_bytes(stream: BinaryIO, max_bytes: int | None) -> bytes:
    """Read through fixed-size requests, stopping one byte beyond a bound."""
    if max_bytes is None:
        return stream.read()
    remaining = max_bytes + 1
    chunks: list[bytes] = []
    while remaining:
        chunk = stream.read(min(_BOUNDED_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@contextmanager
def _open_regular(root: Path, path: Path) -> Iterator[BinaryIO | None]:
    """Open one confined regular file without following its leaf on POSIX."""
    _confined(root, path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        yield None
        return
    if not stat.S_ISREG(mode):
        raise PublicationError(f"artifact is not a regular file: {path}")
    if not DIR_FD_ANCHORED_WRITES:
        # checked fallback; no descriptor-relative API
        with path.open("rb") as stream:
            yield stream
        return
    parent_fd = open_dir_confined(root, path.parent)
    if parent_fd is None:
        raise PublicationError(f"artifact parent was redirected: {path}")
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PublicationError(f"artifact is not a regular file: {path}")
            yield stream
    finally:
        os.close(parent_fd)


def _contents(root: Path, path: Path, *, max_bytes: int | None = None) -> bytes | None:
    """Read a regular file, optionally stopping after one bounded sentinel byte."""
    with _open_regular(root, path) as stream:
        return None if stream is None else _read_bytes(stream, max_bytes)


def _git_identity_from_confined_file(root: Path, path: Path, repo: Path, rel: str) -> str:
    """Stream an opened source into a snapshot Git hashes under ``rel`` attributes."""
    with _open_regular(root, path) as stream:
        if stream is None:
            raise PublicationError(f"artifact deliverable is missing: {path}")
        opened = os.fstat(stream.fileno())
        opened_size = opened.st_size
        remaining = opened_size
        copied = 0
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "source"
            with snapshot.open("wb") as target:
                while remaining:
                    chunk = stream.read(min(_BOUNDED_READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    target.write(chunk)
                    copied += len(chunk)
                    remaining -= len(chunk)
                extra = stream.read(1)
            if (
                remaining != 0
                or bool(extra)
                or copied != opened_size
                or os.fstat(stream.fileno()).st_size != opened_size
                or not _destination_still_names(root, path, opened)
            ):
                raise PublicationError(f"artifact deliverable changed during binding: {path}")
            return verify.git_normalized_blob_oid(repo, rel, snapshot)


class _DestinationObservation(NamedTuple):
    size: int
    digest: str
    complete: bool = True


class _DestinationProbe(NamedTuple):
    observation: _DestinationObservation | None
    matches_expected: bool


class _FileIdentity(NamedTuple):
    device: int
    inode: int
    regular: bool


def _file_identity(metadata: os.stat_result) -> _FileIdentity | None:
    inode = getattr(metadata, "st_ino", 0)
    if not inode:
        return None
    return _FileIdentity(metadata.st_dev, inode, stat.S_ISREG(metadata.st_mode))


def _destination_path_identity(root: Path, path: Path) -> _FileIdentity | None:
    """Freshly observe one destination leaf without following a redirect."""
    if not DIR_FD_ANCHORED_WRITES:
        try:
            _confined(root, path)
        except PublicationError:
            return None
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        return _file_identity(metadata)

    parent_fd = open_dir_confined(root, path.parent)
    if parent_fd is None:
        return None
    try:
        try:
            metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return _file_identity(metadata)
    finally:
        os.close(parent_fd)


def _destination_still_names(root: Path, path: Path, opened: os.stat_result) -> bool:
    """Whether a fresh confined lookup still names the opened regular file."""
    opened_identity = _file_identity(opened)
    return opened_identity is not None and _destination_path_identity(root, path) == opened_identity


def _probe_destination(root: Path, path: Path, expected: bytes | None = None) -> _DestinationProbe:
    """Bound one destination probe to its opened size plus one growth check."""
    with _open_regular(root, path) as stream:
        if stream is None:
            return _DestinationProbe(observation=None, matches_expected=False)
        opened = os.fstat(stream.fileno())
        opened_size = opened.st_size
        remaining = opened_size
        size = 0
        digest = hashlib.sha256()
        matches_expected = expected is not None and opened_size == len(expected)
        while remaining:
            chunk = stream.read(min(_BOUNDED_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            if (
                matches_expected
                and expected is not None
                and chunk != expected[size : size + len(chunk)]
            ):
                matches_expected = False
            size += len(chunk)
            remaining -= len(chunk)
        extra = stream.read(_BOUNDED_READ_CHUNK_BYTES)
        if extra:
            digest.update(extra)
            size += len(extra)
        complete = (
            remaining == 0
            and not extra
            and os.fstat(stream.fileno()).st_size == size
            and _destination_still_names(root, path, opened)
        )
        observation = _DestinationObservation(
            size=size,
            digest=digest.hexdigest(),
            complete=complete,
        )
        return _DestinationProbe(
            observation=observation,
            matches_expected=matches_expected and complete,
        )


def _destination_observation(root: Path, path: Path) -> _DestinationObservation | None:
    """Stream one confined destination into a bounded size/digest observation."""
    return _probe_destination(root, path).observation


def _destination_equals(root: Path, path: Path, expected: bytes) -> bool:
    """Compare a confined destination to expected bytes without materializing it."""
    with _open_regular(root, path) as stream:
        if stream is None:
            return False
        opened = os.fstat(stream.fileno())
        if opened.st_size != len(expected):
            return False
        offset = 0
        while offset < len(expected):
            chunk = stream.read(min(_BOUNDED_READ_CHUNK_BYTES, len(expected) - offset))
            if not chunk or chunk != expected[offset : offset + len(chunk)]:
                return False
            offset += len(chunk)
        return (
            not stream.read(_BOUNDED_READ_CHUNK_BYTES)
            and os.fstat(stream.fileno()).st_size == offset
            and _destination_still_names(root, path, opened)
        )


def _file_size(root: Path, path: Path) -> int | None:
    """Measure a confined regular file without following a replaced leaf."""
    _confined(root, path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise PublicationError(f"artifact is not a regular file: {path}")
    if not DIR_FD_ANCHORED_WRITES:
        return metadata.st_size  # checked fallback; no descriptor-relative API
    parent_fd = open_dir_confined(root, path.parent)
    if parent_fd is None:
        raise PublicationError(f"artifact parent was redirected: {path}")
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise PublicationError(f"artifact is not a regular file: {path}")
            return opened.st_size
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _local(paths: ProjectPaths) -> bool:
    root = paths.implementation_artifacts
    return root != paths.repo_root and root.is_relative_to(paths.repo_root)


def _root(paths: ProjectPaths) -> Path:
    root = paths.implementation_artifacts
    if not _local(paths):
        raise PublicationError(f"artifact directory must be strictly inside the repository: {root}")
    _confined(paths.repo_root, root)
    return root


def capture(task: StoryTask, paths: ProjectPaths) -> None:
    """Called only for a newly opened bundle mount, before its first execution."""
    root = paths.implementation_artifacts
    inventory: dict[str, str] = {}

    def walk(directory: Path) -> None:
        _confined(root, directory)
        directory_fd = open_dir_confined(root, directory) if DIR_FD_ANCHORED_WRITES else None
        if DIR_FD_ANCHORED_WRITES and directory_fd is None:
            raise PublicationError(f"artifact inventory directory was redirected: {directory}")
        try:
            with os.scandir(directory_fd if directory_fd is not None else directory) as entries:
                for entry in entries:
                    path = directory / entry.name
                    mode = entry.stat(follow_symlinks=False).st_mode
                    rel = path.relative_to(root).as_posix()
                    if stat.S_ISDIR(mode):
                        inventory[rel] = "directory"
                        walk(path)
                    elif stat.S_ISREG(mode):
                        observed = _destination_observation(root, path)
                        if observed is None:
                            raise PublicationError(f"artifact disappeared during inventory: {path}")
                        if not observed.complete:
                            raise PublicationError(f"artifact changed during inventory: {path}")
                        inventory[rel] = observed.digest
                    else:
                        inventory[rel] = "nonregular"
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    # Shared external artifacts already live at their destination. They neither
    # need implicit transport nor grant authority for explicit publication.
    if _local(paths):
        _root(paths)
        if root.exists():
            walk(root)
    task.artifact_baseline = inventory
    task.artifact_destination = str(root)
    task.artifact_source_digests = None
    task.artifact_tracked_source_oids = None
    task.artifact_acceptance_identity = None
    task.artifact_payload = None
    task.artifact_publication_complete = False


class _SelectedSources(NamedTuple):
    contents: dict[str, bytes]
    tracked_oids: dict[str, str]
    tracked_rels: frozenset[str]
    """Every rel that rides Git: tracked, or pending-tracked when admitted.

    Always populated, without opening the file — a caller that only needs the
    SELECTION (``prepare``) must not touch a tracked working-tree file behind a
    sealed commit, where deletion or replacement is tolerated.
    """


def _selected_sources(
    task: StoryTask,
    source: ProjectPaths,
    *,
    allow_pending_tracked: bool = False,
    collect_tracked_identities: bool = False,
    file_max_bytes: int = DEFAULT_FILE_MAX_BYTES,
    payload_max_bytes: int = DEFAULT_PAYLOAD_MAX_BYTES,
) -> _SelectedSources:
    """Read the exact ignored selection and optionally bind Git identities."""
    if file_max_bytes < 1 or payload_max_bytes < 1:
        raise ValueError("artifact publication byte limits must be positive")
    if not task.spec_file:
        raise PublicationError("accepted bundle spec is missing")
    spec = verify.resolve_spec_path(task.spec_file, source)
    root = source.implementation_artifacts
    # Specs outside implementation_artifacts are not automatically published,
    # but their explicit declaration is still subject to the same confinement.
    spec_root = source.project if spec.is_relative_to(source.project) else root
    implicit_rel: str | None = None
    if _local(source) and spec != root and spec.is_relative_to(root):
        implicit_rel = _relative(spec.relative_to(root).as_posix())
        _root(source)
    spec_tracked = False
    spec_ignored = False
    if implicit_rel is not None:
        repo_rel = spec.relative_to(source.repo_root).as_posix()
        spec_tracked = verify.path_tracked(source.repo_root, repo_rel)
        if not spec_tracked:
            spec_ignored = verify.path_ignored(source.repo_root, spec)
    spec_read_limit = min(file_max_bytes, payload_max_bytes)
    spec_data = _contents(spec_root, spec, max_bytes=spec_read_limit if spec_ignored else None)
    if spec_data is None:
        raise PublicationError(f"accepted bundle spec is missing: {spec}")
    if spec_ignored and len(spec_data) > spec_read_limit:
        cause: Literal["file-limit", "payload-limit"] = (
            "file-limit" if file_max_bytes <= payload_max_bytes else "payload-limit"
        )
        raise PublicationSizeError(
            cause,
            len(spec_data),
            spec_read_limit,
            path=spec,
            at_least=True,
        )
    fm = parse_frontmatter(spec_data.decode("utf-8"))
    if not fm:
        raise PublicationError(f"invalid accepted spec frontmatter: {spec}")
    declarations = fm.get("artifact_deliverables", [])
    if not isinstance(declarations, list):
        raise PublicationError(f"artifact_deliverables must be a list of exact paths: {spec}")
    selected = {_relative(item) for item in declarations}
    if selected:
        _root(source)  # explicit unsafe external declarations must fail
    if implicit_rel is not None:
        selected.add(implicit_rel)

    inputs: list[tuple[str, Path, int]] = []
    tracked_oids: dict[str, str] = {}
    tracked_rels: set[str] = set()
    for rel in sorted(selected):
        path = root / rel
        repo_rel = path.relative_to(source.repo_root).as_posix()
        if verify.path_tracked(source.repo_root, repo_rel):
            tracked_rels.add(rel)
            if collect_tracked_identities:
                if path == spec:
                    tracked_oids[rel] = verify.git_normalized_blob_oid_for_bytes(
                        source.repo_root, repo_rel, spec_data
                    )
                else:
                    tracked_oids[rel] = _git_identity_from_confined_file(
                        root, path, source.repo_root, repo_rel
                    )
            continue  # tracked deliverables ride Git
        ignored = verify.path_ignored(source.repo_root, path)
        size = _file_size(root, path)
        if size is None:
            raise PublicationError(f"artifact deliverable is missing: {path}")
        if not ignored:
            if allow_pending_tracked:
                # Binding runs before the orchestrator's final `git add -A`.
                # Preparation must later prove this path became tracked; a path
                # an embedded repository kept untracked must not vanish silently.
                tracked_rels.add(rel)
                if collect_tracked_identities:
                    if path == spec:
                        tracked_oids[rel] = verify.git_normalized_blob_oid_for_bytes(
                            source.repo_root, repo_rel, spec_data
                        )
                    else:
                        tracked_oids[rel] = _git_identity_from_confined_file(
                            root, path, source.repo_root, repo_rel
                        )
                continue
            raise PublicationError(f"artifact deliverable was not tracked by commit: {path}")
        if size > file_max_bytes:
            raise PublicationSizeError("file-limit", size, file_max_bytes, path=path)
        inputs.append((rel, path, size))

    preflight_total = sum(size for _, _, size in inputs)
    if preflight_total > payload_max_bytes:
        raise PublicationSizeError("payload-limit", preflight_total, payload_max_bytes)

    # Read every admitted source before encoding any of it. The bound uses both
    # the per-file cap and the aggregate bytes still available, so a growing file
    # is stopped at the first sentinel byte beyond either budget.
    raw_payload: dict[str, bytes] = {}
    actual_total = 0
    for rel, path, _ in inputs:
        remaining = payload_max_bytes - actual_total
        read_limit = min(file_max_bytes, remaining)
        data = _contents(root, path, max_bytes=read_limit)
        if data is None:
            raise PublicationError(f"artifact deliverable is missing: {path}")
        if len(data) > read_limit:
            if file_max_bytes <= remaining:
                raise PublicationSizeError(
                    "file-limit",
                    len(data),
                    file_max_bytes,
                    path=path,
                    at_least=True,
                )
            raise PublicationSizeError(
                "payload-limit",
                actual_total + len(data),
                payload_max_bytes,
                path=path,
                at_least=True,
            )
        if path == spec and data != spec_data:
            raise PublicationError(f"accepted bundle spec changed during preparation: {spec}")
        raw_payload[rel] = data
        actual_total += len(data)

    return _SelectedSources(raw_payload, tracked_oids, frozenset(tracked_rels))


def arm_binding(task: StoryTask, acceptance_identity: str) -> bool:
    """Durably arm a distinct accepted result before any source bytes are read.

    The caller must save immediately when this returns True. Re-arming the same
    identity is forbidden: a crash replay must keep the original authority (or
    the original refusal), never derive it again from potentially changed bytes.
    """
    if not acceptance_identity:
        raise ValueError("artifact acceptance identity must be non-empty")
    if task.artifact_acceptance_identity == acceptance_identity:
        return False
    task.artifact_acceptance_identity = acceptance_identity
    task.artifact_source_digests = None
    task.artifact_tracked_source_oids = None
    task.artifact_payload = None
    task.artifact_publication_complete = False
    return True


def bind_armed(
    task: StoryTask,
    source: ProjectPaths,
    *,
    file_max_bytes: int = DEFAULT_FILE_MAX_BYTES,
    payload_max_bytes: int = DEFAULT_PAYLOAD_MAX_BYTES,
) -> None:
    """Bind an already-armed result to exact ignored and Git-backed bytes."""
    if task.artifact_acceptance_identity is None:
        raise PublicationError("artifact acceptance identity is missing")
    if task.artifact_source_digests is not None and task.artifact_tracked_source_oids is not None:
        return
    if task.artifact_source_digests is not None or task.artifact_tracked_source_oids is not None:
        raise PublicationError("accepted artifact source binding is incomplete")
    selected = _selected_sources(
        task,
        source,
        allow_pending_tracked=True,
        collect_tracked_identities=True,
        file_max_bytes=file_max_bytes,
        payload_max_bytes=payload_max_bytes,
    )
    task.artifact_source_digests = {rel: _digest(data) for rel, data in selected.contents.items()}
    task.artifact_tracked_source_oids = dict(selected.tracked_oids)


def _validated_source_maps(task: StoryTask) -> tuple[dict[str, str], dict[str, str]]:
    ignored = task.artifact_source_digests
    tracked = task.artifact_tracked_source_oids
    if not isinstance(ignored, dict) or not all(
        isinstance(rel, str) and isinstance(identity, str) for rel, identity in ignored.items()
    ):
        raise PublicationError("accepted ignored artifact source binding is missing or malformed")
    if not isinstance(tracked, dict) or not all(
        isinstance(rel, str) and isinstance(identity, str) for rel, identity in tracked.items()
    ):
        raise PublicationError("accepted tracked artifact source binding is missing or malformed")
    if ignored.keys() & tracked.keys():
        raise PublicationError("accepted artifact source classifications are ambiguous")
    return ignored, tracked


def _validate_git_snapshot(
    ignored: dict[str, str], tracked: dict[str, str], observed: dict[str, str]
) -> None:
    differing = [rel for rel, oid in tracked.items() if observed.get(rel) != oid]
    differing.extend(rel for rel in ignored if rel in observed)
    if differing:
        raise PublicationError(
            "Git artifact deliverables differ from accepted verification: "
            + ", ".join(sorted(differing))
        )


def validate_staged(task: StoryTask, source: ProjectPaths) -> dict[str, str]:
    """Require every accepted deliverable classification and staged blob.

    The mapping is the complete accepted tracked/pending-tracked path set. It is
    never rebuilt here: replay keeps the original acceptance authority, and a
    missing legacy/incomplete proof fails closed before commit.
    """
    ignored, tracked = _validated_source_maps(task)
    rels = tuple(ignored) + tuple(tracked)
    if rels:
        _root(source)
    repo_rels = {
        rel: (source.implementation_artifacts / _relative(rel))
        .relative_to(source.repo_root)
        .as_posix()
        for rel in rels
    }
    try:
        staged = verify.staged_blob_oids(source.repo_root, repo_rels.values())
    except verify.GitError as exc:
        raise PublicationError(
            "Git artifact deliverables have unavailable index evidence: "
            + ", ".join(sorted(repo_rels))
        ) from exc
    observed = {rel: staged[repo_rel] for rel, repo_rel in repo_rels.items() if repo_rel in staged}
    _validate_git_snapshot(ignored, tracked, observed)
    return observed


def validate_committed(
    task: StoryTask,
    source: ProjectPaths,
    revision: str,
    staged_snapshot: dict[str, str],
) -> None:
    """Require the committed tree to equal the already-validated index snapshot."""
    ignored, tracked = _validated_source_maps(task)
    rels = tuple(ignored) + tuple(tracked)
    if rels:
        _root(source)
    repo_rels = {
        rel: (source.implementation_artifacts / _relative(rel))
        .relative_to(source.repo_root)
        .as_posix()
        for rel in rels
    }
    try:
        committed = verify.revision_blob_oids(source.repo_root, revision, repo_rels.values())
    except verify.GitError as exc:
        raise PublicationError(
            "Git artifact deliverables have unavailable commit evidence: "
            + ", ".join(sorted(repo_rels))
        ) from exc
    observed = {
        rel: committed[repo_rel] for rel, repo_rel in repo_rels.items() if repo_rel in committed
    }
    if observed != staged_snapshot:
        differing = sorted(
            rel
            for rel in observed.keys() | staged_snapshot.keys()
            if observed.get(rel) != staged_snapshot.get(rel)
        )
        raise PublicationError(
            "committed Git artifact deliverables differ from the validated index: "
            + ", ".join(differing)
        )


def validate_integrated(task: StoryTask, target: ProjectPaths, revision: str) -> bool:
    """Validate the target commit and post-hook index against accepted blobs.

    Returns ``False`` only for a legacy frozen payload which predates accepted
    source maps.  Such a payload keeps its historical integration/publication
    path; missing authority is never synthesized from target or live source
    bytes.  Once either new-style map (or its acceptance owner) exists, both
    complete maps are required and malformed evidence fails closed.
    """
    if not requires_target_integration_receipt(task):
        return False

    ignored, tracked = _validated_source_maps(task)
    repo_rels = _integrated_repo_rels(target, ignored, tracked)
    path_list = tuple(repo_rels.values())
    try:
        committed = verify.revision_blob_oids(target.repo_root, revision, path_list)
    except verify.GitError as exc:
        raise PublicationError(
            "target commit artifact evidence is unavailable for declared paths: "
            + ", ".join(sorted(repo_rels))
        ) from exc
    committed_observed = {
        rel: committed[repo_rel] for rel, repo_rel in repo_rels.items() if repo_rel in committed
    }
    _validate_git_snapshot(ignored, tracked, committed_observed)

    try:
        staged = verify.staged_blob_oids(target.repo_root, path_list)
    except verify.GitError as exc:
        raise PublicationError(
            "target index artifact evidence is unavailable for declared paths: "
            + ", ".join(sorted(repo_rels))
        ) from exc
    staged_observed = {
        rel: staged[repo_rel] for rel, repo_rel in repo_rels.items() if repo_rel in staged
    }
    _validate_git_snapshot(ignored, tracked, staged_observed)
    return True


def _validated_frozen_payload(task: StoryTask) -> dict[str, str]:
    payload = task.artifact_payload
    if not isinstance(payload, dict) or not all(
        isinstance(rel, str) and isinstance(encoded, str) for rel, encoded in payload.items()
    ):
        raise PublicationError("frozen artifact publication payload is missing or malformed")
    for rel, encoded in payload.items():
        _relative(rel)
        try:
            base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise PublicationError("frozen artifact publication payload is malformed") from exc
    return payload


def requires_target_integration_receipt(task: StoryTask) -> bool:
    """Classify complete modern authority versus the released legacy payload.

    The legacy arm is deliberately narrow: only a valid historical dictionary
    payload with no tracked map bypasses the target receipt/reflog contract.
    Partial modern shapes and malformed payloads fail before any target mutation.
    """
    if task.artifact_tracked_source_oids is not None:
        ignored, _tracked = _validated_source_maps(task)
        if task.artifact_payload is not None:
            payload = _validated_frozen_payload(task)
            if payload.keys() != ignored.keys() or any(
                _digest(base64.b64decode(payload[rel], validate=True)) != digest
                for rel, digest in ignored.items()
            ):
                raise PublicationError(
                    "frozen artifact payload differs from accepted ignored source binding"
                )
        if (
            not isinstance(task.artifact_acceptance_identity, str)
            or not task.artifact_acceptance_identity
        ):
            raise PublicationError("accepted artifact acceptance identity is missing or malformed")
        return True

    _validated_frozen_payload(task)
    if task.artifact_tracked_source_oids is None:
        ignored = task.artifact_source_digests
        owner = task.artifact_acceptance_identity
        if ignored is not None and (
            not isinstance(ignored, dict)
            or not all(
                isinstance(rel, str) and isinstance(value, str) for rel, value in ignored.items()
            )
        ):
            raise PublicationError("legacy artifact source binding is malformed")
        if owner is not None and (not isinstance(owner, str) or not owner):
            raise PublicationError("legacy artifact acceptance identity is malformed")
        return False
    raise AssertionError("unreachable")


def integrated_artifact_repo_paths(task: StoryTask, target: ProjectPaths) -> tuple[str, ...]:
    """Return validated target-repository paths covered by accepted authority."""
    ignored, tracked = _validated_source_maps(task)
    return tuple(_integrated_repo_rels(target, ignored, tracked).values())


def _integrated_repo_rels(
    target: ProjectPaths, ignored: dict[str, str], tracked: dict[str, str]
) -> dict[str, str]:
    rels = tuple(ignored) + tuple(tracked)
    if rels:
        _root(target)
    return {
        rel: (target.implementation_artifacts / _relative(rel))
        .relative_to(target.repo_root)
        .as_posix()
        for rel in rels
    }


def prepare(
    task: StoryTask,
    paths: ProjectPaths,
    source: ProjectPaths,
    *,
    file_max_bytes: int = DEFAULT_FILE_MAX_BYTES,
    payload_max_bytes: int = DEFAULT_PAYLOAD_MAX_BYTES,
) -> None:
    """Freeze ignored bytes only when they match final accepted verification.

    The tracked selection is re-derived too and its rel SET must equal the
    accepted one. The ignored map alone would let a spec that lives outside
    ``implementation_artifacts`` — and so is never itself a selected deliverable
    — swap one tracked declaration for another after acceptance (#795 review):
    the staged and committed validators only ever consult the ACCEPTED tracked
    rels, so the swapped-in path would ride the commit unproven. Preparation
    runs after ``finalize_commit``'s ``git add -A``, so every pending-tracked
    rel is tracked by now and the two rel sets are directly comparable. Only
    the rels are compared, and no tracked file is opened to get them: the
    accepted rels' blob identities were already proven on the validated index
    and the committed tree, and the working tree behind a sealed commit is not
    the authority — a writer landing there after staging, or removing the file
    outright, is tolerated by design (it cannot enter the commit), not a
    refusal.
    """
    # A frozen payload is the durable publication intent, including legacy runs
    # that predate accepted-source binding. Never reread its source on replay.
    if task.artifact_payload is not None:
        return
    if (
        task.artifact_acceptance_identity is None
        or task.artifact_source_digests is None
        or task.artifact_tracked_source_oids is None
    ):
        raise PublicationError("accepted artifact source binding is missing")
    selected = _selected_sources(
        task,
        source,
        file_max_bytes=file_max_bytes,
        payload_max_bytes=payload_max_bytes,
    )
    current = {rel: _digest(data) for rel, data in selected.contents.items()}
    if current != task.artifact_source_digests:
        differing = sorted(
            rel
            for rel in current.keys() | task.artifact_source_digests.keys()
            if current.get(rel) != task.artifact_source_digests.get(rel)
        )
        raise PublicationError(
            "artifact deliverables changed since accepted verification: " + ", ".join(differing)
        )
    if selected.tracked_rels != task.artifact_tracked_source_oids.keys():
        differing = sorted(selected.tracked_rels ^ task.artifact_tracked_source_oids.keys())
        raise PublicationError(
            "tracked artifact deliverables changed since accepted verification: "
            + ", ".join(differing)
        )
    task.artifact_payload = {
        rel: base64.b64encode(data).decode("ascii") for rel, data in selected.contents.items()
    }
    # Store intended bytes even for a legacy task: refusal must retain recovery
    # material, and a later resume must never read changed source bytes as intent.
    if task.artifact_destination is None:
        task.artifact_destination = str(paths.implementation_artifacts)


def publish(task: StoryTask, paths: ProjectPaths) -> None:
    """Compare each destination against pre-execution evidence, then replace."""
    if task.artifact_publication_complete:
        return
    root = paths.implementation_artifacts
    if task.artifact_destination != str(root):
        raise PublicationError(f"artifact destination changed since execution: {root}")
    if task.artifact_payload is None:
        raise PublicationError("artifact publication intent is missing")
    if task.artifact_payload:
        _root(paths)
    for rel, encoded in task.artifact_payload.items():
        path = root / _relative(rel)
        intended = base64.b64decode(encoded, validate=True)
        probe = _probe_destination(root, path, intended)
        if probe.matches_expected:
            continue
        current = probe.observation
        if task.artifact_baseline is None:
            raise PublicationError(f"no pre-execution artifact baseline for {path}")
        before = (task.artifact_baseline or {}).get(rel)
        if current is not None and not current.complete:
            raise PublicationError(f"artifact destination conflict: {path}")
        now = None if current is None else current.digest
        if now != before:
            raise PublicationError(f"artifact destination conflict: {path}")
        if verify.path_tracked(paths.repo_root, path.relative_to(paths.repo_root).as_posix()):
            raise PublicationError(f"artifact destination became tracked: {path}")
        if not verify.path_ignored(paths.repo_root, path):
            raise PublicationError(f"artifact destination is no longer ignored: {path}")
        _confined(root, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if _destination_observation(root, path) != current:
            raise PublicationError(f"artifact destination changed during publication: {path}")

        def validate_destination() -> None:
            if _destination_observation(root, path) != current:
                raise PublicationError(f"artifact destination changed during publication: {path}")

        atomic_write_bytes_confined(
            path, intended, confine_root=root, _before_replace=validate_destination
        )
        if not _destination_equals(root, path, intended):
            raise PublicationError(f"published artifact is not visible at destination: {path}")
    task.artifact_publication_complete = True
