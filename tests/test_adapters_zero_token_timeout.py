"""DW-364: the base adapter's zero-token-timeout post-mortem.

`CodingCLIAdapter.run()` ends with `_classify_zero_token_timeout`, which stamps
`env_fault` on a `timeout` whose usage is TRACKED and totals zero — a CLI that
never got a usable response from the provider. Driven through `MockAdapter.run()`,
which inherits the base `run()`, over the spec's I/O matrix.

ABLATION: with the `_classify_zero_token_timeout` call removed from `run()`,
`test_tracked_zero_timeout_is_env_fault` fails (env_fault stays False).
"""

from __future__ import annotations

import pytest

from bmad_loop.adapters.base import (
    ZERO_TOKEN_TIMEOUT_EVIDENCE,
    SessionResult,
    SessionSpec,
)
from bmad_loop.adapters.mock import MockAdapter
from bmad_loop.model import TokenUsage


def _spec(tmp_path) -> SessionSpec:
    return SessionSpec(task_id="t1", role="dev", prompt="p", cwd=tmp_path)


class _CountingAdapter(MockAdapter):
    """MockAdapter that counts `read_usage` calls and can raise from it."""

    def __init__(self, script, usage_per_session=None, raises: Exception | None = None):
        super().__init__(script, usage_per_session=usage_per_session)
        self.usage_reads = 0
        self.raises = raises

    def read_usage(self, result: SessionResult) -> TokenUsage | None:
        self.usage_reads += 1
        if self.raises is not None:
            raise self.raises
        return super().read_usage(result)


def test_tracked_zero_timeout_is_env_fault(tmp_path):
    adapter = MockAdapter([SessionResult(status="timeout")], usage_per_session=TokenUsage())
    result = adapter.run(_spec(tmp_path))
    assert result.status == "timeout"
    assert result.env_fault is True
    assert result.env_fault_evidence == ZERO_TOKEN_TIMEOUT_EVIDENCE


def test_untracked_timeout_is_unchanged(tmp_path):
    scripted = SessionResult(status="timeout")
    adapter = MockAdapter([scripted], usage_per_session=None)
    result = adapter.run(_spec(tmp_path))
    assert result == scripted
    assert result.env_fault is False


def test_tracked_nonzero_timeout_is_unchanged(tmp_path):
    scripted = SessionResult(status="timeout")
    adapter = MockAdapter([scripted], usage_per_session=TokenUsage(cache_read_tokens=1))
    result = adapter.run(_spec(tmp_path))
    assert result == scripted
    assert result.env_fault is False


def test_pattern_matched_env_fault_wins_and_skips_usage_read(tmp_path):
    evidence = "API Error: Unable to connect (ECONNREFUSED)"
    scripted = SessionResult(status="timeout", env_fault=True, env_fault_evidence=evidence)
    adapter = _CountingAdapter([scripted], usage_per_session=TokenUsage())
    result = adapter.run(_spec(tmp_path))
    assert result.env_fault_evidence == evidence
    assert adapter.usage_reads == 0


def test_overruled_timeout_skips_zero_token_read(tmp_path):
    """#764: a pattern match the adapter already overruled on tracked nonzero
    usage is not re-read here — the read happened and was nonzero, so even a
    zero-reading second pass must not flip it back to an env fault.
    ABLATION: drop the `env_fault_overruled` guard and env_fault reads True."""
    scripted = SessionResult(status="timeout", env_fault_overruled="x")
    adapter = _CountingAdapter([scripted], usage_per_session=TokenUsage())
    result = adapter.run(_spec(tmp_path))
    assert result.env_fault is False
    assert result.env_fault_overruled == "x"
    assert adapter.usage_reads == 0


@pytest.mark.parametrize("status", ["stalled", "crashed", "over_budget"])
def test_non_timeout_zero_spend_is_unchanged(tmp_path, status):
    scripted = SessionResult(status=status)
    adapter = _CountingAdapter([scripted], usage_per_session=TokenUsage())
    result = adapter.run(_spec(tmp_path))
    assert result == scripted
    assert adapter.usage_reads == 0


def test_timeout_with_result_json_is_unchanged(tmp_path):
    scripted = SessionResult(status="timeout", result_json={"status": "done"})
    adapter = _CountingAdapter([scripted], usage_per_session=TokenUsage())
    result = adapter.run(_spec(tmp_path))
    assert result == scripted
    assert adapter.usage_reads == 0


@pytest.mark.parametrize("exc", [OSError("unreadable"), ValueError("torn utf-8")])
def test_usage_read_fault_leaves_verdict_unchanged(tmp_path, exc):
    scripted = SessionResult(status="timeout")
    adapter = _CountingAdapter([scripted], raises=exc)
    result = adapter.run(_spec(tmp_path))
    assert result == scripted
    assert adapter.usage_reads == 1
