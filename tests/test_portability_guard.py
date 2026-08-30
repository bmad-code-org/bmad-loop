"""Regression guard against POSIX-only patterns creeping back into the core.

The POSIX-decoupling pass (multiplexer seam + portability fixes) quarantined
every Unix assumption behind a single tmux backend and a handful of
platform-guarded helpers. This guard byte/AST-scans ``src/bmad_loop`` so a new
hard POSIX dependency can't sneak in unnoticed. Each sanctioned exception lives
in an allowlisted file and — outside the wholesale tmux quarantine — carries a
``# portability:`` ack on its line, so exceptions stay deliberate.

The same single-pass scan also carries the two non-POSIX quarantines that have the
identical shape: AGENTS.md's "New core env vars register in ``envvars.py``;
plugin-owned env-var families stay with their plugin" — see
``test_bmad_loop_env_reads_only_in_the_registry`` — and its "all git subprocess
calls go through the ``_run_git`` chokepoint in ``verify.py``" — see
``test_no_git_invocation_outside_verify``.

If this test flags something unexpected, fix the source (route it through the
seam / a platform helper) rather than widening an allowlist.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import bmad_loop
from bmad_loop import envvars

SRC = Path(bmad_loop.__file__).resolve().parent
# Marker an allowlisted exception line must carry. Written as ``# portability: …``;
# matched as the bare keyword so it also rides along on a ``# nosec B108 portability: …``.
ACK = "portability:"

# ----------------------------------------------------------------- allowlists

# The files allowed to shell out to ``tmux`` — the whole-file quarantine for
# tmux/POSIX-shell knowledge, split across the shared base (where the spawn
# primitive + argv live) and its POSIX leaf. No per-line ack needed: these files
# *are* the sanctioned spot (their module docstrings say so).
TMUX_BACKENDS = {"adapters/tmux_base.py", "adapters/tmux_backend.py"}

# The one file allowed to build a ``["git", ...]`` argv — and within it, only as
# the argv argument of a ``_run_git(...)`` call, the position where the
# chokepoint (engine-configured timeout, ``LC_ALL=C``, the GitError taxonomy) is
# being FED rather than bypassed. Unlike the tmux quarantine the sanction is not
# the whole file: verify.py is mostly non-chokepoint helpers, and a bare
# ``subprocess.run(["git", ...])`` added to one of them skips all three
# guarantees exactly like a bypass in any other module, so the scanner tags each
# git finding with the call-position bit and ``_git_offenders`` requires both
# halves. Every other module calls a verify helper (``git_bytes``,
# ``worktree_clean``, …) instead of spawning git itself. Both bypasses open when
# this guard landed carried real defects (#390): a strict decode crashing the
# TUI checkpoint modal, and a probe ignoring `limits.git_timeout_s`.
GIT_CHOKEPOINT = {"verify.py"}

# Files where resolving a raw `task.spec_file` / `task.dispatched_spec_file` with a
# bare `Path(...)` is CORRECT, because the reader runs inside the tree the value was
# recorded against. `runs.py` is the chokepoint itself; `engine.py`, `verify.py` and
# `recovery_flow.py` are in-process consumers driving a live run, where the field is
# still the absolute path the engine stamped and no reload has round-tripped it
# through `StoryTask.to_dict`.
#
# Everywhere else the field arrives from `load_state`, and
# `_serialized_worktree_path` persists an isolated unit's spec RELATIVE to the mount.
# A bare `Path(...)` there resolves against the READER's cwd — the main checkout,
# which carries the same `_bmad-output/specs/...` layout and answers with the wrong
# tree's copy. That defect shipped in `tui/app.py::_paused_spec`, where it reached a
# destructive write, and was then re-found one surface at a time in `resolve.py`,
# `sweep.py`, `stories_engine.py` and `worktree_flow.py` across four review rounds.
# Nothing enforced the rule, which is why each round only ever found the next one.
#
# Adding a file here is a claim that its cwd IS the run's tree. If it is not, route
# the read through `runs.task_spec_path` (or `StoryTask.rebase_spec_paths_on` when
# re-anchoring persisted state) instead.
SPEC_ANCHOR_CHOKEPOINT = {"runs.py", "engine.py", "verify.py", "recovery_flow.py"}
SPEC_PATH_FIELDS = {"spec_file", "dispatched_spec_file"}

# Files that may name a bare POSIX path, each on a line carrying a `# portability:`
# ack. process_host.py's Linux identity reader walks `/proc/<pid>/stat` behind a
# sys.platform branch; the Unity teardown scripts are POSIX-only. verify.py is the
# one non-platform case: git's *diff format* spells an absent file `/dev/null` on
# every platform, so `patch_new_files` compares against it as a protocol token.
PATH_ALLOW = {
    "data/plugins/unity/unity_cleanup.py",
    "data/plugins/unity/unity_teardown.py",
    "process_host.py",
    "verify.py",
}

# The detach helpers that legitimately request POSIX `start_new_session` (each
# branches on `sys.platform` for a Windows creationflags fallback).
DETACH_ALLOW = {
    "platform_util.py",
    "data/plugins/unity/unity_setup.py",
    "data/plugins/unity/unity_plugin.py",
}

# `os.kill(pid, 0)` is a read-only existence probe on POSIX but *destructive* on
# Windows (it maps to TerminateProcess). Confine it to the platform-guarded
# liveness helpers, each on a line carrying a `# portability:` ack; everything
# else routes through the ProcessHost seam (`get_process_host().is_alive`). The
# Unity teardown no longer probes directly — it delegates to the seam.
KILL_PROBE_ALLOW = {
    "process_host.py",
}

# Broader than the signal-0 probe: *any* `os.kill(` — a real signal send is just as
# destructive-on-Windows as the probe form. Only the ProcessHost may call it directly;
# everything else routes through the seam (terminate / force_kill / is_alive).
OS_KILL_ALLOW = {
    "process_host.py",
}

# The two sanctioned `shell=True` spots: operator-authored command strings whose
# cmd/PowerShell port is an explicit out-of-scope follow-up.
SHELL_ALLOW = {
    "verify.py",
    "plugins/bus.py",
}

# Bare POSIX paths that must not be hardcoded outside PATH_ALLOW. `os.devnull` is
# the portable replacement for "/dev/null".
POSIX_PATHS = ("/tmp", "/proc", "/dev/null")

# The subprocess spawn entry points a string-form git command could ride in on —
# `subprocess.run("git status", shell=True)`, or the same string with no shell at
# all, which Windows happily execs (CreateProcess takes a command line). Matched
# as `subprocess.<name>(...)` or as the bare from-import spelling. String
# detection anchors on these calls, unlike the sequence detector, because a
# string starting with "git " is routinely prose (an error message, a doc line)
# while a sequence literal headed by "git" is not.
SPAWN_CALL_NAMES = {"run", "Popen", "call", "check_call", "check_output"}

# Prefix that makes an environment variable this project's to register.
ENV_PREFIX = "BMAD_LOOP_"

# ``CONSTANT_NAME -> "BMAD_LOOP_…"`` for the registry's own public constants, read
# off the live module so the guard cannot drift from it: register a fourth var in
# envvars.py and the scan resolves reads spelled through it with no edit here.
# This is what lets a read reach the guard when it borrows the registry's constant
# but skips the registry's reader — the shape a well-meaning change actually takes.
REGISTRY_NAMES = {
    name: value
    for name, value in vars(envvars).items()
    if isinstance(value, str) and value.startswith(ENV_PREFIX)
}

# The session-protocol vars the engine injects into every child session so a
# stand-alone script can find the run it belongs to. They are not operator knobs:
# engine.py / resolve.py / probe.py / plugins.bus build them on the producing side,
# and these scripts read back what was handed to them.
SESSION_PROTOCOL_ENV = (
    "BMAD_LOOP_RUN_DIR",
    "BMAD_LOOP_EVENTS_DIR",
    "BMAD_LOOP_TASK_ID",
    "BMAD_LOOP_WORKTREE",
    "BMAD_LOOP_REPO_ROOT",
    "BMAD_LOOP_CLEAN_TMP",
    "BMAD_LOOP_QUIESCE_PHASE",
    "BMAD_LOOP_PROBE_CAPTURE_DIR",
)

# The plugin's own families, which AGENTS.md's second clause leaves with the plugin
# ("plugin-owned env-var families stay with their plugin"), plus the session
# protocol every injected script reads.
UNITY_ENV = ("BMAD_LOOP_UNITY_", "BMAD_LOOP_ENGINE_", *SESSION_PROTOCOL_ENV)

# ``rel -> the keys and key families that file may read straight out of the
# environment``. Scoped by FAMILY rather than by file on purpose: a
# file-wide exemption would let one of these read a core knob such as
# `BMAD_LOOP_MUX_BACKEND` inline and have the finding dropped on its path alone,
# which is the exact distinction the invariant draws.
#
# `envvars.py` *is* the registry — the one place a core var is named, typed and
# given a reader (AGENTS.md: "New core env vars register in `envvars.py`") — so it
# is scoped to the names it defines, read off the live module: register a fourth
# var there and this needs no edit. The two hook relays are copied OUT of the
# package into the target project and run inside the coding CLI's process under
# whatever interpreter the host has (both say "Stdlib only" in their docstrings),
# so they cannot import bmad_loop to reach the registry at all; the Unity helpers
# are stand-alone the same way. None of them may reach past the families below.
#
# Writes stay out of scope on purpose: engine/resolve/probe/plugins.bus/unity_plugin
# *build* a `BMAD_LOOP_*` env dict to inject into a child session, and that
# producing side is what these readers consume, not a second source of truth.
ENV_READ_ALLOW = {
    "envvars.py": tuple(REGISTRY_NAMES.values()),
    # `events.py` is the ONE in-package entry here, and the "cannot import
    # bmad_loop" justification above does not reach it — it obviously can. It is
    # exempt as the importable PARITY TWIN of the stdlib-only hook relay: the same
    # session-protocol vars, read at the same points in the same protocol, by
    # the code the hook config points at when it points at `bmad-loop relay`
    # instead of the copied script. Routing one twin through `envvars` and leaving
    # the other on `os.environ` would put the reads out of parity, and parity is
    # what the AST test on those two files exists to keep. Family-scoped like the
    # rest, so a core knob read inline here is still an offender.
    "events.py": SESSION_PROTOCOL_ENV,
    "data/bmad_loop_hook.py": SESSION_PROTOCOL_ENV,
    "data/bmad_loop_probe_hook.py": SESSION_PROTOCOL_ENV,
    "data/plugins/unity/unity_cleanup.py": UNITY_ENV,
    "data/plugins/unity/unity_dialog_probe.py": UNITY_ENV,
    "data/plugins/unity/unity_quiesce.py": UNITY_ENV,
    "data/plugins/unity/unity_ready.py": UNITY_ENV,
    "data/plugins/unity/unity_seed_assets.py": UNITY_ENV,
    "data/plugins/unity/unity_setup.py": UNITY_ENV,
    "data/plugins/unity/unity_teardown.py": UNITY_ENV,
}


def _env_key_allowed(key: str, entries: tuple[str, ...]) -> bool:
    """A trailing underscore marks a FAMILY, matched as a prefix; every other entry
    is one variable, matched exactly.

    The split is the difference between exempting a name and exempting everything
    built on it. Under a bare prefix test an entry for ``BMAD_LOOP_MUX_BACKEND``
    would also exempt an unregistered ``BMAD_LOOP_MUX_BACKEND_FALLBACK`` — the
    guard would wave through the very thing it exists to make someone register."""
    return any(key.startswith(e) if e.endswith("_") else key == e for e in entries)


def _env_read_offenders(findings) -> list[tuple[str, int, str, str]]:
    """The env reads no file's declared entries cover — the assertion's whole
    policy, factored out so it can be graded on synthetic findings rather than only
    on today's tree."""
    return [
        (rel, ln, txt, key)
        for _, rel, ln, txt, key in findings
        if not _env_key_allowed(key, ENV_READ_ALLOW.get(rel, ()))
    ]


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """Ids of the string-Constant nodes that are module/class/function docstrings
    — excluded from literal scans (prose, not code)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _classify_posix_path(value: str) -> str | None:
    """The POSIX path this string literal hardcodes, or None. Matches the whole
    value or a subpath of it, so big shell strings that merely *contain*
    ``2>/dev/null`` and lookalikes such as ``~/.gemini/tmp/...`` are not flagged."""
    for pat in POSIX_PATHS:
        if value == pat:
            return pat
        if pat != "/dev/null" and value.startswith(pat + "/"):
            return pat
    return None


def _is_os_environ(node: ast.expr) -> bool:
    """True for the ``os.environ`` / ``os.environb`` attribute access itself.

    ``environb`` is the bytes-keyed twin (POSIX-only, absent on Windows). Nobody
    reaches for it here, but it is the same mapping and costs one string to cover,
    which is cheaper than discovering it later."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr in ("environ", "environb")
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_name_aliases(tree: ast.AST) -> dict[str, str]:
    """``NAME -> "BMAD_LOOP_…"`` for every constant binding in the module, so a read
    spelled through a named constant still resolves. That indirection is the norm
    here, not an edge case: the registry reads ``os.environ.get(MUX_BACKEND)`` and
    gates.py names its notify vars ``_TITLE_ENV`` / ``_MESSAGE_ENV`` — matching the
    string literal alone would miss exactly the well-behaved shape.

    A ``bytes`` constant binds too, since ``os.environb`` can only be keyed by
    bytes: the registry's own constants are ``str`` and would raise there, so a
    bytes literal or a bytes constant are the only two spellings that axis has."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        else:
            continue
        value = node.value
        if not targets or not isinstance(value, ast.Constant):
            continue
        name = value.value
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if isinstance(name, str) and name.startswith(ENV_PREFIX):
            for target in targets:
                aliases[target.id] = name
    return aliases


def _git_name_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """``(head_names, command_names)`` — the names bound anywhere in the module
    to the constant ``"git"`` (a sequence head) and to a string-form git command
    (``"git"`` or a ``"git "`` prefix). The spawn-argv twin of
    ``_env_name_aliases``: a command factored into a named constant
    (``GIT = "git"``, ``GIT_STATUS = "git status"``) is the tidy spelling a
    well-meaning bypass takes, and matching the literal alone would miss exactly
    that shape — in both the sequence and the string branch. ANY binding
    qualifies a name — a later rebind must not launder a spawn that was git
    somewhere in the module — which can only over-flag, and a false positive is
    a review prompt, not a miss. The tmux detector keeps its literal-only head:
    widening that older tripwire is a separate decision from the git chokepoint
    invariant this one enforces."""
    heads: set[str] = set()
    commands: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        value = node.value
        if not targets or not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if value.value == "git":
            heads.update(targets)
        if value.value == "git" or value.value.startswith("git "):
            commands.update(targets)
    return heads, commands


def _env_call_key_node(call: ast.Call) -> ast.expr | None:
    """The node holding the looked-up key: the first positional arg, or the ``key=``
    keyword when the call passes none.

    The keyword form is not hypothetical. ``os.environ`` is ``os._Environ``, a
    Python-level ``MutableMapping``, so its ``get`` / ``pop`` / ``setdefault`` are
    the ABC's plain-Python defs and DO bind ``key=`` — unlike ``dict.get``, whose C
    signature is positional-only and would raise. ``os.getenv(key=...)`` binds for
    the same reason. All four were confirmed against the live interpreter rather
    than assumed, because the dict intuition points the wrong way here."""
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "key":
            return kw.value
    return None


def _env_read_key(node: ast.expr | None, aliases: dict[str, str]) -> str | None:
    """The ``BMAD_LOOP_*`` variable an environment-lookup key names, or None.

    No docstring exclusion here, unlike the POSIX-path scan: that one walks *every*
    string Constant in the tree and so must skip prose, but this one only ever
    inspects a key position (a call's first arg, a subscript's slice). A docstring
    is a standalone ``Expr`` statement and can never appear there, so a
    ``BMAD_LOOP_*`` mention in prose produces no finding to exclude. Verified by
    counting key-position nodes that are also docstring nodes across the whole
    tree: zero. An exclusion here would be unreachable code implying a check that
    is not happening.

    Four spellings resolve, because the interesting violation is the *half-right*
    one: someone who reuses the registry's own constant but skips its reader. A
    literal and a same-module alias were never the risky shapes — reaching for
    ``envvars.MUX_BACKEND`` is, precisely because it looks tidy.

    1. ``os.environ.get("BMAD_LOOP_X")``          — string literal
    2. ``os.environ.get(LOCAL)``                  — bound to a literal here
    3. ``os.environ.get(envvars.MUX_BACKEND)``    — qualified registry attribute
    4. ``os.environ.get(MUX_BACKEND)``            — registry constant imported in

    (3) matches on the attribute name alone rather than proving the object is the
    registry module: `import bmad_loop.envvars as ev` / `from . import envvars`
    and a rebound alias all spell it differently, and resolving that statically
    costs more than it buys. A false positive here is a review prompt on a line
    that reads like an env lookup, not a silent miss — the direction a tripwire
    should fail in."""
    if isinstance(node, ast.Constant):
        # bytes ride along for os.environb's b"BMAD_LOOP_…" keys
        if isinstance(node.value, bytes):
            decoded = node.value.decode("utf-8", "replace")
            return decoded if decoded.startswith(ENV_PREFIX) else None
        if isinstance(node.value, str) and node.value.startswith(ENV_PREFIX):
            return node.value
    if isinstance(node, ast.Name):
        # a same-module binding wins over the registry name it may shadow
        return aliases.get(node.id) or REGISTRY_NAMES.get(node.id)
    if isinstance(node, ast.Attribute):
        return REGISTRY_NAMES.get(node.attr)
    return None


def _scan():
    """Single pass over the tree → list of (kind, rel, lineno, line_text)."""
    findings = []
    for path in _py_files():
        findings.extend(_scan_source(path.read_text(encoding="utf-8"), _rel(path)))
    return findings


def _scan_source(src: str, rel: str):
    """The whole per-file scan, over one source string → the same
    ``(kind, rel, lineno, line_text)`` tuples ``_scan`` collects.

    Split out from ``_scan`` so the detectors can be driven by a snippet and not
    only by what happens to be in the tree today. A repo-wide "nothing is flagged"
    assertion is green both when the invariant holds and when the detector has
    quietly stopped detecting; the probes below feed known-bad sources through
    THIS function — the same code path the real scan uses — so the two failure
    modes stop being indistinguishable."""
    findings = []
    lines = src.splitlines()
    tree = ast.parse(src, filename=rel)
    docs = _docstring_node_ids(tree)
    env_aliases = _env_name_aliases(tree)

    # First positional args of `_run_git(...)` calls — the one position where a
    # git argv literal feeds the chokepoint instead of bypassing it. Collected up
    # front so the walk below can tag each git finding; an argv bound to a name
    # first is deliberately NOT resolved through the binding (same stance as the
    # tuple form: a false positive is a review prompt, not a miss).
    run_git_argvs = {
        id(call.args[0])
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_run_git"
        and call.args
    }
    git_heads, git_commands = _git_name_bindings(tree)

    def line_at(lineno: int) -> str:
        return lines[lineno - 1] if 1 <= lineno <= len(lines) else ""

    for node in ast.walk(tree):
        # spawn-argv literals: ["tmux", ...] / ["git", ...] — each quarantined to
        # its owner. tmux matches lists only: the which-list *tuple*
        # ("tmux", ...) is a real lookup shape in the tree. git matches tuples
        # too — subprocess accepts any sequence, and git has no legitimate tuple
        # form to spare, so the tuple spelling of a bypass must not slip the
        # net. A path segment ("git" outside a sequence) and prose stay silent.
        # A git head also resolves through the module's own constant bindings
        # (`GIT = "git"` — see `_git_name_bindings`), and each git finding carries
        # one extra field: whether the literal sits in the argv position of a
        # `_run_git(...)` call — the only spot the chokepoint file's own
        # exemption covers.
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if (
                isinstance(first, ast.Constant)
                and first.value == "tmux"
                and isinstance(node, ast.List)
            ):
                findings.append(("tmux", rel, node.lineno, line_at(node.lineno)))
            if (isinstance(first, ast.Constant) and first.value == "git") or (
                isinstance(first, ast.Name) and first.id in git_heads
            ):
                findings.append(
                    ("git", rel, node.lineno, line_at(node.lineno), id(node) in run_git_argvs)
                )

        # string-form git spawn: `subprocess.run("git status", shell=True)`, or
        # the same string with no shell — a spelling Windows execs directly. The
        # sequence detector never sees it, and in the SHELL_ALLOW files the
        # shell guard is silent too, so it gets its own anchored check (see
        # SPAWN_CALL_NAMES). "git" exactly or a "git " prefix: `gitk` is a
        # different program. The command resolves through the module's constant
        # bindings the same way a sequence head does (`GIT_STATUS = "git
        # status"` — see `_git_name_bindings`). Never the chokepoint's feed
        # position — `_run_git` takes a sequence — so the extra field is
        # constant False.
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            is_spawn = (
                isinstance(func, ast.Attribute)
                and func.attr in SPAWN_CALL_NAMES
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ) or (isinstance(func, ast.Name) and func.id in SPAWN_CALL_NAMES)
            cmd = node.args[0]
            if is_spawn and (
                (
                    isinstance(cmd, ast.Constant)
                    and isinstance(cmd.value, str)
                    and (cmd.value == "git" or cmd.value.startswith("git "))
                )
                or (isinstance(cmd, ast.Name) and cmd.id in git_commands)
            ):
                findings.append(("git", rel, node.lineno, line_at(node.lineno), False))

        # bare POSIX path string literal (skip docstrings)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and _classify_posix_path(node.value)
        ):
            findings.append(("path", rel, node.lineno, line_at(node.lineno)))

        # signal.SIGKILL attribute access (the guarded form is a "SIGKILL"
        # *string* passed to getattr — not an attribute access — so it's clean)
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "SIGKILL"
            and isinstance(node.value, ast.Name)
            and node.value.id == "signal"
        ):
            findings.append(("sigkill", rel, node.lineno, line_at(node.lineno)))

        # os.kill(<pid>, 0) — the existence-probe form (signal 0), not a real
        # signal send like os.kill(pid, SIGTERM)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "kill"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == 0
            and node.args[1].value is not False
        ):
            findings.append(("killprobe", rel, node.lineno, line_at(node.lineno)))

        # os.kill(...) in any form — every signal send maps to a destructive
        # TerminateProcess on Windows, so confine the call to the ProcessHost.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "kill"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ):
            findings.append(("oskill", rel, node.lineno, line_at(node.lineno)))

        # start_new_session=True as a call kwarg
        if (
            isinstance(node, ast.keyword)
            and node.arg == "start_new_session"
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        ):
            findings.append(("detach", rel, node.lineno, line_at(node.lineno)))

        # {"start_new_session": True} as a dict literal (the detach-kwargs form)
        if isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "start_new_session"
                    and isinstance(val, ast.Constant)
                    and val.value is True
                ):
                    findings.append(("detach", rel, key.lineno, line_at(key.lineno)))

        # shell=True as a call kwarg
        if (
            isinstance(node, ast.keyword)
            and node.arg == "shell"
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        ):
            findings.append(("shell", rel, node.lineno, line_at(node.lineno)))

        # A `BMAD_LOOP_*` variable READ out of the process environment:
        # os.environ.get(K) / os.environ.pop(K) / os.getenv(K) / os.environ[K].
        # Reads only — the env dicts modules *build* to inject into a child
        # session are the producing side, which the invariant does not constrain.
        env_key = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            key_node = _env_call_key_node(node)
            if key_node is None:
                pass
            elif func.attr in ("get", "pop", "setdefault") and _is_os_environ(func.value):
                env_key = _env_read_key(key_node, env_aliases)
            elif (
                # `getenvb` is the bytes twin, and POSIX-only like `environb`
                func.attr in ("getenv", "getenvb")
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                env_key = _env_read_key(key_node, env_aliases)
        elif (
            isinstance(node, ast.Subscript)
            and _is_os_environ(node.value)
            and isinstance(node.ctx, ast.Load)
        ):
            env_key = _env_read_key(node.slice, env_aliases)
        elif isinstance(node, ast.Compare):
            # `"BMAD_LOOP_X" in os.environ` / `not in` — a presence read, and the
            # most natural way to spell a boolean flag. A chain expands PAIRWISE
            # (`c == K in os.environ` means `c == K and K in os.environ`), so the
            # operand a membership tests is the one to its immediate left — the
            # PRECEDING comparator, not `node.left`, for any op past the first.
            # Carry the left operand across the pairs rather than re-reading
            # `node.left`, which resolves the wrong name on a chain.
            left = node.left
            for op, rhs in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and _is_os_environ(rhs):
                    env_key = _env_read_key(left, env_aliases)
                    if env_key:
                        break
                left = rhs
        if env_key:
            # The only 5-wide finding: the allowlist is keyed by variable FAMILY,
            # not by file, so the filter needs the resolved key and not just the
            # source line — a read spelled through a constant does not carry it.
            findings.append(("envread", rel, node.lineno, line_at(node.lineno), env_key))

    # A raw `Path(x.spec_file)` / `Path(x.dispatched_spec_file)`: the persisted value
    # may be worktree-RELATIVE, so this resolves against the reader's cwd rather than
    # the tree the run owns. Detected as the call shape rather than by name, so an
    # alias (`Path(t.spec_file)`, `Path(self._task.dispatched_spec_file)`) is caught
    # too; the enclosing `if x.spec_file else` ternary does not hide it.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Path"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr in SPEC_PATH_FIELDS
        ):
            findings.append(("specanchor", rel, node.lineno, line_at(node.lineno)))

    return findings


FINDINGS = _scan()


def _of(kind: str):
    return [f for f in FINDINGS if f[0] == kind]


def test_no_tmux_invocation_outside_backend():
    """Only the tmux backend may build a ``["tmux", ...]`` argv — every other call
    site goes through the multiplexer seam."""
    offenders = [(rel, ln, txt) for _, rel, ln, txt in _of("tmux") if rel not in TMUX_BACKENDS]
    assert not offenders, (
        "tmux invoked outside the tmux backend (adapters/tmux_base.py, "
        "adapters/tmux_backend.py) — route it through get_multiplexer() instead:\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def _git_offenders(findings) -> list[tuple[str, int, str]]:
    """The chokepoint invariant as a filter: a git argv is sanctioned only in a
    ``GIT_CHOKEPOINT`` file AND only as the argv argument of a ``_run_git(...)``
    call — the file alone is not enough (see the allowlist's comment)."""
    return [
        (rel, ln, txt)
        for _, rel, ln, txt, feeds_chokepoint in findings
        if not (rel in GIT_CHOKEPOINT and feeds_chokepoint)
    ]


def test_no_git_invocation_outside_verify():
    """Only ``verify.py`` may build a ``["git", ...]`` argv, and only to hand it
    to ``_run_git`` — every other call site goes through the chokepoint's helpers
    (``git_bytes`` and siblings), which buy the engine-configured timeout, the
    ``LC_ALL=C`` pin, and the GitError taxonomy. AGENTS.md has stated this since
    the chokepoint existed; nothing enforced it, which is how both #390 bypasses
    survived."""
    offenders = _git_offenders(_of("git"))
    assert not offenders, (
        "git spawned outside the _run_git chokepoint — route it through "
        "verify.git_bytes or a sibling helper instead:\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def test_spec_path_resolved_only_through_the_anchor():
    """A persisted `spec_file` is re-anchored through ``runs.task_spec_path``, never
    resolved with a bare ``Path(...)``, outside the tree-local consumers.

    ``StoryTask._serialized_worktree_path`` persists an isolated unit's spec RELATIVE
    to its mounted worktree and ``from_dict`` reads it back raw, so every reader that
    loads state from disk must say WHICH tree the value is relative to. The four
    allowlisted files run inside that tree already; everything else — the TUI, the
    resolve-context builder, the sweep and stories engines, the read-model
    projections — does not, and the main checkout carries an identical
    ``_bmad-output/specs/...`` layout that answers a bare ``Path(...)`` with the wrong
    copy. That is not a hypothetical: it shipped in ``tui/app.py::_paused_spec``,
    where ``_do_replan`` then WROTE to the main checkout's file and the operator's
    replan silently did not happen.

    This is the guard's whole point — the same defect was found and fixed one surface
    at a time over four review rounds, each round discovering the next unanchored
    reader, because nothing made the rule checkable.

    Ablation: revert ``_paused_spec``'s ``runs.task_spec_path(task, state)`` to
    ``Path(task.spec_file)`` and this reddens naming ``tui/app.py``."""
    offenders = [
        (rel, ln, txt) for _, rel, ln, txt in _of("specanchor") if rel not in SPEC_ANCHOR_CHOKEPOINT
    ]
    assert not offenders, (
        "a persisted spec path resolved against the reader's cwd — route it through "
        "runs.task_spec_path (or StoryTask.rebase_spec_paths_on) so the anchor names "
        "the tree the run owns:\n"
        + "\n".join(f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders)
    )


def test_spec_anchor_detector_flags_the_shipped_defect():
    """The guard above asserts an ABSENCE, so it passes for every reason a match could
    be missing. Feed it the exact line the defect shipped as, through the same
    ``_scan_source`` the real scan uses."""
    found = _scan_source("from pathlib import Path\npath = Path(task.spec_file)\n", "tui/app.py")
    assert [f[0] for f in found if f[0] == "specanchor"] == ["specanchor"]
    # and the dispatched twin, which carries the identical serialization hazard
    found = _scan_source(
        "from pathlib import Path\np = Path(self._task.dispatched_spec_file)\n", "tui/app.py"
    )
    assert [f[0] for f in found if f[0] == "specanchor"] == ["specanchor"]


def test_spec_anchor_detector_stays_silent_on_the_anchored_form():
    """The sanctioned spellings must not trip it, or the guard becomes noise that
    gets allowlisted away."""
    for src in (
        "p = runs.task_spec_path(task, state)\n",
        "task.rebase_spec_paths_on(wt)\n",
        "from pathlib import Path\np = Path(state.project)\n",
    ):
        assert not [f for f in _scan_source(src, "tui/app.py") if f[0] == "specanchor"]


def test_no_hardcoded_posix_paths():
    """No bare ``/tmp`` / ``/proc`` / ``/dev/null`` literal outside the allowlisted
    platform-guarded Unity files; each allowed line carries a `# portability:` ack.
    Use ``os.devnull`` / ``tempfile`` / the psutil fallback instead."""
    bad = []
    for _, rel, ln, txt in _of("path"):
        if rel not in PATH_ALLOW:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (not an allowlisted file)")
        elif ACK not in txt:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (missing '{ACK}' ack)")
    assert not bad, "hardcoded POSIX path(s):\n" + "\n".join(bad)


def test_no_unguarded_sigkill():
    """``signal.SIGKILL`` is absent on Windows — reference it only via the
    ``getattr(signal, "SIGKILL", signal.SIGTERM)`` guard, never as a bare
    attribute access."""
    offenders = _of("sigkill")
    assert not offenders, "unguarded signal.SIGKILL attribute access:\n" + "\n".join(
        f"  {rel}:{ln}: {txt.strip()}" for _, rel, ln, txt in offenders
    )


def test_pid_existence_probe_only_in_liveness_helpers():
    """``os.kill(pid, 0)`` is read-only on POSIX but destructive on Windows
    (TerminateProcess) — confine it to the platform-guarded liveness helpers, each
    line carrying a `# portability:` ack. Other call sites route through
    ``platform_util.pid_alive``."""
    bad = []
    for _, rel, ln, txt in _of("killprobe"):
        if rel not in KILL_PROBE_ALLOW:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (route through platform_util.pid_alive)")
        elif ACK not in txt:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (missing '{ACK}' ack)")
    assert not bad, "os.kill(pid, 0) outside liveness helpers:\n" + "\n".join(bad)


def test_os_kill_only_in_process_host():
    """Any reachable ``os.kill`` maps to a destructive TerminateProcess on Windows —
    confine it to ``process_host.py``. Detects the literal ``os.kill(`` form only;
    import aliases and assigned aliases are deliberately not tracked — this is a
    review tripwire, not a sandbox. Other call sites route through the ProcessHost
    seam (``terminate`` / ``force_kill`` / ``is_alive``)."""
    offenders = [(rel, ln, txt) for _, rel, ln, txt in _of("oskill") if rel not in OS_KILL_ALLOW]
    assert (
        not offenders
    ), "os.kill( outside process_host.py — route it through the ProcessHost seam:\n" + "\n".join(
        f"  {rel}:{ln}: {txt.strip()}" for rel, ln, txt in offenders
    )


def test_start_new_session_only_in_detach_helpers():
    """``start_new_session=True`` is POSIX-only — confine it to the detach helpers
    (which branch on ``sys.platform``), each line carrying a `# portability:` ack."""
    bad = []
    for _, rel, ln, txt in _of("detach"):
        if rel not in DETACH_ALLOW:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (not a detach helper)")
        elif ACK not in txt:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (missing '{ACK}' ack)")
    assert not bad, "start_new_session=True outside detach helpers:\n" + "\n".join(bad)


def test_shell_true_only_in_sanctioned_spots():
    """``shell=True`` only in the two operator-authored-command spots, each line
    carrying a `# portability:` ack."""
    bad = []
    for _, rel, ln, txt in _of("shell"):
        if rel not in SHELL_ALLOW:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (not a sanctioned shell spot)")
        elif ACK not in txt:
            bad.append(f"  {rel}:{ln}: {txt.strip()}  (missing '{ACK}' ack)")
    assert not bad, "shell=True outside verify.py / plugins/bus.py:\n" + "\n".join(bad)


def test_bmad_loop_env_reads_only_in_the_registry():
    """AGENTS.md's env invariant, enforced: "New core env vars register in
    ``envvars.py``; plugin-owned env-var families stay with their plugin." Reading a
    knob inline is what made these undiscoverable before the registry existed, so a
    core module must call an ``envvars`` reader rather than touch ``os.environ``
    itself.

    COVERED — the access shape: ``os.environ.get/pop/setdefault``, ``os.getenv``,
    ``os.environ[K]``, the ``key=`` keyword form of each, and ``K in os.environ`` /
    ``not in`` (including as a link in a chained comparison). Each has a POSIX-only
    bytes twin — ``os.environb`` for the mapping forms, ``os.getenvb`` for the
    function — and both twins are covered.

    Crossed with the key spelling: a literal, a same-module constant,
    ``envvars.MUX_BACKEND``, and the registry constant imported in (see
    ``_env_read_key``). Borrowing the registry's own constant while skipping its
    reader is the *likeliest* violation rather than an exotic one — the
    tidy-looking version is the one that gets written — so every key spelling
    resolves. The bytes twins take only the first two: their keys must be bytes,
    and the registry's constants are ``str``, so that half of the cross product
    cannot be written at all rather than being an uncovered case.

    ``ENV_READ_PROBES`` / ``ENV_READ_NON_PROBES`` are that matrix, executable. Read
    them for what is covered; this docstring only argues the boundary.

    NOT COVERED, deliberately — both obscure the *lookup* rather than the key:
    rebinding the mapping (``e = os.environ; e.get(K)``) and ``from os import
    environ``. This is a review tripwire, not a sandbox: it exists to catch the
    change someone writes while trying to do the right thing, not to withstand
    someone routing around it.

    Bulk copies (``dict(os.environ)``, ``{**os.environ}``) are correctly silent:
    they name no variable, so there is no var being defined outside the registry.

    Scoped to reads. Writes are a different act: engine.py, resolve.py, probe.py,
    plugins/bus.py and unity_plugin.py all BUILD a ``BMAD_LOOP_*`` dict to inject
    into a child session, and gates.py hands notify text to osascript/PowerShell the
    same way — all producing side, none of it a second place a var is *defined*.
    Reads of a SessionSpec's ``spec.env`` (adapters/generic.py) are likewise out:
    that is a plain dict handed down in-process, not the environment.

    ⚠️ THIS assertion cannot grade the detector. It says only that today's tree
    carries no unallowlisted finding — equally green when the scan has silently
    stopped scanning. Delete any single branch of the ``envread`` detector and this
    test still passes while exactly the matching ``ENV_READ_PROBES`` rows redden.
    The assertion is the invariant; the probes are the proof it is being checked,
    and neither replaces the other. What this test does grade alone is the
    allowlist: empty ``ENV_READ_ALLOW`` and it fails naming every real read, so a
    green run means the scan saw those reads rather than that it found nothing.

    ⚠️ The prose row in ``ENV_READ_NON_PROBES`` is a CONTROL, not an ablation. A
    ``BMAD_LOOP_*`` mention in a docstring creates no key-position node at all, so
    it stays silent no matter what the detector does — an earlier revision cited it
    as proof of a docstring exclusion in ``_env_read_key`` that was in fact
    unreachable. Keep the row for the property, never as evidence.

    ⚠️ New uncovered shapes keep surfacing here, and that is a property of the
    design rather than a run of bad luck: this is a denylist of access forms, so it
    is only ever as complete as the last sweep over them, and the NOT COVERED list
    is the honest boundary rather than an oversight. Sweep an axis when you touch
    it — every mapping form at once, not the one that prompted the visit — and
    extend the matrix before the branch: add the probe row, watch it fail, then fix
    the scan.

    The exemption is scoped by variable FAMILY, not by file — see
    ``ENV_READ_ALLOW`` for why, and ``ENV_SCOPE_CASES`` for that claim as rows
    rather than prose."""
    offenders = _env_read_offenders(_of("envread"))
    assert not offenders, (
        "BMAD_LOOP_* read outside envvars.py and the families each stand-alone "
        "script owns — name the var in envvars.py and call its reader instead of "
        "widening the allowlist:\n"
        + "\n".join(f"  {rel}:{ln}: {key} — {txt.strip()}" for rel, ln, txt, key in offenders)
    )


# Every access form the env-read detector claims to cover, as a source snippet that
# MUST produce an `envread` finding. These are the executable half of the matrix the
# test above documents: that test asserts only that today's tree is clean, which stays
# green both when the invariant holds and when the detector has silently stopped
# detecting. Driving known-bad sources through the real `_scan_source` separates
# those. Snippets are parsed, never imported, so a nonexistent relative import is fine.
# Fix order when a new form turns up: add the row here FIRST and watch it fail.
ENV_READ_PROBES = [
    ("get-literal", 'import os\nX = os.environ.get("BMAD_LOOP_X")\n'),
    ("get-local-const", 'import os\nK = "BMAD_LOOP_X"\nX = os.environ.get(K)\n'),
    (
        "get-qualified-registry",
        "import os\nfrom . import envvars\nX = os.environ.get(envvars.MUX_BACKEND)\n",
    ),
    (
        "get-aliased-registry",
        "import os\nfrom . import envvars as ev\nX = os.environ.get(ev.MUX_BACKEND)\n",
    ),
    (
        "get-imported-registry",
        "import os\nfrom .envvars import MUX_BACKEND\nX = os.environ.get(MUX_BACKEND)\n",
    ),
    ("getenv", 'import os\nX = os.getenv("BMAD_LOOP_X")\n'),
    ("subscript", 'import os\ndef f():\n    return os.environ["BMAD_LOOP_X"]\n'),
    ("getenv-keyword", 'import os\nX = os.getenv(key="BMAD_LOOP_X")\n'),
    ("get-keyword", 'import os\nX = os.environ.get(key="BMAD_LOOP_X")\n'),
    ("pop-keyword", 'import os\ndef f():\n    return os.environ.pop(key="BMAD_LOOP_X")\n'),
    ("setdefault-keyword", 'import os\nX = os.environ.setdefault(key="BMAD_LOOP_X", value="v")\n'),
    ("membership-in", 'import os\nX = "BMAD_LOOP_X" in os.environ\n'),
    ("membership-not-in", 'import os\nX = "BMAD_LOOP_X" not in os.environ\n'),
    (
        "membership-registry",
        "import os\nfrom .envvars import MUX_BACKEND\nX = MUX_BACKEND in os.environ\n",
    ),
    # A chain, where the membership's left operand is the PRECEDING comparator.
    # Keyed by a literal on purpose: the registry row above already grades key
    # resolution, so this row reddens for one reason only — chain position.
    ("membership-chained", 'import os\nc = "x"\nX = c == "BMAD_LOOP_X" in os.environ\n'),
    # The `os.environb` axis, swept rather than sampled: every mapping form above
    # has a bytes twin, and `os.getenvb` is the twin of `os.getenv`. Keys here are a
    # bytes literal or a bytes constant, which is the whole spelling axis — the
    # registry's constants are `str` and would raise against a bytes mapping.
    ("environb-get", 'import os\nX = os.environb.get(b"BMAD_LOOP_X")\n'),
    ("environb-subscript", 'import os\ndef f():\n    return os.environb[b"BMAD_LOOP_X"]\n'),
    ("environb-local-const", 'import os\nK = b"BMAD_LOOP_X"\nX = os.environb.get(K)\n'),
    ("environb-pop-keyword", 'import os\nX = os.environb.pop(key=b"BMAD_LOOP_X")\n'),
    ("environb-setdefault", 'import os\nX = os.environb.setdefault(b"BMAD_LOOP_X", b"v")\n'),
    ("environb-membership", 'import os\nX = b"BMAD_LOOP_X" in os.environb\n'),
    (
        "environb-membership-chained",
        'import os\nc = b"y"\nX = c == b"BMAD_LOOP_X" in os.environb\n',
    ),
    ("getenvb", 'import os\nX = os.getenvb(b"BMAD_LOOP_X")\n'),
    ("getenvb-keyword", 'import os\nX = os.getenvb(key=b"BMAD_LOOP_X")\n'),
]

# The other half: shapes that must stay SILENT. Without these the detector could pass
# every probe above by flagging everything, which would be just as broken — a guard
# that cries wolf gets its allowlist widened until it means nothing.
ENV_READ_NON_PROBES = [
    ("bulk-dict-copy", "import os\nX = dict(os.environ)\n"),
    ("bulk-splat-copy", "import os\nX = {**os.environ}\n"),
    ("bulk-copy-method", "import os\nX = os.environ.copy()\n"),
    ("foreign-var", 'import os\nX = os.environ.get("PATH")\n'),
    (
        "prose-in-docstring",
        'import os\ndef f():\n    """Injects BMAD_LOOP_X downstream."""\n    return 1\n',
    ),
    ("write-not-read", 'import os\nos.environ["BMAD_LOOP_X"] = "1"\n'),
    ("session-spec-env", 'def f(spec):\n    return spec.env.get("BMAD_LOOP_X")\n'),
]


@pytest.mark.parametrize(("label", "source"), ENV_READ_PROBES, ids=[p[0] for p in ENV_READ_PROBES])
def test_env_read_detector_flags_every_claimed_access_form(label, source):
    """Each documented access form really does produce a finding.

    This is the check the repo-wide assertion cannot be: delete any single branch of
    the `envread` scan and the tree-wide test stays green (nothing in `src/` uses that
    branch today), while exactly the matching row here reddens. The coverage claim
    lives here rather than in the guard's docstring alone, because prose does not
    fail a build."""
    found = [f for f in _scan_source(source, "probe.py") if f[0] == "envread"]
    assert found, (
        f"the {label!r} access form produced no `envread` finding — the detector does "
        f"not cover a shape the guard's docstring claims:\n{source}"
    )


@pytest.mark.parametrize(
    ("label", "source"), ENV_READ_NON_PROBES, ids=[p[0] for p in ENV_READ_NON_PROBES]
)
def test_env_read_detector_stays_silent_on_non_reads(label, source):
    """The complement: a bulk environment copy, a foreign variable, prose, a WRITE,
    and a `SessionSpec.env` lookup are all silent. Pins the scoping decisions the
    guard's docstring argues for, so narrowing or widening the detector has to be
    deliberate — and stops a future fix from passing the probes by flagging
    everything."""
    found = [f for f in _scan_source(source, "probe.py") if f[0] == "envread"]
    assert not found, (
        f"the {label!r} shape was flagged as an env read; it is deliberately out of "
        f"scope:\n{source}"
    )


# The git-argv detector's probe matrix, same rationale as the env pair above:
# nothing in `src/` builds a bare ["git", ...] today, so deleting the detector
# branch leaves the tree-wide guard green — only these rows redden.
GIT_ARGV_PROBES = [
    ("bare-run", 'import subprocess\nsubprocess.run(["git", "-C", str(p), "status"])\n'),
    ("bare-popen", 'import subprocess\nsubprocess.Popen(["git", "ls-files"])\n'),
    ("argv-built-first", 'argv = ["git", "log", "-1"]\n'),
    # subprocess accepts any sequence, so the tuple spelling is a legal spawn —
    # unlike tmux there is no which-tuple shape to spare, so it is flagged even
    # unattached to a call (a false positive is a review prompt, not a miss).
    ("tuple-argv", 'import subprocess\nsubprocess.run(("git", "status"))\n'),
    # The executable factored into a named constant — the head resolves through
    # the module's own bindings, as the env detector's aliases do.
    (
        "named-executable",
        'import subprocess\nGIT = "git"\nsubprocess.run([GIT, "status"])\n',
    ),
    # …and a rebind does not launder it: any binding to "git" qualifies the name.
    (
        "named-executable-rebound",
        'import subprocess\nGIT = "git"\nGIT = "other"\nsubprocess.run([GIT, "status"])\n',
    ),
    # The string spellings: a shell command, and the same string with no shell —
    # which Windows execs directly — plus the from-import spawn name.
    (
        "string-shell",
        'import subprocess\nsubprocess.run("git status", shell=True)\n',
    ),
    (
        "string-no-shell",
        'import subprocess\nsubprocess.Popen("git -C . log")\n',
    ),
    (
        "string-from-import",
        'from subprocess import run\nrun("git status", shell=True)\n',
    ),
    # …and the string command factored into a constant resolves the same way a
    # sequence head does — the last cell of the spelling matrix
    # ({sequence, string} × {inline, named}).
    (
        "string-named-command",
        'import subprocess\nGIT_STATUS = "git status"\nsubprocess.run(GIT_STATUS, shell=True)\n',
    ),
]
GIT_ARGV_NON_PROBES = [
    ("path-segment", 'from pathlib import Path\nX = Path(h) / "git" / "ignore"\n'),
    ("prose-in-docstring", 'def f():\n    """Runs `git add -A` downstream."""\n    return 1\n'),
    ("chokepoint-args-tail", 'proc = git_bytes(repo, "ls-files", "-z")\n'),
    # A named head that binds to a DIFFERENT executable, and one that never binds
    # at all (a parameter), stay silent — the alias reach is exactly the names
    # the module itself ties to "git".
    (
        "named-other-executable",
        'import subprocess\nRG = "rg"\nsubprocess.run([RG, "--files"])\n',
    ),
    (
        "named-unbound-head",
        'import subprocess\ndef run(exe):\n    return subprocess.run([exe, "status"])\n',
    ),
    # The string check anchors on spawn calls and on the word boundary: a git
    # command in a NON-spawn call (the message shape — an exception, a logger)
    # and a different program that merely starts with "git" both stay silent.
    (
        "string-in-message-call",
        'raise RuntimeError("git status failed")\n',
    ),
    (
        "string-other-program",
        'import subprocess\nsubprocess.run("gitk", shell=True)\n',
    ),
    # The named-command reach is exactly the strings the module ties to git:
    # a different command and a "git"-prefixed different program stay silent
    # through the alias path too.
    (
        "string-named-other-command",
        'import subprocess\nLS = "ls -la"\nsubprocess.run(LS, shell=True)\n',
    ),
    (
        "string-named-other-program",
        'import subprocess\nGITK = "gitk"\nsubprocess.run(GITK, shell=True)\n',
    ),
]


@pytest.mark.parametrize(("label", "source"), GIT_ARGV_PROBES, ids=[p[0] for p in GIT_ARGV_PROBES])
def test_git_argv_detector_flags_every_spawn_shape(label, source):
    """Each spawn shape produces a `git` finding — including an argv bound to a
    name first, which is how a bypass would most tidily be written."""
    found = [f for f in _scan_source(source, "probe.py") if f[0] == "git"]
    assert found, f"the {label!r} shape produced no `git` finding:\n{source}"


@pytest.mark.parametrize(
    ("label", "source"), GIT_ARGV_NON_PROBES, ids=[p[0] for p in GIT_ARGV_NON_PROBES]
)
def test_git_argv_detector_stays_silent_on_lookalikes(label, source):
    """The complement: a path segment, prose, and the chokepoint's own args-tail
    name git without building an argv — flagging them would get the allowlist
    widened until it means nothing."""
    found = [f for f in _scan_source(source, "probe.py") if f[0] == "git"]
    assert not found, f"the {label!r} shape was flagged; it is not a git argv:\n{source}"


# The git exemption's scoping, as rows: `(rel, source, is_offender)`. Every git
# argv in verify.py today already sits in a `_run_git(...)` call, so a file-wide
# filter and the call-position one are indistinguishable on the real tree — only
# synthetic sources can tell them apart.
GIT_SCOPE_CASES = [
    # The hole a file-wide exemption leaves open: a verify.py helper spawning git
    # directly, past the timeout, the locale pin, and the GitError taxonomy.
    (
        "verify-bare-spawn",
        "verify.py",
        'import subprocess\nsubprocess.run(["git", "status"])\n',
        True,
    ),
    # The tuple spelling of the same bypass stays refused inside the file too.
    (
        "verify-bare-tuple",
        "verify.py",
        'import subprocess\nsubprocess.run(("git", "status"))\n',
        True,
    ),
    # …while the chokepoint's real feed line stays exempt: the argv as
    # `_run_git`'s first argument, the shape of every sanctioned site today.
    (
        "verify-chokepoint-arg",
        "verify.py",
        'proc = _run_git(["git", "-C", str(repo), "status"], repo)\n',
        False,
    ),
    # An argv bound to a name first is flagged even en route to `_run_git` — the
    # detector's documented stance (a false positive is a review prompt, not a
    # miss), and today's tree has no such site to spare.
    (
        "verify-argv-built-first",
        "verify.py",
        'argv = ["git", "log", "-1"]\nproc = _run_git(argv, repo)\n',
        True,
    ),
    # The private spelling does not travel: `_run_git` imported into another
    # module is an offender there, argv position notwithstanding.
    (
        "engine-calls-run-git",
        "engine.py",
        'proc = _run_git(["git", "fetch"], repo)\n',
        True,
    ),
    # The string form is refused inside verify.py too — there `shell=True` is
    # allowlisted (SHELL_ALLOW), so without this the spelling would slip both
    # tripwires at once; it can never be the chokepoint's feed position, since
    # `_run_git` takes a sequence.
    (
        "verify-string-shell",
        "verify.py",
        'import subprocess\nsubprocess.run("git status", shell=True)\n',
        True,
    ),
]


@pytest.mark.parametrize(
    ("label", "rel", "source", "is_offender"),
    GIT_SCOPE_CASES,
    ids=[c[0] for c in GIT_SCOPE_CASES],
)
def test_git_argv_exemption_is_scoped_to_the_chokepoint_call(label, rel, source, is_offender):
    """Being verify.py buys the file its `_run_git(...)` feed lines and nothing
    wider. Without this, `_git_offenders` could go back to exempting the file
    wholesale and every assertion in this file would stay green — the difference
    only shows up on a bypass that does not exist yet, which is the only kind a
    tripwire is for."""
    offenders = _git_offenders([f for f in _scan_source(source, rel) if f[0] == "git"])
    assert bool(offenders) is is_offender, (
        f"a git argv in {rel} here should {'be refused' if is_offender else 'be allowed'}:\n"
        f"{source}"
    )


# The allowlist's scoping, as rows: `(rel, key, is_offender)`. Same reason the
# access-form matrix is executable — a file-scoped exemption and a family-scoped one
# are indistinguishable on today's tree, where every read already sits inside its
# own family, so only synthetic findings can tell them apart.
ENV_SCOPE_CASES = [
    # A core knob read inline from a file that is exempt for OTHER reasons. This is
    # the case a file-wide allowlist drops on the path alone.
    ("unity-reads-core-knob", "data/plugins/unity/unity_ready.py", "BMAD_LOOP_MUX_BACKEND", True),
    ("hook-reads-core-knob", "data/bmad_loop_hook.py", "BMAD_LOOP_SESSION_TIMEOUT_S", True),
    # The registry is scoped to the names it defines, not to the prefix at large.
    ("registry-reads-session-var", "envvars.py", "BMAD_LOOP_RUN_DIR", True),
    # …and the reads each file genuinely owns stay exempt.
    ("unity-reads-own-family", "data/plugins/unity/unity_ready.py", "BMAD_LOOP_UNITY_PATH", False),
    (
        "unity-reads-engine-family",
        "data/plugins/unity/unity_setup.py",
        "BMAD_LOOP_ENGINE_MCP",
        False,
    ),
    ("unity-reads-session-var", "data/plugins/unity/unity_cleanup.py", "BMAD_LOOP_WORKTREE", False),
    ("hook-reads-session-var", "data/bmad_loop_hook.py", "BMAD_LOOP_RUN_DIR", False),
    ("registry-reads-own-name", "envvars.py", "BMAD_LOOP_MUX_BACKEND", False),
    # A non-allowlisted core module is refused whatever the key.
    ("core-module-any-key", "verify.py", "BMAD_LOOP_RUN_DIR", True),
    # An entry naming ONE variable must not exempt every longer name built on it,
    # or a new unregistered knob rides in on a registered one's spelling.
    ("registry-name-extended", "envvars.py", "BMAD_LOOP_MUX_BACKEND_FALLBACK", True),
    ("session-name-extended", "data/bmad_loop_hook.py", "BMAD_LOOP_RUN_DIR_EXTRA", True),
    # …while a real family (trailing underscore) still covers a member it has
    # never seen, which is what makes it a family rather than a list.
    (
        "unity-family-unseen-member",
        "data/plugins/unity/unity_ready.py",
        "BMAD_LOOP_UNITY_NEW",
        False,
    ),
]


@pytest.mark.parametrize(
    ("label", "rel", "key", "is_offender"),
    ENV_SCOPE_CASES,
    ids=[c[0] for c in ENV_SCOPE_CASES],
)
def test_env_read_allowlist_is_scoped_by_family_not_by_file(label, rel, key, is_offender):
    """Being allowlisted buys a file its own variable families and nothing wider.

    Without this, `ENV_READ_ALLOW` could go back to a set of paths and every
    assertion in this file would stay green — the distinction only shows up on a
    read that does not exist yet, which is the only kind a tripwire is for."""
    offenders = _env_read_offenders([("envread", rel, 1, f"os.environ.get({key!r})", key)])
    assert bool(offenders) is is_offender, (
        f"{rel} reading {key} should {'be refused' if is_offender else 'be allowed'}; "
        f"declared families for that file: {ENV_READ_ALLOW.get(rel, ())}"
    )


def test_guard_actually_scanned_files():
    """Sanity: the scan walked a non-trivial number of files (catches a broken
    SRC root silently passing every assertion)."""
    assert len(_py_files()) > 20
