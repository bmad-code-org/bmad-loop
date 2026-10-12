"""Unit tests for the OpenCode wire-protocol module: version parsing and the
support verdict both the adapter (refuse at spawn) and `validate` (warn) use."""

from __future__ import annotations

import pytest

from bmad_loop.adapters.opencode_protocol import (
    SUPPORTED,
    parse_opencode_version,
    support_verdict,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param("1.19.0", (1, 19, 0), id="v1-bare"),
        pytest.param("opencode v2.0.27", (2, 0, 27), id="v2-prefixed"),
        pytest.param("opencode v2.0.27\n", (2, 0, 27), id="v2-trailing-newline"),
        pytest.param("v3.1.0", (3, 1, 0), id="unknown-major-still-parses"),
        pytest.param("", None, id="empty"),
        pytest.param("garbage", None, id="garbage"),
        pytest.param("1.2", None, id="two-part"),
    ],
)
def test_parse_opencode_version(text, expected):
    assert parse_opencode_version(text) == expected


def test_support_verdict_floors_and_majors():
    assert SUPPORTED == {1: (1, 18, 0), 2: (2, 0, 21)}

    for version in [(1, 18, 0), (1, 19, 0), (2, 0, 21), (2, 0, 27), (2, 3, 0)]:
        verdict, message = support_verdict(version)
        assert verdict == "ok", version
        assert f"V{version[0]} server API" in message

    verdict, message = support_verdict((1, 17, 9))
    assert verdict == "below-floor"
    assert "opencode 1.17.9" in message and "1.18.0" in message

    verdict, message = support_verdict((2, 0, 20))
    assert verdict == "below-floor"
    assert "opencode 2.0.20" in message and "2.0.21" in message

    for version in [(0, 9, 0), (3, 1, 0)]:
        verdict, message = support_verdict(version)
        assert verdict == "unsupported-major", version
        assert "1.x (>= 1.18.0)" in message and "2.x (>= 2.0.21)" in message
