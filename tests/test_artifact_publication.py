"""Publication authority and byte preservation without involving receipt proof."""

import base64
import hashlib
import json
import os
import sys
import time
import tracemalloc
from contextlib import contextmanager

import pytest
from conftest import git

from bmad_loop import artifact_publication as publication
from bmad_loop.journal import save_state
from bmad_loop.model import RunState, StoryTask


def bind_and_prepare(task, paths, source, **limits):
    """Exercise the production sequence: arm, bind at acceptance, then freeze."""
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source, **limits)
    publication.prepare(task, paths, source, **limits)


@pytest.fixture
def publication_case(project, monkeypatch):
    source = project.rebased(project.project / "unit")
    source.implementation_artifacts.mkdir(parents=True)
    spec = source.implementation_artifacts / "spec.md"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [report.bin]\n---\n")
    (source.implementation_artifacts / "report.bin").write_bytes(b"\xff\x00\r\nreport")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    monkeypatch.setattr(publication.verify, "path_ignored", lambda *_: True)
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: False)
    publication.capture(task, project)
    return task, project, source


def test_exact_selection_and_frozen_binary_payload(publication_case):
    task, paths, source = publication_case
    (source.implementation_artifacts / "unrelated.md").write_text("unrelated")
    bind_and_prepare(task, paths, source)
    (source.implementation_artifacts / "report.bin").write_bytes(b"later source edit")
    publication.publish(task, paths)
    assert (paths.implementation_artifacts / "report.bin").read_bytes() == b"\xff\x00\r\nreport"
    assert (paths.implementation_artifacts / "spec.md").read_bytes() == (
        source.implementation_artifacts / "spec.md"
    ).read_bytes()
    assert not (paths.implementation_artifacts / "unrelated.md").exists()
    assert task.artifact_publication_complete


def test_binding_records_the_exact_ignored_selection_before_payload_freeze(publication_case):
    task, paths, source = publication_case

    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)

    assert task.artifact_acceptance_identity == "dev:0"
    assert task.artifact_source_digests == {
        "report.bin": publication._digest(b"\xff\x00\r\nreport"),
        "spec.md": publication._digest((source.implementation_artifacts / "spec.md").read_bytes()),
    }
    assert task.artifact_payload is None

    publication.prepare(task, paths, source)
    assert set(task.artifact_payload) == set(task.artifact_source_digests)


def test_changed_accepted_bytes_refuse_before_any_payload_is_encoded(publication_case, monkeypatch):
    task, paths, source = publication_case
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    accepted = dict(task.artifact_source_digests)
    (source.implementation_artifacts / "report.bin").write_bytes(b"post-verify writer")
    monkeypatch.setattr(
        publication.base64,
        "b64encode",
        lambda _data: pytest.fail("payload encoding started before binding comparison"),
    )

    with pytest.raises(publication.PublicationError, match="report\\.bin") as exc:
        publication.prepare(task, paths, source)

    assert "post-verify writer" not in str(exc.value)
    assert accepted["report.bin"] not in str(exc.value)
    assert task.artifact_source_digests == accepted
    assert task.artifact_payload is None


def test_declaration_set_drift_is_an_exact_mapping_mismatch(publication_case, monkeypatch):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    monkeypatch.setattr(
        publication.verify,
        "path_tracked",
        lambda _repo, rel: rel.endswith("spec.md"),
    )
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    assert set(task.artifact_source_digests) == {"report.bin"}
    spec.write_text("---\nstatus: done\nartifact_deliverables: []\n---\n")

    with pytest.raises(publication.PublicationError, match="report\\.bin"):
        publication.prepare(task, paths, source)

    assert task.artifact_payload is None


def test_added_ignored_path_is_named_by_complete_map_refusal(publication_case, monkeypatch):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    monkeypatch.setattr(
        publication.verify,
        "path_tracked",
        lambda _repo, rel: rel.endswith("spec.md"),
    )
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    (source.implementation_artifacts / "added.bin").write_bytes(b"secret payload")
    spec.write_text("---\nstatus: done\nartifact_deliverables: [report.bin, added.bin]\n---\n")

    with pytest.raises(publication.PublicationError, match="added\\.bin") as exc:
        publication.prepare(task, paths, source)

    assert "report.bin" not in str(exc.value)
    assert "secret payload" not in str(exc.value)


def test_distinct_accepted_result_refreshes_binding_but_same_result_does_not(
    publication_case,
):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    first = dict(task.artifact_source_digests)
    report.write_bytes(b"accepted repair")

    assert publication.arm_binding(task, "dev:0") is False
    publication.bind_armed(task, source)
    assert task.artifact_source_digests == first
    with pytest.raises(publication.PublicationError, match="changed since accepted verification"):
        publication.prepare(task, paths, source)

    assert publication.arm_binding(task, "dev:1") is True
    publication.bind_armed(task, source)
    assert task.artifact_source_digests != first
    publication.prepare(task, paths, source)
    assert base64.b64decode(task.artifact_payload["report.bin"]) == b"accepted repair"


def test_default_per_file_limit_is_inclusive(publication_case):
    task, _paths, source = publication_case
    data = b"x" * publication.DEFAULT_FILE_MAX_BYTES
    (source.implementation_artifacts / "report.bin").write_bytes(data)

    bind_and_prepare(task, _paths, source)

    assert base64.b64decode(task.artifact_payload["report.bin"]) == data


def test_default_per_file_limit_plus_one_refuses_before_encoding(publication_case, monkeypatch):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    report.write_bytes(b"x" * (publication.DEFAULT_FILE_MAX_BYTES + 1))
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(
        task,
        source,
        file_max_bytes=publication.DEFAULT_FILE_MAX_BYTES + 1,
    )
    encoded = []
    monkeypatch.setattr(publication.base64, "b64encode", lambda data: encoded.append(data))

    with pytest.raises(publication.PublicationSizeError) as exc:
        publication.prepare(task, paths, source)

    assert exc.value.cause == "file-limit"
    assert exc.value.measured_bytes == publication.DEFAULT_FILE_MAX_BYTES + 1
    assert exc.value.limit_bytes == publication.DEFAULT_FILE_MAX_BYTES
    assert encoded == []
    assert task.artifact_payload is None


def test_implicit_spec_preliminary_read_obeys_smaller_aggregate_limit(publication_case):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    spec.write_bytes(b"---\nstatus: done\n---\n" + b"x" * 200)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)

    with pytest.raises(publication.PublicationSizeError) as exc:
        publication.prepare(task, paths, source, file_max_bytes=300, payload_max_bytes=100)

    assert exc.value.cause == "payload-limit"
    assert exc.value.measured_bytes == 101
    assert exc.value.limit_bytes == 100
    assert task.artifact_payload is None


def test_extreme_positive_limits_use_fixed_size_read_requests(publication_case):
    task, paths, source = publication_case
    extreme_legal_limit = sys.maxsize * 1_048_576

    bind_and_prepare(
        task,
        paths,
        source,
        file_max_bytes=extreme_legal_limit,
        payload_max_bytes=extreme_legal_limit,
    )

    assert base64.b64decode(task.artifact_payload["report.bin"]) == b"\xff\x00\r\nreport"


@pytest.mark.parametrize("cause", ["file-limit", "payload-limit"])
def test_metadata_preflight_refuses_before_any_payload_read(publication_case, monkeypatch, cause):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    if cause == "file-limit":
        (source.implementation_artifacts / "report.bin").write_bytes(
            b"x" * (publication.DEFAULT_FILE_MAX_BYTES + 1)
        )
    else:
        spec.write_text("---\nstatus: done\nartifact_deliverables: [a.bin, z.bin]\n---\n")
        (source.implementation_artifacts / "a.bin").write_bytes(
            b"a" * publication.DEFAULT_FILE_MAX_BYTES
        )
        second = (
            publication.DEFAULT_PAYLOAD_MAX_BYTES
            - len(spec.read_bytes())
            - publication.DEFAULT_FILE_MAX_BYTES
        )
        (source.implementation_artifacts / "z.bin").write_bytes(b"z" * (second + 1))
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(
        task,
        source,
        file_max_bytes=(
            publication.DEFAULT_FILE_MAX_BYTES + 1
            if cause == "file-limit"
            else publication.DEFAULT_FILE_MAX_BYTES
        ),
        payload_max_bytes=(
            publication.DEFAULT_PAYLOAD_MAX_BYTES + 1
            if cause == "payload-limit"
            else publication.DEFAULT_PAYLOAD_MAX_BYTES
        ),
    )
    read = publication._contents
    preliminary_reads = 0

    def reject_payload_read(root, path, **kwargs):
        nonlocal preliminary_reads
        if path == spec and preliminary_reads == 0:
            preliminary_reads += 1
            return read(root, path, **kwargs)
        pytest.fail(f"payload read started before {cause} metadata preflight completed: {path}")

    monkeypatch.setattr(publication, "_contents", reject_payload_read)

    with pytest.raises(publication.PublicationSizeError) as exc:
        publication.prepare(task, paths, source)

    assert exc.value.cause == cause
    assert preliminary_reads == 1
    assert task.artifact_payload is None


@pytest.mark.parametrize("over", [0, 1], ids=["exact", "plus-one"])
def test_default_aggregate_limit_counts_unique_ignored_inputs(publication_case, monkeypatch, over):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    spec.write_text(
        "---\nstatus: done\n" "artifact_deliverables: [spec.md, a.bin, z.bin, spec.md]\n---\n"
    )
    first = publication.DEFAULT_FILE_MAX_BYTES
    last = publication.DEFAULT_PAYLOAD_MAX_BYTES - first - len(spec.read_bytes()) + over
    (source.implementation_artifacts / "a.bin").write_bytes(b"a" * first)
    (source.implementation_artifacts / "z.bin").write_bytes(b"z" * last)
    encoded = []
    original_encode = publication.base64.b64encode

    def record_encode(data):
        encoded.append(len(data))
        return original_encode(data)

    publication.arm_binding(task, "dev:0")
    publication.bind_armed(
        task,
        source,
        payload_max_bytes=publication.DEFAULT_PAYLOAD_MAX_BYTES + over,
    )
    monkeypatch.setattr(publication.base64, "b64encode", record_encode)
    if over:
        with pytest.raises(publication.PublicationSizeError) as exc:
            publication.prepare(task, paths, source)
        assert exc.value.cause == "payload-limit"
        assert exc.value.measured_bytes == publication.DEFAULT_PAYLOAD_MAX_BYTES + 1
        assert encoded == []
        assert task.artifact_payload is None
    else:
        publication.prepare(task, paths, source)
        assert sum(len(base64.b64decode(value)) for value in task.artifact_payload.values()) == (
            publication.DEFAULT_PAYLOAD_MAX_BYTES
        )
        assert len(encoded) == 3  # duplicate spec declarations count once


def test_tracked_oversize_declaration_does_not_consume_payload_budget(
    publication_case, monkeypatch
):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    report.write_bytes(b"x" * (publication.DEFAULT_FILE_MAX_BYTES + 1))
    monkeypatch.setattr(
        publication.verify,
        "path_tracked",
        lambda _repo, rel: rel.endswith("report.bin"),
    )

    bind_and_prepare(task, paths, source)

    assert set(task.artifact_payload) == {"spec.md"}


def test_tracked_oversize_implicit_spec_does_not_consume_payload_budget(
    publication_case, monkeypatch
):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    spec.write_bytes(
        b"---\nstatus: done\nartifact_deliverables: [report.bin]\n---\n"
        + b"x" * publication.DEFAULT_FILE_MAX_BYTES
    )
    monkeypatch.setattr(
        publication.verify,
        "path_tracked",
        lambda _repo, rel: rel.endswith("spec.md"),
    )

    bind_and_prepare(task, paths, source)

    assert set(task.artifact_payload) == {"report.bin"}


@pytest.mark.parametrize("cause", ["file-limit", "payload-limit"])
def test_growth_after_preflight_is_bounded_and_nothing_is_encoded(
    publication_case, monkeypatch, cause
):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    report = source.implementation_artifacts / "report.bin"
    if cause == "file-limit":
        report.write_bytes(b"x")
    else:
        spec.write_text("---\nstatus: done\nartifact_deliverables: [a.bin, z.bin]\n---\n")
        (source.implementation_artifacts / "a.bin").write_bytes(
            b"a" * publication.DEFAULT_FILE_MAX_BYTES
        )
        remaining = (
            publication.DEFAULT_PAYLOAD_MAX_BYTES
            - publication.DEFAULT_FILE_MAX_BYTES
            - len(spec.read_bytes())
            - 1
        )
        report = source.implementation_artifacts / "z.bin"
        report.write_bytes(b"z" * remaining)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    measured = publication._file_size
    grew = False

    def grow_after_measurement(root, path):
        nonlocal grew
        size = measured(root, path)
        if path == report and not grew:
            grew = True
            if cause == "file-limit":
                path.write_bytes(b"x" * (publication.DEFAULT_FILE_MAX_BYTES + 50_000))
            else:
                with path.open("ab") as stream:
                    stream.write(b"zz")
        return size

    monkeypatch.setattr(publication, "_file_size", grow_after_measurement)
    monkeypatch.setattr(
        publication.base64,
        "b64encode",
        lambda _data: pytest.fail("encoding started before every bounded read passed"),
    )

    with pytest.raises(publication.PublicationSizeError) as exc:
        publication.prepare(task, paths, source)

    assert exc.value.cause == cause
    assert exc.value.measured_bytes == exc.value.limit_bytes + 1
    assert exc.value.measurement_is_lower_bound is True
    assert task.artifact_payload is None


def test_legacy_frozen_oversize_payload_still_publishes(publication_case):
    task, paths, _source = publication_case
    intended = b"x" * (publication.DEFAULT_FILE_MAX_BYTES + 1)
    task.artifact_payload = {"report.bin": base64.b64encode(intended).decode("ascii")}

    publication.publish(task, paths)

    assert (paths.implementation_artifacts / "report.bin").read_bytes() == intended
    assert task.artifact_publication_complete


def _ten_mib_payload_five_save_capacity_envelope(tmp_path):
    raw_size = publication.DEFAULT_PAYLOAD_MAX_BYTES
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    started = time.perf_counter()
    peak = None
    try:
        raw = b"x" * raw_size
        encoded = base64.b64encode(raw).decode("ascii")
        task = StoryTask(story_key="dw-capacity", epic=0, artifact_payload={"bundle.bin": encoded})
        state = RunState(
            run_id="capacity",
            project=str(tmp_path),
            started_at="now",
            tasks={task.story_key: task},
        )
        run_dir = tmp_path / "run"
        for _ in range(5):
            save_state(run_dir, state)
        elapsed = time.perf_counter() - started
        if started_tracing:
            _, peak = tracemalloc.get_traced_memory()
    finally:
        if started_tracing:
            tracemalloc.stop()

    structural_base64 = 4 * ((raw_size + 2) // 3)
    assert len(encoded) == structural_base64
    state_bytes = (run_dir / "state.json").read_bytes()
    assert len(state_bytes) <= structural_base64 + 64 * 1024
    assert json.loads(state_bytes)["tasks"]["dw-capacity"]["artifact_payload"]["bundle.bin"] == (
        encoded
    )
    if peak is not None:
        assert peak < 160 * 1_048_576
    assert elapsed < 20


def test_ten_mib_payload_five_save_capacity_envelope(tmp_path):
    _ten_mib_payload_five_save_capacity_envelope(tmp_path)


def test_capacity_envelope_preserves_an_existing_tracemalloc_session(tmp_path):
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        _ten_mib_payload_five_save_capacity_envelope(tmp_path)
        assert tracemalloc.is_tracing()
    finally:
        if not was_tracing:
            tracemalloc.stop()


def test_late_declaration_does_not_capture_late_destination(publication_case):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / "report.bin"
    destination.write_bytes(b"operator")
    bind_and_prepare(task, paths, source)
    with pytest.raises(publication.PublicationError, match="conflict.*report.bin"):
        publication.publish(task, paths)
    assert destination.read_bytes() == b"operator"
    assert not task.artifact_publication_complete
    assert task.artifact_payload is not None


def test_large_destination_baseline_is_streamed_without_contents_materialization(
    publication_case, monkeypatch
):
    task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "unrelated-large.bin"
    block = b"baseline" * 8192
    digest = hashlib.sha256()
    with destination.open("wb") as stream:
        for _ in range(192):
            stream.write(block)
            digest.update(block)
    directory = paths.implementation_artifacts / "baseline-directory"
    directory.mkdir()
    link = paths.implementation_artifacts / "baseline-link"
    link.symlink_to(destination)

    monkeypatch.setattr(
        publication,
        "_contents",
        lambda *_args, **_kwargs: pytest.fail("capture materialized destination contents"),
    )
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    traced_before = tracemalloc.get_traced_memory()[0]
    try:
        tracemalloc.reset_peak()
        publication.capture(task, paths)
        peak_growth = tracemalloc.get_traced_memory()[1] - traced_before
    finally:
        if started_tracing:
            tracemalloc.stop()

    assert task.artifact_baseline["unrelated-large.bin"] == digest.hexdigest()
    assert task.artifact_baseline["baseline-directory"] == "directory"
    assert task.artifact_baseline["baseline-link"] == "nonregular"
    assert peak_growth < 2 * 1_048_576


def test_destination_helpers_use_size_first_fixed_chunk_reads(publication_case, monkeypatch):
    _task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "bounded.bin"
    expected = b"a" * 96
    destination.write_bytes(expected)
    chunk_size = 32
    requests = []
    open_regular = publication._open_regular

    class GuardedStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            requests.append(size)
            assert 0 < size <= chunk_size
            return self.stream.read(size)

    @contextmanager
    def guarded_open(root, path):
        with open_regular(root, path) as stream:
            yield None if stream is None else GuardedStream(stream)

    monkeypatch.setattr(publication, "_BOUNDED_READ_CHUNK_BYTES", chunk_size)
    monkeypatch.setattr(publication, "_open_regular", guarded_open)
    monkeypatch.setattr(
        publication,
        "_contents",
        lambda *_args, **_kwargs: pytest.fail("destination helper called _contents"),
    )

    observed = publication._destination_observation(paths.implementation_artifacts, destination)
    assert observed == publication._DestinationObservation(
        size=len(expected), digest=hashlib.sha256(expected).hexdigest()
    )
    assert requests == [chunk_size, chunk_size, chunk_size, chunk_size]

    requests.clear()
    assert publication._destination_equals(paths.implementation_artifacts, destination, expected)
    assert requests == [chunk_size, chunk_size, chunk_size, chunk_size]

    empty = paths.implementation_artifacts / "empty.bin"
    empty.write_bytes(b"")
    requests.clear()
    assert publication._destination_equals(paths.implementation_artifacts, empty, b"")
    assert requests == [chunk_size]

    requests.clear()
    assert not publication._destination_equals(
        paths.implementation_artifacts, destination, expected + b"x"
    )
    assert requests == []

    requests.clear()
    assert not publication._destination_equals(
        paths.implementation_artifacts, destination, b"z" + expected[1:]
    )
    assert requests == [chunk_size]


def test_destination_observation_stops_after_one_growth_chunk(publication_case, monkeypatch):
    _task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "continuous-growth.bin"
    destination.write_bytes(b"x")
    open_regular = publication._open_regular
    requests = []

    class GrowingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            requests.append(size)
            return b"g" * size

    @contextmanager
    def growing_open(root, path):
        with open_regular(root, path) as stream:
            yield None if stream is None else GrowingStream(stream)

    monkeypatch.setattr(publication, "_open_regular", growing_open)

    observed = publication._destination_observation(paths.implementation_artifacts, destination)

    assert observed is not None
    assert not observed.complete
    assert requests == [1, publication._BOUNDED_READ_CHUNK_BYTES]


def test_capture_refuses_incomplete_destination_observation(publication_case, monkeypatch):
    task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "growing-capture.bin"
    destination.write_bytes(b"x")
    task.artifact_baseline = None
    open_regular = publication._open_regular

    class GrowingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            return b"g" * size

    @contextmanager
    def growing_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield GrowingStream(stream)
            else:
                yield stream

    monkeypatch.setattr(publication, "_open_regular", growing_open)

    with pytest.raises(publication.PublicationError, match="changed during inventory"):
        publication.capture(task, paths)

    assert task.artifact_baseline is None


def test_capture_refuses_destination_truncated_before_first_read(publication_case, monkeypatch):
    task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "truncated-capture.bin"
    destination.write_bytes(b"operator baseline")
    task.artifact_baseline = None
    open_regular = publication._open_regular
    mutated = False

    class TruncatingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal mutated
            if not mutated:
                mutated = True
                destination.write_bytes(b"")
            return self.stream.read(size)

    @contextmanager
    def truncating_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield TruncatingStream(stream)
            else:
                yield stream

    monkeypatch.setattr(publication, "_open_regular", truncating_open)

    with pytest.raises(publication.PublicationError, match="changed during inventory"):
        publication.capture(task, paths)

    assert mutated
    assert task.artifact_baseline is None


def test_large_exact_destination_publication_has_bounded_extra_allocation(
    publication_case, monkeypatch
):
    task, paths, _source = publication_case
    intended = b"visible" * (2 * 1_048_576)
    destination = paths.implementation_artifacts / "report.bin"
    destination.write_bytes(b"before")
    publication.capture(task, paths)
    task.artifact_payload = {"report.bin": "frozen-large-payload"}
    monkeypatch.setattr(publication.base64, "b64decode", lambda *_args, **_kwargs: intended)
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    traced_before = tracemalloc.get_traced_memory()[0]
    try:
        tracemalloc.reset_peak()
        publication.publish(task, paths)
        peak_growth = tracemalloc.get_traced_memory()[1] - traced_before
    finally:
        if started_tracing:
            tracemalloc.stop()

    assert task.artifact_publication_complete
    assert peak_growth < 2 * 1_048_576


@pytest.mark.parametrize("case", ["authorized", "conflict"])
def test_large_baseline_publication_decisions_have_bounded_extra_allocation(publication_case, case):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / "report.bin"
    large = b"operator" * (2 * 1_048_576)
    if case == "authorized":
        destination.write_bytes(large)
        publication.capture(task, paths)
    bind_and_prepare(task, paths, source)
    if case == "conflict":
        destination.write_bytes(large)
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    traced_before = tracemalloc.get_traced_memory()[0]
    try:
        tracemalloc.reset_peak()
        if case == "conflict":
            with pytest.raises(publication.PublicationError, match="destination conflict"):
                publication.publish(task, paths)
        else:
            publication.publish(task, paths)
        peak_growth = tracemalloc.get_traced_memory()[1] - traced_before
    finally:
        if started_tracing:
            tracemalloc.stop()

    if case == "authorized":
        assert destination.read_bytes() == b"\xff\x00\r\nreport"
        assert task.artifact_publication_complete
    else:
        assert destination.read_bytes() == large
        assert not task.artifact_publication_complete
    assert peak_growth < 2 * 1_048_576


@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "parent-symlink"])
def test_destination_streaming_preserves_shape_checks(
    publication_case, monkeypatch, fallback, kind
):
    _task, paths, source = publication_case
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative reads are unavailable")
    if fallback:
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    destination = root / "shape.bin"
    if kind == "directory":
        destination.mkdir()
    elif kind == "symlink":
        destination.symlink_to(source.implementation_artifacts / "report.bin")
    elif kind == "parent-symlink":
        outside = paths.project / "outside-shape"
        outside.mkdir()
        (outside / "shape.bin").write_bytes(b"outside")
        linked = root / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        destination = linked / "shape.bin"

    if kind == "missing":
        assert publication._destination_observation(root, destination) is None
        assert not publication._destination_equals(root, destination, b"")
    else:
        with pytest.raises(publication.PublicationError, match="symlink|regular file"):
            publication._destination_observation(root, destination)
        with pytest.raises(publication.PublicationError, match="symlink|regular file"):
            publication._destination_equals(root, destination, b"outside")


def test_destination_streaming_propagates_read_fault(publication_case, monkeypatch):
    _task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "fault.bin"
    destination.write_bytes(b"expected")
    open_regular = publication._open_regular

    class FaultingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, _size=-1):
            raise OSError("destination read fault")

    @contextmanager
    def faulting_open(root, path):
        with open_regular(root, path) as stream:
            yield None if stream is None else FaultingStream(stream)

    monkeypatch.setattr(publication, "_open_regular", faulting_open)

    with pytest.raises(OSError, match="destination read fault"):
        publication._destination_observation(paths.implementation_artifacts, destination)
    with pytest.raises(OSError, match="destination read fault"):
        publication._destination_equals(paths.implementation_artifacts, destination, b"expected")


@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
@pytest.mark.parametrize("helper", ["observation", "equals"])
@pytest.mark.parametrize("replacement", ["missing", "directory", "symlink"])
def test_destination_streaming_rejects_detached_descriptor_shape(
    publication_case, monkeypatch, fallback, helper, replacement
):
    _task, paths, _source = publication_case
    if sys.platform == "win32":
        pytest.skip("Windows refuses rename of an open destination")
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative reads are unavailable")
    if fallback:
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    destination = root / "detached.bin"
    detached = root / "detached-old.bin"
    expected = b"expected"
    destination.write_bytes(expected)
    open_regular = publication._open_regular
    replaced = False

    class ReplacingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal replaced
            data = self.stream.read(size)
            if not data and not replaced:
                replaced = True
                destination.rename(detached)
                if replacement == "directory":
                    destination.mkdir()
                elif replacement == "symlink":
                    destination.symlink_to(detached)
            return data

    @contextmanager
    def replacing_open(root, path):
        with open_regular(root, path) as stream:
            yield None if stream is None else ReplacingStream(stream)

    monkeypatch.setattr(publication, "_open_regular", replacing_open)

    if helper == "observation":
        observed = publication._destination_observation(root, destination)
        assert observed is not None
        assert not observed.complete
    else:
        assert not publication._destination_equals(root, destination, expected)
    assert replaced
    assert detached.read_bytes() == expected


@pytest.mark.parametrize("helper", ["observation", "equals"])
def test_destination_streaming_propagates_identity_fault(publication_case, monkeypatch, helper):
    _task, paths, _source = publication_case
    root = paths.implementation_artifacts
    destination = root / "identity-fault.bin"
    expected = b"expected"
    destination.write_bytes(expected)

    def faulting_identity(*_args):
        raise OSError("destination identity fault")

    monkeypatch.setattr(publication, "_destination_path_identity", faulting_identity)

    with pytest.raises(OSError, match="destination identity fault"):
        if helper == "observation":
            publication._destination_observation(root, destination)
        else:
            publication._destination_equals(root, destination, expected)


@pytest.mark.parametrize("inode_available", [True, False], ids=["zero", "unavailable"])
def test_destination_streaming_rejects_indeterminate_inode(
    publication_case, monkeypatch, inode_available
):
    _task, paths, _source = publication_case
    root = paths.implementation_artifacts
    destination = root / "indeterminate-inode.bin"
    expected = b"expected"
    destination.write_bytes(expected)
    real_fstat = os.fstat

    class IndeterminateInode:
        def __init__(self, metadata):
            self.st_dev = metadata.st_dev
            self.st_mode = metadata.st_mode
            self.st_size = metadata.st_size
            if inode_available:
                self.st_ino = 0

    monkeypatch.setattr(os, "fstat", lambda fd: IndeterminateInode(real_fstat(fd)))
    monkeypatch.setattr(
        publication,
        "_destination_path_identity",
        lambda *_args: publication._file_identity(IndeterminateInode(destination.stat())),
    )

    observed = publication._destination_observation(root, destination)
    assert observed is not None
    assert not observed.complete
    assert not publication._destination_equals(root, destination, expected)


@pytest.mark.parametrize("case", ["idempotent", "authorized", "conflict"])
def test_publication_destination_decisions_never_materialize_contents(
    publication_case, monkeypatch, case
):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / "report.bin"
    if case == "authorized":
        destination.write_bytes(b"before")
        publication.capture(task, paths)
    bind_and_prepare(task, paths, source)
    if case == "idempotent":
        destination.write_bytes(b"\xff\x00\r\nreport")
    elif case == "conflict":
        destination.write_bytes(b"operator")
    monkeypatch.setattr(
        publication,
        "_contents",
        lambda *_args, **_kwargs: pytest.fail("publish materialized destination contents"),
    )

    if case == "conflict":
        with pytest.raises(publication.PublicationError, match="conflict.*report.bin"):
            publication.publish(task, paths)
        assert destination.read_bytes() == b"operator"
        assert not task.artifact_publication_complete
    else:
        publication.publish(task, paths)
        assert destination.read_bytes() == b"\xff\x00\r\nreport"
        assert task.artifact_publication_complete


@pytest.mark.parametrize("mutation", ["grow", "shrink"])
def test_initial_idempotence_probe_refuses_file_mutation(publication_case, monkeypatch, mutation):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    intended = b"i" * (publication._BOUNDED_READ_CHUNK_BYTES * 2)
    report.write_bytes(intended)
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    destination.write_bytes(intended)
    open_regular = publication._open_regular
    mutated = False

    class MutatingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal mutated
            data = self.stream.read(size)
            if data and not mutated:
                mutated = True
                if mutation == "grow":
                    with destination.open("ab") as writer:
                        writer.write(b"operator growth")
                else:
                    with destination.open("r+b") as writer:
                        writer.truncate(0)
            return data

    @contextmanager
    def mutating_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield MutatingStream(stream)
            else:
                yield stream

    monkeypatch.setattr(publication, "_open_regular", mutating_open)
    monkeypatch.setattr(
        publication,
        "atomic_write_bytes_confined",
        lambda *_args, **_kwargs: pytest.fail("unstable destination reached replacement"),
    )

    with pytest.raises(publication.PublicationError, match="destination conflict"):
        publication.publish(task, paths)

    assert mutated
    assert destination.read_bytes() != intended
    assert not task.artifact_publication_complete


@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
def test_initial_idempotence_probe_refuses_leaf_replacement(
    publication_case, monkeypatch, fallback
):
    task, paths, source = publication_case
    if sys.platform == "win32":
        pytest.skip("Windows refuses rename of an open destination")
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative reads are unavailable")
    if fallback:
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    intended = b"intended"
    report = source.implementation_artifacts / "report.bin"
    report.write_bytes(intended)
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    detached = paths.implementation_artifacts / "detached-report.bin"
    replacement = b"operator replacement"
    destination.write_bytes(intended)
    open_regular = publication._open_regular
    writer = publication.atomic_write_bytes_confined
    replaced = False

    class ReplacingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal replaced
            data = self.stream.read(size)
            if not data and not replaced:
                replaced = True
                destination.rename(detached)
                destination.write_bytes(replacement)
            return data

    @contextmanager
    def replacing_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield ReplacingStream(stream)
            else:
                yield stream

    monkeypatch.setattr(publication, "_open_regular", replacing_open)

    def refuse_destination_write(path, data, **kwargs):
        if path == destination:
            pytest.fail("detached destination reached replacement")
        writer(path, data, **kwargs)

    monkeypatch.setattr(
        publication,
        "atomic_write_bytes_confined",
        refuse_destination_write,
    )

    with pytest.raises(publication.PublicationError, match="destination conflict"):
        publication.publish(task, paths)

    assert replaced
    assert destination.read_bytes() == replacement
    assert detached.read_bytes() == intended
    assert not task.artifact_publication_complete


def test_growing_destination_between_observations_refuses_replace(publication_case, monkeypatch):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / "report.bin"
    destination.write_bytes(b"before")
    publication.capture(task, paths)
    bind_and_prepare(task, paths, source)

    def operator_growth(*_args):
        with destination.open("ab") as stream:
            stream.write(b"g" * (publication._BOUNDED_READ_CHUNK_BYTES * 3))
        return False

    monkeypatch.setattr(publication.verify, "path_tracked", operator_growth)
    monkeypatch.setattr(
        publication,
        "atomic_write_bytes_confined",
        lambda *_args, **_kwargs: pytest.fail("replacement staged before destination recheck"),
    )
    monkeypatch.setattr(
        publication,
        "_contents",
        lambda *_args, **_kwargs: pytest.fail("publish materialized growing destination"),
    )

    with pytest.raises(publication.PublicationError, match="changed during publication"):
        publication.publish(task, paths)

    assert destination.read_bytes().startswith(b"before")
    assert destination.stat().st_size > publication._BOUNDED_READ_CHUNK_BYTES
    assert not task.artifact_publication_complete


def test_initial_equality_and_baseline_identity_share_one_probe(publication_case, monkeypatch):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / "report.bin"
    destination.write_bytes(b"before")
    publication.capture(task, paths)
    bind_and_prepare(task, paths, source)
    destination.write_bytes(b"conflicting operator bytes")
    probe_destination = publication._probe_destination
    probes = 0

    def probe_then_restore_baseline(root, path, expected=None):
        nonlocal probes
        result = probe_destination(root, path, expected)
        if path == destination:
            probes += 1
            if probes == 1:
                destination.write_bytes(b"before")
        return result

    monkeypatch.setattr(publication, "_probe_destination", probe_then_restore_baseline)

    with pytest.raises(publication.PublicationError, match="destination conflict"):
        publication.publish(task, paths)

    assert probes == 1
    assert destination.read_bytes() == b"before"
    assert not task.artifact_publication_complete


@pytest.mark.parametrize("mutation", ["grow", "shrink"])
def test_post_write_visibility_refuses_file_mutation_during_read(
    publication_case, monkeypatch, mutation
):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    open_regular = publication._open_regular
    mutated = False

    class MutatingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal mutated
            data = self.stream.read(size)
            if data and not mutated:
                mutated = True
                if mutation == "grow":
                    with destination.open("ab") as writer:
                        writer.write(b"operator growth")
                else:
                    with destination.open("r+b") as writer:
                        writer.truncate(0)
            return data

    @contextmanager
    def mutating_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield MutatingStream(stream)
            else:
                yield stream

    def write_then_mutate(path, data, **kwargs):
        kwargs["_before_replace"]()
        path.write_bytes(data)

    monkeypatch.setattr(publication, "_open_regular", mutating_open)
    monkeypatch.setattr(publication, "atomic_write_bytes_confined", write_then_mutate)

    with pytest.raises(publication.PublicationError, match="not visible at destination"):
        publication.publish(task, paths)

    assert mutated
    assert not task.artifact_publication_complete


@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
def test_post_write_visibility_refuses_leaf_replacement(publication_case, monkeypatch, fallback):
    task, paths, source = publication_case
    if sys.platform == "win32":
        pytest.skip("Windows refuses rename of an open destination")
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative reads are unavailable")
    if fallback:
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    intended = b"intended"
    report = source.implementation_artifacts / "report.bin"
    report.write_bytes(intended)
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    detached = paths.implementation_artifacts / "detached-report.bin"
    replacement = b"operator replacement"
    open_regular = publication._open_regular
    replaced = False

    class ReplacingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal replaced
            data = self.stream.read(size)
            if not data and not replaced:
                replaced = True
                destination.rename(detached)
                destination.write_bytes(replacement)
            return data

    @contextmanager
    def replacing_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield ReplacingStream(stream)
            else:
                yield stream

    def write_then_check(path, data, **kwargs):
        kwargs["_before_replace"]()
        path.write_bytes(data)

    monkeypatch.setattr(publication, "_open_regular", replacing_open)
    monkeypatch.setattr(publication, "atomic_write_bytes_confined", write_then_check)

    with pytest.raises(publication.PublicationError, match="not visible at destination"):
        publication.publish(task, paths)

    assert replaced
    assert destination.read_bytes() == replacement
    assert detached.read_bytes() == intended
    assert not task.artifact_publication_complete


@pytest.mark.parametrize(
    "visible",
    [None, b"short", b"\xff\x00\r\nreport-more", b"\x00\x00\r\nreport"],
    ids=["missing", "truncated", "extended", "different"],
)
def test_visibility_check_streams_and_refuses_inexact_destination(
    publication_case, monkeypatch, visible
):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)

    def inexact_write(path, _data, **kwargs):
        kwargs["_before_replace"]()
        if visible is not None:
            path.write_bytes(visible)

    monkeypatch.setattr(publication, "atomic_write_bytes_confined", inexact_write)
    monkeypatch.setattr(
        publication,
        "_contents",
        lambda *_args, **_kwargs: pytest.fail("visibility check materialized destination"),
    )

    with pytest.raises(publication.PublicationError, match="not visible at destination"):
        publication.publish(task, paths)

    assert not task.artifact_publication_complete


@pytest.mark.parametrize("relative", ["report.bin", "errata/correction.md"])
def test_existing_baseline_allows_replace(publication_case, relative):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"before")
    output = source.implementation_artifacts / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"\xff\x00\r\nreport")
    (source.implementation_artifacts / "spec.md").write_text(
        f"---\nstatus: done\nartifact_deliverables: [{relative}]\n---\n"
    )
    publication.capture(task, paths)
    bind_and_prepare(task, paths, source)
    publication.publish(task, paths)
    assert destination.read_bytes() == b"\xff\x00\r\nreport"


def test_partial_write_replays_saved_intent(publication_case, monkeypatch):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    writer = publication.atomic_write_bytes_confined

    def interrupted(path, data, **kw):
        writer(path, data, **kw)
        raise OSError("host lost after replacement")

    monkeypatch.setattr(publication, "atomic_write_bytes_confined", interrupted)
    with pytest.raises(OSError, match="host lost"):
        publication.publish(task, paths)
    back = StoryTask.from_dict(task.to_dict())
    assert not back.artifact_publication_complete
    (source.implementation_artifacts / "report.bin").write_bytes(b"unverified")
    monkeypatch.setattr(publication, "atomic_write_bytes_confined", writer)
    publication.publish(back, paths)
    assert (paths.implementation_artifacts / "report.bin").read_bytes() == b"\xff\x00\r\nreport"
    assert back.artifact_publication_complete


@pytest.mark.parametrize(
    "declaration",
    [
        "../escape",
        "/absolute",
        "C:/absolute",
        "*.md",
        "dir/../x",
        "deferred-work.md",
        "sprint-status.yaml",
        "report.bin/",
        "SPRINT-STATUS.YAML",
        "Deferred-Work.md",
        "sprint-status.yaml. ",
        "deferred-work.md ",
        "dir./report.bin",
        "NUL.txt",
        "report.bin:stream",
        "...",
    ],
)
def test_invalid_paths_refused(publication_case, declaration):
    task, paths, source = publication_case
    (source.implementation_artifacts / "spec.md").write_text(
        f"---\nartifact_deliverables: ['{declaration}']\n---\n"
    )
    with pytest.raises(publication.PublicationError, match="invalid artifact|reserved"):
        bind_and_prepare(task, paths, source)
    assert task.artifact_payload is None


@pytest.mark.parametrize("declaration", ["null", "report.bin", "{}", "[null]"])
def test_malformed_list_refused(publication_case, declaration):
    task, paths, source = publication_case
    (source.implementation_artifacts / "spec.md").write_text(
        f"---\nartifact_deliverables: {declaration}\n---\n"
    )
    with pytest.raises(publication.PublicationError):
        bind_and_prepare(task, paths, source)


@pytest.mark.parametrize("kind", ["directory", "symlink", "parent-symlink", "missing"])
def test_nonregular_sources_refused(publication_case, kind):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    report.unlink()
    if kind == "directory":
        report.mkdir()
    elif kind == "symlink":
        report.symlink_to(source.implementation_artifacts / "spec.md")
    elif kind == "parent-symlink":
        linked = source.implementation_artifacts / "linked"
        linked.symlink_to(paths.implementation_artifacts, target_is_directory=True)
        (paths.implementation_artifacts / "report.bin").write_bytes(b"outside")
        (source.implementation_artifacts / "spec.md").write_text(
            "---\nartifact_deliverables: [linked/report.bin]\n---\n"
        )
    with pytest.raises(publication.PublicationError):
        bind_and_prepare(task, paths, source)


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink"])
def test_future_tracked_declaration_still_validates_source_shape(
    publication_case, monkeypatch, kind
):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    report.unlink()
    if kind == "directory":
        report.mkdir()
    elif kind == "symlink":
        report.symlink_to(source.implementation_artifacts / "spec.md")
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: False)
    monkeypatch.setattr(publication.verify, "path_ignored", lambda *_: False)

    with pytest.raises(publication.PublicationError, match="missing|regular file|symlink"):
        bind_and_prepare(task, paths, source)

    assert task.artifact_source_digests is None
    assert task.artifact_payload is None


def test_old_state_cannot_create_overwrite_authority(publication_case):
    task, paths, source = publication_case
    task.artifact_baseline = None
    bind_and_prepare(task, paths, source)
    with pytest.raises(publication.PublicationError, match="no pre-execution"):
        publication.publish(task, paths)
    assert task.artifact_payload is not None
    assert base64.b64decode(task.artifact_payload["report.bin"]) == b"\xff\x00\r\nreport"


def test_destination_symlink_refused_even_when_equal(publication_case):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    destination.symlink_to(source.implementation_artifacts / "report.bin")
    with pytest.raises(publication.PublicationError, match="symlink"):
        publication.publish(task, paths)
    assert destination.is_symlink()


def test_destination_changed_during_git_probe_is_preserved(publication_case, monkeypatch):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"

    def operator_edit(*_):
        destination.write_bytes(b"operator while git ran")
        return False

    monkeypatch.setattr(publication.verify, "path_tracked", operator_edit)
    with pytest.raises(publication.PublicationError, match="changed during publication"):
        publication.publish(task, paths)
    assert destination.read_bytes() == b"operator while git ran"


def test_tracked_deliverables_ride_git(publication_case, monkeypatch):
    task, paths, source = publication_case
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: True)
    bind_and_prepare(task, paths, source)
    assert task.artifact_payload == {}
    publication.publish(task, paths)
    assert task.artifact_publication_complete


def test_destination_that_becomes_tracked_is_refused(publication_case, monkeypatch):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: True)
    with pytest.raises(publication.PublicationError, match="became tracked"):
        publication.publish(task, paths)
    assert not destination.exists()
    assert not task.artifact_publication_complete


def test_destination_that_becomes_unignored_is_refused(publication_case, monkeypatch):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    monkeypatch.setattr(publication.verify, "path_ignored", lambda *_: False)
    with pytest.raises(publication.PublicationError, match="no longer ignored"):
        publication.publish(task, paths)
    assert not destination.exists()
    assert not task.artifact_publication_complete


def test_unignored_declaration_is_left_to_the_pending_git_commit(publication_case, monkeypatch):
    task, paths, source = publication_case
    monkeypatch.setattr(publication.verify, "path_ignored", lambda *_: False)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    assert task.artifact_source_digests == {}
    assert set(task.artifact_tracked_source_oids) == {"report.bin", "spec.md"}
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: True)
    publication.prepare(task, paths, source)
    assert task.artifact_payload == {}


def test_binding_records_git_normalized_tracked_and_pending_identities(project):
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "tracked-spec.md"
    tracked = root / "tracked.txt"
    pending = root / "pending.txt"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [tracked.txt, pending.txt]\n---\n")
    tracked.write_bytes(b"accepted\r\n")
    (project.project / ".gitattributes").write_text("*.txt text eol=lf\n")
    git(project.project, "add", ".gitattributes", spec, tracked)
    git(project.project, "commit", "-q", "-m", "tracked publication inputs")
    pending.write_bytes(b"pending\r\n")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)

    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, project)

    assert task.artifact_source_digests == {}
    assert set(task.artifact_tracked_source_oids) == {
        "pending.txt",
        "tracked-spec.md",
        "tracked.txt",
    }
    tracked_rel = tracked.relative_to(project.repo_root).as_posix()
    assert task.artifact_tracked_source_oids["tracked.txt"] == git(
        project.project, "hash-object", f"--path={tracked_rel}", tracked
    )

    first = dict(task.artifact_tracked_source_oids)
    tracked.write_bytes(b"accepted repair\r\n")
    pending.write_bytes(b"pending repair\r\n")
    assert publication.arm_binding(task, "review:1")
    publication.bind_armed(task, project)
    assert task.artifact_tracked_source_oids["tracked.txt"] != first["tracked.txt"]
    assert task.artifact_tracked_source_oids["pending.txt"] != first["pending.txt"]


def test_non_spec_git_deliverable_binding_hashes_a_streamed_confined_snapshot(project, monkeypatch):
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "spec.md"
    report = root / "large-report.bin"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [large-report.bin]\n---\n")
    report.write_bytes(b"tracked bytes")
    git(project.repo_root, "add", "-A")
    git(project.repo_root, "commit", "-q", "-m", "tracked publication inputs")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)
    publication.arm_binding(task, "dev:0")
    path_hash = publication.verify.git_normalized_blob_oid
    bytes_hash = publication.verify.git_normalized_blob_oid_for_bytes
    path_calls = []
    bytes_calls = []

    def record_path_hash(repo, rel, path):
        assert path != report
        assert path.is_file() and path.read_bytes() == b"tracked bytes"
        path_calls.append(path)
        return path_hash(repo, rel, path)

    def record_bytes_hash(repo, rel, data):
        bytes_calls.append(data)
        return bytes_hash(repo, rel, data)

    monkeypatch.setattr(publication.verify, "git_normalized_blob_oid", record_path_hash)
    monkeypatch.setattr(publication.verify, "git_normalized_blob_oid_for_bytes", record_bytes_hash)

    publication.bind_armed(task, project)

    assert len(path_calls) == 1
    assert path_calls[0] != report and not path_calls[0].exists()
    assert bytes_calls == [spec.read_bytes()]

    publication.arm_binding(task, "dev:1")
    open_regular = publication._open_regular

    class GrowingStream:
        def __init__(self, stream):
            self.stream = stream
            self.grew = False

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            chunk = self.stream.read(size)
            if not self.grew:
                self.grew = True
                with report.open("ab") as writer:
                    writer.write(b"growth")
            return chunk

    @contextmanager
    def grow_report_during_copy(open_root, path):
        with open_regular(open_root, path) as stream:
            if path == report and stream is not None:
                yield GrowingStream(stream)
            else:
                yield stream

    monkeypatch.setattr(publication, "_open_regular", grow_report_during_copy)

    with pytest.raises(publication.PublicationError, match="changed during binding"):
        publication.bind_armed(task, project)

    assert len(path_calls) == 1


def test_staged_validation_refuses_sorted_paths_without_exposing_object_ids(project):
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "spec.md"
    first = root / "z-last.txt"
    second = root / "a-first.txt"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [z-last.txt, a-first.txt]\n---\n")
    first.write_text("accepted z\n")
    second.write_text("accepted a\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "accepted publication inputs")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, project)
    accepted_oids = set(task.artifact_tracked_source_oids.values())
    first.write_text("later z\n")
    second.write_text("later a\n")
    git(project.project, "add", "-A")

    with pytest.raises(publication.PublicationError) as raised:
        publication.validate_staged(task, project)

    assert str(raised.value).endswith("a-first.txt, z-last.txt")
    assert not any(oid in str(raised.value) for oid in accepted_oids)


def test_staged_validation_refuses_an_ignored_deliverable_that_becomes_tracked(
    publication_case,
):
    task, _paths, source = publication_case
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    report = source.implementation_artifacts / "report.bin"
    report_rel = report.relative_to(source.repo_root).as_posix()
    git(source.repo_root, "add", "-f", "--", report_rel)

    with pytest.raises(publication.PublicationError, match=r"report\.bin"):
        publication.validate_staged(task, source)


def test_staged_validation_refuses_missing_or_malformed_persisted_maps(project):
    incomplete = StoryTask(story_key="dw-fix", epic=0)
    with pytest.raises(publication.PublicationError, match="binding is missing"):
        publication.validate_staged(incomplete, project)

    malformed = StoryTask(story_key="dw-fix", epic=0)
    malformed.artifact_source_digests = {}  # type: ignore[reportAssignmentType]
    malformed.artifact_tracked_source_oids = []  # type: ignore[reportAssignmentType]
    with pytest.raises(publication.PublicationError, match="malformed"):
        publication.validate_staged(malformed, project)


def test_integrated_validation_checks_commit_and_post_hook_index_with_path_only_errors(project):
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "spec.md"
    report = root / "report.bin"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [report.bin]\n---\n")
    report.write_bytes(b"accepted\r\n")
    (project.project / ".gitattributes").write_text("*.bin text eol=lf\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "accepted target")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, project)
    accepted_oids = set(task.artifact_tracked_source_oids.values())

    assert publication.validate_integrated(task, project, "HEAD") is True

    report.write_bytes(b"hook drift\n")
    git(project.project, "add", "--", report)
    with pytest.raises(publication.PublicationError) as raised:
        publication.validate_integrated(task, project, "HEAD")
    assert str(raised.value).endswith("report.bin")
    assert not any(oid in str(raised.value) for oid in accepted_oids)


def test_integrated_validation_preserves_legacy_frozen_payload_compatibility(project):
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"])
    task.artifact_source_digests = {"report.bin": "legacy-digest"}
    task.artifact_tracked_source_oids = None
    task.artifact_acceptance_identity = "legacy-owner"
    task.artifact_payload = {"report.bin": "bGVnYWN5"}

    assert publication.validate_integrated(task, project, "HEAD") is False


@pytest.mark.parametrize("payload", [["not-a-map"], {"report.bin": "not base64!"}])
def test_integrated_validation_refuses_malformed_legacy_payload(project, payload):
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"])
    task.artifact_payload = payload  # type: ignore[reportAssignmentType]

    with pytest.raises(publication.PublicationError, match="payload"):
        publication.validate_integrated(task, project, "HEAD")


def test_integrated_validation_refuses_partial_modern_authority(project):
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"])
    task.artifact_payload = {}
    task.artifact_tracked_source_oids = {}
    task.artifact_source_digests = None
    task.artifact_acceptance_identity = "review:dev:0"

    with pytest.raises(publication.PublicationError, match="binding"):
        publication.validate_integrated(task, project, "HEAD")


def test_integrated_validation_binds_modern_payload_to_accepted_ignored_digests(project):
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"])
    task.artifact_payload = {"report.bin": base64.b64encode(b"changed").decode("ascii")}
    task.artifact_source_digests = {"report.bin": hashlib.sha256(b"accepted").hexdigest()}
    task.artifact_tracked_source_oids = {}
    task.artifact_acceptance_identity = "review:dev:0"

    with pytest.raises(publication.PublicationError, match="differs from accepted"):
        publication.validate_integrated(task, project, "HEAD")


def test_integrated_validation_refuses_commit_drift_even_when_index_is_accepted(project):
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "spec.md"
    report = root / "report.bin"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [report.bin]\n---\n")
    report.write_bytes(b"accepted\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "accepted target")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, project)
    accepted_oids = set(task.artifact_tracked_source_oids.values())
    report.write_bytes(b"commit drift\n")
    git(project.project, "add", "--", report)
    git(project.project, "commit", "-q", "-m", "drifted target")
    report.write_bytes(b"accepted\n")
    git(project.project, "add", "--", report)

    with pytest.raises(publication.PublicationError) as raised:
        publication.validate_integrated(task, project, "HEAD")

    assert str(raised.value).endswith("report.bin")
    assert not any(oid in str(raised.value) for oid in accepted_oids)


def test_integrated_validation_keeps_accepted_ignored_paths_absent(publication_case):
    task, _paths, source = publication_case
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    report = source.implementation_artifacts / "report.bin"
    git(source.repo_root, "add", "-f", "--", report)

    with pytest.raises(publication.PublicationError, match=r"report\.bin"):
        publication.validate_integrated(task, source, "HEAD")


def test_unignored_untracked_declaration_must_be_tracked_by_preparation(
    publication_case, monkeypatch
):
    task, paths, source = publication_case
    nested = source.implementation_artifacts / "embedded"
    (nested / ".git").mkdir(parents=True)
    output = nested / "output.bin"
    output.write_bytes(b"nested repository output")
    (source.implementation_artifacts / "spec.md").write_text(
        "---\nstatus: done\nartifact_deliverables: [embedded/output.bin]\n---\n"
    )
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: False)
    monkeypatch.setattr(
        publication.verify,
        "path_ignored",
        lambda _repo, path: path.name == "spec.md",
    )

    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    assert set(task.artifact_source_digests) == {"spec.md"}

    with pytest.raises(publication.PublicationError, match="was not tracked.*embedded"):
        publication.prepare(task, paths, source)

    assert task.artifact_payload is None


def test_external_spec_tracked_declaration_swap_is_refused_by_preparation(project):
    # A spec inside the project but outside implementation_artifacts is never
    # itself a selected deliverable, so the ignored map alone ({} == {}) cannot
    # see its declarations move; the tracked rel set has to be re-derived and
    # compared whole (Codex P1 on #795).
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    accepted = root / "a.bin"
    swapped = root / "b.bin"
    accepted.write_bytes(b"accepted deliverable")
    swapped.write_bytes(b"unaccepted deliverable")
    spec = project.project / "docs" / "external-spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("---\nstatus: done\nartifact_deliverables: [a.bin]\n---\n")
    git(project.repo_root, "add", "-A")
    git(project.repo_root, "commit", "-q", "-m", "tracked publication inputs")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, project)
    assert task.artifact_source_digests == {}
    assert set(task.artifact_tracked_source_oids) == {"a.bin"}
    bound = dict(task.artifact_tracked_source_oids)

    spec.write_text("---\nstatus: done\nartifact_deliverables: [b.bin]\n---\n")

    with pytest.raises(
        publication.PublicationError,
        match="tracked artifact deliverables changed since accepted verification: a\\.bin, b\\.bin",
    ) as exc:
        publication.prepare(task, project, project)

    assert bound["a.bin"] not in str(exc.value)
    assert task.artifact_payload is None
    assert task.artifact_tracked_source_oids == bound

    spec.write_text("---\nstatus: done\nartifact_deliverables: [a.bin]\n---\n")
    publication.prepare(task, project, project)
    assert task.artifact_payload == {}


def test_preparation_never_reopens_a_tracked_deliverable_behind_the_sealed_commit(
    publication_case, monkeypatch
):
    # The rel-set check must be read off the classification alone: a tracked
    # deliverable removed from the working tree after `finalize_commit` sealed
    # it is tolerated (the commit carries it), not a refusal (CodeRabbit on
    # #795 round 4).
    task, paths, source = publication_case
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: True)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    assert set(task.artifact_tracked_source_oids) == {"report.bin", "spec.md"}
    (source.implementation_artifacts / "report.bin").unlink()

    publication.prepare(task, paths, source)

    assert task.artifact_payload == {}


def test_read_fault_retains_baseline_and_refuses_payload(publication_case, monkeypatch):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    read = publication._contents

    def unreadable(root, path, **kwargs):
        if path == report:
            raise OSError("unreadable report.bin")
        return read(root, path, **kwargs)

    monkeypatch.setattr(publication, "_contents", unreadable)
    with pytest.raises(OSError, match="unreadable"):
        bind_and_prepare(task, paths, source)
    assert task.artifact_baseline is not None
    assert task.artifact_payload is None


def test_accepted_spec_parent_traversal_refused_before_read(publication_case, monkeypatch):
    task, paths, source = publication_case
    task.spec_file = str(source.project / ".." / "escaped.md")
    escaped = source.project.parent / "escaped.md"
    escaped.write_text("---\nstatus: done\n---\n")
    with pytest.raises(publication.PublicationError, match="parent traversal"):
        bind_and_prepare(task, paths, source)
    assert task.artifact_payload is None


@pytest.mark.parametrize(
    "text",
    [
        "---\nartifact_deliverables: [report.bin\n---\n",
        "---\n- report.bin\n---\n",
        "---\nstatus: done\n",
        "not frontmatter",
        "---\n{}\n---\n",
    ],
)
def test_malformed_frontmatter_refuses_publication_intent(publication_case, text):
    task, paths, source = publication_case
    (source.implementation_artifacts / "spec.md").write_text(text)
    with pytest.raises(publication.PublicationError, match="invalid accepted spec frontmatter"):
        bind_and_prepare(task, paths, source)
    assert task.artifact_payload is None


def test_external_declaration_is_refused(publication_case, tmp_path):
    from dataclasses import replace

    task, paths, source = publication_case
    external = replace(source, implementation_artifacts=tmp_path / "external")
    external.implementation_artifacts.mkdir()
    with pytest.raises(publication.PublicationError, match="strictly inside"):
        bind_and_prepare(task, paths, external)
    assert task.artifact_payload is None


@pytest.mark.parametrize("fallback", [False, True])
def test_destination_edit_during_fsync_refuses_replace(publication_case, monkeypatch, fallback):
    from bmad_loop import platform_util

    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    fsync = os.fsync

    def edit_during_fsync(fd):
        fsync(fd)
        destination.write_bytes(b"operator during fsync")

    if fallback:
        monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
        monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    monkeypatch.setattr(os, "fsync", edit_during_fsync)
    with pytest.raises(publication.PublicationError, match="changed during publication"):
        publication.publish(task, paths)
    assert destination.read_bytes() == b"operator during fsync"
    assert not task.artifact_publication_complete
    assert list(destination.parent.glob("*.tmp")) == []


def test_publication_refuses_when_confined_write_is_not_visible(publication_case, monkeypatch):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"

    def detached_write(path, data, **kwargs):
        kwargs["_before_replace"]()
        (path.parent / "detached-report.bin").write_bytes(data)

    monkeypatch.setattr(publication, "atomic_write_bytes_confined", detached_write)
    with pytest.raises(publication.PublicationError, match="not visible at destination"):
        publication.publish(task, paths)
    assert not destination.exists()
    assert not task.artifact_publication_complete


@pytest.mark.skipif(not publication.DIR_FD_ANCHORED_WRITES, reason="POSIX descriptor reads")
@pytest.mark.parametrize("swap", ["file", "parent"])
def test_source_swap_between_check_and_read_is_refused(publication_case, monkeypatch, swap):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    outside = paths.implementation_artifacts / "outside"
    outside.mkdir()
    (outside / report.name).write_bytes(b"outside secrets")
    if swap == "file":
        opener = os.open

        def swap_file(name, flags, *args, **kwargs):
            if name == report.name and "dir_fd" in kwargs:
                report.unlink()
                report.symlink_to(outside / report.name)
            return opener(name, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", swap_file)
    else:
        opener = publication.open_dir_confined

        def swap_parent(root, parent):
            report.parent.rename(report.parent.with_name("original"))
            report.parent.symlink_to(outside, target_is_directory=True)
            return opener(root, parent)

        monkeypatch.setattr(publication, "open_dir_confined", swap_parent)
    with pytest.raises((OSError, publication.PublicationError)):
        publication._contents(source.project, report)
    assert (outside / report.name).read_bytes() == b"outside secrets"


@pytest.mark.skipif(not publication.DIR_FD_ANCHORED_WRITES, reason="POSIX descriptor inventory")
def test_baseline_directory_swap_never_reads_redirected_contents(publication_case, monkeypatch):
    task, paths, _ = publication_case
    root = paths.implementation_artifacts
    directory = root / "nested"
    directory.mkdir()
    (directory / "report.bin").write_bytes(b"before")
    outside = paths.project / "outside"
    outside.mkdir()
    (outside / "report.bin").write_bytes(b"outside secrets")
    inode = directory.stat().st_ino
    scandir = os.scandir

    def swap_directory(fd):
        if isinstance(fd, int) and os.fstat(fd).st_ino == inode:
            directory.rename(root / "original")
            directory.symlink_to(outside, target_is_directory=True)
        return scandir(fd)

    monkeypatch.setattr(os, "scandir", swap_directory)
    with pytest.raises(publication.PublicationError, match="symlink"):
        publication.capture(task, paths)


def test_undecodable_accepted_spec_refuses_intent(publication_case):
    task, paths, source = publication_case
    (source.implementation_artifacts / "spec.md").write_bytes(b"---\nstatus: done\n\xff\n---\n")
    with pytest.raises(UnicodeDecodeError):
        bind_and_prepare(task, paths, source)
    assert task.artifact_payload is None
