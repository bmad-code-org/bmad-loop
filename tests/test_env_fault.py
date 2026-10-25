"""EnvFaultMixin's multi-file scan: ``ENV_FAULT_LOG_SUFFIXES``.

The transport-specific halves (which file each adapter names, the shipped
patterns) live in tests/test_generic_tmux.py, tests/test_opencode_http.py and
tests/test_env_fault_patterns.py; these pin the mixin's own contract on a bare
host: every named file is scanned, the last match across them wins, and each
unreadable file is its own crumb while the rest still count.
"""

from __future__ import annotations

from pathlib import Path

import regex

from bmad_loop.adapters.base import SessionHandle, SessionResult, SessionSpec
from bmad_loop.adapters.env_fault import EnvFaultMixin
from bmad_loop.adapters.generic import GenericTmuxAdapter
from bmad_loop.adapters.opencode_http import OpencodeHttpAdapter

TASK = "t-1"


class _Host(EnvFaultMixin):
    """The smallest host the mixin's contract names: logs_dir, a lifecycle sink,
    and read_usage (untracked, so no #764 veto)."""

    ENV_FAULT_LOG_SUFFIXES = (".a.out", ".b.log")

    def __init__(self, logs_dir: Path):
        self.logs_dir = logs_dir
        self.crumbs: list[dict] = []
        self._env_fault_patterns = (regex.compile(r"^OUTAGE \w+"),)
        self._usage_limit_patterns = ()
        self._usage_wait_patterns = ()

    def _note_lifecycle(self, task_id: str, event: str, **fields: object) -> None:
        self.crumbs.append({"task": task_id, "event": event, **fields})

    def read_usage(self, result: SessionResult):
        return None


def _classify(host: EnvFaultMixin) -> SessionResult:
    handle = SessionHandle(task_id=TASK, native_id=TASK)
    spec = SessionSpec(task_id=TASK, role="dev", prompt="/x", cwd=Path("."))
    return host._classify_env_fault(handle, spec, SessionResult(status="timeout"))


def _write(host: _Host, suffix: str, text: str) -> None:
    (host.logs_dir / f"{TASK}{suffix}").write_text(text, encoding="utf-8")


def test_env_fault_scans_every_suffix(tmp_path):
    """A match in the SECOND file classifies: the scan does not stop at the
    primary one. ABLATION: scan only `_env_fault_log_path` and this reddens."""
    host = _Host(tmp_path)
    _write(host, ".a.out", "healthy server chatter\n")
    _write(host, ".b.log", "OUTAGE second\n")
    result = _classify(host)
    assert result.env_fault is True
    assert result.env_fault_evidence == "OUTAGE second"
    assert [c["event"] for c in host.crumbs] == ["env-fault-classified"]


def test_env_fault_last_match_across_files_wins(tmp_path):
    """Files are scanned in suffix order and the last match wins across them,
    as it does within one file."""
    host = _Host(tmp_path)
    _write(host, ".a.out", "OUTAGE first\nOUTAGE first-later\n")
    _write(host, ".b.log", "OUTAGE second\nnoise\n")
    assert _classify(host).env_fault_evidence == "OUTAGE second"
    # ...and a later file with no match does not erase an earlier one's.
    _write(host, ".b.log", "noise\n")
    assert _classify(host).env_fault_evidence == "OUTAGE first-later"


def test_missing_secondary_log_is_read_fault_crumb(tmp_path):
    """A missing file is a read fault — crumbed, never a clean answer — and it
    is per file: the other file is still scanned and still classifies."""
    host = _Host(tmp_path)
    _write(host, ".a.out", "OUTAGE first\n")
    result = _classify(host)
    assert result.env_fault is True
    assert result.env_fault_evidence == "OUTAGE first"
    failed = [c for c in host.crumbs if c["event"] == "env-fault-scan-failed"]
    assert len(failed) == 1
    assert failed[0]["stage"] == "read"
    assert failed[0]["log"] == str(tmp_path / f"{TASK}.b.log")
    assert failed[0]["error"].startswith("FileNotFoundError: ")


def test_single_suffix_host_scans_only_that_file(tmp_path):
    """An adapter class that only ever set ``ENV_FAULT_LOG_SUFFIX`` (the
    documented single-file knob) keeps scanning exactly that file."""

    class _OneFile(_Host):
        ENV_FAULT_LOG_SUFFIXES = ()
        ENV_FAULT_LOG_SUFFIX = ".diag"

    host = _OneFile(tmp_path)
    assert host._env_fault_log_paths(TASK) == (tmp_path / f"{TASK}.diag",)
    _write(host, ".diag", "OUTAGE only\n")
    assert _classify(host).env_fault_evidence == "OUTAGE only"
    assert [c["event"] for c in host.crumbs] == ["env-fault-classified"]


def test_tmux_adapters_still_scan_only_dot_log(tmp_path):
    """The tmux adapters scan their pane capture and nothing else: a second
    file would be a second model-written surface their patterns were never
    held against. ABLATION: give EnvFaultMixin a multi-file default and this
    reddens."""
    assert GenericTmuxAdapter.ENV_FAULT_LOG_SUFFIXES == ()
    assert GenericTmuxAdapter.ENV_FAULT_LOG_SUFFIX == ".log"
    host = GenericTmuxAdapter.__new__(GenericTmuxAdapter)
    host.logs_dir = tmp_path  # the only attribute the path helpers read
    assert host._env_fault_log_paths(TASK) == (tmp_path / f"{TASK}.log",)


def test_opencode_scans_server_out_then_provider_errors(tmp_path):
    """opencode-http names both its model-free files, server log first — and
    never the `.log` transcript, which carries the model's own words."""
    host = OpencodeHttpAdapter.__new__(OpencodeHttpAdapter)
    host.logs_dir = tmp_path
    assert host._env_fault_log_paths(TASK) == (
        tmp_path / f"{TASK}.server.out",
        tmp_path / f"{TASK}.provider-errors.log",
    )
    assert host._env_fault_log_path(TASK) == tmp_path / f"{TASK}.server.out"
