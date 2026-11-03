"""claude.toml pinned against CAPTURED Claude Code StopFailure output (DW-611, DW-612).

``tests/fixtures/claude-stopfailure/`` holds what the real ``claude`` 2.1.317 did
with seven scripted API failures, captured zero-token by
``scripts/capture_claude_stopfailure.py`` (see the fixture README). These tests
read only those files — no claude or tmux process is spawned — and push them
through the production code:

* each StopFailure payload goes the relay's way, ``events.shape_event`` → an event
  file → ``signals._parse_event``, and the resulting event drives the claude dev
  adapter's real wait loop to the outcome the profile's
  ``[hooks.stop_failure_types]`` promises for that capture;
* each pipe-pane log is the tail-scan input of the real ``_classify_env_fault``
  on a timed-out session, so ``env_fault_patterns`` are held to the CLI's own
  bytes rather than to a typed-in sentence.

No captured error line is written out in this module (the repo-wide pane-pattern
scan in test_env_fault_patterns.py would read it as a provider outage); every
expected sentence comes from the fixture it is checked against.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from bmad_loop.adapters import generic
from bmad_loop.adapters.base import SessionHandle, SessionResult, SessionSpec
from bmad_loop.adapters.generic import GenericDevAdapter, GenericTmuxAdapter
from bmad_loop.adapters.profile import get_profile
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.events import event_file_name, shape_event
from bmad_loop.policy import LimitsPolicy, Policy
from bmad_loop.signals import _parse_event

FIXTURES = Path(__file__).parent / "fixtures" / "claude-stopfailure"
TASK_ID = "3-1-dev-1"

# scenario -> (captured `error`, the profile's action for it). The capture itself
# is the source: a re-capture that reports a different `error` fails
# test_fixture_payload_is_the_captured_error rather than silently re-pinning.
CAPTURED = {
    "mid-response-cut": ("server_error", "transient"),
    "truncated-stream": ("unknown", "transient"),
    "rate-limit-429": ("rate_limit", "rate_limit"),
    "overloaded-529": ("server_error", "transient"),
    "server-error-500": ("server_error", "transient"),
    "auth-401": ("authentication_failed", "account"),
    "billing-402": ("billing_error", "account"),
}
# The scenarios whose pane holds an env-fault line the tail scan must classify.
ENV_FAULT_SCENARIOS = frozenset({"mid-response-cut", "overloaded-529", "server-error-500"})


def _payload(scenario: str) -> dict:
    return json.loads((FIXTURES / f"{scenario}.stopfailure.json").read_text(encoding="utf-8"))


def _first_sentence(message: str) -> str:
    return message.split(". ", 1)[0]


def _relayed(tmp_path: Path, scenario: str):
    """The fixture payload as the orchestrator reads it: shaped by the relay's own
    ``shape_event``, written as an event file, parsed back by ``_parse_event``."""
    payload = _payload(scenario)
    event = shape_event(1, payload["hook_event_name"], TASK_ID, payload)
    path = tmp_path / event_file_name(1, TASK_ID, event["event"])
    path.write_text(json.dumps(event), encoding="utf-8")
    parsed = _parse_event(path)
    assert parsed is not None
    return parsed


# ------------------------------------------------- the fixture set is the capture


def test_manifest_records_a_capture_of_every_scenario():
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["harness"] == "scripts/capture_claude_stopfailure.py"
    assert set(manifest["scenarios"]) == set(CAPTURED)
    for name, entry in manifest["scenarios"].items():
        assert entry["claude_version"].startswith("2.1."), name
        assert entry["stop_failure_error"] == CAPTURED[name][0], name
        # Every request the CLI made was a message request the fake server served.
        assert entry["requests"], name
        assert all(r["served"] and r["path"].startswith("/v1/messages") for r in entry["requests"])


@pytest.mark.parametrize("scenario", sorted(CAPTURED))
def test_fixture_payload_is_the_captured_error(scenario):
    payload = _payload(scenario)
    assert payload["hook_event_name"] == "StopFailure"
    assert payload["error"] == CAPTURED[scenario][0]
    assert isinstance(payload["last_assistant_message"], str)
    assert payload["last_assistant_message"]
    # Absent from every capture on 2.1.317; recorded as captured, not invented.
    assert "error_details" not in payload


def test_every_captured_error_is_mapped_with_the_pinned_action():
    """The table was derived from docs and a binary grep; every value the CLI
    actually sent is a key of it, with the action this module pins."""
    table = get_profile("claude").hooks.stop_failure_types
    for scenario in CAPTURED:
        error, action = CAPTURED[scenario]
        assert _payload(scenario)["error"] == error
        assert table.get(error) == action, f"{scenario}: {error!r} -> {table.get(error)!r}"


@pytest.mark.parametrize("scenario", sorted(CAPTURED))
def test_relay_carries_the_captured_fields(tmp_path, scenario):
    payload = _payload(scenario)
    event = _relayed(tmp_path, scenario)
    assert event.event == "StopFailure"
    assert event.task_id == TASK_ID
    assert event.session_id == payload["session_id"]
    assert event.stop_error == payload["error"]
    assert event.stop_message == payload["last_assistant_message"]
    assert event.stop_error_details is None


# ----------------------------------------------------- the wait loop's outcome
#
# A claude dev adapter over a stub mux (nothing reaches a real one), a 10 s stall
# grace with two wake nudges, and a frozen clock pushed past the grace on every
# idle tick — the same harness test_generic_tmux.py's StopFailure tests use.


class _Mux:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.captures: list[str] = []

    def has_session(self, name):
        return True

    def has_registry_namespace(self):
        return False

    def send_text(self, window_id, text):
        self.sent.append((window_id, text))

    def capture_pane(self, window_id):
        self.captures.append(window_id)
        return ""


def _drive(tmp_path, monkeypatch, event):
    monkeypatch.setattr(generic, "RESULT_GRACE_S", 0.0)
    monkeypatch.setattr(generic, "RESULT_POLL_S", 0.0)
    impl = tmp_path / "impl"
    impl.mkdir()
    mux = _Mux()
    adapter = GenericDevAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("claude"),
        paths=ProjectPaths(
            project=tmp_path, implementation_artifacts=impl, planning_artifacts=tmp_path / "plan"
        ),
        mux=mux,
    )
    adapter._stall_grace_s = 10.0
    adapter._stall_nudges = 2
    adapter._window_alive = lambda handle: True
    clock = {"t": 1000.0}

    class _Clock:
        monotonic = staticmethod(lambda: clock["t"])
        time = staticmethod(lambda: 0.0)
        sleep = staticmethod(lambda *_: None)
        time_ns = staticmethod(lambda: 0)

    monkeypatch.setattr(generic, "time", _Clock)
    script = [event]

    class _Watcher:
        def wait_for(self, task_id, kinds, timeout_s, since_ns=0):
            nxt = script.pop(0) if script else None
            if nxt is None:
                clock["t"] += 11.0  # an idle tick: past the grace
            return nxt

    adapter.watcher = _Watcher()
    spec = SessionSpec(
        task_id=TASK_ID,
        role="dev",
        prompt="/bmad-dev-auto 3-1",
        cwd=tmp_path,
        env={"BMAD_LOOP_STORY_KEY": "3-1"},
    )
    result = adapter.wait_for_completion(SessionHandle(task_id=TASK_ID, native_id="@1"), spec)
    return adapter, mux, result


def _crumbs(adapter, kind):
    path = adapter.tasks_dir / TASK_ID / "session-lifecycle.jsonl"
    if not path.is_file():
        return []
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line["event"] == kind]


@pytest.mark.parametrize(
    "scenario", sorted(s for s, (_, action) in CAPTURED.items() if action == "transient")
)
def test_captured_transient_failure_gets_the_wake_nudge_and_is_not_parked(
    tmp_path, monkeypatch, scenario
):
    """mid-response cut, truncated stream, 529, 500: the wake nudge goes out at the
    event, the session is never parked, and the captured error and message ride
    out on the crumb and the result."""
    payload = _payload(scenario)
    adapter, mux, result = _drive(tmp_path, monkeypatch, _relayed(tmp_path, scenario))
    (crumb,) = _crumbs(adapter, "stop-failure")
    assert (crumb["error"], crumb["action"]) == (payload["error"], "transient")
    # The crumb windows a long message (the truncated stream's runs ~400 chars).
    assert payload["last_assistant_message"].startswith(crumb["message"].rstrip("…"))
    assert [text for _, text in mux.sent] == [generic.STALL_NUDGE_TEXT] * 2
    assert _crumbs(adapter, "stop-failure-nudge-exhausted") == []
    assert (result.status, result.parked, result.usage_limited) == ("stalled", False, None)
    assert result.stop_failure == payload["error"]
    assert result.stop_seen is False


def test_captured_429_matches_no_usage_pattern(tmp_path, monkeypatch):
    """An ordinary 429 shares `rate_limit` with the subscription usage limits; its
    captured message matches neither usage list, so it is crumbed unmatched and the
    stall timing decides — not usage-limited, not parked."""
    adapter, mux, result = _drive(tmp_path, monkeypatch, _relayed(tmp_path, "rate-limit-429"))
    (crumb,) = _crumbs(adapter, "stop-failure-rate-limit-unmatched")
    assert crumb["error"] == "rate_limit"
    assert [text for _, text in mux.sent] == [generic.STALL_NUDGE_TEXT] * 2
    assert (result.status, result.parked, result.usage_limited) == ("stalled", False, None)
    assert result.stop_failure == "rate_limit"


@pytest.mark.parametrize("scenario", ["auth-401", "billing-402"])
def test_captured_account_failure_parks_with_the_message_as_evidence(
    tmp_path, monkeypatch, scenario
):
    payload = _payload(scenario)
    adapter, mux, result = _drive(tmp_path, monkeypatch, _relayed(tmp_path, scenario))
    assert (result.status, result.parked) == ("stalled", True)
    assert result.parked_evidence == (
        f"StopFailure({payload['error']}): {payload['last_assistant_message']}"
    )
    assert result.usage_limited is None
    assert result.stop_failure == payload["error"]
    assert mux.sent == []


# --------------------------------------------------------- the pane tail scan


def _classify(tmp_path, scenario, profile=None) -> SessionResult:
    adapter = GenericTmuxAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=profile or get_profile("claude"),
        mux=_Mux(),
        events_dir=tmp_path / "events",
    )
    (adapter.logs_dir / f"{TASK_ID}.log").write_bytes(
        (FIXTURES / f"{scenario}.pipe-pane.log").read_bytes()
    )
    spec = SessionSpec(task_id=TASK_ID, role="dev", prompt="x", cwd=tmp_path, env={})
    handle = SessionHandle(task_id=TASK_ID, native_id="@1")
    return adapter._classify_env_fault(handle, spec, SessionResult(status="timeout"))


@pytest.mark.parametrize("scenario", sorted(CAPTURED))
def test_captured_pane_classifies_exactly_the_transport_failures(tmp_path, scenario):
    """The real tail scan over the real pane bytes: the mid-response cut, 529 and
    500 read as env faults with the CLI's own first sentence as evidence; the
    truncated stream, 429, 401 and 402 do not (no pattern was seeded for their
    wording, and none may be until it is a deliberate choice)."""
    result = _classify(tmp_path, scenario)
    if scenario in ENV_FAULT_SCENARIOS:
        assert result.env_fault is True
        sentence = _first_sentence(_payload(scenario)["last_assistant_message"])
        assert sentence in result.env_fault_evidence
    else:
        assert result.env_fault is False
        assert result.env_fault_evidence is None


def test_mid_response_cut_is_classified_only_by_the_lost_seed(tmp_path):
    """Ablation via a profile copy: without the `lost` seed the current CLI's
    mid-response cut is NOT an env fault — the old `closed` seed does not reach it,
    which was DW-611."""
    profile = get_profile("claude")
    seeded = [p for p in profile.env_fault_patterns if "lost" in p]
    assert len(seeded) == 1
    ablated = dataclasses.replace(
        profile, env_fault_patterns=tuple(p for p in profile.env_fault_patterns if p not in seeded)
    )
    assert _classify(tmp_path / "with", "mid-response-cut").env_fault is True
    assert _classify(tmp_path / "without", "mid-response-cut", ablated).env_fault is False
