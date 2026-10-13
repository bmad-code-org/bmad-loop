"""`events.py` — the importable twin of the stdlib-only hook relay, and the
`bmad-loop relay` command built on it.

Two things are under test. The twin's *source parity* with
`data/bmad_loop_hook.py`, because two separately-maintained writers of one
control plane is how the hardening in one silently stops applying to the other.
And the twin's *behavior*, mirrored from the #493 hardening tests in
test_hook_script.py — a copy that is byte-identical today can still be edited on
both sides at once, and these are what say the shape still holds.
"""

from __future__ import annotations

import ast
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bmad_loop import cli, events
from bmad_loop import policy as policy_mod

HOOK = Path(events.__file__).resolve().parent / "data" / "bmad_loop_hook.py"
EVENTS = Path(events.__file__).resolve()

# Everything `events.py` copies verbatim out of the hook script. The write path and
# the two helpers it stands on; the payload SHAPING is not here because the hook
# does it inline in its `main()` and there is no source segment to compare — it is
# pinned behaviorally instead, by test_relay_and_the_hook_shape_the_same_event.
TWINNED = (
    "_LINK_REPARSE_TAGS",
    "_first_workspace",
    "_notification_type",
    "_source",
    # DW-507's lineage block: the tag both relays stamp on every event.
    "_LAUNCH_SHIM_WINDOW_S",
    "_proc_stat",
    "_cmdline",
    "_HOOK_SHELLS",
    "_runs_relay",
    "_is_hook_wrapper",
    "_lineage",
    "_is_link_like",
    "_write_all",
    "_write_event",
    "_read_payload",
)


def _top_level_sources(path: Path) -> dict[str, str]:
    """`name -> the exact source segment that defines it`, for the module's
    top-level defs and plain assignments."""
    src = path.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef):
            found[node.name] = ast.get_source_segment(src, node) or ""
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = ast.get_source_segment(src, node) or ""
    return found


def _relay(
    event: str,
    payload,
    monkeypatch,
    run_dir: Path | None,
    task_id: str = "t1",
    events_dir: Path | str | None = None,
    launch_pid: int | None = None,
) -> int:
    """Drive `bmad-loop relay <event>` in-process with `payload` on stdin.

    ``events_dir`` sets (or, as None, explicitly CLEARS) BMAD_LOOP_EVENTS_DIR:
    cleared by default so an operator shell that happens to export it cannot
    redirect a test's events out from under its assertions. ``launch_pid`` does
    the same for BMAD_LOOP_LAUNCH_PID (DW-507): a pytest run inside a bmad-loop
    session inherits the session's value, which would retag every event."""
    if run_dir is None:
        monkeypatch.delenv("BMAD_LOOP_RUN_DIR", raising=False)
        monkeypatch.delenv("BMAD_LOOP_TASK_ID", raising=False)
    else:
        monkeypatch.setenv("BMAD_LOOP_RUN_DIR", str(run_dir))
        monkeypatch.setenv("BMAD_LOOP_TASK_ID", task_id)
    if events_dir is None:
        monkeypatch.delenv("BMAD_LOOP_EVENTS_DIR", raising=False)
    else:
        monkeypatch.setenv("BMAD_LOOP_EVENTS_DIR", str(events_dir))
    if launch_pid is None:
        monkeypatch.delenv("BMAD_LOOP_LAUNCH_PID", raising=False)
    else:
        monkeypatch.setenv("BMAD_LOOP_LAUNCH_PID", str(launch_pid))
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    return cli.main(["relay", event])


# ------------------------------------------------------------------- parity


def test_the_twinned_source_is_identical():
    """The hook ships as package DATA and is stdlib-only by contract, so it cannot
    import `events.py` and `events.py` cannot import it: the hardened write exists
    twice on purpose. A fix applied to one copy and not the other leaves one writer
    of the events control plane unhardened while every behavioral test stays green
    — both copies pass their own suites either way. Compare the source directly.

    Ablation guard: change so much as a comment in either copy's `_write_event`
    and this fails. The `missing` assertion is the second half: rename a twinned
    function on one side and the comparison loop would otherwise have nothing to
    compare and pass vacuously."""
    hook = _top_level_sources(HOOK)
    twin = _top_level_sources(EVENTS)

    missing = {name: (name in hook, name in twin) for name in TWINNED}
    assert all(
        all(present) for present in missing.values()
    ), f"a twinned name is gone from one side (name: in-hook, in-events): {missing}"
    for name in TWINNED:
        assert twin[name] == hook[name], (
            f"{name} has drifted between {HOOK.name} and {EVENTS.name} — "
            f"fix both copies, or the two writers of the events control plane "
            f"stop being hardened the same way"
        )


def test_relay_and_the_hook_shape_the_same_event(tmp_path, monkeypatch):
    """The payload shaping and the event file name are the half of the twin that
    `ast.get_source_segment` cannot reach — the hook does both inline in `main()`.
    Pin them the only way left: feed one payload to both writers and compare what
    lands. Every CLI's key spelling is in the payload, so a fallback dropped from
    one side shows up as a null on that side only.

    Ablation guard: drop any `or payload.get(...)` arm from `events.shape_event`,
    or reorder the event file name's fields, and this fails."""
    payload = {
        "conversationId": "agy-3",
        "transcriptPath": "/ws/transcript.jsonl",
        "workspacePaths": ["/ws"],
    }
    hook_run, relay_run = tmp_path / "hook", tmp_path / "relay"

    proc = subprocess.run(
        [sys.executable, str(HOOK), "Stop"],
        input=json.dumps(payload),
        # `env=` REPLACES the environment rather than extending it, and on Windows
        # a Python child started without SYSTEMROOT can fail to load its
        # side-by-side assemblies — a start failure that would read here as the
        # relay misbehaving. Kept minimal otherwise: the relay is stdlib-only and
        # must need nothing else.
        env={
            "PATH": os.environ.get("PATH", ""),
            **({"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if os.name == "nt" else {}),
            "BMAD_LOOP_RUN_DIR": str(hook_run),
            "BMAD_LOOP_TASK_ID": "1-1-a-dev-1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert _relay("Stop", payload, monkeypatch, relay_run, task_id="1-1-a-dev-1") == 0

    from_hook = next((hook_run / "events").glob("*.json"))
    from_relay = next((relay_run / "events").glob("*.json"))
    # `ts` is a timestamp and the name is built from it; compare the rest.
    assert json.loads(from_relay.read_text()) | {"ts": 0} == (
        json.loads(from_hook.read_text()) | {"ts": 0}
    )
    assert from_relay.name.split("-", 1)[1] == from_hook.name.split("-", 1)[1]
    # DW-507: both carry the lineage key, and with no launch pid both say so.
    assert json.loads(from_relay.read_text())["lineage"] == "unknown"
    assert json.loads(from_hook.read_text())["lineage"] == "unknown"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"notification_type": "permission_prompt"}, "permission_prompt"),
        ({"notificationType": "idle_prompt"}, "idle_prompt"),
        # only a string is forwarded — anything else no profile table can key on
        ({"notification_type": 7}, None),
        ({"notification_type": ["permission_prompt"]}, None),
        ({}, None),
    ],
)
def test_both_relay_twins_forward_the_notification_type(tmp_path, monkeypatch, payload, expected):
    """DW-348: the Notification subtype reaches the event file from BOTH writers,
    spelled the same — the hook does its shaping inline in `main()`, so this is
    pinned behaviorally like the rest of the payload shape.

    Ablation: drop the `"notification_type"` key from either writer's event dict
    and this fails on that side's KeyError."""
    hook_run, relay_run = tmp_path / "hook", tmp_path / "relay"
    proc = subprocess.run(
        [sys.executable, str(HOOK), "Notification"],
        input=json.dumps(payload),
        env={
            "PATH": os.environ.get("PATH", ""),
            **({"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if os.name == "nt" else {}),
            "BMAD_LOOP_RUN_DIR": str(hook_run),
            "BMAD_LOOP_TASK_ID": "1-1-a-dev-1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert _relay("Notification", payload, monkeypatch, relay_run, task_id="1-1-a-dev-1") == 0
    from_hook = json.loads(next((hook_run / "events").glob("*.json")).read_text())
    from_relay = json.loads(next((relay_run / "events").glob("*.json")).read_text())
    assert from_hook["notification_type"] == expected
    assert from_relay["notification_type"] == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"source": "clear"}, "clear"),
        ({"source": "compact"}, "compact"),
        # only a string is forwarded — attribution compares it against a str set
        ({"source": 1}, None),
        ({"source": ["clear"]}, None),
        ({}, None),
    ],
)
def test_both_relay_twins_forward_the_session_start_source(
    tmp_path, monkeypatch, payload, expected
):
    """#767: SessionStart's `source` reaches the event file from BOTH writers,
    so attribution can rebind on clear/compact. Pinned behaviorally for the same
    reason as the notification subtype: the hook shapes the event inline.

    Ablation: drop the `"source"` key from either writer's event dict and this
    fails on that side's KeyError."""
    hook_run, relay_run = tmp_path / "hook", tmp_path / "relay"
    proc = subprocess.run(
        [sys.executable, str(HOOK), "SessionStart"],
        input=json.dumps(payload),
        env={
            "PATH": os.environ.get("PATH", ""),
            **({"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if os.name == "nt" else {}),
            "BMAD_LOOP_RUN_DIR": str(hook_run),
            "BMAD_LOOP_TASK_ID": "1-1-a-dev-1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert _relay("SessionStart", payload, monkeypatch, relay_run, task_id="1-1-a-dev-1") == 0
    from_hook = json.loads(next((hook_run / "events").glob("*.json")).read_text())
    from_relay = json.loads(next((relay_run / "events").glob("*.json")).read_text())
    assert from_hook["source"] == expected
    assert from_relay["source"] == expected


# ---------------------------------------------------------- lineage (DW-507)

# A fake process table for `events._lineage`: pid -> (ppid, starttime ticks,
# cmdline). L is the launched CLI; clock ticks are 100/s, so the launch-shim
# window is 500 ticks after L's start.
L = 100
L_START = 1_000
TICKS = 100
WRAP = ["sh", "-c", "/opt/bin/bmad-loop relay Stop"]
RELAY = ("bmad-loop", "relay")


def _fake_table(monkeypatch, table, parent, launch_pid=L, ticks=TICKS):
    """Point `events._lineage` at `table` with the relay's parent `parent`."""
    table = {L: (1, L_START, ["node", "/usr/bin/codex"]), **table}
    if launch_pid is None:
        monkeypatch.delenv("BMAD_LOOP_LAUNCH_PID", raising=False)
    else:
        monkeypatch.setenv("BMAD_LOOP_LAUNCH_PID", str(launch_pid))
    monkeypatch.setattr(events.os, "getppid", lambda: parent)
    monkeypatch.setattr(events.os, "sysconf", lambda name: ticks, raising=False)
    monkeypatch.setattr(events, "_proc_stat", lambda pid: table[pid][:2] if pid in table else None)
    monkeypatch.setattr(events, "_cmdline", lambda pid: table[pid][2] if pid in table else [])


@pytest.mark.parametrize(
    ("table", "parent", "expected"),
    [
        pytest.param({}, L, "match", id="direct-hook"),
        pytest.param({200: (L, 90_000, WRAP)}, 200, "match", id="shell-wrapped"),
        pytest.param(
            {
                200: (201, 90_000, WRAP),
                201: (L, 90_000, ["bash", "-c", "sh -c '/opt/bin/bmad-loop relay Stop'"]),
            },
            200,
            "match",
            id="wrapped-twice",
        ),
        pytest.param({300: (L, L_START + 200, ["codex-bin"])}, 300, "match", id="launch-shim"),
        pytest.param(
            {
                300: (301, L_START + 300, ["codex-bin"]),
                301: (L, L_START + 100, ["node", "codex-shim"]),
            },
            300,
            "match",
            id="launch-chain-two-levels",
        ),
        pytest.param(
            {
                200: (300, 90_000, WRAP),
                300: (301, L_START + 300, ["codex-bin"]),
                301: (L, L_START + 100, ["node", "codex-shim"]),
            },
            200,
            "match",
            id="wrapped-hook-under-a-two-level-launch-chain",
        ),
        pytest.param(
            {
                300: (301, L_START + 501, ["codex-bin"]),
                301: (L, L_START + 100, ["node", "codex-shim"]),
            },
            300,
            "mismatch",
            id="launch-chain-grandchild-started-too-late",
        ),
        pytest.param(
            {200: (L, 90_000, ["sh", "-c", "/opt/bin/bmad-loop relay Stop; exit $?"])},
            200,
            "match",
            id="wrapper-then-semicolon",
        ),
        pytest.param(
            {200: (L, 90_000, ["sh", "-c", "/opt/bin/bmad-loop relay Stop && true"])},
            200,
            "match",
            id="wrapper-then-and-list",
        ),
        pytest.param(
            {200: (L, 90_000, ["sh", "-c", "'/opt/my dir/bmad-loop' relay 'Stop'"])},
            200,
            "match",
            id="wrapper-with-a-quoted-relay-path",
        ),
        pytest.param(
            {200: (L, 90_000, ["sh", "-c", '"/opt/bin/bmad-loop" relay "Stop"&&true'])},
            200,
            "match",
            id="wrapper-double-quoted-no-spaces",
        ),
        pytest.param(
            {300: (L, L_START + 500, ["codex-bin"])}, 300, "match", id="launch-shim-at-window-edge"
        ),
        pytest.param(
            {200: (300, 90_000, WRAP), 300: (L, L_START + 3, ["codex-bin"])},
            200,
            "match",
            id="wrapped-hook-under-a-shim",
        ),
        pytest.param(
            {300: (L, L_START + 501, ["codex-bin"])}, 300, "mismatch", id="shim-started-too-late"
        ),
        pytest.param(
            {300: (L, L_START - 1, ["codex-bin"])}, 300, "mismatch", id="shim-started-before-L"
        ),
        pytest.param(
            {
                200: (400, 90_000, WRAP),
                400: (500, 90_000, ["node", "/usr/bin/claude", "-p", "hi"]),
                500: (L, 80_000, ["bash", "-c", "claude -p hi"]),
            },
            200,
            "mismatch",
            id="nested-cli",
        ),
        pytest.param(
            {400: (L, 90_000, ["node", "/usr/bin/claude", "-p", "hi"])},
            400,
            "mismatch",
            id="nested-cli-execd-by-L-long-after-launch",
        ),
        pytest.param(
            {200: (L, 90_000, ["sh", "-c", "/opt/bin/bmad-loop relay Stopped"])},
            200,
            "mismatch",
            id="event-name-must-be-a-whole-word",
        ),
        pytest.param(
            {200: (L, 90_000, ["sh", "-c", "/opt/bin/other relay Stop"])},
            200,
            "mismatch",
            id="wrapper-needs-the-marker",
        ),
        pytest.param(
            {
                200: (400, 90_000, WRAP),
                400: (500, 90_000, ["node", "/usr/bin/claude", "-p", "run bmad-loop relay Stop"]),
                500: (L, 80_000, ["bash", "-c", "claude -p 'run bmad-loop relay Stop'"]),
            },
            200,
            "mismatch",
            id="nested-cli-whose-prompt-names-the-relay",
        ),
        pytest.param(
            {400: (L, 90_000, ["claude", "-c", "/opt/bin/bmad-loop relay Stop"])},
            400,
            "mismatch",
            id="non-shell-with-a-dash-c-argument",
        ),
        pytest.param(
            {400: (L, 90_000, ["bash", "/tmp/cli.sh", "-c", "/opt/bin/bmad-loop relay Stop"])},
            400,
            "mismatch",
            id="shell-running-a-script-file",
        ),
        pytest.param(
            {200: (L, 90_000, ["sh", "-c", "/opt/bin/bmad-loop relay; echo Stop"])},
            200,
            "mismatch",
            id="relay-words-must-be-consecutive",
        ),
        pytest.param(
            {200: (L, 90_000, ["-bash", "-lc", "/opt/bin/bmad-loop relay Stop"])},
            200,
            "match",
            id="login-shell-with-an-option-cluster",
        ),
        pytest.param({200: (1, 90_000, WRAP)}, 200, "mismatch", id="other-tree-reaches-init"),
        pytest.param({}, 1, "mismatch", id="detached-child-parent-is-init"),
        pytest.param({}, 0, "mismatch", id="parent-outside-the-pid-namespace"),
        pytest.param({200: (400, 90_000, WRAP)}, 200, "unknown", id="intermediate-vanished"),
        pytest.param(
            {pid: (pid + 1, 90_000, WRAP) for pid in range(1_000, 1_070)},
            1_000,
            "unknown",
            id="more-than-64-hops",
        ),
    ],
)
def test_lineage_tags_every_row_of_the_process_table(monkeypatch, table, parent, expected):
    """DW-507: `_lineage` over a fake `/proc`, one case per Tag row of the spec.
    The walk skips only hook-command wrappers (a shell whose `-c` string holds
    the relay invocation and event name as consecutive words, split on
    whitespace, quotes and shell operators) and the launch chain (any process
    started within 5 s of L, at any depth — a forking fish and a node shim stack
    two levels); anything else on the way is a nested CLI.

    Ablation: drop the launch-chain arm and the shim/chain rows fail; restrict
    it to L's direct children and the two-level rows fail; drop the wrapper arm
    and the wrapped rows fail; split the `-c` string on whitespace only and the
    `;`/`&&`/quoted rows fail; make it a plain "L is an ancestor" test and the
    nested-cli row tags "match"; match the marker anywhere in the joined argv
    (the old rule) and the nested-cli-whose-prompt-names-the-relay, non-shell
    and script-file rows tag "match"."""
    _fake_table(monkeypatch, table, parent)
    assert events._lineage("Stop", RELAY) == expected


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            ["uv", "run", "--no-project", "python", "/p/.bmad-loop/bmad_loop_hook.py", "Stop"],
            "match",
            id="legacy-uv-runner-ends-in-the-invocation",
        ),
        pytest.param(
            ["uv", "run", "python", "/p/.bmad-loop/bmad_loop_hook.py", "Stop", "--extra"],
            "mismatch",
            id="invocation-not-at-the-end",
        ),
        pytest.param(
            ["node", "/usr/bin/claude", "-p", "see /p/.bmad-loop/bmad_loop_hook.py Stop now"],
            "mismatch",
            id="prompt-naming-the-hook-script",
        ),
    ],
)
def test_lineage_skips_a_runner_whose_argv_ends_in_the_hook_invocation(monkeypatch, argv, expected):
    """DW-507: the copied hook's legacy registration (`uv run --no-project python
    .../bmad_loop_hook.py Stop`) puts a non-shell runner between the hook and the
    CLI; it is a wrapper because its argv ENDS in the invocation, not because it
    holds those words anywhere.

    Ablation: drop the argv-tail arm of `_is_hook_wrapper` and the uv row tags
    "mismatch"; scan the whole argv instead of its tail and the other rows tag
    "match"."""
    _fake_table(monkeypatch, {200: (L, 90_000, argv)}, 200)
    assert events._lineage("Stop", ("bmad_loop_hook.py",)) == expected


@pytest.mark.parametrize(
    ("value", "stat_of_l"),
    [
        pytest.param(None, True, id="unset"),
        pytest.param("", True, id="empty"),
        pytest.param("abc", True, id="non-int"),
        pytest.param("1", True, id="init"),
        pytest.param("0", True, id="zero"),
        pytest.param("-5", True, id="negative"),
        pytest.param(str(L), False, id="L-exited-or-no-proc"),
    ],
)
def test_lineage_is_unknown_when_the_launch_pid_cannot_be_read(monkeypatch, value, stat_of_l):
    monkeypatch.setattr(events.os, "getppid", lambda: L)
    monkeypatch.setattr(events, "_proc_stat", lambda pid: (1, L_START) if stat_of_l else None)
    monkeypatch.setattr(events, "_cmdline", lambda pid: [])
    monkeypatch.setattr(events.os, "sysconf", lambda name: TICKS, raising=False)
    if value is None:
        monkeypatch.delenv("BMAD_LOOP_LAUNCH_PID", raising=False)
    else:
        monkeypatch.setenv("BMAD_LOOP_LAUNCH_PID", value)
    assert events._lineage("Stop", RELAY) == "unknown"


def test_lineage_is_unknown_when_clock_ticks_are_unreadable(monkeypatch):
    def no_sysconf(name):
        raise ValueError(name)

    _fake_table(monkeypatch, {}, L)
    monkeypatch.setattr(events.os, "sysconf", no_sysconf, raising=False)
    assert events._lineage("Stop", RELAY) == "unknown"


def test_lineage_turns_any_unexpected_fault_into_unknown(monkeypatch):
    """The relay must still write its event: a fault inside the walk degrades the
    tag, never the write (the relays keep rc 0 and say nothing)."""
    _fake_table(monkeypatch, {}, 200)

    def explode(pid):
        raise RuntimeError("boom")

    monkeypatch.setattr(events, "_cmdline", explode)
    monkeypatch.setattr(events, "_proc_stat", explode)
    assert events._lineage("Stop", RELAY) == "unknown"


def test_proc_stat_splits_after_the_last_paren(tmp_path, monkeypatch):
    """comm is parenthesized and may hold spaces and ")": ppid and starttime
    are read after the LAST one."""
    fields = ["S", "4242"] + ["0"] * 17 + ["987654"] + ["0"] * 10
    line = "123 (we) ird (name) " + " ".join(fields) + "\n"
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if path == "/proc/123/stat":  # portability: a faked path, never opened
            return io.BytesIO(line.encode())
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    assert events._proc_stat(123) == (4242, 987654)


_HAS_PROC = os.path.isfile(f"/proc/{os.getpid()}/stat")  # portability: probes for /proc


def _hook_lineage(tmp_path: Path, launch_pid: int | None) -> str:
    """Run the copied hook script as a real subprocess; return its lineage tag."""
    run_dir = tmp_path / "hook"
    env = {
        "PATH": os.environ.get("PATH", ""),
        **({"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if os.name == "nt" else {}),
        "BMAD_LOOP_RUN_DIR": str(run_dir),
        "BMAD_LOOP_TASK_ID": "t1",
    }
    if launch_pid is not None:
        env["BMAD_LOOP_LAUNCH_PID"] = str(launch_pid)
    proc = subprocess.run(
        [sys.executable, str(HOOK), "Stop"],
        input="{}",
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    return json.loads(next((run_dir / "events").glob("*.json")).read_text())["lineage"]


def _relay_lineage(tmp_path: Path, monkeypatch, launch_pid: int | None) -> str:
    run_dir = tmp_path / "relay"
    assert _relay("Stop", {}, monkeypatch, run_dir, launch_pid=launch_pid) == 0
    return json.loads(next((run_dir / "events").glob("*.json")).read_text())["lineage"]


@pytest.mark.skipif(not _HAS_PROC, reason="no /proc: lineage is unknown by construction")
def test_both_twins_tag_match_when_the_launch_pid_is_their_parent(tmp_path, monkeypatch):
    """Real processes: the hook subprocess's parent is this test process, and the
    in-process relay's parent is this process's parent."""
    assert _hook_lineage(tmp_path, os.getpid()) == "match"
    assert _relay_lineage(tmp_path, monkeypatch, os.getppid()) == "match"


@pytest.mark.skipif(not _HAS_PROC, reason="no /proc: lineage is unknown by construction")
def test_both_twins_tag_mismatch_for_an_unrelated_live_launch_pid(tmp_path, monkeypatch):
    """A live launch pid that is not on either relay's parent chain (a `sleep`
    from another branch of the tree) is someone else's session."""
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        assert _hook_lineage(tmp_path, sleeper.pid) == "mismatch"
        assert _relay_lineage(tmp_path, monkeypatch, sleeper.pid) == "mismatch"
    finally:
        sleeper.kill()
        sleeper.wait()


def test_both_twins_tag_unknown_without_a_launch_pid(tmp_path, monkeypatch):
    assert _hook_lineage(tmp_path, None) == "unknown"
    assert _relay_lineage(tmp_path, monkeypatch, None) == "unknown"


def _past_the_launch_window() -> None:
    """Sleep until this process is older than the relays' launch window, so a
    wrapper it spawns is skipped by the WRAPPER arm alone (its invocation and event
    name) and never by the launch-chain arm (started within 5 s of L)."""
    ticks = os.sysconf("SC_CLK_TCK")
    started = events._proc_stat(os.getpid())
    assert started is not None
    with open("/proc/uptime", encoding="ascii") as handle:  # portability: /proc-gated test
        uptime = float(handle.read().split()[0])
    age = uptime - started[1] / ticks
    margin = events._LAUNCH_SHIM_WINDOW_S + 1
    if age < margin:
        time.sleep(margin - age)


def _wrapped_lineage(tmp_path: Path, command: list[str]) -> str:
    """Run `command Stop` behind a real, non-exec'ing `sh -c '… Stop && true'`
    wrapper — the shape a hook host gives a registered hook command — with this
    process as the launched pid; return the event's lineage tag."""
    _past_the_launch_window()
    run_dir = tmp_path / "wrapped"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "BMAD_LOOP_RUN_DIR": str(run_dir),
        "BMAD_LOOP_TASK_ID": "t1",
        "BMAD_LOOP_LAUNCH_PID": str(os.getpid()),
    }
    source = f"{shlex.join(command)} Stop && true"
    proc = subprocess.run(
        ["sh", "-c", source], input="{}", env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    return json.loads(next((run_dir / "events").glob("*.json")).read_text())["lineage"]


@pytest.mark.skipif(not _HAS_PROC, reason="no /proc: lineage is unknown by construction")
def test_hook_script_invocation_matches_its_real_wrapped_call_site(tmp_path):
    """DW-507: the copied hook's invocation literal ("bmad_loop_hook.py",) must match the
    command a hook host actually runs — `<python> <path>/bmad_loop_hook.py Stop`
    behind a shell that does not exec it. Past the launch window, only the
    wrapper arm can skip that shell.

    Ablation: change the hook's invocation literal and this tags "mismatch"."""
    assert _wrapped_lineage(tmp_path, [sys.executable, str(HOOK)]) == "match"


_RELAY_SCRIPT = Path(sys.executable).parent / "bmad-loop"


@pytest.mark.skipif(not _HAS_PROC, reason="no /proc: lineage is unknown by construction")
@pytest.mark.skipif(not _RELAY_SCRIPT.is_file(), reason="no bmad-loop console script beside python")
def test_relay_invocation_matches_its_real_wrapped_call_site(tmp_path):
    """DW-507: `bmad-loop relay`'s invocation literal ("bmad-loop", "relay") must match the
    command `init` registers — `<abs>/bmad-loop relay Stop` — behind a shell
    that does not exec it. Past the launch window, only the wrapper arm can skip
    that shell.

    Ablation: change `relay()`'s invocation literal and this tags "mismatch"."""
    assert _wrapped_lineage(tmp_path, [str(_RELAY_SCRIPT), "relay"]) == "match"


# -------------------------------------------------------- hardening (twinned)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_events_dir_writes_nothing_and_exits_zero(tmp_path, monkeypatch, capsys):
    """#461 (Low): a driven session can write inside the run dir, so it can plant
    `events/` as a symlink and redirect the orchestrator's control-plane event
    stream — swallowing the Stop signal and stalling the run to timeout. The relay
    must refuse the link and degrade to a no-op, never write through it.

    This is the OUTCOME, which is what the operator has: rc 0, nothing said,
    nothing anywhere. Two independent layers produce it on POSIX and deleting
    either one alone leaves this green — the `_is_link_like` pre-check, and the
    `O_NOFOLLOW` on the anchored dir open, which makes `os.open` of a symlinked
    final component fail ELOOP on its own. So each layer is pinned where it is the
    only thing standing: the pre-check by
    `test_the_precheck_refuses_before_makedirs_on_the_fallback_path` (the fallback
    has no dir open to lean on), and `O_NOFOLLOW`'s branch by
    `test_the_anchored_branch_is_actually_taken_on_posix`.

    Ablation guard: delete the pre-`makedirs` `_is_link_like` refusal AND drop
    `o_nofollow` from the `os.open(events_dir, …)` flags — with both layers gone
    the payload lands in the attacker's directory and this fails. Verified;
    removing only one does not redden it, which is why the two tests above
    exist."""
    target = tmp_path / "attacker"
    target.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events").symlink_to(target, target_is_directory=True)

    assert _relay("Stop", {"session_id": "s1"}, monkeypatch, run_dir) == 0
    assert capsys.readouterr() == ("", "")
    assert list(target.iterdir()) == []
    assert list((run_dir / "events").iterdir()) == []


class _ReparseStat:
    """Stand-in for the os.lstat() result of a Windows junction: a DIRECTORY
    mode (which is why os.path.islink() answers False) carrying a reparse tag."""

    st_mode = stat.S_IFDIR | 0o755
    st_reparse_tag = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT


def test_is_link_like_refuses_a_reparse_tagged_dir(tmp_path, monkeypatch):
    """A Windows directory junction is a reparse point but NOT a symlink, so
    `os.path.islink` is False for it while `os.makedirs`/`os.open` follow it —
    and `mklink /J` needs no elevation, unlike a directory symlink, so it is the
    cheaper attack. The refusal keys on the reparse tag instead. That branch is
    reachable only on Windows; drive its logic here so it is not shipped
    unexercised (the `stat.IO_REPARSE_TAG_*` constants do not exist on POSIX,
    hence the substituted tuple).

    Ablation guard: dropping the `st_reparse_tag` arm of `_is_link_like` makes
    the last assertion fail."""
    plain = tmp_path / "events"
    plain.mkdir()
    assert events._is_link_like(plain) is False

    real_lstat = os.lstat
    monkeypatch.setattr(events, "_LINK_REPARSE_TAGS", (_ReparseStat.st_reparse_tag,))
    monkeypatch.setattr(
        os,
        "lstat",
        lambda p, *a, **k: _ReparseStat() if str(p) == str(plain) else real_lstat(p),
    )
    assert events._is_link_like(plain) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_the_precheck_refuses_before_makedirs_on_the_fallback_path(tmp_path, monkeypatch):
    """What the pre-check is for, in the one place it is load-bearing. On POSIX the
    anchored branch's `O_NOFOLLOW` dir open refuses a symlinked `events/` by
    itself, so the pre-check's absence is invisible there. The Windows fallback has
    no dir open at all: `O_NOFOLLOW` on the create applies to the temp file's own
    (non-link) name, so the create happily resolves THROUGH the redirected
    directory and the payload is written into the attacker's tree before the
    post-write re-check unlinks it again.

    Hence the two assertions. The refusal is the pre-check's, identified by its
    message rather than by "an OSError happened" — the post-write re-check raises
    one too and would mask this entirely. And `os.makedirs` never runs, which is
    the docstring's own claim (`makedirs(exist_ok=True)` `isdir()`-checks THROUGH
    the link, so the refusal has to come before it) stated as behavior.

    Ablation guard: delete the pre-`makedirs` `_is_link_like` refusal and this
    fails on both counts — the message becomes `redirected mid-write` and
    `makedirs` has run."""
    target = tmp_path / "attacker"
    target.mkdir()
    events_dir = tmp_path / "events"
    events_dir.symlink_to(target, target_is_directory=True)

    made = []
    real_makedirs = os.makedirs
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())  # force the fallback
    monkeypatch.setattr(os, "makedirs", lambda p, **kw: (made.append(p), real_makedirs(p, **kw))[1])

    with pytest.raises(OSError, match="refusing to write events into a redirected directory"):
        events._write_event(str(events_dir), "1-t1-Stop.json", {"event": "Stop"})

    assert made == [], "the refusal has to come BEFORE makedirs, which follows the link"
    assert list(target.iterdir()) == []


def test_fallback_refuses_a_redirect_that_appears_mid_write(tmp_path, monkeypatch):
    """The Windows fallback has no dir_fd to anchor to, so it re-resolves
    `events_dir` by path and a swap between the check and the create lands the
    temp file in the attacker's directory. The post-write re-check catches a
    swap that is still in place. Driven here with the dir_fd branch disabled,
    because on POSIX that branch is always taken and the fallback would ship
    unexercised.

    Ablation guard: deleting the post-write `_is_link_like` block makes this
    fail — the event gets published instead of refused."""
    events_dir = tmp_path / "events"
    calls = []
    real = events._is_link_like

    def swapped_after_the_check(path):
        calls.append(path)
        return len(calls) > 1 and real(path) is False  # clean at check, dirty after

    monkeypatch.setattr(os, "supports_dir_fd", frozenset())  # force the fallback
    monkeypatch.setattr(events, "_is_link_like", swapped_after_the_check)

    with pytest.raises(OSError, match="redirected mid-write"):
        events._write_event(str(events_dir), "1-t1-Stop.json", {"event": "Stop", "task_id": "t1"})

    assert len(calls) == 2  # the check ran on both sides of the write
    assert list(events_dir.iterdir()) == []  # nothing published, no .tmp left behind


@pytest.mark.skipif(os.name == "nt", reason="dir_fd is implemented with the POSIX *at() calls")
def test_the_anchored_branch_is_actually_taken_on_posix(tmp_path, monkeypatch):
    """The capability probe must resolve to True where the capability exists, or
    the TOCTOU-closing layer ships dead while every other test stays green — the
    `islink` refusal covers the same cases, so nothing else would redden.

    This is not hypothetical: `os.replace` is NOT in `os.supports_dir_fd` on
    Linux (only `os.rename` is) even though it accepts src_dir_fd/dst_dir_fd, so
    probing `os.replace` — the function the code used to call — made the branch
    unreachable on every platform. Observe the anchoring behaviorally rather
    than re-deriving the probe, so editing the probe reddens this.

    Ablation guard: change the probe to `os.replace` (or drop the branch) and
    this fails; no other test notices."""
    real_open, real_supports = os.open, os.supports_dir_fd
    # The premise, asserted rather than assumed: were a future CPython to drop
    # rename from the set, the branch would go dead and this says so directly.
    assert {real_open, os.rename} <= real_supports
    anchored = []

    def spy(path, flags, mode=0o777, *, dir_fd=None):
        anchored.append(dir_fd)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", spy)
    # The probe tests os.open by IDENTITY, so the spy has to be in the capability
    # set too or the probe answers False and this measures the fallback instead
    # of the branch it exists to pin — which is how it first went red.
    monkeypatch.setattr(os, "supports_dir_fd", real_supports | {spy})
    events._write_event(str(tmp_path / "events"), "1-t1-Stop.json", {"event": "Stop"})
    monkeypatch.undo()

    assert any(fd is not None for fd in anchored), "the create was never anchored to a dir_fd"


@pytest.mark.parametrize("forced_fallback", [False, True])
def test_a_short_os_write_still_publishes_the_whole_payload(tmp_path, monkeypatch, forced_fallback):
    """`os.write` may write FEWER bytes than asked and just return the count. The
    buffered `open()` this replaced looped internally; the raw fd needed for
    O_NOFOLLOW/dir_fd does not. A truncated event file is not retried but LOST:
    `SignalWatcher.poll` adds a name to `_consumed` before parsing it, so
    malformed JSON is skipped and never re-read — the Stop signal is gone and the
    run waits out `session_timeout_min`.

    Both branches are driven: the loop is shared, but the fallback is the only
    path Windows takes and POSIX would otherwise never exercise it.

    Ablation guard: collapsing `_write_all` back to a single `os.write` makes
    this fail. Nothing else in the suite catches that — a real `os.write`
    returns the full count, so the normal-path tests pass either way."""
    events_dir = tmp_path / "events"
    real_write = os.write

    def a_byte_at_a_time(fd, data):
        return real_write(fd, bytes(data)[:1])  # a legal short write

    if forced_fallback:
        monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    event = {"event": "Stop", "task_id": "t1", "session_id": "s" * 300}
    monkeypatch.setattr(os, "write", a_byte_at_a_time)
    events._write_event(str(events_dir), "1-t1-Stop.json", event)
    monkeypatch.undo()

    published = list(events_dir.glob("*.json"))
    assert len(published) == 1
    assert json.loads(published[0].read_text()) == event
    assert list(events_dir.glob("*.tmp")) == []


def test_a_zero_length_write_raises_instead_of_spinning(tmp_path, monkeypatch):
    """`_write_all` loops on short writes, so a descriptor that always accepts 0
    bytes would spin forever. Refuse instead: the caller degrades to a no-op and
    the run takes the timeout path, which beats a hook process that never exits.

    Ablation guard: dropping the `written <= 0` arm hangs this test."""
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="short write"):
        events._write_event(str(tmp_path / "events"), "1-t1-Stop.json", {"event": "Stop"})


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_junctioned_events_dir_writes_nothing_and_exits_zero(tmp_path, monkeypatch, capsys):
    """The Windows half of the symlink test — Windows CI is its only oracle, a
    junction cannot be created on POSIX."""
    # If either tag constant were misnamed the tuple is empty and the refusal
    # silently never fires. Assert it directly rather than inferring from below.
    assert events._LINK_REPARSE_TAGS

    target = tmp_path / "attacker"
    target.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(run_dir / "events"), str(target)],
        check=True,
        capture_output=True,
    )
    # The premise, asserted rather than assumed: were a future CPython to start
    # reporting junctions as links, this says so directly instead of going green
    # for the wrong reason.
    assert os.path.islink(run_dir / "events") is False

    assert _relay("Stop", {"session_id": "s1"}, monkeypatch, run_dir) == 0
    assert capsys.readouterr() == ("", "")
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_event_file_mode_is_0600(tmp_path, monkeypatch):
    """Event files carry the orchestrator's control plane; nothing but the
    operator running the loop needs to read them (narrowed from the umask-derived
    0644 a plain `open()` produced).

    Ablation guard: drop the `0o600` argument from either `os.open` call and this
    fails (the default 0o777 lands as 0o755 under the usual umask)."""
    assert _relay("Stop", {"session_id": "s1"}, monkeypatch, tmp_path) == 0
    written = next((tmp_path / "events").glob("*.json"))
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


# ------------------------------------------------------------- the relay command


def test_relay_writes_the_event_and_says_nothing(tmp_path, monkeypatch, capsys):
    """The happy path, and the stdout invariant that rides on every path with it:
    the hosts parse hook stdout, so a single stray line there is a protocol
    violation even when the event itself landed."""
    payload = {
        "session_id": "abc-123",
        "transcript_path": "/home/u/.claude/projects/x/abc-123.jsonl",
        "cwd": "/proj",
    }
    assert _relay("Stop", payload, monkeypatch, tmp_path, task_id="1-1-a-dev-1") == 0
    assert capsys.readouterr() == ("", "")

    files = list((tmp_path / "events").glob("*.json"))
    assert len(files) == 1
    assert "1-1-a-dev-1" in files[0].name and "Stop" in files[0].name
    event = json.loads(files[0].read_text())
    assert event["event"] == "Stop"
    assert event["task_id"] == "1-1-a-dev-1"
    assert event["session_id"] == "abc-123"
    assert event["transcript_path"].endswith("abc-123.jsonl")
    assert not list((tmp_path / "events").glob("*.tmp"))


def test_relay_is_a_silent_noop_outside_a_driven_session(tmp_path, monkeypatch, capsys):
    """An operator (or a stray hook config) can invoke `bmad-loop relay` in a
    session bmad-loop never spawned. The session-protocol env is the detector, and
    the answer is to write nothing and say nothing at rc 0 — a normal interactive
    session must be unaffected by having the hooks installed."""
    monkeypatch.chdir(tmp_path)
    assert _relay("Stop", {"session_id": "s1"}, monkeypatch, None) == 0
    assert capsys.readouterr() == ("", "")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "garbage",
    ["", "   ", "{not json", "[1, 2, 3]", '"a bare string"', "null"],
    ids=["empty", "blank", "truncated", "list", "string", "null"],
)
def test_relay_tolerates_garbage_on_stdin(tmp_path, monkeypatch, capsys, garbage):
    """A hook that fires with nothing (or something non-dict) on stdin still has to
    produce the event: the run's completion signal rides on the file, not on the
    payload. Every unusable payload collapses to nulls, never to a refusal.

    Ablation guard: delete the `isinstance(payload, dict)` collapse and the
    list/string/null rows fail on the `.get` that follows. Every row here is a
    `json.JSONDecodeError`, so the rest of `_read_payload`'s `except` is pinned
    separately by `test_relay_tolerates_an_unreadable_stdin`."""
    assert _relay("SessionEnd", garbage, monkeypatch, tmp_path) == 0
    assert capsys.readouterr() == ("", "")
    files = list((tmp_path / "events").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["session_id"] is None


def _run_twin(twin: str, event: str, stdin: bytes, run_dir: Path, **env: str):
    """Run one relay twin as a real subprocess, feeding ``stdin`` as raw bytes.
    ``twin`` is "hook" (the copied script) or "relay" (``bmad-loop relay``)."""
    argv = (
        [sys.executable, str(HOOK), event]
        if twin == "hook"
        else [sys.executable, "-m", "bmad_loop", "relay", event]
    )
    return subprocess.run(
        argv,
        input=stdin,
        # As in test_relay_and_the_hook_shape_the_same_event: `env=` replaces the
        # environment, and a Windows child needs SYSTEMROOT to start at all.
        env={
            "PATH": os.environ.get("PATH", ""),
            **({"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if os.name == "nt" else {}),
            "BMAD_LOOP_RUN_DIR": str(run_dir),
            "BMAD_LOOP_TASK_ID": "t1",
            **env,
        },
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize("twin", ["hook", "relay"])
def test_utf8_payload_survives_cp1252_stdin(tmp_path, twin):
    """A Windows host runs the relay without UTF-8 mode, so a text stdin decodes
    with the locale code page (cp1252). The CLIs write UTF-8, so a non-ASCII
    transcript path either fails to decode (0x81 is undefined in cp1252, so the
    whole payload became `{}`) or comes out garbled and names a file that does
    not exist. Both twins must read the bytes and recover the exact path.

    Ablation: revert `_read_payload` to `json.load(stdin)` (on either twin) and
    that twin's row fails — the event carries a null path."""
    path = str(tmp_path / "Ánna" / "rollout.jsonl")
    payload = {"session_id": "s1", "transcript_path": path}
    stdin = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert b"\xc3\x81" in stdin

    proc = _run_twin(twin, "Stop", stdin, tmp_path, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")

    assert proc.returncode == 0, proc.stderr
    event = json.loads(next((tmp_path / "events").glob("*.json")).read_text(encoding="utf-8"))
    assert event["transcript_path"] == path
    assert event["session_id"] == "s1"
    assert "payload_error" not in event


@pytest.mark.parametrize("twin", ["hook", "relay"])
@pytest.mark.parametrize(
    ("stdin", "expected"),
    [
        (b'{"session_id": "s1", "transcript_pa', "JSONDecodeError"),
        (b'["s1"]', "not-an-object"),
        (b'{"session_id": "s1"}', None),
    ],
    ids=["truncated", "list", "valid"],
)
def test_unreadable_payload_is_tagged(tmp_path, twin, stdin, expected):
    """An unreadable payload still writes the event (the completion signal rides
    on it), but the event says WHY it carries nulls, so the orchestrator can crumb
    the degrade instead of folding it into an ordinary missing transcript. A
    readable payload's event has no `payload_error` key at all.

    Ablation: drop the tag (return `{}, None` from `_read_payload`'s failure
    arms) and the truncated/list rows fail on either twin."""
    proc = _run_twin(twin, "Stop", stdin, tmp_path)

    assert proc.returncode == 0, proc.stderr
    event = json.loads(next((tmp_path / "events").glob("*.json")).read_text(encoding="utf-8"))
    assert event.get("payload_error") == expected
    if expected is None:
        assert "payload_error" not in event
        assert event["session_id"] == "s1"
    else:
        assert event["session_id"] is None


class _UnreadableStream:
    """A stdin `json.load` cannot read. `json.load` calls `fp.read()`, so the
    failure surfaces from there exactly as a real one would."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def read(self, *_a):
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [
        OSError("broken pipe"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
    ids=["oserror", "undecodable"],
)
def test_relay_tolerates_an_unreadable_stdin(tmp_path, monkeypatch, capsys, exc):
    """Beyond malformed JSON, the read itself can fail: the host can hand the hook
    a closed descriptor, or bytes that are not valid UTF-8 (a transcript path from
    a differently-encoded filesystem is the realistic source). Neither is
    actionable and neither may cost the event — the run's completion signal rides
    on the file landing.

    These are the two arms the malformed-JSON rows do NOT reach:
    `UnicodeDecodeError` is a `ValueError` but not a `json.JSONDecodeError`, and
    `OSError` is neither.

    Ablation guard: narrow `_read_payload`'s `except (ValueError, OSError)` to
    `json.JSONDecodeError` and both rows fail — the exception escapes to
    `cmd_relay`'s backstop, so no event is written and stderr is no longer
    empty."""
    monkeypatch.setenv("BMAD_LOOP_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("BMAD_LOOP_TASK_ID", "t1")
    # An outer bmad-loop session exports this variable. This row exercises the
    # legacy RUN_DIR/events fallback, so isolate it just as `_relay` does.
    monkeypatch.delenv("BMAD_LOOP_EVENTS_DIR", raising=False)
    monkeypatch.setattr(sys, "stdin", _UnreadableStream(exc))

    assert cli.main(["relay", "Stop"]) == 0
    assert capsys.readouterr() == ("", "")
    files = list((tmp_path / "events").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["session_id"] is None


def test_relay_degrades_to_zero_when_the_write_fails(tmp_path, monkeypatch, capsys):
    """Any OSError out of the write — a full disk, a read-only run dir, the
    redirect refusals above — degrades to the orchestrator's normal
    `session_timeout_min` path. A non-zero rc here is surfaced by several hosts as
    a failed tool call inside the very session whose completion this reports.

    Ablation guard: delete the `except OSError` arm in `events.relay` and this
    fails with the OSError escaping to the caller."""

    def boom(*_a, **_k):
        raise OSError("no space left on device")

    monkeypatch.setattr(events, "_write_event", boom)
    assert _relay("Stop", {"session_id": "s1"}, monkeypatch, tmp_path) == 0
    assert capsys.readouterr() == ("", "")


def test_relay_survives_an_unexpected_exception(tmp_path, monkeypatch, capsys):
    """The backstop for a bug in this code path rather than a hostile events dir.
    Same reason as the OSError arm — a hook that exits non-zero breaks the session
    — but it reports on stderr, which the hosts do not parse, instead of failing
    silently.

    Ablation guard: delete `cmd_relay`'s `except Exception` and this fails with
    the RuntimeError escaping `main()`."""

    def boom(*_a, **_k):
        raise RuntimeError("a bug, not a hostile events dir")

    monkeypatch.setattr(events, "relay", boom)
    assert _relay("Stop", {"session_id": "s1"}, monkeypatch, tmp_path) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "RuntimeError" in err


def test_relay_runs_without_the_mux_or_a_readable_policy(tmp_path, monkeypatch, capsys):
    """`main()` configures the mux backend from policy before dispatch, for the
    handlers that reach the mux without ever loading policy. Relay reaches neither,
    and a project whose policy.toml is unparseable must still be able to report
    that its session stopped — so relay dispatches ahead of that call entirely.

    Ablation guard: move the relay dispatch below `_configure_mux(_project(args))`
    and this fails — `main()`'s `except PolicyError` arm prints `error: …` and
    returns 1, which is exactly the CLI-window failure the hook contract forbids."""

    def explode(_project):
        raise policy_mod.PolicyError("policy.toml is not valid TOML")

    monkeypatch.setattr(cli, "_configure_mux", explode)
    assert _relay("Stop", {"session_id": "s1"}, monkeypatch, tmp_path) == 0
    assert capsys.readouterr() == ("", "")
    assert len(list((tmp_path / "events").glob("*.json"))) == 1


def test_relay_dispatches_outside_the_shared_error_handler(tmp_path, monkeypatch):
    """The placement of the dispatch, stated directly rather than inferred: relay
    is not wrapped by `main()`'s shared try/except, whose arms print `error: …`
    and return 1 or 130. `cmd_relay` is total, so in production nothing reaches
    this — the test forces the question by making the handler itself raise.

    Ablation guard: delete the early dispatch and this fails; the raise is caught
    by `main()`'s typed arm and turned into rc 1."""

    def explode(_args):
        raise policy_mod.PolicyError("would be swallowed by main()'s handler")

    monkeypatch.setattr(cli, "cmd_relay", explode)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    with pytest.raises(policy_mod.PolicyError):
        cli.main(["relay", "Stop"])


def test_relay_defaults_the_event_name_like_the_hook(tmp_path, monkeypatch, capsys):
    """The hook script reads `sys.argv[1] if len(sys.argv) > 1 else "Unknown"`, so
    a misconfigured registration that forgets the event name still produces a file
    the operator can see. argparse would otherwise turn that into a usage error at
    rc 2, before any handler runs — nothing `cmd_relay` does could take it back."""
    assert _relay_no_event({"session_id": "s1"}, monkeypatch, tmp_path) == 0
    assert capsys.readouterr() == ("", "")
    assert "Unknown" in next((tmp_path / "events").glob("*.json")).name


def _relay_no_event(payload, monkeypatch, run_dir: Path) -> int:
    monkeypatch.setenv("BMAD_LOOP_RUN_DIR", str(run_dir))
    monkeypatch.setenv("BMAD_LOOP_TASK_ID", "t1")
    monkeypatch.delenv("BMAD_LOOP_EVENTS_DIR", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return cli.main(["relay"])


# ------------------------------------------- where the event is written (#494)


def test_relay_prefers_the_events_dir_env(tmp_path, monkeypatch, capsys):
    """The relay is the OTHER writer of this control plane, so it resolves the
    directory exactly as the copied hook script does — the two are pointed at one
    channel by the same variable, and a preference that held on only one of them
    would split the channel in half depending on which target a project's hook
    config happens to name."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # Not `events` — that name is the imported module for the rest of this file.
    events_dir = tmp_path / "state" / "runs" / "RID" / "events"

    assert _relay("Stop", {"session_id": "s1"}, monkeypatch, run_dir, events_dir=events_dir) == 0
    assert capsys.readouterr() == ("", "")

    files = list(events_dir.glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["session_id"] == "s1"
    assert not (run_dir / "events").exists()


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_relay_falls_back_to_the_run_dir(tmp_path, monkeypatch, capsys, value):
    """An orchestrator predating #494 names no events dir; its sessions must still
    write where it polls. Empty is the same case in disguise — `export
    BMAD_LOOP_EVENTS_DIR=` leaves an empty value behind, and an empty path names
    the launch cwd rather than a control plane.

    Ablation guard: swap the `or` for a presence test and the empty case writes
    into the cwd — this fails."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert _relay("Stop", {"session_id": "s1"}, monkeypatch, run_dir, events_dir=value) == 0
    assert capsys.readouterr() == ("", "")

    files = list((run_dir / "events").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["session_id"] == "s1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_relay_refuses_a_symlinked_env_directed_events_dir(tmp_path, monkeypatch, capsys):
    """The #493 hardening is a property of the directory the relay is pointed at,
    not of how that directory was derived — so it must hold for an env-named one,
    with the same silent rc 0 degrade."""
    target = tmp_path / "attacker"
    target.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "events").symlink_to(target, target_is_directory=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rc = _relay("Stop", {"session_id": "s1"}, monkeypatch, run_dir, events_dir=state / "events")
    assert rc == 0
    assert capsys.readouterr() == ("", "")
    assert list(target.iterdir()) == []
    assert list((state / "events").iterdir()) == []
    assert not (run_dir / "events").exists()  # no silent fallback to the legacy dir
