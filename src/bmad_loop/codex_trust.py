"""Read Codex's own hook trust verdict without starting a model turn.

The private trust hash belongs to Codex.  This module asks its read-only
``hooks/list`` app-server method and joins that answer to the exact commands in
the hook config at the directory an operation will use.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .adapters.profile import CLIProfile, ProfileError
from .install import _hook_command, _relay_command
from .process_host import ProcessHostError, get_process_host

_EVENTS = {"SessionStart": "sessionStart", "Stop": "stop"}
_RELAY_MARKER = "bmad-loop"
_PROBE_MARKER = "bmad_loop_probe_hook.py"
_TIMEOUT_S = 5.0
# Runs the client with an in-process app-server instead of a shared background
# daemon, so tools and hooks inherit the session window's BMAD_LOOP_* env.
NO_DAEMON_ARG = "--no-daemon"
# The first Codex that accepts NO_DAEMON_ARG; older ones exit at launch on it.
CODEX_MIN_VERSION: tuple[int, int, int] = (0, 156, 0)
# Why a Codex session can report no hook events: the sweep's failure text and
# the adapter's session-start-missing journal notice both carry it.
DAEMON_ENV_HINT = (
    "a Codex session attached to a shared app-server daemon runs tools and hooks"
    f" without the BMAD_LOOP_* environment; keep {NO_DAEMON_ARG} in the profile's"
    f" launch_args (Codex >= {CODEX_MIN_VERSION[0]}.{CODEX_MIN_VERSION[1]})"
)
# Launch args that leave hook discovery unchanged: the bypass switch changes
# approvals, and NO_DAEMON_ARG changes where the app-server is hosted.
_HOOK_DISCOVERY_SAFE_ARGS = frozenset({"--dangerously-bypass-approvals-and-sandbox", NO_DAEMON_ARG})
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
# The ``unverifiable`` reason when the ``hooks/list`` query itself failed (spawn
# error, timeout, protocol breakage) rather than Codex returning a verdict.
# Exported so callers can tell a transient query failure from a real answer.
QUERY_FAILED_REASON = "hook trust could not be queried from Codex"


@dataclass(frozen=True)
class TrustResult:
    status: str  # trusted | untrusted | unverifiable
    reason: str


def parse_codex_version(text: str) -> tuple[int, int, int] | None:
    """The first ``X.Y.Z`` in ``codex --version`` output, or None when there is
    none. Codex prints ``codex-cli 0.164.0``."""
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    major, minor, patch = (int(group) for group in match.groups())
    return major, minor, patch


def hook_discovery_args_safe(args: tuple[str, ...] | None) -> bool:
    """Whether extra launch args can leave hook discovery unchanged."""
    return args is None or all(arg in _HOOK_DISCOVERY_SAFE_ARGS for arg in args)


def resolved_codex_binary(binary: str, env: dict[str, str]) -> str | None:
    """Resolve with the same PATH used by the trust query and session launch."""
    return shutil.which(binary, path={**os.environ, **env}.get("PATH"))


def _commands(
    config: object, profile: CLIProfile, project: Path, marker: str
) -> dict[str, list[str]] | None:
    if not isinstance(config, dict) or not isinstance(config.get("hooks"), dict):
        raise ValueError("malformed Codex hook config")
    events = profile.hooks.events
    if not all(events.get(event) == event for event in _EVENTS):
        return None
    found: dict[str, list[str]] = {}
    for canonical in _EVENTS:
        if marker == _RELAY_MARKER:
            expected_command = _hook_command(project, profile, canonical)
        else:
            host = get_process_host()
            expected_command = (
                f"{host.hook_interpreter()} {host.shell_quote(str(project / marker))} {canonical}"
            )
        handlers = config["hooks"].get(canonical)
        if handlers is None:
            return None
        if not isinstance(handlers, list):
            raise ValueError("malformed Codex hook handlers")
        commands: list[str] = []
        for group in handlers:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("malformed Codex hook group")
            for hook in group["hooks"]:
                if not isinstance(hook, dict):
                    raise ValueError("malformed Codex hook entry")
                command = hook.get("command")
                if not isinstance(command, str):
                    continue
                is_relay = _relay_command(command) if marker == _RELAY_MARKER else marker in command
                if is_relay:
                    # A SessionStart matcher can exclude startup even when
                    # Codex reports the command trusted and enabled. The
                    # installed relay has none; refuse customized matchers.
                    if canonical == "SessionStart" and group.get("matcher") not in (None, ""):
                        return None
                    if command != expected_command:
                        return None
                    commands.append(command)
        if not commands:
            return None
        found[canonical] = commands
    return found


def _hooks_list(binary: str, cwd: Path, env: dict[str, str]) -> object:
    """Execute initialize → initialized → hooks/list with one absolute deadline."""
    child = subprocess.Popen(
        [binary, "app-server", "--stdio"],
        cwd=cwd,
        env={**os.environ, **env},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    stdin, stdout = child.stdin, child.stdout
    assert stdin is not None and stdout is not None
    lines: queue.Queue[str] = queue.Queue()

    def read_lines() -> None:
        for line in stdout:
            lines.put(line)
        lines.put("")

    threading.Thread(target=read_lines, daemon=True).start()
    deadline = time.monotonic() + _TIMEOUT_S

    def send(message: dict) -> None:
        stdin.write(json.dumps(message) + "\n")
        stdin.flush()

    def response(identifier: int) -> object:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex hooks/list timed out")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Codex hooks/list timed out") from exc
            if not line:
                raise ValueError("Codex app server closed before hooks/list")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("Codex app server returned invalid JSON") from exc
            if not isinstance(message, dict) or message.get("id") != identifier:
                continue  # notifications and unrelated messages
            if "error" in message:
                raise ValueError("Codex app server refused hooks/list")
            if "result" not in message:
                raise ValueError("Codex app server omitted a result")
            return message["result"]

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "bmad-loop", "version": "0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        response(1)
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "hooks/list",
                "params": {"cwds": [str(cwd.resolve())]},
            }
        )
        return response(2)
    finally:
        if child.poll() is None:
            if os.name == "nt":
                # An npm .cmd shim is a cmd.exe parent. Kill its tree before
                # the wrapper exits and leaves the app server running.
                try:
                    get_process_host().force_kill(child.pid)
                except (OSError, ProcessHostError):
                    child.kill()
            else:
                child.kill()
        child.wait(timeout=2)


def project_hook_trust(
    project: Path,
    profile: CLIProfile,
    *,
    binary: str | None = None,
    marker: str = _RELAY_MARKER,
) -> TrustResult:
    """Fail closed unless Codex reports every required configured hook trusted."""
    if profile.hooks.dialect != "codex-hooks-json":
        return TrustResult("unverifiable", "hook trust applies only to Codex hooks")
    # The bypass switch changes approvals, and --no-daemon changes where the
    # app-server is hosted; neither changes which config or hooks are discovered,
    # and the `_hooks_list` oracle already runs its own `app-server --stdio`. Other
    # launch arguments can select a different config and cannot be mirrored here.
    if not hook_discovery_args_safe(profile.launch_args) or not hook_discovery_args_safe(
        profile.bypass_args
    ):
        return TrustResult("unverifiable", "hook trust cannot verify profile launch arguments")
    config_path = (project / profile.hooks.config_path).resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return TrustResult("unverifiable", "hook trust config is unreadable")
    try:
        commands = _commands(config, profile, project, marker)
    except ProfileError as e:
        return TrustResult("unverifiable", f"hook trust installed relay unavailable: {e}")
    except ValueError:
        return TrustResult("unverifiable", "hook trust config has malformed fields")
    if commands is None:
        return TrustResult(
            "untrusted", "hook trust: a required SessionStart or Stop relay is not usable"
        )
    # Windows npm installs expose a codex.cmd shim through PATHEXT. Popen with
    # a bare name need not find it; which() returns the executable run would use.
    resolved_binary = resolved_codex_binary(binary or profile.binary, profile.env)
    if resolved_binary is None:
        return TrustResult("unverifiable", "hook trust Codex binary is unavailable")
    try:
        result = _hooks_list(resolved_binary, project, profile.env)
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        return TrustResult("unverifiable", QUERY_FAILED_REASON)
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return TrustResult("unverifiable", "hook trust response has an unfamiliar shape")
    entries = result["data"]
    if len(entries) != 1 or not isinstance(entries[0], dict):
        return TrustResult("unverifiable", "hook trust response has an unfamiliar directory")
    entry = entries[0]
    if entry.get("cwd") != str(project.resolve()):
        return TrustResult("unverifiable", "hook trust response names another directory")
    if (
        not isinstance(entry.get("errors"), list)
        or not isinstance(entry.get("hooks"), list)
        or not isinstance(entry.get("warnings"), list)
    ):
        return TrustResult("unverifiable", "hook trust response has malformed fields")
    if entry["errors"] or entry["warnings"]:
        return TrustResult("unverifiable", "hook trust discovery reported errors or warnings")
    hooks = entry["hooks"]
    for hook in hooks:
        if (
            not isinstance(hook, dict)
            or not all(
                isinstance(hook.get(key), str) for key in ("sourcePath", "eventName", "trustStatus")
            )
            or not isinstance(hook.get("enabled"), bool)
        ):
            return TrustResult("unverifiable", "hook trust response contains a malformed hook")
    for canonical, expected in commands.items():
        matches: list[dict] = []
        for hook in hooks:
            if (
                hook.get("sourcePath") != str(config_path)
                or hook.get("eventName") != _EVENTS[canonical]
            ):
                continue
            if hook.get("handlerType") != "command" or not isinstance(hook.get("command"), str):
                return TrustResult(
                    "unverifiable", "hook trust response has unfamiliar handler fields"
                )
            matches.append(hook)
        for command in expected:
            candidates = [h for h in matches if h["command"] == command]
            if len(candidates) != 1:
                return TrustResult("untrusted", f"hook trust: Codex omitted the {canonical} relay")
            hook = candidates[0]
            status = hook.get("trustStatus")
            if not isinstance(status, str) or not isinstance(hook.get("enabled"), bool):
                return TrustResult("unverifiable", "hook trust response has malformed trust fields")
            if status not in {"trusted", "managed", "modified", "untrusted"}:
                return TrustResult("unverifiable", "hook trust response has unfamiliar status")
            if status not in {"trusted", "managed"} or not hook["enabled"]:
                return TrustResult(
                    "untrusted", f"hook trust is stale for {canonical}; accept hooks in Codex"
                )
    return TrustResult("trusted", "Codex hook trust current for SessionStart and Stop")
